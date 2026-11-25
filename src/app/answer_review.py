"""作答审核：老师对单条作答主动发起 AI 分析，AI 只给候选错因与步骤证据。

流程上只做三件事：
1. 分析前把「要发什么」交给界面确认；请求里只有题号、题目、单元、标准解法和学生步骤，
   不含学生姓名、学校编号、班级名和账号名。
2. AI 返回的候选项先落成一条待核对的 answer_reviews 记录（status=pending），
   不是错题，也不进任何统计；证据在原文里找不到的候选项一律丢掉，不替模型补写。
3. 老师编辑后确认，才通过 records.insert_mistake 生成一条正式错题并回写 mistake_id；
   拒绝则只改审核记录本身。原始 answers 行在这里从头到尾只读。
"""

import json
import re

from . import records
from .db import now_iso

MAX_CAUSES = 3
MAX_CAUSE_CHARS = 60
MAX_EVIDENCE_CHARS = 200
MAX_REASON_CHARS = 200

# 固定提示词：强调证据必须逐字取自学生步骤，宁可说不确定，也不许补写。
SYSTEM_PROMPT = (
    "你是初中数学老师助手，只分析七年级一元一次方程的解题过程。"
    "你只能依据给出的题目、标准解法和学生步骤判断，不得假设学生写了没写的内容。"
    "每条候选错因必须附一条学生步骤里的原文作为证据，证据要逐字照抄，不能改写、概括或补写。"
    "如果学生的步骤没有明显错误，或信息不足以判断，就把 uncertain 设为 true 并说明原因。"
    "只输出一个 JSON 对象，不要输出解释文字或代码块。"
)

PROMPT_MARKER = "【作答分析】"

OUTPUT_SHAPE = (
    '{"causes":[{"cause":"可能错因，最多 20 字","evidence":"学生步骤中的原文，逐字照抄",'
    '"reasoning":"这条证据为什么支持该错因，最多 60 字"}],'
    '"uncertain":false,"note":"信息不足或未发现明确错误时说明原因，其余情况留空"}'
)


class AnswerReviewError(Exception):
    """审核流程里可以直接展示给老师的错误。"""

    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def build_prompt(answer):
    """拼出发给服务商的内容：只有数学信息，没有任何身份字段。"""
    lines = [
        PROMPT_MARKER,
        "请分析下面这份学生作答，判断可能的错因，并指出对应的学生步骤证据。",
        "题号：%s" % (answer["question_no"] or "（未填）"),
        "单元：%s" % (answer["unit"] or "（未填）"),
        "题目：%s" % (answer["question_text"] or "（未填）"),
        "标准解法：%s" % (answer["standard_solution"] or "（未填）"),
        "学生步骤：%s" % (answer["student_steps"] or "（未提供）"),
        "输出格式：%s" % OUTPUT_SHAPE,
    ]
    if answer["missing"]:
        lines.append("注意：老师把这条作答标为缺测，学生没有提供步骤，不要猜测错因。")
    return "\n".join(lines)


def _norm(text):
    """去掉所有空白，用来判断证据是否真的出现在原文里。"""
    return re.sub(r"\s+", "", text or "")


def _extract_json(text):
    """从模型输出里取出 JSON 对象；允许前后有代码块或说明文字。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise AnswerReviewError("AI 返回的内容不是约定格式，可以重试或手动填写。", 502, "format")
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        raise AnswerReviewError("AI 返回的内容不是约定格式，可以重试或手动填写。", 502, "format")
    if not isinstance(data, dict):
        raise AnswerReviewError("AI 返回的内容不是约定格式，可以重试或手动填写。", 502, "format")
    return data


def _clean_cause(item):
    if not isinstance(item, dict):
        return None
    cause = str(item.get("cause") or "").strip()
    evidence = str(item.get("evidence") or "").strip()
    reasoning = str(item.get("reasoning") or item.get("reason") or "").strip()
    if not cause:
        return None
    return {
        "cause": cause[:MAX_CAUSE_CHARS],
        "evidence": evidence[:MAX_EVIDENCE_CHARS],
        "reasoning": reasoning[:MAX_REASON_CHARS],
    }


def parse_suggestion(text, student_steps):
    """把模型输出变成可信的候选错因：证据必须逐字出现在学生步骤里。"""
    data = _extract_json(text)
    raw_causes = data.get("causes")
    if not isinstance(raw_causes, list):
        raw_causes = []
    steps = _norm(student_steps)
    causes = []
    dropped = 0
    for item in raw_causes:
        cause = _clean_cause(item)
        if not cause:
            continue
        if not cause["evidence"] or _norm(cause["evidence"]) not in steps:
            dropped += 1
            continue
        causes.append(cause)
        if len(causes) >= MAX_CAUSES:
            break

    note = str(data.get("note") or "").strip()[:300]
    uncertain = bool(data.get("uncertain"))
    if dropped:
        note = (note + "；" if note else "") + "AI 给出的 %d 条证据在原始步骤里找不到，已忽略。" % dropped
    if not causes:
        uncertain = True
        if not note:
            note = "AI 没有给出有原文依据的错因；信息不足或步骤中没有明显错误。"
    elif uncertain and not note:
        note = "AI 自己也不确定，供老师参考。"
    return {"causes": causes, "note": note, "uncertain": uncertain}


def _answer(db, class_id, answer_id):
    row = db.query_one("SELECT * FROM answers WHERE id=? AND class_id=?", (answer_id, class_id))
    if not row:
        raise AnswerReviewError("这条作答不存在，或不属于当前班级。", 404, "not_found")
    return row


def _serialise_answer(row):
    return {
        "id": row["id"],
        "record_key": row["record_key"],
        "batch": row["batch"],
        "question_no": row["question_no"],
        "question_text": row["question_text"],
        "unit": row["unit"],
        "standard_solution": row["standard_solution"],
        "student_steps": row["student_steps"],
        "occurred_on": row["occurred_on"],
        "missing": int(row["missing"] or 0),
        "missing_reason": row["missing_reason"],
        "student_name": row["student_name"],
        "student_id": row["student_id"],
    }


def _serialise(db, row):
    answer = db.query_one("SELECT * FROM answers WHERE id=?", (row["answer_id"],))
    return {
        "id": row["id"],
        "answer_id": row["answer_id"],
        "status": row["status"],
        "provider": row["provider"],
        "model": row["model"],
        "causes": json.loads(row["causes"] or "[]"),
        "note": row["note"],
        "uncertain": bool(row["uncertain"]),
        "mistake_id": row["mistake_id"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "answer": _serialise_answer(answer) if answer else None,
    }


def _store(db, class_id, answer, status, provider, model, suggestion, raw, fingerprint):
    """把一次分析写成待核对记录；同一条作答的旧待核对记录先清掉，避免堆叠。"""
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM answer_reviews WHERE answer_id=? AND status='pending'", (answer["id"],)
        )
        cur = conn.execute(
            "INSERT INTO answer_reviews(class_id, student_id, answer_id, status, provider, model,"
            " causes, note, uncertain, raw, answer_fingerprint, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                class_id,
                answer["student_id"],
                answer["id"],
                status,
                provider,
                model,
                json.dumps(suggestion["causes"], ensure_ascii=False),
                suggestion["note"],
                1 if suggestion["uncertain"] else 0,
                raw or "",
                fingerprint,
                now_iso(),
                now_iso(),
            ),
        )
        review_id = cur.lastrowid
    return db.query_one("SELECT * FROM answer_reviews WHERE id=?", (review_id,))


def analyse(db, class_id, answer_id, provider, service_name, model):
    """发起一次分析。provider 为 None 表示模拟模式：不发请求，只记录本机判断。"""
    answer = _answer(db, class_id, answer_id)
    fingerprint = answer["fingerprint"] or ""

    if int(answer["missing"] or 0):
        suggestion = {
            "causes": [],
            "note": "这条作答标为缺测（%s），没有学生步骤可核对，不做错因推断。" % (answer["missing_reason"] or "未写原因"),
            "uncertain": True,
        }
        row = _store(db, class_id, answer, "pending", "本地判断", "", suggestion, "", fingerprint)
        return _serialise(db, row)

    if provider is None:
        suggestion = {
            "causes": [],
            "note": "模拟分析不会判断对错，也不会发送任何内容；本次只验证流程。",
            "uncertain": True,
        }
        row = _store(db, class_id, answer, "pending", service_name or "模拟分析", "", suggestion, "", fingerprint)
        return _serialise(db, row)

    prompt = build_prompt(answer)
    text, usage = provider.chat_text(prompt, SYSTEM_PROMPT)
    parsed = parse_suggestion(text, answer["student_steps"])
    row = _store(
        db, class_id, answer, "pending", service_name, model or "", parsed, text, fingerprint
    )
    data = _serialise(db, row)
    data["usage"] = usage
    return data


def update(db, class_id, review_id, causes, note):
    """老师修改候选错因：只改待核对记录，不碰原始作答。"""
    row = db.query_one("SELECT * FROM answer_reviews WHERE id=? AND class_id=?", (review_id, class_id))
    if not row:
        raise AnswerReviewError("这次分析记录不存在。", 404, "not_found")
    if row["status"] != "pending":
        raise AnswerReviewError("这条建议已经处理过，不能再修改。", 409, "conflict")

    clean = []
    for item in causes or []:
        cause = _clean_cause(item)
        if cause:
            clean.append(cause)
    clean = clean[:MAX_CAUSES]
    with db.tx() as conn:
        conn.execute(
            "UPDATE answer_reviews SET causes=?, note=?, uncertain=?, updated_at=? WHERE id=? AND status='pending'",
            (
                json.dumps(clean, ensure_ascii=False),
                str(note or "").strip()[:300],
                1 if (not clean or row["uncertain"]) else 0,
                now_iso(),
                review_id,
            ),
        )
    return _serialise(db, db.query_one("SELECT * FROM answer_reviews WHERE id=?", (review_id,)))


def _mistake_result(review, answer):
    causes = json.loads(review["causes"] or "[]")
    return {
        "question_text": answer["question_text"],
        "solution_text": answer["student_steps"],
        "knowledge_points": [answer["unit"]] if answer["unit"] else [],
        "error_type": "",
        "observed_errors": [item["evidence"] for item in causes if item.get("evidence")],
        "possible_causes": [item["cause"] for item in causes],
        "advice_text": review["note"],
        "insufficient": bool(review["uncertain"]),
        "insufficient_reason": review["note"] if review["uncertain"] else "",
        "likely_correct": False,
        "mode": "answer",
        "answer_id": answer["id"],
        "review_id": review["id"],
        "causes": causes,
    }


def confirm(db, class_id, review_id, user_id):
    """老师确认：生成一条正式错题并回写关联；同一条作答只允许确认一次。"""
    with db.tx() as conn:
        review = db.query_one("SELECT * FROM answer_reviews WHERE id=? AND class_id=?", (review_id, class_id))
        if not review:
            raise AnswerReviewError("这次分析记录不存在。", 404, "not_found")
        if review["status"] == "confirmed":
            raise AnswerReviewError("这条建议已经确认过，不会重复生成错题。", 409, "conflict")
        if review["status"] != "pending":
            raise AnswerReviewError("这条建议已经拒绝，不能再确认。", 409, "conflict")
        answer = db.query_one("SELECT * FROM answers WHERE id=?", (review["answer_id"],))
        if not answer:
            raise AnswerReviewError("来源作答已被删除，不能生成错题。", 409, "conflict")
        if (answer["fingerprint"] or "") != review["answer_fingerprint"]:
            raise AnswerReviewError("这条作答的内容已经更新，请重新分析后再确认。", 409, "conflict")
        done = db.query_one(
            "SELECT id FROM answer_reviews WHERE answer_id=? AND status='confirmed'", (answer["id"],)
        )
        if done:
            raise AnswerReviewError("这条作答已经生成过错题，不会重复生成。", 409, "conflict")

        result = _mistake_result(review, answer)
        meta = {
            "source_type": "作答分析",
            "source_detail": "%s · 题%s" % (answer["batch"] or "未命名批次", answer["question_no"] or "-"),
            "note": "",
            "occurred_on": answer["occurred_on"] or "",
        }
        mistake_id = records.insert_mistake(db, class_id, answer["student_id"], "", "", 0, 0, meta, result)
        conn.execute(
            "UPDATE mistakes SET status='confirmed', confirmed_at=?, confirmed_by=?, updated_at=? WHERE id=?",
            (now_iso(), user_id, now_iso(), mistake_id),
        )
        conn.execute(
            "UPDATE answer_reviews SET status='confirmed', mistake_id=?, decided_at=?, updated_at=? WHERE id=?",
            (mistake_id, now_iso(), now_iso(), review_id),
        )
    return _serialise(db, db.query_one("SELECT * FROM answer_reviews WHERE id=?", (review_id,)))


def reject(db, class_id, review_id):
    with db.tx() as conn:
        review = db.query_one("SELECT * FROM answer_reviews WHERE id=? AND class_id=?", (review_id, class_id))
        if not review:
            raise AnswerReviewError("这次分析记录不存在。", 404, "not_found")
        if review["status"] == "confirmed":
            raise AnswerReviewError("这条建议已经确认并生成错题，不能拒绝。", 409, "conflict")
        if review["status"] == "rejected":
            return _serialise(db, review)
        conn.execute(
            "UPDATE answer_reviews SET status='rejected', decided_at=?, updated_at=? WHERE id=?",
            (now_iso(), now_iso(), review_id),
        )
    return _serialise(db, db.query_one("SELECT * FROM answer_reviews WHERE id=?", (review_id,)))


def detail(db, class_id, answer_id):
    """作答详情 + 最近一次分析（待核对或已处理），没有分析时 review 为 None。"""
    answer = _answer(db, class_id, answer_id)
    row = db.query_one(
        "SELECT * FROM answer_reviews WHERE answer_id=? ORDER BY id DESC LIMIT 1", (answer["id"],)
    )
    return {"answer": _serialise_answer(answer), "review": _serialise(db, row) if row else None}


def listing(db, class_id, batch="", limit=200):
    """按作答列出审核状态，供「作答记录」窗口查看。"""
    sql = (
        "SELECT a.*, r.status AS review_status, r.id AS review_id, r.uncertain AS review_uncertain,"
        " r.mistake_id AS review_mistake_id"
        " FROM answers a LEFT JOIN answer_reviews r ON r.id=("
        "SELECT id FROM answer_reviews WHERE answer_id=a.id ORDER BY id DESC LIMIT 1)"
        " WHERE a.class_id=?"
    )
    params = [class_id]
    if batch:
        sql += " AND a.batch=?"
        params.append(batch)
    sql += " ORDER BY a.imported_at DESC, a.id DESC LIMIT ?"
    params.append(int(limit))
    rows = db.query(sql, params)
    items = []
    for row in rows:
        item = _serialise_answer(row)
        item["review_status"] = row["review_status"] or ""
        item["review_id"] = row["review_id"]
        item["review_uncertain"] = bool(row["review_uncertain"])
        item["mistake_id"] = row["review_mistake_id"]
        items.append(item)
    batches = [row["batch"] for row in db.query(
        "SELECT DISTINCT batch FROM answers WHERE class_id=? ORDER BY batch", (class_id,))]
    return {"total": len(items), "items": items, "batches": batches}
