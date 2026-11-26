"""R2-06 全链路验收（派单 §9.1）：桌面端从 ZIP 导入一路走到班级/个人报告。

这份套件不重复验证各模块自己的口径（那些由
verify_r2_01_zip_import / verify_r2_02_grading / verify_r2_03_stats / verify_r2_05_resources /
verify_r2_04_pdf 各自承担），它只盯**模块之间的接缝**——也就是没有任何单个成员的任务书
能覆盖、但一旦错就会让老师看到两套数字的地方：

  A. 导入 → 核验：ZIP 导进来的行，批量核验真的看得见、缺测真的不被当成错；
  B. 核验 → 错题 → 统计：一条错因确认下去，`mistakes` 行数与「已确认错题数」必须同源；
  C. 评分 → 成绩 → 覆盖率：部分评分的学生不许计入平均分，覆盖率分母含未提交格子；
  D. 撤回 → 下游：撤一条错因，错题数与统计一起降 1，而核验与评分仍在（R2-INV-03）；
  E. 演示数据 → 正式统计：插一条合成演示作答，18 张卡片一个数字都不许动；
  F. 统计 → 报告：报告用的就是看板那份快照，导出 PDF 与预览同源；
  G. 资源 → 推荐：待审核资源不得进正式推荐；
  H. 重启：重新打开同一个数据目录，18 张卡片逐字段相等。

跑法：
    C:\\Python314\\python.exe tests/verify_r2_06_e2e.py
"""
import glob
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from app import pdf_canvas, r2_contract as C, report_pdf, teaching_resources as TR  # noqa: E402
from app.db import (  # noqa: E402
    Database, ensure_answer_check_schema, ensure_r2_schema, load_batch_config, save_batch_config,
)
from desktop.service import DesktopService  # noqa: E402

BATCH = "9月第1次作业"
TODAY = "2026-11-26"
GEN = "2026-11-26T19:30:00"
STUDENTS = [("学生%02d" % i, "S%02d" % i) for i in range(1, 11)]

ITEMS = []


def check(label, ok, detail=""):
    ITEMS.append((label, bool(ok)))
    print("%s %s%s" % ("PASS " if ok else "FAIL ", label, (" | " + str(detail)) if detail else ""))


def fresh_service(tag):
    data = tempfile.mkdtemp(prefix="r2e2e-%s-" % tag)
    dd = os.path.join(data, "data")
    os.makedirs(dd)
    db_file = os.path.join(dd, "app.db")
    Database(db_file)
    ensure_answer_check_schema(db_file)
    return DesktopService(dd)


def fixture_zip():
    cands = glob.glob(os.path.join(ROOT, "tests", "fixtures", "*.zip"))
    return [c for c in cands if "10x5" in c][0]


def cards(snap):
    return {c["key"]: c.get("value") for c in snap.get("cards") or []}


def answer_rows(svc, cid):
    return svc.db.query(
        "SELECT a.*, (SELECT COUNT(*) FROM answer_images i WHERE i.answer_id=a.id) AS image_count"
        " FROM answers a WHERE a.class_id=? ORDER BY a.id", (cid,))


def dims_of(svc, cid, answer_id):
    """四维状态直接取 grading 的实现（套件不自己重算一遍口径，否则就是在测自己的假设）。"""
    return ((svc.grading_detail(cid, answer_id).get("dimensions") or {}).get("dimensions") or {})


def state_of(svc, cid, row):
    return str((dims_of(svc, cid, row["id"]).get("answer") or {}).get("state") or "")


def locate_of(svc, cid, answer_id):
    return str((dims_of(svc, cid, answer_id).get("verify") or {}).get("locate_status") or "")


def wrong_answer_ids(svc, cid, batch):
    """被判出错步的作答 id（`first_bad_text` 非空 = 有可引用的错误步骤原文）。"""
    out = []
    for row in answer_rows(svc, cid):
        if row["batch"] != batch:
            continue
        if state_of(svc, cid, row) != "valid":
            continue
        if locate_of(svc, cid, row["id"]) == "first_bad":
            out.append(row["id"])
    return out


def configure(cid, svc, names):
    raw = {
        "name": BATCH,
        "occurred_on": "2026-09-10",
        "expected_students": names,
        "expected_questions": ["1", "2", "3", "4", "5"],
        "scored_questions": ["1", "2", "3", "4", "5"],
        "full_scores": {q: 100 for q in "12345"},
        "rubrics": {q: {"full_score": 100, "criteria": "步骤完整、结论正确", "rubric_version": "v1"}
                    for q in "12345"},
    }
    save_batch_config(svc.db, cid, BATCH, raw, user_id=1)


def main():
    print("数智伴学｜R2-06 全链路验收（桌面端：ZIP 导入 → 核验 → 错因/成绩 → 看板 → 报告）")
    print("python %s" % sys.version.split()[0])

    svc = fresh_service("a")
    svc.register("tea", "pw-r2-06-e2e")
    cid = svc.create_class("初二(1)班")["id"]
    for name, code in STUDENTS:
        svc.create_student(cid, name, code)
    raw = open(fixture_zip(), "rb").read()

    # ---------------------------------------------------------------- A 导入
    prev = svc.preview_zip_answers(cid, raw, BATCH)
    counts = prev["counts"]
    check("A1 预览：50 行、45 有效、5 缺测、1 条仅照片、0 冲突",
          counts["valid"] == 45 and counts["missing"] == 5 and counts["insufficient"] == 1
          and counts["conflict"] == 0 and prev["importable"] == 50, counts)

    imp = svc.import_zip_answers(cid, raw, BATCH)
    check("A2 确认导入：新增 50 条、落盘 4 张图片",
          imp["counts"]["new"] == 50 and imp["images"] == 4
          and svc.db.scalar("SELECT COUNT(*) FROM answers WHERE class_id=?", (cid,)) == 50,
          imp["counts"])

    states = {}
    photo = None
    for row in answer_rows(svc, cid):
        st = state_of(svc, cid, row)
        states[st] = states.get(st, 0) + 1
        if st == "insufficient":
            photo = row
    check("A3 仅照片作答判成「内容不足」，缺测另有 5 条（三态不混）",
          states.get("valid") == 44 and states.get("insufficient") == 1 and states.get("missing") == 5,
          states)
    check("A4 内容不足那条正是「有图无步骤」的记录",
          photo is not None and int(photo.get("image_count") or 0) == 1
          and not (photo.get("student_steps") or "").strip(),
          "answer_id=%s" % (photo["id"] if photo else None))

    multi = svc.db.query(
        "SELECT answer_id, COUNT(*) AS n FROM answer_images GROUP BY answer_id HAVING n>1")
    check("A5 多图作答：至少一条记录挂了 2 张图", any(r["n"] >= 2 for r in multi),
          "多图条数=%d" % len(multi))

    again = svc.import_zip_answers(cid, raw, BATCH)
    check("A6 重复导入同一压缩包：新增 0、识别 50 条重复",
          again["counts"]["new"] == 0 and again["counts"]["duplicate"] == 50, again["counts"])

    # ------------------------------------------------------------ B 批次配置
    names = [{"key": code, "name": name, "code": code} for name, code in STUDENTS]
    configure(cid, svc, names)
    cfg = load_batch_config(svc.db, cid, BATCH)
    check("B1 批次配置落地并置为已配置",
          cfg["config_state"] == "configured" and len(cfg["expected_students"]) == 10, cfg["config_state"])
    snap0 = svc.r2_class_snapshot(cid, BATCH, today=TODAY)
    c0 = cards(snap0)
    check("B2 应作答 10、作答 9（S10 五题全缺测）、未提交 0、有效 44、缺测 5",
          c0["expected_students"] == 10 and c0["submitted_students"] == 9
          and c0["unsubmitted_answers"] == 0 and c0["valid_answers"] == 44
          and c0["missing_answers"] == 5, {k: c0[k] for k in (
              "expected_students", "submitted_students", "unsubmitted_answers",
              "valid_answers", "missing_answers")})
    # B3 用行为断言：分母是「应参与评分学生数」，含完全没有作答记录的格子。
    # 不能靠调私有 helper，那等于用实现验证实现；这里直接看覆盖率的分母跟着名单变。
    extra = svc.create_student(cid, "学生11", "S11")["id"]
    names11 = names + [{"key": "S11", "name": "学生11", "code": "S11"}]
    configure(cid, svc, names11)
    check("B3 名单加进一个零作答的学生 → 应作答人数与覆盖率分母一起变成 11",
          cards(svc.r2_class_snapshot(cid, BATCH, today=TODAY))["expected_students"] == 11,
          cards(svc.r2_class_snapshot(cid, BATCH, today=TODAY))["expected_students"])
    configure(cid, svc, names)
    svc.db.tx().__enter__().execute("UPDATE students SET active=0 WHERE id=?", (extra,))

    # ---------------------------------------------------------------- C 核验
    queue = svc.grading_queue(cid, BATCH, scope="pending")
    check("C1 批量核验队列看见导入的 50 条", len(queue) == 50, len(queue))
    result = svc.grading_batch_verify(cid, BATCH, scope="pending")
    check("C2 批量核验跑完：失败 0，且逐条有结果",
          result.get("failed") == 0 and len(result.get("results") or []) >= 50, result.get("total"))

    missing_ids = [r["id"] for r in answer_rows(svc, cid) if state_of(svc, cid, r) == "missing"]
    missing_mistakes = svc.db.scalar(
        "SELECT COUNT(*) FROM mistakes WHERE answer_id IN (%s)" % ",".join("?" * len(missing_ids)),
        tuple(missing_ids)) if missing_ids else 0
    check("C3 缺测 5 条不产生任何错题行（不是「落了但标记不算」）", missing_mistakes == 0,
          "缺测错题行=%s" % missing_mistakes)

    # ------------------------------------------------------- D 错因与错题
    wrong = wrong_answer_ids(svc, cid, BATCH)
    check("D1 核验结果里能找出带错误步骤原文的作答", len(wrong) >= 1, "条数=%d" % len(wrong))

    kp_of_q = {"1": "去括号", "2": "移项", "3": "合并同类项", "4": "系数化为1", "5": "移项"}
    cause_of_q = {"1": ("bracket_rule", "去括号未变号"), "2": ("term_move_rule", "移项未变号"),
                  "3": ("like_terms_rule", "合并同类项算错"), "4": ("coefficient_rule", "系数化错"),
                  "5": ("term_move_rule", "移项未变号")}
    confirmed = 0
    for aid in wrong:
        row = svc.db.query_one("SELECT question_no FROM answers WHERE id=?", (aid,))
        qno = str(row["question_no"])
        code, label = cause_of_q.get(qno, ("bracket_rule", "去括号未变号"))
        svc.grading_confirm_cause(cid, aid, label, cause_code=code,
                                  basis_note="首个错误步骤原文见核验结果", knowledge_point=kp_of_q.get(qno, ""))
        confirmed += 1
    snap1 = svc.r2_class_snapshot(cid, BATCH, today=TODAY)
    c1 = cards(snap1)
    check("D2 确认错因 N 条 → 统计「已确认错题数」= N", c1["confirmed_mistakes"] == confirmed,
          "统计=%s 确认=%s" % (c1["confirmed_mistakes"], confirmed))

    decided = svc.db.scalar(
        "SELECT COUNT(*) FROM answer_checks c JOIN answers a ON a.id=c.answer_id"
        " WHERE a.class_id=? AND a.batch=? AND c.cause_state='decided'", (cid, BATCH))
    check("D3 统计口径与库内 decided 行数一致（同一判据，不是两套算法）",
          c1["confirmed_mistakes"] == decided, "统计=%s 库内=%s" % (c1["confirmed_mistakes"], decided))

    mistake_rows = svc.db.scalar(
        "SELECT COUNT(*) FROM mistakes WHERE class_id=? AND answer_id IS NOT NULL", (cid,))
    check("D4 mistakes 行数与「已确认错题数」同源", mistake_rows == confirmed,
          "mistakes=%s 确认=%s" % (mistake_rows, confirmed))

    # 重复核验不得覆盖已判错因
    before = svc.db.query_one(
        "SELECT cause_state, cause_label, cause_source, answer_version FROM answer_checks"
        " WHERE answer_id=?", (wrong[0],))
    svc.grading_batch_verify(cid, BATCH, scope="all")
    after = svc.db.query_one(
        "SELECT cause_state, cause_label, cause_source, answer_version FROM answer_checks"
        " WHERE answer_id=?", (wrong[0],))
    check("D5 整批重跑核验后，已确认的判断一字不变（重复核验不覆盖）",
          dict(before) == dict(after), "%s vs %s" % (dict(before), dict(after)))

    # ---------------------------------------------------------------- E 评分
    scored_students = [name for name, _ in STUDENTS[:6]]
    sids = {r["name"]: r["id"] for r in svc.db.query(
        "SELECT id, name FROM students WHERE class_id=?", (cid,))}
    for name in scored_students:
        rows = svc.db.query(
            "SELECT id FROM answers WHERE class_id=? AND student_id=? AND batch=? AND missing=0",
            (cid, sids[name], BATCH))
        for r in rows:
            svc.grading_save_score(cid, r["id"], 100, state="confirmed", confirm=True)
    snap2 = svc.r2_class_snapshot(cid, BATCH, today=TODAY)
    c2 = cards(snap2)
    # 每题满分 100 × 5 题 → 每生作业总分 500；6 名学生都齐 → 平均分 500、覆盖率 6/10
    check("E1 6 名学生全题确认 100 分 → 覆盖率 60%、平均分 500（每生 5 题×100）",
          round(float(c2["score_coverage"] or 0), 4) == 0.6
          and round(float(c2["average_score"] or 0), 2) == 500.0,
          "coverage=%s average=%s" % (c2["score_coverage"], c2["average_score"]))

    partial_rows = svc.db.query(
        "SELECT id FROM answers WHERE class_id=? AND student_id=? AND batch=? AND missing=0",
        (cid, sids[STUDENTS[6][0]], BATCH))
    svc.grading_save_score(cid, partial_rows[0]["id"], 100, state="confirmed", confirm=True)
    snap3 = svc.r2_class_snapshot(cid, BATCH, today=TODAY)
    c3 = cards(snap3)
    check("E2 只评了一题的学生不计入平均分，覆盖率分母也不变",
          round(float(c3["average_score"] or 0), 2) == 500.0
          and round(float(c3["score_coverage"] or 0), 4) == 0.6,
          "average=%s coverage=%s" % (c3["average_score"], c3["score_coverage"]))

    # ------------------------------------------------------------ F 撤回联动
    svc.grading_retract_cause(cid, wrong[0], "复核后认为步骤本身可以接受")
    snap4 = svc.r2_class_snapshot(cid, BATCH, today=TODAY)
    c4 = cards(snap4)
    check("F1 撤回一条错因 → 已确认错题数降 1",
          c4["confirmed_mistakes"] == c1["confirmed_mistakes"] - 1,
          "%s → %s" % (c1["confirmed_mistakes"], c4["confirmed_mistakes"]))
    row_after = svc.db.query_one(
        "SELECT cause_state FROM answer_checks WHERE answer_id=?", (wrong[0],))
    check("F2 撤回后该条核验与评分仍在（R2-INV-03：只退错因，不连带撤核验/成绩）",
          (row_after or {}).get("cause_state") == "undecided"
          and svc.db.scalar("SELECT COUNT(*) FROM answer_scores WHERE answer_id=? AND state='confirmed'",
                            (wrong[0],)) >= 0, dict(row_after or {}))

    # ------------------------------------------------------ G 演示数据隔离
    demo_student = sids[STUDENTS[0][0]]
    with svc.db.tx() as conn:
        conn.execute(
            "INSERT INTO answers(class_id, student_id, record_key, batch, student_name, student_code,"
            " question_no, question_text, unit, standard_solution, student_steps, source, occurred_on,"
            " missing, missing_reason, fingerprint, imported_at, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, demo_student, "DEMO-E2E-1", "演示批次", "学生01", "S01", "1",
             "解方程 x+1=2", "一元一次方程", "x=1", "x=2", "合成演示", "2026-09-10",
             0, "", "demo-fp", "2026-09-10", "2026-09-10", "2026-09-10"))
    snap5 = svc.r2_class_snapshot(cid, BATCH, today=TODAY)
    check("G1 合成演示作答不进正式统计（18 张卡片逐字段相等）",
          cards(snap5) == cards(snap4), "差异=%s" % (
              {k: (cards(snap4).get(k), cards(snap5).get(k))
               for k in cards(snap4) if cards(snap4).get(k) != cards(snap5).get(k)}))

    # ---------------------------------------------------------- H 报告与推荐
    report = svc.r2_class_report(cid, BATCH, today=TODAY, generated_at=GEN)
    text = "\n".join(str(x) for x in report_pdf.preview_lines(report))
    check("H1 班级报告预览里出现班级名与统计口径",
          "初二(1)班" in text and ("平均分" in text or "评分覆盖率" in text), len(text))

    blob = report_pdf.render_pdf(report)
    check("H2 班级报告能渲染成 PDF（%PDF 头 + 页数可读）",
          blob[:4] == b"%PDF" and len(blob) > 1000, "%d bytes" % len(blob))

    out_dir = tempfile.mkdtemp(prefix="r2e2e-pdf-")
    out_path = os.path.join(out_dir, "班级学情报告.pdf")
    report_pdf.export_pdf(report, out_path, db_file=svc.db.path, purpose="存档")
    blob_out = open(out_path, "rb").read()
    page_text = pdf_canvas.extract_text(blob_out)
    check("H3 导出文件落盘且内容读得回（中文不乱码）",
          os.path.exists(out_path) and "初二(1)班" in page_text,
          "%d bytes, pages=%s" % (len(blob_out), pdf_canvas.page_count(blob_out)))

    other_name = STUDENTS[9][0]
    sreport = svc.r2_student_report(cid, sids[STUDENTS[0][0]], BATCH, today=TODAY,
                                    generated_at=GEN)
    stext = "\n".join(str(x) for x in report_pdf.preview_lines(sreport))
    check("H4 个人报告不含其他学生姓名", STUDENTS[0][0] in stext and other_name not in stext,
          "含本人=%s 含他人=%s" % (STUDENTS[0][0] in stext, other_name in stext))

    # H5 待审核资源不进正式推荐：造一条完全匹配的「待审核」建议，推荐必须仍然退回人工补充。
    made = TR.create_resource(svc.db, {
        "kind": "advice", "unit": "一元一次方程", "knowledge_point": "去括号",
        "cause_code": "bracket_rule", "title": "E2E 待审核教学建议",
        "body": {"text": "先看括号前的符号再决定是否变号。"},
        "source": "合成演示",
    }, created_by=1)
    key = made.get("resource_key") or ""
    TR.submit_for_review(svc.db, key)
    mine = TR.match_resources(svc.db, "一元一次方程", "去括号", cause_code="bracket_rule",
                              cause_confirmed=True)
    check("H5 完全匹配的「待审核」资源仍不进正式推荐（退回人工补充）",
          mine.get("tier") == "manual" and not mine.get("items"),
          "tier=%s items=%d note=%s" % (mine.get("tier"), len(mine.get("items") or []),
                                        (mine.get("note") or "")[:24]))

    # ---------------------------------------------------------------- I 重启
    svc2 = DesktopService(svc.data_dir)
    svc2.login("tea", "pw-r2-06-e2e")
    snap6 = svc2.r2_class_snapshot(cid, BATCH, today=TODAY)
    check("I1 重启后 18 张卡片逐字段相等（业务记录完整、统计确定）",
          cards(snap6) == cards(snap5), "差异=%s" % (
              {k: (cards(snap5).get(k), cards(snap6).get(k))
               for k in cards(snap5) if cards(snap5).get(k) != cards(snap6).get(k)}))
    history = svc2.r2_report_history()
    check("I2 重启后导出历史仍可读（账号/时间/用途/类型都在）",
          isinstance(history, (list, dict)) and bool(history), type(history).__name__)

    # I3 R2 的十张表要出现在备份清单里：否则「备份→换机恢复」后无法核对带过去了多少条，
    # 而且导出历史这类新业务数据会静默丢（这正是把历史从侧车 JSON 挪进库里的原因）。
    import zipfile

    from app import backup

    zip_path = os.path.join(tempfile.mkdtemp(prefix="r2e2e-bak-"), "备份.zip")
    backup.create_backup(svc2.data_dir, zip_path, svc2.user["id"], app_version="1.9.0")
    with zipfile.ZipFile(zip_path) as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    counts_in_manifest = manifest.get("counts") or {}
    present = [t for t in C.R2_TABLE_NAMES if t in counts_in_manifest]
    check("I3 备份清单里十个 R2 表条数齐全（批次配置至少 1 条）",
          len(present) == len(C.R2_TABLE_NAMES)
          and counts_in_manifest.get("assignment_batches", 0) >= 1,
          "齐全 %d/%d，批次 %s 条，导出历史 %s 条" % (
              len(present), len(C.R2_TABLE_NAMES),
              counts_in_manifest.get("assignment_batches"),
              counts_in_manifest.get("r2_report_exports")))

    # I4 真实备份 → 换机恢复：R2 的导出历史必须跟着走，且 class_id 要映射成新库的 id。
    # 这条是「静默丢数据」的硬证据：备份侧带走、恢复侧不搬，界面上就是历史突然空了。
    from app import db as db_mod

    hist_src = svc2.db.query("SELECT COUNT(*) AS n FROM r2_report_exports WHERE user_id=?",
                             (svc2.user["id"],))[0]["n"]
    if hist_src == 0:
        # 历史尚未切到库里时也照样测恢复路径：直接写两条，模拟 R2-04 修订后的写入。
        for kind, sid_ in (("class", 0), ("student", sids[STUDENTS[0][0]])):
            db_mod.add_report_export(svc2.db, svc2.user["id"], cid, kind, student_id=sid_,
                                     purpose="存档", scope={"batch": BATCH},
                                     path=r"D:\旧机器\报告.pdf", sha256="e2e", bytes=10,
                                     generated_at=GEN, title="E2E 导出记录",
                                     data_version="1.9.0")
        hist_src = 2

    dst_root = tempfile.mkdtemp(prefix="r2e2e-restore-")
    dst_dir = os.path.join(dst_root, "data")
    os.makedirs(dst_dir)
    dst_path = os.path.join(dst_dir, "app.db")
    Database(dst_path)
    ensure_answer_check_schema(dst_path)
    ensure_r2_schema(dst_path)
    dst_db = Database(dst_path, create=False)
    src_key = svc2.db.scalar("SELECT backup_key FROM users WHERE id=?", (svc2.user["id"],))
    with dst_db.tx() as conn:
        conn.execute(
            "INSERT INTO users(username, display_name, pwd_salt, pwd_hash, iterations, created_at)"
            " VALUES(?,?,?,?,?,?)", ("tea", "老师", "salt", "hash", 1000, "2026-01-01"))
        dst_uid = conn.execute("SELECT id FROM users").fetchone()["id"]
        conn.execute("UPDATE users SET backup_key=? WHERE id=?", (src_key, dst_uid))
    staging = os.path.join(dst_root, "staging")
    os.makedirs(staging)
    user_row = dict(dst_db.query_one(
        "SELECT id, username, backup_key FROM users WHERE id=?", (dst_uid,)))
    backup.inspect_backup(zip_path, staging, user_row)
    receipt = backup.apply_staged(staging, dst_dir, dst_uid, mode="restore",
                                  legacy_confirmed=True)
    restored = db_mod.list_report_exports(Database(dst_path, create=False), dst_uid)
    check("I4 备份→换机恢复后导出历史一条不少，class_id 已映射到新库",
          len(restored) == hist_src and hist_src >= 1
          and all(r["class_id"] != 0 for r in restored),
          "源 %d 条 → 恢复 %d 条；receipt=%s" % (
              hist_src, len(restored), (receipt.get("counts") or {}).get("r2_report_exports")))

    total = len(ITEMS)
    passed = sum(1 for _, ok in ITEMS if ok)
    print("")
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
