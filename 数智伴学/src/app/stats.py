"""统计：只在「已确认」错题上计算，每条统计都给出可追溯的错题编号。

口径说明（写在前端提示里，也在返回值中给出）：
- 只统计 status='confirmed' 的记录，待核对与已退回不计入。
- 知识点与错误类型同时给出「错题条数」和「涉及学生人数」。
- 不计算正确率、掌握率、趋势线；样本过少时只提示，不做推断。
"""

import json

from . import answer_check as check_state

MIN_SAMPLE = 3


def _load_list(raw):
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _row_mistake(row):
    return {
        "id": row["id"],
        "student_id": row["student_id"],
        "student_name": row["student_name"],
        "student_note": row["student_note"],
        "knowledge_points": _load_list(row["knowledge_points"]),
        "error_type": row["error_type"],
        "occurred_on": row["occurred_on"],
        "status": row["status"],
    }


def confirmed_mistakes(db, class_id, student_id=None, filters=None):
    filters = filters or {}
    sql = (
        "SELECT m.*, s.name AS student_name, s.note AS student_note FROM mistakes m JOIN students s ON s.id = m.student_id"
        " WHERE m.class_id = ? AND m.status = 'confirmed'"
    )
    params = [class_id]
    if student_id:
        sql += " AND m.student_id = ?"
        params.append(student_id)
    if filters.get("from"):
        sql += " AND m.occurred_on >= ?"
        params.append(filters["from"])
    if filters.get("to"):
        sql += " AND m.occurred_on <= ?"
        params.append(filters["to"])
    sql += " ORDER BY m.occurred_on DESC, m.id DESC"
    return db.query(sql, params)


def _buckets(rows, field):
    """按知识点（多值）或错误类型（单值）聚合。

    同一条错题里同一知识点重复出现时只计一次，对应错题编号也不重复。
    """
    order = []
    data = {}
    for row in rows:
        if field == "knowledge_points":
            values = row["knowledge_points"] or []
        else:
            values = [row["error_type"]] if row["error_type"] else []
        seen = set()
        for value in values:
            value = (value or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            if value not in data:
                data[value] = {"name": value, "mistakes": 0, "students": set(), "mistake_ids": []}
                order.append(value)
            bucket = data[value]
            bucket["mistakes"] += 1
            bucket["students"].add(row["student_id"])
            bucket["mistake_ids"].append(row["id"])
    items = list(data.values())
    for item in items:
        item["student_count"] = len(item["students"])
        del item["students"]
    items.sort(key=lambda b: (-b["mistakes"], -b["student_count"], b["name"]))
    return items


def _basis_text(rows, kp_items, error_items):
    if not rows:
        return "当前范围内没有已确认的错题，因此没有可依据的统计。"
    parts = ["已确认的 %d 条错题中，" % len(rows)]
    if kp_items:
        top = kp_items[0]
        parts.append("「%s」出现最多，共 %d 条错题、涉及 %d 名学生；" % (top["name"], top["mistakes"], top["student_count"]))
    if error_items:
        top = error_items[0]
        parts.append("错误类型里「%s」最多，共 %d 条；" % (top["name"], top["mistakes"]))
    parts.append("以上只反映错题分布，不能据此推算学生的正确率或掌握率。")
    return "".join(parts)


def build_stats(db, class_row, student_row=None, filters=None):
    filters = filters or {}
    rows = confirmed_mistakes(db, class_row["id"], student_row["id"] if student_row else None, filters)
    rows = [_row_mistake(r) for r in rows]

    kp_filter = (filters.get("kp") or "").strip()
    error_filter = (filters.get("error_type") or "").strip()
    scoped = rows
    if kp_filter:
        scoped = [r for r in scoped if kp_filter in (r["knowledge_points"] or [])]
    if error_filter:
        scoped = [r for r in scoped if r["error_type"] == error_filter]

    kp_items = _buckets(scoped, "knowledge_points")
    error_items = _buckets(scoped, "error_type")
    student_ids = {r["student_id"] for r in scoped}

    # 状态计数：用于说明「为什么统计里没有那几条」。
    # 与已确认条数用同一套日期范围，否则选了日期后这两行会显示全部日期，读起来像统计错了。
    status_counts = {"pending": 0, "returned": 0, "confirmed": len(rows)}
    count_sql = "SELECT status, COUNT(*) AS n FROM mistakes WHERE class_id = ?"
    count_params = [class_row["id"]]
    if student_row:
        count_sql += " AND student_id = ?"
        count_params.append(student_row["id"])
    if filters.get("from"):
        count_sql += " AND occurred_on >= ?"
        count_params.append(filters["from"])
    if filters.get("to"):
        count_sql += " AND occurred_on <= ?"
        count_params.append(filters["to"])
    count_sql += " GROUP BY status"
    for item in db.query(count_sql, count_params):
        status_counts[item["status"]] = item["n"]
    status_counts["confirmed"] = len(rows)

    notes = []
    if not rows:
        notes.append("当前范围内没有已确认的错题，暂无可统计内容。待核对或已退回的记录不计入统计。")
    elif len(scoped) < MIN_SAMPLE:
        notes.append("已确认错题 %d 条，样本较少，目前只供查看错题分布。" % len(scoped))
    no_kp = [r for r in scoped if not r["knowledge_points"]]
    no_error = [r for r in scoped if not r["error_type"]]
    if no_kp or no_error:
        notes.append(
            "已确认错题中有 %d 条尚未填写知识点、%d 条尚未填写错误类型，这些错题仍计入总数，空白不代表分析结论。"
            % (len(no_kp), len(no_error))
        )
    if student_row is None and len(scoped) >= MIN_SAMPLE and len(student_ids) < 2:
        notes.append("这些错题只涉及 1 名学生，只能反映个人情况，不能代表班级整体。")

    filtered_out = len(rows) - len(scoped)
    if filtered_out and (kp_filter or error_filter):
        notes.append("当前筛选条件下有 %d 条已确认错题被排除在本次统计之外。" % filtered_out)
    if filters.get("from") or filters.get("to"):
        notes.append("待核对与已退回的条数也按所选日期范围统计。")

    return {
        "scope": "student" if student_row else "class",
        "class": {"id": class_row["id"], "name": class_row["name"]},
        "student": ({"id": student_row["id"], "name": student_row["name"], "note": student_row["note"]} if student_row else None),
        "filters": {"from": filters.get("from", ""), "to": filters.get("to", ""), "kp": kp_filter, "error_type": error_filter},
        "summary": {
            "confirmed": len(scoped),
            "confirmed_all": len(rows),
            "students": len(student_ids),
            "pending": status_counts.get("pending", 0),
            "returned": status_counts.get("returned", 0),
            "no_knowledge_points": len(no_kp),
            "no_error_type": len(no_error),
        },
        "knowledge_points": kp_items,
        "error_types": error_items,
        "mistake_ids": [r["id"] for r in scoped],
        "notes": notes,
        "basis": _basis_text(scoped, kp_items, error_items),
        "rule": "仅统计「已确认」的错题；一条错题涉及多个知识点，所以知识点条数相加可能超过错题总数；不计算正确率或掌握率。",
    }


def options_for_filters(db, class_row, student_id=None):
    """筛选下拉选项：来自该范围（不限状态）的已有取值。"""
    rows = db.query(
        "SELECT knowledge_points, error_type FROM mistakes WHERE class_id=? AND (? IS NULL OR student_id=?)",
        (class_row["id"], student_id, student_id),
    )
    kps, errors = set(), set()
    for row in rows:
        for kp in _load_list(row["knowledge_points"]):
            if kp.strip():
                kps.add(kp.strip())
        if row["error_type"]:
            errors.add(row["error_type"])
    return {"knowledge_points": sorted(kps), "error_types": sorted(errors)}


ANSWER_CHECK_BASIS = "原题错误数按已保存错步计；原因判断数单独统计，两者口径不同"


def answer_check_stats(db, class_row, batch=""):
    """作答核对统计块（契约 §3.6，口径按 A3）。

    缺测与正确作答不计入原题错误数：original_errors 只数「当前版本 check 且定位为首处错步」，
    cause_decided 单独数「教师已判断」，两者不是同一个分母。
    桌面端打开旧库时可能还没有新表，这里退化成零值而不报错。
    """
    block = {
        "batch": "",
        "expected": 0,
        "valid": 0,
        "missing": 0,
        "pending": 0,
        "checked": 0,
        "original_errors": 0,
        "cause_decided": 0,
        "basis": ANSWER_CHECK_BASIS,
    }
    if not check_state.table_ready(db):
        return block

    batch = check_state.resolve_batch(db, class_row["id"], batch)
    block["batch"] = batch
    row = db.query_one(
        "SELECT COUNT(*) AS expected,"
        " COALESCE(SUM(CASE WHEN a.missing=1 THEN 1 ELSE 0 END), 0) AS missing,"
        " COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM answer_checks c"
        "   WHERE c.answer_id=a.id AND c.answer_version=a.fingerprint) THEN 1 ELSE 0 END), 0) AS checked"
        " FROM answers a WHERE a.class_id=? AND a.batch=?",
        (class_row["id"], batch),
    ) or {}
    expected = int(row.get("expected") or 0)
    missing = int(row.get("missing") or 0)
    checked = int(row.get("checked") or 0)

    # 只认当前版本：重导后旧 check 自动退出统计，不写任何清理逻辑。
    original_errors = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND a.batch=? AND c.answer_version=a.fingerprint"
        " AND c.locate_status='first_bad' AND a.missing=0",
        (class_row["id"], batch),
    ) or 0
    cause_decided = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND a.batch=? AND c.answer_version=a.fingerprint"
        " AND c.cause_state='decided'",
        (class_row["id"], batch),
    ) or 0

    block.update({
        "expected": expected,
        "valid": expected - missing,
        "missing": missing,
        "pending": expected - checked,
        "checked": checked,
        "original_errors": int(original_errors),
        "cause_decided": int(cause_decided),
    })
    return block
