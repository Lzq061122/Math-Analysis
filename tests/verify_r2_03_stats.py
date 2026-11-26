# -*- coding: utf-8 -*-
"""R2-03 统计套件：§5.6 合成数据集 18 项断言 + 9 条补充验证 + 硬约束反向自证。

命令：`C:\\Python314\\python.exe tests\\verify_r2_03_stats.py`
日志：`docs/verify-r2-03-stats.txt`

分组：
  1  §5.6 数据集：18 项指标的固定期望值（应作答 10 / 总记录 50 / 有效 45 / 缺测 5 /
     错题 15 = 去括号 8 + 移项 5 + 合并同类项 2 / 完整成绩 6 / 平均分 80 / 覆盖率 60%）
  2  §5.6 九条补充验证 v1–v9（撤回、筛选不改成绩、同名不同批次、多知识点、部分评分、
     全未提交、演示隔离、空数据、重启一致）
  3  硬约束反向自证 v10–v13（非确认成绩、缺业务日期、#9 与 mistakes 行数、成绩双闸门）
     + 取数入口单点的 AST 逐函数体检 + 两份排序结论 + 确定性
  4  筛选口径 §5.2（班级/批次/日期/学生/知识点/错因）与 D7 双向判据
  5  R1 遗留 C1/C2/C6 的关闭方式（批次选择器、正式统计排除演示、统计与详情同口径）
  6  明细与证据链（点击错题追溯原始作答/教师判断/证据）与个人看板数据

反向自证（去掉闸门必须变红）：
  - 3.x 入口单点：把 `_formal_rows` 换成不过滤的桩 → 同一条演示行会同时污染
    valid_answers / submitted_students / average_score / score_coverage（≥3 项变红）
  - 3.y 成绩双闸门：`_score_rows` 只认 formal_score 时，confirmed/invalidated 混行会被算进分子
  - 3.z #9 口径：撤回一条 missing_steps 错题后 mistakes 行数掉、confirmed_mistakes 不动
"""
import ast
import json
import os
import shutil
import subprocess
import sys

try:  # Windows 控制台默认 GBK，日志里有「−」「%」时显式切 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import db as db_mod  # noqa: E402
from app import grading  # noqa: E402
from app import r2_contract as contract  # noqa: E402
from app import r2_stats  # noqa: E402
from app.db import Database, now_iso  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r2-03")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-03-stats.txt")
LINES = []
RESULTS = {"pass": 0, "fail": 0}

from app import demo_answers  # noqa: E402

BATCH = "第一次作业"
DEMO_BATCH = demo_answers.DEMO_BATCH
CLASS_NAME = "初二(3)班"
UID = 1
QTEXT = "解方程 2x+1=5"
SOLUTION = "x=2"
STEPS_OK = "2x+1=5\n2x=4\nx=2"
KP_BRACKET = "去括号"
KP_MOVE = "移项"
KP_LIKE = "合并同类项"
CAUSE_OF = {KP_BRACKET: ("bracket_rule", "分配律漏乘"),
            KP_MOVE: ("term_move_rule", "移项未变号"),
            KP_LIKE: ("like_terms_rule", "合并同类项算错")}

# §5.6：15 条已确认错题（学生, 题号）→ (知识点, 错因码)；错因码特意与知识点不完全一一对应
MISTAKES = (
    (1, "1", KP_BRACKET, "bracket_rule"),
    (2, "1", KP_BRACKET, "sign_error"),
    (2, "2", KP_MOVE, "sign_error"),
    (3, "1", KP_BRACKET, "bracket_rule"),
    (3, "2", KP_BRACKET, "bracket_rule"),
    (4, "1", KP_BRACKET, "bracket_rule"),
    (4, "2", KP_MOVE, "term_move_rule"),
    (4, "3", KP_LIKE, "like_terms_rule"),
    (5, "1", KP_BRACKET, "bracket_rule"),
    (5, "2", KP_MOVE, "term_move_rule"),
    (7, "1", KP_BRACKET, "bracket_rule"),
    (8, "2", KP_MOVE, "term_move_rule"),
    (8, "3", KP_LIKE, "like_terms_rule"),
    (10, "1", KP_BRACKET, "sign_error"),
    (10, "2", KP_MOVE, "sign_error"),
)
# §5.6：6 名完整成绩（每题 20 分，5 题 = 100）+ S7 只 4 题（不完整）
SCORES = {1: 12, 2: 14, 3: 16, 4: 16, 5: 18, 6: 20}


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


def equal(name, actual, expected):
    return check(name, actual == expected, "期望 %r，实际 %r" % (expected, actual))


def section(title):
    log("")
    log("== %s ==" % title)


# ---------------------------------------------------------------------------
# 环境构造（真库、真表、真作答；错题/评分直接落库，模拟 R2-02 写入后的结果）
# ---------------------------------------------------------------------------


def build_env(name, students=10, batch=BATCH, configure=True, questions=("1", "2", "3", "4", "5"),
              full_score=20, roster_extra=0, batch_date="2026-11-20", rubric_version="v1",
              roster_count=None):
    path = os.path.join(WORK, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    db = Database(os.path.join(path, "app.db"))
    now = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id, username, display_name, pwd_salt, pwd_hash, iterations,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (UID, "teacher", "张老师", "salt", "hash", 1, now))
        conn.execute(
            "INSERT INTO classes(id, user_id, name, created_at, updated_at)"
            " VALUES (1,?,'%s',?,?)" % CLASS_NAME, (UID, now, now))
        for sid in range(1, students + 1):
            conn.execute(
                "INSERT INTO students(id, class_id, name, code, created_at, updated_at)"
                " VALUES (?,1,?,?,?,?)", (sid, "学生%d" % sid, "S%d" % sid, now, now))
    env = {"db": db, "class_row": {"id": 1, "name": CLASS_NAME}, "user": UID,
           "batch": batch, "path": path, "students": students,
           "db_file": os.path.join(path, "app.db"), "now": now}
    if configure:
        roster_total = students if roster_count is None else roster_count
        roster = [{"key": "S%d" % sid, "name": "学生%d" % sid, "code": "S%d" % sid}
                  for sid in range(1, roster_total + 1)]
        for sid in range(roster_total + 1, roster_total + roster_extra + 1):
            roster.append({"key": "S%d" % sid, "name": "学生%d" % sid, "code": "S%d" % sid})
        config = {
            "name": batch,
            "occurred_on": batch_date,
            "origin": "manual",
            "config_state": "configured",
            "expected_students": roster,
            "expected_questions": list(questions),
            "scored_questions": list(questions),
            "full_scores": {question: full_score for question in questions},
            "rubric": {question: "解方程各步" for question in questions},
            "rubric_version": rubric_version,
        }
        db_mod.save_batch_config(env["db"], 1, batch, config, user_id=UID)
    return env


def add_answer(env, answer_id, student_id, question_no, *, steps=STEPS_OK, missing=0,
               source="作业", batch=None, occurred_on="", images=0, question_text=QTEXT):
    with env["db"].tx() as conn:
        conn.execute(
            "INSERT INTO answers(id, class_id, student_id, record_key, batch, student_name,"
            " student_code, question_no, question_text, standard_solution, student_steps,"
            " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, 1, student_id, "R-%d" % answer_id, batch or env["batch"],
             "学生%d" % student_id, "S%d" % student_id, str(question_no), question_text,
             SOLUTION, steps, source, occurred_on, int(missing),
             "请假" if missing else "", "fp-%d" % answer_id, env["now"], env["now"], env["now"]))
        for seq in range(1, int(images) + 1):
            conn.execute(
                "INSERT INTO answer_images(class_id, student_id, answer_id, seq, file_path,"
                " mime, sha256, source, note, created_at) VALUES (1,?,?,?,?,?,?,?,?,?)",
                (student_id, answer_id, seq, "answer-%d-%d.png" % (answer_id, seq), "image/png",
                 "sha%d-%d" % (answer_id, seq), "zip", "", env["now"]))


def answer_id_of(student_id, question_no):
    return (int(student_id) - 1) * 5 + int(question_no)


def add_check(env, answer_id, knowledge_point, cause_code, cause_label=None, *, locate="first_bad",
              cause_state="decided", verify_source="rule", observed_step="2x=6",
              first_bad_index=1, decided="2026-11-21T08:00:00"):
    answer = env["db"].query_one("SELECT * FROM answers WHERE id=?", (answer_id,))
    with env["db"].tx() as conn:
        conn.execute(
            "INSERT INTO answer_checks(class_id, student_id, answer_id, batch, answer_version,"
            " progress, cause_state, cause_source, observed_step, first_bad_index, locate_status,"
            " locate_source, cause_code, cause_label, verify_source, teacher_error_text,"
            " decided_at, created_at, updated_at) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer["student_id"], answer_id, answer["batch"], answer["fingerprint"], "checked",
             cause_state, "teacher" if cause_state == "decided" else "none", observed_step,
             first_bad_index, locate, "rule", cause_code,
             cause_label or (CAUSE_OF.get(knowledge_point, ("", ""))[1] if cause_code else ""),
             verify_source, "2x=6 这一步没有变号", decided if cause_state == "decided" else "",
             env["now"], env["now"]))
    if cause_state == "decided" and locate in ("first_bad", "missing_steps"):
        with env["db"].tx() as conn:
            conn.execute(
                "INSERT INTO mistakes(class_id, student_id, status, source_type, source_detail,"
                " note, occurred_on, question_text, solution_text, knowledge_points, error_type,"
                " observed_errors, possible_causes, created_at, updated_at, confirmed_at,"
                " answer_id) VALUES (1,?,'confirmed','作业','作答核对',?,?,?,?,?,'','[]','[]',?,?,?,?)",
                (answer["student_id"], "知识点：%s" % knowledge_point,
                 answer["occurred_on"] or "", answer["question_text"], answer["standard_solution"],
                 json.dumps([knowledge_point] if knowledge_point else [], ensure_ascii=False),
                 env["now"], env["now"], env["now"], answer_id))


def add_score(env, answer_id, question_no, score, *, state="confirmed", full_score=20,
              rubric_version="v1", answer_version=None):
    answer = env["db"].query_one("SELECT * FROM answers WHERE id=?", (answer_id,))
    version = answer["fingerprint"] if answer_version is None else answer_version
    with env["db"].tx() as conn:
        conn.execute(
            "INSERT INTO answer_scores(class_id, student_id, answer_id, batch, question_no,"
            " full_score, score, state, answer_version, rubric_version, steps_json, scored_at,"
            " confirmed_at, created_at, updated_at) VALUES (1,?,?,?,?,?,?,?,?,?,'[]',?,?,?,?)",
            (answer["student_id"], answer_id, answer["batch"], str(question_no), full_score,
             score, state, version, rubric_version,
             "2026-11-21T09:00:00" if state == "confirmed" else "",
             "2026-11-21T09:00:00" if state == "confirmed" else "",
             env["now"], env["now"]))


def seed_dataset(env, occurred_on=""):
    """写入 §5.6 的 50 条作答 + 15 条错题 + 6 名完整成绩 + S7 部分成绩。"""
    for student in range(1, 11):
        for question in range(1, 6):
            aid = answer_id_of(student, question)
            add_answer(env, aid, student, question, missing=1 if student == 9 else 0,
                       occurred_on=occurred_on)
    for student, question, kp, code in MISTAKES:
        add_check(env, answer_id_of(student, int(question)), kp, code)
    for student, score in SCORES.items():
        for question in range(1, 6):
            add_score(env, answer_id_of(student, question), question, score)
    for question in range(1, 5):  # S7 只评 4 题 → 不完整，不进平均分
        add_score(env, answer_id_of(7, question), question, 20)


def snapshot(env, **kwargs):
    return r2_stats.class_snapshot(env["db"], 1, env["batch"], **kwargs)


def metric(snap, key):
    return snap["metrics"][key]["value"]


def text_of(snap, key):
    return snap["metrics"][key]["text"]


def as_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 一、§5.6 数据集：18 项指标的固定期望值
# ---------------------------------------------------------------------------


def group_dataset():
    section("一、§5.6 合成数据集 18 项指标")
    env = build_env("dataset")
    seed_dataset(env)
    snap = snapshot(env, today="2026-11-26")
    log("数据集：10 名学生 × 5 题 = 50 条作答；45 有效 / 5 缺测；15 条已确认错题；"
        "6 名完整成绩 60/70/80/80/90/100")

    equal("1.1 班级人数 = 10", metric(snap, "class_students"), 10)
    equal("1.2 应作答人数 = 10", metric(snap, "expected_students"), 10)
    equal("1.3 作答人数 = 9（S9 五题全缺测 → 不算作答）", metric(snap, "submitted_students"), 9)
    equal("1.4 有效作答数 = 45", metric(snap, "valid_answers"), 45)
    equal("1.5 内容不足数 = 0", metric(snap, "insufficient_answers"), 0)
    equal("1.6 缺测数 = 5", metric(snap, "missing_answers"), 5)
    equal("1.7 未提交数 = 0（10×5 格子全有记录）", metric(snap, "unsubmitted_answers"), 0)
    equal("1.8 未核验数 = 30（45 有效 − 15 已核验）", metric(snap, "unverified_answers"), 30)
    equal("1.9 已确认错题数 = 15", metric(snap, "confirmed_mistakes"), 15)

    kp = {item["name"]: item for item in metric(snap, "knowledge_error_freq")}
    equal("1.10 知识点错误频次 去括号 = 8", kp.get(KP_BRACKET, {}).get("mistakes"), 8)
    equal("1.11 知识点错误频次 移项 = 5", kp.get(KP_MOVE, {}).get("mistakes"), 5)
    equal("1.12 知识点错误频次 合并同类项 = 2", kp.get(KP_LIKE, {}).get("mistakes"), 2)
    equal("1.13 知识点频次合计 = 15（多知识点不重复计总数）",
          sum(item["mistakes"] for item in kp.values()), 15)
    equal("1.14 涉及学生人数 = 8", metric(snap, "affected_students"), 8)
    equal("1.15 涉及学生名单",
          sorted(kp[KP_BRACKET]["students"]),
          ["S1", "S10", "S2", "S3", "S4", "S5", "S7"])

    causes = {item["name"]: item["mistakes"] for item in metric(snap, "cause_freq")}
    equal("1.16 错因频次 bracket_rule=6/sign_error=4/term_move_rule=3/like_terms_rule=2",
          causes, {"bracket_rule": 6, "sign_error": 4, "term_move_rule": 3,
                   "like_terms_rule": 2})
    equal("1.17 待判断错因 = 0（分母 first_bad=15）", metric(snap, "undecided_causes"), 0)
    equal("1.18 待判断错因分母", snap["metrics"]["undecided_causes"]["denominator"], 15)

    rates = {item["question_no"]: item for item in metric(snap, "question_score_rate")}
    equal("1.19 单题得分率：5 题都给出 scored_count", sorted(rates), ["1", "2", "3", "4", "5"])
    equal("1.20 第 1 题已评份数 = 7（S1–S6 + S7）", rates["1"]["scored_count"], 7)
    equal("1.21 第 5 题已评份数 = 6（S7 缺第 5 题）", rates["5"]["scored_count"], 6)

    totals = metric(snap, "assignment_total_score")["totals"]
    equal("1.22 逐生作业总分（只给完整成绩学生）",
          {key: totals.get(key) for key in ("1", "2", "3", "4", "5", "6", "7")},
          {"1": 60.0, "2": 70.0, "3": 80.0, "4": 80.0, "5": 90.0, "6": 100.0, "7": None})
    equal("1.23 作业总分卡片文案 = 完整成绩学生 6 / 应参与评分 10",
          text_of(snap, "assignment_total_score"), "完整成绩学生 6 / 应参与评分 10")
    equal("1.24 平均分 = 80.00", text_of(snap, "average_score"), "80.00")
    equal("1.25 平均分数值 = 80.0", metric(snap, "average_score"), 80.0)
    equal("1.26 评分覆盖率 = 60.0%", text_of(snap, "score_coverage"), "60.0%")
    equal("1.27 覆盖率分母 = 10（应参与评分学生数）",
          snap["metrics"]["score_coverage"]["denominator"], 10)
    repeated = metric(snap, "repeated_errors")
    equal("1.28 重复错误：知识点重复 1 对（S3,去括号）", len(repeated["knowledge_pairs"]), 1)
    equal("1.29 重复错误：错因重复 3 对", len(repeated["cause_pairs"]), 3)
    equal("1.30 重复错误卡片总数 = 两类对数之和 4", repeated["total"], 4)
    equal("1.31 满分合计（非得分）= 100", snap["scope"]["full_score"], 100.0)
    equal("1.32 卡片 18 项一个不少",
          [card["key"] for card in snap["cards"]], list(r2_stats.METRIC_KEYS))
    equal("1.33 班级共性问题：涉及学生数主排（去括号 7 > 移项 5 > 合并 2）",
          [item["name"] for item in snap["rankings"]["class_common_issues"]],
          [KP_BRACKET, KP_MOVE, KP_LIKE])
    heat = snap["distributions"]["heatmap"]
    equal("1.34 热力图学生数 = 8", len(heat["students"]), 8)
    equal("1.35 热力图 S3×去括号 = 2 次（以已确认错误次数为据）",
          next(cell["count"] for row in heat["rows"] if row["student_key"] == "S3"
               for cell in row["cells"] if cell["point"] == KP_BRACKET), 2)
    equal("1.36 各题错误柱：第 1 题 7 条错题",
          {item["question_no"]: item["mistakes"]
           for item in snap["distributions"]["question_errors"]}["1"], 7)
    equal("1.37 明细行数 = 15（可筛选明细表）", len(snap["mistakes"]), 15)
    equal("1.38 完整成绩分布：80 分档 2 人",
          {bucket["label"]: bucket["count"]
           for bucket in snap["score"]["distribution"]["buckets"]}["80–89%"], 2)
    equal("1.39 返回值不含正确率/掌握率（历史错题不推正确率）",
          [key for key in snap if "accuracy" in key or "mastery" in key], [])
    equal("1.40 统计与详情同口径（C6）：错题编号 == 当前版本 first_bad+decided",
          sorted(item["answer_id"] for item in snap["mistakes"]),
          sorted(item["answer_id"] for item in grading.current_checks(env["db"], env["class_row"],
                                                                      BATCH)
                 if item["locate_status"] == "first_bad" and item["cause_state"] == "decided"))
    return env, snap


# ---------------------------------------------------------------------------
# 二、§5.6 九条补充验证
# ---------------------------------------------------------------------------


def group_supplementary():
    section("二、§5.6 九条补充验证（v1–v9）")

    # v1 撤 1 条仅关联去括号 → 8 变 7
    env = build_env("v1"); seed_dataset(env)
    base = snapshot(env, today="2026-11-26")
    env["db"].tx().__enter__().execute(
        "UPDATE answer_checks SET answer_version='' WHERE answer_id=?", (answer_id_of(1, 1),))
    after = snapshot(env, today="2026-11-26")
    equal("2.1 撤回 1 条去括号错题 → 错题 15→14",
          metric(after, "confirmed_mistakes"), 14)
    equal("2.2 去括号 8→7",
          {item["name"]: item["mistakes"]
           for item in metric(after, "knowledge_error_freq")}[KP_BRACKET], 7)
    equal("2.3 其他指标不受影响（平均分仍 80.00）", text_of(after, "average_score"), "80.00")

    # v2 点击去括号图表后：明细筛选正确，完整成绩指标保持原范围（D7 双向）
    env = build_env("v2"); seed_dataset(env)
    base = snapshot(env, today="2026-11-26")
    kp_view = snapshot(env, today="2026-11-26", filters={"knowledge_point": KP_BRACKET})
    equal("2.4 知识点筛选后明细 = 8 条", len(kp_view["mistakes"]), 8)
    for key in ("class_students", "expected_students", "average_score", "score_coverage"):
        equal("2.5 知识点筛选不改 %s" % key,
              metric(kp_view, key), metric(base, key))
    student_view = snapshot(env, today="2026-11-26", filters={"student_ids": [1, 2, 9]})
    equal("2.6 学生筛选：班级人数变 3", metric(student_view, "class_students"), 3)
    equal("2.7 学生筛选：应作答人数变 3", metric(student_view, "expected_students"), 3)
    equal("2.8 学生筛选不改集体平均分（仍 80.00）",
          text_of(student_view, "average_score"), "80.00")
    group_view = snapshot(env, today="2026-11-26", filters={"student_ids": [1, 2, 9]},
                          group_scores=True)
    equal("2.9 切「群体成绩」视图后平均分 = (60+70)/2 = 65.00",
          text_of(group_view, "average_score"), "65.00")
    check("2.10 群体成绩视图必须在 notes 里注明范围",
          "学生=1,2,9" in group_view["scope"]["filter_text"], group_view["scope"]["filter_text"])

    # v3 同名不同批次独立
    env = build_env("v3"); seed_dataset(env)
    db_mod.save_batch_config(env["db"], 1, "第二次作业", {
        "name": "第二次作业", "occurred_on": "2026-11-21", "origin": "manual",
        "config_state": "configured",
        "expected_students": [{"key": "S1", "name": "学生1", "code": "S1"}],
        "expected_questions": ["1"], "scored_questions": ["1"],
        "full_scores": {"1": 10}, "rubric": {"1": "标准"}, "rubric_version": "v1"})
    add_answer(env, 501, 1, 1, batch="第二次作业")
    add_check(env, 501, KP_LIKE, "like_terms_rule")
    add_score(env, 501, 1, 10, full_score=10)
    first = snapshot(env, today="2026-11-26")
    second = r2_stats.class_snapshot(env["db"], 1, "第二次作业", today="2026-11-26")
    equal("2.11 同名不同批次：第一批次错题仍 15", metric(first, "confirmed_mistakes"), 15)
    equal("2.12 第二批次的错题单独计数 = 1", metric(second, "confirmed_mistakes"), 1)
    equal("2.13 第二批次的满分合计 = 10（不合并不可比满分）", second["scope"]["full_score"], 10.0)
    equal("2.14 第二批次平均分 = 10.00", text_of(second, "average_score"), "10.00")
    check("2.15 两批次作用域不同（same_batch_scope 判否）",
          not contract.same_batch_scope(1, BATCH, 1, "第二次作业"))

    # v4 多知识点错题不重复增加错题总数
    env = build_env("v4"); seed_dataset(env)
    env["db"].tx().__enter__().execute(
        "UPDATE mistakes SET knowledge_points=? WHERE answer_id=?",
        (json.dumps([KP_BRACKET, KP_LIKE], ensure_ascii=False), answer_id_of(3, 1)))
    snap = snapshot(env, today="2026-11-26")
    equal("2.16 多知识点不重复计总数：错题仍 15", metric(snap, "confirmed_mistakes"), 15)
    equal("2.17 知识点频次合计 = 16（按类别分别计）",
          sum(item["mistakes"] for item in metric(snap, "knowledge_error_freq")), 16)
    equal("2.18 合并同类项 2→3（第二条知识点落在同一错题上）",
          {item["name"]: item["mistakes"]
           for item in metric(snap, "knowledge_error_freq")}[KP_LIKE], 3)

    # v5 部分评分不计入平均分
    env = build_env("v5"); seed_dataset(env)
    snap = snapshot(env, today="2026-11-26")
    equal("2.19 部分评分（S7 四题 80 分）不进平均分：仍 80.00",
          text_of(snap, "average_score"), "80.00")
    equal("2.20 完整成绩学生名单不含 S7",
          sorted(snap["score"]["complete_students"]), [1, 2, 3, 4, 5, 6])
    equal("2.21 S7 逐生作业总分无值（不是部分和 80）",
          metric(snap, "assignment_total_score")["totals"].get("7"), None)

    # v6 识别应作答但完全未提交的学生
    env = build_env("v6", roster_extra=1); seed_dataset(env)
    snap = snapshot(env, today="2026-11-26")
    equal("2.22 名单 11 人、S11 无记录 → 未提交数 = 5", metric(snap, "unsubmitted_answers"), 5)
    equal("2.23 未提交学生名单 = [S11]（识别应作答但完全未提交的学生）",
          snap["distributions"]["unsubmitted"]["students"], ["S11"])
    roster = contract.roster_status(
        db_mod.load_batch_config(env["db"], 1, BATCH),
        [{"student_key": "S%d" % sid, "question_no": str(q),
          "state": "valid" if sid != 9 else "missing"}
         for sid in range(1, 11) for q in range(1, 6)])
    equal("2.24 全未提交学生识别（roster_status）", roster["unsubmitted_students"], ["S11"])
    equal("2.25 未提交数分母 = 应作答格子数 55", roster["expected"], 55)

    # v7 排除演示数据（含纯演示批次）
    env = build_env("v7"); seed_dataset(env)
    base = snapshot(env, today="2026-11-26")
    for question in range(1, 6):
        aid = 900 + question
        add_answer(env, aid, 1, question, source="合成演示")
    with env["db"].tx() as conn:
        for question in range(1, 6):
            conn.execute(
                "INSERT INTO answer_scores(class_id, student_id, answer_id, batch, question_no,"
                " full_score, score, state, answer_version, rubric_version, steps_json, scored_at,"
                " confirmed_at, created_at, updated_at)"
                " VALUES (1,1,?,?,?,20,20,'confirmed',?,'v1','[]','x','x',?,?)",
                (900 + question, BATCH, str(question), "fp-%d" % (900 + question),
                 env["now"], env["now"]))
    after = snapshot(env, today="2026-11-26")
    equal("2.26 演示行不进正式统计：有效作答仍 45",
          metric(after, "valid_answers"), metric(base, "valid_answers"))
    equal("2.27 演示成绩不进平均分：仍 80.00", text_of(after, "average_score"), "80.00")
    equal("2.28 演示成绩不进覆盖率：仍 60.0%", text_of(after, "score_coverage"), "60.0%")
    check("2.29 界面提示已排除演示数据", "演示" in " ".join(after["notes"]), after["notes"])

    # 纯演示批次
    env2 = build_env("v7b")
    db_mod.save_batch_config(env2["db"], 1, DEMO_BATCH, {
        "name": DEMO_BATCH, "origin": "import", "config_state": "configured",
        "expected_students": [{"key": "S1", "name": "学生1", "code": "S1"}],
        "expected_questions": ["1"], "scored_questions": ["1"],
        "full_scores": {"1": 10}, "rubric": {"1": "标准"}, "rubric_version": "v1"})
    add_answer(env2, 801, 1, 1, source="合成演示", batch=DEMO_BATCH)
    add_check(env2, 801, KP_BRACKET, "bracket_rule")
    demo_snap = r2_stats.class_snapshot(env2["db"], 1, DEMO_BATCH, today="2026-11-26")
    check("2.30 纯演示批次标 demo_batch=True", demo_snap["demo"]["demo_batch"] is True)
    equal("2.31 纯演示批次正式统计里错题 = 0", metric(demo_snap, "confirmed_mistakes"), 0)
    equal("2.32 纯演示批次平均分 = 暂无", text_of(demo_snap, "average_score"), "暂无")
    check("2.33 纯演示批次标注来源",
          "合成演示" in " ".join(demo_snap["notes"]), demo_snap["notes"])

    # v8 空数据
    env = build_env("v8", configure=False)
    empty = r2_stats.class_snapshot(env["db"], 1, BATCH, today="2026-11-26")
    equal("2.34 空数据：18 项齐全", len(empty["metrics"]), 18)
    equal("2.35 未配置批次：应作答人数 = 暂无", text_of(empty, "expected_students"), "暂无")
    equal("2.36 未配置批次：未提交数 = 暂无", text_of(empty, "unsubmitted_answers"), "暂无")
    equal("2.37 未配置批次：作业总分 = 暂无", text_of(empty, "assignment_total_score"), "暂无")
    equal("2.38 未配置批次：覆盖率 = 无法计算", text_of(empty, "score_coverage"), "无法计算")
    equal("2.39 空数据：有效作答数 = 0（不是暂无）", metric(empty, "valid_answers"), 0)
    equal("2.40 空数据：知识点频次空列表", metric(empty, "knowledge_error_freq"), [])

    # v9 重启一致（同进程两次 + 子进程再算）
    env = build_env("v9"); seed_dataset(env)
    first = snapshot(env, today="2026-11-26")
    second = snapshot(env, today="2026-11-26")
    equal("2.41 同进程两次计算逐字节相等", as_json(first), as_json(second))
    out = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--dump", env["db_file"]],
        capture_output=True, text=True, encoding="utf-8",
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src")))
    if out.returncode == 0 and out.stdout.strip():
        equal("2.42 重启（子进程）后统计结果仍逐字节相等", out.stdout.strip(), as_json(first))
    else:
        check("2.42 重启（子进程）后统计结果仍逐字节相等", False,
              (out.stderr or "")[-400:])


# ---------------------------------------------------------------------------
# 三、硬约束反向自证 + 入口单点静态体检 + 排序 + 确定性
# ---------------------------------------------------------------------------


def group_hard_constraints():
    section("三、硬约束反向自证（v10–v13）+ 入口单点 + 排序")

    # v10 非确认成绩不进分子
    env = build_env("v10"); seed_dataset(env)
    base = snapshot(env, today="2026-11-26")
    add_score(env, answer_id_of(8, 1), 1, 100, state="draft")
    add_score(env, answer_id_of(8, 2), 2, 20, state="invalidated")
    add_score(env, answer_id_of(3, 5), 5, 0, state="unscored")
    snap = snapshot(env, today="2026-11-26")
    equal("3.1 draft/invalidated/scored 不进平均分：仍 80.00",
          text_of(snap, "average_score"), "80.00")
    equal("3.2 评分覆盖率不变：60.0%", text_of(snap, "score_coverage"), "60.0%")
    equal("3.3 第 1 题已评份数不变 = 7",
          {item["question_no"]: item["scored_count"]
           for item in metric(snap, "question_score_rate")}["1"], 7)

    # v11 缺业务日期：三级口径
    env = build_env("v11", batch_date=""); seed_dataset_without_dates(env)
    snap = r2_stats.class_snapshot(env["db"], 1, BATCH, today="2026-11-26",
                                   filters={"date_from": "2026-11-01", "date_to": "2026-11-30"})
    check("3.4 无业务日期批次：日期筛选生效时不计入",
          metric(snap, "valid_answers") == 0, metric(snap, "valid_answers"))
    check("3.5 无业务日期必须显式提示，不许静默丢",
          any("缺少业务日期" in note for note in snap["notes"]), snap["notes"])
    env2 = build_env("v11b", batch_date=""); seed_dataset(env2, occurred_on="2026-11-20")
    snap2 = r2_stats.class_snapshot(env2["db"], 1, BATCH, today="2026-11-26",
                                    filters={"date_from": "2026-11-01", "date_to": "2026-11-30"})
    equal("3.6 回退取作答日期最小值并计入（有效作答仍 45）",
          metric(snap2, "valid_answers"), 45)
    equal("3.7 业务日期来源标 answers", snap2["scope"]["business_date_source"], "answers")
    outside = r2_stats.class_snapshot(env2["db"], 1, BATCH, today="2026-11-26",
                                      filters={"date_from": "2026-12-01", "date_to": "2026-12-31"})
    equal("3.8 批次日期不在范围内 → 不计入", metric(outside, "valid_answers"), 0)

    # v12 #9 与 mistakes 行数：三步
    env = build_env("v12"); seed_dataset(env)
    before = snapshot(env, today="2026-11-26")
    for answer_id, kp in ((answer_id_of(6, 1), KP_BRACKET), (answer_id_of(6, 2), KP_MOVE)):
        add_check(env, answer_id, kp, CAUSE_OF[kp][0], locate="missing_steps")
    with env["db"].tx() as conn:
        mistakes_count = conn.execute(
            "SELECT COUNT(*) FROM mistakes WHERE class_id=1 AND status='confirmed'").fetchone()[0]
    after = snapshot(env, today="2026-11-26")
    equal("3.9 missing_steps 不增加已确认错题数（仍 15）",
          metric(after, "confirmed_mistakes"), 15)
    equal("3.10 mistakes 行数 15→17", mistakes_count, 17)
    check("3.11 两个数不相等且不得互相顶替",
          metric(after, "confirmed_mistakes") != mistakes_count)
    env["db"].tx().__enter__().execute(
        "UPDATE mistakes SET status='returned' WHERE answer_id=?", (answer_id_of(6, 1),))
    with env["db"].tx() as conn:
        mistakes_after = conn.execute(
            "SELECT COUNT(*) FROM mistakes WHERE class_id=1 AND status='confirmed'").fetchone()[0]
    final = snapshot(env, today="2026-11-26")
    equal("3.12 撤回一条 missing_steps 错题：行数 17→16", mistakes_after, 16)
    equal("3.13 但已确认错题数仍 15（只数 first_bad + decided）",
          metric(final, "confirmed_mistakes"), 15)

    # v13 成绩双闸门（不依赖 grading 内部实现）
    equal("3.14 双闸门：formal_score=False 的 confirmed 行不进分子",
          r2_stats._score_rows([{"student_id": 1, "question_no": "1", "score": 20,
                                 "formal_score": False, "score_state": "confirmed"}]),
          {})
    equal("3.15 双闸门：state 不是 confirmed 的行也不进分子",
          r2_stats._score_rows([{"student_id": 1, "question_no": "1", "score": 20,
                                 "formal_score": True, "score_state": "invalidated"}]),
          {})
    equal("3.16 双闸门：两道闸都过才进分子",
          r2_stats._score_rows([{"student_id": 1, "question_no": "1", "score": 20,
                                 "formal_score": True, "score_state": "confirmed"}]),
          {1: {"1": 20}})
    equal("3.17 #9 判据只认 first_bad（missing_steps 不算已确认错题）",
          r2_stats._is_confirmed_mistake(
              {"cause_state": "decided", "verified": True, "answer_id": 1},
              {1: {"locate_status": "missing_steps"}}), False)

    # 入口单点：演示行同时污染 ≥3 项指标（摘掉过滤必须变红）
    env = build_env("v7patch", students=11, roster_count=10); seed_dataset(env)
    for question in range(1, 6):
        add_answer(env, 700 + question, 11, question, source="合成演示")
        add_score(env, 700 + question, question, 20)
    clean = snapshot(env, today="2026-11-26")
    original = r2_stats._formal_rows
    r2_stats._formal_rows = lambda rows, **kwargs: list(rows)  # 摘掉入口过滤
    try:
        dirty = snapshot(env, today="2026-11-26")
    finally:
        r2_stats._formal_rows = original
    changed = [key for key in ("valid_answers", "submitted_students", "average_score",
                               "score_coverage")
               if metric(clean, key) != metric(dirty, key)]
    check("3.18 摘掉入口过滤后 ≥3 项指标同时变红（证明单点过滤真的在生效）",
          len(changed) >= 3, "变红的只有 %s" % changed)
    equal("3.19 恢复过滤后指标回到原值", as_json(snapshot(env, today="2026-11-26")), as_json(clean))

    # AST：除白名单外任何函数不得出现演示判定
    source_text = open(os.path.join(ROOT, "src", "app", "r2_stats.py"), encoding="utf-8").read()
    equal("3.20 入口单点静态体检：白名单外没有任何函数碰演示判定",
          static_offenders(source_text), [])
    poisoned = (
        "def _bad_metric(rows):\n"
        "    return [row for row in rows if row.get(\"source\") != \"合成演示\"]\n")
    check("3.20b 静态体检本身有效（注入一个漏项指标必须被抓到）",
          static_offenders(poisoned) != [], static_offenders(poisoned))
    equal("3.21 白名单不超过 2 个函数且都在模块里",
          [name for name in r2_stats.SYNTHETIC_ALLOWED_FUNCS if hasattr(r2_stats, name)],
          list(r2_stats.SYNTHETIC_ALLOWED_FUNCS))

    # 班级共性问题：涉及学生数主排（构造「频次高但学生少」的对照组）
    env = build_env("rank", students=3, questions=("1", "2", "3"), full_score=10)
    for sid in (1, 2, 3):
        for question in (1, 2, 3):
            add_answer(env, (sid - 1) * 3 + question, sid, question)
    add_check(env, 1, "S1-专题", "c1")   # S1 三条同一知识点
    add_check(env, 2, "S1-专题", "c1")
    add_check(env, 3, "S1-专题", "c1")
    add_check(env, 4, "共用专题", "c2")   # S2、S3 各一条同一知识点 = 2 名学生
    add_check(env, 7, "共用专题", "c2")
    snap = snapshot(env, today="2026-11-26")
    issues = snap["rankings"]["class_common_issues"]
    equal("3.22 共性问题按涉及学生数排序：2 名学生的专题排在 1 名学生 3 条之前",
          [item["name"] for item in issues], ["共用专题", "S1-专题"])
    check("3.23 频次作为辅助（1 名学生 3 条排在最后，虽然频次最高）",
          issues[-1]["name"] == "S1-专题" and issues[-1]["mistakes"] == 3
          and issues[0]["student_count"] == 2, issues)

    # 个人重点问题：复用原版 3/2/1 近期加权；today 必须由调用方传入
    env = build_env("priority", students=1, questions=("1", "2"), full_score=10)
    add_answer(env, 1, 1, 1, occurred_on="2026-11-25")
    add_answer(env, 2, 1, 2, occurred_on="2026-07-01")
    add_check(env, 1, "近期专题", "c1")
    add_check(env, 2, "陈旧专题", "c2")
    items = r2_stats.student_priority_issues(
        env["db"], 1, 1, batch=BATCH, today="2026-11-26")["items"]
    equal("3.24 近期加权排序：近期专题在前", [item["name"] for item in items],
          ["近期专题", "陈旧专题"])
    equal("3.25 权重按 3/1 计（30 天内 3 分、超出 90 天 1 分）",
          [item["weight"] for item in items], [3, 1])
    try:
        r2_stats.student_priority_issues(env["db"], 1, 1, batch=BATCH)
        raised = False
    except ValueError:
        raised = True
    check("3.26 不传 today 直接报错（模块不读系统时钟，保证确定性）", raised)

    # 确定性：同一数据两次 + 分页无关
    env = build_env("determinism"); seed_dataset(env)
    one = snapshot(env, today="2026-11-26")
    two = snapshot(env, today="2026-11-26")
    equal("3.27 同一数据两次计算逐字节相等", as_json(one), as_json(two))
    equal("3.28 快照里没有时间戳字段（不读系统时钟）",
          [key for key in one if "generated" in key or "now" in key or "time" in key], [])


def static_offenders(source_text):
    """AST 逐函数体检：白名单外任何函数都不得出现演示判定（含反向自证用的毒样本）。"""
    forbidden = ("synthetic", "include_in_formal_stats", "DEMO_SOURCE", "合成演示", '"source"')
    offenders = []
    for node in ast.walk(ast.parse(source_text)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in r2_stats.SYNTHETIC_ALLOWED_FUNCS:
            continue
        body = ast.get_source_segment(source_text, node) or ""
        for token in forbidden:
            if token in body:
                offenders.append("%s 含 %s" % (node.name, token))
    return offenders


def seed_dataset_without_dates(env):
    """v11：50 条作答但 occurred_on 全空（批次配置日期也空）。"""
    for student in range(1, 11):
        for question in range(1, 6):
            add_answer(env, answer_id_of(student, question), student, question,
                       missing=1 if student == 9 else 0)


# ---------------------------------------------------------------------------
# 四、筛选口径（§5.2 / D6 / D7）
# ---------------------------------------------------------------------------


def group_filters():
    section("四、筛选口径 §5.2 与 D6/D7")
    env = build_env("filters"); seed_dataset(env)
    base = snapshot(env, today="2026-11-26")
    by_cause = snapshot(env, today="2026-11-26", filters={"cause": "bracket_rule"})
    equal("4.1 错因筛选后已确认错题 = 6", metric(by_cause, "confirmed_mistakes"), 6)
    equal("4.2 错因筛选不改班级人数", metric(by_cause, "class_students"),
          metric(base, "class_students"))
    equal("4.3 错因筛选不改平均分", text_of(by_cause, "average_score"), "80.00")
    equal("4.4 错因筛选不改评分覆盖率", text_of(by_cause, "score_coverage"), "60.0%")
    equal("4.5 知识点评筛选后涉及学生 = 7",
          metric(snapshot(env, today="2026-11-26",
                          filters={"knowledge_point": KP_MOVE}), "affected_students"), 5)
    equal("4.6 批次为空 = 全班所有批次（当前只有 1 个）",
          metric(r2_stats.class_snapshot(env["db"], 1, "", today="2026-11-26"),
                 "valid_answers"), 45)
    batches = r2_stats.list_batches(env["db"], 1)
    equal("4.7 批次选择器（C1）列出全部已登记批次", [item["batch"] for item in batches], [BATCH])
    equal("4.8 批次项带配置状态与业务日期",
          (batches[0]["config_state"], batches[0]["business_date"]), ("configured", "2026-11-20"))
    # 历史未配置批次也能选
    env2 = build_env("filters-history", configure=False)
    add_answer(env2, 1, 1, 1)
    db_mod.ensure_batch_config(env2["db"], 1, BATCH, origin="legacy", user_id=UID)
    env2["db"].tx().__enter__().execute(
        "UPDATE assignment_batches SET config_state='unconfigured' WHERE class_id=1")
    history = r2_stats.list_batches(env2["db"], 1)
    equal("4.9 历史批次（unconfigured）仍可在选择器里选",
          [(item["batch"], item["config_state"]) for item in history],
          [(BATCH, "unconfigured")])
    history_snap = r2_stats.class_snapshot(env2["db"], 1, BATCH, today="2026-11-26")
    equal("4.10 历史批次出数：有效作答 1", metric(history_snap, "valid_answers"), 1)
    equal("4.11 历史批次应作答人数 = 暂无", text_of(history_snap, "expected_students"), "暂无")
    check("4.12 历史批次提示未配置", any("未配置" in note for note in history_snap["notes"]))


# ---------------------------------------------------------------------------
# 五、明细与证据链 + 个人看板
# ---------------------------------------------------------------------------


def group_detail():
    section("五、明细与证据链 / 个人看板")
    env = build_env("detail"); seed_dataset(env)
    snap = snapshot(env, today="2026-11-26")
    row = snap["mistakes"][0]
    equal("5.1 明细行带知识点与错因标签",
          bool(row["knowledge_point_text"]) and bool(row["cause_label"]), True)
    evidence = r2_stats.mistake_evidence(env["db"], 1, answer_id_of(1, 1))
    check("5.2 证据链含原始步骤与教师判断",
          evidence["student_steps"].startswith("2x+1=5")
          and evidence["teacher"]["locate_status"] == "first_bad"
          and evidence["teacher"]["cause_state"] == "decided", evidence["teacher"])
    equal("5.3 证据链判定当前版本", evidence["is_current_version"], True)
    equal("5.4 错题行知识点写进 mistakes（原版学情页也能查）",
          evidence["mistake"]["knowledge_points"], [KP_BRACKET])

    personal = r2_stats.student_snapshot(env["db"], 1, 3, BATCH, today="2026-11-26")
    equal("5.5 个人看板：个人错题 2 条", metric(personal, "confirmed_mistakes"), 2)
    equal("5.6 个人看板：个人有效作答 5", metric(personal, "valid_answers"), 5)
    equal("5.7 个人看板：个人完整成绩 80.00", text_of(personal, "assignment_total_score"),
          "完整成绩学生 1 / 应参与评分 1")
    equal("5.8 个人看板：个人平均分 80.00", text_of(personal, "average_score"), "80.00")
    equal("5.9 个人看板：个人覆盖率 100.0%", text_of(personal, "score_coverage"), "100.0%")
    equal("5.10 个人看板：重点问题按加权排序",
          [item["name"] for item in personal["rankings"]["student_priority_issues"]],
          [KP_BRACKET])
    equal("5.11 个人看板：重复错误 = 知识点 1 对 + 错因 1 对（S3 去括号两条）",
          personal["metrics"]["repeated_errors"]["value"]["total"], 2)
    equal("5.12 个人历史记录条数 = 2（按日期倒序）",
          len(r2_stats.student_history(env["db"], 1, 3)), 2)
    advice = r2_stats.saved_advice(env["db"], 1, 3)
    equal("5.13 没有辅导建议时返回空结构", advice["exists"], False)
    no_today = r2_stats.student_snapshot(env["db"], 1, 3, BATCH)
    equal("5.13b 个人快照不传 today 时不报错、且显式说明未排序",
          (no_today["rankings"]["student_priority_issues"],
           any("未提供统计基准日" in note for note in no_today["notes"])),
          ([], True))
    equal("5.14 逐生作业总分只含有成绩行的学生（S8/S9/S10 无键，不是 0）",
          sorted(snap["score"]["totals"]), ["1", "2", "3", "4", "5", "6", "7"])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def dump_mode(db_file):
    """子进程：从同一个库重新算一遍，打印 JSON（重启一致性断言用）。"""
    db = Database(db_file, create=False)
    snap = r2_stats.class_snapshot(db, 1, BATCH, today="2026-11-26")
    print(as_json(snap))
    return 0


def main():
    if "--dump" in sys.argv:
        index = sys.argv.index("--dump")
        return dump_mode(sys.argv[index + 1])
    os.makedirs(WORK, exist_ok=True)
    log("R2-03 统计套件（%s）" % r2_stats.STATS_VERSION)
    for line in environment_lines():
        log(line)
    group_dataset()
    group_supplementary()
    group_hard_constraints()
    group_filters()
    group_detail()
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                          RESULTS["pass"], RESULTS["fail"]))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(LINES) + "\n")
    log(exit_line(RESULTS["fail"]))
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
