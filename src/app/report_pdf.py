# -*- coding: utf-8 -*-
"""R2-04 学情分析 PDF：班级报告与个人报告（预览与导出共用同一次数据快照）。

三条硬规则（派单 §6.2，captain 明确要求）：

1. **快照是唯一输入**：`build_class_report()` / `build_student_report()` 只读一次库，
   把 `r2_stats.class_snapshot/student_snapshot` 的结果、证据链、已保存建议全部装进
   `report` 字典；`render_pdf(report)` / `preview_lines(report)` 只吃这个字典，**不再查库**。
   所以「预览之后改了数据再导出」得到的 PDF 仍然对应原预览快照。
2. **排版只用 `pdf_canvas`**（tutor 在 R2-05b 抽出的纯标准库 A4 画布）：中文 TrueType 子集 +
   /ToUnicode、自动分页、长表格跨页重画表头、页脚「第 n / 共 m 页」、条形/柱状图带数值标签。
   本模块不写第二套 PDF 底层。
3. **空态不拿 0 冒充**：缺分母显示「暂无 / 无法计算」，报告里用 `empty_state()` 或原文。

边界：不改 `r2_stats.py` 的既有接口签名；不改 `ui.py/api.py/service.py`；演示数据由
`r2_stats` 的入口单点排除，报告只消费快照，不另判来源。
"""

import hashlib
import json
import os
from datetime import datetime

from . import pdf_canvas
from . import r2_stats

REPORT_VERSION = "R2-04.1"
DATA_VERSION = "统计 %s / 契约 %s" % (r2_stats.STATS_VERSION, "R2-00.4")
PURPOSES = ("存档", "教学研究", "家长沟通", "其他")
MAX_REPRESENTATIVE = 6          # 代表性错题总数上限
PER_KNOWLEDGE_LIMIT = 2         # 每类（知识点）最多几条
HISTORY_FILE = "r2_report_exports.json"
HISTORY_LIMIT = 50


class ReportError(Exception):
    """报告生成/导出错误（带稳定 code，界面按 code 出中文提示）。"""

    def __init__(self, message, code="report_error", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# 一、快照采集（唯一读库的地方）
# ---------------------------------------------------------------------------


def _class_row(db, class_id):
    row = db.query_one("SELECT * FROM classes WHERE id=?", (int(class_id),))
    if row is None:
        raise ReportError("班级不存在：%s" % class_id, "class_not_found", 404)
    return dict(row)


def _require_access(db, class_row, user_id):
    """复用账号与班级归属权限：非本账号的班级一律拒绝（越权 404，不泄露存在性）。"""
    if user_id is None:
        return True
    if int(class_row.get("user_id") or 0) != int(user_id):
        raise ReportError("这个班级不属于当前账号，不能生成报告。", "not_owner", 404)
    return True


def _require_student(db, class_row, student_id):
    row = db.query_one(
        "SELECT * FROM students WHERE id=? AND class_id=?",
        (int(student_id), int(class_row["id"])))
    if row is None:
        raise ReportError("这名学生不在当前班级里。", "student_not_found", 404)
    return dict(row)


def _class_advice(db, class_id):
    """已保存的班级教学建议（R1 的 advice 表，scope='class'）。只读。"""
    row = db.query_one(
        "SELECT * FROM advice WHERE class_id=? AND scope='class' AND student_id IS NULL",
        (int(class_id),))
    if row is None:
        return {"exists": False, "content": "", "generated_at": "", "teacher_edited": False}
    return {"exists": True, "content": row["content"] or "",
            "generated_at": row["generated_at"] or row["updated_at"] or "",
            "teacher_edited": bool(row["teacher_edited"])}


def representative_mistakes(snapshot, limit=MAX_REPRESENTATIVE,
                            per_knowledge=PER_KNOWLEDGE_LIMIT):
    """代表性错题：默认高频（知识点频次 → 涉及学生数），每类≤2 条、总数≤6 条。

    只用快照里的明细（`snapshot["mistakes"]`），不查库；同一知识点超过上限的条数丢弃，
    多出来的名额让给下一类，保证「每类≤2、总数≤6」同时成立。
    """
    detail = list(snapshot.get("mistakes") or [])
    order = {item["name"]: index for index, item in
             enumerate(snapshot["distributions"]["knowledge_points"])}
    detail.sort(key=lambda row: (
        order.get((row.get("knowledge_points") or [r2_stats.UNCLASSIFIED])[0],
                  len(order)),
        -int(row.get("images") or 0),
        row.get("answer_id") or 0,
    ))
    picked = []
    per_category = {}
    for row in detail:
        points = row.get("knowledge_points") or []
        category = points[0] if points else r2_stats.UNCLASSIFIED
        if per_category.get(category, 0) >= int(per_knowledge):
            continue
        per_category[category] = per_category.get(category, 0) + 1
        picked.append(dict(row, _category=category))
        if len(picked) >= int(limit):
            break
    return picked


def _base_report(kind, snapshot, generated_at, user, purpose):
    if not str(generated_at or "").strip():
        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if purpose not in PURPOSES:
        purpose = PURPOSES[0]
    scope = snapshot["scope"]
    title = ("%s · 学情分析报告" % (scope.get("class_name") or "班级")) if kind == "class" \
        else ("%s · 个人学情报告" % (scope.get("student_name") or "学生"))
    return {
        "report_version": REPORT_VERSION,
        "kind": kind,
        "title": title,
        "generated_at": generated_at,
        "data_version": DATA_VERSION,
        "purpose": purpose,
        "user": {"id": user.get("id"), "username": user.get("username") or ""} if user else None,
        "snapshot": snapshot,
        "scope": scope,
        "demo": snapshot.get("demo") or {},
        "sections": [],
        "planned_practice": [],       # R2-05 教学任务数据在 R2-06 接入，这里先留接口
        "planned_practice_note": "暂无已安排的针对性练习（R2-05 教学任务接入后显示）",
        "class_advice": {"exists": False, "content": "", "generated_at": ""},
        "student_advice": {"exists": False, "content": "", "generated_at": "", "stale": False},
    }


def build_class_report(db, class_id, batch="", filters=None, today="", generated_at="",
                       user=None, purpose="存档", limit=MAX_REPRESENTATIVE):
    """班级报告：一次读库 → 快照；之后预览与导出的内容完全由快照决定。"""
    class_row = _class_row(db, class_id)
    _require_access(db, class_row, (user or {}).get("id"))
    snapshot = r2_stats.class_snapshot(db, class_id, batch=batch, filters=filters,
                                       today=today)
    report = _base_report("class", snapshot, generated_at, user, purpose)
    report["class_advice"] = _class_advice(db, class_id)
    report["representative"] = representative_mistakes(snapshot, limit=limit)
    report["sections"] = _class_sections(report)
    return report


def build_student_report(db, class_id, student_id, batch="", filters=None, today="",
                         generated_at="", user=None, purpose="存档",
                         limit=MAX_REPRESENTATIVE):
    """个人报告：快照 + 证据链 + 已保存建议一并装进 report（导出时不再碰库）。"""
    class_row = _class_row(db, class_id)
    _require_access(db, class_row, (user or {}).get("id"))
    student = _require_student(db, class_row, student_id)
    snapshot = r2_stats.student_snapshot(db, class_id, student_id, batch=batch,
                                         filters=filters, today=today)
    report = _base_report("student", snapshot, generated_at, user, purpose)
    report["student"] = {"id": int(student_id), "name": student.get("name") or "",
                         "code": student.get("code") or ""}
    report["student_advice"] = r2_stats.saved_advice(db, class_id, student_id)
    report["representative"] = representative_mistakes(snapshot, limit=limit)
    evidence = []
    for row in report["representative"]:
        item = r2_stats.mistake_evidence(db, class_id, row["answer_id"])
        if item:
            evidence.append(item)
    report["evidence"] = evidence
    report["history"] = r2_stats.student_history(db, class_id, student_id)
    report["sections"] = _student_sections(report)
    return report


# ---------------------------------------------------------------------------
# 二、报告结构（预览与 PDF 都从 sections 出，保证「数值与预览一致」）
# ---------------------------------------------------------------------------


def _metric_text(snapshot, key):
    return (snapshot["metrics"].get(key) or {}).get("text") or r2_stats.EMPTY_TEXT


def _scope_rows(report):
    scope = report["scope"]
    rows = [("班级", scope.get("class_name") or ""),
            ("批次", scope.get("batch_name") or "全部批次"),
            ("业务日期", scope.get("business_date") or "无业务日期"),
            ("统计范围", scope.get("filter_text") or "全班"),
            ("记录数", str(scope.get("row_count") or 0)),
            ("生成时间", report["generated_at"]),
            ("数据版本", report["data_version"])]
    if scope.get("full_score") is not None:
        rows.append(("满分合计（非得分）", "%g" % scope["full_score"]))
    if report["demo"].get("demo_batch"):
        rows.append(("数据来源", "合成演示（不计入正式统计）"))
    return rows


def _overview_rows(snapshot):
    return [("有效作答数", _metric_text(snapshot, "valid_answers")),
            ("内容不足数", _metric_text(snapshot, "insufficient_answers")),
            ("缺测数", _metric_text(snapshot, "missing_answers")),
            ("应作答人数", _metric_text(snapshot, "expected_students")),
            ("作答人数", _metric_text(snapshot, "submitted_students")),
            ("未提交数", _metric_text(snapshot, "unsubmitted_answers")),
            ("未核验数", _metric_text(snapshot, "unverified_answers")),
            ("已确认错题数", _metric_text(snapshot, "confirmed_mistakes"))]


def _score_rows(snapshot):
    scope = snapshot["scope"]
    rows = [("平均分（仅完整成绩）", _metric_text(snapshot, "average_score")),
            ("评分覆盖率", _metric_text(snapshot, "score_coverage")),
            ("完整成绩学生", str(len(snapshot["score"]["complete_students"]))),
            ("应参与评分学生", str(snapshot["score"]["eligible_students"]))]
    if scope.get("full_score") is not None:
        rows.append(("满分合计（非得分）", "%g" % scope["full_score"]))
    return rows


def _rate_table(snapshot):
    return {
        "columns": [{"title": "题号", "key": "no", "width": 60},
                    {"title": "已评份数", "key": "count", "width": 76, "align": "right"},
                    {"title": "满分", "key": "full", "width": 62, "align": "right"},
                    {"title": "得分率", "key": "rate", "align": "right"}],
        "rows": [{"no": "%s 题" % item["question_no"],
                  "count": str(item["scored_count"]),
                  "full": "" if item["full_score"] is None else "%g" % item["full_score"],
                  "rate": item["rate_text"]}
                 for item in snapshot["score"]["question_rates"]],
    }


def _kp_table(snapshot):
    return {
        "columns": [{"title": "知识点", "key": "name"},
                    {"title": "错题数", "key": "mistakes", "width": 80, "align": "right"},
                    {"title": "涉及学生", "key": "students", "width": 88, "align": "right"}],
        "rows": [{"name": item["name"], "mistakes": str(item["mistakes"]),
                  "students": str(item["student_count"])}
                 for item in snapshot["distributions"]["knowledge_points"]],
    }


def _cause_table(snapshot):
    return {
        "columns": [{"title": "错因", "key": "name"},
                    {"title": "错题数", "key": "mistakes", "width": 80, "align": "right"},
                    {"title": "涉及学生", "key": "students", "width": 88, "align": "right"}],
        "rows": [{"name": item["name"], "mistakes": str(item["mistakes"]),
                  "students": str(item["student_count"])}
                 for item in snapshot["distributions"]["causes"]],
    }


def _distribution_table(snapshot):
    distribution = snapshot["score"]["distribution"]
    return {
        "columns": [{"title": "总分区间", "key": "label"},
                    {"title": "学生数", "key": "count", "width": 96, "align": "right"}],
        "rows": [{"label": item["label"], "count": str(item["count"])}
                 for item in distribution["buckets"]],
    }


def _representative_table(report, anonymous=False):
    """代表性错题表。`anonymous=True`（班级报告）**不列学生列**——班级报告默认不出现
    可直接识别学生的信息，这是派单 §6.2 的硬要求，不是排版取舍。"""
    rows = []
    columns = [{"title": "题号", "key": "no", "width": 56},
               {"title": "知识点", "key": "point", "width": 110},
               {"title": "错因", "key": "cause", "width": 92}]
    if anonymous:
        columns.append({"title": "错步原文", "key": "step"})
    else:
        columns.append({"title": "错步原文", "key": "step", "width": 140})
        columns.append({"title": "学生", "key": "slot", "width": 80})
    for row in report["representative"]:
        item = {
            "no": "%s 题" % row.get("question_no"),
            "point": row.get("knowledge_point_text") or r2_stats.UNCLASSIFIED,
            "cause": row.get("cause_label") or row.get("cause_code") or "未确认",
            "step": (row.get("observed_step") or "无错步原文")[:20],
        }
        if not anonymous:
            item["slot"] = row.get("student_name") or row.get("student_key") or ""
        rows.append(item)
    return {"columns": columns, "rows": rows}


def _class_sections(report):
    snapshot = report["snapshot"]
    sections = [
        {"kind": "kv", "title": "1. 班级与统计范围", "items": _scope_rows(report)},
        {"kind": "kv", "title": "2. 作答及审核概况", "items": _overview_rows(snapshot)},
        {"kind": "kv", "title": "3. 有效成绩统计", "items": _score_rows(snapshot)},
        {"kind": "table", "title": "3.1 各题得分率（同时给出已评份数）", **_rate_table(snapshot)},
        {"kind": "chart", "title": "3.2 完整成绩分布",
         "items": [(item["label"], item["count"])
                   for item in snapshot["score"]["distribution"]["buckets"]],
         "note": "按完整成绩学生的总分区间统计；不完整成绩不进平均分"},
        {"kind": "chart", "title": "4. 知识点错误分布",
         "items": [(item["name"], item["mistakes"])
                   for item in snapshot["distributions"]["knowledge_points"]],
         "note": "多知识点按类别分别计数，同一错题同一知识点只计一次"},
        {"kind": "table", "title": "4.1 知识点明细", **_kp_table(snapshot)},
        {"kind": "chart", "title": "5. 错因分布",
         "items": [(item["name"], item["mistakes"])
                   for item in snapshot["distributions"]["causes"]],
         "note": "只统计教师已确认的错因；撤回的错因不计入"},
        {"kind": "table", "title": "5.1 错因明细", **_cause_table(snapshot)},
        {"kind": "issues", "title": "6. 班级共性问题",
         "items": snapshot["rankings"]["class_common_issues"],
         "note": "按涉及学生人数排序，频次作为辅助"},
        {"kind": "table", "title": "7. 代表性错题（默认高频、每类≤%d 条、总数≤%d 条）"
                                  % (PER_KNOWLEDGE_LIMIT, MAX_REPRESENTATIVE),
         **_representative_table(report, anonymous=True),
         "note": "班级报告默认不出现可直接识别学生的信息"},
        {"kind": "advice", "title": "8. 已保存教学建议", "advice": report["class_advice"]},
    ]
    if snapshot["distributions"]["unclassified"]:
        sections.insert(7, {"kind": "text", "title": "4.2 未分类错题",
                            "paragraphs": ["有 %d 条已确认错题尚未补知识点分类，"
                                           "这些错题仍计入总数，但不出现在知识点分布里。"
                                           % snapshot["distributions"]["unclassified"]]})
    for note in snapshot.get("notes") or []:
        sections.append({"kind": "text", "title": "口径说明", "paragraphs": [note]})
    return sections


def _student_sections(report):
    snapshot = report["snapshot"]
    student = report.get("student") or {}
    scope = report["scope"]
    sections = [
        {"kind": "kv", "title": "1. 学生与统计范围",
         "items": [("班级", scope.get("class_name") or ""),
                   ("学生", "%s（%s）" % (student.get("name") or "", student.get("code") or "")),
                   ("批次", scope.get("batch_name") or "全部批次"),
                   ("统计范围", scope.get("filter_text") or "个人"),
                   ("生成时间", report["generated_at"]),
                   ("数据版本", report["data_version"])]},
        {"kind": "kv", "title": "2. 个人作答及审核概况", "items": _overview_rows(snapshot)},
        {"kind": "kv", "title": "3. 有效个人成绩", "items": _score_rows(snapshot)},
        {"kind": "table", "title": "3.1 各题得分率", **_rate_table(snapshot)},
        {"kind": "chart", "title": "4. 知识点错误分布",
         "items": [(item["name"], item["mistakes"])
                   for item in snapshot["distributions"]["knowledge_points"]],
         "note": "以已确认错题为依据，不推算正确率或掌握率"},
        {"kind": "table", "title": "4.1 知识点明细", **_kp_table(snapshot)},
        {"kind": "chart", "title": "5. 主要错因",
         "items": [(item["name"], item["mistakes"])
                   for item in snapshot["distributions"]["causes"]]},
        {"kind": "issues", "title": "6. 重点辅导问题",
         "items": snapshot["rankings"].get("student_priority_issues") or [],
         "note": "复用原版近期加权排序（30 天内 3 分、90 天内 2 分、其余 1 分）"},
        {"kind": "table", "title": "7. 对应错题及证据", **_representative_table(report)},
        {"kind": "evidence", "title": "7.1 证据链（原始作答 / 教师判断）",
         "items": report.get("evidence") or []},
        {"kind": "advice", "title": "8. 已保存的个人辅导建议",
         "advice": report.get("student_advice") or {}},
        {"kind": "practice", "title": "9. 已安排的针对性练习",
         "items": report.get("planned_practice") or [],
         "note": report.get("planned_practice_note") or ""},
        {"kind": "table", "title": "附录：历史记录（已确认错题，按日期倒序）",
         "columns": [{"title": "日期", "key": "date", "width": 96},
                     {"title": "题号", "key": "no", "width": 60},
                     {"title": "知识点", "key": "points"}],
         "rows": [{"date": item["occurred_on"] or "无日期",
                   "no": item["question_no"] or "",
                   "points": "、".join(item["knowledge_points"]) or r2_stats.UNCLASSIFIED}
                  for item in report.get("history") or []]},
    ]
    return sections


# ---------------------------------------------------------------------------
# 三、预览（与 PDF 同源：都只读 sections / report）
# ---------------------------------------------------------------------------


def preview_lines(report):
    """预览文本：与 PDF 的段落顺序、数值完全一致（验收会逐条比对）。"""
    lines = [report["title"], report["generated_at"], report["data_version"]]
    for section in report["sections"]:
        lines.append("【%s】" % section["title"])
        kind = section["kind"]
        if kind == "kv":
            lines.extend("%s：%s" % (label, value) for label, value in section["items"])
        elif kind == "table":
            lines.append(" | ".join(column["title"] for column in section["columns"]))
            for row in section["rows"]:
                lines.append(" | ".join(str(row.get(column["key"], ""))
                                        for column in section["columns"]))
            if not section["rows"]:
                lines.append("暂无数据")
        elif kind == "chart":
            if section["items"]:
                lines.extend("%s：%g" % (label, float(value))
                             for label, value in section["items"])
            else:
                lines.append("暂无数据")
        elif kind in ("issues", "practice"):
            if section["items"]:
                for index, item in enumerate(section["items"], 1):
                    lines.append("%d. %s（加权 %s 分 / 涉及 %s 名学生）" % (
                        index, item.get("name"), item.get("weight", item.get("student_count")),
                        item.get("student_count", item.get("student_count"))))
            else:
                lines.append("暂无" if kind == "issues" else section.get("note") or "暂无")
        elif kind == "evidence":
            if section["items"]:
                for item in section["items"]:
                    teacher = item.get("teacher") or {}
                    lines.append("作答 #%s · 第 %s 题 · 错因 %s · 判定 %s" % (
                        item.get("answer_id"), item.get("question_no"),
                        teacher.get("cause_label") or "未确认",
                        teacher.get("locate_status") or "无"))
                    lines.append("学生步骤：%s" % (item.get("student_steps") or "").replace(
                        "\n", " / "))
                    lines.append("错步原文：%s" % (teacher.get("observed_step") or "无"))
            else:
                lines.append("暂无")
        elif kind == "advice":
            advice = section.get("advice") or {}
            if advice.get("exists"):
                lines.append(advice.get("content") or "")
                if advice.get("stale"):
                    lines.append("（待复核：建议生成后错题有变化）")
            else:
                lines.append("暂无已保存的建议")
        else:  # text
            lines.extend(section["paragraphs"])
        if section.get("note"):
            lines.append("说明：%s" % section["note"])
    return lines


# ---------------------------------------------------------------------------
# 四、导出（只吃 report，不查库）
# ---------------------------------------------------------------------------


def render_pdf(report):
    """把 report 排成 A4 纵向 PDF 字节。纯函数：同一 report 两次输出逐字节相等。"""
    doc = pdf_canvas.PdfCanvas(header_text=report["title"])
    doc.new_page()
    doc.heading(report["title"], level=1)
    doc.text("%s　｜　%s　｜　用途：%s" % (report["generated_at"], report["data_version"],
                                         report.get("purpose") or "存档"),
             size=pdf_canvas.SMALL_SIZE, color=(0.42, 0.44, 0.48))
    doc.rule()
    for section in report["sections"]:
        doc.heading(section["title"], level=3, rule=True)
        kind = section["kind"]
        if kind == "kv":
            for label, value in section["items"]:
                doc.label_value("%s：" % label, value)
        elif kind == "table":
            if section["rows"]:
                doc.table(section["rows"], section["columns"])
            else:
                doc.empty_state("暂无数据")
        elif kind == "chart":
            if section["items"]:
                doc.bar_chart([{"label": label, "value": value}
                               for label, value in section["items"]])
            else:
                doc.empty_state("暂无数据")
        elif kind == "issues":
            if section["items"]:
                rows = [{"name": item["name"],
                         "students": str(item.get("student_count") or 0),
                         "mistakes": str(item.get("mistakes") or 0),
                         "weight": str(item.get("weight") or "")}
                        for item in section["items"]]
                columns = [{"title": "知识点/问题", "key": "name"},
                           {"title": "涉及学生", "key": "students", "width": 80,
                            "align": "right"},
                           {"title": "错题数", "key": "mistakes", "width": 76,
                            "align": "right"},
                           {"title": "加权分", "key": "weight", "width": 76, "align": "right"}]
                doc.table(rows, columns)
            else:
                doc.empty_state("暂无")
        elif kind == "evidence":
            if section["items"]:
                for item in section["items"]:
                    teacher = item.get("teacher") or {}
                    doc.text("作答 #%s · 第 %s 题 · 错因：%s · 判定：%s" % (
                        item.get("answer_id"), item.get("question_no"),
                        teacher.get("cause_label") or "未确认",
                        teacher.get("locate_status") or "无"), bold=True)
                    doc.text("题目：%s" % (item.get("question_text") or ""))
                    doc.text("标准解法：%s" % (item.get("standard_solution") or ""))
                    doc.text("学生步骤：%s" % (item.get("student_steps") or ""))
                    doc.text("教师判断：%s（%s）" % (teacher.get("teacher_error_text") or "无",
                                                teacher.get("basis_note") or "无依据说明"))
                    doc.text("错步原文：%s" % (teacher.get("observed_step") or "无"))
                    doc.spacer(4)
            else:
                doc.empty_state("暂无")
        elif kind == "advice":
            advice = section.get("advice") or {}
            if advice.get("exists"):
                doc.text(advice.get("content") or "")
                if advice.get("stale"):
                    doc.text("（待复核：建议生成后错题有变化）", color=(0.66, 0.38, 0.05))
            else:
                doc.empty_state("暂无已保存的建议")
        elif kind == "practice":
            if section["items"]:
                doc.table(section["items"], [{"title": "练习", "key": "title"},
                                             {"title": "来源", "key": "source", "width": 120}])
            else:
                doc.empty_state(section.get("note") or "暂无")
        else:
            for paragraph in section["paragraphs"]:
                doc.text(paragraph)
        if section.get("note"):
            doc.text("说明：%s" % section["note"], size=pdf_canvas.SMALL_SIZE,
                     color=(0.42, 0.44, 0.48))
    return doc.to_bytes(report["title"], report.get("kind") or "", "数智伴学 1.9.0",
                        "R2-04 report_pdf")


def export_pdf(report, path, db_file=None, purpose=None):
    """导出到文件并（可选）记一条导出历史。签名只吃 report，不碰数据库。"""
    chosen = purpose or report.get("purpose") or PURPOSES[0]
    report = dict(report, purpose=chosen if chosen in PURPOSES else PURPOSES[0])
    data = render_pdf(report)
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    entry = {
        "at": report["generated_at"],
        "kind": report["kind"],
        "title": report["title"],
        "purpose": report.get("purpose") or PURPOSES[0],
        "filters": report["scope"].get("filter_text") or "",
        "batch": report["scope"].get("batch") or "",
        "path": os.path.abspath(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "user": (report.get("user") or {}).get("username") or "",
        "data_version": report["data_version"],
    }
    if db_file:
        entry["user_id"] = (report.get("user") or {}).get("id")
        save_history(db_file, entry)
    return {"path": os.path.abspath(path), "bytes": len(data), "entry": entry}


def history_path(db_file):
    return os.path.join(os.path.dirname(os.path.abspath(db_file)), HISTORY_FILE)


def load_history(db_file):
    path = history_path(db_file)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_history(db_file, entry):
    """追加一条导出历史（原子写：临时文件 + os.replace，坏文件不会写半个）。"""
    items = load_history(db_file)
    items.insert(0, entry)
    items = items[:HISTORY_LIMIT]
    path = history_path(db_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(items, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp, path)
    return items


__all__ = [
    "REPORT_VERSION", "DATA_VERSION", "PURPOSES", "MAX_REPRESENTATIVE",
    "PER_KNOWLEDGE_LIMIT", "ReportError",
    "build_class_report", "build_student_report", "representative_mistakes",
    "preview_lines", "render_pdf", "export_pdf",
    "history_path", "load_history", "save_history",
]
