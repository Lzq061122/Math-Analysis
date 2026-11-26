# -*- coding: utf-8 -*-
"""R2-02 后端验收：批量核验调度、教师评分与评分标准、失效链、错题同步与知识点分类。

命令：`C:\\Python314\\python.exe tests\\verify_r2_02_grading.py`
日志：`docs/verify-r2-02-grading.txt`

断言口径（全部走真实业务路径：真库 + 真表 + 真 answer_check 核验链，不用桩替代被测逻辑）：
  1  结构与批次配置：9 张 R2 表就绪、批次配置读写、未配置批次不出分
  2  四维读取：作答/核验/错因/评分各自独立，判据来自 r2_contract
  3  批量核验：按批次 / 全部待核验 / 指定记录；逐条保存、逐条回调、可取消、可重试、
     单条失败不阻塞；重复核验**不覆盖**人工核验 / 已判错因 / 已确认成绩（含反向自证）
  4  错因：候选 → 确认 → 撤回；撤回只让 cause→undecided，核验与评分独立保留（R2-INV-03）
  5  评分：满分 10 录 7.5 通过；11、负数、三位小数、超范围一律拒绝；草稿不计正式统计；
     步骤扣分与总分不一致拒绝且不改库（R2-INV-07）；改已确认分要理由；撤回退出正式统计
  6  失效链：改作答内容 → 核验 stale + 错因 undecided + 评分 invalidated（R2-INV-01）；
     改评分标准必须同时改 rubric_version，否则旧成绩不失效（R2-INV-05，含反向自证）
  7  错题与知识点：确认错因 → 错题行；缺测/内容不足/无法核验**零错题行**；
     知识点写入 mistakes.knowledge_points，原版学情页面可查询；撤回错因错题退回（K1/K2/K3）
  8  只读接口：current_checks / current_scores / effective_rows / valid_verifications /
     confirmed_causes / filter_options / score_sheet / scoring_progress（覆盖率分母=应参与评分学生数）
  9  外发闸门：任何 provider/model 一律 403 ai_isolation，且打开 ai_individual_allowed 仍拒绝
 10  R1 分析生成的错题：经 R1 更新/校正/撤回后下游不留失效判断

反向自证（每条都验证「去掉这道闸，断言必红」）：
  - 3.13/3.14 重复核验保护：裸调 answer_check.save 会清掉错因，走 verify_one 不会
  - 5.15/5.16 步骤分一致性：把 step_deductions_consistent 换成恒真后同一保存会成功
  - 6.7/6.8   改标准失效：只改正文不改版本时旧成绩仍是 confirmed，走 update_rubric 才失效
  - 7.7/7.8   知识点查询：未分类（[]）时原版统计查不到，确认后能查到
"""
import json
import os
import shutil
import sys
from contextlib import contextmanager

try:  # Windows 控制台默认 GBK，日志里有「−」等符号时必须显式切 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 老解释器没有 reconfigure
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import answer_check  # noqa: E402
from app import answer_review  # noqa: E402
from app import db as db_mod  # noqa: E402
from app import grading  # noqa: E402
from app import r2_contract  # noqa: E402
from app import stats as stats_mod  # noqa: E402
from app.db import Database, now_iso  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r2-02")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-02-grading.txt")
LINES = []
RESULTS = {"pass": 0, "fail": 0}

QTEXT = "解方程 2x+1=5"
SOLUTION = "x=2"
STEPS_OK = "2x+1=5\n2x=4\nx=2"
STEPS_BAD = "2x+1=5\n2x=6\nx=3"
BATCH = "第一次作业"
UID = 1


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s  %s" % (name, detail))
    return bool(ok)


def section(title):
    log("")
    log("== %s ==" % title)


@contextmanager
def patched(obj, name, replacement):
    """临时替换一个函数（反向自证用），退出时无条件还原。"""
    original = getattr(obj, name)
    setattr(obj, name, replacement)
    try:
        yield original
    finally:
        setattr(obj, name, original)


def caught(fn, *args, **kwargs):
    """调用应当抛 GradingError 的接口，返回异常本身（没抛则返回 None）。"""
    try:
        fn(*args, **kwargs)
        return None
    except grading.GradingError as exc:
        return exc


# ---------------------------------------------------------------------------
# 环境构造（真库、真表、真作答）
# ---------------------------------------------------------------------------


def make_env(name, answers, students=None, batch=BATCH, configure=True,
             questions=("1",), full_scores=None, rubric=None, version="v1",
             extra_students=0):
    """建一份临时库并写入用户/班级/学生/作答；返回 env 字典。

    answers 每项：{"sid":1,"qno":"1","steps":STEPS_OK,"missing":0,"images":0,
                   "source":"作业","qtext":QTEXT,"sol":SOLUTION,"occurred_on":""}
    """
    path = os.path.join(WORK, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    db_file = os.path.join(path, "app.db")
    db = Database(db_file)
    now = now_iso()
    students = students if students is not None else max([a["sid"] for a in answers] + [1])
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id, username, display_name, pwd_salt, pwd_hash, iterations,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (UID, "teacher", "张老师", "salt", "hash", 1, now),
        )
        conn.execute(
            "INSERT INTO classes(id, user_id, name, created_at, updated_at)"
            " VALUES (1,?,'初一(1)班',?,?)", (UID, now, now))
        for sid in range(1, students + 1):
            conn.execute(
                "INSERT INTO students(id, class_id, name, code, created_at, updated_at)"
                " VALUES (?,1,?,?,?,?)", (sid, "学生%d" % sid, "S%d" % sid, now, now))
        for index, item in enumerate(answers, 1):
            aid = item.get("id") or index
            conn.execute(
                "INSERT INTO answers(id, class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, 1, item["sid"], item.get("key") or "R-%d" % aid, item.get("batch") or batch,
                 "", "S%d" % item["sid"], item.get("qno") or "1",
                 item.get("qtext", QTEXT), item.get("sol", SOLUTION), item.get("steps", STEPS_OK),
                 item.get("source") or "作业", item.get("occurred_on") or "",
                 int(item.get("missing") or 0), item.get("missing_reason") or "",
                 item.get("fingerprint") or "fp-%d" % aid, now, now, now),
            )
            for seq in range(1, int(item.get("images") or 0) + 1):
                conn.execute(
                    "INSERT INTO answer_images(class_id, student_id, answer_id, seq, file_path,"
                    " mime, sha256, source, note, created_at) VALUES (1,?,?,?,?,?,?,?,?,?)",
                    (item["sid"], aid, seq, "answer-%d-%d.png" % (aid, seq), "image/png",
                     "sha%d-%d" % (aid, seq), "zip", "", now),
                )
    class_row = {"id": 1, "name": "初一(1)班"}
    if configure:
        roster = [{"key": "S%d" % sid, "name": "学生%d" % sid, "code": "S%d" % sid}
                  for sid in range(1, students + 1)]
        for sid in range(students + 1, students + extra_students + 1):
            roster.append({"key": "S%d" % sid, "name": "学生%d" % sid, "code": "S%d" % sid})
        full = full_scores or {q: 10 for q in questions}
        config = {
            "name": batch,
            "occurred_on": "2026-11-20",
            "origin": "manual",
            "config_state": "configured",
            "expected_students": roster,
            "expected_questions": list(questions),
            "scored_questions": list(questions),
            "full_scores": full,
            "rubric": rubric or {q: "解方程各步：移项、合并、系数化 1" for q in questions},
            "rubric_version": version,
        }
        db_mod.save_batch_config(db, 1, batch, config, user_id=UID)
    return {"db": db, "class_row": class_row, "user": UID, "batch": batch,
            "path": path, "db_file": db_file, "students": students}


def mistake_rows(env, status=None):
    sql = "SELECT * FROM mistakes WHERE class_id=1"
    params = ()
    if status:
        sql += " AND status=?"
        params = (status,)
    return [dict(row) for row in env["db"].query(sql + " ORDER BY id", params)]


def check_row(env, answer_id):
    return env["db"].query_one(
        "SELECT * FROM answer_checks WHERE answer_id=? AND answer_version="
        "(SELECT fingerprint FROM answers WHERE id=?) ORDER BY id DESC LIMIT 1",
        (answer_id, answer_id))


def score_row(env, answer_id, question_no="1"):
    return env["db"].query_one(
        "SELECT * FROM answer_scores WHERE answer_id=? AND question_no=? ORDER BY id DESC LIMIT 1",
        (answer_id, question_no))


def verify_all(env, batch=None, scope=None, **kwargs):
    return grading.batch_verify(env["db"], env["class_row"], UID, batch or env["batch"],
                                scope=scope or grading.SCOPE_PENDING, **kwargs)


# ---------------------------------------------------------------------------
# 一、结构与批次配置
# ---------------------------------------------------------------------------


def section_structure():
    section("1. 结构与批次配置")
    env = make_env("01-structure", [
        {"sid": 1, "steps": STEPS_OK},
        {"sid": 2, "steps": STEPS_BAD},
    ])
    db = env["db"]
    ready = db_mod.r2_schema_ready(env["db_file"])
    check("1.1 R2 结构就绪（9 表 + 索引齐备）", ready is True, repr(ready))
    tables = {row["name"] for row in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [name for name in r2_contract.R2_TABLE_NAMES if name not in tables]
    check("1.2 9 张 R2 表全部落库", not missing, ",".join(missing))
    config = db_mod.load_batch_config(db, 1, BATCH)
    check("1.3 批次配置读回：configured + 满分 10 + 标准版本 v1",
          config["config_state"] == "configured"
          and r2_contract.full_score_of(config, "1") == 10
          and config["rubric_version"] == "v1", json.dumps(config, ensure_ascii=False)[:120])
    plain = db_mod.load_batch_config(db, 1, "没有登记的批次")
    check("1.4 未登记批次是 unconfigured 且满分 None（显示暂无，不猜 100）",
          plain["config_state"] == "unconfigured"
          and r2_contract.full_score_of(plain, "1") is None)
    check("1.5 加分前：scored_question_nos 取配置",
          r2_contract.scored_question_nos(config) == ("1",))
    dims = grading.answer_dimensions(db, env["class_row"], 1)
    check("1.6 四维空值：作答 valid / 核验 unverified / 错因 undecided / 评分 unscored",
          dims["dimensions"]["answer"]["state"] == "valid"
          and dims["dimensions"]["verify"]["state"] == "unverified"
          and dims["dimensions"]["cause"]["state"] == "undecided"
          and dims["dimensions"]["score"]["state"] == "unscored",
          json.dumps(dims["dimensions"], ensure_ascii=False))


# ---------------------------------------------------------------------------
# 二、四维读取
# ---------------------------------------------------------------------------


def section_dimensions():
    section("2. 四维读取（作答/核验/错因/评分互相独立）")
    env = make_env("02-dims", [
        {"sid": 1, "steps": STEPS_OK},
        {"sid": 2, "steps": STEPS_BAD},
        {"sid": 3, "steps": "", "missing": 1, "missing_reason": "病假"},
        {"sid": 1, "steps": "", "images": 1, "qno": "1", "key": "R-4"},
    ])
    db, cls = env["db"], env["class_row"]
    verify_all(env)
    states = {aid: grading.answer_dimensions(db, cls, aid)["dimensions"] for aid in (1, 2, 3, 4)}
    check("2.1 正确作答 → 核验 verified_rule + 错因 not_applicable",
          states[1]["verify"]["state"] == "verified_rule"
          and states[1]["cause"]["state"] == "not_applicable",
          json.dumps(states[1], ensure_ascii=False))
    check("2.2 错误作答 → 核验 verified_rule + 错因 undecided（可归因待判断）",
          states[2]["verify"]["state"] == "verified_rule"
          and states[2]["cause"]["state"] == "undecided")
    check("2.3 缺测 → 作答 missing（R1 的 missing 优先于内容量）",
          states[3]["answer"]["state"] == "missing", states[3]["answer"])
    check("2.4 仅照片 → 作答 insufficient（不猜内容够不够）",
          states[4]["answer"]["state"] == "insufficient", states[4]["answer"])
    check("2.5 缺测与仅照片都不能判错因（无需判断）",
          states[3]["cause"]["state"] == "not_applicable"
          and states[4]["cause"]["state"] == "not_applicable")
    check("2.6 评分维与核验维互不影响（error: unscored；缺测: unscored）",
          states[2]["score"]["state"] == "unscored"
          and states[3]["score"]["state"] == "unscored")
    err = caught(grading.answer_dimensions, db, cls, 999)
    check("2.7 跨班/不存在的作答按 404 拒绝（跨班不可见）",
          err is not None and err.status == 404 and err.code == "answer_not_found")
    # 另一个班级的作答
    with db.tx() as conn:
        now = now_iso()
        conn.execute(
            "INSERT INTO classes(id, user_id, name, created_at, updated_at)"
            " VALUES (2,?,'初二(9)班',?,?)", (UID, now, now))
        conn.execute(
            "INSERT INTO students(id, class_id, name, code, created_at, updated_at)"
            " VALUES (99,2,'外班学生','S99',?,?)", (now, now))
        conn.execute(
            "INSERT INTO answers(id, class_id, student_id, record_key, batch, question_no,"
            " question_text, standard_solution, student_steps, source, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (99,2,99,'R-99',?,'1',?,?,?,'作业','fp-99',?,?,?)",
            (BATCH, QTEXT, SOLUTION, STEPS_OK, now, now, now))
    err = caught(grading.answer_dimensions, db, cls, 99)
    check("2.8 同名批次的他班作答不可见（class_id 收口）",
          err is not None and err.status == 404)


# ---------------------------------------------------------------------------
# 三、批量核验
# ---------------------------------------------------------------------------


def section_batch_verify():
    section("3. 批量核验调度")
    env = make_env("03-verify", [
        {"sid": 1, "steps": STEPS_OK},
        {"sid": 2, "steps": STEPS_BAD},
        {"sid": 3, "steps": STEPS_OK, "key": "R-3"},
        {"sid": 3, "steps": STEPS_BAD, "key": "R-4"},
    ])
    db, cls = env["db"], env["class_row"]
    queue = grading.batch_rows(db, cls, BATCH, scope=grading.SCOPE_PENDING)
    check("3.1 待核验队列含全部 4 条，顺序按 answers.id", [r["answer_id"] for r in queue] == [1, 2, 3, 4],
          repr([r["answer_id"] for r in queue]))
    check("3.2 队列行带四维状态与 source/batch（统计不必再猜）",
          all(row["source"] == "作业" and row["batch"] == BATCH for row in queue)
          and queue[0]["dimensions"]["verify"]["state"] == "unverified")
    progress = grading.batch_progress(db, cls, BATCH)
    check("3.3 核验前进度：pending=4 / verified=0", progress["pending"] == 4 and progress["verified"] == 0,
          json.dumps(progress, ensure_ascii=False))
    seen = []
    summary = verify_all(env, on_result=lambda r: seen.append((r["index"], r["answer_id"], r["status"])))
    check("3.4 逐条核验：4 条全部 verified，失败 0",
          summary["total"] == 4 and summary["verified"] == 4 and summary["failed"] == 0,
          json.dumps({k: summary[k] for k in ("total", "verified", "skipped", "failed")},
                     ensure_ascii=False))
    check("3.5 逐条回调按进度顺序触发（界面进度条数据源）",
          [item[0] for item in seen] == [1, 2, 3, 4], repr(seen))
    again = grading.batch_rows(db, cls, BATCH, scope=grading.SCOPE_PENDING)
    check("3.6 重复点击核验：已核验的不再进待核验队列", again == [], repr(again))
    check("3.7 结果逐条落库（4 条当前版本结论都在库里）",
          sum(1 for aid in (1, 2, 3, 4) if check_row(env, aid)) == 4)
    ids_rows = grading.batch_rows(db, cls, "", scope=grading.SCOPE_IDS, answer_ids=[4, 2])
    check("3.8 指定记录模式只取给定 id，且按传入顺序",
          [row["answer_id"] for row in ids_rows] == [4, 2],
          repr([row["answer_id"] for row in ids_rows]))
    # 取消：处理 1 条后停
    env2 = make_env("03-cancel", [{"sid": 1, "steps": STEPS_OK}, {"sid": 2, "steps": STEPS_BAD},
                                  {"sid": 3, "steps": STEPS_OK}, {"sid": 1, "steps": STEPS_BAD,
                                                                  "key": "R-4"}])
    counter = {"n": 0}

    def cancel_after_one():
        counter["n"] += 1
        return counter["n"] > 1

    summary = verify_all(env2, cancel_check=cancel_after_one)
    check("3.9 取消：只处理 1 条，其余留作未处理",
          summary["cancelled"] is True and summary["processed"] == 1
          and len(summary["remaining"]) == 3,
          json.dumps({k: summary[k] for k in ("cancelled", "processed", "remaining")},
                     ensure_ascii=False))
    check("3.10 取消前已完成的结果已保存（不因取消回滚）",
          check_row(env2, 1) is not None and check_row(env2, 2) is None)
    # 单条失败不阻塞
    env3 = make_env("03-fail", [{"sid": 1, "steps": STEPS_OK}, {"sid": 2, "steps": STEPS_BAD},
                                {"sid": 3, "steps": STEPS_OK}])
    real_save = answer_check.save

    def flaky(db_, class_row_, user_id_, answer_id_, body):
        if answer_id_ == 2:
            raise answer_check.AnswerCheckError("模拟：这一条规则解析异常", 500, "boom")
        return real_save(db_, class_row_, user_id_, answer_id_, body)

    with patched(answer_check, "save", flaky):
        summary = verify_all(env3)
    check("3.11 单条失败不阻塞：1 条 failed，其余 2 条 verified（已完成结果正常保存）",
          summary["verified"] == 2 and summary["failed"] == 1
          and summary["failed_ids"] == [2] and check_row(env3, 1) and check_row(env3, 3),
          json.dumps({k: summary[k] for k in ("verified", "failed", "failed_ids")},
                     ensure_ascii=False))
    check("3.12 失败记录的错误码与消息回给界面（可判可重试）",
          summary["results"][1]["code"] == "boom" and "模拟" in summary["results"][1]["message"])
    retried = grading.retry_failed(env3["db"], env3["class_row"], UID, summary["failed_ids"])
    check("3.13 重试失败记录：只重跑失败的那条，且这次成功",
          retried["total"] == 1 and retried["verified"] == 1
          and check_row(env3, 2) is not None,
          json.dumps({k: retried[k] for k in ("total", "verified", "failed")}, ensure_ascii=False))

    # 重复核验不覆盖教师判断（含反向自证）
    env4 = make_env("03-protect", [{"sid": 1, "steps": STEPS_BAD}, {"sid": 2, "steps": STEPS_OK}])
    db4, cls4 = env4["db"], env4["class_row"]
    verify_all(env4)
    grading.confirm_cause(db4, cls4, UID, 1, cause_label="移项符号写错",
                          basis_note="2x=6 应为 2x=4")
    grading.save_score(db4, cls4, UID, 1, score=7.5, state="confirmed", confirm=True)
    before_check = dict(check_row(env4, 1))
    before_score = dict(score_row(env4, 1))
    summary = verify_all(env4, scope=grading.SCOPE_ALL, batch=BATCH)
    after_check = dict(check_row(env4, 1))
    after_score = dict(score_row(env4, 1))
    unchanged = all([
        before_check["cause_state"] == after_check["cause_state"] == "decided",
        before_check["cause_source"] == after_check["cause_source"],
        before_check["cause_label"] == after_check["cause_label"],
        before_check["answer_version"] == after_check["answer_version"],
        before_score["state"] == after_score["state"] == "confirmed",
        before_score["score"] == after_score["score"] == 7.5,
        before_score["answer_version"] == after_score["answer_version"],
    ])
    check("3.14 重复核验不覆盖：已判错因与已确认成绩一字不变（跳过原因可解释）",
          unchanged and summary["skipped"] == 1
          and "cause_decided" in summary["results"][0]["reasons"],
          json.dumps({"skipped": summary["skipped"],
                      "reasons": summary["results"][0].get("reasons")}, ensure_ascii=False))
    # 反向自证：裸调 R1 的 save（不带错因）会把已判错因清成待判断 → 证明上一道的保护不是恒真
    answer = db4.query_one("SELECT * FROM answers WHERE id=1")
    answer_check.save(db4, cls4, UID, 1, {"expected_version": answer["fingerprint"],
                                          "progress": "checked"})
    wiped = dict(check_row(env4, 1))
    check("3.15 反向自证：去掉保护闸（裸调 answer_check.save）后错因被清空，断言必红",
          wiped["cause_state"] != "decided",
          "cause_state=%s" % wiped["cause_state"])
    # 人工核验也必须被保护
    env5 = make_env("03-manual", [{"sid": 1, "steps": "第一步随便写\n和题目无关的文字"}])
    db5, cls5 = env5["db"], env5["class_row"]
    answer = db5.query_one("SELECT * FROM answers WHERE id=1")
    answer_check.save(db5, cls5, UID, 1, {
        "expected_version": answer["fingerprint"], "progress": "checked",
        "observed_step": "第 1 行把移项写成了加项",
        "basis_note": "作业本原图上第 1 行确是移项写错",
    })
    manual_before = dict(check_row(env5, 1))
    result = grading.verify_one(db5, cls5, UID, 1)
    manual_after = dict(check_row(env5, 1))
    check("3.16 人工核验结论不被重复核验覆盖（verify_source=manual，updated_at 未变）",
          manual_before["verify_source"] == "manual"
          and manual_after["verify_source"] == "manual"
          and manual_after["updated_at"] == manual_before["updated_at"]
          and result["status"] == "skipped",
          "before=%s after=%s" % (manual_before["verify_source"], manual_after["verify_source"]))
    check("3.17 强制重跑也只重算规则，教师观察到的错步与依据保留",
          manual_after["teacher_error_text"] == manual_before["teacher_error_text"]
          and manual_after["basis_note"] == manual_before["basis_note"])


# ---------------------------------------------------------------------------
# 四、错因确认与撤回
# ---------------------------------------------------------------------------


def section_cause():
    section("4. 错因判断（确认 / 撤回 / 独立性）")
    env = make_env("04-cause", [{"sid": 1, "steps": STEPS_BAD}])
    db, cls = env["db"], env["class_row"]
    err = caught(grading.confirm_cause, db, cls, UID, 1, cause_label="没核验就判错因")
    check("4.1 未核验先判错因被拒（STATE_GATES cause.decided）",
          err is not None and err.code == "verify_required", getattr(err, "code", ""))
    verify_all(env)
    candidates = grading.cause_candidates(db, cls, 1)
    check("4.2 候选错因走本地只读桥（真实作答无预置候选，给证据与适用范围）",
          candidates["local_only"] is True and "applicability" in candidates
          and candidates["applicability"] == "required",
          json.dumps({k: candidates[k] for k in ("scope", "applicability")}, ensure_ascii=False))
    err = caught(grading.confirm_cause, db, cls, UID, 1, cause_label="")
    check("4.3 判错因必须写原因说明", err is not None and err.code == "cause_required")
    result = grading.confirm_cause(db, cls, UID, 1, cause_label="移项符号写错",
                                   basis_note="2x=6 应为 2x=4：把 −1 写成 +1")
    check("4.4 确认错因：cause=decided，核验仍 verified_rule",
          result["cause"]["state"] == "decided"
          and result["dimensions"]["verify"]["state"] == "verified_rule")
    mistake = result["mistake"]
    check("4.5 确认错因同步错题：status confirmed + 来源「作答核对」+ 依据可追溯",
          mistake and mistake["status"] == "confirmed"
          and mistake["source_type"] == "作答核对" and "判断依据" in mistake["note"],
          json.dumps(mistake, ensure_ascii=False)[:160] if mistake else "None")
    check("4.6 未给知识点时 knowledge_points 是空数组（未分类，不编造知识点名）",
          mistake["knowledge_points"] == [] and mistake["classified"] is False)
    check("4.7 错题备注标「未分类（缺依据，待教师补充）」",
          "未分类" in mistake["note"] and "待教师补充" in mistake["note"])
    grading.save_score(db, cls, UID, 1, score=7.5, state="confirmed", confirm=True)
    retract = grading.retract_cause(db, cls, UID, 1, reason="再核对后认为不是符号错")
    check("4.8 撤回错因：cause→undecided，核验 verified_rule、评分 confirmed 独立保留",
          retract["cause"]["state"] == "undecided"
          and retract["verify"]["state"] == "verified_rule"
          and retract["score"]["state"] == "confirmed"
          and retract["score_kept"] is True,
          json.dumps({"cause": retract["cause"]["state"], "verify": retract["verify"]["state"],
                      "score": retract["score"]["state"]}, ensure_ascii=False))
    check("4.9 撤回错因的动作来自契约（cause_history / 错题退回 / 频次重算）",
          "mistakes.unlink_confirmed" in retract["actions"]
          and "answer_checks.cause_history.append" in retract["actions"],
          repr(retract["actions"]))
    check("4.10 撤回后错题退回（我方写的那条），错题管理不再计它为已确认",
          retract["mistake_returned"] is True
          and [row["status"] for row in mistake_rows(env)] == ["returned"])
    check("4.11 撤回只影响错因：统计里有效确认错因为 0，但有效确认评分仍为 1",
          len(grading.confirmed_causes(db, cls, BATCH)) == 0
          and len(grading.confirmed_scores(db, cls, BATCH)) == 1)
    err = caught(grading.retract_cause, db, cls, UID, 1)
    check("4.12 没有可撤回的错因时明确拒绝", err is not None and err.code == "nothing_to_retract")
    # 对照：走 R1 的整条撤回会把核验一起撤掉 → 证明 4.8 的「不连带」不是恒真
    answer = db.query_one("SELECT * FROM answers WHERE id=1")
    answer_check.save(db, cls, UID, 1, {"expected_version": answer["fingerprint"],
                                        "progress": "pending"})
    dims = grading.answer_dimensions(db, cls, 1)["dimensions"]
    check("4.13 反向自证：整条撤回核验后 verify→unverified（而 retract_cause 不会）",
          dims["verify"]["state"] == "unverified" and dims["score"]["state"] == "confirmed",
          json.dumps(dims, ensure_ascii=False)[:200])


# ---------------------------------------------------------------------------
# 五、教师评分
# ---------------------------------------------------------------------------


def section_score():
    section("5. 教师评分（范围 / 小数 / 草稿 / 确认 / 步骤扣分 / 撤回）")
    env = make_env("05-score", [{"sid": 1, "steps": STEPS_BAD}], questions=("1", "2"))
    db, cls = env["db"], env["class_row"]
    check("5.1 满分来自批次配置（同批次同题统一）",
          grading.score_full_of(db, cls, BATCH, "1") == 10
          and grading.score_full_of(db, cls, BATCH, "9") is None)
    result = grading.save_score(db, cls, UID, 1, score=7.5, state="confirmed", confirm=True)
    check("5.2 满分 10 录 7.5 通过并计入正式统计",
          result["state"] == "confirmed" and result["formal"] is True
          and result["score"] == 7.5, json.dumps(result, ensure_ascii=False)[:160])
    check("5.3 7.5 落库（answer_scores.score=7.5，state=confirmed，带作答版本）",
          score_row(env, 1)["score"] == 7.5 and score_row(env, 1)["state"] == "confirmed"
          and score_row(env, 1)["answer_version"] == "fp-1")
    check("5.4 不完整成绩不进平均分（缺第 2 题 → complete=False、total=None）",
          result["completeness"]["complete"] is False
          and result["completeness"]["missing"] == ["2"]
          and result["completeness"]["total"] is None,
          json.dumps(result["completeness"], ensure_ascii=False))
    for bad, code in ((11, "above_full"), (-1, "out_of_range"), (7.555, "too_many_decimals")):
        err = caught(grading.save_score, db, cls, UID, 1, score=bad, state="confirmed",
                     confirm=True)
        check("5.5 拒绝越界分数 %r（%s）" % (bad, code),
              err is not None and err.code == code, getattr(err, "code", ""))
    err = caught(grading.save_score, db, cls, UID, 1, score="abc", state="draft")
    check("5.6 拒绝非数值分数", err is not None and err.code == "not_number")
    err = caught(grading.save_score, db, cls, UID, 1, score=8, state="confirmed")
    check("5.7 未勾选确认的正式得分被拒（正式得分须教师明确确认）",
          err is not None and err.code == "confirm_required")
    err = caught(grading.save_score, db, cls, UID, 1, score=None, state="confirmed", confirm=True)
    check("5.8 无分数不得确认（无依据时建议得分留空 → 只能存草稿）",
          err is not None and err.code == "no_evidence")
    draft = grading.save_score(db, cls, UID, 1, score=9.5, state="draft",
                               revision_reason="先存草稿")
    check("5.9 草稿允许保存但不进正式统计（formal=False，confirmed_scores 不含）",
          draft["state"] == "draft" and draft["formal"] is False
          and grading.current_scores(db, cls, BATCH)[0]["formal"] is False)
    confirmed_row = score_row(env, 1)
    check("5.10 存草稿把已确认分改成草稿：confirmed_at 清空、state=draft",
          confirmed_row["state"] == "draft" and confirmed_row["confirmed_at"] == "",
          json.dumps(dict(confirmed_row), ensure_ascii=False)[:160])
    err = caught(grading.save_score, db, cls, UID, 1, score=6, state="confirmed", confirm=True)
    check("5.11 草稿→确认不需要修改理由（草稿不是「已确认分」，改的只是待定分）",
          err is None and score_row(env, 1)["score"] == 6
          and score_row(env, 1)["confirmed_at"] != "",
          getattr(err, "code", ""))
    err = caught(grading.save_score, db, cls, UID, 1, score=5, state="confirmed", confirm=True)
    check("5.12 改已确认的分数不写理由被拒（revision_reason_required）",
          err is not None and err.code == "revision_reason_required", getattr(err, "code", ""))
    changed = grading.save_score(db, cls, UID, 1, score=5, state="confirmed", confirm=True,
                                 revision_reason="讲评后追加扣分")
    history = json.loads(score_row(env, 1)["history"])
    check("5.13 写明理由后改分成功，history 留住改分人与理由",
          changed["score"] == 5 and any(item.get("action") == "confirm" for item in history)
          and any(item.get("revision_reason") == "讲评后追加扣分" for item in history),
          json.dumps(history, ensure_ascii=False)[-160:])
    ok_steps = grading.save_score(
        db, cls, UID, 1, score=7.5, state="confirmed", confirm=True,
        revision_reason="按步骤重评",
        steps=[{"step_index": 1, "label": "移项符号错", "deduction": 2.5, "note": "第 2 行"}])
    check("5.14 步骤扣分与总分一致（10−2.5=7.5）时保存成功并落扣分项",
          ok_steps["state"] == "confirmed"
          and ok_steps["steps"][0]["deduction"] == 2.5
          and env["db"].scalar("SELECT COUNT(*) FROM score_step_deductions") == 1)
    err = caught(grading.save_score, db, cls, UID, 1, score=8, state="confirmed", confirm=True,
                 revision_reason="x", steps=[{"step_index": 1, "label": "漏写一步",
                                               "deduction": 1}])
    check("5.15 步骤扣分与总分不一致被拒（10−1≠8），库内仍是 7.5（不改库）",
          err is not None and err.code == "deduction_mismatch"
          and score_row(env, 1)["score"] == 7.5, getattr(err, "code", ""))
    # 反向自证：把一致性校验换成恒真 → 同一保存会成功
    with patched(r2_contract, "step_deductions_consistent",
                 lambda full, score, steps: {"ok": True, "expected": score, "actual": score,
                                             "diff": 0.0, "code": "ok", "message": ""}):
        bypass = grading.save_score(db, cls, UID, 1, score=8, state="confirmed", confirm=True,
                                    revision_reason="y",
                                    steps=[{"step_index": 1, "label": "漏写一步",
                                            "deduction": 1}])
    check("5.16 反向自证：去掉一致性校验后 8 分被写入（证明 5.15 的拦截是真闸门）",
          bypass["score"] == 8 and score_row(env, 1)["score"] == 8)
    back = grading.save_score(db, cls, UID, 1, score=7.5, state="confirmed", confirm=True,
                              revision_reason="恢复正确分数",
                              steps=[{"step_index": 1, "label": "移项符号错",
                                      "deduction": 2.5}])
    check("5.17 恢复一致性后重新保存成功（闸门可恢复，不是一次性的）",
          back["score"] == 7.5 and score_row(env, 1)["score"] == 7.5)
    retracted = grading.retract_score(db, cls, UID, 1, reason="本次作业不计分")
    check("5.18 撤回成绩：state→unscored、退出正式统计、历史与扣分项保留",
          retracted["state"] == "unscored" and retracted["formal"] is False
          and score_row(env, 1)["state"] == "unscored"
          and score_row(env, 1)["score"] is None
          and env["db"].scalar("SELECT COUNT(*) FROM score_step_deductions") == 1)
    check("5.19 撤回后有效确认评分为 0（退出平均分/得分率/覆盖率/完整成绩）",
          grading.confirmed_scores(db, cls, BATCH) == []
          and grading.score_completeness_of(db, cls, BATCH, 1)["complete"] is False)
    err = caught(grading.retract_score, db, cls, UID, 1)
    check("5.20 没有可撤回的成绩时明确拒绝", err is not None and err.code == "nothing_to_retract")
    err = caught(grading.save_score, db, cls, UID, 1, score=9, state="confirmed",
                 confirm=True, expected_version="旧版本指纹")
    check("5.21 版本不符拒绝（409 version_conflict），不拿旧版本覆盖新内容",
          err is not None and err.status == 409 and err.code == "version_conflict")
    err = caught(grading.save_score, db, cls, UID, 1, score=9, state="bogus")
    check("5.22 非法评分状态拒绝", err is not None and err.code == "bad_score_state")
    err = caught(grading.save_score, db, cls, UID, 1, score=9, state="draft",
                 steps=[{"step_index": 1, "label": "", "deduction": 1}])
    check("5.23 扣分项缺说明拒绝（不静默丢老师的输入）",
          err is not None and err.code == "bad_steps")


# ---------------------------------------------------------------------------
# 六、失效链
# ---------------------------------------------------------------------------


def section_invalidation():
    section("6. 失效链（改内容 / 改标准）")
    env = make_env("06-invalidate", [{"sid": 1, "steps": STEPS_BAD}])
    db, cls = env["db"], env["class_row"]
    verify_all(env)
    grading.confirm_cause(db, cls, UID, 1, cause_label="移项符号写错",
                          basis_note="2x=6 应为 2x=4")
    grading.save_score(db, cls, UID, 1, score=7.5, state="confirmed", confirm=True)
    answer = db.query_one("SELECT * FROM answers WHERE id=1")
    result = grading.update_answer_content(db, cls, UID, 1, {
        "expected_version": answer["fingerprint"],
        "student_steps": "2x+1=5\n2x=4\nx=2\n验算：2×2+1=5 成立",
    })
    dims = result["dimensions"]
    check("6.1 改作答内容：核验→stale（旧结论退出当前视图）",
          dims["verify"]["state"] == "stale", dims["verify"]["state"])
    check("6.2 改作答内容：错因→undecided（旧判断不再作为当前结论）",
          dims["cause"]["state"] == "undecided", dims["cause"]["state"])
    check("6.3 改作答内容：评分→invalidated（不删行，留历史）",
          dims["score"]["state"] == "invalidated"
          and score_row(env, 1)["state"] == "invalidated"
          and score_row(env, 1)["invalidated_reason"] == "answer_updated",
          json.dumps(dict(score_row(env, 1)), ensure_ascii=False)[:160])
    check("6.4 失效动作来自契约 R2-INV-01",
          "answer_scores.invalidate" in result["actions"]
          and "answer_followups.invalidate:answer_updated" in result["actions"],
          repr(result["actions"]))
    check("6.5 失效后旧成绩退出正式统计（confirmed_scores 为空）",
          grading.confirmed_scores(db, cls, BATCH) == [])
    verify_all(env, scope=grading.SCOPE_ALL)
    dims = grading.answer_dimensions(db, cls, 1)["dimensions"]
    check("6.6 重跑核验后核验恢复 verified_rule（stale→verified_rule 是合法迁移）",
          dims["verify"]["state"] == "verified_rule")
    # 改标准：先证明「只改正文不改版本」不会让旧成绩失效
    grading.confirm_cause(db, cls, UID, 1, cause_label="移项符号写错", basis_note="依据同上")
    grading.save_score(db, cls, UID, 1, score=7.5, state="confirmed", confirm=True,
                       revision_reason="重评")
    config = db_mod.load_batch_config(db, 1, BATCH)
    sneaky = dict(config)
    sneaky["rubrics"] = {str(key): dict(value) for key, value in
                         (config.get("rubrics") or {}).items() if isinstance(value, dict)}
    sneaky["rubrics"]["1"] = dict(sneaky["rubrics"].get("1") or {})
    sneaky["rubrics"]["1"]["criteria"] = "新的标准正文（但版本号没改）"
    sneaky["rubric"] = dict(config["rubric"])
    sneaky["rubric"]["1"] = "新的标准正文（但版本号没改）"
    db_mod.save_batch_config(db, 1, BATCH, sneaky, user_id=UID)
    check("6.7 反向自证：只改标准正文、不改 rubric_version 时旧成绩仍是 confirmed（不会自动失效）",
          score_row(env, 1)["state"] == "confirmed")
    result = grading.update_rubric(db, cls, BATCH, UID, "1",
                                   {"text": "移项、合并、系数化 1，每步 4 分，共 12 分"},
                                   "v2", full_score=12)
    check("6.8 改标准 + 版本号 → 旧成绩立即失效（invalidated_reason=rubric_changed）",
          result["invalidated"] == 1
          and score_row(env, 1)["state"] == "invalidated"
          and score_row(env, 1)["invalidated_reason"] == "rubric_changed",
          json.dumps({k: result[k] for k in ("rubric_version", "invalidated")}, ensure_ascii=False))
    check("6.9 新标准生效：该题满分变成 12、该题标准版本变成 v2（题级权威）",
          grading.score_full_of(db, cls, BATCH, "1") == 12
          and grading.question_rubric_version(
              db_mod.load_batch_config(db, 1, BATCH), "1") == "v2",
          json.dumps(grading.question_rubric(db_mod.load_batch_config(db, 1, BATCH), "1"),
                     ensure_ascii=False))
    check("6.10 旧成绩退出正式统计（标准一变，旧分不再可比）",
          grading.confirmed_scores(db, cls, BATCH) == [])
    err = caught(grading.update_rubric, db, cls, BATCH, UID, "1", {"text": "再改"}, "v2")
    check("6.11 版本号没变时拒绝改标准（否则旧成绩不会失效）",
          err is not None and err.code == "rubric_version_unchanged")
    err = caught(grading.update_rubric, db, cls, BATCH, UID, "1", {"text": "再改"}, "")
    check("6.12 空版本号拒绝", err is not None and err.code == "rubric_version_missing")
    err = caught(grading.update_rubric, db, cls, "", UID, "1", {"text": "x"}, "v3")
    check("6.13 缺批次拒绝（跨批次不合并标准）", err is not None and err.code == "missing_batch")
    # 失效后重评：invalidated → draft → confirmed
    regraded = grading.save_score(db, cls, UID, 1, score=10, state="confirmed", confirm=True,
                                  revision_reason="按新标准重评")
    rows = [dict(row) for row in env["db"].query(
        "SELECT * FROM answer_scores WHERE answer_id=1 ORDER BY id")]
    live = [row for row in rows if row["state"] in ("draft", "confirmed")]
    check("6.14 失效后可重评（invalidated→draft→confirmed 走状态机，同一版本只留一条活行）",
          regraded["state"] == "confirmed" and len(live) == 1
          and live[0]["rubric_version"] == "v2" and live[0]["score"] == 10,
          json.dumps([{k: row[k] for k in ("id", "state", "score", "rubric_version")}
                      for row in rows], ensure_ascii=False))
    check("6.15 旧作答版本的失效行保留（不删行，历史可追溯）",
          len(rows) == 2 and any(row["state"] == "invalidated" for row in rows)
          and any(row["answer_version"] != live[0]["answer_version"] for row in rows),
          json.dumps([{k: row[k] for k in ("id", "state", "answer_version")} for row in rows],
                     ensure_ascii=False))
    section_per_question_rubric()


def section_per_question_rubric():
    """题级标准版本：改一题只失效该题（派单 §2.3 / §2.5「受影响的旧成绩失效」）。"""

    def build(name):
        env = make_env(name, [
            {"sid": 1, "qno": "1", "steps": STEPS_BAD, "key": "Q1"},
            {"sid": 2, "qno": "2", "steps": STEPS_BAD, "key": "Q2"},
            {"sid": 3, "qno": "3", "steps": STEPS_BAD, "key": "Q3"},
            {"sid": 4, "qno": "4", "steps": STEPS_BAD, "key": "Q4"},
            {"sid": 5, "qno": "5", "steps": STEPS_BAD, "key": "Q5"},
        ], students=5, questions=("1", "2", "3", "4", "5"))
        verify_all(env)
        for sid in range(1, 6):
            grading.save_score(env["db"], env["class_row"], UID, sid, score=7.5,
                               state="confirmed", confirm=True)
        return env

    def snapshot(env, sid):
        row = score_row(env, sid, str(sid))
        return {key: row[key] for key in ("state", "score", "full_score", "rubric_version",
                                          "confirmed_at", "history", "answer_version")}

    env = build("06-q3-rubric")
    before = {sid: snapshot(env, sid) for sid in (1, 2, 4, 5)}
    check("6.16 改第 3 题前：5 题都是已确认成绩",
          all(score_row(env, sid, str(sid))["state"] == "confirmed" for sid in range(1, 6)))
    result = grading.update_rubric(env["db"], env["class_row"], BATCH, UID, "3",
                                   {"text": "第 3 题新标准：移项与合并各 4 分"},
                                   "q3-v2", full_score=12)
    after = {sid: snapshot(env, sid) for sid in (1, 2, 4, 5)}
    check("6.17 改第 3 题标准 → 第 3 题旧成绩 invalidated",
          score_row(env, 3, "3")["state"] == "invalidated"
          and score_row(env, 3, "3")["invalidated_reason"] == "rubric_changed"
          and result["invalidated"] == 1,
          "invalidated=%s" % result["invalidated"])
    check("6.18 第 1/2/4/5 题仍 confirmed，且分数/满分/版本/历史逐字段相等（不受牵连）",
          after == before,
          json.dumps({"before": before, "after": after}, ensure_ascii=False)[:300])
    check("6.19 题级版本各自独立：第 1/2/4/5 题版本仍是 v1，只有第 3 题变 q3-v2",
          result["question_versions"] == {"1": "v1", "2": "v1", "3": "q3-v2",
                                          "4": "v1", "5": "v1"},
          json.dumps(result["question_versions"], ensure_ascii=False))
    check("6.20 有效确认评分仍为 4 条（只有第 3 题退出正式统计）",
          len(grading.confirmed_scores(env["db"], env["class_row"], BATCH)) == 4)
    err = caught(grading.update_rubric, env["db"], env["class_row"], BATCH, UID, "3",
                 {"text": "又改一版正文"}, "q3-v2")
    check("6.21 只改第 3 题正文不给新版本号 → 400 rubric_version_unchanged（版本按题比较）",
          err is not None and err.code == "rubric_version_unchanged", getattr(err, "code", ""))
    # 反向自证：把「按题比较」换成「拿批次级单版本一刀切」（模拟旧实现的失效判定）
    # → 没改动的第 1/2/4/5 题会被连带判失效，6.18 的断言必红。
    env2 = build("06-batch-compare")
    with patched(grading, "question_rubric_version", lambda config, question: "batched-single-v2"):
        grading.update_rubric(env2["db"], env2["class_row"], BATCH, UID, "3",
                              {"text": "第 3 题新标准"}, "q3-v2", full_score=12)
    check("6.22 反向自证：失效判定改回批次级单版本比较时，没改的第 1/2/4/5 题被连带失效（断言必红）",
          score_row(env2, 1, "1")["state"] == "invalidated"
          and score_row(env2, 2, "2")["state"] == "invalidated"
          and score_row(env2, 4, "4")["state"] == "invalidated"
          and score_row(env2, 5, "5")["state"] == "invalidated",
          "q1=%s q2=%s" % (score_row(env2, 1, "1")["state"],
                           score_row(env2, 2, "2")["state"]))
    # 历史批次兼容：只有批次级版本、没有题级条目时，回退使用批次级版本
    legacy = make_env("06-legacy-version", [{"sid": 1, "qno": "1", "steps": STEPS_BAD}],
                      questions=("1",), version="legacy-v1")
    legacy_config = db_mod.load_batch_config(legacy["db"], 1, BATCH)
    check("6.23 历史批次（只有批次级 rubric_version）回退到批次级版本，不当作「无标准」",
          grading.question_rubric_version(legacy_config, "1") == "legacy-v1"
          and grading.question_rubric(legacy_config, "1")["text"] != "",
          json.dumps(legacy_config.get("rubric"), ensure_ascii=False)[:120])


# ---------------------------------------------------------------------------
# 七、错题同步与知识点
# ---------------------------------------------------------------------------


def section_mistakes():
    section("7. 错题同步与知识点（K1/K2/K3 + 缺测/内容不足/无法核验不得落错题）")
    answers = []
    # 15 条答错（first_bad）+ 5 条缺测 + 2 条仅照片 + 1 条无法核验
    for index in range(15):
        answers.append({"sid": index % 3 + 1, "steps": STEPS_BAD, "key": "BAD-%d" % index})
    for index in range(5):
        answers.append({"sid": index % 3 + 1, "steps": "", "missing": 1,
                        "missing_reason": "病假", "key": "MISS-%d" % index})
    for index in range(2):
        answers.append({"sid": index + 1, "steps": "", "images": 2, "key": "IMG-%d" % index})
    answers.append({"sid": 1, "steps": "第一步随便写\n和题目无关的文字", "key": "OOS-0"})
    students = 3
    env = make_env("07-mistakes", answers, students=students)
    db, cls = env["db"], env["class_row"]
    verify_all(env)
    first_bad = [row for row in grading.current_checks(db, cls, BATCH)
                 if row["locate_status"] == "first_bad"]
    check("7.1 15 条作答的定位结论是 first_bad", len(first_bad) == 15, str(len(first_bad)))
    confirmed = 0
    for row in first_bad:
        grading.confirm_cause(db, cls, UID, row["answer_id"], cause_label="移项符号写错",
                              basis_note="2x=6 应为 2x=4")
        confirmed += 1
    check("7.2 确认 15 条错误 → 15 条正式错题（confirmed，来源作答核对）",
          confirmed == 15 and len(mistake_rows(env, "confirmed")) == 15,
          "confirmed_rows=%d" % len(mistake_rows(env, "confirmed")))
    rows = mistake_rows(env, "confirmed")
    bad_ids = {row["answer_id"] for row in first_bad}
    check("7.3 断言 A：错题行数 == first_bad 且 cause=decided 的条数",
          len(rows) == len(bad_ids), "%d vs %d" % (len(rows), len(bad_ids)))
    miss_ids = [aid for aid in range(16, 21)]
    img_ids = [21, 22]
    oos_id = 23
    extra = [row for row in mistake_rows(env) if row["answer_id"] in set(miss_ids + img_ids + [oos_id])]
    check("7.4 断言 B：缺测/仅照片/无法核验在 mistakes 里零行（不是「行存在但不算」）",
          extra == [], repr(extra)[:160])
    oos = grading.answer_dimensions(db, cls, oos_id)["dimensions"]
    check("7.5 无法核验的作答：定位 out_of_scope、错因无需判断、不得计为错误",
          oos["cause"]["state"] == "not_applicable", json.dumps(oos, ensure_ascii=False))
    err = caught(grading.confirm_cause, db, cls, UID, oos_id, cause_label="硬判个错因")
    check("7.6 无法核验的作答上强判错因先被「人工核验依据」闸门拦下（basis_required）",
          err is not None and err.code == "basis_required", getattr(err, "code", ""))
    grading.confirm_cause(db, cls, UID, oos_id, cause_label="疑似看错题意",
                          basis_note="题目下方写着「请写出每一步」，学生只写了答案")
    oos_rows = [row for row in mistake_rows(env) if row["answer_id"] == oos_id]
    check("7.6b 无法核验的作答上判了错因也不得落 mistakes 行（定位不是可归因错步）",
          oos_rows == [] and grading.answer_dimensions(db, cls, oos_id)["dimensions"]["cause"]
          ["state"] == "decided",
          repr(oos_rows)[:120])
    # 知识点：未分类时原版学情页查不到 → 反向自证；确认后能查到
    target = first_bad[0]["answer_id"]
    before_rows = stats_mod.confirmed_mistakes(db, 1, None, {})
    before_kp = [item for row in before_rows for item in json.loads(row["knowledge_points"] or "[]")]
    check("7.7 反向自证：未分类（knowledge_points=[]）时原版统计查不到任何知识点",
          before_kp == [], repr(before_kp))
    updated = grading.set_knowledge_point(db, cls, UID, target, "一元一次方程·移项",
                                          basis="错在第 2 行 2x=6：移项时把 −1 写成 +1")
    after_rows = stats_mod.confirmed_mistakes(db, 1, None, {})
    bucket = None
    for row in after_rows:
        points = json.loads(row["knowledge_points"] or "[]")
        if "一元一次方程·移项" in points:
            bucket = row
    check("7.8 确认知识点后原版学情页面可查询（stats.confirmed_mistakes 读同一列）",
          updated["knowledge_points"] == ["一元一次方程·移项"] and bucket is not None,
          json.dumps(updated, ensure_ascii=False)[:160])
    check("7.9 知识点依据写进错题备注（可追溯：依据 + 谁在什么时候补录）",
          "一元一次方程·移项" in updated["note"] and "依据" in updated["note"]
          and ("由 %d" % UID) in updated["note"], updated["note"][-120:])
    check("7.10 有效确认错因带上知识点（R2-03 的知识点频次直接用 effective_rows）",
          any(item["knowledge_points"] == ["一元一次方程·移项"]
              for item in grading.effective_rows(db, cls, BATCH)),
          "no row carried knowledge_points")
    check("7.11 knowledge_point 过滤只命中该知识点的行",
          len(grading.effective_rows(db, cls, BATCH, knowledge_point="一元一次方程·移项")) == 1)
    err = caught(grading.set_knowledge_point, db, cls, UID, miss_ids[0], "随便一个知识点")
    check("7.12 缺测记录没有错题行，补知识点被明确拒绝（不凭空造错题）",
          err is not None and err.code == "mistake_missing")
    # 撤回 1 条：错题退回、统计退出
    victim = first_bad[1]["answer_id"]
    causes_before = len(grading.confirmed_causes(db, cls, BATCH))
    grading.retract_cause(db, cls, UID, victim, reason="复核后不是这个原因")
    check("7.13 撤回 1 条错因 → 有效确认错题少 1 条，错题行退回但保留",
          len(grading.confirmed_causes(db, cls, BATCH)) == causes_before - 1
          and len(mistake_rows(env, "confirmed")) == 14
          and len(mistake_rows(env, "returned")) == 1,
          "before=%d after=%d confirmed_rows=%d" % (
              causes_before, len(grading.confirmed_causes(db, cls, BATCH)),
              len(mistake_rows(env, "confirmed"))))
    check("7.14 撤回的那条仍保留核验结论（K3：错因退出不等于核验失效）",
          grading.answer_dimensions(db, cls, victim)["dimensions"]["verify"]["state"]
          == "verified_rule")
    # 断言 C：缺测记录事后补录并确认错误，才允许出现错题行
    supplement = miss_ids[0]
    answer = db.query_one("SELECT * FROM answers WHERE id=?", (supplement,))
    grading.update_answer_content(db, cls, UID, supplement, {
        "expected_version": answer["fingerprint"], "missing": "否", "student_steps": STEPS_BAD})
    verify_all(env, scope=grading.SCOPE_ALL)
    dims = grading.answer_dimensions(db, cls, supplement)["dimensions"]
    check("7.15 断言 C：缺测补录后作答变 valid、核验 first_bad 可归因",
          dims["answer"]["state"] == "valid" and dims["verify"]["state"] == "verified_rule",
          json.dumps(dims, ensure_ascii=False)[:160])
    before_c = len(mistake_rows(env))
    grading.confirm_cause(db, cls, UID, supplement, cause_label="移项符号写错",
                          basis_note="补录后第 2 行仍是 2x=6")
    check("7.16 断言 C：补录并确认错误后才出现 1 条新的错题行",
          len(mistake_rows(env)) == before_c + 1
          and len(mistake_rows(env, "confirmed")) == 15,
          "%d -> %d" % (before_c, len(mistake_rows(env))))


# ---------------------------------------------------------------------------
# 八、只读接口（R2-03 / R2-05 用）
# ---------------------------------------------------------------------------


def section_readonly():
    section("8. 只读接口（确定性 / 跨班隔离 / 覆盖率分母）")
    answers = [{"sid": 1, "steps": STEPS_BAD, "qno": "1"},
               {"sid": 2, "steps": STEPS_OK, "qno": "1"},
               {"sid": 3, "steps": STEPS_BAD, "qno": "2"}]
    env = make_env("08-readonly", answers, students=4, questions=("1", "2"),
                   extra_students=6)
    db, cls = env["db"], env["class_row"]
    verify_all(env)
    grading.confirm_cause(db, cls, UID, 1, cause_label="移项符号写错", basis_note="依据" * 3)
    grading.save_score(db, cls, UID, 1, score=7.5, state="confirmed", confirm=True)
    grading.save_score(db, cls, UID, 2, score=10, state="confirmed", confirm=True)
    checks = grading.current_checks(db, cls, BATCH)
    check("8.1 current_checks 只返回当前版本结论（每条作答一条）",
          len(checks) == 3 and {row["answer_id"] for row in checks} == {1, 2, 3})
    check("8.2 current_checks 字段齐备（locate/cause/verify/teacher_*）",
          all(key in checks[0] for key in ("locate_status", "cause_state", "cause_code",
                                           "cause_label", "verify_source", "progress",
                                           "teacher_error_text", "teacher_note", "basis_note")))
    scores = grading.current_scores(db, cls, BATCH)
    check("8.3 current_scores：confirmed 行 formal=True，draft 行 formal=False",
          len(scores) == 2 and all(row["formal"] for row in scores))
    check("8.4 valid_verifications：count=3 且按来源拆分（rule=3）",
          grading.valid_verifications(db, cls, BATCH)["count"] == 3
          and grading.valid_verifications(db, cls, BATCH)["by_source"] == {"rule": 3})
    check("8.5 confirmed_causes：只含 decided 且核验仍有效的行（1 条）",
          [row["answer_id"] for row in grading.confirmed_causes(db, cls, BATCH)] == [1])
    rows = grading.effective_rows(db, cls, BATCH)
    check("8.6 effective_rows 每行都带 source / batch / occurred_on / knowledge_points（统计不必再猜）",
          all(set(("source", "batch", "occurred_on", "knowledge_points")) <= set(row)
              for row in rows)
          and rows[0]["source"] == "作业")
    again = grading.effective_rows(db, cls, BATCH)
    check("8.7 只读接口确定性：两次调用结果完全一致",
          json.dumps(rows, ensure_ascii=False, sort_keys=True)
          == json.dumps(again, ensure_ascii=False, sort_keys=True))
    options = grading.filter_options(db, cls, BATCH)
    check("8.8 filter_options 给批次/题号/学生/核验范围候选值",
          options["batches"] == [BATCH] and options["questions"] == ["1", "2"]
          and len(options["students"]) == 3
          and [item["key"] for item in options["scopes"]]
          == ["pending", "all", "ids"],
          json.dumps(options, ensure_ascii=False)[:200])
    sheet = grading.score_sheet(db, cls, BATCH)
    q1 = [item for item in sheet["questions"] if item["question_no"] == "1"][0]
    check("8.9 score_sheet：第 1 题 confirmed=2、average=8.75、满分 10",
          q1["confirmed"] == 2 and q1["average"] == 8.75 and q1["full_score"] == 10,
          json.dumps(q1, ensure_ascii=False))
    progress = grading.scoring_progress(db, cls, BATCH)
    check("8.10 覆盖率分母 = 应参与评分学生数（名单 10 人，其中 7 人完全没交，分母仍是 10）",
          progress["eligible_students"] == 10 and progress["denominator_source"] == "roster",
          json.dumps({k: progress[k] for k in ("eligible_students", "confirmed_students",
                                               "coverage", "denominator_source")},
                     ensure_ascii=False))
    check("8.11 覆盖率分子只认完整成绩（本题 2 人确认但都缺另一题 → 0 人完整）",
          progress["confirmed_students"] == 0 and progress["coverage"] == 0.0)
    check("8.12 完整成绩判定：两题都确认的学生才 complete（这里都没有 → 暂无）",
          all(grading.score_completeness_of(db, cls, BATCH, sid)["complete"] is False
              for sid in (1, 2, 3)))
    check("8.13 筛选参数生效：question_no / cause_state / score_state",
          len(grading.effective_rows(db, cls, BATCH, question_no="2")) == 1
          and len(grading.effective_rows(db, cls, BATCH, cause_state="decided")) == 1
          and len(grading.effective_rows(db, cls, BATCH, score_state="confirmed")) == 2)
    with db.tx() as conn:
        now = now_iso()
        conn.execute("INSERT INTO classes(id, user_id, name, created_at, updated_at)"
                     " VALUES (2,?,'他班',?,?)", (UID, now, now))
        conn.execute("INSERT INTO students(id, class_id, name, code, created_at, updated_at)"
                     " VALUES (50,2,'他班学生','X1',?,?)", (now, now))
        conn.execute(
            "INSERT INTO answers(id, class_id, student_id, record_key, batch, question_no,"
            " question_text, standard_solution, student_steps, source, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (50,2,50,'X-1',?,?,'题','解','2x=4','作业','fp-50',?,?,?)",
            (BATCH, "1", now, now, now))
    own = grading.effective_rows(db, cls, BATCH)
    check("8.14 跨班隔离：他班同批次作答不进本班只读结果",
          all(row["answer_id"] != 50 for row in own) and len(own) == 3)


# ---------------------------------------------------------------------------
# 九、外发闸门
# ---------------------------------------------------------------------------


def section_egress():
    section("9. 外发闸门（R2 一律本地，不读 ai_individual_allowed）")
    env = make_env("09-egress", [{"sid": 1, "steps": STEPS_BAD}])
    db, cls = env["db"], env["class_row"]
    decision = grading.local_decision("answer_check_grading")
    check("9.1 批量核验/评分的本地决策恒为 deny（ai_isolation）",
          decision["allowed"] is False and decision["code"] == "ai_isolation")
    err = caught(grading.guard_egress, "answer_check_grading", provider="openai", model="gpt-x")
    check("9.2 带 provider/model 的批改请求被 403 拒绝",
          err is not None and err.status == 403 and err.code == "ai_isolation")
    err = caught(grading.guard_egress, "answer_check_cause",
                 fields=["student_name", "answer_text"])
    check("9.3 携带禁止外发字段被拒（学生姓名/作答原文）",
          err is not None and "student_name" in err.message, getattr(err, "message", "")[:80])
    db_mod.set_ai_individual_allowed(db, UID, True)
    setting = db.query_one("SELECT ai_individual_allowed FROM users WHERE id=?", (UID,))
    err = caught(grading.guard_egress, "answer_check_cause", provider="openai")
    check("9.4 打开账号级 ai_individual_allowed 后仍然 403（R2 流程不读这个开关）",
          setting and int(setting["ai_individual_allowed"] or 0) == 1
          and err is not None and err.code == "ai_isolation",
          "switch=%s err=%s" % (setting["ai_individual_allowed"] if setting else None,
                                getattr(err, "code", None)))
    check("9.5 契约层同样拒绝：egress_decision(flow, individual_allowed=True) 仍 allowed=False",
          r2_contract.egress_decision("answer_check_grading", True)["allowed"] is False
          and r2_contract.egress_decision("answer_check_cause", True)["allowed"] is False)
    summary = verify_all(env)
    audits = db.query("SELECT * FROM ai_egress_audit")
    check("9.6 批量核验全程不产生外发审计（本地规则，一次都没发）",
          summary["verified"] == 1 and audits == [], "audit_rows=%d" % len(audits))


# ---------------------------------------------------------------------------
# 十、R1 分析生成的错题在下游不留失效判断
# ---------------------------------------------------------------------------


def section_r1_analysis_mistake():
    section("10. R1 分析生成的错题经更新/校正/撤回后下游不留失效判断")
    env = make_env("10-r1-analysis", [{"sid": 1, "steps": STEPS_BAD}])
    db, cls = env["db"], env["class_row"]
    answer = db.query_one("SELECT * FROM answers WHERE id=1")
    now = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO answer_reviews(class_id, student_id, answer_id, status, provider, model,"
            " causes, note, uncertain, raw, answer_fingerprint, created_at, updated_at)"
            " VALUES (1,1,1,'pending','mock','m','[]','',0,'{}',?,?,?)",
            (answer["fingerprint"], now, now))
        review_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    answer_review.confirm(db, 1, review_id, UID)
    rows = mistake_rows(env, "confirmed")
    check("10.1 R1 分析确认后生成 1 条错题（来源「作答分析」，不含知识点）",
          len(rows) == 1 and rows[0]["source_type"] == "作答分析"
          and json.loads(rows[0]["knowledge_points"] or "[]") == [],
          json.dumps(dict(rows[0]), ensure_ascii=False)[:160])
    check("10.2 分析来的错题不算「有效确认错因」（没有教师判断，cause=undecided）",
          grading.confirmed_causes(db, cls, BATCH) == [])
    verify_all(env)
    result = grading.confirm_cause(db, cls, UID, 1, cause_label="移项符号写错",
                                   basis_note="2x=6 应为 2x=4")
    rows = mistake_rows(env)
    check("10.3 教师确认错因复用同一条错题（不插第二条），来源保持不变",
          len(rows) == 1 and rows[0]["source_type"] == "作答分析",
          "rows=%d" % len(rows))
    check("10.4 确认后有效确认错因为 1 条（统计与详情一致）",
          len(grading.confirmed_causes(db, cls, BATCH)) == 1)
    grading.retract_cause(db, cls, UID, 1, reason="复核后撤回")
    rows = mistake_rows(env)
    check("10.5 撤回错因：「作答分析」生成的错题不被我方退回（只退我方那条）",
          len(rows) == 1 and rows[0]["status"] == "confirmed",
          json.dumps(dict(rows[0]), ensure_ascii=False)[:120])
    check("10.6 撤回后下游没有失效判断：有效确认错因 0、有效确认评分 0",
          grading.confirmed_causes(db, cls, BATCH) == []
          and grading.confirmed_scores(db, cls, BATCH) == [])
    answer = db.query_one("SELECT * FROM answers WHERE id=1")
    grading.update_answer_content(db, cls, UID, 1, {
        "expected_version": answer["fingerprint"],
        "student_steps": "2x+1=5\n2x=4\nx=2\n验算成立"})
    check("10.7 校正内容后：核验 stale、错因 undecided，下游不再显示旧结论",
          grading.answer_dimensions(db, cls, 1)["dimensions"]["verify"]["state"] == "stale"
          and grading.answer_dimensions(db, cls, 1)["dimensions"]["cause"]["state"] == "undecided")
    check("10.8 校正后有效确认错因仍为 0（旧判断没有借旧版本复活）",
          grading.confirmed_causes(db, cls, BATCH) == [])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("对数智伴学 R2-02 后端（批量核验 / 教师评分 / 失效链 / 错题与知识点）做业务路径验收")
    for line in environment_lines():
        log(line)
    for runner in (section_structure, section_dimensions, section_batch_verify, section_cause,
                   section_score, section_invalidation, section_mistakes, section_readonly,
                   section_egress, section_r1_analysis_mistake):
        runner()
    log("")
    log("== 汇总 ==")
    log("通过 %d / 失败 %d" % (RESULTS["pass"], RESULTS["fail"]))
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
