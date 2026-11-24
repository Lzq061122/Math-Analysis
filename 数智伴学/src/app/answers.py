"""CSV 批量导入作答：解析、逐行校验、学生匹配、重复/冲突判断与入库。

这里只处理本地文件内容：不调用任何 AI 服务，不发送照片或作答文字。
一条「作答记录」对应 CSV 的一行，记录编号（record_key）在同一个班级里唯一，
用来识别重复导入；编号相同但内容不同时按冲突处理，必须由老师明确选择覆盖。
"""

import csv
import hashlib
import io
import json
import re
from datetime import datetime

MAX_ROWS = 500
MAX_TEXT_BYTES = 2 * 1024 * 1024

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HEADERS = (
    ("记录编号", "record_key"),
    ("学生姓名", "student_name"),
    ("学校编号", "student_code"),
    ("题号", "question_no"),
    ("题目内容", "question_text"),
    ("单元", "unit"),
    ("标准解法", "standard_solution"),
    ("学生步骤", "student_steps"),
    ("来源批次", "source"),
    ("作答日期", "occurred_on"),
    ("缺测", "missing"),
    ("缺测原因", "missing_reason"),
)

# 也接受英文表头，方便老师从别的表格导出后改名。
ALIASES = {
    "record_key": "record_key",
    "student_name": "student_name",
    "student_code": "student_code",
    "question_no": "question_no",
    "question_text": "question_text",
    "unit": "unit",
    "standard_solution": "standard_solution",
    "student_steps": "student_steps",
    "source": "source",
    "occurred_on": "occurred_on",
    "missing": "missing",
    "missing_reason": "missing_reason",
}

REQUIRED = ("record_key", "student_name", "question_no", "question_text", "unit",
            "standard_solution", "occurred_on")

LABELS = {key: label for label, key in HEADERS}

TRUE_WORDS = ("是", "1", "true", "yes", "y", "缺测", "缺考")
FALSE_WORDS = ("否", "0", "false", "no", "n", "正常")

MAX_FIELD = 4000


class AnswerImportError(Exception):
    """整份文件不能导入（编码、表头、行数等）。"""


def decode(data):
    """把上传的字节解码成文本：优先 UTF-8（含 BOM），失败再按 GB18030。"""
    if not isinstance(data, (bytes, bytearray)):
        raise AnswerImportError("没有读到文件内容")
    if len(data) > MAX_TEXT_BYTES:
        raise AnswerImportError("文件太大（超过 2 MB），请只放一个批次的作答")
    raw = bytes(data)
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        raise AnswerImportError("文件编码无法识别，请用 UTF-8 或 GBK 保存后重试")


def parse(text, batch=""):
    """解析 CSV，返回 (rows, header_error)。

    每行都带 line（文件里的行号，从 2 开始，含表头一行）和 errors（这一行的全部问题）。
    表头缺列时 header_error 非空，rows 为空。
    """
    if not isinstance(text, str):
        raise AnswerImportError("没有读到文件内容")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise AnswerImportError("文件是空的")
    except csv.Error as exc:
        raise AnswerImportError("CSV 格式无法解析（%s）" % exc)

    columns = {}
    for index, name in enumerate(header):
        key = None
        cleaned = (name or "").strip().lstrip("\ufeff")
        for label, field in HEADERS:
            if cleaned == label:
                key = field
                break
        if key is None:
            key = ALIASES.get(cleaned.lower())
        if key and key not in columns:
            columns[key] = index

    missing = [LABELS[key] for key in REQUIRED if key not in columns]
    if missing:
        return [], "缺少必需的列：%s。请用软件提供的模板填写。" % "、".join(missing)

    rows = []
    seen = {}
    for line, cells in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in cells):
            continue
        if len(rows) >= MAX_ROWS:
            return [], "一个文件最多导入 %d 行，请拆成多个批次。" % MAX_ROWS
        row = {"line": line, "errors": []}
        for key in ALIASES:
            index = columns.get(key)
            value = cells[index] if index is not None and index < len(cells) else ""
            row[key] = (value or "").strip()
        _check_row(row)
        key = row["record_key"]
        if key:
            if key in seen:
                row["errors"].append("记录编号与第 %d 行重复" % seen[key])
            else:
                seen[key] = line
        if batch and not row["source"]:
            row["source"] = batch
        rows.append(row)
    if not rows:
        raise AnswerImportError("文件里没有作答数据行")
    return rows, ""


def _check_row(row):
    for key in REQUIRED:
        if not row[key]:
            row["errors"].append("缺少%s" % LABELS[key])
    for key in ("record_key", "student_name", "student_code", "question_no", "unit"):
        if len(row[key]) > 40:
            row["errors"].append("%s太长（最多 40 个字）" % LABELS[key])
    if len(row["question_text"]) > MAX_FIELD:
        row["errors"].append("题目内容太长（最多 %d 个字）" % MAX_FIELD)
    if len(row["standard_solution"]) > MAX_FIELD:
        row["errors"].append("标准解法太长（最多 %d 个字）" % MAX_FIELD)
    if len(row["student_steps"]) > MAX_FIELD:
        row["errors"].append("学生步骤太长（最多 %d 个字）" % MAX_FIELD)

    if row["occurred_on"]:
        try:
            if not DATE_RE.match(row["occurred_on"]):
                raise ValueError
            datetime.strptime(row["occurred_on"], "%Y-%m-%d")
        except ValueError:
            row["errors"].append("作答日期要写成 2026-09-01 这样的格式")

    mark = row["missing"].lower()
    if mark in ("",):
        row["missing"] = 1 if not row["student_steps"] else 0
    elif mark in TRUE_WORDS:
        row["missing"] = 1
    elif mark in FALSE_WORDS:
        row["missing"] = 0
    else:
        row["missing"] = 0
        row["errors"].append("缺测只能填「是」或「否」")

    if row["missing"] and row["student_steps"]:
        row["errors"].append("标成缺测，但又填写了学生步骤；缺测不能有步骤")
    if row["missing"] and not row["missing_reason"]:
        row["missing_reason"] = "未提供学生步骤"
    if not row["missing"] and not row["student_steps"]:
        row["missing"] = 1
        row["missing_reason"] = row["missing_reason"] or "未提供学生步骤"


def fingerprint(row, student_id):
    """内容指纹：只放会改变这条作答含义的字段；批次、来源、原姓名/编号不算。"""
    payload = {
        "student_id": int(student_id),
        "question_no": row["question_no"],
        "question_text": row["question_text"],
        "unit": row["unit"],
        "standard_solution": row["standard_solution"],
        "student_steps": row["student_steps"],
        "occurred_on": row["occurred_on"],
        "missing": int(row["missing"]),
        "missing_reason": row["missing_reason"],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _class_students(db, class_id):
    return db.query(
        "SELECT id, name, code, active FROM students WHERE class_id=? ORDER BY id", (class_id,)
    )


def _match(row, students):
    """返回 (student 或 None, 候选列表, 问题说明)。同名学生绝不自动挑一个。"""
    by_code = {}
    by_name = {}
    for student in students:
        code = (student.get("code") or "").strip()
        if code:
            by_code.setdefault(code, []).append(student)
        by_name.setdefault(student["name"], []).append(student)

    if row["student_code"]:
        matches = by_code.get(row["student_code"], [])
        if not matches:
            return None, [], "班级里没有学校编号为「%s」的学生；清空这一列可以按姓名匹配" % row["student_code"]
        if len(matches) > 1:
            return None, matches, "学校编号「%s」对应多个学生，请老师选择" % row["student_code"]
        student = matches[0]
        if row["student_name"] and student["name"] != row["student_name"]:
            return None, [], "学校编号「%s」属于「%s」，与文件里的姓名「%s」不一致" % (
                row["student_code"], student["name"], row["student_name"])
        return _check_active(student, matches)

    matches = by_name.get(row["student_name"], [])
    if not matches:
        return None, [], "班级里没有叫「%s」的学生" % row["student_name"]
    if len(matches) > 1:
        return None, matches, "同班有 %d 个「%s」，请老师选择是哪一个" % (len(matches), row["student_name"])
    return _check_active(matches[0], matches)


def _check_active(student, candidates):
    if not int(student.get("active", 1) or 0):
        return None, candidates, "「%s」已停用，请先在学生名单里启用" % student["name"]
    return student, candidates, ""


def preview(db, class_id, text, batch=""):
    """逐行给出结果，不改数据库。

    每行 status：ok（可导入）、duplicate（同编号同内容，跳过）、
    conflict（同编号内容不同，需老师确认覆盖）、error（这一行不能导入）。
    """
    rows, header_error = parse(text, batch)
    if header_error:
        raise AnswerImportError(header_error)
    students = _class_students(db, class_id)
    existing = {
        row["record_key"]: row
        for row in db.query(
            "SELECT record_key, student_id, fingerprint FROM answers WHERE class_id=?", (class_id,)
        )
    }
    result = []
    for row in rows:
        item = dict(row)
        item["status"] = "error"
        item["reason"] = ""
        item["student_id"] = None
        item["student_name_matched"] = ""
        item["candidates"] = []
        item["existing"] = None

        student, candidates, problem = _match(row, students)
        if candidates and not student:
            item["candidates"] = [
                {"id": s["id"], "name": s["name"], "code": s.get("code") or "",
                 "active": int(s.get("active", 1) or 0)}
                for s in candidates
            ]
        if problem:
            item["reason"] = problem
        elif row["errors"]:
            item["reason"] = "；".join(row["errors"])
        else:
            item["student_id"] = student["id"]
            item["student_name_matched"] = student["name"]
            old = existing.get(row["record_key"])
            if not old:
                item["status"] = "ok"
            elif old["fingerprint"] == fingerprint(row, student["id"]):
                item["status"] = "duplicate"
                item["reason"] = "这条记录已经导入过，内容相同，不会重复计数"
            else:
                item["status"] = "conflict"
                item["reason"] = "记录编号已存在但内容不同，需要老师确认是否覆盖"
                item["existing"] = {
                    "student_id": old["student_id"],
                    "fingerprint": old["fingerprint"],
                }
        if row["errors"] and item["status"] == "ok":
            item["status"] = "error"
        result.append(item)
    return {"batch": batch, "total": len(result), "rows": result, "counts": _counts(result)}


def _counts(rows):
    counts = {"ok": 0, "duplicate": 0, "conflict": 0, "error": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def apply_selections(rows, students, selections):
    """把老师在预览里给同名行选的学生套进去，再重新判断这一行的状态。"""
    by_id = {int(s["id"]): s for s in students}
    for row in rows:
        chosen = selections.get(str(row["line"]))
        if not chosen:
            continue
        student = by_id.get(int(chosen))
        if not student:
            continue
        row["student_id"] = student["id"]
        row["student_name_matched"] = student["name"]
        row["candidates"] = []
        if not int(student.get("active", 1) or 0):
            row["status"] = "error"
            row["reason"] = "「%s」已停用，请先在学生名单里启用" % student["name"]
        elif row["errors"]:
            row["status"] = "error"
            row["reason"] = "；".join(row["errors"])
        elif row["status"] in ("error",):
            # 只是「同名学生要选」这类问题，选好之后按新记录处理；
            # 编号已存在的情况在下面统一重判。
            row["status"] = "ok"
            row["reason"] = ""
    return rows


def commit(db, class_id, text, batch="", selections=None, overwrite=False):
    """在一个事务里写入。

    只写 ok 的行；duplicate 跳过；conflict 只有 overwrite=True 才替换。
    其他行保留为失败明细，不阻断整批（老师在预览里已经确认过）。
    """
    preview_data = preview(db, class_id, text, batch)
    rows = preview_data["rows"]
    students = _class_students(db, class_id)
    apply_selections(rows, students, selections or {})

    # 选好学生之后重新核对「编号已存在」的两种情况，避免把冲突当成新记录插入。
    existing = {
        row["record_key"]: row
        for row in db.query(
            "SELECT record_key, student_id, fingerprint FROM answers WHERE class_id=?", (class_id,)
        )
    }
    for row in rows:
        if row["status"] not in ("ok", "error") or not row["student_id"]:
            continue
        old = existing.get(row["record_key"])
        if not old:
            continue
        if old["fingerprint"] == fingerprint(row, row["student_id"]):
            row["status"] = "duplicate"
            row["reason"] = "这条记录已经导入过，内容相同，不会重复计数"
        else:
            row["status"] = "conflict"
            row["reason"] = "记录编号已存在但内容不同，需要老师确认是否覆盖"

    now = _now()
    inserted = updated = 0
    with db.tx() as conn:
        for row in rows:
            if row["status"] == "ok":
                conn.execute(
                    "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                    " student_code, question_no, question_text, unit, standard_solution,"
                    " student_steps, source, occurred_on, missing, missing_reason, fingerprint,"
                    " imported_at, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        class_id, row["student_id"], row["record_key"], batch,
                        row["student_name"], row["student_code"], row["question_no"],
                        row["question_text"], row["unit"], row["standard_solution"],
                        row["student_steps"], row["source"], row["occurred_on"],
                        int(row["missing"]), row["missing_reason"],
                        fingerprint(row, row["student_id"]), now, now, now,
                    ),
                )
                inserted += 1
            elif row["status"] == "conflict" and overwrite:
                conn.execute(
                    "UPDATE answers SET student_id=?, batch=?, student_name=?, student_code=?,"
                    " question_no=?, question_text=?, unit=?, standard_solution=?, student_steps=?,"
                    " source=?, occurred_on=?, missing=?, missing_reason=?, fingerprint=?,"
                    " imported_at=?, updated_at=? WHERE class_id=? AND record_key=?",
                    (
                        row["student_id"], batch, row["student_name"], row["student_code"],
                        row["question_no"], row["question_text"], row["unit"],
                        row["standard_solution"], row["student_steps"], row["source"],
                        row["occurred_on"], int(row["missing"]), row["missing_reason"],
                        fingerprint(row, row["student_id"]), now, now, class_id, row["record_key"],
                    ),
                )
                row["status"] = "overwritten"
                updated += 1

    counts = {"new": inserted, "overwritten": updated, "duplicate": 0, "conflict": 0, "failed": 0}
    for row in rows:
        if row["status"] == "duplicate":
            counts["duplicate"] += 1
        elif row["status"] == "conflict":
            counts["conflict"] += 1
        elif row["status"] == "error":
            counts["failed"] += 1
    return {
        "batch": batch,
        "total": len(rows),
        "counts": counts,
        "rows": rows,
        "errors": [row for row in rows if row["status"] in ("error", "conflict")],
    }


def listing(db, class_id):
    """按批次汇总已导入的作答，用于导入后的查看。"""
    rows = db.query(
        "SELECT batch, COUNT(*) AS total, SUM(missing) AS missing,"
        " MIN(imported_at) AS imported_at, MAX(imported_at) AS last_at"
        " FROM answers WHERE class_id=? GROUP BY batch ORDER BY last_at DESC, batch",
        (class_id,),
    )
    total = db.scalar("SELECT COUNT(*) FROM answers WHERE class_id=?", (class_id,)) or 0
    return {
        "total": total,
        "batches": [
            {
                "batch": row["batch"] or "未命名批次",
                "total": row["total"],
                "missing": row["missing"] or 0,
                "imported_at": row["imported_at"],
                "last_at": row["last_at"],
            }
            for row in rows
        ],
    }


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def template_text():
    """老师用的模板：表头 + 两行示例（虚构数据）。"""
    header = ",".join(label for label, _ in HEADERS)
    sample = [
        "A-001,示例学生甲,XY001,1,解方程 3(x-2)=2x+5,一元一次方程,3x-6=2x+5；3x-2x=5+6；x=11,"
        "3x-6=2x+5；x=11,9 月第 1 次作业,2026-09-02,否,",
        "A-002,示例学生乙,,1,解方程 3(x-2)=2x+5,一元一次方程,3x-6=2x+5；3x-2x=5+6；x=11,,"
        "9 月第 1 次作业,2026-09-02,是,没交作业",
    ]
    return header + "\r\n" + "\r\n".join(sample) + "\r\n"
