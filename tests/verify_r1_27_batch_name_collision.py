# -*- coding: utf-8 -*-
"""R1-08 修复验证：真实作答撞了演示批名时，总览/队列/统计三处口径必须一致。

跑法：C:\\Python314\\python.exe tests\\verify_r1_27_batch_name_collision.py

缺陷（t9 评审实测）：真实作答落在批名 = 某个演示批名时，
  - `resolve_batch` 返回该演示批名；
  - `overview.assignment = {total:0, pending:0, synthetic:true}`、`assignments` 整条不列；
  - 但 `stats.answer_check.expected = 1`。
⇒ 「总览看不见、统计数得到」。

修法（契约 §12.7，方案 A）：判「演示批次」不再只看批名，要求
**批名命中演示批名 且 该批次里没有任何真实行**；只要该批次有真实作答，就按真实批次列出。

本套件验：
1. 撞名情形：三处一致（总览列出 + 队列含它 + 统计数得到），且 synthetic=false；
2. 纯演示批次：仍按演示口径（synthetic=true、不进统计）；
3. 混合批次（演示行 + 真实行同名）：按真实批次列出、只数真实行，演示行仍不进统计；
4. 反例（反向自证）：把判据改回「只看批名」的变异体，同一夹具必须重新出现「看不见但数得到」。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import answer_check, api  # noqa: E402
from app import db as db_mod  # noqa: E402
from app import stats as stats_mod  # noqa: E402
from app.db import Database, ensure_answer_check_schema  # noqa: E402
from desktop.service import DesktopService  # noqa: E402
from verify_r1_07_isolation import FakeCtx  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "r1-27-batch-collision")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-27-batch-name-collision.txt")
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


def add_answer(db, class_id, student_id, batch, record_key, steps, source="", fp=None,
               imported_at="2026-09-02T00:00:00+08:00"):
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
            " student_code, question_no, question_text, unit, standard_solution, student_steps,"
            " source, occurred_on, missing, missing_reason, fingerprint, imported_at, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, record_key, batch, "学生甲", "XY001", "1",
             "解方程 2x+4=10", "一元一次方程", "2x=6；x=3", steps, source, "2026-09-02", 0, "",
             fp or ("fp-" + record_key), imported_at, imported_at, imported_at),
        )
    return db.query_one("SELECT * FROM answers WHERE record_key=?", (record_key,))


def three_views(db, class_row, batch):
    """三处口径：总览 assignment/assignments、队列、统计 expected。"""
    view = answer_check.overview(db, class_row, batch)
    queue = answer_check.queue_rows(db, class_row["id"], batch)
    stats = stats_mod.answer_check_stats(db, class_row, batch)
    listed = [item["batch"] for item in (view.get("assignments") or [])]
    return {
        "assignment": view.get("assignment") or {},
        "listed": listed,
        "queue_ids": [row["id"] for row in queue],
        "expected": int((stats or {}).get("expected") or 0),
        "state": view.get("state"),
    }


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-08 修复：真实作答撞演示批名（总览/队列/统计三处一致）")
    for line in environment_lines():
        log(line)

    data_dir = os.path.join(WORK, "data")
    os.makedirs(data_dir)
    db_file = os.path.join(data_dir, "app.db")
    Database(db_file)
    ensure_answer_check_schema(db_file)
    svc = DesktopService(data_dir)
    svc.register("oc-27", "pw-r1-27")
    class_row = svc.create_class("撞名验证班")
    cid = class_row["id"]
    sid = svc.create_student(cid, "学生甲", "XY001")["id"]
    db = svc.db
    demo_batch = db_mod.DEMO_BATCH
    log("演示批名（来自 db.DEMO_BATCH）：%s" % demo_batch)

    # ---------------------------------------------------------------
    section("一、撞名真实作答：三处必须一致（都当真实批次列出并计数）")
    # ---------------------------------------------------------------
    real = add_answer(db, cid, sid, demo_batch, "R1-27-REAL-1", "2x+4=10；2x=3；x=1.5")
    views = three_views(db, class_row, demo_batch)
    log("总览 assignment=%s；assignments 列出=%s；队列=%s；统计 expected=%s"
        % (json.dumps(views["assignment"], ensure_ascii=False), views["listed"],
           views["queue_ids"], views["expected"]))
    check("1.1 总览不再把它当成演示批次（synthetic=false）",
          views["assignment"].get("synthetic") is False, views["assignment"].get("synthetic"))
    check("1.2 总览的应交数 = 1（这份真实作答数得到）",
          int(views["assignment"].get("total") or 0) == 1, views["assignment"].get("total"))
    check("1.3 总览的 assignments 里列出了这个批次",
          demo_batch in views["listed"], views["listed"])
    check("1.4 队列里有这份作答（不再看不见）",
          views["queue_ids"] == [real["id"]], views["queue_ids"])
    check("1.5 统计 expected = 1（与总览同口径）",
          views["expected"] == 1, views["expected"])
    check("1.6 三处口径一致：总览应交 == 队列条数 == 统计 expected",
          int(views["assignment"].get("total") or 0) == len(views["queue_ids"]) == views["expected"],
          (views["assignment"].get("total"), len(views["queue_ids"]), views["expected"]))
    check("1.7 状态不再是 no_answers（有真实作答要能核对）",
          views["state"] in ("has_pending", "all_checked"), views["state"])

    # ---------------------------------------------------------------
    section("二、纯演示批次：仍按演示口径，不进真实统计")
    # ---------------------------------------------------------------
    # 用一个**只有演示行**的演示批次（第二演示批名）验证「演示口径没被改坏」。
    demo_mod = answer_check._demo_module()
    demo_batches = list(getattr(demo_mod, "DEMO_BATCHES", []) or [])
    pure_demo = next((name for name in demo_batches if name != db_mod.DEMO_BATCH),
                     "【合成演示】9月第3次作业")
    log("纯演示批次名（来自 demo_answers.DEMO_BATCHES）：%s" % pure_demo)
    demo_row = add_answer(db, cid, sid, pure_demo, "DEMO-R1-27-1",
                          "2x+4=10；2x=14；x=7", source=db_mod.DEMO_SOURCE,
                          imported_at="2026-09-01T00:00:00+08:00")
    views_demo = three_views(db, class_row, pure_demo)
    check("2.1 纯演示批次仍标 synthetic=true（演示口径没被改坏）",
          views_demo["assignment"].get("synthetic") is True, views_demo["assignment"])
    check("2.2 纯演示批次的应交 = 演示行数（演示数据自己能核对）",
          int(views_demo["assignment"].get("total") or 0) == 1,
          views_demo["assignment"].get("total"))
    stats_demo = stats_mod.answer_check_stats(db, class_row, pure_demo)
    check("2.3 统计永远不数演示数据（纯演示批次的 expected = 0）",
          int((stats_demo or {}).get("expected") or 0) == 0, stats_demo.get("expected"))

    # ---------------------------------------------------------------
    section("三、同名混合：真实行 + 演示行同批 → 按真实批次列出，只数真实行")
    # ---------------------------------------------------------------
    views_mixed = three_views(db, class_row, demo_batch)
    check("3.1 混合批次 synthetic=false（有真实行就按真实批次）",
          views_mixed["assignment"].get("synthetic") is False, views_mixed["assignment"])
    check("3.2 混合批次 total = 1（只数真实行，演示行不计入）",
          int(views_mixed["assignment"].get("total") or 0) == 1,
          views_mixed["assignment"].get("total"))
    check("3.3 队列里只有真实那份（演示行不混进真实队列）",
          views_mixed["queue_ids"] == [real["id"]], views_mixed["queue_ids"])
    check("3.4 统计 expected 与总览一致（=1）", views_mixed["expected"] == 1, views_mixed["expected"])

    # 保存一次真实判断，确认改动后保存链路仍把该批次当真实批次
    detail = api.api_check_answer(FakeCtx(
        db, svc.user, svc.uploads, params={"id": cid, "answer_id": real["id"]}))
    pool = detail.get("candidates") or []
    if pool:
        code, label = pool[0]["code"], pool[0]["label"]
    else:
        code, label = next(iter(answer_check.CAUSE_LABELS.items()))
    saved = api.api_check_save(FakeCtx(
        db, svc.user, svc.uploads, params={"id": cid, "answer_id": real["id"]},
        body={"expected_version": detail["answer"]["version"], "progress": "checked",
              "cause_state": "decided", "cause_code": code, "cause_label": label,
              "cause_source": "teacher", "basis_note": "撞名批次里的真实判断",
              "observed_step": "2x+4=10；2x=3；x=1.5"}))
    check("3.5 撞名批次里保存判断能成功（走的是真实批次口径）",
          saved.get("action") == "saved", saved.get("action"))
    views_after = three_views(db, class_row, demo_batch)
    check("3.6 保存后三处仍一致（总览应交 1 / 队列 0 待核对 / 统计 expected 1）",
          int(views_after["assignment"].get("total") or 0) == 1
          and views_after["queue_ids"] == [] and views_after["expected"] == 1,
          (views_after["assignment"].get("total"), views_after["queue_ids"], views_after["expected"]))

    # ---------------------------------------------------------------
    section("四、反向自证：判据改回「只看批名」就必须重新出现缺陷")
    # ---------------------------------------------------------------
    # 用 monkeypatch 把 _batch_is_synthetic 换回「只看批名」，同一夹具必须复现「看不见但数得到」。
    original = answer_check._batch_is_synthetic
    answer_check._batch_is_synthetic = lambda db_, class_id_, batch_: answer_check._is_demo_batch(batch_)
    try:
        fresh = add_answer(db, cid, sid, demo_batch, "R1-27-REAL-2", "2x+4=10；2x=3；x=1.5",
                           imported_at="2026-09-03T00:00:00+08:00")
        mutant = three_views(db, class_row, demo_batch)
        mutant_all = answer_check.overview(db, class_row, "")
        log("变异体：assignment=%s；统计 expected=%s；队列=%s"
            % (json.dumps(mutant["assignment"], ensure_ascii=False), mutant["expected"],
               mutant["queue_ids"]))
        real_rows = db.scalar(
            "SELECT COUNT(*) FROM answers WHERE class_id=? AND batch=? AND source<>?",
            (cid, demo_batch, db_mod.DEMO_SOURCE)) or 0
        # 评审记录的缺陷签名正是 {total:0, synthetic:true}：这批里有真实作答，变异体却一条也数不到。
        check("4.1 变异体复现缺陷签名 {total:0, synthetic:true}（该批真实作答数=%d）" % real_rows,
              mutant["assignment"].get("synthetic") is True
              and int(mutant["assignment"].get("total") or 0) == 0
              and int(real_rows) >= 1,
              (mutant["assignment"], real_rows))
        check("4.2 变异体下这份真实作答在队列里不可见",
              fresh["id"] not in [row["id"] for row in
                                  answer_check.queue_rows(db, cid, demo_batch)],
              "queue")
        check("4.3 而统计仍然数得到（=1）—— 这正是「看不见但数得到」",
              mutant["expected"] >= 1, mutant["expected"])
        check("4.4 断言不恒真：变异体下 1.6 那条『三处一致』必然不成立",
              not (int(mutant["assignment"].get("total") or 0) == mutant["expected"]),
              (mutant["assignment"].get("total"), mutant["expected"]))
    finally:
        answer_check._batch_is_synthetic = original
        with db.tx() as conn:
            conn.execute("DELETE FROM answers WHERE record_key='R1-27-REAL-2'")
    fixed_again = three_views(db, class_row, demo_batch)
    check("4.5 恢复真实实现后三处又一致（total=1、expected=1）",
          int(fixed_again["assignment"].get("total") or 0) == 1
          and fixed_again["expected"] == 1,
          (fixed_again["assignment"].get("total"), fixed_again["expected"]))

    # ---------------------------------------------------------------
    section("五、两个零引用兼容壳已删除")
    # ---------------------------------------------------------------
    check("5.1 _default_question 已不存在",
          not hasattr(answer_check, "_default_question"), dir(answer_check))
    check("5.2 _followup_source 已不存在",
          not hasattr(answer_check, "_followup_source"), dir(answer_check))

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                    RESULTS["pass"], RESULTS["fail"]))
    code_exit = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code_exit))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LINES) + "\n")
    shutil.rmtree(WORK, ignore_errors=True)
    return code_exit


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
