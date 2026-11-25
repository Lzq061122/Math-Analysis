"""HTTP API：请求分发、参数校验、业务逻辑。

约定：
- 所有 /api/* 接口都要求登录会话（登录与注册接口除外），跨账号访问一律 404，避免泄露存在性。
- 写操作要求 Content-Type: application/json，配合 SameSite=Lax 会话 Cookie 抵御跨站提交。
- 错误统一返回 {"error": "..."} + 4xx 状态码。
"""

import base64
import binascii
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import date, datetime

from . import advice as advice_mod
from . import answer_check as answer_check_mod
from . import answer_review as review_mod
from . import answers as answers_mod
from . import tutor as tutor_mod
from . import followup as followup_mod
from . import auth, backup, vision
from .analysis import ERROR_TYPES, KNOWLEDGE_POINTS, get_provider
from .db import now_iso
from .preview import PreviewError
from .records import DECISIVE_FIELDS, edited_fields, insert_mistake, record_fingerprint
from .stats import answer_check_stats, build_stats, options_for_filters
from .version import VERSION

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _fault(name):
    """测试用故障注入；老师正常使用时 MISTAKE_FAULT 不存在，不会触发。"""
    if name in (os.environ.get("MISTAKE_FAULT") or "").split(","):
        raise RuntimeError("模拟故障：%s" % name)


EDITABLE_FIELDS = (
    "student_id",
    "question_text",
    "solution_text",
    "knowledge_points",
    "error_type",
    "observed_errors",
    "possible_causes",
    "advice_text",
    "source_type",
    "source_detail",
    "note",
    "occurred_on",
    "teacher_note",
)


class ApiError(Exception):
    def __init__(self, message, status=400, code=None, payload=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.payload = payload or {}


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------
def _json_list(raw):
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _json_obj(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def serialize_student(row):
    keys = row.keys()
    return {
        "id": row["id"],
        "class_id": row["class_id"],
        "name": row["name"],
        "note": row["note"],
        "code": row["code"] if "code" in keys else "",
        "active": int(row["active"]) if "active" in keys else 1,
    }


def serialize_class(db, row, with_counts=True):
    data = {
        "id": row["id"],
        "name": row["name"],
        "grade": row["grade"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_counts:
        data["student_count"] = db.scalar("SELECT COUNT(*) FROM students WHERE class_id=?", (row["id"],)) or 0
        data["confirmed"] = db.scalar(
            "SELECT COUNT(*) FROM mistakes WHERE class_id=? AND status='confirmed'", (row["id"],)
        ) or 0
        data["pending"] = db.scalar(
            "SELECT COUNT(*) FROM mistakes WHERE class_id=? AND status='pending'", (row["id"],)
        ) or 0
        data["returned"] = db.scalar(
            "SELECT COUNT(*) FROM mistakes WHERE class_id=? AND status='returned'", (row["id"],)
        ) or 0
    return data


def serialize_mistake(row, brief=False):
    data = {
        "id": row["id"],
        "class_id": row["class_id"],
        "student_id": row["student_id"],
        "student_name": row.get("student_name", ""),
        "student_note": row.get("student_note", ""),
        "status": row["status"],
        "source_type": row["source_type"],
        "source_detail": row["source_detail"],
        "note": row["note"],
        "teacher_note": row["teacher_note"],
        "occurred_on": row["occurred_on"],
        "has_image": bool(row["image_file"]),
        "image_width": row["image_width"],
        "image_height": row["image_height"],
        "ai_mode": row["ai_mode"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "confirmed_at": row["confirmed_at"],
        "confirmed_by": row.get("confirmed_by"),
        "returned_at": row["returned_at"],
        "returned_reason": row["returned_reason"],
    }
    if brief:
        data["question_text"] = (row["question_text"] or "")[:120]
        data["knowledge_points"] = _json_list(row["knowledge_points"])
        data["error_type"] = row["error_type"]
        data["insufficient"] = bool(row["insufficient"])
        return data
    ai = _json_obj(row["ai_snapshot"])
    data.update(
        {
            "question_text": row["question_text"],
            "solution_text": row["solution_text"],
            "knowledge_points": _json_list(row["knowledge_points"]),
            "error_type": row["error_type"],
            "observed_errors": _json_list(row["observed_errors"]),
            "possible_causes": _json_list(row["possible_causes"]),
            "advice_text": row["advice_text"],
            "insufficient": bool(row["insufficient"]),
            "insufficient_reason": row["insufficient_reason"],
            "ai_error": row.get("ai_error", ""),
            "ai_error_code": row.get("ai_error_code", ""),
            "likely_correct": bool(row.get("ai_likely_correct")),
            "ai": ai,
            "teacher_edited_fields": _json_list(row["teacher_edited_fields"]),
        }
    )
    return data


# --------------------------------------------------------------------------
# 权限辅助
# --------------------------------------------------------------------------
def owned_class(db, user, class_id):
    row = db.query_one("SELECT * FROM classes WHERE id=? AND user_id=?", (class_id, user["id"]))
    if not row:
        raise ApiError("班级不存在", 404)
    return row


def owned_student(db, user, student_id):
    row = db.query_one(
        "SELECT s.*, c.user_id FROM students s JOIN classes c ON c.id = s.class_id WHERE s.id=?", (student_id,)
    )
    if not row or row["user_id"] != user["id"]:
        raise ApiError("学生不存在", 404)
    return row


def owned_mistake(db, user, mistake_id):
    row = db.query_one(
        "SELECT m.*, s.name AS student_name, s.note AS student_note, c.user_id FROM mistakes m"
        " JOIN students s ON s.id = m.student_id JOIN classes c ON c.id = m.class_id WHERE m.id=?",
        (mistake_id,),
    )
    if not row or row["user_id"] != user["id"]:
        raise ApiError("错题不存在", 404)
    return row


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
def api_login(ctx):
    username = (ctx.body.get("username") or "").strip()
    password = ctx.body.get("password") or ""
    user_id, message = auth.check_login(ctx.db, username, password, ctx.client_ip)
    if not user_id:
        raise ApiError(message, 401)
    token = auth.create_session(ctx.db, user_id)
    user = ctx.db.query_one("SELECT id, username, display_name FROM users WHERE id=?", (user_id,))
    return {"token": token, "user": user}


def api_register(ctx):
    username = (ctx.body.get("username") or "").strip()
    password = ctx.body.get("password") or ""
    if not username:
        raise ApiError("用户名不能为空", 400)
    if len(username) > 30:
        raise ApiError("用户名最多 30 个字符", 400)
    if len(password) < 8:
        raise ApiError("密码至少 8 位", 400)
    if ctx.db.query_one("SELECT id FROM users WHERE username=?", (username,)):
        raise ApiError("这个用户名已被使用，请换一个", 409)
    try:
        user_id = auth.create_user(ctx.db, username, password, username)
    except sqlite3.IntegrityError:
        raise ApiError("这个用户名已被使用，请换一个", 409)
    token = auth.create_session(ctx.db, user_id)
    user = ctx.db.query_one("SELECT id, username, display_name FROM users WHERE id=?", (user_id,))
    return {"token": token, "user": user}


def api_me(ctx):
    return {"user": ctx.user, "ai_mode": vision.describe_config()["mode"]}


def api_logout(ctx):
    auth.destroy_session(ctx.db, ctx.token)
    return {"ok": True}


def api_change_password(ctx):
    old = ctx.body.get("old_password") or ""
    new = ctx.body.get("new_password") or ""
    user = ctx.db.query_one("SELECT * FROM users WHERE id=?", (ctx.user["id"],))
    if not auth.verify_password(old, user["pwd_salt"], user["pwd_hash"], user["iterations"]):
        raise ApiError("原密码不正确", 400)
    if len(new) < 8:
        raise ApiError("新密码至少 8 位", 400)
    auth.set_password(ctx.db, user["id"], new)
    token = auth.create_session(ctx.db, user["id"])
    return {"ok": True, "token": token}


# --------------------------------------------------------------------------
# 班级与学生
# --------------------------------------------------------------------------
def api_list_classes(ctx):
    rows = ctx.db.query("SELECT * FROM classes WHERE user_id=? ORDER BY created_at DESC", (ctx.user["id"],))
    return {"items": [serialize_class(ctx.db, r) for r in rows]}


def api_create_class(ctx):
    name = (ctx.body.get("name") or "").strip()
    if not name:
        raise ApiError("班级名称不能为空")
    with ctx.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO classes(user_id, name, grade, note, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (
                ctx.user["id"],
                name,
                (ctx.body.get("grade") or "").strip(),
                (ctx.body.get("note") or "").strip(),
                now_iso(),
                now_iso(),
            ),
        )
        class_id = cur.lastrowid
    row = ctx.db.query_one("SELECT * FROM classes WHERE id=?", (class_id,))
    return serialize_class(ctx.db, row)


def api_get_class(ctx):
    row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    students = ctx.db.query("SELECT * FROM students WHERE class_id=? ORDER BY id", (row["id"],))
    data = serialize_class(ctx.db, row)
    data["students"] = [serialize_student(s) for s in students]
    # 班级页顺带给出作答核对统计块（A3 口径）；老库缺表时该函数自行退化为零值。
    data["answer_check"] = answer_check_stats(ctx.db, row)
    return data


def api_update_class(ctx):
    row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    name = (ctx.body.get("name") or row["name"]).strip()
    if not name:
        raise ApiError("班级名称不能为空")
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE classes SET name=?, grade=?, note=?, updated_at=? WHERE id=?",
            (
                name,
                (ctx.body.get("grade") if "grade" in ctx.body else row["grade"]) or "",
                (ctx.body.get("note") if "note" in ctx.body else row["note"]) or "",
                now_iso(),
                row["id"],
            ),
        )
    return serialize_class(ctx.db, ctx.db.query_one("SELECT * FROM classes WHERE id=?", (row["id"],)))


def api_delete_class(ctx):
    row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    with ctx.db.tx() as conn:
        files = [
            r[0]
            for r in conn.execute("SELECT image_file FROM mistakes WHERE class_id=? AND image_file<>''", (row["id"],))
        ]
        conn.execute("DELETE FROM mistakes WHERE class_id=?", (row["id"],))
        conn.execute("DELETE FROM advice WHERE class_id=?", (row["id"],))
        conn.execute("DELETE FROM students WHERE class_id=?", (row["id"],))
        conn.execute("DELETE FROM classes WHERE id=?", (row["id"],))
    removed = ctx.uploads.remove(files)
    return {"ok": True, "removed_files": removed}


def api_purge_class(ctx):
    """清空班级数据：学生名单、错题、照片、建议。班级本身保留。"""
    row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    confirm_name = (ctx.body.get("confirm_name") or "").strip()
    if confirm_name != row["name"]:
        raise ApiError("请输入完整的班级名称以确认清空")
    with ctx.db.tx() as conn:
        files = [
            r[0]
            for r in conn.execute("SELECT image_file FROM mistakes WHERE class_id=? AND image_file<>''", (row["id"],))
        ]
        mistakes = conn.execute("SELECT COUNT(*) FROM mistakes WHERE class_id=?", (row["id"],)).fetchone()[0]
        students = conn.execute("SELECT COUNT(*) FROM students WHERE class_id=?", (row["id"],)).fetchone()[0]
        conn.execute("DELETE FROM mistakes WHERE class_id=?", (row["id"],))
        conn.execute("DELETE FROM advice WHERE class_id=?", (row["id"],))
        conn.execute("DELETE FROM students WHERE class_id=?", (row["id"],))
        conn.execute("UPDATE classes SET updated_at=? WHERE id=?", (now_iso(), row["id"]))
    removed = ctx.uploads.remove(files)
    return {"ok": True, "deleted_mistakes": mistakes, "deleted_students": students, "deleted_files": removed}


def api_create_student(ctx):
    row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    name = (ctx.body.get("name") or "").strip()
    if not name:
        raise ApiError("学生姓名不能为空")
    if len(name) > 20:
        raise ApiError("姓名过长（最多 20 个字）")
    code = _student_code(ctx, row["id"], ctx.body.get("code"))
    with ctx.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO students(class_id, name, note, code, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (row["id"], name, (ctx.body.get("note") or "").strip(), code, now_iso(), now_iso()),
        )
        student_id = cur.lastrowid
    return serialize_student(ctx.db.query_one("SELECT * FROM students WHERE id=?", (student_id,)))


def _student_code(ctx, class_id, value, student_id=None):
    """学校随机编号：可空；同班内唯一，用来在导入时免姓名匹配。"""
    code = (value or "").strip()
    if not code:
        return ""
    if len(code) > 20:
        raise ApiError("学校编号过长（最多 20 个字符）")
    busy = ctx.db.query_one(
        "SELECT id FROM students WHERE class_id=? AND code=?" + (" AND id<>?" if student_id else ""),
        (class_id, code) + ((student_id,) if student_id else ()),
    )
    if busy:
        raise ApiError("这个学校编号已经被同班另一个学生使用")
    return code


def api_update_student(ctx):
    row = owned_student(ctx.db, ctx.user, ctx.params["id"])
    name = (ctx.body.get("name") or row["name"]).strip()
    if not name:
        raise ApiError("学生姓名不能为空")
    if len(name) > 20:
        raise ApiError("姓名过长（最多 20 个字）")
    # 停用/启用：不改 id、不删数据，只改状态位，以后关联的错题仍然对得上。
    active = int(row.get("active", 1) or 0)
    if "active" in ctx.body:
        active = 1 if ctx.body.get("active") else 0
    if "code" in ctx.body:
        code = _student_code(ctx, row["class_id"], ctx.body.get("code"), row["id"])
    else:
        code = row.get("code") or ""
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE students SET name=?, note=?, code=?, active=?, updated_at=? WHERE id=?",
            (name, (ctx.body.get("note") if "note" in ctx.body else row["note"]) or "", code, active, now_iso(), row["id"]),
        )
    return serialize_student(ctx.db.query_one("SELECT * FROM students WHERE id=?", (row["id"],)))


def api_delete_student(ctx):
    row = owned_student(ctx.db, ctx.user, ctx.params["id"])
    count = ctx.db.scalar("SELECT COUNT(*) FROM mistakes WHERE student_id=?", (row["id"],))
    with ctx.db.tx() as conn:
        files = [
            r[0]
            for r in conn.execute("SELECT image_file FROM mistakes WHERE student_id=? AND image_file<>''", (row["id"],))
        ]
        conn.execute("DELETE FROM mistakes WHERE student_id=?", (row["id"],))
        conn.execute("DELETE FROM advice WHERE student_id=?", (row["id"],))
        conn.execute("DELETE FROM students WHERE id=?", (row["id"],))
    ctx.uploads.remove(files)
    return {"ok": True, "deleted_mistakes": count}


# --------------------------------------------------------------------------
# 错题
# --------------------------------------------------------------------------
def _decode_image(raw):
    if not isinstance(raw, dict):
        raise ApiError("缺少图片")
    data_url = raw.get("data_url") or ""
    match = re.match(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", data_url, re.S)
    if not match:
        raise ApiError("图片格式不支持，仅接受 JPG / PNG / WebP")
    mime = match.group(1)
    try:
        blob = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("图片数据损坏")
    if len(blob) > MAX_IMAGE_BYTES:
        raise ApiError("图片超过 10MB，请压缩后重试")
    if len(blob) < 1024:
        raise ApiError("图片过小，请重新拍摄")
    return blob, mime, ALLOWED_MIME[mime]


def _analysis_provider(ctx=None):
    """优先使用当前会话按账号配置的 provider；网页端仍走环境变量配置。"""
    provider = getattr(ctx, "ai_provider", None) if ctx is not None else None
    return provider if provider is not None else get_provider()


def _run_analysis(job, ctx=None):
    provider = _analysis_provider(ctx)
    return provider.analyze(job)


def _failure_result(job, mode, exc):
    """分析失败时落库的空结果：保留录入文字、照片与失败原因，供老师重试或手填。"""
    return {
        "mode": mode,
        "question_text": job.get("question_text") or "",
        "solution_text": job.get("solution_text") or "",
        "knowledge_points": [],
        "error_type": "",
        "observed_errors": [],
        "possible_causes": [],
        "advice_text": "",
        "insufficient": False,
        "insufficient_reason": "",
        "likely_correct": False,
        "ai_error": exc.message,
        "ai_error_code": exc.code,
        "basis": "分析失败，未生成 AI 内容。",
        "analyzed_at": now_iso(),
    }


def _require_consent(ctx, provider):
    """真实模式必须先得到老师对发送范围的明确同意，服务端再次校验。"""
    if provider.mode in vision.REMOTE_MODES and not ctx.body.get("consent"):
        service = getattr(provider, "service", "") or vision.service_name(provider.mode)
        raise ApiError("需要先勾选同意，才能把照片与文字发送给%s" % service, 400)


def _local_result(job):
    """桌面版手动录入的结果：不调用 AI，分析类字段留空，由老师自己填。"""
    return {
        "mode": "local",
        "question_text": job.get("question_text") or "",
        "solution_text": job.get("solution_text") or "",
        "knowledge_points": [],
        "error_type": "",
        "observed_errors": job.get("observed_errors") or [],
        "possible_causes": [],
        "advice_text": "",
        "insufficient": False,
        "insufficient_reason": "",
        "likely_correct": False,
        "basis": "桌面版手动录入，未调用 AI 分析。",
        "analyzed_at": now_iso(),
    }


def api_create_mistake(ctx):
    body = ctx.body
    class_row = owned_class(ctx.db, ctx.user, body.get("class_id"))
    student = owned_student(ctx.db, ctx.user, body.get("student_id"))
    if student["class_id"] != class_row["id"]:
        raise ApiError("学生不属于该班级")
    blob, mime, ext = _decode_image(body.get("image"))
    width = int((body.get("image") or {}).get("width") or 0)
    height = int((body.get("image") or {}).get("height") or 0)

    provider = None
    if not body.get("local_only"):
        provider = _analysis_provider(ctx)
        _require_consent(ctx, provider)

    image_file = "%s.%s" % (uuid.uuid4().hex, ext)
    ctx.uploads.save(image_file, blob)

    job = {
        "question_text": (body.get("question_text") or "").strip(),
        "solution_text": (body.get("solution_text") or "").strip(),
        "observed_errors": [
            str(item).strip() for item in (body.get("observed_errors") or []) if str(item).strip()
        ],
        "image_file": image_file,
        "image_bytes": blob,
        "image_mime": mime,
        "image_width": width,
        "image_height": height,
        "blurry": bool(body.get("blurry")),
    }
    if provider is None:
        result = _local_result(job)
    else:
        try:
            result = provider.analyze(job)
        except vision.AnalysisError as exc:
            result = _failure_result(job, provider.mode, exc)

    occurred = (body.get("occurred_on") or "").strip() or date.today().isoformat()
    try:
        _fault("mistake_insert")
        mistake_id = insert_mistake(
            ctx.db,
            class_row["id"],
            student["id"],
            image_file,
            mime,
            width,
            height,
            {
                "source_type": (body.get("source_type") or "作业").strip(),
                "source_detail": (body.get("source_detail") or "").strip(),
                "note": (body.get("note") or "").strip(),
                "occurred_on": occurred,
            },
            result,
        )
    except Exception:
        ctx.uploads.remove([image_file])
        raise
    teacher_note = (body.get("teacher_note") or "").strip()
    if teacher_note:
        with ctx.db.tx() as conn:
            conn.execute("UPDATE mistakes SET teacher_note=? WHERE id=?", (teacher_note, mistake_id))
    row = ctx.db.query_one(
        "SELECT m.*, s.name AS student_name, s.note AS student_note FROM mistakes m JOIN students s ON s.id=m.student_id WHERE m.id=?",
        (mistake_id,),
    )
    return serialise_detail(ctx, row)


def serialise_detail(ctx, row):
    data = serialize_mistake(row)
    # 界面拿它做冲突检测：保存、采用、确认都带回打开时的指纹。
    data["base_fingerprint"] = record_fingerprint(row)
    data["student_options"] = [
        serialize_student(s) for s in ctx.db.query("SELECT * FROM students WHERE class_id=? ORDER BY id", (row["class_id"],))
    ]
    data["class_name"] = ctx.db.scalar("SELECT name FROM classes WHERE id=?", (row["class_id"],))
    return data


AI_SEND_FIELDS = ["错题照片", "题目文字", "学生解题过程"]


def describe_ai(ctx=None):
    """桌面版与网页版共用的分析说明：模式、模型、密钥状态与发送范围。"""
    cfg = vision.describe_config(getattr(ctx, "ai_config", None) if ctx is not None else None)
    remote = cfg["mode"] in vision.REMOTE_MODES
    return {
        "ai_mode": cfg["mode"],
        "ai_reason": cfg["reason"],
        "ai_provider": cfg["service"] if remote else "未启用云端分析",
        "ai_model": cfg["model"],
        "ai_base_url": cfg["base_url"],
        "ai_request_url": cfg["request_url"],
        "ai_model_source": cfg["model_source"],
        "ai_base_url_source": cfg["base_url_source"],
        "ai_key_source": cfg["key_source"],
        "ai_key_configured": cfg["key_configured"],
        "ai_price_note": vision.price_note(cfg["mode"]),
        "ai_policy_summary": vision.policy_summary(cfg["mode"]),
        "ai_policy_url": vision.POLICY_URL,
        "ai_send_fields": list(AI_SEND_FIELDS),
    }


def _fresh_detail(ctx, mistake_id):
    row = ctx.db.query_one(
        "SELECT m.*, s.name AS student_name, s.note AS student_note FROM mistakes m JOIN students s ON s.id=m.student_id WHERE m.id=?",
        (mistake_id,),
    )
    return serialise_detail(ctx, row)


def _analysis_job(ctx, row):
    blob = b""
    if row["image_file"]:
        path = ctx.uploads.path(row["image_file"])
        if os.path.exists(path):
            with open(path, "rb") as fh:
                blob = fh.read()
    return {
        "question_text": row["question_text"],
        "solution_text": row["solution_text"],
        "image_file": row["image_file"],
        "image_bytes": blob,
        "image_mime": row["image_mime"],
        "image_width": row["image_width"],
        "image_height": row["image_height"],
        "blurry": bool(ctx.body.get("blurry")),
    }


def _record_analysis_failure(ctx, mistake_id, message, code):
    """失败只记最近一次失败原因：不改来源、不覆盖内容、不动核对状态。"""
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE mistakes SET ai_error=?, ai_error_code=?, updated_at=? WHERE id=?",
            (message, code, now_iso(), mistake_id),
        )


PREVIOUS_FIELDS = (
    "question_text",
    "solution_text",
    "knowledge_points",
    "error_type",
    "observed_errors",
    "possible_causes",
    "advice_text",
    "insufficient",
    "insufficient_reason",
)


def _previous_content(row):
    """重新分析已确认记录前，把老师确认过的内容留在 AI 快照里可追溯。"""
    data = {}
    for key in PREVIOUS_FIELDS:
        value = row[key]
        if key in ("knowledge_points", "observed_errors", "possible_causes"):
            value = _json_list(value)
        data[key] = value
    data["status"] = row["status"]
    data["confirmed_at"] = row["confirmed_at"]
    data["saved_at"] = now_iso()
    return data


def _analysis_updates(result, student_id, previous=None):
    """一次分析落库的全部内容：正式字段、AI 原始建议、来源、结论、核对状态。"""
    snapshot = dict(result)
    snapshot["student_id"] = student_id
    if previous:
        snapshot["_previous"] = previous
    return {
        "question_text": result["question_text"],
        "solution_text": result["solution_text"],
        "knowledge_points": json.dumps(result["knowledge_points"], ensure_ascii=False),
        "error_type": result["error_type"],
        "observed_errors": json.dumps(result["observed_errors"], ensure_ascii=False),
        "possible_causes": json.dumps(result["possible_causes"], ensure_ascii=False),
        "advice_text": result["advice_text"],
        "insufficient": 1 if result["insufficient"] else 0,
        "insufficient_reason": result["insufficient_reason"],
        "ai_snapshot": json.dumps(snapshot, ensure_ascii=False),
        "ai_mode": result["mode"],
        "ai_error": "",
        "ai_error_code": "",
        "ai_likely_correct": 1 if result.get("likely_correct") else 0,
        # 新内容一律回到待核对：旧确认与旧退回信息同时作废。
        "status": "pending",
        "confirmed_at": "",
        "confirmed_by": None,
        "returned_at": "",
        "returned_reason": "",
    }


def _write_analysis(ctx, row, result, expected_fingerprint=""):
    """在一个写事务里核对记录指纹再落库；老师同时在别的窗口改过就整体放弃。"""
    with ctx.db.tx() as conn:
        current = ctx.db.query_one("SELECT * FROM mistakes WHERE id=?", (row["id"],))
        if not current:
            raise ApiError("这条错题已被删除", 404)
        if expected_fingerprint and record_fingerprint(current) != expected_fingerprint:
            raise ApiError("这条错题已被修改，请重新加载后再采用", 409, code="conflict")
        previous = _previous_content(current) if current["status"] == "confirmed" else None
        updates = _analysis_updates(result, current["student_id"], previous)
        merged = dict(current)
        merged.update(updates)
        updates["teacher_edited_fields"] = _edited_json(merged)
        updates["updated_at"] = now_iso()
        _fault("mistake_apply")
        assignments = ", ".join("%s=?" % key for key in updates)
        conn.execute("UPDATE mistakes SET %s WHERE id=?" % assignments, list(updates.values()) + [row["id"]])
    return _fresh_detail(ctx, row["id"])


def api_get_mistake(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    return serialise_detail(ctx, row)


def api_mistake_image(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    if not row["image_file"]:
        raise ApiError("该记录没有图片", 404)
    path = ctx.uploads.path(row["image_file"])
    if not os.path.exists(path):
        raise ApiError("图片文件缺失", 404)
    with open(path, "rb") as fh:
        return {"__raw__": True, "body": fh.read(), "content_type": row["image_mime"] or "image/jpeg"}


def _apply_edit(db, row, body):
    updates = {}
    if "student_id" in body:
        student_id = int(body["student_id"] or 0)
        student = db.query_one("SELECT * FROM students WHERE id=?", (student_id,))
        if not student or student["class_id"] != row["class_id"]:
            raise ApiError("所选学生不属于该班级")
        updates["student_id"] = student_id
    for field in ("question_text", "solution_text", "source_type", "source_detail", "note", "teacher_note", "occurred_on"):
        if field in body:
            updates[field] = (body[field] or "").strip()
    if "error_type" in body:
        value = (body["error_type"] or "").strip()
        if value and value not in ERROR_TYPES:
            raise ApiError("错误类型不在可选项内")
        updates["error_type"] = value
    for field in ("knowledge_points", "observed_errors", "possible_causes"):
        if field in body:
            value = body[field]
            if not isinstance(value, list):
                raise ApiError("%s 必须是数组" % field)
            cleaned = []
            for item in value:
                text = str(item).strip()
                if text and text not in cleaned:
                    cleaned.append(text)
            updates[field] = json.dumps(cleaned, ensure_ascii=False)
    if "advice_text" in body:
        updates["advice_text"] = (body["advice_text"] or "").strip()
    for flag in ("insufficient", "ai_likely_correct"):
        if flag in body:
            updates[flag] = 1 if body[flag] else 0
    if "insufficient_reason" in body:
        updates["insufficient_reason"] = (body["insufficient_reason"] or "").strip()
    return updates


def api_update_mistake(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    updates = _apply_edit(ctx.db, row, ctx.body)
    image = ctx.body.get("image") if isinstance(ctx.body.get("image"), dict) else None
    base = str(ctx.body.get("base_fingerprint") or "")
    new_image = None
    if image:
        # 本轮只补照片：已经有照片的记录不替换，避免误丢原图。
        if row["image_file"]:
            raise ApiError("这条错题已有照片，本轮不修改照片")
        blob, mime, ext = _decode_image(image)
        new_image = "%s.%s" % (uuid.uuid4().hex, ext)
        ctx.uploads.save(new_image, blob)
        updates["image_file"] = new_image
        updates["image_mime"] = mime
        updates["image_width"] = int(image.get("width") or 0)
        updates["image_height"] = int(image.get("height") or 0)
    if not updates:
        return serialise_detail(ctx, row)
    try:
        with ctx.db.tx() as conn:
            current = ctx.db.query_one("SELECT * FROM mistakes WHERE id=?", (row["id"],))
            if base and record_fingerprint(current) != base:
                raise ApiError("这条错题已被修改，请重新加载", 409, code="conflict")
            merged = dict(current)
            merged.update(updates)
            updates["teacher_edited_fields"] = _edited_json(merged)
            changed = [f for f in DECISIVE_FIELDS if f in updates and str(updates[f]) != str(current[f])]
            if changed and current["status"] == "confirmed":
                # 内容真的变了才打回待核对，并把确认与退回信息一起清掉。
                updates.update(
                    {
                        "status": "pending",
                        "confirmed_at": "",
                        "confirmed_by": None,
                        "returned_at": "",
                        "returned_reason": "",
                    }
                )
            updates["updated_at"] = now_iso()
            _fault("mistake_update")
            assignments = ", ".join("%s=?" % key for key in updates)
            conn.execute("UPDATE mistakes SET %s WHERE id=?" % assignments, list(updates.values()) + [row["id"]])
    except Exception:
        if new_image:
            # 数据库没写成功，刚存进 uploads 的照片不能留下。
            ctx.uploads.remove([new_image])
        raise
    return _fresh_detail(ctx, row["id"])


REVERTABLE_FIELDS = (
    "student_id",
    "question_text",
    "solution_text",
    "knowledge_points",
    "error_type",
    "observed_errors",
    "possible_causes",
    "advice_text",
)


def _edited_json(row):
    """比对当前内容与 AI 快照，记录老师改过哪些字段。"""
    return json.dumps(edited_fields(_json_obj(row.get("ai_snapshot") or "{}"), row), ensure_ascii=False)


def api_revert_field(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    field = ctx.params["field"]
    if field not in REVERTABLE_FIELDS:
        raise ApiError("该字段不支持还原")
    ai = _json_obj(row["ai_snapshot"])
    if field not in ai:
        raise ApiError("该字段没有 AI 建议可还原")
    value = ai[field]
    if field == "student_id":
        student = ctx.db.query_one("SELECT * FROM students WHERE id=?", (value,))
        if not student or student["class_id"] != row["class_id"]:
            raise ApiError("AI 建议的学生已不存在")
        updates = {"student_id": int(value)}
    elif field in ("knowledge_points", "observed_errors", "possible_causes"):
        updates = {field: json.dumps(value, ensure_ascii=False)}
    else:
        updates = {field: value}
    merged = dict(row)
    merged.update(updates)
    updates["teacher_edited_fields"] = _edited_json(merged)
    updates["updated_at"] = now_iso()
    assignments = ", ".join("%s=?" % key for key in updates)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE mistakes SET %s WHERE id=?" % assignments, list(updates.values()) + [row["id"]])
    fresh = ctx.db.query_one(
        "SELECT m.*, s.name AS student_name, s.note AS student_note FROM mistakes m JOIN students s ON s.id=m.student_id WHERE m.id=?",
        (row["id"],),
    )
    return serialise_detail(ctx, fresh)


def api_reanalyze(ctx):
    """网页端入口：分析后立即写入记录。桌面端走预览，不调用这里。"""
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    provider = _analysis_provider(ctx)
    _require_consent(ctx, provider)
    try:
        result = provider.analyze(_analysis_job(ctx, row))
    except vision.AnalysisError as exc:
        # 分析失败不丢已有内容：只记录失败原因，供老师在页面上重试或手填。
        _record_analysis_failure(ctx, row["id"], exc.message, exc.code)
        return _fresh_detail(ctx, row["id"])
    return _write_analysis(ctx, row, result)


def _preview_store(ctx):
    store = getattr(ctx, "previews", None)
    if store is None:
        raise ApiError("当前环境不支持预览建议", 404)
    return store


def api_preview_analysis(ctx):
    """只算不写：建议交给当前进程保管，采用之前正式记录一个字都不改。"""
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    store = _preview_store(ctx)
    provider = _analysis_provider(ctx)
    _require_consent(ctx, provider)
    try:
        result = provider.analyze(_analysis_job(ctx, row))
    except vision.AnalysisError as exc:
        # 这里只把失败交回界面，一个字都不写：结果可能已经迟到，
        # 窗口是否关掉、账号是否换过只有界面知道，由它确认后再单独记。
        return {
            "ok": False,
            "error": exc.message,
            "error_code": exc.code,
            "detail": _fresh_detail(ctx, row["id"]),
        }
    # 分析期间退出登录或换了账号：这份结果不能落到任何会话里。
    alive = getattr(ctx, "session_current", None)
    if alive is not None and not alive():
        raise ApiError("登录状态已变化，这次分析结果已丢弃", 409, code="session_changed")
    record = store.put(
        ctx.user["id"],
        getattr(ctx, "token", ""),
        row["id"],
        record_fingerprint(row),
        str(ctx.body.get("task_id") or ""),
        result,
    )
    return {
        "ok": True,
        "preview_id": record["preview_id"],
        "suggestion": result,
        "detail": _fresh_detail(ctx, row["id"]),
    }


def api_apply_analysis(ctx):
    """采用建议：内容取自业务层保管的原件，界面传来的只是标识。"""
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    store = _preview_store(ctx)
    preview_id = str(ctx.body.get("preview_id") or "")
    try:
        record = store.take(preview_id, ctx.user["id"], getattr(ctx, "token", ""), row["id"])
    except PreviewError as exc:
        raise ApiError(exc.message, 409, code="preview_invalid")
    # 永远用分析开始时业务层存下的指纹，界面传什么都不参与比对：
    # 出一条建议后老师又改过题目，这里必须拒绝，不能把新内容盖掉。
    return _write_analysis(ctx, row, record["result"], expected_fingerprint=record["fingerprint"])


def api_discard_preview(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    store = _preview_store(ctx)
    store.drop(preview_id=str(ctx.body.get("preview_id") or ""), user_id=ctx.user["id"], mistake_id=row["id"])
    return {"ok": True}


def api_record_analysis_failure(ctx):
    """界面确认这次失败仍属于当前窗口、当前会话之后，才记下失败原因。"""
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    message = str(ctx.body.get("message") or "").strip()
    code = str(ctx.body.get("code") or "").strip()
    if message:
        _record_analysis_failure(ctx, row["id"], message, code)
    return _fresh_detail(ctx, row["id"])


def api_confirm_mistake(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    base = str(ctx.body.get("base_fingerprint") or "")
    with ctx.db.tx() as conn:
        current = ctx.db.query_one("SELECT * FROM mistakes WHERE id=?", (row["id"],))
        if base and record_fingerprint(current) != base:
            raise ApiError("这条错题已被修改，请重新加载", 409, code="conflict")
        if not current["question_text"] and not current["solution_text"] and not current["knowledge_points"]:
            raise ApiError("内容为空，请先补充题目或知识点再确认")
        _fault("mistake_confirm")
        conn.execute(
            "UPDATE mistakes SET status='confirmed', confirmed_at=?, confirmed_by=?, returned_at='',"
            " returned_reason='', updated_at=? WHERE id=?",
            (now_iso(), ctx.user["id"], now_iso(), row["id"]),
        )
    return _fresh_detail(ctx, row["id"])


def api_return_mistake(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    reason = (ctx.body.get("reason") or "").strip()
    base = str(ctx.body.get("base_fingerprint") or "")
    with ctx.db.tx() as conn:
        current = ctx.db.query_one("SELECT * FROM mistakes WHERE id=?", (row["id"],))
        if base and record_fingerprint(current) != base:
            raise ApiError("这条错题已被修改，请重新加载", 409, code="conflict")
        conn.execute(
            "UPDATE mistakes SET status='returned', returned_at=?, returned_reason=?, confirmed_at='',"
            " confirmed_by=NULL, updated_at=? WHERE id=?",
            (now_iso(), reason, now_iso(), row["id"]),
        )
    fresh = ctx.db.query_one(
        "SELECT m.*, s.name AS student_name, s.note AS student_note FROM mistakes m JOIN students s ON s.id=m.student_id WHERE m.id=?",
        (row["id"],),
    )
    return serialise_detail(ctx, fresh)


def api_reopen_mistake(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE mistakes SET status='pending', confirmed_at='', confirmed_by=NULL, returned_at='',"
            " returned_reason='', updated_at=? WHERE id=?",
            (now_iso(), row["id"]),
        )
    return _fresh_detail(ctx, row["id"])


def api_delete_mistake(ctx):
    row = owned_mistake(ctx.db, ctx.user, ctx.params["id"])
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM mistakes WHERE id=?", (row["id"],))
    if row["image_file"]:
        ctx.uploads.remove([row["image_file"]])
    return {"ok": True}


def api_list_mistakes(ctx):
    query = ctx.query
    class_id = int(query.get("class_id") or 0)
    class_row = owned_class(ctx.db, ctx.user, class_id)
    sql = (
        "SELECT m.*, s.name AS student_name, s.note AS student_note FROM mistakes m JOIN students s ON s.id=m.student_id WHERE m.class_id=?"
    )
    params = [class_row["id"]]
    status = query.get("status") or ""
    if status in ("pending", "confirmed", "returned"):
        sql += " AND m.status=?"
        params.append(status)
    if query.get("student_id"):
        sql += " AND m.student_id=?"
        params.append(int(query["student_id"]))
    if query.get("from"):
        sql += " AND m.occurred_on >= ?"
        params.append(query["from"])
    if query.get("to"):
        sql += " AND m.occurred_on <= ?"
        params.append(query["to"])
    if query.get("error_type"):
        sql += " AND m.error_type=?"
        params.append(query["error_type"])
    rows = ctx.db.query(sql + " ORDER BY m.occurred_on DESC, m.id DESC", params)
    if query.get("kp"):
        kp = query["kp"]
        rows = [r for r in rows if kp in _json_list(r["knowledge_points"])]
    items = [serialize_mistake(r, brief=True) for r in rows]
    counts = {"pending": 0, "confirmed": 0, "returned": 0}
    for item in ctx.db.query(
        "SELECT status, COUNT(*) AS n FROM mistakes WHERE class_id=? GROUP BY status", (class_row["id"],)
    ):
        counts[item["status"]] = item["n"]
    return {"items": items, "total": len(items), "counts": counts}


# --------------------------------------------------------------------------
# 统计与建议
# --------------------------------------------------------------------------
def _stats_filters(query):
    filters = {
        "from": (query.get("from") or "").strip(),
        "to": (query.get("to") or "").strip(),
        "kp": (query.get("kp") or "").strip(),
        "error_type": (query.get("error_type") or "").strip(),
    }
    for key in ("from", "to"):
        value = filters[key]
        if not value:
            continue
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ApiError("日期格式应为年-月-日，请重新选择", 400, code="bad_date")
    if filters["from"] and filters["to"] and filters["from"] > filters["to"]:
        raise ApiError("开始日期不能晚于结束日期，请修改后再查询", 400, code="bad_range")
    return filters


def api_class_stats(ctx):
    class_row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    filters = _stats_filters(ctx.query)
    data = build_stats(ctx.db, class_row, None, filters)
    data["options"] = options_for_filters(ctx.db, class_row)
    data["students"] = [
        {"id": s["id"], "class_id": s["class_id"], "name": s["name"], "note": s["note"], "active": s["active"]}
        for s in ctx.db.query("SELECT * FROM students WHERE class_id=? ORDER BY id", (class_row["id"],))
    ]
    # 原题正误与原因判断分别统计：缺测、正确作答不计入原题错误数。
    data["answer_check"] = answer_check_stats(ctx.db, class_row)
    return data


def api_student_stats(ctx):
    student = owned_student(ctx.db, ctx.user, ctx.params["id"])
    class_row = owned_class(ctx.db, ctx.user, student["class_id"])
    filters = _stats_filters(ctx.query)
    data = build_stats(ctx.db, class_row, student, filters)
    data["options"] = options_for_filters(ctx.db, class_row, student["id"])
    data["students"] = []
    return data


# --------------------------------------------------------------------------
# 个人学情与辅导建议
# --------------------------------------------------------------------------
def _insight_student(ctx):
    student = owned_student(ctx.db, ctx.user, ctx.params["student_id"])
    class_row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    if student["class_id"] != class_row["id"]:
        raise ApiError("这名学生不属于该班级", 404)
    return class_row, student


def api_student_insight(ctx):
    """个人问题汇总：只统计这名学生已确认的错题，不碰班级汇总。"""
    class_row, student = _insight_student(ctx)
    data = tutor_mod.load(ctx.db, class_row["id"], student["id"])
    return {
        "student": {
            "id": student["id"],
            "name": student["name"],
            "class_id": class_row["id"],
            "class_name": class_row["name"],
        },
        "summary": data["summary"],
        "advice": data["advice"],
    }


def api_student_advice_generate(ctx):
    """老师主动发起：真实模式必须先确认发送范围，请求里没有任何身份字段。"""
    class_row, student = _insight_student(ctx)
    provider, service, model = _review_provider(ctx)
    if provider is not None and not ctx.body.get("consent"):
        raise ApiError("需要先确认发送范围，才能把问题与题目、步骤证据发送给%s" % service, 400)
    try:
        result = tutor_mod.generate(ctx.db, class_row["id"], student["id"], provider, service, model)
    except tutor_mod.TutorError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    except vision.AnalysisError as exc:
        return {"ok": False, "error": exc.message, "error_code": exc.code}
    return {"ok": True, **result}


def api_student_advice_save(ctx):
    class_row, student = _insight_student(ctx)
    try:
        result = tutor_mod.save(ctx.db, class_row["id"], student["id"], ctx.body.get("content"))
    except tutor_mod.TutorError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    return {"ok": True, **result}


# --------------------------------------------------------------------------
# 教师短追问
# --------------------------------------------------------------------------
def _followup_student(ctx):
    class_row, student = _insight_student(ctx)
    return class_row, student


def _followup_call(handler):
    """把 FollowupError 与模型错误统一成老师能看懂的响应。"""
    try:
        return handler()
    except followup_mod.FollowupError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    except vision.AnalysisError as exc:
        return {"ok": False, "error": exc.message, "error_code": exc.code}


def api_followup_list(ctx):
    class_row, student = _followup_student(ctx)
    items = followup_mod.list_all(ctx.db, class_row["id"], student["id"])
    return {"ok": True, "items": items}


def api_followup_context(ctx):
    """这一条追问要用的依据：题目、学生过程、已确认错因。不调用 AI。"""
    class_row, student = _followup_student(ctx)
    try:
        payload = followup_mod.evidence_payload(
            ctx.db, class_row["id"], student["id"],
            ctx.query.get("problem") or "", ctx.query.get("mistake_id"),
        )
    except followup_mod.FollowupError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    return {"ok": True, "context": payload}


def api_followup_suggest(ctx):
    """老师主动发起：先生成候选追问，尚未记为实际提问。"""
    class_row, student = _followup_student(ctx)
    provider, service, model = _review_provider(ctx)
    if provider is not None and not ctx.body.get("consent"):
        raise ApiError("需要先确认发送范围，才能把问题与题目、步骤证据发送给%s" % service, 400)

    def run():
        try:
            result = followup_mod.suggest(
                ctx.db, class_row["id"], student["id"], ctx.body.get("problem"),
                ctx.body.get("mistake_id"), provider, service, model,
            )
        except vision.AnalysisError:
            raise
        except followup_mod.FollowupError:
            raise
        return {"ok": True, **result}

    return _followup_call(run)


def api_followup_create(ctx):
    class_row, student = _followup_student(ctx)
    try:
        payload = followup_mod.evidence_payload(
            ctx.db, class_row["id"], student["id"], ctx.body.get("problem"), ctx.body.get("mistake_id")
        )
    except followup_mod.FollowupError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    try:
        record = followup_mod.create(
            ctx.db, class_row["id"], student["id"], ctx.body.get("problem"),
            ctx.body.get("question"), ctx.body.get("source"), ctx.body.get("suggestion"), payload,
        )
    except followup_mod.FollowupError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    return {"ok": True, "followup": record}


def api_followup_get(ctx):
    class_row, student = _followup_student(ctx)
    try:
        record = followup_mod.get(ctx.db, class_row["id"], student["id"], int(ctx.params["fid"]))
    except followup_mod.FollowupError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    return {"ok": True, "followup": record}


def api_followup_answer(ctx):
    class_row, student = _followup_student(ctx)
    try:
        record = followup_mod.save_answer(
            ctx.db, class_row["id"], student["id"], int(ctx.params["fid"]), ctx.body.get("answer")
        )
    except followup_mod.FollowupError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    return {"ok": True, "followup": record}


def api_followup_update(ctx):
    """根据学生的回答更新错因候选；结果一律是待老师核对的候选。"""
    class_row, student = _followup_student(ctx)
    provider, service, model = _review_provider(ctx)
    if provider is not None and not ctx.body.get("consent"):
        raise ApiError("需要先确认发送范围，才能把问题、学生步骤与这次回答发送给%s" % service, 400)

    def run():
        return followup_mod.update_causes(
            ctx.db, class_row["id"], student["id"], int(ctx.params["fid"]), provider, service, model
        )

    result = _followup_call(run)
    if isinstance(result, dict) and result.get("ok") is False:
        return result
    return {"ok": True, **result}


def api_followup_decide(ctx):
    class_row, student = _followup_student(ctx)
    try:
        record = followup_mod.decide(
            ctx.db, class_row["id"], student["id"], int(ctx.params["fid"]), int(ctx.params["uid"]),
            ctx.body.get("action"), ctx.body.get("causes"), ctx.body.get("note"),
        )
    except followup_mod.FollowupError as exc:
        raise ApiError(exc.message, exc.status, code=exc.code)
    return {"ok": True, "followup": record}


def _advice_payload(db, row):
    evidence = _json_obj(row["evidence"])
    return {
        "id": row["id"],
        "scope": row["scope"],
        "student_id": row["student_id"],
        "content": row["content"],
        "evidence": evidence,
        "evidence_count": len(evidence.get("mistake_ids") or []),
        "teacher_edited": bool(row["teacher_edited"]),
        "generated_at": row["generated_at"],
        "updated_at": row["updated_at"],
    }


def api_class_advice(ctx):
    class_row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    students = ctx.db.query("SELECT * FROM students WHERE class_id=? ORDER BY id", (class_row["id"],))
    existing = ctx.db.query("SELECT * FROM advice WHERE class_id=?", (class_row["id"],))
    if not existing or ctx.query.get("refresh") == "1":
        advice_mod.refresh(ctx.db, class_row, students)
    rows = ctx.db.query("SELECT * FROM advice WHERE class_id=? ORDER BY scope DESC, student_id", (class_row["id"],))
    return {
        "class_advice": next((_advice_payload(ctx.db, r) for r in rows if r["scope"] == "class"), None),
        "student_advices": [_advice_payload(ctx.db, r) for r in rows if r["scope"] == "student"],
    }


def api_refresh_advice(ctx):
    class_row = owned_class(ctx.db, ctx.user, ctx.params["id"])
    students = ctx.db.query("SELECT * FROM students WHERE class_id=? ORDER BY id", (class_row["id"],))
    advice_mod.refresh(ctx.db, class_row, students)
    return {"ok": True}


def api_update_advice(ctx):
    advice_id = int(ctx.params["id"])
    row = ctx.db.query_one(
        "SELECT a.*, c.user_id FROM advice a JOIN classes c ON c.id=a.class_id WHERE a.id=?", (advice_id,)
    )
    if not row or row["user_id"] != ctx.user["id"]:
        raise ApiError("建议不存在", 404)
    content = (ctx.body.get("content") or "").strip()
    if not content:
        raise ApiError("建议内容不能为空")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE advice SET content=?, teacher_edited=1, updated_at=? WHERE id=?", (content, now_iso(), advice_id))
    return _advice_payload(ctx.db, ctx.db.query_one("SELECT * FROM advice WHERE id=?", (advice_id,)))


def _last_restore_summary(data_dir):
    last = backup.read_result(data_dir) or None
    if not last:
        return None
    phase = last.get("phase") or ("unknown" if last.get("rollback_failed") else "")
    return {
        "ok": bool(last.get("ok")),
        "at": last.get("at") or "",
        "counts": last.get("counts") or {},
        "error": last.get("error") or "",
        "phase": phase,
        "rollback_failed": bool(last.get("rollback_failed")),
    }


def api_meta(ctx):
    data = describe_ai(ctx)
    data.update(
        {
            "server_time": now_iso(),
            "knowledge_points": KNOWLEDGE_POINTS,
            "error_types": ERROR_TYPES,
            "max_image_mb": MAX_IMAGE_BYTES // (1024 * 1024),
            "app_version": VERSION,
            "portable": bool(getattr(ctx.server, "portable", False)),
            "demo_enabled": bool(getattr(ctx.server, "use_demo", False)),
            "data_dir": getattr(ctx.server, "data_dir", ""),
            "restore_blocked": backup.is_restore_blocked(ctx.server.data_dir),
            "last_restore": _last_restore_summary(ctx.server.data_dir),
        }
    )
    return data


def api_info(ctx):
    """登录页需要的信息：是否带演示数据、是否便携版，不返回任何用户数据。"""
    return {
        "app_version": VERSION,
        "portable": bool(getattr(ctx.server, "portable", False)),
        "demo_enabled": bool(getattr(ctx.server, "use_demo", False)),
        "last_restore": _last_restore_summary(ctx.server.data_dir),
        "restore_blocked": backup.is_restore_blocked(ctx.server.data_dir),
    }


def _stop_server_later(server, exit_code, delay=0.8):
    """先让响应发出去，再停止服务；启动器根据退出码决定是否换数据。"""

    def worker():
        time.sleep(delay)
        server.exit_code = exit_code
        server.shutdown()

    threading.Thread(target=worker, daemon=True).start()


def api_backup(ctx):
    """把当前账号的班级、学生、错题和照片打成 zip 返回，浏览器另存到本机。"""
    folder = os.path.join(ctx.server.data_dir, "backups")
    os.makedirs(folder, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".download-", suffix=".zip", dir=folder)
    os.close(fd)
    try:
        backup.create_backup(ctx.server.data_dir, path, ctx.user["id"])
        with open(path, "rb") as fh:
            payload = fh.read()
    except backup.BackupError as exc:
        raise ApiError(str(exc), 400)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "__raw__": True,
        "body": payload,
        "content_type": "application/zip",
        "download_name": "错题数据备份-%s.zip" % stamp,
    }


def _current_user_row(ctx):
    return ctx.db.query_one("SELECT id, username, backup_key FROM users WHERE id=?", (ctx.user["id"],)) or {
        "id": ctx.user["id"], "username": ctx.user["username"], "backup_key": "",
    }


def _write_marker(ctx, task_dir, staging, mode, legacy, manifest):
    payload = {
        "mode": mode,
        "staging": staging,
        "task_dir": task_dir,
        "user_id": ctx.user["id"],
        "username": ctx.user["username"],
        "counts": manifest.get("counts") or {},
        "created_at": manifest.get("created_at"),
        "requested_at": now_iso(),
        "legacy": legacy,
    }
    backup.write_pending(ctx.server.data_dir, payload)
    return payload


def _queue_restore(ctx):
    backup.clear_result(ctx.server.data_dir)
    _stop_server_later(ctx.server, 75)


def api_restore(ctx):
    """校验「当前账号」的备份并解压到本任务暂存目录；合并由便携版启动器在服务停止后执行。

    归属规则（绝不静默）：
    - backup_key 相同            → 可靠确认是本人账号，直接进入恢复。
    - 旧版备份（format 2 或清单无 backup_key）且 id 与用户名同时匹配
                                 → 409 code=legacy_confirm_required，需前端显式确认后再提交 /api/restore/confirm。
    - 其他                        → 400 code=foreign_backup，指向「从其他电脑导入」。
    """
    if not getattr(ctx.server, "portable", False):
        raise ApiError("一键恢复只在便携版启动器里可用；命令行版请手动替换 data 目录", 400)
    task_dir = ctx.body.get("task_dir")
    upload_path = ctx.body.get("upload_path")
    if not upload_path or not os.path.isfile(upload_path):
        raise ApiError("没有收到备份文件")
    staging = os.path.join(task_dir, "staging")
    user_row = _current_user_row(ctx)
    try:
        manifest, relation = backup.inspect_backup(upload_path, staging, user_row)
    except backup.BackupError as exc:
        backup.release_upload(ctx.server.data_dir, task_dir)
        raise ApiError("备份校验未通过：%s" % exc, 400)
    try:
        if relation["same_key"]:
            _write_marker(ctx, task_dir, staging, "restore", legacy=False, manifest=manifest)
            backup.clear_upload_claim(ctx.server.data_dir)
        elif relation["legacy"] and relation["id_match"] and relation["username_match"]:
            backup.write_task_meta(task_dir, {"kind": "restore", "legacy": True, "user_id": ctx.user["id"]})
            backup.clear_upload_claim(ctx.server.data_dir)
            raise ApiError(
                "这份备份来自旧版本软件，没有账号身份标识，无法可靠确认它属于当前账号。"
                "请先确认它就是本账号的备份，再继续恢复。",
                409,
                code="legacy_confirm_required",
                payload={
                    "task": os.path.basename(task_dir),
                    "owner_username": (manifest.get("owner") or {}).get("username") or "未知账号",
                    "created_at": manifest.get("created_at"),
                },
            )
        else:
            backup.release_upload(ctx.server.data_dir, task_dir)
            raise ApiError(
                "这份备份属于其他账号（%s），不能恢复到当前账号。"
                "如果这是从另一台电脑带来的备份，请用「从其他电脑导入」。"
                % ((manifest.get("owner") or {}).get("username") or "未知账号"),
                400,
                code="foreign_backup",
            )
    except ApiError:
        raise
    except BaseException:
        backup.release_upload(ctx.server.data_dir, task_dir)
        raise
    _queue_restore(ctx)
    return {
        "ok": True,
        "counts": manifest["counts"],
        "created_at": manifest.get("created_at"),
        "message": "备份已校验，正在恢复当前账号的数据并重启软件；完成后会回到登录页。",
    }


def api_restore_confirm(ctx):
    """旧版备份的显式确认入口：确认后才进入恢复流程。"""
    if not getattr(ctx.server, "portable", False):
        raise ApiError("一键恢复只在便携版启动器里可用", 400)
    if not ctx.body.get("confirm_legacy"):
        raise ApiError("必须明确确认这是本账号的备份后才能恢复", 400)
    task_id = str(ctx.body.get("task") or "")
    task_dir = backup.task_dir_for(ctx.server.data_dir, task_id)
    meta = backup.read_task_meta(task_dir) if task_dir else None
    if not task_dir or not meta or meta.get("kind") != "restore" or not meta.get("legacy"):
        raise ApiError("这份备份的校验结果已失效，请重新选择备份文件", 400, code="task_gone")
    if backup.pending_problem(ctx.server.data_dir):
        raise ApiError("已有一个恢复任务正在等待处理，请等它完成后再试", 409, code="restore_busy")
    staging = os.path.join(task_dir, "staging")
    manifest_path = os.path.join(staging, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ApiError("这份备份的校验结果已失效，请重新选择备份文件", 400, code="task_gone")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    owner = manifest.get("owner") or {}
    user_row = _current_user_row(ctx)
    if owner.get("id") != user_row["id"] or (owner.get("username") or "") != user_row["username"]:
        backup.release_upload(ctx.server.data_dir, task_dir)
        raise ApiError("这份备份与当前账号不一致，请改用「从其他电脑导入」", 400, code="foreign_backup")
    _write_marker(ctx, task_dir, staging, "restore", legacy=True, manifest=manifest)
    _queue_restore(ctx)
    return {"ok": True, "message": "已确认，正在恢复并重启软件；完成后会回到登录页。"}


def api_import(ctx):
    """从其他电脑导入：先校验并返回预览（来源账号、条数、是否旧版备份），不改动数据。

    导入要求目标账号还没有任何班级数据（推荐新建一个空账号再导入）。
    """
    if not getattr(ctx.server, "portable", False):
        raise ApiError("导入只在便携版启动器里可用；命令行版请手动替换 data 目录", 400)
    task_dir = ctx.body.get("task_dir")
    upload_path = ctx.body.get("upload_path")
    if not upload_path or not os.path.isfile(upload_path):
        raise ApiError("没有收到备份文件")
    staging = os.path.join(task_dir, "staging")
    user_row = _current_user_row(ctx)
    try:
        manifest, relation = backup.inspect_backup(upload_path, staging, user_row)
    except backup.BackupError as exc:
        backup.release_upload(ctx.server.data_dir, task_dir)
        raise ApiError("备份校验未通过：%s" % exc, 400)
    try:
        if relation["same_key"]:
            backup.release_upload(ctx.server.data_dir, task_dir)
            raise ApiError("这份备份就是当前账号自己的备份，请用「从备份恢复」", 400, code="own_backup")
        if ctx.db.scalar("SELECT COUNT(*) FROM classes WHERE user_id=?", (ctx.user["id"],)):
            backup.release_upload(ctx.server.data_dir, task_dir)
            raise ApiError("当前账号已有班级数据。导入只支持还没有数据的新账号：请新建一个账号后再导入。", 400, code="account_not_empty")
        backup.write_task_meta(task_dir, {"kind": "import", "user_id": ctx.user["id"]})
        backup.clear_upload_claim(ctx.server.data_dir)
    except ApiError:
        raise
    except BaseException:
        backup.release_upload(ctx.server.data_dir, task_dir)
        raise
    return {
        "ok": True,
        "task": os.path.basename(task_dir),
        "source_username": (manifest.get("owner") or {}).get("username") or "未知账号",
        "counts": manifest.get("counts") or {},
        "created_at": manifest.get("created_at"),
        "legacy": relation["legacy"],
        "message": "备份已校验。确认后会把它导入当前账号（当前账号的数据会被导入内容替换）并重启软件。",
    }


def api_import_confirm(ctx):
    """确认导入：把备份里的班级、学生、错题、照片重映射给当前空账号。"""
    if not getattr(ctx.server, "portable", False):
        raise ApiError("导入只在便携版启动器里可用", 400)
    if not ctx.body.get("confirm"):
        raise ApiError("必须确认后才能导入", 400)
    task_id = str(ctx.body.get("task") or "")
    task_dir = backup.task_dir_for(ctx.server.data_dir, task_id)
    meta = backup.read_task_meta(task_dir) if task_dir else None
    if not task_dir or not meta or meta.get("kind") != "import":
        raise ApiError("这份备份的预览已失效，请重新选择备份文件", 400, code="task_gone")
    if backup.pending_problem(ctx.server.data_dir):
        raise ApiError("已有一个恢复任务正在等待处理，请等它完成后再试", 409, code="restore_busy")
    staging = os.path.join(task_dir, "staging")
    manifest_path = os.path.join(staging, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ApiError("这份备份的预览已失效，请重新选择备份文件", 400, code="task_gone")
    if ctx.db.scalar("SELECT COUNT(*) FROM classes WHERE user_id=?", (ctx.user["id"],)):
        backup.release_upload(ctx.server.data_dir, task_dir)
        raise ApiError("当前账号已有班级数据。导入只支持还没有数据的新账号：请新建一个账号后再导入。", 400, code="account_not_empty")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    legacy = not ((manifest.get("owner") or {}).get("backup_key"))
    _write_marker(ctx, task_dir, staging, "import", legacy=legacy, manifest=manifest)
    _queue_restore(ctx)
    return {"ok": True, "message": "正在导入并重启软件；完成后会回到登录页。"}


def api_shutdown(ctx):
    if not getattr(ctx.server, "portable", False):
        raise ApiError("命令行版请在终端按 Ctrl+C 停止服务", 400)
    _stop_server_later(ctx.server, 0)
    return {"ok": True, "message": "软件已关闭，可以关掉这个页面。"}


def api_mistake_list_for_ids(ctx):
    ids = [int(x) for x in (ctx.query.get("ids") or "").split(",") if x.strip().isdigit()]
    if not ids:
        return {"items": []}
    rows = ctx.db.query(
        "SELECT m.*, s.name AS student_name, s.note AS student_note FROM mistakes m JOIN students s ON s.id=m.student_id"
        " JOIN classes c ON c.id=m.class_id WHERE c.user_id=? AND m.id IN (%s)" % ",".join("?" * len(ids)),
        [ctx.user["id"]] + ids,
    )
    return {"items": [serialize_mistake(r, brief=not ctx.query.get("full")) for r in rows]}


# --------------------------------------------------------------------------
# 作答 CSV 导入
# --------------------------------------------------------------------------
def _answer_input(ctx):
    text = ctx.body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError("没有收到 CSV 内容", 400)
    batch = str(ctx.body.get("batch") or "").strip()[:60]
    selections = ctx.body.get("selections") or {}
    if not isinstance(selections, dict):
        raise ApiError("选择参数格式不正确", 400)
    return text, batch, selections


def api_answers_list(ctx):
    """某个班级已导入的批次汇总；带 batch 时同时返回该批次的作答明细。"""
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    data = answers_mod.listing(ctx.db, cls["id"])
    batch = (ctx.query.get("batch") or "").strip()
    if batch:
        rows = ctx.db.query(
            "SELECT a.*, s.name AS matched_name FROM answers a JOIN students s ON s.id=a.student_id"
            " WHERE a.class_id=? AND a.batch=? ORDER BY a.id LIMIT 500",
            (cls["id"], batch),
        )
        data["items"] = [dict(row) for row in rows]
        data["batch"] = batch
    return data


def api_answers_preview(ctx):
    """预览导入：只读校验，不改数据库，也不发任何网络请求。"""
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    text, batch, selections = _answer_input(ctx)
    try:
        data = answers_mod.preview(ctx.db, cls["id"], text, batch)
        if selections:
            data["rows"] = answers_mod.apply_selections(
                data["rows"], answers_mod._class_students(ctx.db, cls["id"]), selections
            )
            data["counts"] = answers_mod._counts(data["rows"])
    except answers_mod.AnswerImportError as exc:
        raise ApiError(str(exc), 400)
    return data


def api_answers_import(ctx):
    """确认导入：写在一个事务里；失败整批回滚，不会留下半批次。"""
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    if not ctx.body.get("confirm"):
        raise ApiError("必须确认后才能导入", 400)
    text, batch, selections = _answer_input(ctx)
    overwrite = bool(ctx.body.get("overwrite"))
    try:
        data = answers_mod.commit(ctx.db, cls["id"], text, batch, selections, overwrite)
    except answers_mod.AnswerImportError as exc:
        raise ApiError(str(exc), 400)
    except sqlite3.IntegrityError:
        raise ApiError("导入时遇到重复记录（可能另一个窗口刚导入过），本批未写入，请重新预览", 409)
    return data


def api_answers_template(ctx):
    """给老师下载的 CSV 模板；只有表头与虚构示例。"""
    return {
        "filename": "作答导入模板.csv",
        "text": answers_mod.template_text(),
        "headers": [label for label, _ in answers_mod.HEADERS],
    }


def _review_provider(ctx):
    """作答分析只能走「纯文字」通道：模拟模式返回 None，不发任何请求。"""
    provider = _analysis_provider(ctx)
    mode = getattr(provider, "mode", "mock")
    if mode == "mock":
        return None, vision.service_name(mode), ""
    config = getattr(provider, "config", None) or {}
    service = getattr(provider, "service", "") or vision.service_name(mode)
    return provider, service, str(config.get("model") or "")


def _review_error(exc):
    raise ApiError(exc.message, exc.status, code=exc.code)


def api_answer_reviews(ctx):
    """作答记录列表：带最近一次审核状态，没分析过也照常列出。"""
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    batch = (ctx.query.get("batch") or "").strip()
    return review_mod.listing(ctx.db, cls["id"], batch)


def api_answer_review_get(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    try:
        return review_mod.detail(ctx.db, cls["id"], int(ctx.params["answer_id"]))
    except review_mod.AnswerReviewError as exc:
        _review_error(exc)


def api_answer_analyse(ctx):
    """老师主动发起：真实模式必须先勾选同意，请求里不含任何身份字段。"""
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    provider, service, model = _review_provider(ctx)
    if provider is not None and not ctx.body.get("consent"):
        raise ApiError("需要先确认发送范围，才能把题目与学生步骤发送给%s" % service, 400)
    try:
        review = review_mod.analyse(
            ctx.db, cls["id"], int(ctx.params["answer_id"]), provider, service, model
        )
    except review_mod.AnswerReviewError as exc:
        _review_error(exc)
    except vision.AnalysisError as exc:
        return {"ok": False, "error": exc.message, "error_code": exc.code}
    return {"ok": True, "review": review}


def api_answer_review_update(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    causes = ctx.body.get("causes")
    if causes is not None and not isinstance(causes, list):
        raise ApiError("错因格式不正确", 400)
    try:
        return review_mod.update(ctx.db, cls["id"], int(ctx.params["review_id"]), causes or [], ctx.body.get("note"))
    except review_mod.AnswerReviewError as exc:
        _review_error(exc)


def api_answer_review_confirm(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    try:
        return review_mod.confirm(ctx.db, cls["id"], int(ctx.params["review_id"]), ctx.user["id"])
    except review_mod.AnswerReviewError as exc:
        _review_error(exc)


def api_answer_review_reject(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    try:
        return review_mod.reject(ctx.db, cls["id"], int(ctx.params["review_id"]))
    except review_mod.AnswerReviewError as exc:
        _review_error(exc)


# --------------------------------------------------------------------------
# 作答核对（第一轮 A1–A5，契约 §3）
# --------------------------------------------------------------------------
def _check_error(exc):
    raise ApiError(exc.message, exc.status, code=exc.code)


def _demo_module():
    """合成演示模块由 demo 成员交付；这里只做懒加载，缺了也不影响其它接口。"""
    try:
        from . import demo_answers
    except ImportError:
        raise ApiError("合成演示数据模块尚不可用，请稍后再试。", 503, code="demo_unavailable")
    return demo_answers


def api_check_overview(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    try:
        return answer_check_mod.overview(ctx.db, cls, (ctx.query.get("batch") or "").strip())
    except answer_check_mod.AnswerCheckError as exc:
        _check_error(exc)


def api_check_answer(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    try:
        return answer_check_mod.detail(ctx.db, cls, int(ctx.params["answer_id"]))
    except answer_check_mod.AnswerCheckError as exc:
        _check_error(exc)


def api_check_save(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    try:
        return answer_check_mod.save(
            ctx.db, cls, ctx.user["id"], int(ctx.params["answer_id"]), ctx.body
        )
    except answer_check_mod.AnswerCheckError as exc:
        _check_error(exc)


def api_check_records(ctx):
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    try:
        return answer_check_mod.records(
            ctx.db,
            cls,
            (ctx.query.get("batch") or "").strip(),
            (ctx.query.get("cause_state") or "undecided").strip(),
        )
    except answer_check_mod.AnswerCheckError as exc:
        _check_error(exc)


def api_check_demo_seed(ctx):
    """载入合成演示数据：返回体由 demo_answers.seed 决定，这里原样透传，不另包一层。"""
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    module = _demo_module()
    try:
        return module.seed(ctx.db, ctx.uploads, cls["id"])
    except Exception:
        raise ApiError("合成演示数据写入失败，已导入的作答没有改动。", 500, code="demo_failed")


def api_check_demo_reset(ctx):
    """重置只影响合成演示数据；真实导入作答由 demo_answers.reset 保证不动。"""
    cls = owned_class(ctx.db, ctx.user, ctx.params["id"])
    module = _demo_module()
    try:
        return module.reset(ctx.db, ctx.uploads, cls["id"])
    except Exception:
        raise ApiError("合成演示数据重置失败，已导入的作答没有改动。", 500, code="demo_failed")


# --------------------------------------------------------------------------
# 路由表
# --------------------------------------------------------------------------
ROUTES = [
    ("POST", r"^/api/auth/login$", api_login, False),
    ("POST", r"^/api/auth/register$", api_register, False),
    ("POST", r"^/api/auth/logout$", api_logout, True),
    ("GET", r"^/api/auth/me$", api_me, True),
    ("POST", r"^/api/auth/password$", api_change_password, True),
    ("GET", r"^/api/meta$", api_meta, True),
    ("GET", r"^/api/info$", api_info, False),
    ("POST", r"^/api/backup$", api_backup, True),
    ("POST", r"^/api/restore$", api_restore, True),
    ("POST", r"^/api/restore/confirm$", api_restore_confirm, True),
    ("POST", r"^/api/import$", api_import, True),
    ("POST", r"^/api/import/confirm$", api_import_confirm, True),
    ("POST", r"^/api/shutdown$", api_shutdown, True),
    ("GET", r"^/api/classes$", api_list_classes, True),
    ("POST", r"^/api/classes$", api_create_class, True),
    ("GET", r"^/api/classes/(?P<id>\d+)$", api_get_class, True),
    ("PATCH", r"^/api/classes/(?P<id>\d+)$", api_update_class, True),
    ("DELETE", r"^/api/classes/(?P<id>\d+)$", api_delete_class, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/purge$", api_purge_class, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/students$", api_create_student, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/stats$", api_class_stats, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/advice$", api_class_advice, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/advice/refresh$", api_refresh_advice, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/answers$", api_answers_list, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/answers/preview$", api_answers_preview, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/answers/import$", api_answers_import, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/answer-reviews$", api_answer_reviews, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/answers/(?P<answer_id>\d+)/review$", api_answer_review_get, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/answers/(?P<answer_id>\d+)/review/analyse$", api_answer_analyse, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/reviews/(?P<review_id>\d+)$", api_answer_review_update, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/reviews/(?P<review_id>\d+)/confirm$", api_answer_review_confirm, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/reviews/(?P<review_id>\d+)/reject$", api_answer_review_reject, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/check/overview$", api_check_overview, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/check/answers/(?P<answer_id>\d+)$", api_check_answer, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/check/answers/(?P<answer_id>\d+)/save$", api_check_save, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/check/records$", api_check_records, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/check/demo/seed$", api_check_demo_seed, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/check/demo/reset$", api_check_demo_reset, True),
    ("GET", r"^/api/answers/template$", api_answers_template, True),
    ("GET", r"^/api/students/(?P<id>\d+)/stats$", api_student_stats, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/insight$", api_student_insight, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/insight/advice/generate$",
     api_student_advice_generate, True),
    ("PATCH", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/insight/advice$",
     api_student_advice_save, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups$",
     api_followup_list, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups/suggest$",
     api_followup_suggest, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups/context$",
     api_followup_context, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups$",
     api_followup_create, True),
    ("GET", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups/(?P<fid>\d+)$",
     api_followup_get, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups/(?P<fid>\d+)/answer$",
     api_followup_answer, True),
    ("POST", r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups/(?P<fid>\d+)/updates$",
     api_followup_update, True),
    ("POST",
     r"^/api/classes/(?P<id>\d+)/students/(?P<student_id>\d+)/followups/(?P<fid>\d+)/updates/(?P<uid>\d+)/decide$",
     api_followup_decide, True),
    ("PATCH", r"^/api/students/(?P<id>\d+)$", api_update_student, True),
    ("DELETE", r"^/api/students/(?P<id>\d+)$", api_delete_student, True),
    ("GET", r"^/api/mistakes$", api_list_mistakes, True),
    ("POST", r"^/api/mistakes$", api_create_mistake, True),
    ("GET", r"^/api/mistakes/(?P<id>\d+)$", api_get_mistake, True),
    ("PATCH", r"^/api/mistakes/(?P<id>\d+)$", api_update_mistake, True),
    ("DELETE", r"^/api/mistakes/(?P<id>\d+)$", api_delete_mistake, True),
    ("GET", r"^/api/mistakes/(?P<id>\d+)/image$", api_mistake_image, True),
    ("POST", r"^/api/mistakes/(?P<id>\d+)/analyze$", api_reanalyze, True),
    ("POST", r"^/api/mistakes/(?P<id>\d+)/confirm$", api_confirm_mistake, True),
    ("POST", r"^/api/mistakes/(?P<id>\d+)/return$", api_return_mistake, True),
    ("POST", r"^/api/mistakes/(?P<id>\d+)/reopen$", api_reopen_mistake, True),
    ("POST", r"^/api/mistakes/(?P<id>\d+)/revert/(?P<field>[a-z_]+)$", api_revert_field, True),
    ("PATCH", r"^/api/advice/(?P<id>\d+)$", api_update_advice, True),
    ("GET", r"^/api/mistake-list$", api_mistake_list_for_ids, True),
]

COMPILED_ROUTES = [(method, re.compile(pattern), handler, need_auth) for method, pattern, handler, need_auth in ROUTES]


def resolve(method, path):
    allowed = set()
    for route_method, pattern, handler, need_auth in COMPILED_ROUTES:
        match = pattern.match(path)
        if not match:
            continue
        if route_method != method:
            allowed.add(route_method)
            continue
        return handler, match.groupdict(), need_auth
    if allowed:
        raise ApiError("请求方法不支持", 405)
    raise ApiError("接口不存在", 404)


__all__ = ["ApiError", "resolve", "confirmed_mistakes"]
