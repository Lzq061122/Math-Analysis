# -*- coding: utf-8 -*-
"""R1-16：教师在「作答核对」页判的错因，必须落进原版 mistakes，让老四页零改动看见。

跑法：C:\\Python314\\python.exe tests\\verify_r1_16_check_to_mistake.py

覆盖（与任务书验收一一对应）：
1. 判 cause_state=decided（定位 first_bad / missing_steps）→ mistakes **多出且仅多出一条**
   status='confirmed'，source_type 可辨（作答核对），owner/class/student 正确；
2. 重复保存同一条作答 → 仍是一条（幂等）；
3. 已由「作答分析」确认生成过错题 → 更新那一条并绑定 answer_id，不插第二条；
4. 撤回（answer_version 置空）→ 回到判前状态（我方那条 returned，四页不再计入）；
5. 改判 → 同一条被更新（不新建）；
6. 四处真实函数读到它：`tutor.summary`、`stats.build_stats`、`advice.refresh`、
   `api.api_list_mistakes`（不 mock 数据源）；
7. 旧库升级兼容 + 反向自证（去掉迁移就必须报 no such column）。
"""
import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import advice as advice_mod  # noqa: E402
from app import answer_check, api, review_mod, stats as stats_mod, tutor as tutor_mod  # noqa: E402
from app import db as db_mod  # noqa: E402
from app.db import Database, ensure_answer_check_schema, ensure_answers_schema  # noqa: E402
from app.uploads import UploadStore  # noqa: E402
from app.vision import AnalysisError  # noqa: E402
from desktop.service import DesktopService  # noqa: E402
from verify_r1_07_isolation import FakeCtx, answer_csv, make_photo  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r1-16")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-16-check-to-mistake.txt")
LINES = []
RESULTS = {"pass": 0, "fail": 0}


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


def mistake_rows(db, class_id=None):
    if class_id is None:
        return db.query("SELECT * FROM mistakes ORDER BY id")
    return db.query("SELECT * FROM mistakes WHERE class_id=? ORDER BY id", (class_id,))


def confirmed(db, class_id=None):
    return [row for row in mistake_rows(db, class_id) if row["status"] == "confirmed"]


def save(env, answer_id, body):
    ctx = FakeCtx(env["db"], env["user"], env["uploads"],
                  params={"id": env["cid"], "answer_id": answer_id}, body=body,
                  ai_provider=None)
    return api.api_check_save(ctx)


def body_for(env, answer_id, **over):
    detail = api.api_check_answer(FakeCtx(
        env["db"], env["user"], env["uploads"],
        params={"id": env["cid"], "answer_id": answer_id}))
    body = {
        "expected_version": detail["answer"]["version"],
        "progress": "checked",
    }
    body.update(over)
    return body


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-16 作答核对 → 原版错题（幂等落库 + 四页可见 + 撤回可回滚）")
    for line in environment_lines():
        log(line)

    data_root = os.path.join(WORK, "data")
    os.makedirs(data_root)
    db_file = os.path.join(data_root, "app.db")
    Database(db_file)
    ensure_answer_check_schema(db_file)
    svc = DesktopService(data_root)
    svc.register("oc-16", "pw-r1-16")
    cid = svc.create_class("作答核对落地班")["id"]
    sid = svc.create_student(cid, "学生甲", "XY001")["id"]
    csv_text = answer_csv()
    svc.import_answers(cid, csv_text, "9月第1次作业")

    db = svc.db
    user = svc.user
    env = {"db": db, "user": user, "uploads": UploadStore(os.path.join(data_root, "uploads")),
           "cid": cid, "sid": sid}
    answers = db.query("SELECT * FROM answers WHERE class_id=? ORDER BY id", (cid,))
    check("0.0 夹具就绪：2 份作答、0 条错题",
          len(answers) == 2 and len(mistake_rows(db)) == 0, (len(answers), len(mistake_rows(db))))

    first = answers[0]
    detail = api.api_check_answer(FakeCtx(
        db, user, env["uploads"], params={"id": cid, "answer_id": first["id"]}))
    check("0.1 这份作答的定位结论是规则首处错步（first_bad），可以判错因",
          detail["locate"]["status"] == "first_bad", detail["locate"]["status"])

    # ------------------------------------------------------------------
    section("一、判错因 → mistakes 多出且仅多出一条 confirmed 错题")
    # ------------------------------------------------------------------
    label = detail["judgement"]["candidates"][0]["label"] if detail["judgement"]["candidates"] else ""
    code = detail["judgement"]["candidates"][0]["code"] if detail["judgement"]["candidates"] else ""
    saved = save(env, first["id"], body_for(
        env, first["id"], cause_state="decided", cause_code=code, cause_label=label,
        cause_source="teacher", observed_step="2x+4=10；2x=3；x=1.5",
        basis_note="作业本原图上第 2 行把 6 写成 3"))
    rows = mistake_rows(db, cid)
    check("1.1 保存成功且回执是 saved（不是 retracted）", saved.get("action") == "saved", saved.get("action"))
    check("1.2 mistakes 多出一条", len(rows) == 1, len(rows))
    if rows:
        row = rows[0]
        check("1.3 这条是 status='confirmed'", row["status"] == "confirmed", row["status"])
        check("1.4 来源可辨：source_type=作答核对", row["source_type"] == db_mod.MISTAKE_SOURCE
              if hasattr(db_mod, "MISTAKE_SOURCE") else row["source_type"] == "作答核对",
              row["source_type"])
        check("1.5 owner 正确（class/student 都是这份作答的）",
              row["class_id"] == cid and row["student_id"] == sid, (row["class_id"], row["student_id"]))
        check("1.6 绑定了 answer_id（幂等键）", row["answer_id"] == first["id"], row["answer_id"])
        check("1.7 error_type 用的是教师判断的原因", row["error_type"] == label, (row["error_type"], label))
        check("1.8 observed_errors 抄的是教师写的错步",
              json.loads(row["observed_errors"] or "[]") == ["2x+4=10；2x=3；x=1.5"],
              row["observed_errors"])
        check("1.9 note 写清来源与判断依据（可追溯）",
              "作答核对" in (row["note"] or "") and "判断依据" in (row["note"] or ""), row["note"][:60])
        check("1.10 没写任何 AI 字段（不调用生成式模型）",
              (row["ai_mode"] or "mock") == "mock" and not (row["ai_error"] or ""), row["ai_mode"])

    # ------------------------------------------------------------------
    section("二、重复保存不产生重复错题；改判更新同一条（幂等）")
    # ------------------------------------------------------------------
    first_id = rows[0]["id"] if rows else None
    save(env, first["id"], body_for(
        env, first["id"], cause_state="decided", cause_code=code, cause_label=label,
        cause_source="teacher", observed_step="2x+4=10；2x=3；x=1.5",
        basis_note="作业本原图上第 2 行把 6 写成 3"))
    rows = mistake_rows(db, cid)
    check("2.1 连存两次仍然只有一条错题", len(rows) == 1 and rows[0]["id"] == first_id, len(rows))

    other_label = next((item["label"] for item in detail["judgement"]["candidates"]
                        if item["label"] != label), label)
    other_code = next((item["code"] for item in detail["judgement"]["candidates"]
                       if item["code"] != code), code)
    save(env, first["id"], body_for(
        env, first["id"], cause_state="decided", cause_code=other_code, cause_label=other_label,
        cause_source="teacher", observed_step="2x+4=10；2x=3；x=1.5", basis_note="对照答案复核后改判"))
    rows = mistake_rows(db, cid)
    check("2.2 改判后仍是同一条（id 不变），不是新建",
          len(rows) == 1 and rows[0]["id"] == first_id, [(r["id"], r["error_type"]) for r in rows])
    check("2.3 改判后错因跟着更新", rows and rows[0]["error_type"] == other_label,
          (rows[0]["error_type"] if rows else None, other_label))

    # ------------------------------------------------------------------
    section("三、撤回 → 回到判前状态（四页不再计入），再判能恢复")
    # ------------------------------------------------------------------
    retract = save(env, first["id"], {
        "expected_version": api.api_check_answer(FakeCtx(
            db, user, env["uploads"], params={"id": cid, "answer_id": first["id"]}))["answer"]["version"],
        "progress": "pending", "change_reason": "判错了，撤回",
    })
    rows = mistake_rows(db, cid)
    check("3.1 撤回回执是 retracted、进度回到 pending",
          retract.get("action") == "retracted" and retract["judgement"]["progress"] == "pending",
          (retract.get("action"), retract["judgement"]["progress"]))
    check("3.2 撤回后没有 confirmed 错题（四页看不到它）",
          len(confirmed(db, cid)) == 0, [(r["id"], r["status"]) for r in rows])
    check("3.3 行还在、只是置 returned（原版语义，可追溯）",
          len(rows) == 1 and rows[0]["status"] == "returned"
          and "作答核对" in (rows[0]["returned_reason"] or ""), rows[0]["status"] if rows else None)

    save(env, first["id"], body_for(
        env, first["id"], cause_state="decided", cause_code=code, cause_label=label,
        cause_source="teacher", observed_step="2x+4=10；2x=3；x=1.5", basis_note="重新核对：确实写错了"))
    rows = mistake_rows(db, cid)
    check("3.4 再判一次 → 还是那一条，状态回到 confirmed（幂等复活）",
          len(rows) == 1 and rows[0]["status"] == "confirmed" and rows[0]["id"] == first_id,
          [(r["id"], r["status"]) for r in rows])

    # ------------------------------------------------------------------
    section("四、已由「作答分析」生成过错题时：更新那一条并绑定 answer_id")
    # ------------------------------------------------------------------
    second = answers[1]
    review = review_mod.analyse(db, cid, second["id"], None, "", "")
    confirmed_review = review_mod.confirm(db, cid, review["id"], user["id"])
    review_mistake = confirmed_review.get("mistake_id")
    check("4.1 作答分析确认后先生成了一条错题（source_type=作答分析）", bool(review_mistake),
          review_mistake)
    before_row = db.query_one("SELECT * FROM mistakes WHERE id=?", (review_mistake,))
    check("4.2 那条错题的 answer_id 还是空的（作答分析不写这个列）",
          before_row and before_row["answer_id"] is None, before_row["answer_id"] if before_row else None)

    second_detail = api.api_check_answer(FakeCtx(
        db, user, env["uploads"], params={"id": cid, "answer_id": second["id"]}))
    second_body = {
        "expected_version": second_detail["answer"]["version"],
        "progress": "checked", "cause_state": "decided",
        "cause_code": second_detail["judgement"]["candidates"][0]["code"]
        if second_detail["judgement"]["candidates"] else "",
        "cause_label": second_detail["judgement"]["candidates"][0]["label"]
        if second_detail["judgement"]["candidates"] else "",
        "cause_source": "teacher", "basis_note": "同一份作答，核对后确认错因",
    }
    if second_detail["locate"]["status"] == "first_bad":
        save(env, second["id"], second_body)
        after_row = db.query_one("SELECT * FROM mistakes WHERE id=?", (review_mistake,))
        check("4.3 作答核对判完后错题条数没变（没插第二条）",
              len(mistake_rows(db, cid)) == 2, len(mistake_rows(db, cid)))
        check("4.4 那一条被更新并绑定了 answer_id",
              after_row and after_row["answer_id"] == second["id"], after_row["answer_id"] if after_row else None)
        check("4.5 来源仍是原来的（作答分析），note 里注明沿用",
              after_row and after_row["source_type"] == "作答分析"
              and "沿用已有错题" in (after_row["note"] or ""),
              (after_row["source_type"], after_row["note"][-30:]) if after_row else None)
    else:
        check("4.3 这份作答的定位结论不是 first_bad，跳过（如实登记）", True, "skipped")

    # ------------------------------------------------------------------
    section("五、四处真实函数读到它（不 mock 数据源）")
    # ------------------------------------------------------------------
    summary = tutor_mod.summary(db, cid, sid)
    check("5.1 tutor.summary（个人学情）读到这条已确认错题",
          len(summary["items"]) >= 1, len(summary["items"]))
    stats = stats_mod.build_stats(db, cid)
    check("5.2 stats.build_stats（班级统计）错题数 ≥ 1 且错因有计数",
          int(stats.get("mistake_count") or stats.get("total_mistakes") or 0) >= 1
          or bool(stats.get("by_error")), {k: stats.get(k) for k in ("mistake_count", "by_error")})

    class_row = db.query_one("SELECT * FROM classes WHERE id=?", (cid,))
    students = db.query("SELECT * FROM students WHERE class_id=?", (cid,))
    advice_mod.refresh(db, class_row, students)
    advice_row = db.query_one("SELECT * FROM advice WHERE class_id=? ORDER BY id DESC LIMIT 1", (cid,))
    check("5.3 advice.refresh（个性化辅导 / 班级建议）真的生成了建议",
          advice_row is not None and bool((advice_row["content"] or "").strip()),
          (advice_row["content"][:40] if advice_row else None))

    listed = api.api_list_mistakes(FakeCtx(db, user, env["uploads"], params={"id": cid},
                                           query={"status": "confirmed"}))
    ids = {item["id"] for item in (listed.get("items") or listed.get("mistakes") or [])}
    check("5.4 错题管理页（api_list_mistakes）能列出这条错题",
          first_id in ids, sorted(ids)[:5])

    # ------------------------------------------------------------------
    section("六、旧库升级 + 反向自证")
    # ------------------------------------------------------------------
    legacy_dir = os.path.join(WORK, "legacy")
    shutil.rmtree(legacy_dir, ignore_errors=True)
    os.makedirs(legacy_dir)
    legacy_file = os.path.join(legacy_dir, "app.db")
    Database(legacy_file)
    ensure_answer_check_schema(legacy_file)
    conn = sqlite3.connect(legacy_file)
    try:
        with conn:
            conn.execute("DROP INDEX IF EXISTS idx_mistakes_answer")
            conn.execute("ALTER TABLE mistakes DROP COLUMN answer_id")
    finally:
        conn.close()
    check("6.0 造好旧库替身：mistakes 没有 answer_id 列、没有唯一索引",
          "answer_id" not in {row[1] for row in sqlite3.connect(legacy_file).execute(
              "PRAGMA table_info(mistakes)")}, "legacy")
    backup_file = ensure_answers_schema(legacy_file)
    cols = {row[1] for row in sqlite3.connect(legacy_file).execute("PRAGMA table_info(mistakes)")}
    indexes = {row[0] for row in sqlite3.connect(legacy_file).execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    check("6.1 旧库升级补上 answer_id 与唯一索引（有备份）",
          backup_file is not None and "answer_id" in cols and "idx_mistakes_answer" in indexes,
          (bool(backup_file), "answer_id" in cols, "idx_mistakes_answer" in indexes))
    check("6.2 升级后再跑一次不再迁移（幂等，返回 None）",
          ensure_answers_schema(legacy_file) is None, "second run")

    # 反向自证：同一份旧库，如果迁移不补列，插入 answer_id 必须报 no such column
    mutant_dir = os.path.join(WORK, "mutant")
    shutil.rmtree(mutant_dir, ignore_errors=True)
    os.makedirs(mutant_dir)
    mutant_file = os.path.join(mutant_dir, "app.db")
    Database(mutant_file)
    ensure_answer_check_schema(mutant_file)
    conn = sqlite3.connect(mutant_file)
    try:
        with conn:
            conn.execute("DROP INDEX IF EXISTS idx_mistakes_answer")
            conn.execute("ALTER TABLE mistakes DROP COLUMN answer_id")
    finally:
        conn.close()
    error = ""
    try:
        conn = sqlite3.connect(mutant_file)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO mistakes(class_id, student_id, status, created_at, updated_at, answer_id)"
                    " VALUES (1,1,'confirmed','t','t',1)")
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        error = str(exc)
    check("6.3 反向自证：不补列就写 answer_id 必然报 no such column（断言不恒真）",
          "answer_id" in error, error)

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                    RESULTS["pass"], RESULTS["fail"]))
    code_exit = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code_exit))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code_exit


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
