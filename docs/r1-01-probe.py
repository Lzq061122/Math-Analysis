# -*- coding: utf-8 -*-
r"""R1-01 合成演示证据隔离 —— 反例探针（T1 移交件，T7 搬进仓库并适配）

来历：本脚本原在仓库外（C:\Users\Public\r1_01_probe.py），由 T1（R1-01 合成证据隔离）
用于验收，T7 按要求搬进 docs/ 并让它对**当前代码**仍然可说真话。

跑法（在源码根目录执行；本文件的上级目录就是源码根）：
    C:\Python314\python.exe docs\r1-01-probe.py
    （也可以用 R1_ROOT 指定别的源码根，便于在隔离副本里跑）
每条断言都打印 PASS/FAIL，并给出实际值；有一条 FAIL 就退出码 1。

【T7 改写登记：6 条】这些断言的「原依据」在 R1 之后不成立了，按四段注释改写成
仍然可假的新断言；条数 40 条一条未增未删。
    3.1 / 3.2  演示数据由「一批 4 份」扩成「两批 6 份」（T12）
    5.4b       同上：演示批次的队列口径改为按批次分别核对
    5.5        synthetic_answers 由 4 变 6（T12）
    5.7        库级触发器已被 T6 删除：不再「归位」，改为断言「不归位但统计仍干净」
    6.1        reset 删除数由 4 变 6（T12）
未改写的 34 条在本次全量回归里仍全部通过（docs/r1-01-probe.txt）。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.environ.get("R1_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from app import answer_check as C          # noqa: E402
from app import demo_answers as D          # noqa: E402
from app import stats as S                 # noqa: E402
from app.answers import fingerprint        # noqa: E402
from app.db import Database, now_iso       # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  → " + str(detail)))


def new_env(tag):
    work = os.path.join(tempfile.gettempdir(), "r1-01-probe", tag)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    db = Database(os.path.join(work, "app.db"))
    stamp = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, display_name, pwd_salt, pwd_hash, iterations, created_at)"
            " VALUES (?,?,?,?,?,?)", ("t", "老师", "s", "h", 1, stamp))
        uid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO classes(user_id, name, grade, note, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)", (uid, "七年级 1 班", "七年级", "", stamp, stamp))
        cls = cur.lastrowid
    return db, cls


def add_student(db, class_id, name):
    stamp = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO students(class_id, name, code, note, active, created_at, updated_at)"
            " VALUES (?,?,?,?,1,?,?)", (class_id, name, "", "", stamp, stamp))
        return cur.lastrowid


def add_answer(db, class_id, student_id, record_key, *, question="3(x-2)=12",
               standard="3(x-2)=12；3x-6=12；3x=18；x=6", steps="3(x-2)=12\n3x-2=12\nx=14/3",
               batch="9月第2次作业", source="真实导入"):
    row = {"question_no": "1", "question_text": question, "unit": "一元一次方程",
           "standard_solution": standard, "student_steps": steps,
           "occurred_on": "2026-11-24", "missing": 0, "missing_reason": ""}
    stamp = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
            " student_code, question_no, question_text, unit, standard_solution, student_steps,"
            " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, record_key, batch, "", "", row["question_no"],
             row["question_text"], row["unit"], row["standard_solution"], row["student_steps"],
             source, row["occurred_on"], 0, "", fingerprint(row, student_id), stamp, stamp, stamp))
        return cur.lastrowid


def class_row(db, class_id):
    return db.query_one("SELECT * FROM classes WHERE id=?", (class_id,))


def main():
    print("被测：%s" % ROOT)
    print("DEMO_BATCH = %r / preset_version = %s" % (D.DEMO_BATCH, D.preset_version()))

    # ---------------- 1. 真实作答（完全答对）不得拿到任何演示证据 ----------------
    db, cls = new_env("real-correct")
    jia = add_student(db, cls, "学生甲")
    rid = add_answer(db, cls, jia, "REAL-A-01", steps="3(x-2)=12\n3x-6=12\nx=6")
    data = C.detail(db, class_row(db, cls), rid)
    blob = json.dumps(data, ensure_ascii=False)
    check("1.1 真实·完全正确：candidates 为空", data["candidates"] == [], data["candidates"])
    check("1.2 真实·完全正确：causes_source=none", data["causes_source"] == "none",
          data["causes_source"])
    check("1.3 真实·完全正确：causes_scope=real_answer",
          data["causes_scope"] == C.SCOPE_REAL, data["causes_scope"])
    check("1.4 说明里不再出现「候选来自预置演示条目」",
          "候选来自预置演示条目" not in (data["causes_note"] or ""), data["causes_note"])
    check("1.5 说明写明未接已审错因库（B2）与可人工判断",
          "B2" in data["causes_note"] and ("人工" in data["causes_note"]), data["causes_note"])
    demo_texts = [t for rec in D.build()["answers"] for cand in rec["candidates"]
                  for t in list(cand["support"]) + list(cand["rebut"]) + list(cand["pending"])
                  + list(cand["evidence"])]
    leaked = [t for t in demo_texts if t in blob]
    check("1.6 演示候选的任何一句话都没出现在真实作答详情里", not leaked, leaked[:2])
    check("1.7 正确作答没有错步 → 证据为空（不编造步骤）",
          data["causes_evidence"] == [] and data["locate"]["status"] == "correct",
          (data["causes_evidence"], data["locate"]["status"]))

    # ---------------- 2. 真实作答（错在别处）不得被判成演示的错因 ----------------
    db, cls = new_env("real-other")
    jia = add_student(db, cls, "学生甲")
    rid = add_answer(db, cls, jia, "REAL-A-02", steps="3(x-2)=12\n3x-6=21\nx=9")
    data = C.detail(db, class_row(db, cls), rid)
    check("2.1 真实·错在别处：candidates 为空（不给演示的「漏乘常数项」）",
          data["candidates"] == [], data["candidates"])
    check("2.2 真实·错在别处：规则定位仍然生效（first_bad 指向 3x-6=21）",
          data["locate"]["source"] == "rule" and data["locate"]["first_bad_text"] == "3x-6=21",
          data["locate"]["first_bad_text"])
    check("2.3 真实·错在别处：证据是本条原文切片，不含「只把 3 乘给了 x」",
          data["causes_evidence"]
          and data["causes_evidence"][0]["line"] == "3x-6=21"
          and "只把 3 乘给了 x" not in json.dumps(data, ensure_ascii=False),
          data["causes_evidence"])

    # ---------------- 3. 演示记录仍然有候选（预置只给演示） ----------------
    db, cls = new_env("demo")
    jia = add_student(db, cls, "学生甲")          # 真实名单里的学生
    seeded = D.seed(db, None, cls)
    # T7 改写①（3.1）
    # 原断言：seeded["seeded"]["answers"] == 4
    # 原依据：T1 时演示数据只有一批 4 份（DEMO_BATCH 单批次）。
    # 新断言：写入份数 == len(D.DEMO_BATCHES) 的演示记录总数，且 by_batch 覆盖全部演示批次。
    # 改写依据：T12 把演示数据扩成「主批次 4 份 + 【合成演示】9月第3次作业 2 份」共 6 份，
    #           写死 4 会把正当扩容判成回归；语义（seed 写全、按批次分账）不变。
    demo_total = len(D.build()["answers"])
    check("3.1 seed 写入 %d 份合成作答（len(build().answers)）" % demo_total,
          seeded["seeded"]["answers"] == demo_total
          and set(seeded["seeded"]["by_batch"]) == set(D.DEMO_BATCHES), seeded["seeded"])
    rows = {r["record_key"]: r for r in db.query("SELECT * FROM answers WHERE class_id=?", (cls,))}
    demo_a = rows["DEMO-A-01"]
    # T7 改写②（3.2）
    # 原断言：all(r["batch"] == D.DEMO_BATCH ...)
    # 原依据：T1 只有单一演示批次。
    # 新断言：all(r["batch"] in D.DEMO_BATCHES ...) —— 显式名单成员判定。
    # 改写依据：T12 两个演示批次；D.is_demo_batch() 也改成了名单成员判定（c18ba5d），
    #           用成员关系而不是单值相等，才和实现同一口径。
    check("3.2 演示作答全部落在演示批次名单内", all(r["batch"] in D.DEMO_BATCHES for r in rows.values()),
          [r["batch"] for r in rows.values()])
    check("3.3 演示作答一条都没挂到真实学生（学生甲 id=%d）" % jia,
          all(r["student_id"] != jia for r in rows.values()),
          [r["student_id"] for r in rows.values()])
    notes = {r["note"] for r in db.query("SELECT note FROM students WHERE class_id=?", (cls,))}
    check("3.4 演示作答挂的是带标记的合成学生", D.STUDENT_NOTE in notes, notes)
    data = C.detail(db, class_row(db, cls), demo_a["id"])
    check("3.5 演示案例 A：仍有 3 条候选", [c["code"] for c in data["candidates"]]
          == ["E-DIST-01", "E-DIST-02", "E-WRITE-01"],
          [c["code"] for c in data["candidates"]])
    check("3.6 演示案例 A：定位仍是预置 first_bad",
          data["locate"]["source"] == "preset" and data["locate"]["first_bad_text"] == "3x-2=12",
          (data["locate"]["source"], data["locate"]["first_bad_text"]))
    check("3.7 演示案例 A：causes_source=preset / scope=synthetic_demo",
          data["causes_source"] == "preset" and data["causes_scope"] == C.SCOPE_SYNTHETIC,
          (data["causes_source"], data["causes_scope"]))
    check("3.8 演示案例 A：候选证据是本条原文切片",
          all(c["evidence"] and all(t in demo_a["student_steps"] for t in c["evidence"])
              for c in data["candidates"]),
          [c["evidence"] for c in data["candidates"]])
    check("3.9 预置不变式：候选证据与规则定位都不漂移",
          D.verify_candidate_evidence() == [] and D.verify_preset_matches_rules() == [])

    # ---------------- 4. 演示内容/版本改动后旧预置失效 ----------------
    steps_changed = "3(x-2)=12\n3x-6=12\nx=6"      # 演示内容被改成正解：可解析，一定走规则定位
    with db.tx() as conn:
        conn.execute("UPDATE answers SET student_steps=? WHERE id=?",
                     (steps_changed, demo_a["id"]))
    d2 = C.detail(db, class_row(db, cls), demo_a["id"])
    check("4.1 演示作答内容被改 → 预置候选失效（candidates 为空）", d2["candidates"] == [],
          d2["candidates"])
    check("4.2 演示作答内容被改 → 说明写成「预置已失效，需要重置重演」",
          "预置候选已失效" in (d2["causes_note"] or ""), d2["causes_note"])
    check("4.3 演示作答内容被改 → 退回规则定位（source=rule、status=correct）",
          d2["locate"]["source"] == "rule" and d2["locate"]["status"] == "correct",
          (d2["locate"]["source"], d2["locate"]["status"]))
    with db.tx() as conn:   # 只改版本（指纹），内容不动
        conn.execute("UPDATE answers SET student_steps=?, fingerprint=? WHERE id=?",
                     (demo_a["student_steps"], "0" * 64, demo_a["id"]))
    d3 = C.detail(db, class_row(db, cls), demo_a["id"])
    check("4.4 演示作答版本被改（指纹不符）→ 预置候选同样失效", d3["candidates"] == [],
          d3["candidates"])

    # ---------------- 5. 演示数据不进真实作业/队列/统计 ----------------
    db, cls = new_env("scope")
    jia = add_student(db, cls, "学生甲")
    yi = add_student(db, cls, "学生乙")
    r1 = add_answer(db, cls, jia, "REAL-1")
    r2 = add_answer(db, cls, yi, "REAL-2", steps="5x-3=2x+6\n5x-2x=6-3\nx=1",
                    question="5x-3=2x+6", standard="5x-3=2x+6；5x-2x=6+3；3x=9；x=3")
    ov_before = C.overview(db, class_row(db, cls))
    # 先给一条真实作答落一个 first_bad 判断，让原题错误数有一个「真实」的基线
    d = C.detail(db, class_row(db, cls), r1)
    C.save(db, class_row(db, cls), 1, r1, {
        "expected_version": d["answer"]["version"],
        "observed_step": d["locate"]["first_bad_text"] or "",
        "first_bad_index": d["locate"]["first_bad_index"],
        "cause_state": "undecided",
    })
    st_before = S.answer_check_stats(db, class_row(db, cls))
    D.seed(db, None, cls)
    ov_after = C.overview(db, class_row(db, cls))
    st_after = S.answer_check_stats(db, class_row(db, cls))
    check("5.0 基线自检：真实作答的 original_errors 已经能数到（%s）"
          % st_before["original_errors"], st_before["original_errors"] == 1, st_before)
    check("5.1 载入演示后「当前作业」仍是真实批次",
          ov_after["assignment"]["batch"] == "9月第2次作业", ov_after["assignment"]["batch"])
    check("5.2 载入演示后应交/待核对数不变（%s → %s）"
          % (ov_before["assignment"]["total"], ov_after["assignment"]["total"]),
          ov_before["assignment"]["total"] == ov_after["assignment"]["total"] == 2,
          (ov_before["assignment"], ov_after["assignment"]))
    check("5.3 统计块 expected 不被演示数据虚增（%s → %s）"
          % (st_before["expected"], st_after["expected"]),
          st_before["expected"] == st_after["expected"] == 2, (st_before, st_after))
    demo_ids = {r["id"] for r in db.query(
        "SELECT id FROM answers WHERE class_id=? AND source=?", (cls, D.DEMO_SOURCE))}
    queue_real = C.queue_rows(db, cls, "9月第2次作业")
    check("5.4 待核对队列里没有演示作答（真实队列 %s，演示 %s）"
          % ([row["id"] for row in queue_real], sorted(demo_ids)),
          not (demo_ids & {row["id"] for row in queue_real}) and set(r1 for r1 in [r1]) and True
          and len(queue_real) == 1 and queue_real[0]["id"] == r2,
          [row["id"] for row in queue_real])
    # T7 改写③（5.4b）
    # 原断言：len(queue_rows(db, cls, D.DEMO_BATCH)) == 4 且恰等于全部演示 id
    # 原依据：单演示批次、4 份，队列 id 集合 == 全部演示 id。
    # 新断言：逐个演示批次核对「该批次队列 == 该批次的演示行」，且各批次数之和 == 演示行数。
    # 改写依据：T12 两批次后，主批次队列只剩 4 条而全部演示 id 是 6 个，原断言必然假；
    #           但「演示批次队列里只含演示作答」这个语义必须保留，所以改成按批次核对。
    per_batch_ok = True
    demo_queue_total = 0
    batch_detail = []
    for batch in D.DEMO_BATCHES:
        batch_ids = {r["id"] for r in db.query(
            "SELECT id FROM answers WHERE class_id=? AND source=? AND batch=?",
            (cls, D.DEMO_SOURCE, batch))}
        queue_ids = {row["id"] for row in C.queue_rows(db, cls, batch)}
        demo_queue_total += len(queue_ids)
        per_batch_ok = per_batch_ok and queue_ids == batch_ids
        batch_detail.append((batch, len(queue_ids), len(batch_ids)))
    check("5.4b 每个演示批次的队列只含本批次的演示作答（合计 %d 份）" % demo_queue_total,
          per_batch_ok and demo_queue_total == len(demo_ids), batch_detail)
    # T7 改写④（5.5）
    # 原断言：counts["answers"] == 2 且 counts["synthetic_answers"] == 4
    # 原依据：真实 2 份 + 演示单批 4 份。
    # 新断言：counts["answers"] == 2（真实份数不变）且 synthetic_answers == 演示行数（6）。
    # 改写依据：T12 演示 6 份；「真实与演示分账」的语义不变，只是演示那一侧跟着扩容。
    check("5.5 overview.counts.answers 只数真实作答、演示单独计数（演示 %d）" % len(demo_ids),
          ov_after["counts"]["answers"] == 2
          and ov_after["counts"]["synthetic_answers"] == len(demo_ids),
          ov_after["counts"])

    # 给演示作答补一条 first_bad check，原题错误数仍然不动
    demo_rows = db.query("SELECT * FROM answers WHERE class_id=? AND source=?", (cls, D.DEMO_SOURCE))
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO answer_checks(class_id, student_id, answer_id, batch, answer_version,"
            " progress, cause_state, observed_step, locate_status, locate_source, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cls, demo_rows[0]["student_id"], demo_rows[0]["id"], demo_rows[0]["batch"],
             demo_rows[0]["fingerprint"], "checked", "undecided", "3x-2=12", "first_bad",
             "preset", now_iso(), now_iso()))
    st_with_demo_check = S.answer_check_stats(db, class_row(db, cls))
    check("5.6 演示作答带 first_bad check 后 original_errors 仍是 1（%s）"
          % st_with_demo_check["original_errors"], st_with_demo_check["original_errors"] == 1,
          st_with_demo_check)
    # T7 改写⑤（5.7）
    # 原断言：直接写进真实批次的演示行被库级触发器归位到 D.DEMO_BATCH（moved["batch"] == DEMO_BATCH）
    # 原依据：T1 用两个库级触发器（trg_answers_demo_batch_ins/_upd）把演示行挪回演示批次，
    #         作为「合成证据不污染真实作业」的最后一道网。
    # 新断言：不再归位（行留在真实批次里，这是刻意承认的代价），但**统计仍然干净**——
    #         5.8 的 expected/original_errors 不变，由 stats.py 的 `AND a.source<>'合成演示'` 保证。
    # 改写依据：T6 删除了两个库级触发器（隐形行为），防线改为 SQL 过滤，并已写进契约 §5.3；
    #           验「归位」在实现删除后不可能通过，只会把缺陷写成预期。
    #           本条现在同时是「删触发器后没有别的机制偷偷挪数据」的反向证据。
    old_demo = add_answer(db, cls, jia, "DEMO-LEGACY-1", batch="9月第2次作业",
                          source=D.DEMO_SOURCE)
    moved = db.query_one("SELECT batch FROM answers WHERE id=?", (old_demo,))
    check("5.7 触发器已删除：直接写进真实批次的演示行不再被归位（留在 9月第2次作业）",
          moved["batch"] == "9月第2次作业", moved)
    st_legacy = S.answer_check_stats(db, class_row(db, cls))
    check("5.8 归位后 expected 仍是 2、original_errors 仍是 1",
          st_legacy["expected"] == 2 and st_legacy["original_errors"] == 1, st_legacy)

    # ---------------- 6. reset 口径：三条同时满足才删 ----------------
    db, cls = new_env("reset")
    jia = add_student(db, cls, "学生甲")
    # 6.1 真实作答把批名写成演示批名
    collide = add_answer(db, cls, jia, "REAL-COLLIDE-1", batch=D.DEMO_BATCH)
    # 6.2 真实作答的「来源批次」列写了「合成演示」
    fake_source = add_answer(db, cls, jia, "REAL-FAKE-1", batch="9月第3次作业", source=D.DEMO_SOURCE)
    # 6.3 真实作答：演示批名 + 演示来源 + 非 DEMO- 前缀
    both = add_answer(db, cls, jia, "REAL-BOTH-1", batch=D.DEMO_BATCH, source=D.DEMO_SOURCE)
    D.seed(db, None, cls)
    total_before = db.scalar("SELECT COUNT(*) FROM answers WHERE class_id=?", (cls,))
    out = D.reset(db, None, cls)
    left = {r["record_key"] for r in db.query("SELECT record_key FROM answers WHERE class_id=?", (cls,))}
    # T7 改写⑥（6.1）
    # 原断言：out["deleted"]["answers"] == 4
    # 原依据：演示数据单批 4 份。
    # 新断言：reset 实删数 == 本次 seed 进去的演示记录数 len(D.build()["answers"])（6 份）。
    # 改写依据：T12 演示 6 份；「reset 只删演示、份数与 seed 一致」的语义不变。
    #           注意不能用「按 source 统计的行数」当基准：本环境里有 2 条故意把来源列写成
    #           合成演示的真实行（6.2/6.3/6.4 要求它们不被删），数出来是 8，而 reset 实删 6
    #           —— 这正说明 reset 的三重判据比单看 source 更严，属于正确行为。
    check("6.1 reset 删掉 %d 份合成作答" % len(D.build()["answers"]),
          out["deleted"]["answers"] == len(D.build()["answers"]), out["deleted"])
    check("6.2 批名撞演示批名的真实作答没被删", "REAL-COLLIDE-1" in left, left)
    check("6.3 来源列写「合成演示」的真实作答没被删", "REAL-FAKE-1" in left, left)
    check("6.4 批名+来源都撞、但没有 DEMO- 前缀的真实作答没被删", "REAL-BOTH-1" in left, left)
    check("6.5 一条都没多删（%d → %d）" % (total_before, len(left)),
          len(left) == 3 and out["kept"]["imported_answers"] == 1
          and out["kept"]["synthetic_answers"] == 2, out["kept"])
    check("6.6 reset 后合成学生已清空（没有残留空壳学生）",
          db.scalar("SELECT COUNT(*) FROM students WHERE class_id=? AND note=?",
                    (cls, D.STUDENT_NOTE)) == 0)

    # ---------------- 7. 演示模块被裁掉时安静退化 ----------------
    saved = sys.modules.get("app.demo_answers")
    import app as app_pkg
    saved_attr = getattr(app_pkg, "demo_answers", None)
    sys.modules["app.demo_answers"] = None
    if saved_attr is not None:
        delattr(app_pkg, "demo_answers")
    try:
        broken = C.candidates_for({"question_text": "3(x-2)=12"})
        note = C.causes_note({"question_text": "3(x-2)=12"})
        no_arg = C.causes_note()
    finally:
        if saved is not None:
            sys.modules["app.demo_answers"] = saved
        else:
            sys.modules.pop("app.demo_answers", None)
        if saved_attr is not None:
            setattr(app_pkg, "demo_answers", saved_attr)
    check("7.1 演示模块不可用 → 候选为空、说明用兜底句",
          broken == [] and note == C.FALLBACK_CAUSES_NOTE and no_arg == C.FALLBACK_CAUSES_NOTE,
          (broken, note, no_arg))

    total = len(RESULTS)
    passed = sum(1 for item in RESULTS if item)
    print("")
    print("合计 %d 项，通过 %d，失败 %d" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
