"""错题记录的写入与「AI 建议 vs 老师修改」比对。"""

import hashlib
import json

from .db import now_iso

# 这些字段变了就要重新核对：题目内容、分析结论，以及记录归谁、有没有照片。
# 日期、来源、备注不在此列，改了不影响已经核对过的内容。
DECISIVE_FIELDS = (
    "student_id",
    "image_file",
    "question_text",
    "solution_text",
    "knowledge_points",
    "error_type",
    "observed_errors",
    "possible_causes",
    "advice_text",
    "insufficient",
    "insufficient_reason",
    "ai_likely_correct",
)

# 冲突检测比对的范围：足以覆盖任何一次写入造成的内容变化。
FINGERPRINT_FIELDS = DECISIVE_FIELDS + (
    "id",
    "source_type",
    "source_detail",
    "note",
    "teacher_note",
    "occurred_on",
    "status",
    "ai_snapshot",
    "ai_mode",
    "ai_error",
    "ai_error_code",
    "teacher_edited_fields",
    "confirmed_at",
    "confirmed_by",
    "returned_at",
    "returned_reason",
    "updated_at",
)


def record_fingerprint(row):
    """整行指纹：任何字段变了指纹就变，所以同一秒内的两次修改也能检出。"""
    payload = {}
    for field in FINGERPRINT_FIELDS:
        try:
            payload[field] = row[field]
        except (KeyError, IndexError):
            payload[field] = None
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# AI 产出字段 → 数据库列名；student_id 单独处理
AI_FIELDS = {
    "student_id": "student_id",
    "question_text": "question_text",
    "solution_text": "solution_text",
    "knowledge_points": "knowledge_points",
    "error_type": "error_type",
    "observed_errors": "observed_errors",
    "possible_causes": "possible_causes",
    "advice_text": "advice_text",
}

LIST_FIELDS = ("knowledge_points", "observed_errors", "possible_causes")


def _as_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            value = []
    return list(value) if isinstance(value, list) else []


def edited_fields(ai, row):
    """返回老师相对 AI 建议改动过的字段名列表。"""
    if not isinstance(ai, dict):
        return []
    edited = []
    for field in AI_FIELDS:
        if field == "student_id":
            if ai.get("student_id") is not None and int(ai["student_id"]) != int(row["student_id"]):
                edited.append("student_id")
            continue
        if ai.get(field) is None:
            continue
        if field in LIST_FIELDS:
            if _as_list(ai[field]) != _as_list(row[field]):
                edited.append(field)
        elif (ai.get(field) or "") != (row[field] or ""):
            edited.append(field)
    return edited


def insert_mistake(db, class_id, student_id, image_file, mime, width, height, meta, result):
    """写入一条待核对错题，返回 id。analysis 结果、失败原因与 AI 快照同时落库。"""
    snapshot = dict(result)
    snapshot["student_id"] = student_id
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO mistakes(class_id, student_id, status, source_type, source_detail, note, occurred_on,"
            " image_file, image_mime, image_width, image_height, question_text, solution_text, knowledge_points,"
            " error_type, observed_errors, possible_causes, advice_text, insufficient, insufficient_reason,"
            " ai_snapshot, ai_mode, ai_error, ai_error_code, ai_likely_correct, teacher_edited_fields,"
            " created_at, updated_at)"
            " VALUES (?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',?,?)",
            (
                class_id,
                student_id,
                meta.get("source_type") or "作业",
                meta.get("source_detail") or "",
                meta.get("note") or "",
                meta.get("occurred_on") or "",
                image_file,
                mime,
                width,
                height,
                result.get("question_text") or "",
                result.get("solution_text") or "",
                json.dumps(result.get("knowledge_points") or [], ensure_ascii=False),
                result.get("error_type") or "",
                json.dumps(result.get("observed_errors") or [], ensure_ascii=False),
                json.dumps(result.get("possible_causes") or [], ensure_ascii=False),
                result.get("advice_text") or "",
                1 if result.get("insufficient") else 0,
                result.get("insufficient_reason") or "",
                json.dumps(snapshot, ensure_ascii=False),
                result.get("mode") or "mock",
                result.get("ai_error") or "",
                result.get("ai_error_code") or "",
                1 if result.get("likely_correct") else 0,
                now_iso(),
                now_iso(),
            ),
        )
        return cur.lastrowid
