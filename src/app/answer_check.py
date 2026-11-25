"""作答核对：两轴状态、队列、保存回执与追问记录（作答核对契约 §1–§3）。

为什么要和 answer_reviews 分开：answer_reviews 是模型给出的候选错因，answer_checks 是老师对
「这一份作答」的处理记录。两者混在一起会把「原题做错没有」和「老师判断原因没有」算成同一件事，
而 A3 要求这两条轴各自成立、各自统计，所以这里单独建表、单独读写。

版本口径：answer_checks.answer_version 只有等于 answers.fingerprint 时才算当前结论。重导后旧结论
自动退出队列、详情与统计——所有查询都带这个条件，不做任何清理批处理，历史记录原样留在库里。
"""

import json
import os

from . import equation_steps
from .db import now_iso

DEMO_SOURCE = "合成演示"  # 与 demo_answers.DEMO_SOURCE 一致；只用来判定「是不是合成演示作答」
MAX_CANDIDATES = 3

PROGRESS_PENDING = "pending"
PROGRESS_CHECKED = "checked"
CAUSE_UNDECIDED = "undecided"
CAUSE_DECIDED = "decided"

PROGRESS_LABELS = {PROGRESS_PENDING: "待核对", PROGRESS_CHECKED: "已核对"}
CAUSE_LABELS = {CAUSE_UNDECIDED: "原因待判断", CAUSE_DECIDED: "教师已判断"}

DEFAULT_QUESTION = "你当时是怎么展开这个括号的？"

# demo_answers 被裁掉时的兜底说明；正常路径一律用 demo_answers.CAUSES_NOTE。
FALLBACK_CAUSES_NOTE = "尚无已审错因库可用，保留待判断。"


class AnswerCheckError(Exception):
    """可以直接展示给老师的错误；status/code 与接口错误体一致。"""

    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# --------------------------------------------------------------------------
# 通用辅助
# --------------------------------------------------------------------------
def _json_list(raw):
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _fault(name):
    """测试注入点：MISTAKE_FAULT 里列出的环节模拟故障；老师正常使用时不会触发。"""
    if name in (os.environ.get("MISTAKE_FAULT") or "").split(","):
        raise RuntimeError("模拟故障：%s" % name)


def _demo_module():
    """预置候选与预置定位的唯一来源是 demo_answers；导入失败时安静退化，不拖垮详情接口。"""
    try:
        from . import demo_answers
    except ImportError:
        return None
    return demo_answers


def table_ready(db):
    """只读判断新表是否已建好；桌面端打开旧库时没有这两张表，统计块要能安全退化。"""
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return "answer_checks" in names and "answer_followups" in names


def _require_tables(db):
    if not table_ready(db):
        raise AnswerCheckError(
            "当前数据目录还缺少「作答核对」的数据表，请先通过网页版重新打开一次以完成升级。",
            500,
            "schema_missing",
        )


def assignment_name(batch):
    return (batch or "").strip() or "未命名批次"


def resolve_batch(db, class_id, batch=""):
    """当前作业：优先用传入的批次；没传就取最近导入的那条作答所属批次。

    imported_at 相同（同一秒导入）时取 id 较大者，保证任何调用顺序都得到同一个批次。
    """
    batch = (batch or "").strip()
    if batch:
        return batch
    row = db.query_one(
        "SELECT batch FROM answers WHERE class_id=? ORDER BY imported_at DESC, id DESC LIMIT 1",
        (class_id,),
    )
    return row["batch"] if row else ""


def _student_label(row):
    name = (row.get("student_name") or "").strip()
    if not name:
        name = (row.get("roster_name") or "").strip()
    return name or "学生"


def _missing(row):
    return bool(int(row.get("missing") or 0))


def _demo_record(answer):
    """合成演示作答的预置数据（含写死的预设定位与预置问句）。

    只在 `answers.source` 是「合成演示」且题干、步骤都没被改过时才认，避免老师重导后
    还拿旧演示结果冒充当前结论。
    """
    module = _demo_module()
    if module is None:
        return None
    if answer.get("source") != getattr(module, "DEMO_SOURCE", DEMO_SOURCE):
        return None
    for record in module.build().get("answers") or ():
        if record.get("record_key") != answer.get("record_key"):
            continue
        if (record.get("question_text") or "") != (answer.get("question_text") or ""):
            return None
        if (record.get("student_steps") or "") != (answer.get("student_steps") or ""):
            return None
        return record
    return None


def _locate(answer):
    """真实导入作答一律走规则定位；预置定位只给「合成演示」用，二者不能互相冒充。"""
    record = _demo_record(answer)
    if record and isinstance(record.get("locate"), dict):
        return dict(record["locate"])
    return equation_steps.locate(
        answer.get("question_text") or "",
        answer.get("standard_solution") or "",
        answer.get("student_steps") or "",
        missing=_missing(answer),
        missing_reason=answer.get("missing_reason") or "",
    )


def candidates_for(answer):
    """薄封装：候选错因的单一来源是 demo_answers.preset_causes，这里不再另写一份。"""
    module = _demo_module()
    if module is None:
        return []
    return [dict(item) for item in module.preset_causes(answer.get("question_text") or "")][:MAX_CANDIDATES]


def causes_note():
    """候选来源说明：有候选与没候选都用同一句（契约 §3.2），强调这不是已审错因库检索。"""
    module = _demo_module()
    if module is not None and getattr(module, "CAUSES_NOTE", ""):
        return module.CAUSES_NOTE
    return FALLBACK_CAUSES_NOTE


def _default_question(answer):
    """默认问句固定；合成演示里带专门问句的（案例 A 等）用预置那条。"""
    record = _demo_record(answer)
    if record:
        question = (record.get("followup") or {}).get("question") or ""
        if question:
            return question
    return DEFAULT_QUESTION


def _answer(db, class_id, answer_id):
    row = db.query_one(
        "SELECT a.*, s.name AS roster_name FROM answers a"
        " LEFT JOIN students s ON s.id = a.student_id"
        " WHERE a.id=? AND a.class_id=?",
        (answer_id, class_id),
    )
    if not row:
        raise AnswerCheckError("这条作答不存在，或不属于当前班级。", 404, "not_found")
    return row


def current_check(db, answer):
    """当前结论只认版本一致的那一条；旧版本的记录保留，但不参与任何判断。"""
    return db.query_one(
        "SELECT * FROM answer_checks WHERE answer_id=? AND answer_version=?"
        " ORDER BY id DESC LIMIT 1",
        (answer["id"], answer["fingerprint"] or ""),
    )


def _has_stale(db, answer):
    return bool(
        db.scalar(
            "SELECT COUNT(*) FROM answer_checks WHERE answer_id=? AND answer_version<>?",
            (answer["id"], answer["fingerprint"] or ""),
        )
    )


def queue_rows(db, class_id, batch):
    """待核对队列：该批次内、当前版本还没有有效 check 的作答，按 id 升序。

    总览的「待核对数」、详情的「第几份」、保存后的「下一份」都用这一份队列，
    避免三处各算一套导致数字对不上。
    """
    return db.query(
        "SELECT a.id, a.batch, a.question_no, a.question_text, a.standard_solution,"
        " a.student_steps, a.missing, a.missing_reason, a.student_name, a.fingerprint,"
        " s.name AS roster_name"
        " FROM answers a LEFT JOIN students s ON s.id = a.student_id"
        " WHERE a.class_id=? AND a.batch=? AND NOT EXISTS ("
        "   SELECT 1 FROM answer_checks c"
        "   WHERE c.answer_id=a.id AND c.answer_version=a.fingerprint)"
        " ORDER BY a.id ASC",
        (class_id, batch),
    )


def _batch_counts(db, class_id, batch):
    row = db.query_one(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN a.missing=1 THEN 1 ELSE 0 END), 0) AS missing,"
        " COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM answer_checks c"
        "   WHERE c.answer_id=a.id AND c.answer_version=a.fingerprint)"
        "   THEN 1 ELSE 0 END), 0) AS checked"
        " FROM answers a WHERE a.class_id=? AND a.batch=?",
        (class_id, batch),
    ) or {"total": 0, "missing": 0, "checked": 0}
    total = int(row["total"] or 0)
    checked = int(row["checked"] or 0)
    return {
        "batch": batch,
        "name": assignment_name(batch),
        "total": total,
        "pending": total - checked,
        "checked": checked,
        "missing": int(row["missing"] or 0),
    }


# --------------------------------------------------------------------------
# §3.1 课前总览
# --------------------------------------------------------------------------
HEADLINES = {
    "first_bad": "去括号：是规则没掌握，还是这次漏写？",
    "correct": "这份作答每一步都等价，可以直接核对。",
    "missing_steps": "这份只写了最终答案，先看缺少步骤。",
    "missing": "这份标为缺测，没有可核对的过程。",
    "out_of_scope": "这份超出本次核对范围，已转人工。",
}


def overview(db, class_row, batch=""):
    class_id = class_row["id"]
    _require_tables(db)
    batch = resolve_batch(db, class_id, batch)
    answers_total = db.scalar("SELECT COUNT(*) FROM answers WHERE class_id=?", (class_id,)) or 0

    assignments = []
    for row in db.query(
        "SELECT batch, MAX(imported_at) AS last_at FROM answers WHERE class_id=? GROUP BY batch",
        (class_id,),
    ):
        item = _batch_counts(db, class_id, row["batch"])
        assignments.append({
            "batch": item["batch"],
            "name": item["name"],
            "total": item["total"],
            "pending": item["pending"],
            "checked": item["checked"],
            "_last_at": row["last_at"] or "",
        })
    assignments.sort(key=lambda item: (item["_last_at"], item["batch"]), reverse=True)
    for item in assignments:
        item.pop("_last_at", None)

    assignment = _batch_counts(db, class_id, batch)
    if not answers_total or assignment["total"] == 0:
        # 班级还没有作答，或选中的批次里一条都没有：都按「尚未导入作答」处理，
        # 不要显示成「已核对完」，那会让老师以为数据已经处理过了。
        state = "no_answers"
    elif assignment["pending"] > 0:
        state = "has_pending"
    else:
        state = "all_checked"

    pending_rows = queue_rows(db, class_id, batch)
    next_item = None
    if pending_rows:
        first = pending_rows[0]
        located = _locate(first)
        next_item = {
            "answer_id": first["id"],
            "student_label": _student_label(first),
            "question_no": first["question_no"],
            "headline": HEADLINES.get(located["status"], HEADLINES["out_of_scope"]),
        }

    checks_current = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint",
        (class_id,),
    ) or 0
    undecided = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint AND c.cause_state=?",
        (class_id, CAUSE_UNDECIDED),
    ) or 0

    return {
        "state": state,
        "assignment": assignment,
        "assignments": assignments,
        "next": next_item,
        "counts": {
            "answers": answers_total,
            "checks_current": checks_current,
            "undecided_cause": undecided,
        },
    }


# --------------------------------------------------------------------------
# §3.2 单份详情
# --------------------------------------------------------------------------
def _judgement(check, stale=False):
    if not check:
        return {
            "progress": PROGRESS_PENDING,
            "cause_state": CAUSE_UNDECIDED,
            "progress_label": PROGRESS_LABELS[PROGRESS_PENDING],
            "cause_label": CAUSE_LABELS[CAUSE_UNDECIDED],
            "observed_step": "",
            "cause_code": "",
            "cause_label_text": "",
            "teacher_note": "",
            "saved_at": "",
            "stale": bool(stale),
        }
    return {
        "progress": check["progress"],
        "cause_state": check["cause_state"],
        "progress_label": PROGRESS_LABELS.get(check["progress"], "待核对"),
        "cause_label": CAUSE_LABELS.get(check["cause_state"], "原因待判断"),
        "observed_step": check["observed_step"],
        "cause_code": check["cause_code"],
        "cause_label_text": check["cause_label"],
        "teacher_note": check["teacher_note"],
        "saved_at": check["updated_at"],
        "stale": bool(stale),
    }


def _queue_state(db, answer):
    rows = queue_rows(db, answer["class_id"], answer["batch"])
    ids = [row["id"] for row in rows]
    if answer["id"] in ids:
        position = ids.index(answer["id"]) + 1
        total = len(ids)
    else:
        # 已核对过的作答仍按它离开队列时的位置显示；total 把这一份自己算回来，
        # 所以保存前后详情里的「第几份 / 共几份」不会跳。
        position = sum(1 for item in ids if item < answer["id"]) + 1
        total = len(ids) + 1
    return {
        "position": position,
        "total": total,
        "is_last": position == total,
        "batch": answer["batch"],
    }


def _followup_state(db, answer, question):
    row = db.query_one(
        "SELECT * FROM answer_followups WHERE answer_id=? AND answer_version=?"
        " ORDER BY id DESC LIMIT 1",
        (answer["id"], answer["fingerprint"] or ""),
    )
    return {
        "question": (row["question"] if row and row["question"] else question),
        "recorded": bool(row and (row["answer_text"] or "").strip()),
        "answer_text": row["answer_text"] if row else "",
        "asked_at": row["asked_at"] if row else "",
    }


def detail(db, class_row, answer_id):
    _require_tables(db)
    answer = _answer(db, class_row["id"], answer_id)
    locate = _locate(answer)
    check = current_check(db, answer)
    question = _default_question(answer)

    candidates = candidates_for(answer)[:MAX_CANDIDATES]

    return {
        "answer": {
            "id": answer["id"],
            "record_key": answer["record_key"],
            "batch": answer["batch"],
            "question_no": answer["question_no"],
            "question_text": answer["question_text"],
            "unit": answer["unit"],
            "standard_solution": answer["standard_solution"],
            "student_steps": answer["student_steps"],
            "occurred_on": answer["occurred_on"],
            "missing": int(answer["missing"] or 0),
            "missing_reason": answer["missing_reason"],
            "student_label": _student_label(answer),
            "data_source": "synthetic" if answer["source"] == DEMO_SOURCE else "imported",
            "data_source_label": "合成演示数据" if answer["source"] == DEMO_SOURCE else "真实导入",
            "version": answer["fingerprint"] or "",
        },
        "locate": locate,
        "candidates": candidates,
        "causes_source": "preset" if candidates else "none",
        "causes_note": causes_note(),
        "judgement": _judgement(check, stale=_has_stale(db, answer) and check is None),
        "followup": _followup_state(db, answer, question),
        "queue": _queue_state(db, answer),
    }


# --------------------------------------------------------------------------
# §3.3 保存
# --------------------------------------------------------------------------
def _force_undecided(answer, status):
    """缺测、正确、只有答案、超范围都不该挂原因：能保存处理进度，但不产生错题与原因。"""
    return _missing(answer) or status in ("correct", "missing_steps", "out_of_scope")


def _first_bad_index(value, locate):
    if locate["status"] != "first_bad":
        return None
    if value is None or value == "":
        return locate["first_bad_index"]
    try:
        index = int(value)
    except (TypeError, ValueError):
        raise AnswerCheckError("错步位置不正确，请重新选择。", 400, "bad_index")
    if index < 0 or index >= len(locate["lines"]):
        raise AnswerCheckError("错步位置不正确，请重新选择。", 400, "bad_index")
    return index


def _followup_source(question, answer):
    return "preset" if question == _default_question(answer) else "teacher"


def save(db, class_row, user_id, answer_id, body):
    """保存一次核对：任何非预期异常都收敛成 500 save_failed，前端要能留在当前页重试。

    版本冲突、参数不合法这类「业务上说得清」的错误仍按 AnswerCheckError 原样抛出。
    """
    try:
        return _save(db, class_row, user_id, answer_id, body)
    except AnswerCheckError:
        raise
    except Exception:
        raise AnswerCheckError(
            "保存失败，这次没有写入任何记录；本页内容还在，可以直接重试。",
            500,
            "save_failed",
        )


def _save(db, class_row, user_id, answer_id, body):
    _require_tables(db)
    answer = _answer(db, class_row["id"], answer_id)
    version = answer["fingerprint"] or ""
    expected = str(body.get("expected_version") or "")
    if expected != version:
        # 这一条必须在任何写入之前：旧版本判断不能覆盖新内容，也不应该留下半条记录。
        raise AnswerCheckError(
            "这份作答的内容已经更新，旧判断不再作为当前结论；请刷新后重新核对。",
            409,
            "version_conflict",
        )

    locate = _locate(answer)
    cause_state = (str(body.get("cause_state") or CAUSE_UNDECIDED)).strip() or CAUSE_UNDECIDED
    if cause_state not in (CAUSE_UNDECIDED, CAUSE_DECIDED):
        raise AnswerCheckError("原因判断状态只能是「原因待判断」或「教师已判断」。", 400, "bad_cause_state")
    cause_code = str(body.get("cause_code") or "").strip()[:60]
    cause_label = str(body.get("cause_label") or "").strip()[:120]
    if cause_state == CAUSE_DECIDED and not (cause_code and cause_label):
        raise AnswerCheckError("选了「教师已判断」就要同时填原因编码和原因说明。", 400, "cause_required")
    if _force_undecided(answer, locate["status"]):
        cause_state = CAUSE_UNDECIDED
        cause_code = ""
        cause_label = ""

    observed_step = str(body.get("observed_step") or "").strip()[:400]
    if not observed_step:
        observed_step = locate["first_bad_text"] or ""
    first_bad_index = _first_bad_index(body.get("first_bad_index"), locate)

    followup = body.get("followup")
    followup_text = ""
    question = ""
    if isinstance(followup, dict):
        followup_text = str(followup.get("answer_text") or "").strip()[:4000]
        question = str(followup.get("question") or "").strip()[:300]
    if not question:
        question = _default_question(answer)

    now = now_iso()
    decided_at = now if cause_state == CAUSE_DECIDED else ""
    try:
        with db.tx() as conn:
            answer = _answer(db, class_row["id"], answer_id)
            if (answer["fingerprint"] or "") != version:
                raise AnswerCheckError(
                    "这份作答的内容刚刚被更新，旧判断不再作为当前结论；请刷新后重新核对。",
                    409,
                    "version_conflict",
                )
            _fault("answer_check_save")
            existing = current_check(db, answer)
            history = _json_list(existing["cause_history"]) if existing else []
            if existing and existing["cause_code"] and existing["cause_code"] != cause_code:
                history.append({
                    "code": existing["cause_code"],
                    "label": existing["cause_label"],
                    "decided_at": existing["decided_at"] or existing["updated_at"],
                })
            history_text = json.dumps(history, ensure_ascii=False)
            if existing:
                conn.execute(
                    "UPDATE answer_checks SET progress=?, cause_state=?, observed_step=?,"
                    " first_bad_index=?, locate_status=?, locate_source=?, contrast_note=?,"
                    " cause_code=?, cause_label=?, cause_history=?, teacher_note=?,"
                    " updated_at=?, decided_at=? WHERE id=?",
                    (
                        PROGRESS_CHECKED, cause_state, observed_step, first_bad_index,
                        locate["status"], locate["source"], locate["contrast"]["note"] or "",
                        cause_code, cause_label, history_text,
                        str(body.get("teacher_note") or "").strip()[:2000],
                        now, decided_at, existing["id"],
                    ),
                )
                check_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO answer_checks(class_id, student_id, answer_id, batch, answer_version,"
                    " progress, cause_state, observed_step, first_bad_index, locate_status,"
                    " locate_source, contrast_note, cause_code, cause_label, cause_history,"
                    " teacher_note, created_by, created_at, updated_at, decided_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        answer["class_id"], answer["student_id"], answer["id"], answer["batch"],
                        version, PROGRESS_CHECKED, cause_state, observed_step, first_bad_index,
                        locate["status"], locate["source"], locate["contrast"]["note"] or "",
                        cause_code, cause_label, history_text,
                        str(body.get("teacher_note") or "").strip()[:2000],
                        user_id, now, now, decided_at,
                    ),
                )
                check_id = cursor.lastrowid
            if followup_text:
                latest = db.query_one(
                    "SELECT * FROM answer_followups WHERE answer_id=? AND answer_version=?"
                    " ORDER BY id DESC LIMIT 1",
                    (answer["id"], version),
                )
                if not latest or (latest["question"] != question or latest["answer_text"] != followup_text):
                    conn.execute(
                        "INSERT INTO answer_followups(class_id, student_id, answer_id, check_id,"
                        " answer_version, question, source, answer_text, asked_at, answered_at,"
                        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            answer["class_id"], answer["student_id"], answer["id"], check_id,
                            version, question, _followup_source(question, answer),
                            followup_text, now, now, now, now,
                        ),
                    )
    except AnswerCheckError:
        raise
    except Exception:
        # 保存失败必须让前端留在当前页、保留输入；这里不把底层异常原文透给老师。
        raise AnswerCheckError(
            "保存失败，这次没有写入任何记录；本页内容还在，可以直接重试。",
            500,
            "save_failed",
        )

    check = db.query_one("SELECT * FROM answer_checks WHERE id=?", (check_id,))
    judgement = _judgement(check)
    pending = queue_rows(db, answer["class_id"], answer["batch"])
    remaining = len(pending)
    return {
        "ok": True,
        "receipt": {
            "class_name": class_row["name"],
            "assignment_name": assignment_name(answer["batch"]),
            "saved_at": check["updated_at"],
            "status_line": "%s · %s" % (judgement["progress_label"], judgement["cause_label"]),
        },
        "judgement": judgement,
        "queue": {
            "next_answer_id": pending[0]["id"] if pending else None,
            "is_last": remaining == 0,
            "remaining": remaining,
            "total": remaining + 1,
        },
    }


# --------------------------------------------------------------------------
# §3.4 记录找回
# --------------------------------------------------------------------------
def records(db, class_row, batch="", cause_state=CAUSE_UNDECIDED):
    _require_tables(db)
    cause_state = (cause_state or CAUSE_UNDECIDED).strip() or CAUSE_UNDECIDED
    if cause_state not in (CAUSE_UNDECIDED, CAUSE_DECIDED, "all"):
        raise AnswerCheckError("原因判断状态只能是 undecided、decided 或 all。", 400, "bad_cause_state")
    sql = (
        "SELECT c.*, a.batch, a.question_no, a.student_name, s.name AS roster_name"
        " FROM answer_checks c JOIN answers a ON a.id=c.answer_id"
        " LEFT JOIN students s ON s.id=a.student_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint"
    )
    params = [class_row["id"]]
    batch = (batch or "").strip()
    if batch:
        sql += " AND a.batch=?"
        params.append(batch)
    if cause_state != "all":
        sql += " AND c.cause_state=?"
        params.append(cause_state)
    sql += " ORDER BY c.updated_at DESC, c.id DESC"
    items = []
    for row in db.query(sql, params):
        items.append({
            "answer_id": row["answer_id"],
            "student_label": _student_label(row),
            "question_no": row["question_no"],
            "observed_step": row["observed_step"],
            "progress": row["progress"],
            "cause_state": row["cause_state"],
            "cause_code": row["cause_code"],
            "cause_label_text": row["cause_label"],
            "saved_at": row["updated_at"],
            "batch": row["batch"],
        })
    return {"total": len(items), "items": items}


__all__ = [
    "AnswerCheckError", "CAUSE_DECIDED", "CAUSE_LABELS", "CAUSE_UNDECIDED", "DEMO_SOURCE",
    "PROGRESS_CHECKED", "PROGRESS_LABELS", "PROGRESS_PENDING",
    "assignment_name", "candidates_for", "causes_note", "current_check", "detail",
    "overview", "queue_rows", "records", "resolve_batch", "save", "table_ready",
]
