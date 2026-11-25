# -*- coding: utf-8 -*-
"""R1-T22：旧数据只读桥（第一轮 answer_reviews 候选错因 + 原版 followups 追问记录）。

跑法：C:\\Python314\\python.exe tests\\verify_r1_22_legacy_bridge.py

验四件事：
1. 有 `answer_reviews` 时，作答核对详情返回这些候选（`source="answer_review"`，带当时状态），
   `candidate_sources` 能区分 preset / answer_review；没有时 `candidates_note` 明确写「暂无候选」。
2. 第一轮 `followups` / `followup_updates` 在追问卡里可读（`legacy_items`/`legacy_note`），
   有/无两种情形都断言；且**只读**：前后 `followups` / `followup_updates` / `answer_reviews`
   的行数、updated_at 完全不变。
3. 缺证据时不得 404：追问卡给明确说明（`legacy_note` 或 suggestion.note），不抛异常。
4. 旧页面零回归由 review_160 / followup_180 / 150 套件另行证明（本套件只验桥本身）。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import answer_check, api, followup as followup_mod  # noqa: E402
from app.db import Database, ensure_answer_check_schema  # noqa: E402
from desktop.service import DesktopService  # noqa: E402


def answer_csv():
    return (
        "记录编号,学生姓名,学校编号,题号,题目内容,单元,标准解法,学生步骤,来源批次,作答日期,缺测,缺测原因\r\n"
        "R1-22-001,学生甲,XY001,1,解方程 2x+4=10,一元一次方程,2x=6；x=3,2x+4=10；2x=3；x=1.5,"
        "9月第1次作业,2026-09-02,否,\r\n"
    )


WORK = os.path.join(tempfile.gettempdir(), "r1-22-legacy-bridge")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-22-legacy-bridge.txt")
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


def snapshot(db):
    """旧表的只读快照：行数 + 最大 id + updated_at 摘要。"""
    snap = {}
    for table, stamp in (("answer_reviews", "updated_at"), ("followups", "updated_at"),
                         ("followup_updates", "created_at")):
        exists = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not exists:
            snap[table] = None
            continue
        rows = db.query("SELECT * FROM %s ORDER BY id" % table)
        snap[table] = {
            "count": len(rows),
            "ids": [row["id"] for row in rows],
            "stamps": [row.get(stamp) or "" for row in rows],
        }
    return snap


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-T22 旧数据只读桥（answer_reviews 候选 + 原版 followups 追问记录）")
    for line in environment_lines():
        log(line)

    data_dir = os.path.join(WORK, "data")
    os.makedirs(data_dir)
    db_file = os.path.join(data_dir, "app.db")
    Database(db_file)
    ensure_answer_check_schema(db_file)
    svc = DesktopService(data_dir)
    svc.register("oc-22", "pw-r1-22")
    cid = svc.create_class("只读桥班")["id"]
    sid = svc.create_student(cid, "学生甲", "XY001")["id"]
    svc.import_answers(cid, answer_csv(), "9月第1次作业")
    db = svc.db
    answer = db.query_one("SELECT * FROM answers WHERE class_id=? ORDER BY id LIMIT 1", (cid,))

    # ---------------------------------------------------------------
    section("零、桥之前：真实作答既没有候选，也没有原版追问（明确文案，不是空数组糊弄）")
    # ---------------------------------------------------------------
    detail = answer_check.detail(db, {"id": cid}, answer["id"])
    check("0.1 没有 answer_reviews 时候选为空", detail["candidates"] == [], detail["candidates"])
    check("0.2 candidates_note 明确写「暂无候选」",
          "暂无候选" in (detail.get("candidates_note") or ""), detail.get("candidates_note"))
    check("0.3 candidate_sources 为空（没有来源可标）",
          detail.get("candidate_sources") == [], detail.get("candidate_sources"))
    check("0.4 追问卡里 legacy_items 为空且 legacy_note 有说明",
          detail["followup"].get("legacy_items") == []
          and "暂无原版追问记录" in (detail["followup"].get("legacy_note") or ""),
          detail["followup"].get("legacy_note"))

    before = snapshot(db)

    # ---------------------------------------------------------------
    section("一、第一轮 answer_reviews（pending）的候选错因：只读读得到，带来源与状态")
    # ---------------------------------------------------------------
    reviews_before = followup_mod  # 占位，避免 lints 误报未使用
    ts = db.query_one("SELECT now_data FROM (SELECT '' AS now_data)")  # 只为拿到 dict 形态
    stamp = "2026-09-03T10:00:00+08:00"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO answer_reviews(class_id, student_id, answer_id, status, provider, model,"
            " causes, note, uncertain, raw, answer_fingerprint, mistake_id, created_at, updated_at,"
            " decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, sid, answer["id"], "pending", "first-round", "legacy-model",
             json.dumps([{"code": "E-SIGN", "label": "符号错误",
                          "evidence": ["2x+4=10"]},
                         {"code": "E-MOVE", "label": "移项没变号", "evidence": ["2x=3"]}],
                        ensure_ascii=False),
             "第一轮的模拟分析结论", 1, "{}", answer["fingerprint"], None,
             stamp, stamp, ""),
        )
    detail = answer_check.detail(db, {"id": cid}, answer["id"])
    labels = [row.get("label") for row in detail["candidates"]]
    check("1.1 详情里读到了第一轮的两条候选", labels == ["符号错误", "移项没变号"], labels)
    check("1.2 每条候选都标了 source=answer_review",
          all(row.get("source") == "answer_review" for row in detail["candidates"]),
          [row.get("source") for row in detail["candidates"]])
    check("1.3 带上当时的确认状态（pending）",
          all(row.get("review_status") == "pending" for row in detail["candidates"]),
          [row.get("review_status") for row in detail["candidates"]])
    check("1.4 candidate_sources 标明来源是 answer_review",
          detail.get("candidate_sources") == ["answer_review"], detail.get("candidate_sources"))
    check("1.5 有候选时 candidates_note 为空（不需要「暂无」提示）",
          (detail.get("candidates_note") or "") == "", detail.get("candidates_note"))
    check("1.6 证据只保留确实是本条作答原文的切片",
          detail["candidates"][0].get("evidence") == ["2x+4=10"],
          detail["candidates"][0].get("evidence"))

    # ---------------------------------------------------------------
    section("二、第一轮原版 followups / followup_updates：追问卡只读可读")
    # ---------------------------------------------------------------
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO followups(class_id, student_id, problem, mistake_id, review_id, question,"
            " source, suggest, evidence, status, asked_at, answer, answered_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, sid, "一元一次方程", None, detail["candidates"] and None, "你这一步为什么把 4 变成 3？",
             "teacher", "{}", "{}", "answered", "2026-09-04T09:00:00+08:00",
             "我看错了，把 4 看成 3", "2026-09-04T09:05:00+08:00",
             "2026-09-04T09:00:00+08:00", "2026-09-04T09:05:00+08:00"),
        )
    fid = db.scalar("SELECT id FROM followups WHERE class_id=? ORDER BY id DESC LIMIT 1", (cid,))
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO followup_updates(followup_id, causes, note, uncertain, raw, before_json,"
            " after_json, status, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fid, json.dumps([{"code": "E-SIGN", "label": "符号错误"}], ensure_ascii=False),
             "第一轮按学生回答生成的候选", 0, "{}", "[]", "[]", "pending",
             "2026-09-04T09:06:00+08:00", ""),
        )
    detail = answer_check.detail(db, {"id": cid}, answer["id"])
    legacy = detail["followup"].get("legacy_items") or []
    check("2.1 追问卡读到了原版追问记录（1 条）", len(legacy) == 1, legacy)
    if legacy:
        item = legacy[0]
        check("2.2 带问句与学生原话", item["question"].startswith("你这一步为什么")
              and item["answer_text"] == "我看错了，把 4 看成 3", item.get("question"))
        check("2.3 带时间（问了/答了）",
              item["asked_at"].startswith("2026-09-04") and item["answered_at"].startswith("2026-09-04"),
              (item["asked_at"], item["answered_at"]))
        check("2.4 标明来源是「原版追问记录」", item["source"] == "原版追问记录",
              item.get("source"))
        check("2.5 说明凭什么认为它属于这份作答（problem 对上）", item["link"] == "problem",
              item.get("link"))
        check("2.6 原版候选更新也读得到（followup_updates）",
              item["updates"] and item["updates"][0]["causes"][0]["label"] == "符号错误",
              item.get("updates"))
    check("2.7 有原版记录时 legacy_note 为空", (detail["followup"].get("legacy_note") or "") == "",
          detail["followup"].get("legacy_note"))

    # ---------------------------------------------------------------
    section("三、只读：桥本身一行都不写旧表")
    # ---------------------------------------------------------------
    middle = snapshot(db)
    for _ in range(2):
        answer_check.detail(db, {"id": cid}, answer["id"])
        answer_check.candidates_for(answer, db)
    after = snapshot(db)
    check("3.1 反复调用详情/候选之后，answer_reviews 行数与时间戳不变",
          after["answer_reviews"] == middle["answer_reviews"], after["answer_reviews"])
    check("3.2 followups 行数与时间戳不变",
          after["followups"] == middle["followups"], after["followups"])
    check("3.3 followup_updates 行数与时间戳不变",
          after["followup_updates"] == middle["followup_updates"], after["followup_updates"])
    check("3.4 桥过程中 answer_reviews 从 0 → 1 只发生在测试自己插入的那一次",
          before["answer_reviews"]["count"] == 0 and after["answer_reviews"]["count"] == 1,
          (before["answer_reviews"]["count"], after["answer_reviews"]["count"]))

    # ---------------------------------------------------------------
    section("四、缺证据不 404：追问卡给明确说明")
    # ---------------------------------------------------------------
    fresh = db.query_one("SELECT * FROM answers WHERE class_id=? AND id<>?", (cid, answer["id"]))
    if fresh is None:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, sid, "R1-22-002", "9月第1次作业", "学生甲", "XY001", "2",
                 "解方程 3(x-2)=2x+5", "一元一次方程", "3x-6=2x+5；x=11", "3x-6=2x+5；x=11",
                 "", "2026-09-02", 0, "", "fp-second", "2026-09-02T00:00:00+08:00",
                 "2026-09-02T00:00:00+08:00", "2026-09-02T00:00:00+08:00"),
            )
        fresh = db.query_one("SELECT * FROM answers WHERE record_key='R1-22-002'")
    ok = True
    error = ""
    try:
        other_detail = answer_check.detail(db, {"id": cid}, fresh["id"])
    except Exception as exc:  # noqa: BLE001 —— 只要抛异常就算失败（要求「不要 404」）
        ok = False
        error = "%s: %s" % (type(exc).__name__, str(exc)[:60])
        other_detail = {}
    check("4.1 另一份没有证据的作答，详情不抛异常（不 404）", ok, error)
    if ok:
        card = other_detail["followup"]
        check("4.2 追问建议缺失时给出明确 note（不是空白）",
              bool((card.get("note") or "").strip()) or card.get("available") is True,
              card.get("note"))
        # 同一班级/学生下已经有第一轮追问记录，因此这份作答可能按 problem 关联到它（link=problem）；
        # 只要桥给的是「可读的记录」或「明确的暂无文案」，都算符合契约（不许空白、不许抛异常）。
        note_text = card.get("legacy_note") or ""
        has_items = bool(card.get("legacy_items"))
        check("4.3 原版追问记录给出可读结果或明确「暂无原版追问记录」文案",
              has_items or "暂无原版追问记录" in note_text,
              (card.get("legacy_items"), note_text))

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                    RESULTS["pass"], RESULTS["fail"]))
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LINES) + "\n")
    shutil.rmtree(WORK, ignore_errors=True)
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
