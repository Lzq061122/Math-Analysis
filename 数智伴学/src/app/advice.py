"""教学建议：基于「已确认」错题生成参考意见，并保存老师修改后的版本。

规则要点：
- 建议是参考意见，不替老师布置作业、不替老师做教学决定。
- 每条建议都带 evidence（对应错题编号与当时的统计），老师可查看依据。
- 依据不足（已确认错题 < 3 条）时只提示，不生成具体建议内容。
"""

import json

from .analysis import DETAIL_TEMPLATES
from .db import now_iso
from .stats import MIN_SAMPLE, build_stats

HEADER_CLASS = "【参考意见·班级】"
HEADER_STUDENT = "【参考意见·个人】"
GENERIC_HINT = "建议针对该知识点安排一次小练，题目只改一个条件，练完让学生口述关键步骤，再抽查 3 份作业看是否迁移。"


def _hint(point):
    template = DETAIL_TEMPLATES.get(point) or {}
    return template.get("advice") or GENERIC_HINT


def _fmt_range(stats):
    f = stats["filters"]
    if f.get("from") or f.get("to"):
        return "（时间范围 %s ~ %s）" % (f.get("from") or "不限", f.get("to") or "不限")
    return ""


def _class_content(stats):
    if stats["summary"]["confirmed"] == 0:
        return "该班级还没有已确认的错题，暂无依据生成教学建议。请先完成错题核对并确认。"
    if stats["summary"]["confirmed"] < MIN_SAMPLE:
        return (
            "已确认错题仅 %d 条，样本过少，暂不生成具体教学建议。建议继续收集并核对错题后再查看。"
            % stats["summary"]["confirmed"]
        )
    lines = [
        HEADER_CLASS
        + "依据已确认的 %d 条错题（涉及 %d 名学生）%s。"
        % (stats["summary"]["confirmed"], stats["summary"]["students"], _fmt_range(stats)),
    ]
    for index, item in enumerate(stats["knowledge_points"][:3], start=1):
        lines.append(
            "%d. 「%s」：%d 条错题、%d 名学生。%s"
            % (index, item["name"], item["mistakes"], item["student_count"], _hint(item["name"]))
        )
    if stats["error_types"]:
        top = stats["error_types"][0]
        lines.append(
            "%d. 错误类型以「%s」为主（%d 条）。可在讲评时统一示范这一类错误的检查方法，"
            "并要求学生在订正后自查一遍同类问题。" % (len(stats["knowledge_points"][:3]) + 1, top["name"], top["mistakes"])
        )
    if stats["notes"]:
        lines.append("说明：" + "；".join(stats["notes"]))
    lines.append("以上为参考意见，不替代老师的教学判断，也不会自动布置作业。")
    return "\n".join(lines)


def _student_content(stats):
    student = stats["student"]
    if stats["summary"]["confirmed"] == 0:
        return "%s 还没有已确认的错题，暂无依据生成学习建议。" % (student["name"] if student else "该学生")
    if stats["summary"]["confirmed"] < 2:
        return (
            "%s 目前只有 %d 条已确认错题，单次错误不足以判断掌握情况，暂不生成具体建议。"
            % (student["name"], stats["summary"]["confirmed"])
        )
    lines = [HEADER_STUDENT + "依据 %s 的 %d 条已确认错题 %s。" % (student["name"], stats["summary"]["confirmed"], _fmt_range(stats))]
    for index, item in enumerate(stats["knowledge_points"][:3], start=1):
        lines.append(
            "%d. 「%s」出现 %d 次。%s" % (index, item["name"], item["mistakes"], _hint(item["name"]))
        )
    if stats["error_types"]:
        top = stats["error_types"][0]
        lines.append("常见错误类型为「%s」（%d 条），可提醒其做题时按固定顺序自查。" % (top["name"], top["mistakes"]))
    lines.append("以上为参考意见，单次或少数几次错误不能判定学生是否粗心或未掌握。")
    return "\n".join(lines)


def _evidence(stats):
    return {
        "mistake_ids": stats["mistake_ids"],
        "generated_at": now_iso(),
        "summary": stats["summary"],
        "knowledge_points": stats["knowledge_points"][:5],
        "error_types": stats["error_types"][:5],
        "filters": stats["filters"],
    }


def refresh(db, class_row, students):
    """重新计算并保存班级与各学生的建议；老师改过的文本保留。"""
    class_stats = build_stats(db, class_row)
    with db.tx() as conn:
        rows = [
            (None, "class", _class_content(class_stats), _evidence(class_stats)),
        ]
        for student in students:
            st = build_stats(db, class_row, student)
            rows.append((student["id"], "student", _student_content(st), _evidence(st)))

        for student_id, scope, content, evidence in rows:
            existing = conn.execute(
                "SELECT id, teacher_edited FROM advice WHERE class_id=? AND scope=? AND student_id IS ?",
                (class_row["id"], scope, student_id),
            ).fetchone()
            payload = json.dumps(evidence, ensure_ascii=False)
            if existing:
                if existing["teacher_edited"]:
                    conn.execute(
                        "UPDATE advice SET evidence=?, generated_at=? WHERE id=?",
                        (payload, now_iso(), existing["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE advice SET content=?, evidence=?, generated_at=?, updated_at=? WHERE id=?",
                        (content, payload, now_iso(), now_iso(), existing["id"]),
                    )
            else:
                conn.execute(
                    "INSERT INTO advice(class_id, student_id, scope, content, evidence, generated_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (class_row["id"], student_id, scope, content, payload, now_iso(), now_iso()),
                )
    return True
