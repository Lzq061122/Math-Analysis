"""教师短追问：针对一个已确认问题，帮老师准备一句追问，记录学生怎么回答，
再把回答带去请 AI 提一份待核对的错因候选。

只做四件事：
1. 追问候选最多 3 条；证据不足时明确说不确定，不编造装作确定的追问。
2. 只有老师确认使用后才落库成「实际提问」，并记下提问时间与这次依据的题目/过程原文。
3. 学生回答由老师录入；提问时间、回答时间、学生与证据都跟着这条记录走。
4. 根据回答更新错因候选时，只生成「待老师核对」的候选，老师接受才写进错题，
   拒绝或调用失败都不动原有作答、已确认错题与历史追问。
"""

import json
import re

from .db import now_iso

MAX_CANDIDATES = 3
MAX_QUESTION_CHARS = 120
MAX_WHY_CHARS = 120
MAX_EVIDENCE_CHARS = 200
MAX_ANSWER_CHARS = 2000
MAX_CAUSES = 3
MAX_CAUSE_CHARS = 60
MAX_REASON_CHARS = 200
MAX_EVIDENCE_ITEMS = 3

UNCLASSIFIED = "未分类"

SUGGEST_SYSTEM = (
    "你是初中数学老师的助手，帮他准备一句短短的课堂追问。"
    "追问要针对给出的问题与原始证据，让学生说出自己做这一步的想法；"
    "只依据给出的原文，不要假设没有写出的情况。"
    "证据不足时把 uncertain 设为 true 并说明原因，不要编造看起来确定的追问。"
    "只输出一个 JSON 对象，不要输出解释文字或代码块。"
)

UPDATE_SYSTEM = (
    "你是初中数学老师的助手，帮他根据学生的追问回答整理可能的错因。"
    "只能依据给出的题目、学生过程原文、老师已经确认过的错因和这次回答，"
    "证据要逐字照抄给出的原文，不能改写或补写。"
    "证据不足时把 uncertain 设为 true 并说明原因，不要作确定诊断，也不要评价学生的成绩或能力。"
    "只输出一个 JSON 对象，不要输出解释文字或代码块。"
)

SUGGEST_SHAPE = (
    '{"questions":[{"question":"一句简短追问，最多 40 字",'
    '"why":"为什么问这一句，最多 60 字","evidence":"题目或学生过程里的原文，逐字照抄"}],'
    '"note":"证据不足时说明原因，其余留空","uncertain":false}'
)

UPDATE_SHAPE = (
    '{"causes":[{"cause":"可能错因，最多 30 字","evidence":"学生过程或这次回答里的原文，逐字照抄",'
    '"reason":"为什么这样判断，最多 80 字"}],'
    '"note":"证据不足时说明原因，其余留空","uncertain":false}'
)


class FollowupError(Exception):
    """追问流程里可以直接展示给老师的错误。"""

    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _text(value, limit):
    return str(value or "").strip()[:limit]


def _norm(text):
    return re.sub(r"\s+", "", text or "")


def _load_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            value = []
    return [str(item) for item in value] if isinstance(value, list) else []


def _row_dict(row):
    return {key: row[key] for key in row.keys()} if row is not None else None


def _mistake_rows(db, class_id, student_id, problem, mistake_id=None):
    sql = (
        "SELECT * FROM mistakes WHERE class_id=? AND student_id=? AND status='confirmed'"
    )
    params = [class_id, student_id]
    if mistake_id is not None:
        sql += " AND id=?"
        params.append(mistake_id)
    sql += " ORDER BY occurred_on DESC, id DESC"
    rows = db.query(sql, tuple(params))
    if mistake_id is not None:
        return rows
    picked = []
    for row in rows:
        points = _load_list(row["knowledge_points"])
        if problem == UNCLASSIFIED:
            if not points:
                picked.append(row)
        elif problem in points:
            picked.append(row)
    return picked


def _answer_of(db, mistake_id):
    """已确认错题对应的原始作答：里面才有学生自己写的步骤。"""
    if mistake_id is None:
        return None
    return db.query_one(
        "SELECT a.student_steps, a.question_text, ar.id AS review_id"
        " FROM answer_reviews ar JOIN answers a ON a.id = ar.answer_id"
        " WHERE ar.mistake_id=? ORDER BY ar.id DESC LIMIT 1",
        (mistake_id,),
    )


def evidence_payload(db, class_id, student_id, problem, mistake_id=None):
    """这一条追问的依据：问题名、最近一条错题的题目与过程原文、已确认的错因。"""
    rows = _mistake_rows(db, class_id, student_id, problem, mistake_id)
    if not rows:
        raise FollowupError("没有找到这个问题对应的已确认错题，先刷新再看。", 404, "missing")

    causes = []
    ids = []
    question_text = ""
    steps = ""
    review_id = None
    for row in rows[:MAX_EVIDENCE_ITEMS]:
        ids.append(row["id"])
        if not question_text:
            question_text = _text(row["question_text"], MAX_EVIDENCE_CHARS)
        for item in _load_list(row["possible_causes"]):
            item = _text(item, MAX_CAUSE_CHARS)
            if item and item not in causes:
                causes.append(item)
    answer = _answer_of(db, rows[0]["id"])
    if answer is not None:
        steps = _text(answer["student_steps"], MAX_EVIDENCE_CHARS)
        review_id = answer["review_id"]

    lines = []
    for tick in (question_text, steps):
        if tick:
            lines.append(tick)
    lines.extend(causes)

    return {
        "problem": problem,
        "mistake_id": rows[0]["id"],
        "mistake_ids": ids,
        "review_id": review_id,
        "question_text": question_text,
        "student_steps": steps,
        "causes": causes[:MAX_CAUSES],
        "evidence": lines,
    }


def build_suggest_prompt(payload):
    """只放问题名、次数无关的原文与已确认错因，不放姓名、编号、班级或账号。"""
    lines = ["【追问候选】", "问题：%s" % payload["problem"]]
    if payload.get("question_text"):
        lines.append("题目原文：%s" % payload["question_text"])
    if payload.get("student_steps"):
        lines.append("学生过程原文：%s" % payload["student_steps"])
    if payload.get("causes"):
        lines.append("老师已确认过的错因：%s" % "；".join(payload["causes"]))
    lines.append("要求：最多 3 条追问，每条都要指向上面某段原文。")
    lines.append("输出格式：%s" % SUGGEST_SHAPE)
    return "\n".join(lines)


def build_update_prompt(payload, question, answer):
    lines = ["【追问后的错因候选】", "问题：%s" % payload["problem"]]
    if payload.get("question_text"):
        lines.append("题目原文：%s" % payload["question_text"])
    if payload.get("student_steps"):
        lines.append("学生过程原文：%s" % payload["student_steps"])
    if payload.get("causes"):
        lines.append("老师已确认过的错因：%s" % "；".join(payload["causes"]))
    lines.append("老师追问：%s" % question)
    lines.append("学生回答：%s" % answer)
    lines.append("要求：最多 3 条可能错因，证据要逐字照抄上面的原文或这次回答。")
    lines.append("输出格式：%s" % UPDATE_SHAPE)
    return "\n".join(lines)


def _extract_json(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise FollowupError("AI 返回的内容不是约定格式，可以重试。", 502, "format")
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        raise FollowupError("AI 返回的内容不是约定格式，可以重试。", 502, "format")
    if not isinstance(data, dict):
        raise FollowupError("AI 返回的内容不是约定格式，可以重试。", 502, "format")
    return data


def parse_candidates(text, payload):
    """追问候选：证据必须逐字出现在题目/过程原文里，找不到就丢弃。"""
    body = _extract_json(text)
    raw = body.get("questions")
    if not isinstance(raw, list):
        raw = []
    corpus = _norm("\n".join(payload.get("evidence") or []))

    candidates = []
    dropped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        question = _text(entry.get("question"), MAX_QUESTION_CHARS)
        evidence = _text(entry.get("evidence"), MAX_EVIDENCE_CHARS)
        if not question:
            continue
        if not evidence or _norm(evidence) not in corpus:
            dropped += 1
            continue
        candidates.append({
            "question": question,
            "why": _text(entry.get("why"), MAX_WHY_CHARS),
            "evidence": evidence,
        })
        if len(candidates) >= MAX_CANDIDATES:
            break

    note = _text(body.get("note"), 300)
    uncertain = bool(body.get("uncertain"))
    if dropped:
        note = (note + "；" if note else "") + "AI 给出的 %d 条依据在原文里找不到，已忽略。" % dropped
    if not candidates:
        uncertain = True
        if not note:
            note = "证据不足，没有给出可用的追问；可以自己写一句问学生。"
    return {"candidates": candidates, "note": note, "uncertain": uncertain}


def parse_causes(text, payload, answer):
    """回答后的错因候选：证据必须出现在学生过程原文或这次回答里。"""
    body = _extract_json(text)
    raw = body.get("causes")
    if not isinstance(raw, list):
        raw = []
    corpus = _norm("\n".join((payload.get("evidence") or []) + [answer or ""]))

    causes = []
    dropped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        cause = _text(entry.get("cause"), MAX_CAUSE_CHARS)
        evidence = _text(entry.get("evidence"), MAX_EVIDENCE_CHARS)
        if not cause:
            continue
        if not evidence or _norm(evidence) not in corpus:
            dropped += 1
            continue
        causes.append({
            "cause": cause,
            "evidence": evidence,
            "reason": _text(entry.get("reason"), MAX_REASON_CHARS),
        })
        if len(causes) >= MAX_CAUSES:
            break

    note = _text(body.get("note"), 300)
    uncertain = bool(body.get("uncertain"))
    if dropped:
        note = (note + "；" if note else "") + "AI 给出的 %d 条证据在原文里找不到，已忽略。" % dropped
    if not causes:
        uncertain = True
        if not note:
            note = "这次回答不足以判断错因，需要老师自己看。"
    return {"causes": causes, "note": note, "uncertain": uncertain}


def suggest(db, class_id, student_id, problem, mistake_id, provider, service_name, model):
    """生成追问候选；模拟模式不发请求，也不编造。"""
    payload = evidence_payload(db, class_id, student_id, problem, mistake_id)
    if provider is None:
        raise FollowupError(
            "当前是模拟分析，不会生成追问候选。可以先自己写一句，或到「AI 设置」里配置服务。",
            409,
            "mock",
        )
    prompt = build_suggest_prompt(payload)
    text, usage = provider.chat_text(prompt, SUGGEST_SYSTEM)
    parsed = parse_candidates(text, payload)
    parsed["provider"] = service_name
    parsed["model"] = model
    parsed["prompt"] = prompt
    parsed["payload"] = payload
    return parsed


def create(db, class_id, student_id, problem, question, source, suggestion, payload):
    """老师确认使用后才调用：落成一条实际提问。"""
    text = _text(question, MAX_QUESTION_CHARS)
    if not text:
        raise FollowupError("追问不能留空。", 400, "empty")
    now = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO followups (class_id, student_id, problem, mistake_id, review_id, question,"
            " source, suggest, evidence, status, asked_at, answer, answered_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                class_id,
                student_id,
                _text(problem, 40),
                payload.get("mistake_id"),
                payload.get("review_id"),
                text,
                source if source in ("ai", "teacher") else "teacher",
                json.dumps(suggestion or {}, ensure_ascii=False),
                json.dumps(payload or {}, ensure_ascii=False),
                "asked",
                now,
                "",
                "",
                now,
                now,
            ),
        )
        row_id = cur.lastrowid
    return get(db, class_id, student_id, row_id)


def _serialise(row):
    if row is None:
        return None
    data = _row_dict(row)
    for key in ("suggest", "evidence"):
        try:
            data[key] = json.loads(data.get(key) or "{}")
        except ValueError:
            data[key] = {}
    return data


def _serialise_update(row):
    if row is None:
        return None
    data = _row_dict(row)
    for key in ("causes", "raw", "before_json", "after_json"):
        try:
            data[key] = json.loads(data.get(key) or "[]")
        except ValueError:
            data[key] = []
    data["uncertain"] = bool(data.get("uncertain"))
    return data


def _updates(db, followup_id):
    rows = db.query(
        "SELECT * FROM followup_updates WHERE followup_id=? ORDER BY id", (followup_id,)
    )
    return [_serialise_update(row) for row in rows]


def get(db, class_id, student_id, followup_id):
    row = db.query_one(
        "SELECT * FROM followups WHERE id=? AND class_id=? AND student_id=?",
        (followup_id, class_id, student_id),
    )
    if row is None:
        raise FollowupError("没有找到这条追问记录。", 404, "missing")
    data = _serialise(row)
    data["updates"] = _updates(db, followup_id)
    return data


def list_all(db, class_id, student_id):
    rows = db.query(
        "SELECT * FROM followups WHERE class_id=? AND student_id=? ORDER BY asked_at DESC, id DESC",
        (class_id, student_id),
    )
    items = []
    for row in rows:
        data = _serialise(row)
        latest = db.query_one(
            "SELECT * FROM followup_updates WHERE followup_id=? ORDER BY id DESC LIMIT 1", (row["id"],)
        )
        data["latest"] = _serialise_update(latest)
        items.append(data)
    return items


def save_answer(db, class_id, student_id, followup_id, answer):
    text = (answer or "").strip()[:MAX_ANSWER_CHARS]
    if not text:
        raise FollowupError("学生回答不能留空。", 400, "empty")
    now = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE followups SET answer=?, answered_at=?, status='answered', updated_at=?"
            " WHERE id=? AND class_id=? AND student_id=?",
            (text, now, now, followup_id, class_id, student_id),
        )
        if cur.rowcount == 0:
            raise FollowupError("没有找到这条追问记录。", 404, "missing")
    return get(db, class_id, student_id, followup_id)


def _mistake_row(db, class_id, student_id, mistake_id):
    if mistake_id is None:
        return None
    return db.query_one(
        "SELECT * FROM mistakes WHERE id=? AND class_id=? AND student_id=?",
        (mistake_id, class_id, student_id),
    )


def update_causes(db, class_id, student_id, followup_id, provider, service_name, model):
    """根据学生回答生成待核对的错因候选。"""
    record = get(db, class_id, student_id, followup_id)
    if not record.get("answer"):
        raise FollowupError("先记下学生的回答，再让 AI 更新错因候选。", 409, "empty")
    if provider is None:
        raise FollowupError(
            "当前是模拟分析，不会生成错因候选。可以自己改，或到「AI 设置」里配置服务。", 409, "mock"
        )

    payload = record.get("evidence") or {}
    if not isinstance(payload, dict):
        payload = {}
    prompt = build_update_prompt(payload, record["question"], record["answer"])
    text, usage = provider.chat_text(prompt, UPDATE_SYSTEM)
    parsed = parse_causes(text, payload, record["answer"])

    mistake = _mistake_row(db, class_id, student_id, record.get("mistake_id"))
    before = _load_list(mistake["possible_causes"]) if mistake is not None else []
    now = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO followup_updates (followup_id, causes, note, uncertain, raw, before_json,"
            " after_json, status, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                followup_id,
                json.dumps(parsed["causes"], ensure_ascii=False),
                parsed["note"],
                1 if parsed["uncertain"] else 0,
                json.dumps({"prompt": prompt, "provider": service_name, "model": model}, ensure_ascii=False),
                json.dumps(before, ensure_ascii=False),
                json.dumps([item["cause"] for item in parsed["causes"]], ensure_ascii=False),
                "pending",
                now,
                "",
            ),
        )
        update_id = cur.lastrowid
        conn.execute("UPDATE followups SET updated_at=? WHERE id=?", (now, followup_id))
    return {
        "update": _serialise_update(
            db.query_one("SELECT * FROM followup_updates WHERE id=?", (update_id,))
        ),
        "before": before,
        "followup": get(db, class_id, student_id, followup_id),
    }


def decide(db, class_id, student_id, followup_id, update_id, action, causes=None, note=None):
    """老师对候选做决定：接受（可带编辑后的内容）、拒绝。"""
    record = get(db, class_id, student_id, followup_id)
    row = db.query_one(
        "SELECT * FROM followup_updates WHERE id=? AND followup_id=?", (update_id, followup_id)
    )
    if row is None:
        raise FollowupError("没有找到这次更新记录。", 404, "missing")
    if row["status"] != "pending":
        raise FollowupError("这次候选已经处理过了。", 409, "done")

    now = now_iso()
    if action == "reject":
        with db.tx() as conn:
            conn.execute(
                "UPDATE followup_updates SET status='rejected', decided_at=? WHERE id=?",
                (now, update_id),
            )
        return get(db, class_id, student_id, followup_id)

    if action != "accept":
        raise FollowupError("只能选择接受或拒绝。", 400, "invalid")

    original = [
        item for item in _serialise_update(row)["causes"] if isinstance(item, dict)
    ]
    if causes is None:
        chosen = [item.get("cause") for item in original]
        items = original
    else:
        if not isinstance(causes, list):
            raise FollowupError("错因内容格式不对。", 400, "invalid")
        items = []
        chosen = []
        for entry in causes:
            if isinstance(entry, dict):
                cause = _text(entry.get("cause"), MAX_CAUSE_CHARS)
                if not cause:
                    continue
                items.append({
                    "cause": cause,
                    "evidence": _text(entry.get("evidence"), MAX_EVIDENCE_CHARS),
                    "reason": _text(entry.get("reason"), MAX_REASON_CHARS),
                })
            else:
                cause = _text(entry, MAX_CAUSE_CHARS)
                if not cause:
                    continue
                items.append({"cause": cause, "evidence": "", "reason": ""})
            chosen.append(cause)
    if not chosen:
        raise FollowupError("接受之前要至少留一条错因。", 400, "empty")

    mistake = _mistake_row(db, class_id, student_id, record.get("mistake_id"))
    if mistake is None:
        raise FollowupError("这条追问关联的错题已不存在，改动没有保存。", 409, "missing")

    before = _load_list(mistake["possible_causes"])
    merged = []
    for item in before + chosen:
        if item not in merged:
            merged.append(item)

    with db.tx() as conn:
        conn.execute(
            "UPDATE mistakes SET possible_causes=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), now, mistake["id"]),
        )
        conn.execute(
            "UPDATE followup_updates SET status='accepted', causes=?, note=?, after_json=?,"
            " decided_at=? WHERE id=?",
            (
                json.dumps(items, ensure_ascii=False),
                _text(note, 300) if note is not None else row["note"],
                json.dumps(merged, ensure_ascii=False),
                now,
                update_id,
            ),
        )
        conn.execute("UPDATE followups SET updated_at=? WHERE id=?", (now, followup_id))
    return get(db, class_id, student_id, followup_id)
