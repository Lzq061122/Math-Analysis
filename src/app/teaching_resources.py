"""R2-05 教学资源库与辅导：五类资源、双审核发布、建议匹配、三级提示、举一反三与教学任务。

派单 §七 A–E 的后端实现。所有状态字符串都从 `r2_contract` 取，本模块不自己定义状态码表；
所有写入只碰 5 张 R2 新表（teaching_resources / teaching_resource_versions /
teaching_resource_reviews / teaching_tasks / hint_usages），R1 既有表只读。

五条硬口径（会进验收，改这里等于改验收）：

1. **版本是唯一的「当前」判据**：推荐、提示、PDF 一律按**记录下来的版本号**取正文，
   绝不「总读最新版本」。资源出新版本 / 停用后，引用旧版本的推荐、提示与任务只打
   「依据已变，待复核」（`r2_contract.apply_event('resource_version_changed', ...)` →
   `teaching_tasks.review_state='needs_review'`），不静默换版本、不删旧引用（R2-INV-08）。
2. **任务依据变化只打标记**：`mark_task_review()` 走 `apply_event('task_basis_changed')`，
   只写 `review_state`，`status` 不动、不自动改派、不自动完成（R2-INV-09）。
3. **双审核 + 权限**：数学正确性与教学适切性两项审核**分别**记录（审核人/时间/意见/发布版本），
   两项均 `pass` 且审核人是**两个不同账号**才可发布；发布只能由该版本审核人之一执行，
   其它账号（含未登录、不存在的账号）一律拒绝，且拒绝发生在写库之前。
4. **未审 / 停用 / 合成资源不进正式推荐**：正式推荐只认
   `status='published'` + 当前版本 `state='published'` + `source` 不含「合成测试」。
   未审（草稿/待审核）、停用、合成测试资源一律不出现。
5. **完整解析必须在独立尝试之后**：`hint_usages.independent_before_solution` 是唯一记录；
   教师提前看答案（`independent_attempt=0` 却给了完整解析）直接拒绝，不写库。

真实学生作答不外发：本模块不做任何外发，`r2_contract.egress_decision('teaching_resource_match')`
恒为 False，唯一的取数路径是本地 SQLite。
"""

import hashlib
import json
import re
import unicodedata

from . import r2_contract as C
from .db import now_iso

# ---------------------------------------------------------------------------
# 常量（状态字符串一律从 r2_contract 取，这里只放本模块自己的结构常量）
# ---------------------------------------------------------------------------
SYNTHETIC_SOURCE_MARK = "合成测试"          # source 含这个词 = 合成测试资源，不进正式推荐
OFFICIAL_TIERS = ("exact", "general")        # 正式推荐的两级（第三级是「提示人工补充」）
MATCH_TIERS = {
    "exact": "单元+知识点+确认错因",
    "general": "单元+知识点通用",
    "manual": "提示人工补充",
}
REVIEW_DECISIONS = {"pending": "待审核", "pass": "通过", "fail": "不通过"}
EXERCISE_PURPOSES = {
    "concept": "概念澄清",
    "steps": "步骤巩固",
    "transfer": "迁移应用",
}
EXERCISE_PHASES = {"guided": "辅导练习", "post_test": "独立后测"}
TASK_KINDS = {"individual": "个人辅导", "class": "班级辅导"}
DIFFICULTIES = ("基础", "中等", "提高")

# 任务状态里「还能改内容」的两个状态：已完成/已取消的任务只能看历史，不能悄悄改内容。
EDITABLE_TASK_STATES = ("draft", "scheduled")

MAX_TITLE = 80
MAX_BODY_TEXT = 4000


class TeachingResourceError(ValueError):
    """可以直接展示给老师的业务错误；`code` 供界面与测试断言用。"""

    def __init__(self, message, code="invalid", detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _json_load(text, default):
    if isinstance(text, (dict, list)):
        return text
    raw = (text or "").strip()
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _json_dump(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _text(value, limit=0):
    out = str(value or "").strip()
    return out[:limit] if limit else out


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _user_id(user):
    """接受 int / dict / 带 id 的对象；没有身份返回 None（未登录）。"""
    if user is None:
        return None
    if isinstance(user, int):
        return user
    if isinstance(user, dict):
        return _int(user.get("id"), None) if user.get("id") is not None else None
    return _int(getattr(user, "id", None), None)


def _slug(text):
    """resource_key 的可读片段：保留中文与字母数字，其余压成短横线。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", raw, flags=re.UNICODE).strip("-")
    return cleaned[:40] or "x"


def make_resource_key(kind, knowledge_point, cause_code, title):
    """跨版本稳定的 resource_key：同一道资源重复导入得到同一个 key（幂等的前提）。"""
    kp = _slug(knowledge_point)
    cause = _slug(cause_code) if cause_code else "通用"
    digest = hashlib.sha256(
        ("%s|%s|%s|%s" % (kind, knowledge_point, cause_code, _text(title))).encode("utf-8")
    ).hexdigest()[:8]
    return "%s.%s.%s.%s" % (kp, cause, kind, digest)


def is_synthetic(source):
    return SYNTHETIC_SOURCE_MARK in str(source or "")


# ---------------------------------------------------------------------------
# A. 资源库：统一结构 / 校验 / 建档 / 改版 / 检索
# ---------------------------------------------------------------------------
def normalize_resource(payload):
    """校验并规范化一条资源定义。返回可直接落库的 dict（不写库）。

    统一结构：`resource_key`（独立 ID，可留空由标题推导）、`kind`、`unit`、
    `knowledge_point`、`cause_code`、`title`、`body`、`conditions`、`source`。
    练习额外要求 `body.stem`、`body.answer`、`body.solution`、`body.difficulty`、`body.purpose`。
    """
    data = dict(payload or {})
    kind = _text(data.get("kind"), 20)
    if kind not in C.RESOURCE_KINDS:
        raise TeachingResourceError(
            "资源类型必须是 %s 之一，收到 %r" % ("/".join(C.RESOURCE_KINDS), data.get("kind")),
            "bad_kind")
    unit = _text(data.get("unit"), 60)
    kp = _text(data.get("knowledge_point"), 60)
    if not kp:
        raise TeachingResourceError("资源必须标知识点（knowledge_point），否则无法匹配。", "missing_kp")
    cause = _text(data.get("cause_code"), 60)
    title = _text(data.get("title"), MAX_TITLE)
    if not title:
        raise TeachingResourceError("资源标题不能为空。", "missing_title")

    body = data.get("body")
    body = body if isinstance(body, dict) else {}
    body = {str(key): value for key, value in body.items()}
    conditions = data.get("conditions")
    conditions = conditions if isinstance(conditions, dict) else {}

    if kind == "exercise":
        body = _normalize_exercise_body(body)
    elif kind == "hint":
        level = _text(body.get("level"), 4).upper()
        if level not in C.HINT_LEVELS:
            raise TeachingResourceError(
                "三级提示必须标 P1/P2/P3（%s），收到 %r" % ("/".join(C.HINT_LEVEL_ORDER), level),
                "bad_hint_level")
        if not _text(body.get("text"), MAX_BODY_TEXT):
            raise TeachingResourceError("提示正文不能为空。", "missing_text")
        body["level"] = level
    elif kind == "followup":
        if not _text(body.get("question"), MAX_BODY_TEXT):
            raise TeachingResourceError("追问必须有一句问学生的话（body.question）。", "missing_text")
    elif kind == "cause":
        if not cause:
            raise TeachingResourceError("错因资源必须标 cause_code。", "missing_cause")
        if not _text(body.get("label"), MAX_TITLE):
            raise TeachingResourceError("错因资源必须给中文名（body.label）。", "missing_text")
    elif kind == "advice":
        if not _text(body.get("text"), MAX_BODY_TEXT):
            raise TeachingResourceError("教学建议正文不能为空。", "missing_text")

    key = _text(data.get("resource_key"), 160) or make_resource_key(kind, kp, cause, title)
    return {
        "resource_key": key,
        "kind": kind,
        "unit": unit,
        "knowledge_point": kp,
        "cause_code": cause,
        "title": title,
        "body": body,
        "conditions": conditions,
        "source": _text(data.get("source"), 60),
    }


def _normalize_exercise_body(body):
    stem = _text(body.get("stem"), MAX_BODY_TEXT)
    answer = _text(body.get("answer"), MAX_BODY_TEXT)
    solution = _text(body.get("solution"), MAX_BODY_TEXT)
    if not stem:
        raise TeachingResourceError("练习必须有题干（body.stem）。", "missing_stem")
    if not answer:
        raise TeachingResourceError("练习必须有标准答案（body.answer）；教师版 PDF 要用它。", "missing_answer")
    purpose = _text(body.get("purpose"), 20)
    if purpose not in EXERCISE_PURPOSES:
        raise TeachingResourceError(
            "练习用途必须是 %s 之一（概念澄清/步骤巩固/迁移应用），收到 %r"
            % ("/".join(EXERCISE_PURPOSES), purpose), "bad_purpose")
    difficulty = _text(body.get("difficulty"), 10) or "基础"
    if difficulty not in DIFFICULTIES:
        raise TeachingResourceError(
            "练习难度必须是 %s 之一，收到 %r" % ("/".join(DIFFICULTIES), difficulty), "bad_difficulty")
    out = dict(body)
    out["stem"] = stem
    out["answer"] = answer
    out["solution"] = solution
    out["purpose"] = purpose
    out["difficulty"] = difficulty
    # 作答空间：学生版 PDF 按这个行数留空；缺省按难度给一个合理值。
    out["answer_lines"] = max(1, min(12, _int(body.get("answer_lines"), 0)
                                    or {"基础": 3, "中等": 4, "提高": 5}[difficulty]))
    return out


def _resource_row(db, resource_key):
    return db.query_one(
        "SELECT * FROM teaching_resources WHERE resource_key=?", (resource_key,))


def _version_row(db, resource_key, version):
    return db.query_one(
        "SELECT * FROM teaching_resource_versions WHERE resource_key=? AND version=?",
        (resource_key, int(version)))


def _reviews(db, resource_key, version):
    return db.query(
        "SELECT * FROM teaching_resource_reviews WHERE resource_key=? AND version=?"
        " ORDER BY review_kind, id", (resource_key, int(version)))


def _resource_view(db, row, version=None):
    """把资源行 + 版本行拼成对外结构（正文与条件取自**指定版本**，不是最新）。"""
    if row is None:
        return None
    version = _int(version, _int(row.get("current_version"), 0))
    vrow = _version_row(db, row["resource_key"], version) if version else None
    out = {
        "resource_key": row["resource_key"],
        "kind": row["kind"],
        "kind_label": C.RESOURCE_KINDS.get(row["kind"], row["kind"]),
        "unit": row["unit"],
        "knowledge_point": row["knowledge_point"],
        "cause_code": row["cause_code"],
        "title": row["title"],
        "source": row["source"],
        "status": row["status"],
        "status_label": C.RESOURCE_STATES.get(row["status"], row["status"]),
        "current_version": _int(row["current_version"]),
        "version": version,
        "synthetic": is_synthetic(row["source"]),
        "body": _json_load((vrow or row).get("body"), {}),
        "conditions": _json_load((vrow or row).get("conditions"), {}),
        "version_state": (vrow or {}).get("state", ""),
        "published_at": (vrow or {}).get("published_at", ""),
        "disabled_at": (vrow or {}).get("disabled_at", ""),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }
    out["version_state_label"] = C.RESOURCE_STATES.get(out["version_state"], out["version_state"])
    return out


def _assert_version_editable(db, resource_key, version):
    """只有草稿 / 待审核版本能改、能送审；已发布或已停用版本必须先出新版本。"""
    vrow = _version_row(db, resource_key, version)
    if vrow is None:
        raise TeachingResourceError("资源 %s 没有第 %s 版。" % (resource_key, version), "no_version")
    if vrow["state"] not in ("draft", "pending_review"):
        raise TeachingResourceError(
            "第 %s 版当前是「%s」，已发布/已停用的版本不能直接改；请出新版本。"
            % (version, C.RESOURCE_STATES.get(vrow["state"], vrow["state"])),
            "version_locked")
    return vrow


def create_resource(db, payload, created_by=None):
    """建档：资源行 + 第 1 版（草稿）。已存在的 key 不走这里（重复导入见 import_resources）。"""
    data = normalize_resource(payload)
    key = data["resource_key"]
    if _resource_row(db, key) is not None:
        raise TeachingResourceError("资源 %s 已存在；改内容请出新版本。" % key, "duplicate_key")
    now = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO teaching_resources(resource_key, kind, unit, knowledge_point, cause_code,"
            " title, body, conditions, source, status, current_version, created_by, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, data["kind"], data["unit"], data["knowledge_point"], data["cause_code"],
             data["title"], _json_dump(data["body"]), _json_dump(data["conditions"]),
             data["source"], "draft", 1, created_by, now, now),
        )
        conn.execute(
            "INSERT INTO teaching_resource_versions(resource_key, version, body, conditions, state,"
            " note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (key, 1, _json_dump(data["body"]), _json_dump(data["conditions"]), "draft",
             "建档", created_by, now),
        )
    return _resource_view(db, _resource_row(db, key), 1)


def update_resource(db, resource_key, body=None, conditions=None, title=None, note="",
                    created_by=None, unit=None, knowledge_point=None, cause_code=None):
    """改内容 = 出新版本。旧版本一行不动，仍可按版本号追溯。

    已发布 / 已停用的资源改内容后，新版本**立刻回到待审核**（派单 B：修改后新版本重回待审核，
    未审不得用于正式推荐）；还在草稿 / 待审核阶段的资源改内容只留在草稿，不必每次都堆版本。
    """
    row = _resource_row(db, resource_key)
    if row is None:
        raise TeachingResourceError("资源 %s 不存在。" % resource_key, "no_resource")
    current = _resource_view(db, row)
    # 当前版本还没发布时，允许在「草稿/待审核」上继续改：出一版会堆出一串没人看过的历史。
    was_published = row["status"] in ("published", "disabled")
    base = dict(current)
    merged = {
        "resource_key": resource_key,
        "kind": base["kind"],
        "unit": _text(unit, 60) if unit is not None else base["unit"],
        "knowledge_point": _text(knowledge_point, 60) if knowledge_point is not None
        else base["knowledge_point"],
        "cause_code": _text(cause_code, 60) if cause_code is not None else base["cause_code"],
        "title": _text(title, MAX_TITLE) if title is not None else base["title"],
        "body": body if body is not None else base["body"],
        "conditions": conditions if conditions is not None else base["conditions"],
        "source": base["source"],
    }
    data = normalize_resource(merged)
    now = now_iso()
    new_version = _int(row["current_version"]) + 1
    new_state = "pending_review" if was_published else "draft"
    with db.tx() as conn:
        conn.execute(
            "UPDATE teaching_resources SET unit=?, knowledge_point=?, cause_code=?, title=?,"
            " body=?, conditions=?, status=?, current_version=?, updated_at=? WHERE resource_key=?",
            (data["unit"], data["knowledge_point"], data["cause_code"], data["title"],
             _json_dump(data["body"]), _json_dump(data["conditions"]), new_state, new_version,
             now, resource_key),
        )
        conn.execute(
            "INSERT INTO teaching_resource_versions(resource_key, version, body, conditions,"
            " state, note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (resource_key, new_version, _json_dump(data["body"]), _json_dump(data["conditions"]),
             new_state, _text(note, 200) or "内容修改", created_by, now),
        )
        if new_state == "pending_review":
            # 改内容等于重新审：旧版本的「通过」不能顺延到新版本，双审核从零开始。
            conn.execute(
                "DELETE FROM teaching_resource_reviews WHERE resource_key=? AND version=?",
                (resource_key, new_version))
            for review_kind in C.RESOURCE_REVIEW_KINDS:
                conn.execute(
                    "INSERT INTO teaching_resource_reviews(resource_key, version, review_kind,"
                    " reviewer, reviewer_name, decision, opinion, reviewed_at, publish_version)"
                    " VALUES (?,?,?,NULL,'','pending','','',NULL)",
                    (resource_key, new_version, review_kind))
    return _resource_view(db, _resource_row(db, resource_key), new_version)


def get_resource(db, resource_key, version=None):
    row = _resource_row(db, resource_key)
    if row is None:
        return None
    view = _resource_view(db, row, version)
    view["reviews"] = [
        {
            "review_kind": item["review_kind"],
            "review_kind_label": C.RESOURCE_REVIEW_KINDS.get(item["review_kind"],
                                                             item["review_kind"]),
            "reviewer": item["reviewer"],
            "reviewer_name": item["reviewer_name"],
            "decision": item["decision"],
            "decision_label": REVIEW_DECISIONS.get(item["decision"], item["decision"]),
            "opinion": item["opinion"],
            "reviewed_at": item["reviewed_at"],
            "publish_version": item["publish_version"],
        }
        for item in _reviews(db, resource_key, view["version"])
    ]
    return view


def list_resources(db, kind=None, status=None, unit=None, knowledge_point=None, cause_code=None,
                   include_synthetic=True, keyword="", limit=500):
    """检索（A 的「检索」）：条件全部可选；返回按类型/知识点/标题排序的对外结构。"""
    sql = ["SELECT * FROM teaching_resources WHERE 1=1"]
    params = []
    for column, value in (("kind", kind), ("status", status), ("unit", unit),
                          ("knowledge_point", knowledge_point), ("cause_code", cause_code)):
        if value:
            sql.append("AND %s=?" % column)
            params.append(value)
    sql.append("ORDER BY kind, knowledge_point, title LIMIT ?")
    params.append(int(limit))
    rows = db.query(" ".join(sql), tuple(params))
    out = []
    word = _text(keyword)
    for row in rows:
        view = _resource_view(db, row)
        if not include_synthetic and view["synthetic"]:
            continue
        if word and word not in " ".join(
                [view["title"], view["knowledge_point"], view["unit"], view["cause_code"],
                 json.dumps(view["body"], ensure_ascii=False)]):
            continue
        out.append(view)
    return out


def search_resources(db, keyword, kind=None, include_synthetic=True):
    return list_resources(db, kind=kind, keyword=keyword, include_synthetic=include_synthetic)


def version_history(db, resource_key):
    """历史版本逐版可追溯：版本号、状态、正文、审核结论、发布时间都在这里。"""
    rows = db.query(
        "SELECT * FROM teaching_resource_versions WHERE resource_key=? ORDER BY version",
        (resource_key,))
    out = []
    for row in rows:
        out.append({
            "version": row["version"],
            "state": row["state"],
            "state_label": C.RESOURCE_STATES.get(row["state"], row["state"]),
            "note": row["note"],
            "body": _json_load(row["body"], {}),
            "conditions": _json_load(row["conditions"], {}),
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "disabled_at": row["disabled_at"],
            "reviews": [
                {
                    "review_kind": item["review_kind"],
                    "reviewer": item["reviewer"],
                    "reviewer_name": item["reviewer_name"],
                    "decision": item["decision"],
                    "decision_label": REVIEW_DECISIONS.get(item["decision"], item["decision"]),
                    "opinion": item["opinion"],
                    "reviewed_at": item["reviewed_at"],
                    "publish_version": item["publish_version"],
                }
                for item in _reviews(db, resource_key, row["version"])
            ],
        })
    return out


# ---------------------------------------------------------------------------
# B. 审核发布：双审核 / 权限 / 版本 / 停用 / 未审不进推荐
# ---------------------------------------------------------------------------
def submit_for_review(db, resource_key, version=None, note="", user=None):
    """草稿 → 待审核。只有这个动作能让资源进入审核队列。"""
    row = _resource_row(db, resource_key)
    if row is None:
        raise TeachingResourceError("资源 %s 不存在。" % resource_key, "no_resource")
    version = _int(version, _int(row["current_version"]))
    vrow = _assert_version_editable(db, resource_key, version)
    if vrow["state"] == "pending_review":
        raise TeachingResourceError("第 %s 版已在待审核队列里。" % version, "already_pending")
    now = now_iso()
    with db.tx() as conn:
        conn.execute(
            "UPDATE teaching_resource_versions SET state='pending_review', note=?, created_at=?"
            " WHERE resource_key=? AND version=?",
            (_text(note, 200) or vrow["note"], now, resource_key, version))
        conn.execute(
            "UPDATE teaching_resources SET status='pending_review', updated_at=?"
            " WHERE resource_key=?", (now, resource_key))
        # 送审时把旧审核结论清成待审：改了内容还留着上一版的「通过」等于没审。
        conn.execute(
            "DELETE FROM teaching_resource_reviews WHERE resource_key=? AND version=?",
            (resource_key, version))
        for review_kind in C.RESOURCE_REVIEW_KINDS:
            conn.execute(
                "INSERT INTO teaching_resource_reviews(resource_key, version, review_kind,"
                " reviewer, reviewer_name, decision, opinion, reviewed_at, publish_version)"
                " VALUES (?,?,?,NULL,'','pending','','',NULL)",
                (resource_key, version, review_kind))
    return _resource_view(db, _resource_row(db, resource_key), version)


def record_review(db, resource_key, version, review_kind, reviewer, decision, opinion="",
                  reviewer_name=""):
    """记录一项审核结论（数学正确性 / 教学适切性分别记录，含审核人、时间、意见）。"""
    if review_kind not in C.RESOURCE_REVIEW_KINDS:
        raise TeachingResourceError(
            "审核类型必须是 %s 之一。" % "/".join(C.RESOURCE_REVIEW_KINDS), "bad_review_kind")
    if decision not in ("pass", "fail"):
        raise TeachingResourceError("审核结论只能是 pass 或 fail。", "bad_decision")
    reviewer = _user_id(reviewer)
    if reviewer is None:
        raise TeachingResourceError("审核必须记录审核人账号。", "no_reviewer")
    if db.query_one("SELECT id FROM users WHERE id=?", (reviewer,)) is None:
        raise TeachingResourceError("审核人账号 %s 不存在。" % reviewer, "unknown_reviewer")
    row = _resource_row(db, resource_key)
    if row is None:
        raise TeachingResourceError("资源 %s 不存在。" % resource_key, "no_resource")
    version = _int(version, _int(row["current_version"]))
    vrow = _version_row(db, resource_key, version)
    if vrow is None:
        raise TeachingResourceError("资源 %s 没有第 %s 版。" % (resource_key, version), "no_version")
    if vrow["state"] != "pending_review":
        raise TeachingResourceError(
            "第 %s 版当前是「%s」，只有待审核版本能记审核结论。"
            % (version, C.RESOURCE_STATES.get(vrow["state"], vrow["state"])), "not_pending")
    others = [
        item for item in _reviews(db, resource_key, version)
        if item["review_kind"] != review_kind and item["decision"] == "pass"
    ]
    if decision == "pass" and any(_int(item["reviewer"]) == reviewer for item in others):
        raise TeachingResourceError(
            "数学正确性与教学适切性必须由两个不同账号分别审核；同一人两项都签不算双审核。",
            "same_reviewer")
    now = now_iso()
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT id FROM teaching_resource_reviews WHERE resource_key=? AND version=?"
            " AND review_kind=? AND reviewer=?", (resource_key, version, review_kind, reviewer)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE teaching_resource_reviews SET decision=?, opinion=?, reviewed_at=?,"
                " publish_version=NULL, reviewer_name=? WHERE id=?",
                (decision, _text(opinion, 500), now, _text(reviewer_name, 40), existing["id"]))
        else:
            # 「待审」占位行不留人：真审核记录写进来时，把 pending 空行换成这一条。
            conn.execute(
                "DELETE FROM teaching_resource_reviews WHERE resource_key=? AND version=?"
                " AND review_kind=? AND reviewer IS NULL AND decision='pending'",
                (resource_key, version, review_kind))
            conn.execute(
                "INSERT INTO teaching_resource_reviews(resource_key, version, review_kind,"
                " reviewer, reviewer_name, decision, opinion, reviewed_at, publish_version)"
                " VALUES (?,?,?,?,?,?,?,?,NULL)",
                (resource_key, version, review_kind, reviewer, _text(reviewer_name, 40),
                 decision, _text(opinion, 500), now))
    return review_state(db, resource_key, version)


def review_state(db, resource_key, version=None):
    """一份版本的双审核进度：两项结论、是否都过、审核人是否不同（发布闸门就用它）。"""
    row = _resource_row(db, resource_key)
    if row is None:
        raise TeachingResourceError("资源 %s 不存在。" % resource_key, "no_resource")
    version = _int(version, _int(row["current_version"]))
    items = _reviews(db, resource_key, version)
    by_kind = {}
    for item in items:
        if item["decision"] == "pending":
            continue
        # 同一审核类型同一个人只留最后一条；不同人各留一条（便于看出「两人各签一项」）。
        by_kind.setdefault(item["review_kind"], []).append(item)
    passed = {}
    reviewers = {}
    for review_kind in C.RESOURCE_REVIEW_KINDS:
        picked = [item for item in by_kind.get(review_kind, []) if item["decision"] == "pass"]
        passed[review_kind] = bool(picked)
        if picked:
            reviewers[review_kind] = _int(picked[-1]["reviewer"])
    both_pass = all(passed.values())
    distinct = len({value for value in reviewers.values() if value}) == len(reviewers) and both_pass
    return {
        "resource_key": resource_key,
        "version": version,
        "passed": passed,
        "reviewers": reviewers,
        "both_pass": both_pass,
        "reviewers_distinct": distinct,
        "ready_to_publish": both_pass and distinct,
        "reviews": [
            {
                "review_kind": item["review_kind"],
                "review_kind_label": C.RESOURCE_REVIEW_KINDS.get(item["review_kind"],
                                                                 item["review_kind"]),
                "reviewer": item["reviewer"],
                "reviewer_name": item["reviewer_name"],
                "decision": item["decision"],
                "decision_label": REVIEW_DECISIONS.get(item["decision"], item["decision"]),
                "opinion": item["opinion"],
                "reviewed_at": item["reviewed_at"],
                "publish_version": item["publish_version"],
            }
            for item in items
        ],
    }


def publish_permission(db, resource_key, version, user):
    """发布权限判定（不写库）：返回 {"allowed","code","message"}。

    口径：发布权属于**完成该版本审核的审核人**——审核人已对数学/教学负责，发布只是
    把已审版本置为已发布。未登录、账号不存在、非审核人（含自建资源但没审）一律拒绝。
    """
    uid = _user_id(user)
    if uid is None:
        return {"allowed": False, "code": "no_account", "message": "未登录账号不能发布教学资源。"}
    if db.query_one("SELECT id FROM users WHERE id=?", (uid,)) is None:
        return {"allowed": False, "code": "unknown_account",
                "message": "账号 %s 不存在，不能发布教学资源。" % uid}
    state = review_state(db, resource_key, version)
    reviewers = {value for value in state["reviewers"].values() if value}
    if not state["both_pass"]:
        return {"allowed": False, "code": "reviews_incomplete",
                "message": "数学正确性与教学适切性两项审核都通过后才能发布。"}
    if not state["reviewers_distinct"]:
        return {"allowed": False, "code": "same_reviewer",
                "message": "两项审核必须由两个不同账号完成，不能自审自发。"}
    if uid not in reviewers:
        return {"allowed": False, "code": "not_reviewer",
                "message": "当前账号不是这一版的审核人，没有发布权限。"}
    return {"allowed": True, "code": "", "message": "可以发布。"}


def publish_version(db, resource_key, version=None, user=None, note=""):
    """发布：两项审核均过 + 有发布权限才写库；发布后旧版本引用只打「待复核」（R2-INV-08）。"""
    row = _resource_row(db, resource_key)
    if row is None:
        raise TeachingResourceError("资源 %s 不存在。" % resource_key, "no_resource")
    version = _int(version, _int(row["current_version"]))
    vrow = _version_row(db, resource_key, version)
    if vrow is None:
        raise TeachingResourceError("资源 %s 没有第 %s 版。" % (resource_key, version), "no_version")
    if vrow["state"] == "published":
        raise TeachingResourceError("第 %s 版已经是已发布状态。" % version, "already_published")
    if vrow["state"] != "pending_review":
        raise TeachingResourceError(
            "第 %s 版当前是「%s」，先送审并完成双审核才能发布。"
            % (version, C.RESOURCE_STATES.get(vrow["state"], vrow["state"])), "not_pending")
    permission = publish_permission(db, resource_key, version, user)
    if not permission["allowed"]:
        # 拒绝发生在写库之前：状态、版本、审核记录一行都不动。
        raise TeachingResourceError(permission["message"], permission["code"],
                                    {"permission": permission})
    now = now_iso()
    previous = _int(row["current_version"])
    uid = _user_id(user)
    with db.tx() as conn:
        conn.execute(
            "UPDATE teaching_resource_versions SET state='published', published_at=?, note=?"
            " WHERE resource_key=? AND version=?",
            (now, _text(note, 200) or vrow["note"], resource_key, version))
        conn.execute(
            "UPDATE teaching_resources SET status='published', current_version=?, updated_at=?"
            " WHERE resource_key=?", (version, now, resource_key))
        for review_kind in C.RESOURCE_REVIEW_KINDS:
            conn.execute(
                "UPDATE teaching_resource_reviews SET publish_version=?, reviewer_name="
                "COALESCE(NULLIF(reviewer_name,''), ?) WHERE resource_key=? AND version=?"
                " AND review_kind=? AND decision='pass'",
                (version, "审核人", resource_key, version, review_kind))
    # 旧版本被引用时只打「依据已变，待复核」：不换版本、不删引用（R2-INV-08）。
    stale = _mark_references_stale(db, resource_key, version)
    view = _resource_view(db, _resource_row(db, resource_key), version)
    view["reviewers"] = sorted({value for value in
                                review_state(db, resource_key, version)["reviewers"].values()
                                if value})
    view["replaced_version"] = previous if previous != version else None
    view["references_marked"] = stale
    view["published_by"] = uid
    return view


def disable_resource(db, resource_key, user=None, reason=""):
    """停用：当前版本置 disabled，资源整体停用；停用后不再进新推荐（旧引用仍可追溯）。"""
    row = _resource_row(db, resource_key)
    if row is None:
        raise TeachingResourceError("资源 %s 不存在。" % resource_key, "no_resource")
    if row["status"] == "disabled":
        raise TeachingResourceError("资源 %s 已经是停用状态。" % resource_key, "already_disabled")
    permission = disable_permission(db, resource_key, user)
    if not permission["allowed"]:
        raise TeachingResourceError(permission["message"], permission["code"],
                                    {"permission": permission})
    version = _int(row["current_version"])
    now = now_iso()
    with db.tx() as conn:
        conn.execute(
            "UPDATE teaching_resource_versions SET state='disabled', disabled_at=?, note=?"
            " WHERE resource_key=? AND version=?",
            (now, _text(reason, 200) or "停用", resource_key, version))
        conn.execute(
            "UPDATE teaching_resources SET status='disabled', updated_at=? WHERE resource_key=?",
            (now, resource_key))
    stale = _mark_references_stale(db, resource_key, None, disabled=True)
    view = _resource_view(db, _resource_row(db, resource_key), version)
    view["references_marked"] = stale
    return view


def disable_permission(db, resource_key, user):
    """停用权限：已发布资源的审核人或创建人可以停用；未登录/不存在账号一律拒绝。"""
    uid = _user_id(user)
    if uid is None:
        return {"allowed": False, "code": "no_account", "message": "未登录账号不能停用教学资源。"}
    if db.query_one("SELECT id FROM users WHERE id=?", (uid,)) is None:
        return {"allowed": False, "code": "unknown_account", "message": "账号不存在。"}
    row = _resource_row(db, resource_key)
    if row is None:
        return {"allowed": False, "code": "no_resource", "message": "资源不存在。"}
    allowed_ids = {_int(row["created_by"])}
    for item in _reviews(db, resource_key, _int(row["current_version"])):
        allowed_ids.add(_int(item["reviewer"]))
    allowed_ids.discard(0)
    if uid not in allowed_ids:
        return {"allowed": False, "code": "not_reviewer",
                "message": "当前账号不是该资源的创建人或审核人，没有停用权限。"}
    return {"allowed": True, "code": "", "message": "可以停用。"}


def _mark_references_stale(db, resource_key, published_version, disabled=False):
    """R2-INV-08 落库：引用旧版本的任务/提示只标「依据已变，待复核」，状态一律不动。

    返回被标记的任务 id 列表（测试用它证明「旧引用没被静默换版本」）。
    """
    state = {"task": {"review": "ok", "reason": ""}}
    plan = C.apply_event(
        "resource_version_changed", state,
        {"resource_version": "%s@%s" % (resource_key, published_version or "disabled")})
    rule_ids = set(plan["rules"])
    if "R2-INV-08" not in rule_ids:
        return []
    marked = []
    now = now_iso()
    rows = db.query("SELECT * FROM teaching_tasks ORDER BY id")
    for row in rows:
        refs = _json_load(row["resource_versions"], {})
        if resource_key not in refs:
            continue
        ref_version = _int(refs.get(resource_key))
        hit = disabled or (published_version is not None and ref_version != _int(published_version))
        if not hit:
            continue
        if row["review_state"] == "needs_review":
            continue
        marked.append(_touch_task_review(db, row, "resource_version_changed",
                                         "%s 已%s为第 %s 版，本任务引用的第 %s 版需复核"
                                         % (resource_key, "停用" if disabled else "发布",
                                            published_version or "-", ref_version)))
    return marked


def _touch_task_review(db, row, reason, note_text):
    """把一条任务打成 needs_review：只加 review_state 与历史，status 不动（R2-INV-09）。"""
    history = _json_load(row["history"], [])
    now = now_iso()
    history.append({
        "at": now,
        "action": "mark_needs_review",
        "reason": reason,
        "note": note_text,
        "status": row["status"],
    })
    with db.tx() as conn:
        conn.execute(
            "UPDATE teaching_tasks SET review_state='needs_review', history=?, updated_at=?"
            " WHERE id=? AND review_state<>'needs_review'",
            (_json_dump(history), now, row["id"]))
    return row["id"]


def review_queue(db):
    """待审核队列：只列 pending_review 的版本，附双审核进度（界面 B 用）。"""
    rows = db.query(
        "SELECT * FROM teaching_resources WHERE status='pending_review'"
        " ORDER BY updated_at, resource_key")
    out = []
    for row in rows:
        view = _resource_view(db, row)
        state = review_state(db, row["resource_key"])
        view["review_progress"] = state
        out.append(view)
    return out


# ---------------------------------------------------------------------------
# C. 建议匹配：单元+知识点+确认错因 → 单元+知识点通用 → 提示人工补充
# ---------------------------------------------------------------------------
def _official_rows(db, unit=None, knowledge_point=None):
    """正式推荐的候选池：已发布 + 当前版本已发布 + 非合成测试。未审/停用一律不在里面。"""
    sql = ("SELECT * FROM teaching_resources WHERE status='published'"
           " AND status<>'disabled'")
    params = []
    if unit:
        sql += " AND unit=?"
        params.append(unit)
    if knowledge_point:
        sql += " AND knowledge_point=?"
        params.append(knowledge_point)
    sql += " ORDER BY kind, title"
    out = []
    for row in db.query(sql, tuple(params)):
        view = _resource_view(db, row)
        if view["synthetic"]:
            continue
        if view["version_state"] != "published":
            continue
        out.append(view)
    return out


def is_official(view):
    """一条资源视图是否可用于正式推荐（未审 / 停用 / 合成测试都不算）。"""
    if not view:
        return False
    if view.get("synthetic"):
        return False
    if view.get("status") != "published" or view.get("version_state") != "published":
        return False
    return True


def match_resources(db, unit, knowledge_point, cause_code="", cause_confirmed=False,
                    kinds=None, limit=12):
    """按三级顺序匹配已发布资源：精确（单元+知识点+确认错因）→ 通用 → 人工补充。

    错因未确认时**只推通用**：拿一份未确认的错因去推专项资源，等于替老师下判断。
    """
    unit = _text(unit, 60)
    kp = _text(knowledge_point, 60)
    cause = _text(cause_code, 60)
    if not kp:
        return {
            "tier": "manual",
            "tier_label": MATCH_TIERS["manual"],
            "items": [],
            "tiers": [],
            "note": "没有知识点，无法匹配资源；请人工补充。",
            "basis": {"unit": unit, "knowledge_point": kp, "cause_code": cause,
                      "cause_confirmed": bool(cause_confirmed)},
        }
    pool = _official_rows(db, unit=unit or None, knowledge_point=kp)
    if kinds:
        pool = [item for item in pool if item["kind"] in set(kinds)]
    exact, general = [], []
    for view in pool:
        if cause_confirmed and cause and view["cause_code"] == cause:
            exact.append(view)
        elif not view["cause_code"]:
            general.append(view)
    tiers = []
    items = []
    if exact:
        tiers.append({"tier": "exact", "tier_label": MATCH_TIERS["exact"], "count": len(exact)})
        items.extend(_with_basis(view, "exact", unit, kp, cause, True) for view in exact)
    if general:
        tiers.append({"tier": "general", "tier_label": MATCH_TIERS["general"],
                      "count": len(general)})
        items.extend(_with_basis(view, "general", unit, kp, cause, bool(cause_confirmed))
                     for view in general)
    note = ""
    if not items:
        tiers.append({"tier": "manual", "tier_label": MATCH_TIERS["manual"], "count": 0})
        note = "没有匹配的已发布资源，请人工补充（未审资源不能用于正式推荐）。"
    elif not cause_confirmed:
        note = "错因未确认，只推通用资源；教师确认错因后才会匹配「单元+知识点+确认错因」的专项资源。"
    elif not exact:
        note = "当前没有覆盖这一确认错因的已发布资源，先按通用资源用，其余需人工补充。"
    tier = "manual" if not items else ("exact" if exact else "general")
    return {
        "tier": tier,
        "tier_label": MATCH_TIERS[tier],
        "items": items[:int(limit)],
        "tiers": tiers,
        "note": note,
        "basis": {"unit": unit, "knowledge_point": kp, "cause_code": cause,
                  "cause_confirmed": bool(cause_confirmed)},
    }


def _with_basis(view, tier, unit, kp, cause, cause_confirmed):
    item = dict(view)
    item["tier"] = tier
    item["tier_label"] = MATCH_TIERS[tier]
    item["basis"] = {
        "tier": tier,
        "unit": view["unit"] or unit,
        "knowledge_point": view["knowledge_point"],
        "cause_code": view["cause_code"],
        "cause_confirmed": bool(cause_confirmed),
    }
    if tier == "exact":
        item["basis_text"] = "单元+知识点+确认错因全匹配：%s / %s / %s" % (
            view["unit"] or unit or "未分单元", view["knowledge_point"], view["cause_code"])
    else:
        item["basis_text"] = "单元+知识点通用资源：%s / %s" % (
            view["unit"] or unit or "未分单元", view["knowledge_point"])
    item["reason"] = item["basis_text"]
    item["source_text"] = view["source"] or "未标注来源"
    return item


def answer_context(db, class_id, answer_id):
    """从一份作答读匹配依据：单元 / 知识点 / 确认错因 / 是否已确认。

    四维口径一律走 `grading` 的只读查询（R2-02 的 `effective_rows` / `confirmed_causes` /
    `mistake_of`），本模块不重写四维 SQL；这里只额外读一行 `answers.unit`（R1 列，单元不在
    四维里）。错因只认「当前版本 + cause_state='decided' + 核验仍有效」。
    """
    from . import grading

    class_row = db.query_one("SELECT * FROM classes WHERE id=?", (class_id,))
    if class_row is None:
        raise TeachingResourceError("班级不存在。", "no_class")
    answer = db.query_one(
        "SELECT id, unit FROM answers WHERE id=? AND class_id=?", (answer_id, class_id))
    if answer is None:
        raise TeachingResourceError("作答不存在或不属于这个班级。", "no_answer")
    rows = [row for row in grading.effective_rows(db, class_row)
            if row["answer_id"] == answer_id]
    row = rows[0] if rows else {}
    confirmed = row.get("cause_state") == "decided" and bool(row.get("verified"))
    mistake = grading.mistake_of(db, class_row, answer_id) or {}
    points = [str(item).strip() for item in (mistake.get("knowledge_points") or [])
              if str(item).strip()]
    return {
        "class_id": class_id,
        "answer_id": answer_id,
        "student_id": row.get("student_id"),
        "question_no": row.get("question_no", ""),
        "batch": row.get("batch", ""),
        "unit": answer.get("unit", ""),
        "knowledge_point": points[0] if points else "",
        "knowledge_points": points,
        "classified": bool(mistake.get("classified")),
        "cause_code": row.get("cause_code", "") if confirmed else "",
        "cause_label": row.get("cause_label", "") if confirmed else "",
        "cause_state": row.get("cause_state", "undecided"),
        "cause_confirmed": confirmed,
        "answer_version": (row.get("answer_version") or ""),
        "synthetic": bool(row.get("synthetic")),
    }


def match_for_answer(db, class_id, answer_id, limit=12):
    """作答 → 建议匹配（C 的入口）。错因未确认只推通用，附依据与来源。"""
    context = answer_context(db, class_id, answer_id)
    result = match_resources(db, context["unit"], context["knowledge_point"],
                             context["cause_code"], context["cause_confirmed"], limit=limit)
    result["context"] = context
    return result


def issue_entry_points(db, class_id, student_id=None, limit=20):
    """举一反三的三个入口（E）：个人学情 / 错题详情 / 班级共性问题。

    只读 `grading.confirmed_causes` 与 `grading.mistake_of`（四维口径不在这里重写），
    按「知识点 + 确认错因」聚合成一条问题，并附这条问题能用的已发布资源与练习。
    """
    from . import grading

    class_row = db.query_one("SELECT * FROM classes WHERE id=?", (class_id,))
    if class_row is None:
        raise TeachingResourceError("班级不存在。", "no_class")
    rows = grading.confirmed_causes(db, class_row)
    if student_id is not None:
        rows = [row for row in rows if row["student_id"] == student_id]
    groups = {}
    for row in rows:
        mistake = grading.mistake_of(db, class_row, row["answer_id"]) or {}
        points = [str(item).strip() for item in (mistake.get("knowledge_points") or [])
                  if str(item).strip()]
        point = points[0] if points else ""
        key = (point, row.get("cause_code") or "")
        item = groups.setdefault(key, {
            "knowledge_point": point,
            "classified": bool(mistake.get("classified")),
            "cause_code": row.get("cause_code") or "",
            "cause_label": row.get("cause_label") or "",
            "count": 0,
            "student_ids": set(),
            "answer_ids": [],
            "batches": set(),
        })
        item["count"] += 1
        item["student_ids"].add(row["student_id"])
        item["answer_ids"].append(row["answer_id"])
        if row.get("batch"):
            item["batches"].add(row["batch"])
    out = []
    for item in groups.values():
        item["student_count"] = len(item["student_ids"])
        item["student_ids"] = sorted(item["student_ids"])
        item["batches"] = sorted(item["batches"])
        item["classified"] = bool(item["knowledge_point"])
        item["match"] = match_resources(db, "", item["knowledge_point"], item["cause_code"],
                                        True, limit=limit)
        item["exercises"] = recommend_exercises(db, "", item["knowledge_point"],
                                                item["cause_code"], True)
        item["entry_scope"] = "student" if student_id is not None else "class"
        out.append(item)
    out.sort(key=lambda entry: (-entry["count"], -entry["student_count"],
                                entry["knowledge_point"], entry["cause_code"]))
    scope = "个人学情" if student_id is not None else "班级共性问题"
    unclassified = [entry for entry in out if not entry["knowledge_point"]]
    return {
        "class_id": class_id,
        "student_id": student_id,
        "scope": scope,
        "groups": out,
        "total": sum(entry["count"] for entry in out),
        "note": ("有 %d 组问题还没有知识点分类，需教师补充分类后才能匹配专项资源。"
                 % len(unclassified)) if unclassified else "",
    }


# ---------------------------------------------------------------------------
# D. 三级提示：P1 关键步骤 → P2 规则 → P3 局部示例
# ---------------------------------------------------------------------------
def hint_plan(db, unit, knowledge_point, cause_code="", cause_confirmed=False, levels=None):
    """取一套已发布的三级提示（按 P1→P2→P3 排序），并给出下一级建议。"""
    kp = _text(knowledge_point, 60)
    wanted = [item for item in (levels or C.HINT_LEVEL_ORDER) if item in C.HINT_LEVELS]
    if not wanted:
        wanted = list(C.HINT_LEVEL_ORDER)
    matched = match_resources(db, unit, kp, cause_code, cause_confirmed, kinds=("hint",),
                              limit=50)
    by_level = {}
    for item in matched["items"]:
        level = _text(item["body"].get("level"), 4).upper()
        if level in C.HINT_LEVELS and level not in by_level:
            by_level[level] = {
                "level": level,
                "level_label": C.HINT_LEVELS[level],
                "text": item["body"].get("text", ""),
                "resource_key": item["resource_key"],
                "version": item["version"],
                "tier": item["tier"],
                "basis_text": item["basis_text"],
                "source": item["source"],
            }
    ordered = [by_level[level] for level in C.HINT_LEVEL_ORDER if level in by_level]
    available = [item["level"] for item in ordered]
    # 级别可调：调用方给的 levels 决定「从哪一级开始」；没给的级别按默认 P1→P2→P3 补齐顺序。
    preferred = [level for level in wanted if level in available]
    first = preferred[0] if preferred else (available[0] if available else "P1")
    following = next_hint_level([first])
    if available and following not in available:
        following = available[-1]
    return {
        "levels": ordered,
        "available": available,
        "default_order": list(C.HINT_LEVEL_ORDER),
        "start_level": first,
        "next_level": following if available else "P1",
        "note": matched["note"] or ("" if ordered else "没有可用的三级提示资源，请人工补充。"),
    }


def next_hint_level(provided_levels):
    """按默认 P1→P2→P3 给下一级；已经到 P3 就停在 P3（不再往上加）。"""
    given = {item for item in (provided_levels or []) if item in C.HINT_LEVELS}
    for level in C.HINT_LEVEL_ORDER:
        if level not in given:
            return level
    return C.HINT_LEVEL_ORDER[-1]


def record_hint_usage(db, class_id, student_id, level="P1", planned_level="P1", provided=1,
                      independent_attempt=0, student_fixed=0, full_solution_provided=0,
                      independent_before_solution=0, knowledge_point="", cause_code="",
                      answer_id=None, task_id=None, resource_key="", resource_version=None,
                      note=""):
    """记一次提示使用：级别 / 独立尝试 / 是否自行修正 / 完整解析时机。

    「完整解析必须在独立尝试之后」这条**只认 independent_before_solution 字段**：
    教师提前看答案（`independent_attempt=0` 却给了完整解析）直接拒绝，不写库。
    """
    if level not in C.HINT_LEVELS:
        raise TeachingResourceError("提示级别必须是 P1/P2/P3。", "bad_hint_level")
    if planned_level not in C.HINT_LEVELS:
        raise TeachingResourceError("计划级别必须是 P1/P2/P3。", "bad_hint_level")
    provided = 1 if provided else 0
    independent_attempt = 1 if independent_attempt else 0
    student_fixed = 1 if student_fixed else 0
    full_solution_provided = 1 if full_solution_provided else 0
    independent_before_solution = 1 if independent_before_solution else 0
    if student_fixed and not provided:
        raise TeachingResourceError("没给提示却记「学生自己修正了」，记录不成立。", "inconsistent")
    if full_solution_provided and not independent_attempt:
        raise TeachingResourceError(
            "完整解析必须在学生独立尝试之后；教师提前看答案不算学生完成。", "solution_before_attempt")
    if full_solution_provided and not independent_before_solution:
        raise TeachingResourceError(
            "给了完整解析就要记「解析在独立尝试之后」（independent_before_solution=1）；"
            "提前给解析不算学生完成。", "solution_not_after_attempt")
    if not full_solution_provided and independent_before_solution:
        raise TeachingResourceError(
            "没有提供完整解析，不能记「解析在独立尝试之后」。", "inconsistent")
    if db.query_one("SELECT id FROM students WHERE id=? AND class_id=?",
                    (student_id, class_id)) is None:
        raise TeachingResourceError("学生不存在或不属于这个班级。", "no_student")
    if answer_id is not None:
        if db.query_one("SELECT id FROM answers WHERE id=? AND class_id=? AND student_id=?",
                        (answer_id, class_id, student_id)) is None:
            raise TeachingResourceError("作答不存在或不属于这名学生。", "no_answer")
    if task_id is not None:
        if db.query_one("SELECT id FROM teaching_tasks WHERE id=? AND class_id=?",
                        (task_id, class_id)) is None:
            raise TeachingResourceError("教学任务不存在或不属于这个班级。", "no_task")
    now = now_iso()
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO hint_usages(class_id, student_id, answer_id, task_id, knowledge_point,"
            " cause_code, level, planned_level, provided, independent_attempt, student_fixed,"
            " full_solution_provided, independent_before_solution, resource_key,"
            " resource_version, note, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, answer_id, task_id, _text(knowledge_point, 60),
             _text(cause_code, 60), level, planned_level, provided, independent_attempt,
             student_fixed, full_solution_provided, independent_before_solution,
             _text(resource_key, 160),
             None if resource_version in (None, "") else _int(resource_version),
             _text(note, 300), now, now))
        usage_id = cursor.lastrowid
    return get_hint_usage(db, usage_id)


def get_hint_usage(db, usage_id):
    row = db.query_one("SELECT * FROM hint_usages WHERE id=?", (usage_id,))
    if row is None:
        return None
    out = dict(row)
    out["level_label"] = C.HINT_LEVELS.get(row["level"], row["level"])
    out["planned_level_label"] = C.HINT_LEVELS.get(row["planned_level"], row["planned_level"])
    out["independent_before_solution_label"] = (
        "解析在独立尝试之后" if row["independent_before_solution"] else "解析不在独立尝试之后")
    return out


def list_hint_usages(db, class_id, student_id=None, task_id=None, answer_id=None):
    sql = ["SELECT * FROM hint_usages WHERE class_id=?"]
    params = [class_id]
    if student_id is not None:
        sql.append("AND student_id=?")
        params.append(student_id)
    if task_id is not None:
        sql.append("AND task_id=?")
        params.append(task_id)
    if answer_id is not None:
        sql.append("AND answer_id=?")
        params.append(answer_id)
    sql.append("ORDER BY created_at, id")
    return [get_hint_usage(db, row["id"]) for row in db.query(" ".join(sql), tuple(params))]


def hint_progress(db, class_id, student_id):
    """一名学生的提示使用概览：已给级别、独立尝试次数、修正次数、完整解析时机。"""
    rows = list_hint_usages(db, class_id, student_id)
    provided = [row["level"] for row in rows if row["provided"]]
    return {
        "count": len(rows),
        "provided_levels": provided,
        "next_level": next_hint_level(provided),
        "independent_attempts": sum(1 for row in rows if row["independent_attempt"]),
        "fixed_after_hint": sum(1 for row in rows if row["student_fixed"]),
        "full_solution_given": sum(1 for row in rows if row["full_solution_provided"]),
        "solution_before_attempt": sum(
            1 for row in rows if row["full_solution_provided"]
            and not row["independent_before_solution"]),
        "note": "" if rows else "还没有提示使用记录。",
    }


# ---------------------------------------------------------------------------
# E. 举一反三：三类练习 + 教学任务 + 纸笔任务数据
# ---------------------------------------------------------------------------
def recommend_exercises(db, unit, knowledge_point, cause_code="", cause_confirmed=False,
                        phases=("guided",), per_purpose=2):
    """按三类练习（概念澄清/步骤巩固/迁移应用）推荐已发布练习，逐题给推荐理由。"""
    matched = match_resources(db, unit, knowledge_point, cause_code, cause_confirmed,
                              kinds=("exercise",), limit=200)
    groups = {purpose: [] for purpose in EXERCISE_PURPOSES}
    for item in matched["items"]:
        phase = _text(item["conditions"].get("phase"), 20) or "guided"
        if phase not in set(phases):
            continue
        purpose = item["body"].get("purpose")
        if purpose not in groups:
            continue
        if len(groups[purpose]) >= per_purpose:
            continue
        groups[purpose].append({
            "resource_key": item["resource_key"],
            "version": item["version"],
            "title": item["title"],
            "knowledge_point": item["knowledge_point"],
            "purpose": purpose,
            "purpose_label": EXERCISE_PURPOSES[purpose],
            "difficulty": item["body"].get("difficulty", ""),
            "stem": item["body"].get("stem", ""),
            "answer": item["body"].get("answer", ""),
            "solution": item["body"].get("solution", ""),
            "answer_lines": item["body"].get("answer_lines", 3),
            "phase": phase,
            "phase_label": EXERCISE_PHASES.get(phase, phase),
            "tier": item["tier"],
            "reason": item["basis_text"],
            "source": item["source"],
        })
    missing = [purpose for purpose, items in groups.items() if not items]
    return {
        "groups": groups,
        "counts": {purpose: len(items) for purpose, items in groups.items()},
        "missing": missing,
        "note": matched["note"] or ("" if not missing else "缺少 %s 类练习，需人工补充。"
                                    % "、".join(EXERCISE_PURPOSES[p] for p in missing)),
    }


def list_post_test_exercises(db, unit, knowledge_point, cause_code="", cause_confirmed=False):
    """独立后测资源单独管理：只列 phase='post_test' 的已发布练习，不混进辅导练习选择。"""
    found = recommend_exercises(db, unit, knowledge_point, cause_code, cause_confirmed,
                                phases=("post_test",), per_purpose=50)
    items = [item for items in found["groups"].values() for item in items]
    return {"items": items, "count": len(items),
            "note": "" if items else "还没有独立后测资源。"}


def _task_row(db, task_id):
    return db.query_one("SELECT * FROM teaching_tasks WHERE id=?", (task_id,))


def _task_view(db, row):
    out = dict(row)
    for field in ("basis", "hint_plan", "resource_versions"):
        out[field] = _json_load(row[field], {})
    out["exercise_refs"] = _json_load(row["exercise_refs"], [])
    out["history"] = _json_load(row["history"], [])
    out["status_label"] = C.TASK_STATES.get(row["status"], row["status"])
    out["review_state_label"] = C.TASK_REVIEW_STATES.get(row["review_state"], row["review_state"])
    out["kind_label"] = TASK_KINDS.get(row["kind"], row["kind"])
    return out


def _collect_versions(hint_plan_data, exercise_refs):
    """从所选提示/练习里抽 resource_versions（key→version）。任务引用旧版本就靠它追溯。"""
    versions = {}
    for level in (hint_plan_data or {}).get("levels", []):
        key = _text(level.get("resource_key"), 160)
        if key:
            versions[key] = _int(level.get("version"))
    for ref in exercise_refs or []:
        key = _text(ref.get("resource_key"), 160)
        if key:
            versions[key] = _int(ref.get("version"))
    return versions


def create_task(db, class_id, student_id, kind, title, basis=None, hint_plan=None,
                exercise_refs=None, teacher_note="", created_by=None, status="draft",
                resource_versions=None):
    """创建个人或班级辅导任务：目标对象、关联依据、所选提示与练习、教师说明、资源版本。"""
    if kind not in TASK_KINDS:
        raise TeachingResourceError("任务类型必须是 individual / class。", "bad_kind")
    if status not in C.TASK_STATES:
        raise TeachingResourceError("任务状态必须是 %s 之一。" % "/".join(C.TASK_STATES),
                                    "bad_status")
    if kind == "individual" and not student_id:
        raise TeachingResourceError("个人辅导任务必须指定学生。", "missing_student")
    if kind == "class" and student_id:
        raise TeachingResourceError("班级辅导任务不绑定单个学生。", "extra_student")
    if student_id and db.query_one("SELECT id FROM students WHERE id=? AND class_id=?",
                                   (student_id, class_id)) is None:
        raise TeachingResourceError("学生不存在或不属于这个班级。", "no_student")
    title = _text(title, MAX_TITLE)
    if not title:
        raise TeachingResourceError("任务标题不能为空。", "missing_title")
    versions = dict(resource_versions or {})
    versions.update(_collect_versions(hint_plan, exercise_refs))
    now = now_iso()
    history = [{"at": now, "action": "create", "status": status,
                "note": "创建辅导任务（%s）" % TASK_KINDS[kind]}]
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO teaching_tasks(class_id, student_id, kind, title, basis, hint_plan,"
            " exercise_refs, resource_versions, teacher_note, status, review_state, created_by,"
            " created_at, updated_at, completed_at, history)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,'ok',?,?,?,'',?)",
            (class_id, student_id, kind, title, _json_dump(basis or {}),
             _json_dump(hint_plan or {}), _json_dump(exercise_refs or []),
             _json_dump(versions), _text(teacher_note, 1000), status, created_by, now, now,
             _json_dump(history)))
        task_id = cursor.lastrowid
    return _task_view(db, _task_row(db, task_id))


def get_task(db, task_id):
    row = _task_row(db, task_id)
    return _task_view(db, row) if row else None


def list_tasks(db, class_id, student_id=None, status=None):
    sql = ["SELECT * FROM teaching_tasks WHERE class_id=?"]
    params = [class_id]
    if student_id is not None:
        sql.append("AND student_id=?")
        params.append(student_id)
    if status:
        sql.append("AND status=?")
        params.append(status)
    sql.append("ORDER BY created_at DESC, id DESC")
    return [_task_view(db, row) for row in db.query(" ".join(sql), tuple(params))]


def update_task(db, task_id, title=None, teacher_note=None, hint_plan=None, exercise_refs=None,
                basis=None, status=None, user=None):
    """修改任务：已完成的只能看历史；每次修改都留一条 history。"""
    row = _task_row(db, task_id)
    if row is None:
        raise TeachingResourceError("教学任务 %s 不存在。" % task_id, "no_task")
    if row["status"] not in EDITABLE_TASK_STATES:
        raise TeachingResourceError(
            "任务当前是「%s」，不能直接改内容；请先把它恢复为草稿或重新建任务。"
            % C.TASK_STATES.get(row["status"], row["status"]), "task_locked")
    changed = {}
    if title is not None:
        new_title = _text(title, MAX_TITLE)
        if not new_title:
            raise TeachingResourceError("任务标题不能为空。", "missing_title")
        changed["title"] = new_title
    if teacher_note is not None:
        changed["teacher_note"] = _text(teacher_note, 1000)
    if basis is not None:
        changed["basis"] = dict(basis or {})
    if hint_plan is not None:
        changed["hint_plan"] = dict(hint_plan or {})
    if exercise_refs is not None:
        changed["exercise_refs"] = list(exercise_refs or [])
    if status is not None:
        if status not in C.TASK_STATES:
            raise TeachingResourceError("任务状态非法：%r" % status, "bad_status")
        changed["status"] = status
    if not changed:
        return _task_view(db, row)
    versions = _json_load(row["resource_versions"], {})
    if "hint_plan" in changed or "exercise_refs" in changed:
        versions.update(_collect_versions(changed.get("hint_plan", _json_load(row["hint_plan"], {})),
                                         changed.get("exercise_refs",
                                                     _json_load(row["exercise_refs"], []))))
    history = _json_load(row["history"], [])
    now = now_iso()
    history.append({"at": now, "action": "update", "fields": sorted(changed),
                    "status": changed.get("status", row["status"]),
                    "note": "修改任务内容或状态"})
    sets = []
    params = []
    for field in ("title", "teacher_note", "status"):
        if field in changed:
            sets.append("%s=?" % field)
            params.append(changed[field])
    for field in ("basis", "hint_plan", "resource_versions"):
        value = versions if field == "resource_versions" else changed.get(field)
        if value is not None:
            sets.append("%s=?" % field)
            params.append(_json_dump(value))
    if "exercise_refs" in changed:
        sets.append("exercise_refs=?")
        params.append(_json_dump(changed["exercise_refs"]))
    sets.extend(["history=?", "updated_at=?"])
    params.extend([_json_dump(history), now, task_id])
    with db.tx() as conn:
        conn.execute("UPDATE teaching_tasks SET %s WHERE id=?" % ", ".join(sets), tuple(params))
    return _task_view(db, _task_row(db, task_id))


def set_task_status(db, task_id, status, user=None, note=""):
    """状态流转（草稿/已安排/已完成/已取消），手工记录完成时间。"""
    if status not in C.TASK_STATES:
        raise TeachingResourceError("任务状态必须是 %s 之一。" % "/".join(C.TASK_STATES),
                                    "bad_status")
    row = _task_row(db, task_id)
    if row is None:
        raise TeachingResourceError("教学任务 %s 不存在。" % task_id, "no_task")
    now = now_iso()
    history = _json_load(row["history"], [])
    history.append({"at": now, "action": "status", "from": row["status"], "to": status,
                    "by": _user_id(user), "note": _text(note, 200)})
    with db.tx() as conn:
        conn.execute(
            "UPDATE teaching_tasks SET status=?, completed_at=?, history=?, updated_at=?"
            " WHERE id=?",
            (status, now if status == "completed" else row["completed_at"],
             _json_dump(history), now, task_id))
    return _task_view(db, _task_row(db, task_id))


def complete_task(db, task_id, user=None, note=""):
    return set_task_status(db, task_id, "completed", user=user, note=note or "教师手工记录完成")


def mark_task_review(db, task_id, reason="basis_changed", note=""):
    """R2-INV-09：依据变化只打 needs_review；status 不动、不自动改派、不自动完成。"""
    row = _task_row(db, task_id)
    if row is None:
        raise TeachingResourceError("教学任务 %s 不存在。" % task_id, "no_task")
    plan = C.apply_event("task_basis_changed", {"task": {"review": row["review_state"]}},
                         {"basis_changed": True})
    if "R2-INV-09" not in set(plan["rules"]):
        raise TeachingResourceError("失效规则 R2-INV-09 缺失，拒绝静默改任务状态。", "rule_missing")
    if plan["state"]["task"]["review"] == "needs_review" and row["review_state"] != "needs_review":
        _touch_task_review(db, row, reason, note or "任务依据变化，待复核")
    view = _task_view(db, _task_row(db, task_id))
    view["rule"] = "R2-INV-09"
    view["actions"] = list(plan["actions"])
    return view


def resolve_task_review(db, task_id, note=""):
    """复核完成：把 needs_review 恢复成 ok（任务本身不动）。"""
    row = _task_row(db, task_id)
    if row is None:
        raise TeachingResourceError("教学任务 %s 不存在。" % task_id, "no_task")
    if row["review_state"] == "ok":
        return _task_view(db, row)
    history = _json_load(row["history"], [])
    now = now_iso()
    history.append({"at": now, "action": "review_resolved", "status": row["status"],
                    "note": _text(note, 200) or "教师复核完成"})
    with db.tx() as conn:
        conn.execute(
            "UPDATE teaching_tasks SET review_state='ok', history=?, updated_at=? WHERE id=?",
            (_json_dump(history), now, task_id))
    return _task_view(db, _task_row(db, task_id))


def task_resource_versions(db, task_id):
    """任务引用的资源版本明细：key → 版本、当前状态、是否已变（可追溯）。"""
    task = get_task(db, task_id)
    if task is None:
        raise TeachingResourceError("教学任务 %s 不存在。" % task_id, "no_task")
    out = []
    for key, version in sorted(task["resource_versions"].items()):
        row = _resource_row(db, key)
        current = _int(row["current_version"]) if row else 0
        vrow = _version_row(db, key, version) if row else None
        out.append({
            "resource_key": key,
            "title": row["title"] if row else "（资源已不存在）",
            "version": _int(version),
            "current_version": current,
            "state": (vrow or {}).get("state", ""),
            "state_label": C.RESOURCE_STATES.get((vrow or {}).get("state", ""), ""),
            "changed": bool(row) and current != _int(version),
        })
    return {"task_id": task_id, "review_state": task["review_state"],
            "review_state_label": task["review_state_label"], "items": out}


# ---------------------------------------------------------------------------
# 导入（工具 tools/import_resources.py 调这里）
# ---------------------------------------------------------------------------
def import_resources(db, payload, created_by=None, submit=True, dry_run=False):
    """把一份资源包写进库：新资源建档、同内容跳过、内容变了出新版本。

    `submit=True` 时新建/改版后立刻送审（首批真实资源一律「待审核」，未审不得用于推荐）。
    """
    unit = _text((payload or {}).get("unit"), 60)
    kp = _text((payload or {}).get("knowledge_point"), 60)
    cause = _text((payload or {}).get("cause_code"), 60)
    source = _text((payload or {}).get("source"), 60) or "首批资源草稿"
    items = (payload or {}).get("resources")
    if not isinstance(items, list) or not items:
        raise TeachingResourceError("导入包必须带非空的 resources 列表。", "empty_payload")
    report = {"created": [], "updated": [], "skipped": [], "submitted": [], "errors": []}
    for index, item in enumerate(items, 1):
        try:
            data = dict(item or {})
            data.setdefault("unit", unit)
            data.setdefault("knowledge_point", kp)
            data.setdefault("cause_code", cause)
            data.setdefault("source", source)
            normalized = normalize_resource(data)
            key = normalized["resource_key"]
            existing = _resource_row(db, key)
            if existing is None:
                if dry_run:
                    report["created"].append(key)
                    continue
                create_resource(db, normalized, created_by=created_by)
                if submit:
                    submit_for_review(db, key, note="导入送审", user=created_by)
                    report["submitted"].append(key)
                report["created"].append(key)
                continue
            view = _resource_view(db, existing)
            same = (view["body"] == normalized["body"]
                    and view["conditions"] == normalized["conditions"]
                    and view["title"] == normalized["title"])
            if same:
                report["skipped"].append(key)
                continue
            if dry_run:
                report["updated"].append(key)
                continue
            update_resource(db, key, body=normalized["body"],
                            conditions=normalized["conditions"], title=normalized["title"],
                            note="导入更新", created_by=created_by)
            state = _resource_row(db, key)
            if submit and state and state["status"] == "draft":
                submit_for_review(db, key, note="导入更新送审", user=created_by)
                report["submitted"].append(key)
            elif state and state["status"] == "pending_review":
                # 已发布资源改内容时 update_resource 已直接置为待审核，这里不重复送审。
                report["submitted"].append(key)
            report["updated"].append(key)
        except TeachingResourceError as exc:
            report["errors"].append({"index": index, "title": (item or {}).get("title", ""),
                                     "code": exc.code, "message": exc.message})
    return report


def resource_stats(db):
    """资源库概览（界面首页用）：按状态/类型计数，附未审与合成测试资源的条数。"""
    rows = db.query("SELECT kind, status, source, COUNT(*) AS n FROM teaching_resources"
                    " GROUP BY kind, status, source")
    by_status = {state: 0 for state in C.RESOURCE_STATES}
    by_kind = {kind: 0 for kind in C.RESOURCE_KINDS}
    synthetic = 0
    total = 0
    for row in rows:
        count = _int(row["n"])
        total += count
        by_status[row["status"]] = by_status.get(row["status"], 0) + count
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + count
        if is_synthetic(row["source"]):
            synthetic += count
    return {"total": total, "by_status": by_status, "by_kind": by_kind,
            "synthetic": synthetic, "official_ready": by_status.get("published", 0) - synthetic}
