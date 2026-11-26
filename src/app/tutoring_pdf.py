"""R2-05 纸笔任务 PDF：学生版（题目 + 作答空间）与教师版（题目 + 答案 + 解析 + 教学提示）。

PDF 底层已抽到 `app.pdf_canvas`（A4 纵向、游标排版、自动分页、中文 TrueType 子集 +
`/ToUnicode`、长表格、条形/柱状图）。本模块只负责**纸笔任务这一种版式**：
组版数据（按任务记录的版本取资源）→ 按顺序往下写 → 输出字节。

对外契约（R2-05 验收与界面都依赖它，改造后一字不变）：
- `render_student(payload)` → PDF 字节：只有题目与作答空间，**绝不**出现答案、解析、教学提示；
- `render_teacher(payload)` → PDF 字节：题目 + 答案 + 解析 + 三级提示；
- `paper_payload(db, task_id)` → 组版数据（题目/答案/解析/提示都按**任务记录的版本**取）；
- `extract_text(pdf_bytes)` / `student_text(pdf_bytes)` → 回读（验收与界面自检都用它）。
"""

import os

from . import pdf_canvas as PC
from .pdf_canvas import (                                  # 兼容旧引用（tutoring_pdf.X）
    PAGE_WIDTH,
    PAGE_HEIGHT,
    MARGIN_LEFT,
    MARGIN_RIGHT,
    MARGIN_TOP,
    MARGIN_BOTTOM,
    TrueTypeFont,
    extract_text,
)

BODY_SIZE = PC.BODY_SIZE
TITLE_SIZE = PC.TITLE_SIZE
SMALL_SIZE = PC.SMALL_SIZE
LINE_GAP = PC.LINE_GAP
ANSWER_LINE_GAP = PC.ANSWER_LINE_GAP

FONT_CANDIDATES = PC.FONT_CANDIDATES
_font_path = PC.font_path

STUDENT_FORBIDDEN_LABELS = ("答案", "解析", "教学提示")
ANSWER_SPACE_MARK = "作答区"


def _new_canvas():
    """纸笔任务共用同一套页面参数；页脚用画布默认的「第 n / 共 m 页」。"""
    return PC.PdfCanvas(footer_note="数智伴学 · 纸笔任务")


# ---------------------------------------------------------------------------
# 组版数据
# ---------------------------------------------------------------------------
def paper_payload(db, task_id):
    """把一条教学任务整理成 PDF 需要的全部文字，**按任务记录的版本**取资源正文。"""
    from . import teaching_resources as T

    task = T.get_task(db, task_id)
    if task is None:
        raise T.TeachingResourceError("教学任务 %s 不存在，无法生成纸笔任务。" % task_id,
                                      "no_task")
    student = db.query_one("SELECT * FROM students WHERE id=?", (task["student_id"],)) \
        if task["student_id"] else None
    class_row = db.query_one("SELECT * FROM classes WHERE id=?", (task["class_id"],))
    exercises = []
    for ref in task["exercise_refs"]:
        key = ref.get("resource_key", "")
        version = ref.get("version")
        view = T.get_resource(db, key, version) if key else None
        body = (view or {}).get("body", {})
        exercises.append({
            "resource_key": key,
            "version": version,
            "title": (view or {}).get("title") or ref.get("title", ""),
            "stem": body.get("stem", ""),
            "answer": body.get("answer", ""),
            "solution": body.get("solution", ""),
            "answer_lines": body.get("answer_lines", 3),
            "difficulty": body.get("difficulty", ""),
            "purpose": body.get("purpose", ""),
            "purpose_label": T.EXERCISE_PURPOSES.get(body.get("purpose", ""), ""),
            "review_state": task["review_state"],
        })
    hints = []
    for level in (task["hint_plan"] or {}).get("levels", []):
        key = level.get("resource_key", "")
        view = T.get_resource(db, key, level.get("version")) if key else None
        text = (view or {}).get("body", {}).get("text") or level.get("text", "")
        hints.append({"level": level.get("level", ""), "text": text,
                      "resource_key": key, "version": level.get("version")})
    basis = task["basis"] or {}
    return {
        "task_id": task["id"],
        "title": task["title"],
        "kind": task["kind"],
        "kind_label": task["kind_label"],
        "status": task["status"],
        "status_label": task["status_label"],
        "review_state": task["review_state"],
        "review_state_label": task["review_state_label"],
        "teacher_note": task["teacher_note"],
        "class_name": (class_row or {}).get("name", ""),
        "student_name": (student or {}).get("name", "") if student else "",
        "student_code": (student or {}).get("code", "") if student else "",
        "knowledge_point": basis.get("knowledge_point", ""),
        "unit": basis.get("unit", ""),
        "cause_label": basis.get("cause_label", ""),
        "created_at": task["created_at"],
        "exercises": exercises,
        "hints": hints,
        "resource_versions": task["resource_versions"],
        "basis": basis,
    }


# ---------------------------------------------------------------------------
# 版式
# ---------------------------------------------------------------------------
def _header(canvas, payload, variant_label):
    canvas.heading(payload.get("title") or "纸笔辅导任务", size=TITLE_SIZE)
    line = "班级：%s　　学生：%s　　任务编号：%d　　%s" % (
        payload.get("class_name") or "（未记录）",
        payload.get("student_name") or ("全班" if payload.get("kind") == "class" else "（未记录）"),
        payload.get("task_id", 0), variant_label)
    canvas.text(line, SMALL_SIZE, color=(0.35, 0.35, 0.35))
    metas = []
    if payload.get("unit"):
        metas.append("单元：%s" % payload["unit"])
    if payload.get("knowledge_point"):
        metas.append("知识点：%s" % payload["knowledge_point"])
    if payload.get("cause_label"):
        metas.append("已确认错因：%s" % payload["cause_label"])
    if metas:
        canvas.text("　".join(metas), SMALL_SIZE, color=(0.35, 0.35, 0.35))
    if payload.get("review_state") == "needs_review":
        canvas.text("注意：本任务依据已变，待复核（依据变更不影响已布置的纸笔任务）。",
                    SMALL_SIZE, color=(0.7, 0.35, 0.05))
    canvas.text("姓名：____________　　日期：____________　　用时：________", BODY_SIZE)
    canvas.cursor_y -= 4


def render_student(payload):
    """学生版：只有题目与作答空间——**绝不**出现答案、解析、教学提示。"""
    canvas = _new_canvas()
    canvas.new_page()
    _header(canvas, payload, "学生版")
    canvas.text("作答说明：先把每一步写清楚，再写结论；遇到卡住的地方先自己试，"
                "不要翻参考答案。", SMALL_SIZE, color=(0.35, 0.35, 0.35))
    canvas.cursor_y -= 2
    exercises = payload.get("exercises") or []
    if not exercises:
        canvas.text("（这个任务还没有选练习。）")
    for index, item in enumerate(exercises, 1):
        canvas.ensure_space(70)
        canvas.text("%d. %s" % (index, item.get("stem", "")), BODY_SIZE)
        canvas.text("（难度：%s　用途：%s）" % (item.get("difficulty") or "-",
                                          item.get("purpose_label") or "-"),
                    SMALL_SIZE, color=(0.45, 0.45, 0.45))
        canvas.space_lines(item.get("answer_lines", 3))
    canvas.text("自我检查：把每一步重新算一遍，标出最没有把握的一步。", SMALL_SIZE,
                color=(0.45, 0.45, 0.45))
    return canvas.to_bytes(payload.get("title") or "纸笔任务（学生版）", "学生版题目与作答空间",
                           producer="tutoring_pdf")


def render_teacher(payload):
    """教师版：题目 + 答案 + 解析 + 教学提示（三级提示按任务记录的版本）。"""
    canvas = _new_canvas()
    canvas.new_page()
    _header(canvas, payload, "教师版")
    if payload.get("teacher_note"):
        canvas.text("教师说明：%s" % payload["teacher_note"], SMALL_SIZE,
                    color=(0.3, 0.3, 0.3))
    exercises = payload.get("exercises") or []
    if not exercises:
        canvas.text("（这个任务还没有选练习。）")
    for index, item in enumerate(exercises, 1):
        canvas.ensure_space(90)
        canvas.text("%d. %s" % (index, item.get("stem", "")), BODY_SIZE)
        canvas.text("难度：%s　用途：%s　知识点：%s"
                    % (item.get("difficulty") or "-",
                       item.get("purpose_label") or "-",
                       payload.get("knowledge_point") or "-"),
                    SMALL_SIZE, color=(0.45, 0.45, 0.45))
        canvas.text("答案：%s" % (item.get("answer") or "（未提供）"), BODY_SIZE)
        if item.get("solution"):
            canvas.text("解析：%s" % item["solution"], BODY_SIZE)
        canvas.cursor_y -= 6
    if payload.get("hints"):
        canvas.ensure_space(60)
        canvas.heading("教学提示（按学生卡住的层级逐级给）", size=13.0)
        for level in payload["hints"]:
            canvas.text("%s　%s" % (level.get("level", ""), level.get("text", "")),
                        BODY_SIZE)
        canvas.text("提醒：完整解析必须在学生独立尝试之后才给；教师提前看答案不算学生完成。",
                    SMALL_SIZE, color=(0.45, 0.45, 0.45))
    return canvas.to_bytes(payload.get("title") or "纸笔任务（教师版）",
                           "教师版含答案、解析与教学提示", producer="tutoring_pdf")


# ---------------------------------------------------------------------------
# 回读
# ---------------------------------------------------------------------------
def student_text(pdf_bytes):
    """学生版回读检查：返回 (text, violations)；violations 是混进去的答案/解析/提示片段。"""
    text = extract_text(pdf_bytes)
    violations = [item for item in STUDENT_FORBIDDEN_LABELS if item + "：" in text
                  or ("\n" + item + " ") in text]
    return text, violations
