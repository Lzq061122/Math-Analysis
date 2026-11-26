# -*- coding: utf-8 -*-
"""R2-03 学情统计：18 项指标的确定性计算（只读，不写任何表）。

设计约束（派单 §5.1/§5.2，契约 R2-00.2 §10）：

1. **取数只有一个入口** `_formal_rows()`：批次作用域与演示隔离都在**取数时**完成，
   指标函数只接收过滤后的行；除白名单（`SYNTHETIC_ALLOWED_FUNCS`）外，任何函数都不得
   再判 `source`/`synthetic`（`tests/verify_r2_03_stats.py` 有 AST 逐函数体检）。
2. **口径判断不在本模块重写**：非缺测、应参与评分学生数、重复错误对、完整成绩、
   当前版本判定全部调 `r2_contract`；四维状态取 `grading.effective_rows()`（C6 的关闭方式：
   统计与详情共用同一判据）。
3. **确定性**：不读系统时钟（`today` 由调用方传入）、不遍历 set、所有列表显式排序，
   因此同一数据两次计算与重启后再算逐字段相等。
4. **缺分母不拿 0 冒充**：`None` 上抛，界面/PDF 只认 `STAT_EMPTY_TEXT` / `STAT_UNDEFINED_TEXT`。

坐标系：
- 班级看板 = `class_snapshot()`；个人看板 = `student_snapshot()`；R2-04 的 PDF 用同一份快照。
- 筛选：班级/批次作用于全部指标；日期按**批次业务日期**；学生筛选只收窄明细与学生级指标，
  班级总量指标（作业总分/平均分/评分覆盖率）默认不变，切「群体成绩」视图才变（并在 notes 注明）。
"""

import json

from . import db as store
from . import grading
from . import r2_contract as contract
from . import tutor

STATS_VERSION = "R2-03.1"

# 唯一允许接触 source / synthetic 的函数白名单（展示用途只能加在这里，
# 且每个被加进来的函数都必须有行为断言覆盖；见 verify_r2_03_stats.py 第 7 组）
SYNTHETIC_ALLOWED_FUNCS = ("_formal_rows", "_demo_badge")

UNCLASSIFIED = tutor.UNCLASSIFIED
NOT_APPLICABLE_TEXT = contract.STAT_UNDEFINED_TEXT
EMPTY_TEXT = contract.STAT_EMPTY_TEXT

METRIC_KEYS = tuple(item["key"] for item in contract.R2_STAT_METRICS)
RANKING_KEYS = tuple(item["key"] for item in contract.R2_STAT_RANKINGS)

# 学生筛选默认不改这些指标（§5.2 + captain 裁定 D7）
CLASS_LEVEL_KEYS = ("assignment_total_score", "average_score", "score_coverage")
# 知识点/错因筛选只影响这些指标（错题分布 / 涉及学生 / 证据明细）
DISTRIBUTION_KEYS = ("confirmed_mistakes", "knowledge_error_freq", "affected_students",
                     "cause_freq", "undecided_causes", "repeated_errors")

_FILTER_KEYS = ("date_from", "date_to", "student_ids", "knowledge_point", "cause", "group_scores")


# ---------------------------------------------------------------------------
# 一、取数唯一入口（演示隔离 + 批次作用域）
# ---------------------------------------------------------------------------


def _class_row(db, class_id):
    row = db.query_one("SELECT * FROM classes WHERE id=?", (int(class_id),))
    if row is None:
        raise ValueError("班级不存在：%s" % class_id)
    return dict(row)


def _demo_badge(db, class_row, batch, rows_all):
    """本批次的演示数据标记（白名单函数：展示用途才允许看 `source`/`synthetic`）。

    - `demo_batch=True`：整个批次只有合成演示数据（正式统计整批「暂无」，界面标注来源）；
    - `demo_rows`：范围内被排除的演示行数（界面提示「已排除 n 条演示数据」）。
    """
    batch = str(batch or "")
    demo_rows = 0
    real_rows = 0
    for row in rows_all or []:
        if not contract.include_in_formal_stats(row.get("source"), False):
            demo_rows += 1
        else:
            real_rows += 1
    batch_is_synthetic = False
    if batch:
        try:  # 判据的唯一来源在 answer_check（契约 §2.2 的 C2 关闭方式）
            from . import answer_check
            batch_is_synthetic = bool(answer_check._batch_is_synthetic(db, class_row["id"], batch))
        except Exception:  # noqa: BLE001 - 演示判定出错不能把真实批次整批排除
            batch_is_synthetic = False
    demo_batch = bool(batch) and batch_is_synthetic and real_rows == 0
    note = ""
    if demo_batch:
        note = "本批次为合成演示，正式统计不含演示数据"
    elif demo_rows:
        note = "已排除 %d 条合成演示数据，不计入正式统计" % demo_rows
    return {"demo_batch": demo_batch, "demo_rows": demo_rows, "note": note}


def _formal_rows(rows, batch="", demo_batch=False,
                 batch_is_synthetic=None, filters=None):
    """唯一取数入口：只留「正式统计」该看的行。

    过滤在这里一次做完（演示隔离 + 批次作用域）；**任何指标都不得再判一次**。
    `filters` 在这一层只用于「学生」收窄的**范围标记**不作判断——
    学生/知识点/错因筛选由 `_student_rows()` / `_distribution_rows()` 负责，
    这样班级总量指标不会被明细筛选带偏（§5.2）。
    """
    del filters  # 显式说明：入口不做明细级筛选，避免与班级总量口径混淆
    want_batch = str(batch or "")
    synthetic = bool(demo_batch if batch_is_synthetic is None else batch_is_synthetic)
    out = []
    for row in rows or []:
        if not contract.include_in_formal_stats(row.get("source"), synthetic):
            continue
        if want_batch and str(row.get("batch") or "") != want_batch:
            continue
        out.append(row)
    return out


def _student_rows(rows, student_ids=None):
    """学生筛选（只收窄明细与学生级指标，不改班级总量口径）。"""
    chosen = {int(sid) for sid in (student_ids or ())}
    if not chosen:
        return list(rows)
    return [row for row in rows if int(row.get("student_id") or 0) in chosen]


def _distribution_rows(rows, knowledge_point="", cause=""):
    """知识点/错因筛选：只作用于错题分布、涉及学生与证据明细。"""
    target_kp = str(knowledge_point or "").strip()
    target_cause = str(cause or "").strip()
    out = []
    for row in rows:
        if target_kp and target_kp not in (row.get("knowledge_points") or []):
            continue
        if target_cause and str(row.get("cause_code") or "") != target_cause:
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 二、小工具
# ---------------------------------------------------------------------------


def _key(row):
    """学生稳定标识：学号优先，否则姓名（与批次配置 expected_students[].key 同一口径）。"""
    text = str(row.get("student_code") or "").strip()
    if text:
        return text
    text = str(row.get("student_name") or "").strip()
    if text:
        return text
    return str(row.get("student_id") or "")


def _dedupe(seq):
    out = []
    for item in seq:
        if item not in out:
            out.append(item)
    return out


def _pct(value, digits=1):
    if value is None:
        return NOT_APPLICABLE_TEXT
    return ("%." + str(digits) + "f%%") % (100.0 * float(value))


def _number_text(value):
    if value is None:
        return EMPTY_TEXT
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return ("%.2f" % number).rstrip("0").rstrip(".")


def _is_confirmed_mistake(row, checks):
    """已确认错题：当前版本 first_bad 且错因已确认（`missing_steps` 不计，见 §10 第 9 项）。"""
    if str(row.get("cause_state") or "") != "decided":
        return False
    if not row.get("verified"):
        return False
    check = checks.get(row.get("answer_id")) or {}
    return str(check.get("locate_status") or "") == "first_bad"


def _bucket(items, field):
    """按 (学生, 类别) 聚合，同一条错题同类别只计一次；输出按次数降序、名称升序。"""
    data = {}
    order = []
    for row in items:
        student = _key(row)
        values = row.get(field) or []
        if isinstance(values, str):
            values = [values]
        seen = []
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.append(text)
            key = (student, text)
            if key not in data:
                data[key] = {"student_key": student, "name": text, "count": 0,
                             "answer_ids": [], "student_name": row.get("student_name") or ""}
                order.append(key)
            entry = data[key]
            entry["count"] += 1
            if row.get("answer_id") not in entry["answer_ids"]:
                entry["answer_ids"].append(row.get("answer_id"))
    grouped = {}
    for key in order:
        entry = data[key]
        bucket = grouped.setdefault(entry["name"], {"name": entry["name"], "mistakes": 0,
                                                    "students": [], "answer_ids": []})
        bucket["mistakes"] += entry["count"]
        if entry["student_key"] not in bucket["students"]:
            bucket["students"].append(entry["student_key"])
        for aid in entry["answer_ids"]:
            if aid not in bucket["answer_ids"]:
                bucket["answer_ids"].append(aid)
    items_out = []
    for name, bucket in grouped.items():
        items_out.append({
            "name": name,
            "mistakes": bucket["mistakes"],
            "student_count": len(bucket["students"]),
            "students": sorted(bucket["students"]),
            "answer_ids": sorted(bucket["answer_ids"], key=lambda value: (value is None, value)),
        })
    items_out.sort(key=lambda item: (-item["mistakes"], -item["student_count"], item["name"]))
    return items_out


def _batch_business_date(db, class_row, batch):
    """批次业务日期：配置日期 → 回退该批次作答日期非空最小值 → 都没有则为空（D6）。"""
    batch = str(batch or "")
    config = store.load_batch_config(db, class_row["id"], batch)
    configured = str(config.get("occurred_on") or "").strip()
    if configured:
        return {"date": configured, "date_source": "batch_config"}
    row = db.query_one(
        "SELECT MIN(occurred_on) AS value FROM answers"
        " WHERE class_id=? AND batch=? AND occurred_on<>''",
        (class_row["id"], batch),
    ) or {}
    fallback = str(row.get("value") or "").strip()
    if fallback:
        return {"date": fallback, "date_source": "answers"}
    return {"date": "", "date_source": ""}


def _score_rows(rows):
    """逐生确认得分：{student_id: {题号: 分数}}（双闸门：formal_score 且 state=='confirmed'）。"""
    data = {}
    for row in rows:
        if not row.get("formal_score"):
            continue
        if str(row.get("score_state") or "") != "confirmed":
            continue
        if row.get("score") is None:
            continue
        data.setdefault(int(row["student_id"]), {})[str(row.get("question_no") or "")] = row["score"]
    return data


def _roster_cells(config, student_ids=None):
    """名单 × 应评分题 的格子（含未提交格子），用于覆盖率分母。"""
    roster = list(contract.expected_student_keys(config))
    scored = list(contract.scored_question_nos(config))
    chosen = {str(item) for item in (student_ids or ())}
    if chosen:
        roster = [key for key in roster if key in chosen]
    return [{"student_key": key, "question_no": question} for key in roster for question in scored]


def _filter_text(filters, batch):
    parts = []
    if batch:
        parts.append("批次=%s" % batch)
    if filters.get("date_from") or filters.get("date_to"):
        parts.append("日期=%s~%s" % (filters.get("date_from") or "…", filters.get("date_to") or "…"))
    if filters.get("student_ids"):
        parts.append("学生=%s" % ",".join(str(sid) for sid in filters.get("student_ids")))
    if filters.get("knowledge_point"):
        parts.append("知识点=%s" % filters["knowledge_point"])
    if filters.get("cause"):
        parts.append("错因=%s" % filters["cause"])
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 三、快照装配
# ---------------------------------------------------------------------------


def list_batches(db, class_id):
    """批次选择器（关闭 R1 遗留 C1：统计块可以自选批次，含历史未配置批次）。"""
    out = []
    class_row = {"id": int(class_id)}
    for config in store.list_batch_configs(db, class_id):
        business = _batch_business_date(db, class_row, config.get("batch") or "")
        out.append({
            "batch": config.get("batch") or "",
            "name": config.get("name") or config.get("batch") or "",
            "occurred_on": config.get("occurred_on") or "",
            "business_date": business["date"],
            "business_date_source": business["date_source"],
            "config_state": config.get("config_state") or "",
            "origin": config.get("origin") or "",
        })
    return out


def class_snapshot(db, class_id, batch="", filters=None, today="", group_scores=False):
    """班级学情快照（R2-04 的 PDF 复用同一份，不做二次计算）。"""
    return _snapshot(db, class_id, batch=batch, filters=filters, today=today,
                     student_id=None, group_scores=group_scores)


def student_snapshot(db, class_id, student_id, batch="", filters=None, today="",
                     group_scores=True):
    """个人学情快照（个人看板与个人报告共用）。"""
    return _snapshot(db, class_id, batch=batch, filters=filters, today=today,
                     student_id=int(student_id), group_scores=group_scores)


def _snapshot(db, class_id, batch="", filters=None, today="", student_id=None,
              group_scores=False):
    filters = dict(filters or {})
    # 「群体成绩」视图：默认关；个人看板/个人报告默认开（只算这名学生的成绩）
    filters["group_scores"] = bool(filters.get("group_scores") or group_scores)
    if student_id is not None:
        filters["student_ids"] = [int(student_id)]
    class_row = _class_row(db, class_id)
    batch = str(batch or "")
    config = store.load_batch_config(db, class_id, batch)

    rows_all = grading.effective_rows(db, class_row, batch)
    check_rows = grading.current_checks(db, class_row, batch)
    checks = {}
    for item in check_rows:
        checks.setdefault(int(item["answer_id"]), item)

    badge = _demo_badge(db, class_row, batch, rows_all)
    scope_rows = _formal_rows(rows_all, batch=batch, demo_batch=badge["demo_batch"],
                              filters=filters)
    student_ids = [int(sid) for sid in (filters.get("student_ids") or ())]
    student_rows = _student_rows(scope_rows, student_ids)
    dist_rows = _distribution_rows(student_rows, filters.get("knowledge_point") or "",
                                   filters.get("cause") or "")

    notes = []
    business = _batch_business_date(db, class_row, batch)
    date_from = str(filters.get("date_from") or "").strip()
    date_to = str(filters.get("date_to") or "").strip()
    excluded_by_date = False
    if (date_from or date_to) and batch:
        if not business["date"]:
            excluded_by_date = True
            notes.append("1 个批次缺少业务日期，未计入日期筛选")
        elif (date_from and business["date"] < date_from) or (date_to and business["date"] > date_to):
            excluded_by_date = True
            notes.append("批次业务日期 %s 不在所选日期范围内，未计入"
                         % business["date"])
    if excluded_by_date:
        scope_rows = []
        student_rows = []
        dist_rows = []
    if badge["note"]:
        notes.append(badge["note"])
    if batch and config.get("config_state") != contract.BATCH_CONFIG_STATE_CONFIGURED:
        notes.append("该批次未配置应作答名单/题号，应作答人数、未提交数、作业总分等按「暂无」处理")

    # ---- 班级人数与应作答人数（D7：学生筛选会改变，kp/cause 不改变） ----
    active_students = [dict(row) for row in db.query(
        "SELECT id, name, code FROM students WHERE class_id=? AND active=1 ORDER BY id",
        (int(class_id),))]
    min_keys = [str(row.get("code") or row.get("name") or row.get("id"))
                for row in active_students]
    roster_keys = list(contract.expected_student_keys(config))
    if student_ids:
        chosen_ids = set(student_ids)
        chosen_keys = set()
        for row in db.query("SELECT id, name, code FROM students WHERE class_id=? AND active=1",
                            (int(class_id),)):
            if int(row["id"]) in chosen_ids:
                chosen_keys.add(str(row.get("code") or row.get("name") or row["id"]))
        class_students_value = sum(1 for key in min_keys if key in chosen_keys)
        expected_value = (len([key for key in roster_keys if key in chosen_keys])
                          if roster_keys else None)
    else:
        class_students_value = len(min_keys)
        expected_value = len(roster_keys) if roster_keys else None

    # ---- 作答维（记录级） ----
    records = [{"student_key": _key(row), "question_no": str(row.get("question_no") or ""),
                "state": str(row.get("answer_state") or "")} for row in student_rows]
    roster = contract.roster_status(config, records)
    valid_rows = [row for row in student_rows if row.get("answer_state") == "valid"]
    unverified = [row for row in valid_rows if not row.get("verified")]

    # ---- 错题与错因（当前版本 + 已确认） ----
    mistake_rows = [row for row in dist_rows if _is_confirmed_mistake(row, checks)]
    first_bad_rows = []
    for row in dist_rows:
        check = checks.get(row.get("answer_id")) or {}
        if str(check.get("locate_status") or "") == "first_bad" and row.get("verified"):
            first_bad_rows.append(row)
    undecided_rows = [row for row in first_bad_rows
                      if str(row.get("cause_state") or "") == "undecided"]
    kp_items = _bucket([dict(row, knowledge_points=[p for p in (row.get("knowledge_points") or [])
                                                      if str(p).strip()])
                        for row in mistake_rows], "knowledge_points")
    cause_items = _bucket([dict(row, causes=[str(row.get("cause_code") or "")])
                           for row in mistake_rows], "causes")
    unclassified = sum(1 for row in mistake_rows if not (row.get("knowledge_points") or []))

    # ---- 成绩 ----
    score_rows = student_rows if (filters.get("group_scores") or False) else scope_rows
    scores_by_student = _score_rows(score_rows)
    totals = contract.student_total_scores(config, scores_by_student)
    complete_totals = [totals[sid] for sid in sorted(totals) if totals[sid] is not None]
    eligible_keys = None
    if filters.get("group_scores") and student_ids:
        wanted = set(student_ids)
        eligible_keys = [str(row.get("code") or row.get("name") or row.get("id"))
                         for row in db.query(
                             "SELECT id, name, code FROM students WHERE class_id=? AND active=1"
                             " ORDER BY id", (int(class_id),))
                         if int(row["id"]) in wanted]
    eligible = list(contract.scoring_students(config, _roster_cells(config, eligible_keys)))
    coverage = (len(complete_totals) / float(len(eligible))) if eligible else None

    # ---- 单题得分率 ----
    scored_questions = list(contract.scored_question_nos(config))
    question_rates = []
    for question in scored_questions:
        values = []
        for sid in sorted(scores_by_student):
            per_student = scores_by_student[sid]
            if str(question) in per_student:
                values.append(per_student[str(question)])
        full = contract.full_score_of(config, question)
        rate = (sum(values) / (len(values) * full)) if (values and full) else None
        question_rates.append({
            "question_no": str(question),
            "scored_count": len(values),
            "full_score": full,
            "score_sum": round(sum(values), 2) if values else 0.0,
            "rate": rate,
            "rate_text": _pct(rate) if values else EMPTY_TEXT,
            # 展示用百分比（界面直接画，不再自己换算）
            "rate_percent": round(100.0 * rate, 1) if rate is not None else 0.0,
        })

    # ---- 重复错误（两类分开计数，总数只能是两者之和） ----
    kp_pairs = contract.repeated_pairs(
        [{"student_key": _key(row), "category": str(point)}
         for row in mistake_rows for point in _dedupe(row.get("knowledge_points") or [])])
    cause_pairs = contract.repeated_pairs(
        [{"student_key": _key(row), "category": str(row.get("cause_code") or "")}
         for row in mistake_rows if str(row.get("cause_code") or "").strip()])

    metrics = {
        "class_students": _metric("class_students", class_students_value),
        "expected_students": _metric("expected_students", expected_value),
        "submitted_students": _metric("submitted_students",
                                      len(contract.submitted_student_keys(records))),
        "valid_answers": _metric("valid_answers", len(valid_rows)),
        "insufficient_answers": _metric(
            "insufficient_answers",
            sum(1 for row in student_rows if row.get("answer_state") == "insufficient")),
        "missing_answers": _metric(
            "missing_answers",
            sum(1 for row in student_rows if row.get("answer_state") == "missing")),
        "unsubmitted_answers": _metric("unsubmitted_answers", roster["unsubmitted"],
                                       denominator=roster["expected"]),
        "unverified_answers": _metric("unverified_answers", len(unverified),
                                      denominator=len(valid_rows)),
        "confirmed_mistakes": _metric("confirmed_mistakes", len(mistake_rows)),
        "knowledge_error_freq": _metric("knowledge_error_freq", kp_items),
        "affected_students": _metric(
            "affected_students",
            len({_key(row) for row in mistake_rows}), denominator=class_students_value),
        "cause_freq": _metric("cause_freq", cause_items),
        "undecided_causes": _metric("undecided_causes", len(undecided_rows),
                                    denominator=len(first_bad_rows)),
        "question_score_rate": _metric("question_score_rate", question_rates),
        "assignment_total_score": _metric(
            "assignment_total_score",
            None if not scored_questions else
            {"totals": {str(sid): totals[sid] for sid in sorted(totals)},
             "complete": len(complete_totals),
             "eligible": len(eligible),
             "card_text": "完整成绩学生 %d / 应参与评分 %d" % (len(complete_totals), len(eligible))},
            denominator=len(eligible)),
        "average_score": _metric(
            "average_score",
            round(sum(complete_totals) / len(complete_totals), 2) if complete_totals else None,
            denominator=len(complete_totals)),
        "score_coverage": _metric("score_coverage", round(coverage, 4) if coverage is not None else None,
                                  denominator=len(eligible)),
        "repeated_errors": _metric(
            "repeated_errors",
            {"knowledge_pairs": kp_pairs, "cause_pairs": cause_pairs,
             "total": len(kp_pairs) + len(cause_pairs)}),
    }
    for key in METRIC_KEYS:  # 18 项一个不少，顺序即契约顺序
        if key not in metrics:
            metrics[key] = _metric(key, None)

    ranking = {
        "class_common_issues": _class_common_issues(mistake_rows, kp_items),
        "student_priority_issues": [],
    }
    if student_id is not None:
        if str(today or "").strip():
            ranking["student_priority_issues"] = student_priority_issues(
                db, class_id, student_id, batch=batch, today=today,
                rows=mistake_rows)["items"]
        else:
            # 不读系统时钟：没有 today 就不算近期加权，并显式说明（而不是悄悄按 0 分排）
            notes.append("未提供统计基准日（today），个人重点问题暂不排序")

    heatmap = _heatmap(mistake_rows)
    detail = [_detail_row(row, checks.get(row.get("answer_id")) or {}) for row in mistake_rows]

    students_index = []
    for row in db.query("SELECT id, name, code FROM students WHERE class_id=? AND active=1"
                        " ORDER BY id", (int(class_id),)):
        key = str(row.get("code") or row.get("name") or row.get("id"))
        if student_ids and int(row["id"]) not in set(student_ids):
            continue
        students_index.append({"student_id": int(row["id"]), "student_key": key,
                               "student_name": row.get("name") or ""})

    snapshot = {
        "stats_version": STATS_VERSION,
        "contract_version": contract.R2_CONTRACT_VERSION,
        "scope": {
            "class_id": int(class_id),
            "class_name": class_row.get("name") or "",
            "batch": batch,
            "batch_name": config.get("name") or batch,
            "config_state": config.get("config_state") or "",
            "origin": config.get("origin") or "",
            "business_date": business["date"],
            "business_date_source": business["date_source"],
            "student_id": student_id,
            "filters": {key: filters.get(key) for key in _FILTER_KEYS},
            "filter_text": _filter_text({key: filters.get(key) for key in _FILTER_KEYS}, batch),
            "row_count": len(student_rows),
            "scored_questions": scored_questions,
            "full_score": contract.batch_full_score(config),
        },
        "demo": badge,
        "metrics": metrics,
        "cards": metric_cards(metrics, student_scope=student_id is not None),
        "distributions": {
            "knowledge_points": kp_items,
            "causes": cause_items,
            "unclassified": unclassified,
            "question_errors": _question_errors(mistake_rows),
            "heatmap": heatmap,
            "unsubmitted": {"count": roster["unsubmitted"],
                            "students": roster["unsubmitted_students"],
                            "expected_cells": roster["expected"],
                            "recorded_cells": roster["recorded"]},
        },
        "score": {
            "totals": {str(sid): totals[sid] for sid in sorted(totals)},
            "complete_students": sorted(int(sid) for sid in totals if totals[sid] is not None),
            "average": metrics["average_score"]["value"],
            "coverage": metrics["score_coverage"]["value"],
            "eligible_students": len(eligible),
            "question_rates": question_rates,
            "distribution": _score_distribution(config, totals),
        },
        "students": students_index,
        "mistakes": detail,
        "rankings": ranking,
        "notes": _dedupe(notes),
    }
    snapshot["metrics"]["repeated_errors"]["value"] = metrics["repeated_errors"]["value"]
    return snapshot


def _metric(key, value, denominator=None):
    spec = next((item for item in contract.R2_STAT_METRICS if item["key"] == key), {})
    text = _value_text(key, value)
    return {
        "key": key,
        "label": spec.get("label") or key,
        "value": value,
        "text": text,
        "numerator": spec.get("numerator") or "",
        "denominator": denominator,
        "definition": spec.get("definition") or "",
        "empty_rule": spec.get("empty") or "",
    }


def _value_text(key, value):
    if value is None:
        if key == "score_coverage":
            return NOT_APPLICABLE_TEXT
        return EMPTY_TEXT
    if key in ("score_coverage",):
        return _pct(value)
    if key == "average_score":
        return "%.2f" % float(value)
    if key == "assignment_total_score":
        return value.get("card_text") or EMPTY_TEXT
    if key in ("knowledge_error_freq", "cause_freq", "question_score_rate"):
        if key == "question_score_rate":
            return "；".join("%s 题 %s（已评 %d）" % (item["question_no"], item["rate_text"],
                                                    item["scored_count"])
                            for item in value) or EMPTY_TEXT
        return "；".join("%s %d" % (item["name"], item["mistakes"]) for item in value) or EMPTY_TEXT
    if key == "repeated_errors":
        return str(value.get("total") or 0)
    return _number_text(value)


def metric_cards(metrics, student_scope=False):
    """18 项 → 看板卡片（值 + 口径说明），UI 只读这里，不自己算。"""
    cards = []
    for key in METRIC_KEYS:
        entry = metrics.get(key) or {}
        cards.append({
            "key": key,
            "label": entry.get("label") or key,
            "text": entry.get("text") or EMPTY_TEXT,
            "value": entry.get("value"),
            "denominator": entry.get("denominator"),
            # 成绩类卡片在个人看板里只展示个人范围（§5.2 要求单独注明范围）
            "scope_note": ("个人范围" if student_scope and (
                key in CLASS_LEVEL_KEYS or key in DISTRIBUTION_KEYS)
                           else ("全班范围" if key in CLASS_LEVEL_KEYS else "")),
        })
    return cards


def _class_common_issues(mistake_rows, kp_items, top_n=5):
    """班级共性问题：**按涉及学生人数排序**，频次作辅助（captain 裁定）。"""
    issues = []
    for item in kp_items:
        issues.append({
            "name": item["name"],
            "student_count": item["student_count"],
            "mistakes": item["mistakes"],
            "students": item["students"],
            "example_answer_ids": item["answer_ids"][:3],
        })
    issues.sort(key=lambda item: (-item["student_count"], -item["mistakes"], item["name"]))
    return issues[:int(top_n or 5)]


def student_priority_issues(db, class_id, student_id, batch="", today="", top_n=5, rows=None):
    """个人重点问题：复用原版 `app/tutor.py` 的近期加权排序（3/2/1 分、30/90 天）。

    `today` 必须由调用方给定（模块不读系统时钟），否则同一数据两次计算不相等。
    """
    class_row = _class_row(db, class_id)
    if rows is None:
        rows_all = grading.effective_rows(db, class_row, batch)
        checks = {int(item["answer_id"]): item for item in
                  grading.current_checks(db, class_row, batch)}
        badge = _demo_badge(db, class_row, batch, rows_all)
        scope = _formal_rows(rows_all, batch=batch, demo_batch=badge["demo_batch"])
        rows = [row for row in _student_rows(scope, [int(student_id)])
                if _is_confirmed_mistake(row, checks)]
    else:
        rows = [row for row in rows if int(row.get("student_id") or 0) == int(student_id)]
    day = str(today or "").strip()
    if not day:
        raise ValueError("student_priority_issues 必须传入 today（模块不读系统时钟）")
    try:
        from datetime import date
        current = date.fromisoformat(day)
    except ValueError:
        raise ValueError("today 必须是 YYYY-MM-DD：%s" % today)
    groups = {}
    order = []
    for row in rows:
        names = _dedupe([str(point).strip() for point in (row.get("knowledge_points") or [])
                         if str(point).strip()]) or [UNCLASSIFIED]
        occurred = str(row.get("occurred_on") or "").strip()
        weight = 0
        days = None
        if occurred:
            try:
                days = (current - date.fromisoformat(occurred)).days
            except ValueError:
                days = None
        if days is not None and days >= 0:
            weight = tutor.weight_for(days)
        for name in names:
            if name not in groups:
                groups[name] = {"name": name, "count": 0, "weight": 0, "last_on": "",
                                "answer_ids": []}
                order.append(name)
            entry = groups[name]
            entry["count"] += 1
            entry["weight"] += weight
            if occurred and occurred > entry["last_on"]:
                entry["last_on"] = occurred
            if row.get("answer_id") not in entry["answer_ids"]:
                entry["answer_ids"].append(row.get("answer_id"))
    items = list(groups.values())
    for item in items:
        item["kind"] = "unclassified" if item["name"] == UNCLASSIFIED else "knowledge_point"
        item["answer_ids"] = sorted(item["answer_ids"], key=lambda value: (value is None, value))
    items.sort(key=lambda item: (item["kind"] == "unclassified", -item["weight"], -item["count"],
                                 item["name"]))
    return {"student_id": int(student_id), "today": day,
            "rule": "复用原版：≤%d 天 %d 分、≤%d 天 %d 分、其余 %d 分；未分类靠后"
                    % (tutor.RECENT_DAYS, tutor.WEIGHT_RECENT, tutor.MID_DAYS,
                       tutor.WEIGHT_MID, tutor.WEIGHT_OLD),
            "items": items[:int(top_n or 5)]}


def _heatmap(mistake_rows):
    """学生 × 知识点热力图：**以已确认错误次数为数据依据**（不是估算、不是正确率）。"""
    students = {}
    points = []
    cells = {}
    for row in mistake_rows:
        key = _key(row)
        name = str(row.get("student_name") or key)
        students.setdefault(key, name)
        for point in _dedupe([str(point).strip() for point in (row.get("knowledge_points") or [])
                              if str(point).strip()]):
            if point not in points:
                points.append(point)
            cells[(key, point)] = cells.get((key, point), 0) + 1
    points.sort()
    grid = []
    for key in sorted(students):
        grid.append({
            "student_key": key,
            "student_name": students[key],
            "cells": [{"point": point, "count": cells.get((key, point), 0)} for point in points],
        })
    return {"students": sorted(students), "student_names": {k: students[k] for k in sorted(students)},
            "points": points, "rows": grid,
            "max_count": max(list(cells.values()) or [0])}


def _question_errors(mistake_rows):
    questions = {}
    for row in mistake_rows:
        question = str(row.get("question_no") or "")
        entry = questions.setdefault(question, {"question_no": question, "mistakes": 0,
                                                "students": []})
        entry["mistakes"] += 1
        key = _key(row)
        if key not in entry["students"]:
            entry["students"].append(key)
    items = []
    for question in sorted(questions):
        entry = questions[question]
        items.append({"question_no": question, "mistakes": entry["mistakes"],
                      "student_count": len(entry["students"]),
                      "students": sorted(entry["students"])})
    return items


def _score_distribution(config, totals):
    """完整成绩分布（0–59/60–69/70–79/80–89/90–100 五档，按满分比例折算）。"""
    full = contract.batch_full_score(config)
    buckets = [{"label": "0–59%", "count": 0}, {"label": "60–69%", "count": 0},
               {"label": "70–79%", "count": 0}, {"label": "80–89%", "count": 0},
               {"label": "90–100%", "count": 0}]
    if not full:
        return {"full_score": None, "buckets": buckets, "count": 0}
    count = 0
    for sid in sorted(totals):
        total = totals[sid]
        if total is None:
            continue
        ratio = float(total) / float(full)
        index = 0
        if ratio >= 0.9:
            index = 4
        elif ratio >= 0.8:
            index = 3
        elif ratio >= 0.7:
            index = 2
        elif ratio >= 0.6:
            index = 1
        buckets[index]["count"] += 1
        count += 1
    return {"full_score": full, "buckets": buckets, "count": count}


def _detail_row(row, check):
    points = [str(point) for point in (row.get("knowledge_points") or []) if str(point).strip()]
    return {
        "answer_id": row.get("answer_id"),
        "student_id": row.get("student_id"),
        "student_key": _key(row),
        "student_name": row.get("student_name") or "",
        "question_no": str(row.get("question_no") or ""),
        "occurred_on": str(row.get("occurred_on") or ""),
        "knowledge_points": points,
        "knowledge_point_text": "、".join(points) if points else UNCLASSIFIED,
        "cause_code": str(row.get("cause_code") or ""),
        "cause_label": str(row.get("cause_label") or ""),
        "observed_step": str(check.get("observed_step") or ""),
        "first_bad_index": check.get("first_bad_index"),
        "locate_status": str(check.get("locate_status") or ""),
        "verify_source": str(check.get("verify_source") or ""),
        "teacher_note": str(check.get("teacher_note") or ""),
        "basis_note": str(check.get("basis_note") or ""),
        "teacher_error_text": str(check.get("teacher_error_text") or ""),
        "decided_at": str(check.get("decided_at") or ""),
        "score": row.get("score"),
        "score_state": str(row.get("score_state") or ""),
        "formal_score": bool(row.get("formal_score")),
        "images": int(row.get("images") or 0),
    }


# ---------------------------------------------------------------------------
# 四、明细与证据（点击错题 → 追溯原始作答 / 教师判断 / 证据）
# ---------------------------------------------------------------------------


def mistake_evidence(db, class_id, answer_id):
    """一条已确认错题的证据链（原始作答文本、图片、教师判断、版本）。只读。"""
    class_row = _class_row(db, class_id)
    answer = db.query_one(
        "SELECT * FROM answers WHERE id=? AND class_id=?", (int(answer_id), int(class_id)))
    if answer is None:
        return None
    answer = dict(answer)
    images = [dict(row) for row in db.query(
        "SELECT seq, file_path, mime, width, height FROM answer_images"
        " WHERE answer_id=? AND class_id=? ORDER BY seq", (int(answer_id), int(class_id)))]
    check = db.query_one(
        "SELECT c.* FROM answer_checks c JOIN answers a ON a.id=c.answer_id"
        " AND a.class_id=c.class_id WHERE c.answer_id=? AND c.class_id=?"
        " AND c.answer_version=a.fingerprint ORDER BY c.id DESC LIMIT 1",
        (int(answer_id), int(class_id)))
    check = dict(check) if check else {}
    is_current = contract.is_current_version(check.get("answer_version"),
                                             answer.get("fingerprint"))
    mistake = db.query_one(
        "SELECT id, status, knowledge_points, note FROM mistakes"
        " WHERE answer_id=? AND class_id=?", (int(answer_id), int(class_id)))
    mistake = dict(mistake) if mistake else {}
    try:
        points = json.loads(mistake.get("knowledge_points") or "[]")
    except (TypeError, ValueError):
        points = []
    return {
        "answer_id": int(answer_id),
        "student_id": answer.get("student_id"),
        "student_name": answer.get("student_name") or "",
        "question_no": answer.get("question_no") or "",
        "question_text": answer.get("question_text") or "",
        "standard_solution": answer.get("standard_solution") or "",
        "student_steps": answer.get("student_steps") or "",
        "occurred_on": answer.get("occurred_on") or "",
        "missing": bool(int(answer.get("missing") or 0)),
        "images": images,
        "teacher": {
            "locate_status": check.get("locate_status") or "",
            "observed_step": check.get("observed_step") or "",
            "first_bad_index": check.get("first_bad_index"),
            "cause_state": check.get("cause_state") or "",
            "cause_code": check.get("cause_code") or "",
            "cause_label": check.get("cause_label") or "",
            "teacher_error_text": check.get("teacher_error_text") or "",
            "teacher_note": check.get("teacher_note") or "",
            "basis_note": check.get("basis_note") or "",
            "verify_source": check.get("verify_source") or "",
            "decided_at": check.get("decided_at") or "",
        },
        "is_current_version": is_current,
        "mistake": {"id": mistake.get("id"), "status": mistake.get("status") or "",
                    "knowledge_points": points, "note": mistake.get("note") or ""},
    }


def student_history(db, class_id, student_id, limit=20):
    """个人历史记录：该生已确认错题按日期倒序（含知识点与错因），只读。"""
    rows = db.query(
        "SELECT m.id, m.occurred_on, m.knowledge_points, m.status, a.question_no, a.id AS answer_id"
        " FROM mistakes m LEFT JOIN answers a ON a.id=m.answer_id"
        " WHERE m.class_id=? AND m.student_id=? AND m.status='confirmed'"
        " ORDER BY m.occurred_on DESC, m.id DESC LIMIT ?",
        (int(class_id), int(student_id), int(limit)))
    out = []
    for row in rows:
        try:
            points = json.loads(row["knowledge_points"] or "[]")
        except (TypeError, ValueError):
            points = []
        out.append({"mistake_id": row["id"], "occurred_on": row["occurred_on"] or "",
                    "question_no": row["question_no"] or "", "answer_id": row["answer_id"],
                    "knowledge_points": [str(p) for p in points if str(p).strip()]})
    return out


def saved_advice(db, class_id, student_id):
    """已保存的个人辅导建议（R1 的 tutor_advice，只读；没有就返回空结构）。

    R2-04 的「已保存有效建议」段落复用它：`stale=True` 表示生成后又改过错题，
    报告里要标「待复核」，不许当最新结论用。
    """
    data = tutor.load(db, class_id, student_id) or {}
    advice = data.get("advice")
    if not advice:
        return {"exists": False, "content": "", "generated_at": "", "stale": False,
                "teacher_edited": False, "problems": []}
    return {
        "exists": True,
        "content": advice.get("content") or "",
        "generated_at": advice.get("generated_at") or "",
        "stale": bool(advice.get("stale")),
        "teacher_edited": bool(advice.get("teacher_edited")),
        "problems": data.get("summary", {}).get("items") or [],
    }


__all__ = [
    "STATS_VERSION", "SYNTHETIC_ALLOWED_FUNCS", "METRIC_KEYS", "RANKING_KEYS",
    "CLASS_LEVEL_KEYS", "DISTRIBUTION_KEYS", "UNCLASSIFIED",
    "list_batches", "class_snapshot", "student_snapshot", "metric_cards",
    "student_priority_issues", "mistake_evidence", "student_history", "saved_advice",
]
