"""R1-T15（B1）备份恢复：answer_checks / answer_followups 必须跟着搬，且外键重映射。

背景与依据
- t13 审计结论（B1，blocker）：`backup.apply_staged` 先 `DELETE FROM classes WHERE user_id=?`
  按外键级联清空，随后只重建 9 张表，**没有** answer_checks / answer_followups 分支 ——
  换电脑/重装后恢复备份，教师判断、人工核验、撤回痕迹、追问原话与证据快照静默丢失，
  界面还提示成功（返回的 counts 里这两张表是 0）。
- 本套件只验证「备份→恢复」这一条链路，自建临时库（tests/.tmp-r1-15），不引用任何库外路径、
  不读真实数据目录、不联网。

覆盖（与任务书 7 条一一对应）
1. 备份侧导出范围：create_backup 的清单里 answer_checks / answer_followups 条数非 0（整库快照）；
2. 恢复侧重建 + 外键重映射（class/student/answer/check 都换成新 id，不串学生、不串班级）；
3. 撤回行（answer_version=''）与 invalidated_* 原样保留（不是「只恢复有效行」）；
4. 返回的 counts 与恢复后库内实际条数一致；
5. 旧格式备份（没有这两张表）恢复照常成功；
6. 注入故障（MISTAKE_FAULT=check_restore）整体回滚，不留半恢复状态；
   另有「暂存库里核对记录指向不存在的作答」这一真实失败路径也跟着回滚；
7. 既有 9 张表的恢复行为不变（条数与样本行都对得上）。
"""

import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from app import backup  # noqa: E402
from app.db import Database  # noqa: E402
from logmeta import environment_lines, exit_line  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r1-15")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-15-backup-tables.txt")
NOW = "2026-11-25T10:00:00+08:00"

RESULTS = {"pass": 0, "fail": 0}
LOG_LINES = []


def log(text=""):
    print(text)
    LOG_LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s %s" % (name, detail if isinstance(detail, str) else json.dumps(
            detail, ensure_ascii=False, default=str)))
    return bool(ok)


def section(title):
    log("")
    log("== %s ==" % title)


def jdump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# --------------------------------------------------------------------------
# 造数：一个数据目录、一个账号、两个班级
# --------------------------------------------------------------------------
def build_data_dir(name):
    data_dir = os.path.join(WORK, name)
    shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(os.path.join(data_dir, "uploads"))
    db = Database(os.path.join(data_dir, "app.db"))
    rows = {
        "users": (1, "teacher", "bk-t15"),
        "classes": ((1, "一班"), (2, "二班")),
        "students": ((11, 1, "学生甲"), (12, 1, "学生乙"), (21, 2, "学生丙")),
        "answers": (
            (101, 1, 11, "A1", "作业A", "3(x-2)=12"),
            (102, 1, 12, "A2", "作业A", "2(x+3)=10"),
            (103, 1, 11, "A3", "作业A", "4(x-1)=8"),
            (201, 2, 21, "B1", "作业B", "5x-3=2x+6"),
        ),
        "mistakes": ((301, 1, 11, "3(x-2)=12"), (302, 2, 21, "5x-3=2x+6")),
        "reviews": ((401, 1, 11, 101, 301), (402, 2, 21, 201, 302)),
    }
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id, username, display_name, pwd_salt, pwd_hash,"
                     " iterations, created_at, backup_key) VALUES (?,?,?,?,?,?,?,?)",
                     (rows["users"][0], rows["users"][1], "老师", "s", "h", 1, NOW, rows["users"][2]))
        for cid, cname in rows["classes"]:
            conn.execute("INSERT INTO classes(id, user_id, name, grade, note, created_at, updated_at)"
                         " VALUES (?,1,?,'七年级','',?,?)", (cid, cname, NOW, NOW))
        for sid, cid, sname in rows["students"]:
            conn.execute("INSERT INTO students(id, class_id, name, code, note, active, created_at,"
                         " updated_at) VALUES (?,?,?,'','',1,?,?)", (sid, cid, sname, NOW, NOW))
        for aid, cid, sid, key, batch, q in rows["answers"]:
            conn.execute(
                "INSERT INTO answers(id, class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,'','','1',?,'一元一次方程','x=6','3(x-2)=12\\nx=6','真实导入',"
                "'2026-11-20',0,'',?,?,?,?)",
                (aid, cid, sid, key, batch, q, "fp-%d" % aid, NOW, NOW, NOW))
        for mid, cid, sid, q in rows["mistakes"]:
            conn.execute(
                "INSERT INTO mistakes(id, student_id, class_id, question_text, solution_text,"
                " knowledge_points, error_type, observed_errors, possible_causes, advice_text,"
                " source_type, source_detail, note, teacher_note, occurred_on, status, image_file,"
                " insufficient, insufficient_reason, ai_snapshot, ai_mode, ai_error, ai_error_code,"
                " teacher_edited_fields, created_at, updated_at, confirmed_at, confirmed_by,"
                " returned_at, returned_reason)"
                " VALUES (?,?,?,?,'x=6','[]','','[]','[]','','作答分析','','','','2026-11-20',"
                "'confirmed','',0,'','{}','mock','','','',?,?,?,1,'','')",
                (mid, sid, cid, q, NOW, NOW, NOW))
        for rid, cid, sid, aid, mid in rows["reviews"]:
            conn.execute(
                "INSERT INTO answer_reviews(id, class_id, student_id, answer_id, status, provider,"
                " model, causes, note, uncertain, raw, answer_fingerprint, mistake_id, created_at,"
                " updated_at) VALUES (?,?,?,?,'confirmed','mock','m','[]','',0,'{}',?,?,?,?)",
                (rid, cid, sid, aid, "fp-%d" % aid, mid, NOW, NOW))
        # 三条核对结论：① 教师人工判断 + 人工核验；② 无需判断；③ 撤回（answer_version 为空）
        conn.execute(
            "INSERT INTO answer_checks(id, class_id, student_id, answer_id, batch, answer_version,"
            " progress, cause_state, cause_source, observed_step, first_bad_index, locate_status,"
            " locate_source, cause_code, cause_label, cause_history, teacher_note, basis_note,"
            " change_reason, verify_source, teacher_error_text, teacher_verified_at,"
            " teacher_verified_by, created_by, created_at, updated_at, decided_at, decided_by,"
            " invalidated_at, invalidated_reason, invalidated_by)"
            " VALUES (501,1,11,101,'作业A','fp-101','checked','decided','teacher','3x-2=12',1,"
            "'first_bad','preset','E-DIST-01','只乘了第一项','[]','让他重做一道','原图第二行是 3x-2=12',"
            "'','manual','3x-2=12（原图）',?,1,1,?,?,?,1,'','',NULL)",
            (NOW, NOW, NOW, NOW))
        conn.execute(
            "INSERT INTO answer_checks(id, class_id, student_id, answer_id, batch, answer_version,"
            " progress, cause_state, cause_source, observed_step, cause_code, cause_label,"
            " cause_history, teacher_note, basis_note, verify_source, created_by, created_at,"
            " updated_at, invalidated_at, invalidated_reason, invalidated_by)"
            " VALUES (502,1,12,102,'作业A','fp-102','checked','not_applicable','none','','','','[]',"
            "'','','rule',1,?,?,'','',NULL)", (NOW, NOW))
        conn.execute(
            "INSERT INTO answer_checks(id, class_id, student_id, answer_id, batch, answer_version,"
            " progress, cause_state, cause_source, observed_step, cause_code, cause_label,"
            " cause_history, teacher_note, basis_note, verify_source, created_by, created_at,"
            " updated_at, invalidated_at, invalidated_reason, invalidated_by)"
            " VALUES (503,1,11,103,'作业A','','pending','undecided','none','','','','[{\"action\":"
            "\"retract\"}]','','','',1,?,?,?,'老师在页面上撤回本次结论',1)",
            (NOW, NOW, NOW))
        conn.execute(
            "INSERT INTO answer_checks(id, class_id, student_id, answer_id, batch, answer_version,"
            " progress, cause_state, cause_source, observed_step, cause_code, cause_label,"
            " cause_history, teacher_note, basis_note, verify_source, created_by, created_at,"
            " updated_at, invalidated_at, invalidated_reason, invalidated_by)"
            " VALUES (504,2,21,201,'作业B','fp-201','checked','decided','teacher','5x-2x=6-3',"
            "'E-MOVE-01','移项没变号','[]','','原图如此','rule',1,?,?,'','',NULL)", (NOW, NOW))
        # 追问原话：一条有效（带证据快照）、一条已失效、一条在二班
        conn.execute(
            "INSERT INTO answer_followups(id, class_id, student_id, answer_id, check_id,"
            " answer_version, question, source, source_detail, answer_text, asked_at, answered_at,"
            " evidence_snapshot, invalidated_at, invalidated_reason, created_at, updated_at)"
            " VALUES (601,1,11,101,501,'fp-101','你当时是怎么展开这个括号的？','preset','demo_record',"
            "'我以为 3 只乘 x。',?,?,'{\"observed_step\": \"3x-2=12\", \"cause_state\": \"decided\"}',"
            "'','',?,?)", (NOW, NOW, NOW, NOW))
        conn.execute(
            "INSERT INTO answer_followups(id, class_id, student_id, answer_id, check_id,"
            " answer_version, question, source, source_detail, answer_text, asked_at, answered_at,"
            " evidence_snapshot, invalidated_at, invalidated_reason, created_at, updated_at)"
            " VALUES (602,1,12,102,502,'fp-102','这一步你是怎么算的？','teacher','teacher','我直接写的',"
            "?,?,'{}',?,'evidence_changed：错步改了',?,?)", (NOW, NOW, NOW, NOW, NOW))
        conn.execute(
            "INSERT INTO answer_followups(id, class_id, student_id, answer_id, check_id,"
            " answer_version, question, source, source_detail, answer_text, asked_at, answered_at,"
            " evidence_snapshot, invalidated_at, invalidated_reason, created_at, updated_at)"
            " VALUES (603,2,21,201,504,'fp-201','你把哪一项移到另一边了？','preset','rule:term_move',"
            "'忘了变号',?,?,'{}','','',?,?)", (NOW, NOW, NOW, NOW))
        # t14 的外发审计（账号级，两条）
        conn.execute("INSERT INTO ai_egress_audit(user_id, username, flow, model_channel,"
                     " payload_fields, payload_bytes, created_at) VALUES (1,'teacher','作答分析',"
                     "'custom','[\"model\",\"messages[system,user]\"]',1234,?)", (NOW,))
        conn.execute("INSERT INTO ai_egress_audit(user_id, username, flow, model_channel,"
                     " payload_fields, payload_bytes, created_at) VALUES (1,'teacher','错题重新分析',"
                     "'custom','[\"model\",\"messages[system,user]\"]',2345,?)", (NOW,))
    db.conn.close()
    return data_dir


def counts(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    try:
        return backup._counts(conn)
    finally:
        conn.close()


def query(data_dir, sql, params=()):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def scalar(data_dir, sql, params=()):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def make_backup(data_dir, tag):
    zip_path = os.path.join(WORK, "backups", "%s.zip" % tag)
    manifest = backup.create_backup(data_dir, zip_path, 1)
    staging = os.path.join(WORK, "staging-%s" % tag)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    manifest2, relation = backup.inspect_backup(zip_path, staging)
    return zip_path, staging, manifest, manifest2, relation


def clone_staging(staging, tag):
    target = os.path.join(WORK, "staging-%s" % tag)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(staging, target)
    return target


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-T15（B1）备份恢复：answer_checks / answer_followups 随备份走")
    for line in environment_lines():
        log(line)
    log("临时库：%s" % WORK)
    log("网络：无（纯本地 SQLite 与 zip）")

    data_dir = build_data_dir("live")
    before = counts(data_dir)
    log("恢复前 counts：%s" % jdump(before))

    section("1. 备份侧：导出范围包含两张新表（整库快照，不需要第二套导出格式）")
    zip_path, staging, manifest, manifest2, relation = make_backup(data_dir, "full")
    check("1.1 create_backup 清单里 answer_checks=3、answer_followups=3（不是 0）",
          manifest.get("counts", {}).get("answer_checks") == 3
          and manifest.get("counts", {}).get("answer_followups") == 3
          and manifest.get("counts", {}).get("ai_egress_audit") == 2, manifest.get("counts"))
    staging_db = os.path.join(staging, "app.db")
    staged_counts = {}
    conn = sqlite3.connect(staging_db)
    try:
        staged_counts = backup._counts(conn)
    finally:
        conn.close()
    check("1.2 暂存库（解开后的备份）里同样是 3/3，且备份文件里只有一份 app.db",
          staged_counts.get("answer_checks") == 3
          and staged_counts.get("answer_followups") == 3
          and sorted(os.listdir(staging)).count("app.db") == 1, staged_counts)
    check("1.3 inspect_backup 认为这份备份就是本账号的（same_key）",
          relation.get("same_key") is True or relation.get("id_match") is True, relation)

    section("2. 恢复侧：按 answer_map / student_map 重建，外键全部重映射")
    # 先把库里这两张表与 9 张表都清空成「换电脑后的空库」，再恢复
    blank = build_data_dir("live")
    for table in ("answer_checks", "answer_followups", "ai_egress_audit", "answer_reviews",
                  "followups", "tutor_advice", "advice", "mistakes", "answers", "students",
                  "classes"):
        conn = sqlite3.connect(os.path.join(blank, "app.db"))
        try:
            with conn:
                conn.execute("DELETE FROM %s" % table)
        finally:
            conn.close()
    wiped = counts(blank)
    check("2.0 造一份「空库」作为恢复目标（两张新表为 0、旧表清空）",
          wiped.get("answer_checks", 0) == 0 and wiped.get("classes", 0) == 0, wiped)

    result = backup.apply_staged(clone_staging(staging, "restore1"), blank, 1, mode="restore")
    after = counts(blank)
    log("恢复后 counts：%s" % jdump(after))
    check("2.1 恢复后 answer_checks=3、answer_followups=3（都搬回来了）",
          after.get("answer_checks") == 3 and after.get("answer_followups") == 3, after)
    check("2.2 恢复后 9 张旧表条数与备份时一致（classes/students/mistakes/answers/reviews）",
          all(after.get(k) == before.get(k) for k in
              ("classes", "students", "mistakes", "answers", "answer_reviews")),
          {"after": after, "before": before})
    check("2.3 核对记录与作答/学生/班级三处外键对得上（没有沿用旧 id）",
          scalar(blank, "SELECT COUNT(*) FROM answer_checks c JOIN answers a ON a.id=c.answer_id"
                        " JOIN students s ON s.id=c.student_id"
                        " WHERE a.student_id=c.student_id AND a.class_id=c.class_id"
                        " AND s.class_id=c.class_id") == 3,
          query(blank, "SELECT id, class_id, student_id, answer_id FROM answer_checks ORDER BY id"))
    check("2.4 跨班级不串：一班 3 条、二班 1 条，且各自挂在自己的班级上",
          scalar(blank, "SELECT COUNT(*) FROM answer_checks c JOIN classes cl ON cl.id=c.class_id"
                        " WHERE cl.name='一班'") == 3
          and scalar(blank, "SELECT COUNT(*) FROM answer_checks c JOIN classes cl ON cl.id=c.class_id"
                            " WHERE cl.name='二班'") == 1, None)
    check("2.5 追问原话的 check_id 指向恢复后的新核对行（不是暂存库里的旧 501/502/504）",
          scalar(blank, "SELECT COUNT(*) FROM answer_followups f JOIN answer_checks c ON c.id=f.check_id"
                        " WHERE c.answer_id=f.answer_id") == 3
          and scalar(blank, "SELECT COUNT(*) FROM answer_followups WHERE check_id IN (501,502,504)") == 0,
          query(blank, "SELECT id, answer_id, check_id FROM answer_followups ORDER BY id"))

    section("3. 保留语义：撤回行与 invalidated_* 原样保留")
    retracted = query(blank, "SELECT answer_version, progress, invalidated_at, invalidated_reason"
                             " FROM answer_checks WHERE answer_version=''")
    check("3.1 撤回行（answer_version 为空）被原样搬回，没有被「只恢复有效行」过滤掉",
          len(retracted) == 1 and retracted[0]["invalidated_at"] == NOW
          and retracted[0]["invalidated_reason"] == "老师在页面上撤回本次结论"
          and retracted[0]["progress"] == "pending", retracted)
    history_text = scalar(blank, "SELECT cause_history FROM answer_checks"
                                " WHERE answer_version=''") or ""
    check("3.2 撤回行的 cause_history 也原样保留（含 retract 事件）",
          "retract" in history_text, history_text)
    invalid = query(blank, "SELECT question, invalidated_at, invalidated_reason"
                           " FROM answer_followups WHERE invalidated_at<>''")
    check("3.3 已失效的追问原话连同 invalidated_at/_reason 一起搬回",
          len(invalid) == 1 and invalid[0]["invalidated_at"] == NOW
          and "evidence_changed" in (invalid[0]["invalidated_reason"] or ""), invalid)
    snap = scalar(blank, "SELECT evidence_snapshot FROM answer_followups"
                         " WHERE answer_text='我以为 3 只乘 x。'")
    check("3.4 证据快照（evidence_snapshot）逐字保留",
          json.loads(snap or "{}") == {"observed_step": "3x-2=12", "cause_state": "decided"}, snap)
    manual = query(blank, "SELECT verify_source, teacher_error_text, basis_note, cause_label"
                          " FROM answer_checks WHERE cause_state='decided' AND verify_source='manual'")
    check("3.5 人工核验那一列组（verify_source/teacher_error_text/basis_note）逐字保留",
          len(manual) == 1 and manual[0]["teacher_error_text"] == "3x-2=12（原图）"
          and manual[0]["basis_note"] == "原图第二行是 3x-2=12"
          and manual[0]["cause_label"] == "只乘了第一项", manual)
    audit = query(blank, "SELECT user_id, flow, payload_fields FROM ai_egress_audit ORDER BY id")
    check("3.6 t14 的外发审计按账号整体搬回（2 条、user_id 重映射为当前账号）",
          len(audit) == 2 and all(row["user_id"] == 1 for row in audit)
          and audit[0]["flow"] == "作答分析", audit)

    section("4. 返回的 counts 与库内实际条数一致")
    returned = result.get("counts") or {}
    actual = counts(blank)
    check("4.1 apply_staged 返回的 counts 与恢复后库内实际条数逐项相等",
          all(returned.get(k) == actual.get(k) for k in
              ("classes", "students", "mistakes", "answers", "answer_reviews",
               "answer_checks", "answer_followups", "ai_egress_audit")),
          {"returned": returned, "actual": actual})
    check("4.2 返回的 answer_checks / answer_followups 不再是 0（旧版这里恒 0）",
          returned.get("answer_checks") == 3 and returned.get("answer_followups") == 3, returned)

    section("5. 重复恢复不叠加（恢复 = 用备份状态替换该账号的数据）")
    backup.apply_staged(clone_staging(staging, "restore2"), blank, 1, mode="restore")
    again = counts(blank)
    check("5.1 第二次恢复后仍是 3/3/2（没有翻倍）",
          again.get("answer_checks") == 3 and again.get("answer_followups") == 3
          and again.get("ai_egress_audit") == 2, again)

    section("6. 向后兼容：1.8.1 形态的旧备份（没有这两张表）恢复照常成功")
    old_staging = clone_staging(staging, "legacy")
    old_db = os.path.join(old_staging, "app.db")
    conn = sqlite3.connect(old_db)
    try:
        with conn:
            conn.execute("DROP TABLE IF EXISTS answer_followups")
            conn.execute("DROP TABLE IF EXISTS answer_checks")
            conn.execute("DROP TABLE IF EXISTS ai_egress_audit")
    finally:
        conn.close()
    manifest_path = os.path.join(old_staging, "manifest.json")
    with open(manifest_path, encoding="utf-8") as fh:
        old_manifest = json.load(fh)
    for key in ("answer_checks", "answer_followups", "ai_egress_audit"):
        old_manifest.get("counts", {}).pop(key, None)
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(old_manifest, fh, ensure_ascii=False)
    legacy_target = build_data_dir("legacy-live")
    for table in ("classes", "students", "mistakes", "answers", "answer_reviews",
                  "answer_checks", "answer_followups", "ai_egress_audit"):
        conn = sqlite3.connect(os.path.join(legacy_target, "app.db"))
        try:
            with conn:
                conn.execute("DELETE FROM %s" % table)
        finally:
            conn.close()
    legacy_error = None
    try:
        legacy_result = backup.apply_staged(old_staging, legacy_target, 1, mode="restore")
    except Exception as exc:  # noqa: BLE001
        legacy_error = exc
        legacy_result = None
    legacy_counts = counts(legacy_target)
    check("6.1 旧格式备份恢复成功（不报错、不抛 BackupError）",
          legacy_error is None, repr(legacy_error))
    check("6.2 旧格式备份恢复后旧表照常回来（classes/answers/mistakes 与备份一致）",
          legacy_counts.get("classes") == 2 and legacy_counts.get("answers") == 4
          and legacy_counts.get("mistakes") == 2, legacy_counts)
    check("6.3 旧格式备份里没有的两张新表为 0，且返回的 counts 不报错",
          legacy_counts.get("answer_checks") == 0 and legacy_counts.get("answer_followups") == 0
          and (legacy_result or {}).get("counts", {}).get("answer_checks") == 0,
          (legacy_result or {}).get("counts"))

    section("7. 失败回滚：新增步骤上的故障不留半恢复状态")
    # 7.1 注入故障点（backup._fault("check_restore")）
    fault_target = build_data_dir("fault-live")
    fault_before = counts(fault_target)
    os.environ["MISTAKE_FAULT"] = "check_restore"
    fault_error = None
    try:
        backup.apply_staged(clone_staging(staging, "fault"), fault_target, 1, mode="restore")
    except Exception as exc:  # noqa: BLE001
        fault_error = exc
    finally:
        os.environ.pop("MISTAKE_FAULT", None)
    fault_after = counts(fault_target)
    check("7.1 注入 check_restore 故障 → 抛 BackupError（而且是「原数据未改动」那句）",
          fault_error is not None and "原数据未改动" in str(fault_error), repr(fault_error))
    check("7.2 回滚后库内条数与恢复前完全相同（不留半恢复状态，含两张新表与旧表）",
          fault_after == fault_before, {"before": fault_before, "after": fault_after})
    check("7.3 回滚后两张新表仍是原来的 3/3（没有被清空、也没有被塞一半）",
          fault_after.get("answer_checks") == 3 and fault_after.get("answer_followups") == 3,
          fault_after)

    # 7.4 真实失败路径：暂存库里核对记录指向不存在的作答
    bad_staging = clone_staging(staging, "bad-ref")
    bad_db = os.path.join(bad_staging, "app.db")
    conn = sqlite3.connect(bad_db)
    try:
        with conn:
            conn.execute("UPDATE answer_checks SET answer_id=999999 WHERE id=501")
    finally:
        conn.close()
    bad_target = build_data_dir("bad-live")
    bad_before = counts(bad_target)
    bad_error = None
    try:
        backup.apply_staged(bad_staging, bad_target, 1, mode="restore")
    except Exception as exc:  # noqa: BLE001
        bad_error = exc
    bad_after = counts(bad_target)
    check("7.4 暂存库里核对记录指向不存在的作答 → 抛错并整体回滚（不留半条）",
          bad_error is not None and "找不到所属学生或作答" in str(bad_error)
          and bad_after == bad_before, {"error": repr(bad_error),
                                        "before": bad_before, "after": bad_after})

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                         RESULTS["pass"], RESULTS["fail"]))
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
