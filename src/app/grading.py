"""R2-02 批量核验与教师评分（1.9.0）：四维写入的唯一入口。

契约依据：`docs/R2_接口契约.md` §2.3（批次配置与完整成绩）、§2.4（四维独立状态）、
§2.5（11 条失效规则）、§2.6（数据隔离与 AI 外发边界）。本模块**不另立状态字符串**：
状态机、中文标签、失效规则、评分校验、批次口径全部从 `r2_contract` 取。

四维写入分工（本模块是唯一写入口，`r2_stats` 只读）：
  ① 作答 `answer`：R1 的 `answers.missing` + 内容量 → `classify_record_state()` 只读映射；
     内容变化走 `update_answer_content()`（委托 `answer_check.update_content`，不重写核验算法），
     再按 `R2-INV-01` 把评分行标失效。
  ② 核验 `verify`：逐条调用 `answer_check.save()`（规则定位 + 人工核验 + 追问 + 错题同步都在那一层），
     本模块只负责调度、保护已确认结果、汇总与重试。
  ③ 错因 `cause`：确认走 `answer_check.save()`（复用 R1 `cause_state` 语义），撤回走
     `retract_cause()`（`R2-INV-03`：只让 cause→undecided，核验与评分**独立保留**）。
  ④ 评分 `score`：R2 新增。`validate_score()` / `step_deductions_consistent()` 是硬闸门，
     不一致拒绝保存（`R2-INV-07`）；只有 `confirmed` 计入正式统计（`R2-INV-04`）。

外发边界：本模块没有任何生成式模型调用。真实学生作答的批改、错因、统计、建议全部本地规则，
`users.ai_individual_allowed` 不参与判断（`r2_contract.AI_FLOWS` 里这些流程的决策恒为 `deny`）。
"""

import json

from . import answer_check, r2_contract
from .db import DEMO_SOURCE, load_batch_config, now_iso, save_batch_config

# ---------------------------------------------------------------------------
# 一、常量与错误
# ---------------------------------------------------------------------------

# 批量核验的三种来源（派单 §四.1「按批次 / 全部待核验 / 指定记录」）
SCOPE_PENDING = "pending"
SCOPE_ALL = "all"
SCOPE_IDS = "ids"
SCOPES = (SCOPE_PENDING, SCOPE_ALL, SCOPE_IDS)
SCOPE_LABELS = {
    SCOPE_PENDING: "全部待核验",
    SCOPE_ALL: "批次内全部记录",
    SCOPE_IDS: "指定记录",
}

SCORE_UNSCORED = "unscored"
SCORE_DRAFT = "draft"
SCORE_CONFIRMED = "confirmed"
SCORE_INVALIDATED = "invalidated"

# 状态机里的评分迁移（R2-INV-05/06 需要「已失效 → 重评」这条复合路径）
_RESCORE_PATH = (SCORE_INVALIDATED, SCORE_DRAFT, SCORE_CONFIRMED)

KNOWLEDGE_UNCLASSIFIED = "未分类"
KNOWLEDGE_UNCLASSIFIED_NOTE = "知识点：未分类（缺依据，待教师补充）"

# 题级评分标准：权威结构是 `r2_contract`（R2-00.4 起）的
# `assignment_batches.rubrics[q] = {"full_score","criteria","rubric_version"}`；批次级
# `rubric_version` 只在题级缺失时作历史兼容回退，不再是权威。
# 本模块只做两件事：读侧一律走 `r2_contract.question_rubric()`（单一实现），写侧按同一结构落库，
# 并兼容 R2-00.2 时代的「rubric[q] 存 JSON 对象字符串」写法（本模块上一版落过的数据）。
_RUBRIC_TEXT_KEYS = ("text", "criteria", "rubric", "standard")


def _decode_legacy_entry(raw):
    """兼容两种历史写法：`rubric[q]` 是纯文本，或是 JSON 对象字符串（旧版本题写入形态）。"""
    entry = {"text": "", "rubric_version": "", "full_score": None}
    if isinstance(raw, dict):
        for key in _RUBRIC_TEXT_KEYS:
            if raw.get(key):
                entry["text"] = str(raw.get(key))
                break
        entry["rubric_version"] = str(raw.get("rubric_version") or raw.get("version") or "")
        entry["full_score"] = r2_contract._as_float(raw.get("full_score"))
        return entry
    text = str(raw if raw is not None else "")
    entry["text"] = text
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return entry
        if isinstance(parsed, dict):
            return _decode_legacy_entry(parsed)
    return entry


def question_rubric(config, question_no):
    """某道题的权威标准 `{"text","criteria","rubric_version","full_score"}`。

    委托 `r2_contract.question_rubric()`（R2-00.4 的唯一实现）；旧契约没有该函数时，用本模块
    的兼容解析（题级 `rubrics` → 旧 `rubric` 文本 → 批次级版本回退）。
    """
    contract_fn = getattr(r2_contract, "question_rubric", None)
    if contract_fn is not None:
        entry = contract_fn(config, question_no) or {}
        criteria = str(entry.get("criteria") or "")
        return {"text": criteria, "criteria": criteria,
                "rubric_version": str(entry.get("rubric_version") or ""),
                "full_score": entry.get("full_score")}
    config = config or {}
    rubrics = config.get("rubrics") if isinstance(config.get("rubrics"), dict) else {}
    entry = _decode_legacy_entry(rubrics.get(str(question_no)))
    if not entry["text"]:
        legacy = (config.get("rubric") or {}).get(str(question_no))
        entry["text"] = _decode_legacy_entry(legacy)["text"]
        entry["rubric_version"] = entry["rubric_version"] or _decode_legacy_entry(legacy)["rubric_version"]
    if not entry["rubric_version"]:
        entry["rubric_version"] = str(config.get("rubric_version") or "")
    if entry["full_score"] is None:
        entry["full_score"] = r2_contract.full_score_of(config, question_no)
    return {"text": entry["text"], "criteria": entry["text"],
            "rubric_version": entry["rubric_version"], "full_score": entry["full_score"]}


def question_rubric_version(config, question_no):
    """某道题当前生效的标准版本串（失效判定一律按题比较，不拿批次级串一刀切）。"""
    return question_rubric(config, question_no)["rubric_version"]


def _rubric_payload(text, version, full_score):
    """写进 `rubrics[q]` 的题级条目（与 `r2_contract.normalize_batch_config` 的字段名一致）。"""
    return {"criteria": str(text or ""), "rubric_version": str(version or ""),
            "full_score": full_score}



# 本模块写入错题时用的来源标签（与 R1-16 同一常量，避免两套撤回口径 —— R1 遗留 K2）
MISTAKE_SOURCE = answer_check.MISTAKE_SOURCE
MISTAKE_RETURNED_REASON = "撤回错因判断：评分与核验各自保留"

# 只有这些「核验结论存在」的状态才允许判错因（STATE_GATES 的 cause.decided 闸门）
_VERIFIED_STATES = ("verified_rule", "verified_manual", "verified_preset")


class GradingError(Exception):
    """业务错误：`status` 给接口层用，`code` 给界面文案映射用（中文消息已写好）。"""

    def __init__(self, message, status=400, code="grading_error"):
        Exception.__init__(self, message)
        self.message = message
        self.status = status
        self.code = code


def _raise(message, status=400, code="grading_error"):
    raise GradingError(message, status, code)


# ---------------------------------------------------------------------------
# 二、外发边界（R2-02 全部流程恒本地；不读 users.ai_individual_allowed）
# ---------------------------------------------------------------------------

# R2 侧禁止外发的流程（原始决策来自 r2_contract.AI_FLOWS，不在这里另写一份）
LOCAL_ONLY_FLOWS = ("answer_check_grading", "answer_check_cause")


def local_decision(flow):
    """某个 R2-02 流程的外发决策（纯查询，供界面/接口层显示「本地规则」）。

    故意传入 `individual_allowed=True`：即使账号级开关是开的，R2 流程也必须说「不许发」——
    这条断言是本模块「不读现有开关」的证据（测试里用它做反向自证）。
    """
    decision = r2_contract.egress_decision(flow, individual_allowed=True)
    return dict(decision, label=r2_contract.AI_FLOWS.get(flow, {}).get("note", ""))


def guard_egress(flow, provider="", model="", fields=None):
    """外发请求一律拒绝（403 ai_isolation）；返回本流程的本地决策供调用方记录。

    - 任何 provider/model（哪怕是 mock）都不该出现在 R2-02 的批改/错因路径上；
    - `fields` 里只要出现 `EGRESS_FORBIDDEN_FIELDS` 中的任一个，也拒绝（防「先组装再发」）；
    - 真实学生作答相关的流程决策恒为 `deny`，与 `users.ai_individual_allowed` 无关。
    """
    decision = local_decision(flow)
    if decision.get("allowed"):
        _raise("R2-02 流程不允许外发（%s）。" % flow, 403, "ai_isolation")
    problem = r2_contract.egress_field_problem(fields or [])
    if provider or model or problem:
        _raise(
            "R2-02 的批改与错因更新一律本地规则，真实学生作答不外发（%s）。%s"
            % (flow, problem or "本次请求带了模型/provider 参数。"),
            403,
            "ai_isolation",
        )
    return decision


# ---------------------------------------------------------------------------
# 三、通用小工具
# ---------------------------------------------------------------------------


def _loads(raw, default):
    if isinstance(raw, (list, dict)):
        return raw
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _answer(db, class_row, answer_id):
    """按 (班级, id) 取作答：跨班一律不可见（契约 §2.2 的硬规则）。"""
    row = db.query_one(
        "SELECT * FROM answers WHERE id=? AND class_id=?",
        (answer_id, class_row["id"]),
    )
    if row is None:
        _raise("找不到这份作答（可能已被删除），或者它不属于当前班级。", 404, "answer_not_found")
    return row


def _config(db, class_row, batch):
    return load_batch_config(db, class_row["id"], batch or "")


def _images(db, answer_id):
    rows = db.query(
        "SELECT id, seq, file_path, mime, sha256 FROM answer_images"
        " WHERE answer_id=? ORDER BY seq ASC, id ASC",
        (answer_id,),
    )
    return [dict(row) for row in rows]


def _current_check(db, answer):
    """当前版本的核验结论（与 R1 同一判据：answer_version == fingerprint）。"""
    return db.query_one(
        "SELECT * FROM answer_checks WHERE answer_id=? AND answer_version=?"
        " ORDER BY id DESC LIMIT 1",
        (answer["id"], answer["fingerprint"] or ""),
    )


def _stale_check(db, answer):
    """只有旧版本结论 / 撤回行的痕迹（用于把核验状态读成 `stale` 而不是「从未核验」）。"""
    return db.query_one(
        "SELECT * FROM answer_checks WHERE answer_id=? AND answer_version<>''"
        " AND answer_version<>? ORDER BY id DESC LIMIT 1",
        (answer["id"], answer["fingerprint"] or ""),
    )


def _score_rows(db, answer, question_no=None):
    sql = "SELECT * FROM answer_scores WHERE answer_id=?"
    params = [answer["id"]]
    if question_no is not None:
        sql += " AND question_no=?"
        params.append(str(question_no))
    sql += " ORDER BY id DESC"
    return [dict(row) for row in db.query(sql, tuple(params))]


def _score_live(db, answer, question_no):
    """当前「活着」的成绩行：state=draft/confirmed 且版本与标准都对得上。"""
    for row in _score_rows(db, answer, question_no):
        if row.get("state") not in (SCORE_DRAFT, SCORE_CONFIRMED):
            continue
        if not r2_contract.is_current_version(row.get("answer_version"), answer.get("fingerprint")):
            continue
        return row
    return None


def _mistake_of(db, class_row, answer_id):
    row = db.query_one(
        "SELECT * FROM mistakes WHERE answer_id=? AND class_id=? ORDER BY id DESC LIMIT 1",
        (answer_id, class_row["id"]),
    )
    return dict(row) if row else None


def _knowledge_points(raw):
    points = _loads(raw, [])
    out = []
    for item in points:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _score_question_no(answer, question_no=None):
    value = str(question_no if question_no is not None else answer.get("question_no") or "").strip()
    return value


def _full_score(db, class_row, batch, question_no):
    config = _config(db, class_row, batch)
    return r2_contract.full_score_of(config, question_no), config


# ---------------------------------------------------------------------------
# 四、四维状态读取（只读；界面与统计共用同一份口径）
# ---------------------------------------------------------------------------


def _verify_state(db, answer):
    check = _current_check(db, answer)
    if check:
        source = (check.get("verify_source") or "").strip()
        state = {
            answer_check.VERIFY_RULE: "verified_rule",
            answer_check.VERIFY_MANUAL: "verified_manual",
            answer_check.VERIFY_PRESET: "verified_preset",
        }.get(source)
        if state is None:
            # 历史行没有来源字段：有结论就算规则核验，不假装「未核验」
            state = "verified_rule" if (check.get("progress") or "") == answer_check.PROGRESS_CHECKED \
                else "unverified"
        return {
            "state": state,
            "source": source,
            "check_id": check.get("id"),
            "locate_status": check.get("locate_status") or "",
            "answer_version": check.get("answer_version") or "",
            "batch": check.get("batch") or "",
            "label": r2_contract.STATE_LABELS.get(state, state),
        }
    if _stale_check(db, answer):
        return {"state": "stale", "source": "", "check_id": None, "locate_status": "",
                "answer_version": "", "batch": "",
                "label": r2_contract.STATE_LABELS["stale"]}
    return {"state": "unverified", "source": "", "check_id": None, "locate_status": "",
            "answer_version": "", "batch": "",
            "label": r2_contract.STATE_LABELS["unverified"]}


def _cause_state(db, answer, check=None):
    check = check if check is not None else _current_check(db, answer)
    state = (check.get("cause_state") or "") if check else ""
    if state not in r2_contract.CAUSE_STATES:
        state = "undecided"
    return {
        "state": state,
        "code": (check.get("cause_code") or "") if check else "",
        "label_text": (check.get("cause_label") or "") if check else "",
        "source": (check.get("cause_source") or "") if check else "",
        "label": r2_contract.STATE_LABELS.get(state, state),
    }


def _score_state(db, answer, question_no, config):
    """评分维：只认当前版本 + **该题**当前标准版本的行；其余如实报「已失效（需重评）」。"""
    live = _score_live(db, answer, question_no)
    rows = _score_rows(db, answer, question_no)
    entry = question_rubric(config, question_no)
    expected_version = entry["rubric_version"]
    expected_full = entry["full_score"]
    formal = False
    state = SCORE_UNSCORED
    row = live
    if live:
        state = live.get("state") or SCORE_UNSCORED
        stale = False
        if expected_version and str(live.get("rubric_version") or "") != expected_version:
            stale = True
        if expected_full is not None and live.get("full_score") is not None:
            if abs(float(live.get("full_score")) - float(expected_full)) > 1e-9:
                stale = True
        if stale:
            state = SCORE_INVALIDATED
        formal = state == SCORE_CONFIRMED
    elif rows:
        state = SCORE_INVALIDATED
    return {
        "state": state,
        "label": r2_contract.STATE_LABELS.get(state, state),
        "formal": formal,
        "score": live.get("score") if live else None,
        "full_score": r2_contract.full_score_of(config or {}, question_no),
        "rubric_version": (live or {}).get("rubric_version") or "",
        "expected_rubric_version": expected_version,
        "steps": _loads((live or {}).get("steps_json"), []),
        "row_id": (live or {}).get("id"),
        "scored_at": (live or {}).get("scored_at") or "",
        "confirmed_at": (live or {}).get("confirmed_at") or "",
        "invalidated": bool(rows) and state == SCORE_INVALIDATED,
    }


def answer_dimensions(db, class_row, answer_id, question_no=None):
    """一份作答的四维状态快照（shape 与 `r2_contract.empty_state()` 对齐，多余键不影响）。"""
    answer = _answer(db, class_row, answer_id)
    images = _images(db, answer["id"])
    batch = answer.get("batch") or ""
    config = _config(db, class_row, batch)
    question = _score_question_no(answer, question_no) or "1"
    steps_text = str(answer.get("student_steps") or "").strip()
    has_text = bool(steps_text)
    state, reason = r2_contract.classify_record_state(
        missing=answer.get("missing") or 0,
        missing_reason=answer.get("missing_reason") or "",
        has_text=has_text,
        has_steps=False,
        image_count=len(images),
    )
    check = _current_check(db, answer)
    verify = _verify_state(db, answer)
    cause = _cause_state(db, answer, check)
    score = _score_state(db, answer, question, config)
    return {
        "answer_id": answer["id"],
        "student_id": answer["student_id"],
        "fingerprint": answer.get("fingerprint") or "",
        "student_name": answer.get("student_name") or "",
        "student_code": answer.get("student_code") or "",
        "question_no": answer.get("question_no") or "",
        "scored_question_no": question,
        "batch": batch,
        "source": answer.get("source") or "",
        "synthetic": (answer.get("source") or "") == DEMO_SOURCE,
        "formally_counted": r2_contract.include_in_formal_stats(answer.get("source")),
        "images": len(images),
        "image_rows": images,
        "dimensions": {
            "answer": {"state": state, "reason": reason,
                       "label": r2_contract.STATE_LABELS.get(state, state)},
            "verify": verify,
            "cause": cause,
            "score": score,
        },
        "config_state": config.get("config_state") or "",
        "rubric_version": config.get("rubric_version") or "",
        "protected": _protected_reasons(verify, cause, score),
    }


def _protected_reasons(verify, cause, score):
    """批量核验必须绕开的三类「已经有人做过判断」的记录（派单：重复核验不得覆盖）。"""
    reasons = []
    if verify.get("state") == "verified_manual":
        reasons.append("manual_verification")
    if cause.get("state") == "decided":
        reasons.append("cause_decided")
    if score.get("state") == SCORE_CONFIRMED:
        reasons.append("score_confirmed")
    return reasons


# ---------------------------------------------------------------------------
# 五、批量核验调度（逐条调用 answer_check，单条失败不阻塞）
# ---------------------------------------------------------------------------


def _answer_rows(db, class_row, batch=""):
    sql = ("SELECT a.*, s.name AS roster_name FROM answers a"
           " LEFT JOIN students s ON s.id=a.student_id WHERE a.class_id=?")
    params = [class_row["id"]]
    if batch:
        sql += " AND a.batch=?"
        params.append(batch)
    sql += " ORDER BY a.id ASC"
    return [dict(row) for row in db.query(sql, tuple(params))]


def _maps(db, class_row, batch=""):
    """一次取齐该范围内核验/评分/图片，避免逐条查询把批量核验拖慢。"""
    where = "class_id=?"
    params = [class_row["id"]]
    if batch:
        where += " AND batch=?"
        params.append(batch)
    checks = {}
    for row in db.query("SELECT * FROM answer_checks WHERE " + where + " ORDER BY id ASC",
                        tuple(params)):
        checks.setdefault(row["answer_id"], []).append(dict(row))
    scores = {}
    for row in db.query("SELECT * FROM answer_scores WHERE " + where + " ORDER BY id ASC",
                        tuple(params)):
        scores.setdefault(row["answer_id"], []).append(dict(row))
    images = {}
    for row in db.query(
            "SELECT answer_id, COUNT(*) AS n FROM answer_images"
            " WHERE class_id=? GROUP BY answer_id", (class_row["id"],)):
        images[row["answer_id"]] = int(row["n"] or 0)
    return checks, scores, images


def _current_of(rows, answer):
    version = answer.get("fingerprint") or ""
    for row in reversed(rows):
        if (row.get("answer_version") or "") == version and version:
            return row
    return None


def _dims_from_maps(answer, checks, scores, images, config):
    """用预取的数据算四维（与 `answer_dimensions()` 同一套判据，只是不查库）。"""
    answer_rows = checks.get(answer["id"]) or []
    score_rows = scores.get(answer["id"]) or []
    check = _current_of(answer_rows, answer)
    stale = any(
        (row.get("answer_version") or "") not in ("", answer.get("fingerprint") or "")
        for row in answer_rows
    )
    if check:
        source = (check.get("verify_source") or "").strip()
        verify_state = {
            answer_check.VERIFY_RULE: "verified_rule",
            answer_check.VERIFY_MANUAL: "verified_manual",
            answer_check.VERIFY_PRESET: "verified_preset",
        }.get(source)
        if verify_state is None:
            verify_state = "verified_rule" if (check.get("progress") or "") == "checked" else "unverified"
        verify = {"state": verify_state, "source": source, "check_id": check.get("id"),
                  "locate_status": check.get("locate_status") or ""}
    elif stale:
        verify = {"state": "stale", "source": "", "check_id": None, "locate_status": ""}
    else:
        verify = {"state": "unverified", "source": "", "check_id": None, "locate_status": ""}
    cause_state = (check.get("cause_state") or "") if check else ""
    if cause_state not in r2_contract.CAUSE_STATES:
        cause_state = "undecided"
    cause = {"state": cause_state,
             "code": (check.get("cause_code") or "") if check else "",
             "label_text": (check.get("cause_label") or "") if check else "",
             "source": (check.get("cause_source") or "") if check else ""}
    rubric_entry = question_rubric(config, answer.get("question_no") or "")
    rubric_version = rubric_entry["rubric_version"]
    rubric_full = rubric_entry["full_score"]
    live = None
    for row in score_rows:
        if row.get("state") not in (SCORE_DRAFT, SCORE_CONFIRMED):
            continue
        if not r2_contract.is_current_version(row.get("answer_version"), answer.get("fingerprint")):
            continue
        live = row
    if live:
        score_state = live.get("state") or SCORE_UNSCORED
        stale = bool(rubric_version) and str(live.get("rubric_version") or "") != rubric_version
        if rubric_full is not None and live.get("full_score") is not None:
            if abs(float(live.get("full_score")) - float(rubric_full)) > 1e-9:
                stale = True
        if stale:
            score_state = SCORE_INVALIDATED
    elif score_rows:
        score_state = SCORE_INVALIDATED
    else:
        score_state = SCORE_UNSCORED
    score = {"state": score_state, "formal": score_state == SCORE_CONFIRMED,
             "score": live.get("score") if live else None,
             "row_id": live.get("id") if live else None,
             "rubric_version": (live or {}).get("rubric_version") or ""}
    steps_text = str(answer.get("student_steps") or "").strip()
    answer_state, reason = r2_contract.classify_record_state(
        missing=answer.get("missing") or 0,
        missing_reason=answer.get("missing_reason") or "",
        has_text=bool(steps_text),
        has_steps=False,
        image_count=images.get(answer["id"], 0),
    )
    return {"answer": {"state": answer_state, "reason": reason}, "verify": verify,
            "cause": cause, "score": score}


def batch_rows(db, class_row, batch="", scope=SCOPE_PENDING, answer_ids=None, limit=None):
    """批量核验的工作队列：按批次（空=全班所有批次）/ 全部待核验 / 指定记录。

    每行 = 一份作答 + 四维状态 + `protected`（必须跳过的原因）。顺序稳定（按 answers.id 升序；
    `scope='ids'` 时按传入顺序），界面进度条与「逐条状态」直接用这份顺序。
    """
    scope = str(scope or SCOPE_PENDING).strip().lower()
    if scope not in SCOPES:
        _raise("批量核验范围只能是 pending / all / ids。", 400, "bad_scope")
    answers = _answer_rows(db, class_row, batch or "")
    checks, scores, images = _maps(db, class_row, batch or "")
    configs = {}
    rows = []
    order = {int(value): index for index, value in enumerate(answer_ids or [])}
    for answer in answers:
        if scope == SCOPE_IDS:
            if answer["id"] not in order:
                continue
        batch_name = answer.get("batch") or ""
        if batch_name not in configs:
            configs[batch_name] = _config(db, class_row, batch_name)
        dims = _dims_from_maps(answer, checks, scores, images, configs[batch_name])
        row = {
            "answer_id": answer["id"],
            "student_id": answer["student_id"],
            "student_name": answer.get("student_name") or answer.get("roster_name") or "",
            "student_code": answer.get("student_code") or "",
            "record_key": answer.get("record_key") or "",
            "question_no": answer.get("question_no") or "",
            "batch": batch_name,
            "occurred_on": answer.get("occurred_on") or "",
            "source": answer.get("source") or "",
            "synthetic": (answer.get("source") or "") == DEMO_SOURCE,
            "images": images.get(answer["id"], 0),
            "dimensions": dims,
            "protected": _protected_reasons(dims["verify"], dims["cause"], dims["score"]),
        }
        if scope == SCOPE_PENDING:
            # 「待核验」= 当前版本没有有效核验结论（未核验 / 旧版本失效）
            if dims["verify"]["state"] not in ("unverified", "stale"):
                continue
            # 缺测与内容不足也能核验（结论是「无法核验」），但已确认成绩的不再动
            if "score_confirmed" in row["protected"]:
                continue
        rows.append(row)
    if scope == SCOPE_IDS:
        rows.sort(key=lambda item: order.get(item["answer_id"], 0))
    if limit:
        rows = rows[: int(limit)]
    return rows


def _batch_progress_rows(rows):
    total = len(rows)
    verify = {}
    cause = {}
    score = {}
    for row in rows:
        v = row["dimensions"]["verify"]["state"]
        c = row["dimensions"]["cause"]["state"]
        s = row["dimensions"]["score"]["state"]
        verify[v] = verify.get(v, 0) + 1
        cause[c] = cause.get(c, 0) + 1
        score[s] = score.get(s, 0) + 1
    return {
        "total": total,
        "verify": verify,
        "cause": cause,
        "score": score,
        "verified": sum(verify.get(key, 0) for key in _VERIFIED_STATES),
        "pending": verify.get("unverified", 0) + verify.get("stale", 0),
        "cause_decided": cause.get("decided", 0),
        "score_confirmed": score.get(SCORE_CONFIRMED, 0),
        "score_draft": score.get(SCORE_DRAFT, 0),
    }


def batch_progress(db, class_row, batch=""):
    """批次核验进度（界面进度条与结果汇总的唯一来源）。"""
    rows = batch_rows(db, class_row, batch, scope=SCOPE_ALL)
    progress = _batch_progress_rows(rows)
    progress["batch"] = batch or ""
    progress["scope"] = SCOPE_ALL
    return progress


def _preserve_cause_body(db, class_row, answer, existing, body):
    """重复核验时把教师已有的错因判断原样带回去，绝不能因为「再跑一次」被清成待判断。"""
    if not existing:
        return body
    state = (existing.get("cause_state") or "")
    if state not in ("decided", "not_applicable"):
        return body
    body["cause_state"] = state
    if state == "decided":
        body["cause_code"] = existing.get("cause_code") or ""
        body["cause_label"] = existing.get("cause_label") or ""
        source = (existing.get("cause_source") or "").strip()
        if source in (answer_check.CAUSE_SOURCE_PRESET, answer_check.CAUSE_SOURCE_TEACHER):
            body["cause_source"] = source
    return body


def verify_one(db, class_row, user_id, answer_id, force=False):
    """核验一份作答：委托 `answer_check.save()`，返回逐条结果（失败也返回，不抛）。

    `force=False` 时保护三类记录：人工核验结论、已判断错因、已确认成绩——重复点「开始核验」
    不会覆盖老师已经做过的判断（派单原文）。`force=True` 只允许在界面上显式「重跑这一条」时用，
    且仍然保留教师的错因判断（只重算规则定位）。
    """
    try:
        answer = _answer(db, class_row, answer_id)
        dims = answer_dimensions(db, class_row, answer_id)
        reasons = dims["protected"]
        if reasons and not force:
            return {"answer_id": answer_id, "status": "skipped", "reasons": reasons,
                    "code": "protected", "message": "这一条已有教师判断或已确认成绩，批量核验已跳过。",
                    "dimensions": dims["dimensions"]}
        existing = _current_check(db, answer)
        body = {"expected_version": answer.get("fingerprint") or "",
                "progress": answer_check.PROGRESS_CHECKED}
        body = _preserve_cause_body(db, class_row, answer, existing, body)
        answer_check.save(db, class_row, user_id, answer_id, body)
        after = answer_dimensions(db, class_row, answer_id)
        return {"answer_id": answer_id, "status": "verified", "code": "ok", "message": "",
                "reasons": reasons, "verify_source": after["dimensions"]["verify"]["source"],
                "locate_status": after["dimensions"]["verify"]["locate_status"],
                "cause_state": after["dimensions"]["cause"]["state"],
                "dimensions": after["dimensions"]}
    except answer_check.AnswerCheckError as exc:
        return {"answer_id": answer_id, "status": "failed", "code": exc.code,
                "message": exc.message, "dimensions": None}
    except GradingError as exc:
        return {"answer_id": answer_id, "status": "failed", "code": exc.code,
                "message": exc.message, "dimensions": None}
    except Exception as exc:  # 单条异常不阻塞整批（派单：单条失败不阻塞）
        return {"answer_id": answer_id, "status": "failed", "code": "verify_failed",
                "message": "这一条核验失败：%s。其余记录不受影响，可稍后重试。" % exc,
                "dimensions": None}


def _empty_summary():
    return {"total": 0, "processed": 0, "verified": 0, "skipped": 0, "failed": 0,
            "cancelled": False, "remaining": [], "failed_ids": [], "skipped_ids": [],
            "results": [], "started_at": "", "finished_at": ""}


def _run_rows(db, class_row, user_id, rows, summary, on_result=None, cancel_check=None):
    summary["started_at"] = now_iso()
    for index, row in enumerate(rows):
        if cancel_check is not None and cancel_check():
            summary["cancelled"] = True
            summary["remaining"] = [item["answer_id"] for item in rows[index:]]
            break
        result = verify_one(db, class_row, user_id, row["answer_id"])
        result["index"] = index + 1
        summary["results"].append(result)
        summary["processed"] += 1
        if result["status"] == "verified":
            summary["verified"] += 1
        elif result["status"] == "skipped":
            summary["skipped"] += 1
            summary["skipped_ids"].append(row["answer_id"])
        else:
            summary["failed"] += 1
            summary["failed_ids"].append(row["answer_id"])
        if on_result is not None:
            # 界面逐条刷新用；回调异常不许拖垮整批
            try:
                on_result(result)
            except Exception:
                pass
    summary["finished_at"] = now_iso()
    return summary


def batch_verify(db, class_row, user_id, batch="", scope=SCOPE_PENDING, answer_ids=None,
                 limit=None, on_result=None, cancel_check=None):
    """批量核验：按批次 / 全部待核验 / 指定记录调度，逐条保存、逐条回调、可取消。

    返回汇总（`total/verified/skipped/failed/cancelled/remaining/failed_ids/results`）。
    取消只停「未处理」的记录，已处理的结果都已在库里。`retry_failed()` 用它返回的
    `failed_ids` 重试失败的记录。
    """
    rows = batch_rows(db, class_row, batch, scope=scope, answer_ids=answer_ids, limit=limit)
    summary = _empty_summary()
    summary["total"] = len(rows)
    summary["scope"] = scope
    summary["batch"] = batch or ""
    return _run_rows(db, class_row, user_id, rows, summary, on_result, cancel_check)


def retry_failed(db, class_row, user_id, failed_ids, on_result=None, cancel_check=None):
    """重试上一轮失败的记录（只有这些 id 会被重跑，顺序按传入）。"""
    ids = [int(value) for value in (failed_ids or [])]
    rows = batch_rows(db, class_row, "", scope=SCOPE_IDS, answer_ids=ids)
    summary = _empty_summary()
    summary["total"] = len(rows)
    summary["scope"] = SCOPE_IDS
    return _run_rows(db, class_row, user_id, rows, summary, on_result, cancel_check)


# ---------------------------------------------------------------------------
# 六、错因判断（确认 / 撤回）
# ---------------------------------------------------------------------------


def cause_candidates(db, class_row, answer_id):
    """候选错因与证据（本地规则 + 已审资源库；`answer_check.detail` 的只读桥，不外发）。"""
    answer = _answer(db, class_row, answer_id)
    try:
        view = answer_check.detail(db, class_row, answer_id)
    except answer_check.AnswerCheckError as exc:
        _raise(exc.message, exc.status, exc.code)
    return {
        "answer_id": answer["id"],
        "candidates": view.get("candidates") or [],
        "note": view.get("candidates_note") or "",
        "scope": view.get("causes_scope") or "",
        "evidence": view.get("causes_evidence") or "",
        "applicability": view.get("applicability") or "",
        "locate": view.get("locate") or {},
        "local_only": True,
    }


def confirm_cause(db, class_row, user_id, answer_id, cause_code="", cause_label="",
                  basis_note="", cause_source="", knowledge_point="", teacher_note=None,
                  expected_version=None):
    """教师确认错因（复用 R1 `cause_state` 语义），并给同步出的错题补知识点分类。

    - 必须已有当前版本的核验结论（`STATE_GATES[('cause','decided')]`）；
    - 走 `answer_check.save()`，于是追问证据、错题同步、`cause_history` 全是 R1 那一套，不重写；
    - 错题行缺知识点时按 `R1 遗留 K1` 标「未分类」并留待补充依据；给了 `knowledge_point` 就直接写上。
    """
    answer = _answer(db, class_row, answer_id)
    verify = _verify_state(db, answer)
    if verify["state"] not in _VERIFIED_STATES:
        _raise("这份作答还没有当前版本的核验结论，不能判断错因：请先核验（或人工核对）。",
               409, "verify_required")
    if not str(cause_label or "").strip():
        _raise("选了「教师已判断」就要填原因说明（编码可留空）。", 400, "cause_required")
    expected = str(expected_version if expected_version is not None
                   else answer.get("fingerprint") or "")
    body = {
        "expected_version": expected,
        "progress": answer_check.PROGRESS_CHECKED,
        "cause_state": answer_check.CAUSE_DECIDED,
        "cause_code": cause_code or "",
        "cause_label": cause_label or "",
        "basis_note": basis_note or "",
    }
    if teacher_note is not None:
        body["teacher_note"] = teacher_note
    if str(cause_source or "").strip().lower() in (answer_check.CAUSE_SOURCE_PRESET,
                                                  answer_check.CAUSE_SOURCE_TEACHER):
        body["cause_source"] = str(cause_source).strip().lower()
    try:
        answer_check.save(db, class_row, user_id, answer_id, body)
    except answer_check.AnswerCheckError as exc:
        _raise(exc.message, exc.status, exc.code)
    mistake = _sync_mistake_knowledge(db, class_row, answer_id, user_id,
                                      knowledge_point=knowledge_point, basis=basis_note)
    dims = answer_dimensions(db, class_row, answer_id)
    return {"answer_id": answer_id, "cause": dims["dimensions"]["cause"],
            "mistake": mistake, "dimensions": dims["dimensions"],
            "actions": ()}


def _event_state(dims):
    """把四维读出结果整理成 `apply_event()` 要的快照形状。"""
    return {
        r2_contract.DIM_ANSWER: {"state": dims["dimensions"]["answer"]["state"],
                                 "fingerprint": dims.get("fingerprint") or ""},
        r2_contract.DIM_VERIFY: {"state": dims["dimensions"]["verify"]["state"],
                                 "version": dims["dimensions"]["verify"].get("answer_version") or ""},
        r2_contract.DIM_CAUSE: {"state": dims["dimensions"]["cause"]["state"],
                                "code": dims["dimensions"]["cause"].get("code") or "",
                                "label": dims["dimensions"]["cause"].get("label_text") or ""},
        r2_contract.DIM_SCORE: {"state": dims["dimensions"]["score"]["state"],
                                "formal": dims["dimensions"]["score"].get("formal", False),
                                "rubric_version": dims["dimensions"]["score"].get("rubric_version") or ""},
    }


def _append_history(raw, entry):
    history = _loads(raw, [])
    history.append(entry)
    return _dump(history)


def retract_cause(db, class_row, user_id, answer_id, reason=""):
    """撤回错因判断（R2-INV-03）：只让 cause→undecided。

    核验结论（`answer_version`、`verify_source`、`locate_detail`）与已确认成绩**原样保留**；
    `cause_history` 追加一条撤回事件；错题退回（只退回我方写的那条，与 R1-16 同一口径）。
    """
    answer = _answer(db, class_row, answer_id)
    check = _current_check(db, answer)
    if not check:
        _raise("这份作答当前没有可撤回的错因判断（可能已撤回或作答已更新）。", 400,
               "nothing_to_retract")
    current = (check.get("cause_state") or "")
    if current != answer_check.CAUSE_DECIDED:
        _raise("当前不是「教师已判断」，没有可撤回的错因判断。", 400, "nothing_to_retract")
    try:
        r2_contract.transition_state(r2_contract.DIM_CAUSE, current, "undecided")
    except ValueError as exc:
        _raise(str(exc), 400, "bad_transition")
    # 动作清单按**事件发生前**的四维快照算：规则看到「decided」才会给出该失效什么
    before = _event_state(answer_dimensions(db, class_row, answer_id))
    plan = r2_contract.apply_event("cause_retracted", before)
    now = now_iso()
    history = _append_history(check.get("cause_history"), {
        "action": "cause_retract",
        "cause_state": current,
        "code": check.get("cause_code") or "",
        "label": check.get("cause_label") or "",
        "source": check.get("cause_source") or "",
        "reason": reason or "",
        "by": user_id,
        "at": now,
        "answer_version": check.get("answer_version") or "",
    })
    with db.tx() as conn:
        conn.execute(
            "UPDATE answer_checks SET cause_state='undecided', cause_source=?, cause_code='',"
            " cause_label='', decided_at='', decided_by=NULL, cause_history=?, change_reason=?,"
            " updated_at=? WHERE id=?",
            (answer_check.CAUSE_SOURCE_NONE, history, reason or "", now, check["id"]),
        )
        mistake = _mistake_of(db, class_row, answer_id)
        returned = False
        if mistake and (mistake.get("source_type") or "") == MISTAKE_SOURCE \
                and (mistake.get("status") or "") == "confirmed":
            conn.execute(
                "UPDATE mistakes SET status='returned', returned_at=?, returned_reason=?,"
                " updated_at=? WHERE id=?",
                (now, MISTAKE_RETURNED_REASON, now, mistake["id"]),
            )
            returned = True
    dims = answer_dimensions(db, class_row, answer_id)
    return {"answer_id": answer_id, "cause": dims["dimensions"]["cause"],
            "verify": dims["dimensions"]["verify"], "score": dims["dimensions"]["score"],
            "score_kept": dims["dimensions"]["score"]["state"] == SCORE_CONFIRMED,
            "mistake_returned": returned, "dimensions": dims["dimensions"],
            "actions": plan["actions"]}


# ---------------------------------------------------------------------------
# 七、错题同步与知识点分类（R1 遗留 K1/K2/K3）
# ---------------------------------------------------------------------------


def mistake_of(db, class_row, answer_id):
    """这份作答对应的错题（含知识点分类与依据），没有就返回 None。"""
    row = _mistake_of(db, class_row, answer_id)
    if row is None:
        return None
    points = _knowledge_points(row.get("knowledge_points"))
    return {
        "id": row["id"],
        "status": row.get("status") or "",
        "source_type": row.get("source_type") or "",
        "answer_id": row.get("answer_id"),
        "knowledge_points": points,
        "classified": bool(points) and points != [KNOWLEDGE_UNCLASSIFIED],
        "note": row.get("note") or "",
        "error_type": row.get("error_type") or "",
        "observed_errors": _loads(row.get("observed_errors"), []),
        "returned_at": row.get("returned_at") or "",
        "returned_reason": row.get("returned_reason") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _sync_mistake_knowledge(db, class_row, answer_id, user_id, knowledge_point="", basis=""):
    """错题同步后的知识点收口（R1 遗留 K1）。

    - 教师给了分类 → 写进 `mistakes.knowledge_points`（JSON 数组字符串），备注留「依据」；
    - 没给 → `knowledge_points` 保持 `[]`（**不编造知识点名字**），只在备注里标「未分类，
      待教师补充」。这样统计侧看到的空数组就是「未分类」，不会被当成一个真实知识点。
    """
    row = _mistake_of(db, class_row, answer_id)
    if row is None:
        return None
    points = _knowledge_points(row.get("knowledge_points"))
    now = now_iso()
    trace = []
    if knowledge_point:
        target = [knowledge_point]
        trace.append("知识点：%s（依据：%s）" % (knowledge_point, basis or "教师指定"))
    else:
        target = points
    note = row.get("note") or ""
    if not target and KNOWLEDGE_UNCLASSIFIED_NOTE not in note:
        trace.append(KNOWLEDGE_UNCLASSIFIED_NOTE)
    if trace:
        note = (note + "｜" if note else "") + "｜".join(trace) + \
               "｜（%s 由 %s 记录）" % (now, user_id if user_id is not None else "系统")
    if target != points or trace:
        with db.tx() as conn:
            conn.execute(
                "UPDATE mistakes SET knowledge_points=?, note=?, updated_at=? WHERE id=?",
                (_dump(target), note, now, row["id"]),
            )
    return mistake_of(db, class_row, answer_id)


def set_knowledge_point(db, class_row, user_id, answer_id, knowledge_point, basis=""):
    """教师补充/修改错题知识点：写 `mistakes.knowledge_points`（原版学情页读的就是这一列）。

    依据（`basis`）一并写进错题备注，回答「凭什么判成这个知识点」；留空则保留原依据。
    这正是 R1 遗留 K1 的关闭方式：新同步错题先标「未分类」，教师补分类时留痕。
    """
    answer = _answer(db, class_row, answer_id)
    del answer
    row = _mistake_of(db, class_row, answer_id)
    if row is None:
        _raise("这份作答还没有错题记录：先确认错因（会同步错题），再补知识点分类。",
               409, "mistake_missing")
    if (row.get("status") or "") != "confirmed":
        _raise("这条错题当前不是「已确认」状态（可能已退回），补分类前请先重新确认错因。",
               409, "mistake_not_confirmed")
    text = str(knowledge_point or "").strip()
    if not text:
        _raise("知识点不能为空；确实无法归类就保持「未分类」，不要填空白。", 400,
               "knowledge_point_required")
    if len(text) > 60:
        _raise("知识点名称最多 60 字（现在 %d 字）。" % len(text), 400, "text_too_long")
    note = row.get("note") or ""
    note = (note + "｜" if note else "") + "知识点：%s（依据：%s｜%s 由 %s 补录）" % (
        text, basis or "教师指定", now_iso(), user_id if user_id is not None else "系统")
    with db.tx() as conn:
        conn.execute(
            "UPDATE mistakes SET knowledge_points=?, note=?, updated_at=? WHERE id=?",
            (_dump([text]), note, now_iso(), row["id"]),
        )
    return mistake_of(db, class_row, answer_id)


# ---------------------------------------------------------------------------
# 八、教师评分（录入 / 确认 / 撤回 / 标准变更）
# ---------------------------------------------------------------------------


def _normalise_steps(steps):
    """步骤扣分项规范化：step_index 可空（自由扣分项），label 必填，deduction ≥ 0。"""
    if steps is None:
        return []
    if not isinstance(steps, (list, tuple)):
        _raise("步骤扣分项必须是列表。", 400, "bad_steps")
    out = []
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            _raise("第 %d 个扣分项格式不对（应为 {step_index,label,deduction,note}）。"
                   % (index + 1), 400, "bad_steps")
        label = str(item.get("label") or "").strip()
        if not label:
            _raise("第 %d 个扣分项缺少说明（扣分理由要写清）。" % (index + 1), 400, "bad_steps")
        try:
            deduction = float(item.get("deduction"))
        except (TypeError, ValueError):
            _raise("第 %d 个扣分项的扣分值不是数值。" % (index + 1), 400, "bad_steps")
        if deduction < 0:
            _raise("扣分值不能为负（加分请直接改分数）。", 400, "bad_steps")
        step_index = item.get("step_index")
        if step_index is not None and step_index != "":
            try:
                step_index = int(step_index)
            except (TypeError, ValueError):
                _raise("第 %d 个扣分项的步骤序号不是整数。" % (index + 1), 400, "bad_steps")
        else:
            step_index = None
        out.append({"step_index": step_index, "label": label,
                    "deduction": round(deduction, r2_contract.MAX_SCORE_DECIMALS),
                    "note": str(item.get("note") or "").strip()})
    seen = set()
    for item in out:
        key = item["step_index"]
        if key is None:
            continue
        if key in seen:
            _raise("步骤 %d 有重复的扣分项：同一处只记一次。" % key, 400, "bad_steps")
        seen.add(key)
    return out


def _transition_chain(from_state, to_state):
    """评分状态迁移路径（非法迁移抛 GradingError；invalidated→confirmed 走 draft 中转）。"""
    if from_state == to_state:
        return [to_state]
    try:
        r2_contract.transition_state(r2_contract.DIM_SCORE, from_state, to_state)
        return [to_state]
    except ValueError:
        if from_state == SCORE_INVALIDATED and to_state == SCORE_CONFIRMED:
            r2_contract.transition_state(r2_contract.DIM_SCORE, SCORE_INVALIDATED, SCORE_DRAFT)
            r2_contract.transition_state(r2_contract.DIM_SCORE, SCORE_DRAFT, SCORE_CONFIRMED)
            return [SCORE_DRAFT, SCORE_CONFIRMED]
        _raise("评分状态不能从「%s」直接改成「%s」。"
               % (r2_contract.STATE_LABELS.get(from_state, from_state),
                  r2_contract.STATE_LABELS.get(to_state, to_state)), 400, "bad_transition")


def score_full_of(db, class_row, batch, question_no):
    """同批次同题的统一满分（缺配置返回 None；界面显示「暂无」，不猜 100）。"""
    full, _config_row = _full_score(db, class_row, batch, question_no)
    return full


def score_row(db, class_row, answer_id, question_no=None):
    """一份作答的评分回显（草稿/已确认/已失效 + 步骤扣分 + 是否计入正式统计）。"""
    answer = _answer(db, class_row, answer_id)
    question = _score_question_no(answer, question_no)
    config = _config(db, class_row, answer.get("batch") or "")
    state = _score_state(db, answer, question, config)
    row = _score_live(db, answer, question)
    steps = []
    if row:
        steps = [dict(item) for item in db.query(
            "SELECT step_index, label, deduction, note FROM score_step_deductions"
            " WHERE score_id=? ORDER BY COALESCE(step_index, 999999), id ASC", (row["id"],))]
    return dict(state, answer_id=answer_id, question_no=question, steps=steps,
                revision_reason=(row or {}).get("revision_reason") or "",
                scored_by=(row or {}).get("scored_by"),
                target_total=r2_contract.full_score_of(config, question))


def _write_steps(conn, score_id, steps, now):
    conn.execute("DELETE FROM score_step_deductions WHERE score_id=?", (score_id,))
    for item in steps:
        conn.execute(
            "INSERT INTO score_step_deductions(score_id, step_index, label, deduction, note,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (score_id, item["step_index"], item["label"], item["deduction"], item["note"], now),
        )


def save_score(db, class_row, user_id, answer_id, score=None, state=SCORE_DRAFT, steps=None,
               revision_reason="", question_no=None, confirm=False, expected_version=None):
    """录入/修改分数：0≤分≤满分、≤2 位小数、步骤扣分与总分一致，否则**拒绝保存**。

    - `state='draft'`：草稿，不计入任何正式统计（`R2-INV-04`）；允许留空（无依据不给分）。
    - `state='confirmed'`：正式成绩，必须 `confirm=True`（教师显式确认），且必须有分数。
    - 改「已确认」的分数必须给 `revision_reason`（谁改、什么时候、为什么都落库）。
    - `steps` 非空时按 `满分 − Σ扣分 = 分数` 校验（`R2-INV-07`），不一致直接 400，不改库。
    """
    answer = _answer(db, class_row, answer_id)
    question = _score_question_no(answer, question_no)
    if not question:
        _raise("这份作答没有题号，无法评分：请先在批次配置里补齐题号。", 400, "question_required")
    full, config = _full_score(db, class_row, answer.get("batch") or "", question)
    if full is None:
        _raise("批次「%s」的题 %s 没有配置满分，不能录入分数（满分一律以批次配置为准）。"
               % (answer.get("batch") or "未命名", question), 400, "full_score_missing")
    target = str(state or SCORE_DRAFT).strip().lower()
    if target not in (SCORE_DRAFT, SCORE_CONFIRMED):
        _raise("评分只能是草稿（draft）或已确认（confirmed）。", 400, "bad_score_state")
    expected = str(expected_version if expected_version is not None
                   else answer.get("fingerprint") or "")
    if expected != (answer.get("fingerprint") or ""):
        _raise("这份作答的内容已经更新，旧分数不再作为当前成绩；请刷新后重新评分。",
               409, "version_conflict")
    value = None
    if score is not None and str(score).strip() != "":
        checked = r2_contract.validate_score(score, full)
        if not checked["ok"]:
            _raise(checked["message"] + "（本次没有写入任何记录）", 400, checked["code"])
        value = checked["value"]
    if target == SCORE_CONFIRMED:
        if not confirm:
            _raise("正式得分要勾选「确认成绩」后才计入统计；只存草稿不会影响平均分。",
                   400, "confirm_required")
        if value is None:
            _raise("没有分数的记录不能确认：没有依据时先存草稿，等教师给出分数再确认。",
                   400, "no_evidence")
    steps = _normalise_steps(steps)
    if steps:
        if value is None:
            _raise("填了步骤扣分就必须录入分数（按「满分−扣分」核对）。", 400, "not_number")
        consistent = r2_contract.step_deductions_consistent(full, value, steps)
        if not consistent["ok"]:
            _raise(consistent["message"] + "（本次没有写入任何记录，差额 %s 分）"
                   % r2_contract._fmt(abs(consistent["diff"] or 0)), 400, consistent["code"])
    live = _score_live(db, answer, question)
    previous = _score_rows(db, answer, question)
    reused = None
    if live:
        reused = live
    elif previous:
        for row in previous:
            if (row.get("answer_version") or "") == (answer.get("fingerprint") or ""):
                reused = row
                break
    from_state = (reused or {}).get("state") or SCORE_UNSCORED
    chain = _transition_chain(from_state, target)
    if reused and from_state == SCORE_CONFIRMED and (
            reused.get("score") != value or _loads(reused.get("steps_json"), []) != steps):
        if not str(revision_reason or "").strip():
            _raise("修改已确认的成绩必须填写修改理由（留痕：谁改、为什么改）。",
                   400, "revision_reason_required")
    now = now_iso()
    # 评分行记的是**该题**当前生效的标准版本（题级优先，历史批次回退批次级）
    rubric_version = question_rubric_version(config, question)
    before = _event_state(answer_dimensions(db, class_row, answer_id, question))
    history_entry = {
        "action": "confirm" if target == SCORE_CONFIRMED else "draft",
        "from": from_state,
        "to": target,
        "path": chain,
        "score": value,
        "full_score": full,
        "steps": steps,
        "rubric_version": rubric_version,
        "revision_reason": revision_reason or "",
        "by": user_id,
        "at": now,
    }
    with db.tx() as conn:
        if reused:
            history = _append_history(reused.get("history"), history_entry)
            conn.execute(
                "UPDATE answer_scores SET score=?, state=?, full_score=?, answer_version=?,"
                " rubric_version=?, steps_json=?, scored_by=?, scored_at=?, confirmed_by=?,"
                " confirmed_at=?, revision_reason=?, invalidated_at='', invalidated_reason='',"
                " invalidated_by=NULL, history=?, updated_at=? WHERE id=?",
                (
                    value, target, full, answer.get("fingerprint") or "", rubric_version,
                    _dump(steps), user_id, now,
                    user_id if target == SCORE_CONFIRMED else None,
                    now if target == SCORE_CONFIRMED else "",
                    revision_reason or "", history, now, reused["id"],
                ),
            )
            score_id = reused["id"]
        else:
            history = _dump([history_entry])
            conn.execute(
                "INSERT INTO answer_scores(class_id, student_id, answer_id, batch, question_no,"
                " full_score, score, state, answer_version, rubric_version, steps_json, scored_by,"
                " scored_at, confirmed_by, confirmed_at, revision_reason, history, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    class_row["id"], answer["student_id"], answer["id"],
                    answer.get("batch") or "", question, full, value, target,
                    answer.get("fingerprint") or "", rubric_version, _dump(steps), user_id, now,
                    user_id if target == SCORE_CONFIRMED else None,
                    now if target == SCORE_CONFIRMED else "", revision_reason or "", history,
                    now, now,
                ),
            )
            score_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _write_steps(conn, score_id, steps, now)
    plan = r2_contract.apply_event("score_saved", before,
                                   {"new_state": target})
    return {
        "answer_id": answer_id,
        "question_no": question,
        "score_id": score_id,
        "state": target,
        "formal": target == SCORE_CONFIRMED,
        "score": value,
        "full_score": full,
        "steps": steps,
        "rubric_version": rubric_version,
        "revision_reason": revision_reason or "",
        "actions": plan["actions"],
        "completeness": score_completeness_of(db, class_row, answer.get("batch") or "",
                                              answer["student_id"]),
    }


def confirm_score(db, class_row, user_id, answer_id, revision_reason="", question_no=None):
    """把分数确认成正式成绩（可一次把草稿分确认）。"""
    current = score_row(db, class_row, answer_id, question_no)
    if current["state"] == SCORE_CONFIRMED:
        _raise("这条成绩已经是「已确认」，不用再确认；要改分请填修改理由后重新保存。",
               400, "already_confirmed")
    value = current.get("score")
    if value is None:
        _raise("这条还没有分数（草稿为空），不能确认：请先录入分数。", 400, "no_evidence")
    steps = current.get("steps") or []
    return save_score(db, class_row, user_id, answer_id, score=value,
                      state=SCORE_CONFIRMED, steps=steps,
                      revision_reason=revision_reason, question_no=question_no, confirm=True)


def retract_score(db, class_row, user_id, answer_id, reason="", question_no=None):
    """撤回成绩（R2-INV-06）：回到未评分，退出正式统计，历史与扣分项保留。"""
    answer = _answer(db, class_row, answer_id)
    question = _score_question_no(answer, question_no)
    live = _score_live(db, answer, question)
    if not live:
        _raise("这条没有可撤回的成绩（可能已撤回或已失效）。", 400, "nothing_to_retract")
    from_state = live.get("state") or SCORE_UNSCORED
    try:
        r2_contract.transition_state(r2_contract.DIM_SCORE, from_state, SCORE_UNSCORED)
    except ValueError as exc:
        _raise(str(exc), 400, "bad_transition")
    now = now_iso()
    before = _event_state(answer_dimensions(db, class_row, answer_id, question))
    history = _append_history(live.get("history"), {
        "action": "retract",
        "from": from_state,
        "to": SCORE_UNSCORED,
        "score": live.get("score"),
        "reason": reason or "",
        "by": user_id,
        "at": now,
    })
    with db.tx() as conn:
        conn.execute(
            "UPDATE answer_scores SET state=?, score=NULL, confirmed_by=NULL, confirmed_at='',"
            " revision_reason=?, invalidated_at='', invalidated_reason='', invalidated_by=NULL,"
            " history=?, updated_at=? WHERE id=?",
            (SCORE_UNSCORED, reason or "", history, now, live["id"]),
        )
    dims = answer_dimensions(db, class_row, answer_id, question)
    plan = r2_contract.apply_event("score_retracted", before)
    return {
        "answer_id": answer_id,
        "question_no": question,
        "state": SCORE_UNSCORED,
        "formal": False,
        "score": None,
        "steps_kept": True,
        "actions": plan["actions"],
        "completeness": score_completeness_of(db, class_row, answer.get("batch") or "",
                                              answer["student_id"]),
    }


def invalidate_scores_for_answer(db, class_row, user_id, answer_id, reason="answer_updated"):
    """把一份作答的全部「活着」成绩标为失效（内容变化 / 标准变化时调用）。

    返回失效条数；行与步骤扣分都不删（`R2-INV-01`/`R2-INV-05`：留历史，不物理删除）。
    """
    answer = _answer(db, class_row, answer_id)
    rows = [row for row in _score_rows(db, answer)
            if row.get("state") in (SCORE_DRAFT, SCORE_CONFIRMED)]
    if not rows:
        return 0
    now = now_iso()
    for row in rows:
        try:
            r2_contract.transition_state(r2_contract.DIM_SCORE, row["state"], SCORE_INVALIDATED)
        except ValueError as exc:
            _raise(str(exc), 400, "bad_transition")
        history = _append_history(row.get("history"), {
            "action": "invalidate",
            "from": row["state"],
            "to": SCORE_INVALIDATED,
            "score": row.get("score"),
            "reason": reason or "",
            "by": user_id,
            "at": now,
        })
        with db.tx() as conn:
            conn.execute(
                "UPDATE answer_scores SET state=?, invalidated_at=?, invalidated_reason=?,"
                " invalidated_by=?, history=?, updated_at=? WHERE id=?",
                (SCORE_INVALIDATED, now, reason or "", user_id, history, now, row["id"]),
            )
    return len(rows)


def update_rubric(db, class_row, batch, user_id, question_no, rubric, rubric_version,
                  full_score=None):
    """改**某一题**的评分标准 / 满分（R2-INV-05）：必须同时给该题的新版本号。

    派单 §2.3 说标准是**按题**配的，§2.5 说失效的是「**受影响**的旧成绩」。所以：
      1. 版本下沉到题级：`rubric[q] = {"text","rubric_version","full_score"}`，其余题的版本一字不动；
      2. 首次改动时把其它题的「当前生效版本」冻结进各自的题级条目（历史批次的批次级版本只在
         它们还没有题级条目时回退使用），避免批次级 `rubric_version` 一 bump 就把别的题一起判失效；
      3. 只失效「该题版本/满分与现在不一致」的成绩行，其它题的 `confirmed` 行原样保留；
      4. 只改正文不给新版本、或版本号没变，一律拒绝（否则旧成绩不会失效）。
    """
    question = str(question_no or "").strip()
    if not question:
        _raise("要改哪一道题的评分标准？请给出题号。", 400, "question_required")
    batch = str(batch or "").strip()
    if not batch:
        _raise("改评分标准要给批次名（同一批次同题统一标准，跨批次不合并）。", 400, "missing_batch")
    version = str(rubric_version or "").strip()
    if not version:
        _raise("改评分标准必须同时写该题的新标准版本（rubric_version）；否则旧成绩不会失效。",
               400, "rubric_version_missing")
    config = _config(db, class_row, batch)
    current = question_rubric(config, question)
    if current["rubric_version"] == version:
        _raise("题 %s 的标准版本号还是「%s」：只改正文不改版本号，旧成绩不会失效，本次拒绝保存。"
               % (question, version), 400, "rubric_version_unchanged")
    text = str((rubric or {}).get("text") or (rubric or {}).get("criteria")
               or (rubric or {}).get("rubric") or "")
    full = current["full_score"]
    if full_score is not None:
        number = r2_contract._as_float(full_score)
        if number is None or number <= 0:
            _raise("满分必须是大于 0 的数值。", 400, "full_score_invalid")
        if r2_contract._decimals(number) > r2_contract.MAX_SCORE_DECIMALS:
            _raise("满分最多保留两位小数。", 400, "too_many_decimals")
        full = round(number, r2_contract.MAX_SCORE_DECIMALS)
    new_config = dict(config)
    new_config["rubrics"] = {str(key): dict(value) for key, value in
                             (config.get("rubrics") or {}).items() if isinstance(value, dict)}
    new_config["rubric"] = dict(config.get("rubric") or {})
    new_config["full_scores"] = dict(config.get("full_scores") or {})
    # 冻结其它题的当前生效版本：题级条目已存在的原样保留，缺的按当前生效版本补齐，
    # 这样题级版本一旦落地，批次级 `rubric_version` 再也不会被当成「所有题共用的版本」。
    questions = []
    for source in (r2_contract.expected_question_nos(config),
                   r2_contract.scored_question_nos(config),
                   tuple((config.get("full_scores") or {}).keys()),
                   tuple((config.get("rubric") or {}).keys())):
        for item in source:
            key = str(item).strip()
            if key and key not in questions:
                questions.append(key)
    for item in questions:
        if item == question:
            continue
        entry = question_rubric(config, item)
        new_config["rubrics"][item] = _rubric_payload(entry["text"], entry["rubric_version"],
                                                      entry["full_score"])
        # 旧列保留原文镜像（R2-00.2 及以前的读法仍能读到文字；权威仍以 rubrics 为准）
        new_config["rubric"][item] = entry["text"]
    new_config["rubrics"][question] = _rubric_payload(text, version, full)
    new_config["rubric"][question] = text
    if full_score is not None:
        new_config["full_scores"][question] = full
    # 批次级 `rubric_version` 只在它本来就为空时补一个值（历史兼容显示用）；
    # 有题级条目以后它不再是权威，绝不拿它去 bump 别的题。
    if not str(config.get("rubric_version") or "").strip():
        new_config["rubric_version"] = version
    try:
        saved = save_batch_config(db, class_row["id"], batch, new_config, user_id=user_id)
    except ValueError as exc:
        _raise("批次配置不合法：%s" % exc, 400, "batch_config_invalid")
    invalidated = _invalidate_rubric_scores(db, class_row, user_id, batch, question, saved)
    return {
        "batch": batch,
        "question_no": question,
        "rubric_version": version,
        "question_versions": {item: question_rubric_version(saved, item)
                              for item in questions},
        "config": saved,
        "invalidated": invalidated,
        "actions": r2_contract.apply_event(
            "rubric_changed",
            {r2_contract.DIM_SCORE: {"state": SCORE_CONFIRMED, "question_no": question,
                                     "rubric_version": current["rubric_version"] or ""}},
            {"rubric_version": version, "question_no": question},
        )["actions"],
    }


def _invalidate_rubric_scores(db, class_row, user_id, batch, question, config):
    """标准/满分变化后让**受影响**的成绩失效（派单 §2.5「受影响的旧成绩失效」）。

    判定按题比：把每一条 `draft/confirmed` 行的 `rubric_version`/`full_score` 与该行题号的
    **当前生效版本**（`question_rubric()`：题级优先、历史批次回退批次级）比较，不一致才失效。
    改第 3 题时，第 1/2/4/5 题的版本号没动 → 它们保持 `confirmed`，分数一字不变。
    """
    rows = db.query(
        "SELECT * FROM answer_scores WHERE class_id=? AND batch=?"
        " AND state IN ('draft','confirmed')", (class_row["id"], batch),
    )
    now = now_iso()
    count = 0
    for raw in rows:
        row = dict(raw)
        row_question = str(row.get("question_no") or "")
        expected_version = question_rubric_version(config, row_question)
        expected_full = question_rubric(config, row_question)["full_score"]
        version_ok = (not expected_version
                      or str(row.get("rubric_version") or "") == expected_version)
        full_ok = (expected_full is None or row.get("full_score") is None
                   or abs(float(row.get("full_score")) - float(expected_full)) <= 1e-9)
        if version_ok and full_ok:
            continue
        r2_contract.transition_state(r2_contract.DIM_SCORE, row["state"], SCORE_INVALIDATED)
        history = _append_history(row.get("history"), {
            "action": "invalidate",
            "from": row["state"],
            "to": SCORE_INVALIDATED,
            "reason": "rubric_changed",
            "rubric_version": expected_version,
            "full_score": expected_full,
            "changed_question": question,
            "by": user_id,
            "at": now,
        })
        with db.tx() as conn:
            conn.execute(
                "UPDATE answer_scores SET state=?, invalidated_at=?,"
                " invalidated_reason='rubric_changed', invalidated_by=?, history=?,"
                " updated_at=? WHERE id=?",
                (SCORE_INVALIDATED, now, user_id, history, now, row["id"]),
            )
        count += 1
    return count


def update_answer_content(db, class_row, user_id, answer_id, body):
    """校正作答内容（R2-INV-01）：委托 R1 更新内容，再把评分标失效、核验读成 stale。

    R1 的指纹比较已经让旧核验结论退出当前视图（`answer_version<>fingerprint`），所以这里
    只需要补 R2 新表的动作：评分行标 `invalidated`（不删行），并把动作清单回给界面提示。
    """
    answer = _answer(db, class_row, answer_id)
    try:
        result = answer_check.update_content(db, class_row, user_id, answer_id, body)
    except answer_check.AnswerCheckError as exc:
        _raise(exc.message, exc.status, exc.code)
    invalidated = invalidate_scores_for_answer(db, class_row, user_id, answer_id,
                                               reason="answer_updated")
    dims = answer_dimensions(db, class_row, answer_id)
    plan = r2_contract.apply_event(
        "answer_content_changed",
        {r2_contract.DIM_ANSWER: {"state": dims["dimensions"]["answer"]["state"],
                                  "fingerprint": answer.get("fingerprint") or ""}},
        {"fingerprint": _answer(db, class_row, answer_id).get("fingerprint") or ""},
    )
    return {"answer_id": answer_id, "content": result, "invalidated_scores": invalidated,
            "dimensions": dims["dimensions"], "actions": plan["actions"]}


# ---------------------------------------------------------------------------
# 九、成绩口径（完整成绩 / 评分进度；R2-03 直接用）
# ---------------------------------------------------------------------------


def score_completeness_of(db, class_row, batch, student_id):
    """一名学生在一个批次的完整成绩判定（口径来自 `r2_contract.score_completeness`）。"""
    config = _config(db, class_row, batch)
    rows = db.query(
        "SELECT question_no, score FROM answer_scores WHERE class_id=? AND batch=?"
        " AND student_id=? AND state='confirmed'", (class_row["id"], batch, student_id),
    )
    confirmed = {}
    for row in rows:
        question = str(row["question_no"] or "")
        if question and row["score"] is not None:
            confirmed[question] = row["score"]
    result = r2_contract.score_completeness(config, confirmed)
    total = r2_contract.assignment_total_score(config, confirmed)
    result["batch"] = batch
    result["student_id"] = student_id
    result["total_score"] = total["total"]
    result["full_total"] = r2_contract.batch_full_score(config)
    result["label"] = "完整成绩" if result["complete"] else (
        "暂无" if result["reason"] == "batch_unscored" else "成绩不完整")
    return result


def current_checks(db, class_row, batch=""):
    """当前版本的核验结论行（R2-03 看板、详情页、统计共用**同一判据**：C6 的关闭方式）。

    判据：`answer_checks.answer_version = answers.fingerprint`（撤回行 `answer_version=''`
    永远不是当前结论）。返回扁平 dict，字段就是看板与详情要显示的那些，不再二次拼装。
    """
    sql = (
        "SELECT c.answer_id, c.class_id, c.student_id, c.batch, a.question_no,"
        " c.answer_version, c.progress, c.locate_status, c.locate_source, c.cause_state,"
        " c.cause_source, c.cause_code, c.cause_label, c.observed_step, c.first_bad_index,"
        " c.verify_source, c.teacher_error_text, c.teacher_note, c.basis_note, c.decided_at,"
        " c.decided_by, c.updated_at, c.cause_history"
        " FROM answer_checks c JOIN answers a ON a.id=c.answer_id AND a.class_id=c.class_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint"
    )
    params = [class_row["id"]]
    if batch:
        sql += " AND c.batch=?"
        params.append(batch)
    sql += " ORDER BY c.answer_id ASC, c.id DESC"
    return [dict(row) for row in db.query(sql, tuple(params))]


def current_scores(db, class_row, batch="", question_no=""):
    """当前有效评分行（正式统计只用 `confirmed`；`draft` 回显但不进分子）。

    判据三层，缺一不可：`state IN ('draft','confirmed')`、`answer_version = answers.fingerprint`、
    `rubric_version` 与**该题**当前生效版本一致（改别的题的标准不会牵连本题）。
    """
    sql = (
        "SELECT s.id, s.answer_id, s.student_id, s.batch, s.question_no, s.score, s.full_score,"
        " s.state, s.answer_version, s.rubric_version, s.steps_json, s.scored_by, s.scored_at,"
        " s.confirmed_by, s.confirmed_at, s.revision_reason, s.history, a.question_no AS"
        " answer_question_no"
        " FROM answer_scores s"
        " JOIN answers a ON a.id=s.answer_id AND a.class_id=s.class_id"
        " WHERE s.class_id=? AND s.state IN ('draft','confirmed')"
        " AND s.answer_version=a.fingerprint"
    )
    params = [class_row["id"]]
    if batch:
        sql += " AND s.batch=?"
        params.append(batch)
    if question_no:
        sql += " AND s.question_no=?"
        params.append(str(question_no))
    sql += " ORDER BY s.answer_id ASC, s.id DESC"
    configs = {}
    rows = []
    for raw in db.query(sql, tuple(params)):
        row = dict(raw)
        name = row.get("batch") or ""
        if name not in configs:
            configs[name] = _config(db, class_row, name)
        entry = question_rubric(configs[name], row.get("question_no") or "")
        expected_version = entry["rubric_version"]
        full_ok = (entry["full_score"] is None or row.get("full_score") is None
                   or abs(float(row.get("full_score")) - float(entry["full_score"])) <= 1e-9)
        row["current_rubric_version"] = expected_version
        row["formal"] = (row["state"] == SCORE_CONFIRMED
                         and (not expected_version
                              or str(row.get("rubric_version") or "") == expected_version)
                         and full_ok)
        row["steps"] = _loads(row.pop("steps_json", "[]"), [])
        rows.append(row)
    return rows


def scoring_progress(db, class_row, batch):
    """批次级评分进度与覆盖率（分母口径与 `r2_contract.scoring_students` 对齐）。

    覆盖率分母 = **应参与评分学生数**（在册名单里至少有一道应评分题目的学生去重），
    不是「有作答行的学生数」：名单里 10 人、2 人完全没交时，分母仍是 10——
    否则没交的学生从分母里掉出去，覆盖率会被算高，看板与 PDF 就会给出两个不同的数。
    未配置名单的历史批次才退回「有作答行的学生」，并在 `denominator_source` 里写明。
    """
    config = _config(db, class_row, batch)
    scored = list(r2_contract.scored_question_nos(config))
    roster = list(r2_contract.expected_student_keys(config))
    answers = db.query(
        "SELECT student_id, student_code, student_name, question_no FROM answers"
        " WHERE class_id=? AND batch=?", (class_row["id"], batch),
    )
    by_student = {}
    for row in answers:
        key = (row["student_code"] or "").strip() or (row["student_name"] or "").strip() \
            or str(row["student_id"])
        by_student.setdefault(row["student_id"], {"key": key, "rows": []})
        by_student[row["student_id"]]["rows"].append(row)
    if roster and scored:
        # 名单 × 应评分题：含「未提交格子」，这是契约 §2.3 的分母口径
        records = [{"student_key": key, "question_no": question}
                   for key in roster for question in scored]
        source = "roster"
    else:
        records = []
        for info in by_student.values():
            for row in info["rows"]:
                if not scored or str(row["question_no"] or "") in scored:
                    records.append({"student_key": info["key"],
                                    "question_no": str(row["question_no"] or "")})
        source = "answers"
    eligible = r2_contract.scoring_students(config, records)
    confirmed = {}
    for row in db.query(
            "SELECT student_id, question_no, score FROM answer_scores WHERE class_id=?"
            " AND batch=? AND state='confirmed'", (class_row["id"], batch)):
        confirmed.setdefault(row["student_id"], {})[str(row["question_no"] or "")] = row["score"]
    complete = 0
    totals = []
    for student_id, scores in confirmed.items():
        result = r2_contract.assignment_total_score(config, scores)
        if result["complete"]:
            complete += 1
            totals.append(result["total"])
    denominator = len(eligible)
    return {
        "batch": batch,
        "config_state": config.get("config_state") or "",
        "scored_questions": scored,
        "full_total": r2_contract.batch_full_score(config),
        "eligible_students": denominator,
        "denominator_source": source,
        "confirmed_students": complete,
        "coverage": round(complete / denominator, 4) if denominator else None,
        "coverage_text": (("%.1f%%" % (100.0 * complete / denominator)) if denominator
                          else r2_contract.STAT_UNDEFINED_TEXT),
        "average": round(sum(totals) / len(totals), 2) if totals else None,
        "average_text": ("%.2f" % (sum(totals) / len(totals))) if totals
                        else r2_contract.STAT_EMPTY_TEXT,
    }


def score_sheet(db, class_row, batch, question_no=""):
    """按题看评分进度：该题应评份数 / 已确认 / 草稿 / 未评 / 已失效 / 得分率。"""
    config = _config(db, class_row, batch)
    questions = [str(question_no)] if question_no else list(r2_contract.scored_question_nos(config))
    answers = db.query(
        "SELECT id, student_id, question_no FROM answers WHERE class_id=? AND batch=?"
        " AND source<>?", (class_row["id"], batch, DEMO_SOURCE),
    )
    out = []
    for question in questions:
        full = r2_contract.full_score_of(config, question)
        scope = [row for row in answers
                 if str(row["question_no"] or "") == question]
        scored = db.query(
            "SELECT answer_id, score, state FROM answer_scores WHERE class_id=? AND batch=?"
            " AND question_no=?", (class_row["id"], batch, question),
        )
        confirmed = [row for row in scored if row["state"] == SCORE_CONFIRMED]
        draft = [row for row in scored if row["state"] == SCORE_DRAFT]
        invalid = [row for row in scored if row["state"] == SCORE_INVALIDATED]
        total = 0.0
        for row in confirmed:
            total += float(row["score"] or 0)
        count = len(confirmed)
        out.append({
            "batch": batch,
            "question_no": question,
            "full_score": full,
            "answers": len(scope),
            "confirmed": count,
            "draft": len(draft),
            "invalidated": len(invalid),
            "unscored": max(len(scope) - count - len(draft), 0),
            "average": round(total / count, 2) if count else None,
            "rate": round(total / (count * full), 4) if count and full else None,
            "undefined": "暂无" if not count or not full else "",
        })
    return {"batch": batch, "config_state": config.get("config_state") or "",
            "rubric_version": config.get("rubric_version") or "",
            "full_total": r2_contract.batch_full_score(config),
            "questions": out}


# ---------------------------------------------------------------------------
# 十、给 R2-03 / R2-05 的只读接口（确定性、按批次与筛选，不写库）
# ---------------------------------------------------------------------------


def _effective_row(row, knowledge=None):
    """把 `batch_rows` 的一行压成统计友好的扁平结构（只保留当前版本数据）。"""
    dims = row["dimensions"]
    verify = dims["verify"]
    cause = dims["cause"]
    score = dims["score"]
    return {
        "answer_id": row["answer_id"],
        "student_id": row["student_id"],
        "student_name": row["student_name"],
        "student_code": row["student_code"],
        "question_no": row["question_no"],
        "batch": row["batch"],
        "occurred_on": row.get("occurred_on") or "",
        "source": row["source"],
        "synthetic": row["synthetic"],
        "formal_candidate": row.get("synthetic") is False,
        "answer_state": dims["answer"]["state"],
        "answer_reason": dims["answer"].get("reason") or "",
        "valid_answer": dims["answer"]["state"] == "valid",
        "verify_state": verify["state"],
        "verify_source": verify.get("source") or "",
        "verified": verify["state"] in _VERIFIED_STATES,
        "cause_state": cause["state"],
        "cause_code": cause.get("code") or "",
        "cause_label": cause.get("label_text") or "",
        "score_state": score["state"],
        "score": score.get("score"),
        "formal_score": bool(score.get("formal")),
        "images": row.get("images") or 0,
        # 知识点与错题一一对应（同一作答最多一条错题）：统计侧不再另开一口查 mistakes（C6）
        "knowledge_points": list((knowledge or {}).get(row["answer_id"]) or []),
    }


def effective_rows(db, class_row, batch="", question_no="", cause_state="", score_state="",
                   student_id=None, knowledge_point="", only_valid=False):
    """§「有效核验 / 有效确认评分 / 有效确认错因」的统一只读查询（R2-03 直接用）。

    口径（全部来自契约，不在统计模块里重写）：
      - 只返回当前版本的数据（核验/错因按 `answer_version`，评分按 `is_current_version`）；
      - `formal_score=True` 才是「有效确认评分」；`cause_state='decided'` 才是「有效确认错因」；
      - `knowledge_point` 过滤读 `mistakes.knowledge_points`（同一作答多条知识点分别命中）；
      - 每行都带 `source`（演示隔离由统计侧单点收口）、`batch`、`occurred_on`、`knowledge_points`。
    """
    rows = batch_rows(db, class_row, batch, scope=SCOPE_ALL)
    knowledge = {}
    for row in db.query(
            "SELECT answer_id, knowledge_points FROM mistakes"
            " WHERE class_id=? AND answer_id IS NOT NULL", (class_row["id"],)):
        knowledge[row["answer_id"]] = _knowledge_points(row["knowledge_points"])
    out = []
    target = str(knowledge_point or "").strip()
    for row in rows:
        item = _effective_row(row, knowledge)
        if question_no and item["question_no"] != str(question_no):
            continue
        if student_id is not None and item["student_id"] != student_id:
            continue
        if cause_state and item["cause_state"] != cause_state:
            continue
        if score_state and item["score_state"] != score_state:
            continue
        if only_valid and not item["valid_answer"]:
            continue
        if target:
            points = knowledge.get(item["answer_id"]) or []
            if target not in points:
                continue
            item["knowledge_point"] = target
        out.append(item)
    return out


def valid_verifications(db, class_row, batch=""):
    """有效核验数（按来源拆分）+ 明细：R2-03 的 `unverified_answers` 用它算分母。"""
    rows = effective_rows(db, class_row, batch)
    verified = [row for row in rows if row["verified"]]
    by_source = {}
    for row in verified:
        key = row["verify_source"] or "rule"
        by_source[key] = by_source.get(key, 0) + 1
    return {"batch": batch or "", "count": len(verified), "by_source": by_source,
            "answer_ids": [row["answer_id"] for row in verified],
            "total_answers": len(rows),
            "unverified": len([row for row in rows if not row["verified"]])}


def confirmed_scores(db, class_row, batch="", question_no=""):
    """有效确认评分（只认 `state='confirmed'` 且版本/标准都对得上的行）。"""
    rows = effective_rows(db, class_row, batch, question_no=question_no)
    return [row for row in rows if row["formal_score"]]


def confirmed_causes(db, class_row, batch=""):
    """有效确认错因（`cause_state='decided'` 且核验结论仍在当前版本）。"""
    rows = effective_rows(db, class_row, batch)
    return [row for row in rows if row["cause_state"] == "decided" and row["verified"]]


def filter_options(db, class_row, batch=""):
    """筛选参数（批次 / 题号 / 错因 / 知识点）：界面与统计共用同一份候选值。"""
    rows = effective_rows(db, class_row, batch)
    batches = sorted({row["batch"] for row in rows if row["batch"]})
    questions = sorted({row["question_no"] for row in rows if row["question_no"]})
    causes = sorted({row["cause_code"] for row in rows if row["cause_code"]})
    knowledge = set()
    for raw in db.query(
            "SELECT knowledge_points FROM mistakes WHERE class_id=? AND status='confirmed'"
            " AND answer_id IS NOT NULL", (class_row["id"],)):
        for point in _knowledge_points(raw["knowledge_points"]):
            knowledge.add(point)
    students = sorted({(row["student_id"], row["student_name"]) for row in rows})
    return {
        "batches": batches,
        "questions": questions,
        "cause_codes": causes,
        "knowledge_points": sorted(knowledge),
        "students": [{"student_id": sid, "name": name} for sid, name in students],
        "scopes": [{"key": key, "label": label} for key, label in SCOPE_LABELS.items()],
    }


def review_progress(db, class_row, batch=""):
    """R2-02 界面的「结果汇总」：核验/错因/评分三块进度 + 完整成绩人数。"""
    rows = batch_rows(db, class_row, batch, scope=SCOPE_ALL)
    progress = _batch_progress_rows(rows)
    students = {row["student_id"] for row in rows}
    complete = 0
    for student_id in students:
        if score_completeness_of(db, class_row, batch or rows[0]["batch"] if rows else batch,
                                 student_id)["complete"]:
            complete += 1
    progress["complete_students"] = complete
    progress["students"] = len(students)
    progress["batch"] = batch or ""
    return progress


__all__ = [
    "GradingError",
    "SCOPE_PENDING", "SCOPE_ALL", "SCOPE_IDS", "SCOPES", "SCOPE_LABELS",
    "SCORE_UNSCORED", "SCORE_DRAFT", "SCORE_CONFIRMED", "SCORE_INVALIDATED",
    "KNOWLEDGE_UNCLASSIFIED", "MISTAKE_SOURCE", "LOCAL_ONLY_FLOWS",
    "local_decision", "guard_egress",
    "answer_dimensions", "batch_rows", "batch_progress", "review_progress",
    "verify_one", "batch_verify", "retry_failed",
    "cause_candidates", "confirm_cause", "retract_cause",
    "mistake_of", "set_knowledge_point",
    "score_full_of", "score_row", "save_score", "confirm_score", "retract_score",
    "update_rubric", "invalidate_scores_for_answer", "update_answer_content",
    "score_completeness_of", "score_sheet", "scoring_progress", "current_checks", "current_scores",
    "question_rubric", "question_rubric_version",
    "effective_rows", "valid_verifications", "confirmed_scores", "confirmed_causes",
    "filter_options",
]
