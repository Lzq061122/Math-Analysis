"""个人学情：把一名学生的已确认错题按知识点汇总，算近期辅导优先级，再请 AI 给候选建议。

只做四件事：
1. 汇总只取 status='confirmed' 的错题，按知识点分组；没有知识点的归入「未分类」。
2. 优先级按错题发生日期加权：最近 30 天每次 3 分、31–90 天每次 2 分、超过 90 天每次 1 分。
   日期缺失的单列不参与加权；未来日期按数据异常提示、不计入。
3. 老师主动点击后才发请求；请求里只有问题名、次数、分值和题目/过程原文，
   不含学生姓名、学校编号、班级名和账号名。
4. AI 给的证据必须能在给出的原文里找到，找不到就丢弃；老师改过并保存的内容不会被重新生成覆盖。
"""

import hashlib
import json
import re
from datetime import date, datetime

from .db import now_iso
from .stats import confirmed_mistakes

WEIGHT_RECENT = 3
WEIGHT_MID = 2
WEIGHT_OLD = 1
RECENT_DAYS = 30
MID_DAYS = 90

MAX_FOCUS = 3
MAX_NAME_CHARS = 40
MAX_WHY_CHARS = 160
MAX_HOW_CHARS = 240
MAX_EVIDENCE_CHARS = 200
MAX_ITEM_EVIDENCE = 3

UNCLASSIFIED = "未分类"

SYSTEM_PROMPT = (
    "你是初中数学老师助手，帮他安排个别辅导。"
    "你只能依据给出的问题、出现次数、加权分数和题目/学生过程原文判断，不得假设没有写出的信息。"
    "建议最多聚焦 %d 个问题，按加权分数从高到低说明排序依据；引用证据要逐字照抄给出的原文，"
    "不能改写、概括或补写。证据不足时把 uncertain 设为 true 并说明原因，"
    "不要判断学生已经掌握或退步，也不要给出成绩或能力评价。"
    "只输出一个 JSON 对象，不要输出解释文字或代码块。" % MAX_FOCUS
)

PROMPT_MARKER = "【个人学情辅导建议】"

OUTPUT_SHAPE = (
    '{"focus":[{"problem":"必须与给出的问题名完全一致","why":"排序依据，最多 60 字",'
    '"evidence":"题目或学生过程里的原文，逐字照抄","how":"具体可执行的辅导方式，最多 100 字"}],'
    '"note":"证据不足或不确定时说明原因，其余留空","uncertain":false}'
)


class TutorError(Exception):
    """辅导建议流程里可以直接展示给老师的错误。"""

    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _as_date(text):
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def weight_for(days):
    """按期距给权重；超出规则范围（未来日期）返回 None。"""
    if days < 0:
        return None
    if days <= RECENT_DAYS:
        return WEIGHT_RECENT
    if days <= MID_DAYS:
        return WEIGHT_MID
    return WEIGHT_OLD


def _list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            value = []
    return [str(item) for item in value] if isinstance(value, list) else []


def _text(value, limit):
    return str(value or "").strip()[:limit]


def _evidence_of(row):
    """一条错题里能引用给 AI 的原文：题目、学生过程、错误表现、可能原因。"""
    parts = []
    for key in ("question_text", "solution_text"):
        text = _text(row.get(key), MAX_EVIDENCE_CHARS)
        if text:
            parts.append(text)
    for key in ("observed_errors", "possible_causes"):
        parts.extend(_list(row.get(key)))
    return parts


def _mistake_brief(row):
    return {
        "id": row["id"],
        "occurred_on": row.get("occurred_on") or "",
        "question_text": _text(row.get("question_text"), MAX_EVIDENCE_CHARS),
        "evidence": _evidence_of(row),
    }


def summary(db, class_id, student_id, today=None):
    """汇总一名学生的已确认错题，返回分组、加权分、日期异常与指纹。"""
    day = today or date.today()
    rows = confirmed_mistakes(db, class_id, student_id)
    groups = {}
    undated = []
    anomalies = []
    for row in rows:
        occurred = _as_date(row.get("occurred_on"))
        if not row.get("occurred_on"):
            undated.append(_mistake_brief(row))
            continue
        if occurred is None:
            anomalies.append(dict(_mistake_brief(row), reason="日期无法识别"))
            continue
        if occurred > day:
            anomalies.append(dict(_mistake_brief(row), reason="日期在今天之后"))
            continue
        days = (day - occurred).days
        weight = weight_for(days)
        points = [p for p in _list(row.get("knowledge_points")) if p.strip()]
        names = points or [UNCLASSIFIED]
        for name in dict.fromkeys(names):
            item = groups.setdefault(name, {"name": name, "count": 0, "weight": 0,
                                            "last_on": "", "mistake_ids": [], "evidence": []})
            item["count"] += 1
            item["weight"] += weight
            if occurred.isoformat() > item["last_on"]:
                item["last_on"] = occurred.isoformat()
            item["mistake_ids"].append(row["id"])
            if len(item["evidence"]) < MAX_ITEM_EVIDENCE:
                item["evidence"].append(_mistake_brief(row))

    items = []
    for item in groups.values():
        item["kind"] = "unclassified" if item["name"] == UNCLASSIFIED else "knowledge_point"
        item["mistake_ids"].sort()
        items.append(item)
    items.sort(key=lambda x: (x["kind"] == "unclassified", -x["weight"], -x["count"], x["name"]))

    fingerprint = _fingerprint(items)
    return {
        "student_id": student_id,
        "class_id": class_id,
        "today": day.isoformat(),
        "rule": "最近 %d 天每次 %d 分；%d–%d 天每次 %d 分；超过 %d 天每次 %d 分。加权分只用于辅导排序。"
                % (RECENT_DAYS, WEIGHT_RECENT, RECENT_DAYS + 1, MID_DAYS, WEIGHT_MID,
                   MID_DAYS, WEIGHT_OLD),
        "confirmed": len(rows),
        "counted": len(rows) - len(undated) - len(anomalies),
        "items": items,
        "undated": undated,
        "anomalies": anomalies,
        "fingerprint": fingerprint,
    }


def _fingerprint(items):
    payload = [
        [item["name"], item["count"], item["weight"], item["last_on"], item["mistake_ids"]]
        for item in items
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_prompt(data):
    """拼出发给服务商的内容：只有数学信息与分值，没有任何身份字段。"""
    lines = [
        PROMPT_MARKER,
        "下面是一名学生已确认错题的汇总。请给出优先辅导 1–%d 个问题的具体做法。" % MAX_FOCUS,
        "计分规则：%s" % data["rule"],
    ]
    for index, item in enumerate(data["items"][:5], 1):
        lines.append(
            "%d. 问题「%s」：出现 %d 次，加权 %d 分，最近一次 %s"
            % (index, item["name"], item["count"], item["weight"], item["last_on"] or "（无）")
        )
        for ev in item["evidence"]:
            for part in ev["evidence"][:3]:
                lines.append("   原文：%s" % part)
    if data["undated"]:
        lines.append("另有 %d 条错题缺少日期，不参与加权，仅供参考。" % len(data["undated"]))
    lines.append("输出格式：%s" % OUTPUT_SHAPE)
    return "\n".join(lines)


def _extract_json(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise TutorError("AI 返回的内容不是约定格式，可以重试或手动填写。", 502, "format")
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        raise TutorError("AI 返回的内容不是约定格式，可以重试或手动填写。", 502, "format")
    if not isinstance(data, dict):
        raise TutorError("AI 返回的内容不是约定格式，可以重试或手动填写。", 502, "format")
    return data


def _norm(text):
    return re.sub(r"\s+", "", text or "")


def parse_suggestion(text, data):
    """把模型输出变成可信的辅导建议：问题名必须来自汇总，证据必须逐字出现在原文里。"""
    payload = _extract_json(text)
    raw_focus = payload.get("focus")
    if not isinstance(raw_focus, list):
        raw_focus = []
    by_name = {item["name"]: item for item in data["items"]}
    corpus = {}
    for item in data["items"]:
        parts = []
        for ev in item["evidence"]:
            parts.extend(ev["evidence"])
        corpus[item["name"]] = _norm("\n".join(parts))

    focus = []
    dropped_problem = 0
    dropped_evidence = 0
    for entry in raw_focus:
        if not isinstance(entry, dict):
            continue
        name = _text(entry.get("problem"), MAX_NAME_CHARS)
        item = by_name.get(name)
        if item is None:
            dropped_problem += 1
            continue
        evidence = _text(entry.get("evidence"), MAX_EVIDENCE_CHARS)
        if not evidence or _norm(evidence) not in corpus[name]:
            dropped_evidence += 1
            continue
        focus.append({
            "problem": name,
            "weight": item["weight"],
            "count": item["count"],
            "why": _text(entry.get("why"), MAX_WHY_CHARS),
            "evidence": evidence,
            "how": _text(entry.get("how"), MAX_HOW_CHARS),
        })
        if len(focus) >= MAX_FOCUS:
            break

    note = _text(payload.get("note"), 300)
    uncertain = bool(payload.get("uncertain"))
    if dropped_problem:
        note = (note + "；" if note else "") + "AI 给出的 %d 个问题名不在汇总里，已忽略。" % dropped_problem
    if dropped_evidence:
        note = (note + "；" if note else "") + "AI 给出的 %d 条证据在原文里找不到，已忽略。" % dropped_evidence
    if not focus:
        uncertain = True
        if not note:
            note = "AI 没有给出有原文依据的辅导建议；证据不足，需要老师自己判断。"
    elif uncertain and not note:
        note = "AI 自己也不确定，仅供参考。"
    return {"focus": focus, "note": note, "uncertain": uncertain}


def render(parsed, data):
    """把解析后的建议排成老师可编辑的文字。"""
    lines = ["【优先辅导问题】"]
    if not parsed["focus"]:
        lines.append("这次没有可用的建议：" + (parsed["note"] or "证据不足。"))
        return "\n".join(lines)
    for index, entry in enumerate(parsed["focus"], 1):
        lines.append(
            "%d. %s（出现 %d 次，加权 %d 分）" % (index, entry["problem"], entry["count"], entry["weight"])
        )
        if entry["why"]:
            lines.append("   排序依据：%s" % entry["why"])
        if entry["evidence"]:
            lines.append("   引用证据：%s" % entry["evidence"])
        if entry["how"]:
            lines.append("   建议做法：%s" % entry["how"])
    if parsed["note"]:
        lines.extend(["", "说明：" + parsed["note"]])
    lines.extend(["", "以上为 AI 候选建议，由老师核对修改后使用；不是学生成绩或能力评级。"])
    return "\n".join(lines)


def _row(db, class_id, student_id):
    return db.query_one(
        "SELECT * FROM tutor_advice WHERE class_id=? AND student_id=?", (class_id, student_id)
    )


def _serialise(db, row, fingerprint):
    advice = None
    if row:
        try:
            advice = json.loads(row["evidence"] or "{}").get("advice")
        except ValueError:
            advice = None
        advice = {
            "id": row["id"],
            "content": row["content"],
            "ai_content": row["ai_content"],
            "teacher_edited": bool(row["teacher_edited"]),
            "mode": row["mode"],
            "generated_at": row["generated_at"],
            "stale": bool(row["fingerprint"] and fingerprint and row["fingerprint"] != fingerprint),
        }
    return advice


def load(db, class_id, student_id):
    data = summary(db, class_id, student_id)
    row = _row(db, class_id, student_id)
    return {"summary": data, "advice": _serialise(db, row, data["fingerprint"])}


def generate(db, class_id, student_id, provider, service_name, model):
    """发起一次生成。provider 为 None 表示模拟模式：不发请求，也不编造建议。"""
    data = summary(db, class_id, student_id)
    if not data["items"]:
        raise TutorError("这名学生还没有已确认的错题，暂时不能生成辅导建议。", 409, "empty")
    if provider is None:
        raise TutorError(
            "当前是模拟分析，不会生成辅导建议。请先在「AI 设置」里配置服务后再生成。", 409, "mock"
        )

    prompt = build_prompt(data)
    text, usage = provider.chat_text(prompt, SYSTEM_PROMPT)
    parsed = parse_suggestion(text, data)
    content = render(parsed, data)
    now = now_iso()
    evidence = {
        "advice": parsed,
        "items": [
            {"name": item["name"], "count": item["count"], "weight": item["weight"],
             "last_on": item["last_on"], "mistake_ids": item["mistake_ids"]}
            for item in data["items"]
        ],
        "undated": [ev["id"] for ev in data["undated"]],
        "anomalies": [ev["id"] for ev in data["anomalies"]],
        "rule": data["rule"],
        "today": data["today"],
    }
    with db.tx() as conn:
        conn.execute("DELETE FROM tutor_advice WHERE student_id=?", (student_id,))
        conn.execute(
            "INSERT INTO tutor_advice(class_id, student_id, content, ai_content, evidence,"
            " fingerprint, teacher_edited, mode, generated_at, updated_at)"
            " VALUES (?,?,?,?,?,?,0,?,?,?)",
            (
                class_id, student_id, content, content,
                json.dumps(evidence, ensure_ascii=False), data["fingerprint"],
                service_name or "", now, now,
            ),
        )
    row = _row(db, class_id, student_id)
    return {"summary": summary(db, class_id, student_id),
            "advice": _serialise(db, row, data["fingerprint"]), "usage": usage}


def save(db, class_id, student_id, content):
    """老师保存修改：只改这份建议，不动错题与作答；还没有建议时允许老师自己写一份。"""
    row = _row(db, class_id, student_id)
    text = (content or "").strip()
    if not text:
        raise TutorError("辅导建议不能留空；不需要时可以点「重新生成」。", 400, "empty")
    data = summary(db, class_id, student_id)
    if not row:
        if not data["items"]:
            raise TutorError("这名学生还没有已确认的错题，暂时不能保存辅导建议。", 409, "empty")
        now = now_iso()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO tutor_advice(class_id, student_id, content, ai_content, evidence,"
                " fingerprint, teacher_edited, mode, generated_at, updated_at)"
                " VALUES (?,?,?,'','{}',?,1,'',?,?)",
                (class_id, student_id, text, data["fingerprint"], now, now),
            )
        return {"summary": data,
                "advice": _serialise(db, _row(db, class_id, student_id), data["fingerprint"])}
    with db.tx() as conn:
        conn.execute(
            "UPDATE tutor_advice SET content=?, teacher_edited=1, updated_at=? WHERE id=?",
            (text, now_iso(), row["id"]),
        )
    return {"summary": data,
            "advice": _serialise(db, _row(db, class_id, student_id), data["fingerprint"])}
