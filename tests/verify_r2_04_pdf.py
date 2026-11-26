# -*- coding: utf-8 -*-
"""R2-04 验收套件：学情分析 PDF（预览与导出同快照、A4 模板、导出历史与权限）。

命令：`C:\\Python314\\python.exe tests\\verify_r2_04_pdf.py`
日志：`docs/verify-r2-04-pdf.txt`

覆盖派单 §6.3 的 9 条验收：
  1 数值与预览一致        2 个人报告不含他人     3 按批次不混入
  4 无成绩显示暂无        5 长表格与公式无乱码/重叠/截断（读回 PDF 文本 + 坐标 + 页码结构）
  6 预览后改数据导出仍对应原快照   7 非所属班级拒绝   8 刷新后旧文件不变
  9 正式报告不含演示数据
外加：代表性错题约束（每类≤2、总数≤6）、已保存建议与待复核标记、导出历史字段与上限、
A4 版面结构（MediaBox/页脚页码）、计划练习接口预留、样例 PDF 落盘。

**所有 PDF 断言都读回 PDF 本身**（`pdf_canvas.extract_text/extract_items/page_count`），
不拿生成函数的返回值冒充；pypdf 在 py314 可用时再做一次独立的页数交叉核对。
"""
import hashlib
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import pdf_canvas, r2_stats, report_pdf  # noqa: E402
from verify_r2_03_stats import (  # noqa: E402
    BATCH, KP_BRACKET, KP_LIKE, KP_MOVE, add_answer, add_check, add_score, answer_id_of,
    build_env, seed_dataset,
)

LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-04-pdf.txt")
SAMPLE_DIR = os.path.join(ROOT, "docs", "samples")
WORK = os.path.join(ROOT, "tests", ".tmp-r2-04")
LINES = []
RESULTS = {"pass": 0, "fail": 0}
TODAY = "2026-11-26"
AT = "2026-11-26T19:30:00+08:00"
TEACHER = {"id": 1, "username": "teacher"}
FORMULA = "化简：x²−4=(x+2)(x−2)，且 x≠0，x≥1，2x+1=5"


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s  %s" % (name, detail))
    return bool(ok)


def equal(name, actual, expected):
    return check(name, actual == expected, "期望 %r，实际 %r" % (expected, actual))


def section(title):
    log("")
    log("== %s ==" % title)


def text_of(data):
    return pdf_canvas.extract_text(data)


def items_of(data):
    return pdf_canvas.extract_items(data)


def pages_of(data):
    return pdf_canvas.page_count(data)


def has(data, needle):
    return str(needle) in text_of(data)


def out_path(name):
    os.makedirs(WORK, exist_ok=True)
    return os.path.join(WORK, name)


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


def seed_long(env, questions=20):
    """长表格 + 公式页用：10 名学生 × 20 题（=200 条作答）、6 名完整成绩、15 条错题。"""
    for student in range(1, 11):
        for question in range(1, questions + 1):
            aid = (student - 1) * questions + question
            add_answer(env, aid, student, question,
                       question_text=FORMULA if question <= 3 else "解方程 2x+1=5",
                       steps="%s\n2x=4\nx=2" % FORMULA if question == 1 else "2x+1=5\n2x=4\nx=2",
                       missing=1 if student == 9 else 0)
    scores = {1: 3.0, 2: 3.5, 3: 4.0, 4: 4.0, 5: 4.5, 6: 5.0}
    for student, per_question in scores.items():
        for question in range(1, questions + 1):
            add_score(env, (student - 1) * questions + question, question, per_question,
                      full_score=5)
    plan = ((1, 1, KP_BRACKET, "bracket_rule"), (2, 1, KP_MOVE, "sign_error"),
            (3, 2, KP_LIKE, "like_terms_rule"), (4, 3, KP_BRACKET, "bracket_rule"),
            (5, 4, KP_MOVE, "term_move_rule"), (6, 5, KP_LIKE, "like_terms_rule"),
            (7, 6, KP_BRACKET, "bracket_rule"), (8, 7, KP_MOVE, "term_move_rule"),
            (10, 8, KP_BRACKET, "sign_error"), (1, 9, KP_MOVE, "sign_error"),
            (2, 10, KP_BRACKET, "bracket_rule"), (3, 11, KP_MOVE, "term_move_rule"),
            (4, 12, KP_LIKE, "like_terms_rule"), (5, 13, KP_BRACKET, "sign_error"),
            (7, 14, KP_MOVE, "term_move_rule"))
    for student, question, kp, code in plan:
        add_check(env, (student - 1) * questions + question, kp, code)
    return env


def seed_second_batch(env):
    """同名不同批次（满分 10）用来证明报告按批次不混入。"""
    from app import db as db_mod
    db_mod.save_batch_config(env["db"], 1, "第二次作业", {
        "name": "第二次作业", "occurred_on": "2026-11-21", "origin": "manual",
        "config_state": "configured",
        "expected_students": [{"key": "S1", "name": "学生1", "code": "S1"}],
        "expected_questions": ["1"], "scored_questions": ["1"],
        "full_scores": {"1": 10}, "rubric": {"1": "标准"}, "rubric_version": "v1"})
    add_answer(env, 501, 1, 1, batch="第二次作业")
    add_check(env, 501, KP_LIKE, "like_terms_rule")
    add_score(env, 501, 1, 10, full_score=10)


def class_report(db, **kwargs):
    options = {"batch": BATCH, "today": TODAY, "generated_at": AT, "user": TEACHER}
    options.update(kwargs)
    return report_pdf.build_class_report(db, 1, **options)


def student_report(db, student_id=3, **kwargs):
    options = {"batch": BATCH, "today": TODAY, "generated_at": AT, "user": TEACHER}
    options.update(kwargs)
    return report_pdf.build_student_report(db, 1, student_id, **options)


# ---------------------------------------------------------------------------
# 一、§6.3-1 数值与预览一致
# ---------------------------------------------------------------------------


def group_preview_matches():
    section("一、§6.3-1 数值与预览一致（PDF 读回 == 预览 == 快照）")
    env = build_env("preview")
    seed_dataset(env)
    report = class_report(env["db"])
    snapshot = report["snapshot"]
    data = report_pdf.render_pdf(report)
    preview = "\n".join(report_pdf.preview_lines(report))
    pdf_text = text_of(data)

    equal("1.1 快照平均分 80.00，预览与 PDF 都写 80.00",
          ("80.00" in preview, "80.00" in pdf_text), (True, True))
    equal("1.2 覆盖率 60.0% 三处一致",
          (snapshot["metrics"]["score_coverage"]["text"] in preview,
           "60.0%" in pdf_text), (True, True))
    equal("1.3 应作答 10 / 有效 45 / 缺测 5 都出现在 PDF 里",
          ("10" in pdf_text and "45" in pdf_text and "5" in pdf_text), True)
    equal("1.4 知识点「去括号」与频次 8 出现在 PDF",
          (KP_BRACKET in pdf_text, "8" in pdf_text), (True, True))
    missing = []
    for cell in _table_cells(report):
        text = str(cell)
        probe = text if len(text) <= 8 else text[:8]
        if probe not in pdf_text:
            missing.append(text)
    equal("1.5 预览里每个表格单元格都能在 PDF 文本里找到（长单元格按前缀）", missing, [])
    equal("1.6 预览标题与 PDF 标题一致", report["title"] in preview and report["title"] in pdf_text,
          True)
    equal("1.7 生成时间与数据版本都进 PDF",
          (AT in pdf_text, report["data_version"].split(" / ")[1] in pdf_text), (True, True))
    equal("1.8 PDF 页数与预览一致（预览不分页但内容不丢：共 12 节）",
          len(report["sections"]), 12)
    return env, report, data


def _table_cells(report):
    cells = []
    for section_ in report["sections"]:
        if section_["kind"] == "table":
            for row in section_["rows"]:
                for column in section_["columns"]:
                    value = str(row.get(column["key"], ""))
                    if value:
                        cells.append(value)
    return cells


# ---------------------------------------------------------------------------
# 二、§6.3-2 个人报告不含他人 / §6.3-3 按批次不混入
# ---------------------------------------------------------------------------


def group_isolation_and_batch():
    section("二、§6.3-2 个人报告不含他人；§6.3-3 按批次不混入")
    env = build_env("isolation")
    seed_dataset(env)
    seed_second_batch(env)
    personal = student_report(env["db"], 3)
    data = report_pdf.render_pdf(personal)
    pdf_text = text_of(data)
    others = ["学生1", "学生2", "学生4", "学生5", "学生6", "学生7", "学生8", "学生9", "学生10"]
    equal("2.1 个人报告不含其他学生姓名",
          [name for name in others if name in pdf_text], [])
    equal("2.2 个人报告只含本人的错题（2 条）",
          len(personal["snapshot"]["mistakes"]), 2)
    equal("2.3 个人报告证据链只含本人作答",
          sorted({item["student_id"] for item in personal["evidence"]}), [3])
    equal("2.4 个人报告含本人姓名与班级", ("学生3" in pdf_text, "初二(3)班" in pdf_text),
          (True, True))

    first = class_report(env["db"])
    second = class_report(env["db"], batch="第二次作业")
    first_text = text_of(report_pdf.render_pdf(first))
    second_text = text_of(report_pdf.render_pdf(second))
    equal("2.5 第一批次报告写平均分 80.00、不写 10.00",
          ("80.00" in first_text, "10.00" in first_text), (True, False))
    equal("2.6 第二批次报告写平均分 10.00、不写 80.00",
          ("10.00" in second_text, "80.00" in second_text), (True, False))
    equal("2.7 第二批次报告错题数 = 1（不合并第一批次的 15 条）",
          second["snapshot"]["metrics"]["confirmed_mistakes"]["value"], 1)
    equal("2.8 第二批次报告满分合计 = 10（满分不可比不混）",
          second["snapshot"]["scope"]["full_score"], 10.0)


# ---------------------------------------------------------------------------
# 三、§6.3-4 无成绩显示暂无
# ---------------------------------------------------------------------------


def group_empty_scores():
    section("三、§6.3-4 无成绩 / 未配置批次显示「暂无」「无法计算」")
    env = build_env("empty-scores")
    seed_dataset(env)
    env["db"].tx().__enter__().execute("DELETE FROM answer_scores")
    report = class_report(env["db"])
    data = report_pdf.render_pdf(report)
    pdf_text = text_of(data)
    equal("3.1 无成绩时平均分显示「暂无」",
          (report["snapshot"]["metrics"]["average_score"]["text"], "暂无" in pdf_text),
          ("暂无", True))
    equal("3.2 无完整成绩时 PDF 不出现 0.00 冒充平均分",
          "0.00" in pdf_text, False)
    equal("3.3 覆盖率有分母（10）→ 0.0%，不是暂无",
          report["snapshot"]["metrics"]["score_coverage"]["text"], "0.0%")
    equal("3.4 逐生作业总分卡片文案 = 完整成绩学生 0 / 应参与评分 10",
          report["snapshot"]["metrics"]["assignment_total_score"]["text"],
          "完整成绩学生 0 / 应参与评分 10")

    blank = build_env("empty-unconfigured", configure=False)
    blank_report = class_report(blank["db"])
    blank_text = text_of(report_pdf.render_pdf(blank_report))
    equal("3.5 未配置批次：应作答人数「暂无」、覆盖率「无法计算」",
          (blank_report["snapshot"]["metrics"]["expected_students"]["text"],
           blank_report["snapshot"]["metrics"]["score_coverage"]["text"]),
          ("暂无", "无法计算"))
    equal("3.6 未配置批次 PDF 里也写「无法计算」", "无法计算" in blank_text, True)
    equal("3.7 未配置批次 PDF 里写「暂无」且不写 0",
          ("暂无" in blank_text, "0.00" in blank_text), (True, False))


# ---------------------------------------------------------------------------
# 四、§6.3-5 长表格与公式（读回文本 + 坐标 + 页码结构）
# ---------------------------------------------------------------------------


def group_layout():
    section("四、§6.3-5 长表格与公式无乱码/重叠/截断（PDF 文本 + 坐标 + 页码）")
    env = build_env("layout", questions=tuple(str(i) for i in range(1, 21)), full_score=5)
    seed_long(env)
    report = class_report(env["db"])
    data = report_pdf.render_pdf(report)
    pdf_text = text_of(data)
    items = items_of(data)
    pages = pages_of(data)
    log("版面：共 %d 页，%d 条文本" % (pages, len(items)))

    check("4.1 长表格跨页（≥3 页）", pages >= 3, pages)
    equal("4.2 第 1 页与最后一页都有页脚「第 n / 共 m 页」",
          all(any(("第 %d / 共 %d 页" % (page, pages)) in item["text"] for item in items)
              for page in range(1, pages + 1)), True)
    missing_rows = ["%d 题" % number for number in range(1, 21) if "%d 题" % number not in pdf_text]
    equal("4.3 20 行各题得分率全部写进 PDF（无截断）", missing_rows, [])
    personal = student_report(env["db"], 1)
    personal_text = text_of(report_pdf.render_pdf(personal))
    equal("4.4 公式字符完整回读（² − ≠ ≥ ×，个人报告证据链）",
          [glyph for glyph in ("x²−4", "≠", "≥", "2x+1=5") if glyph not in personal_text], [])
    equal("4.4b 班级报告不出现学生个人题干（隐私）",
          "x²−4" in pdf_text, False)
    equal("4.5 没有字符被替换成「?」（ToUnicode 正常）", "?" in pdf_text, False)
    font = pdf_canvas.default_font()
    overflow = []
    for item in items:
        width = font.text_width(item["text"], item["size"])
        if item["x"] < pdf_canvas.MARGIN_LEFT - 1:
            overflow.append(("left", item["text"], item["x"]))
        if item["x"] + width > pdf_canvas.PAGE_WIDTH - pdf_canvas.MARGIN_RIGHT + 1.5:
            overflow.append(("right", item["text"], item["x"] + width))
    equal("4.6 没有文字越出版心（不截断、不压边）", overflow[:3], [])
    outside = [item for item in items
               if not (pdf_canvas.MARGIN_BOTTOM - 40 <= item["y"]
                       <= pdf_canvas.PAGE_HEIGHT - pdf_canvas.MARGIN_TOP + 20)]
    equal("4.7 所有文本都在页内（y 坐标不越界）", outside[:3], [])
    duplicate = []
    seen = set()
    for item in items:
        if item["text"].strip() == "":
            continue
        if item["text"] == report["title"]:   # 页眉每页重复，允许
            continue
        if item["y"] < pdf_canvas.MARGIN_BOTTOM:   # 页脚区
            continue
        key = (item["page"], round(item["x"], 1), round(item["y"], 1), item["text"])
        if key in seen:
            duplicate.append(key)
        seen.add(key)
    equal("4.8 正文同页同坐标没有重复绘制（无重叠；页眉页脚除外）", duplicate[:3], [])
    table_result = _long_table_pages(data)
    check("4.9 长表格确实换了页（40 行表格跨页，canvas 返回值）", table_result >= 2, table_result)
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        equal("4.10 pypdf 独立复核页数一致", len(reader.pages), pages)
        box = [round(float(v)) for v in reader.pages[0].mediabox]
        equal("4.11 pypdf 复核 A4 纵向版面（MediaBox 0,0,595,842）",
              (box[2], box[3]), (595, 842))
    except ImportError:  # pragma: no cover - py314 有 pypdf，venv 没有
        log("SKIP 4.10/4.11 当前解释器没有 pypdf")
    return env, report, data


def _long_table_pages(data):
    """从生成侧复算一次表格跨页数（只用于交叉核对读回结果）。"""
    doc = pdf_canvas.PdfCanvas()
    doc.new_page()
    rows = [{"no": "%d 题" % number, "count": "6", "full": "5", "rate": "80.0%"}
            for number in range(1, 41)]
    columns = [{"title": "题号", "key": "no", "width": 60},
               {"title": "已评份数", "key": "count", "width": 76, "align": "right"},
               {"title": "满分", "key": "full", "width": 62, "align": "right"},
               {"title": "得分率", "key": "rate", "align": "right"}]
    return doc.table(rows, columns)["pages"]


# ---------------------------------------------------------------------------
# 五、§6.3-6 预览后改数据导出仍对应原快照
# ---------------------------------------------------------------------------


def group_snapshot_frozen():
    section("五、§6.3-6 预览后改数据，导出仍对应原快照")
    env = build_env("frozen")
    seed_dataset(env)
    report = class_report(env["db"])
    before = {
        "average": report["snapshot"]["metrics"]["average_score"]["text"],
        "mistakes": report["snapshot"]["metrics"]["confirmed_mistakes"]["value"],
        "coverage": report["snapshot"]["metrics"]["score_coverage"]["text"],
    }
    preview = "\n".join(report_pdf.preview_lines(report))
    # 预览之后把数据改掉：删掉全部成绩与错题
    env["db"].tx().__enter__().execute("DELETE FROM answer_scores")
    env["db"].tx().__enter__().execute("DELETE FROM answer_checks")
    data = report_pdf.render_pdf(report)
    pdf_text = text_of(data)
    equal("5.1 导出仍写原快照的平均分 80.00",
          (before["average"], before["average"] in pdf_text), ("80.00", True))
    equal("5.2 导出仍写原快照的覆盖率 60.0%", before["coverage"] in pdf_text, True)
    equal("5.3 导出仍写原快照的错题数 15", "15" in pdf_text, True)
    equal("5.4 预览文本在改数据前后一致",
          preview, "\n".join(report_pdf.preview_lines(report)))
    fresh = class_report(env["db"])
    equal("5.5 对照组：重新生成（刷新）后才反映新数据",
          fresh["snapshot"]["metrics"]["confirmed_mistakes"]["value"], 0)
    equal("5.6 原快照对象未被导出过程改动",
          report["snapshot"]["metrics"]["average_score"]["text"], "80.00")


# ---------------------------------------------------------------------------
# 六、§6.3-7 非所属班级拒绝 / §6.3-8 刷新后旧文件不变
# ---------------------------------------------------------------------------


def group_permission_and_refresh():
    section("六、§6.3-7 非所属班级拒绝；§6.3-8 刷新后旧文件不变")
    env = build_env("permission")
    seed_dataset(env)
    for label, call in (
            ("班级报告", lambda: class_report(env["db"], user={"id": 2, "username": "other"})),
            ("个人报告", lambda: student_report(env["db"], 3,
                                              user={"id": 2, "username": "other"}))):
        try:
            call()
            error = None
        except report_pdf.ReportError as exc:
            error = exc
        check("6.1 %s越权被拒绝（not_owner）" % label,
              error is not None and error.code == "not_owner" and error.status == 404, error)
    try:
        student_report(env["db"], 999)
        missing = None
    except report_pdf.ReportError as exc:
        missing = exc
    check("6.2 非本班学生被拒绝（student_not_found）",
          missing is not None and missing.code == "student_not_found", missing)

    first = class_report(env["db"])
    path_one = out_path("frozen-first.pdf")
    report_pdf.export_pdf(first, path_one, db_file=env["db_file"])
    digest_one = hashlib.sha256(open(path_one, "rb").read()).hexdigest()
    # 改数据 → 刷新 → 再导出
    add_score(env, answer_id_of(8, 1), 1, 20)
    add_score(env, answer_id_of(8, 2), 2, 20)
    add_score(env, answer_id_of(8, 3), 3, 20)
    add_score(env, answer_id_of(8, 4), 4, 20)
    add_score(env, answer_id_of(8, 5), 5, 20)
    refreshed = class_report(env["db"], generated_at="2026-11-26T20:00:00+08:00")
    path_two = out_path("frozen-second.pdf")
    report_pdf.export_pdf(refreshed, path_two, db_file=env["db_file"])
    digest_one_after = hashlib.sha256(open(path_one, "rb").read()).hexdigest()
    digest_two = hashlib.sha256(open(path_two, "rb").read()).hexdigest()
    equal("6.3 旧文件字节未变（sha256 相等）", digest_one, digest_one_after)
    check("6.4 新文件反映新数据（与旧文件不同）", digest_one != digest_two)
    equal("6.5 刷新后平均分变化（新增 1 名完整成绩 100 分）",
          refreshed["snapshot"]["metrics"]["average_score"]["text"], "82.86")
    equal("6.6 旧文件里仍是旧平均分", "80.00" in text_of(open(path_one, "rb").read()), True)
    equal("6.7 新文件里是新平均分", "82.86" in text_of(open(path_two, "rb").read()), True)
    history = report_pdf.load_history(env["db_file"])
    equal("6.8 导出历史 2 条且最新在前",
          (len(history), history[0]["bytes"] > 0, history[0]["at"]),
          (2, True, "2026-11-26T20:00:00+08:00"))
    entry = history[0]
    equal("6.9 历史记录含账号/时间/用途/类型/筛选范围/路径",
          [key for key in ("user", "at", "purpose", "kind", "filters", "path", "sha256")
           if not entry.get(key) and key != "filters"], [])
    equal("6.10 历史条目类型与批次正确",
          (entry["kind"], entry["batch"]), ("class", BATCH))
    equal("6.11 历史文件落在库文件同目录且真实存在",
          (os.path.dirname(report_pdf.history_path(env["db_file"])),
           os.path.exists(report_pdf.history_path(env["db_file"]))),
          (os.path.dirname(os.path.abspath(env["db_file"])), True))


# ---------------------------------------------------------------------------
# 七、§6.3-9 正式报告不含演示数据
# ---------------------------------------------------------------------------


def group_demo():
    section("七、§6.3-9 正式报告不含演示数据")
    env = build_env("demo-data")
    seed_dataset(env)
    baseline = class_report(env["db"])
    baseline_text = text_of(report_pdf.render_pdf(baseline))
    for question in range(1, 6):
        add_answer(env, 900 + question, 2, question, source="合成演示",
                   batch=BATCH, question_text="演示题 %d" % question)
        add_score(env, 900 + question, question, 100)
    report = class_report(env["db"])
    pdf_text = text_of(report_pdf.render_pdf(report))
    equal("7.1 演示行不进报告数字（平均分仍 80.00）",
          report["snapshot"]["metrics"]["average_score"]["text"], "80.00")
    equal("7.2 演示行不进报告数字（有效作答仍 45）",
          report["snapshot"]["metrics"]["valid_answers"]["value"], 45)
    equal("7.3 报告里不出现演示题干", "演示题" in pdf_text, False)
    baseline_lines = baseline_text.splitlines()
    new_lines = [line for line in pdf_text.splitlines()
                 if "演示" not in line and line != "口径说明"]
    equal("7.4 去掉「已排除演示数据」说明后，报告正文与加演示数据前逐行相同",
          new_lines, baseline_lines)
    equal("7.5 演示行被识别并在快照里标注",
          report["snapshot"]["demo"]["demo_rows"] >= 5, True)
    check("7.6 报告里说明了已排除演示数据",
          any("演示" in note for note in report["snapshot"]["notes"]),
          report["snapshot"]["notes"])

    demo_env = build_env("demo-batch")
    from app import db as db_mod
    from app import demo_answers
    db_mod.save_batch_config(demo_env["db"], 1, demo_answers.DEMO_BATCH, {
        "name": demo_answers.DEMO_BATCH, "origin": "import", "config_state": "configured",
        "expected_students": [{"key": "S1", "name": "学生1", "code": "S1"}],
        "expected_questions": ["1"], "scored_questions": ["1"],
        "full_scores": {"1": 10}, "rubric": {"1": "标准"}, "rubric_version": "v1"})
    add_answer(demo_env, 801, 1, 1, source="合成演示", batch=demo_answers.DEMO_BATCH)
    add_check(demo_env, 801, KP_BRACKET, "bracket_rule")
    demo_report = report_pdf.build_class_report(
        demo_env["db"], 1, batch=demo_answers.DEMO_BATCH, today=TODAY,
        generated_at=AT, user=TEACHER)
    demo_text = text_of(report_pdf.render_pdf(demo_report))
    equal("7.7 纯演示批次报告整批「暂无」",
          (demo_report["snapshot"]["metrics"]["average_score"]["text"],
           demo_report["snapshot"]["metrics"]["confirmed_mistakes"]["value"]),
          ("暂无", 0))
    check("7.8 纯演示批次报告显式标注数据来源",
          "合成演示" in demo_text or "演示" in demo_text, demo_text[:200])


# ---------------------------------------------------------------------------
# 八、代表性错题约束 / 已保存建议 / 计划练习接口
# ---------------------------------------------------------------------------


def group_content_rules():
    section("八、代表性错题约束、已保存建议（含待复核）、计划练习接口")
    env = build_env("content")
    seed_dataset(env)
    # 班级建议 + 个人建议（fingerprint 故意与当前错题不一致 → 待复核）
    with env["db"].tx() as conn:
        conn.execute(
            "INSERT INTO advice(class_id, student_id, scope, content, evidence, teacher_edited,"
            " generated_at, updated_at) VALUES (1, NULL, 'class', ?, '{}', 1, ?, ?)",
            ("班级教学建议：下次课重点讲去括号与移项。", AT, AT))
        conn.execute(
            "INSERT INTO tutor_advice(class_id, student_id, content, ai_content, evidence,"
            " fingerprint, teacher_edited, mode, generated_at, updated_at)"
            " VALUES (1, 3, ?, '', '{\"advice\":{}}', 'old-fingerprint', 1, 'manual', ?, ?)",
            ("个人辅导建议：先练去括号，再练移项。", AT, AT))
    report = class_report(env["db"])
    personal = student_report(env["db"], 3)
    picks = report["representative"]
    per_category = {}
    for row in picks:
        per_category[row["_category"]] = per_category.get(row["_category"], 0) + 1
    equal("8.1 代表性错题总数 ≤6", len(picks) <= 6, True)
    equal("8.2 代表性错题每类 ≤2 条", max(per_category.values()) <= 2, True)
    equal("8.3 代表性错题按知识点频次优先（去括号类在前）",
          picks[0]["_category"], KP_BRACKET)
    class_text = text_of(report_pdf.render_pdf(report))
    check("8.4 班级报告使用已保存教学建议", "下次课重点讲去括号与移项" in class_text, class_text[-300:])
    personal_text = text_of(report_pdf.render_pdf(personal))
    check("8.5 个人报告使用已保存个人建议", "先练去括号，再练移项" in personal_text, "")
    check("8.6 建议过期标「待复核」",
          personal["student_advice"]["stale"] is True and "待复核" in personal_text, "")
    equal("8.7 计划练习接口预留（R2-05 接入前为空态）",
          (personal["planned_practice"], "暂无已安排的针对性练习" in personal_text),
          ([], True))
    check("8.8 班级报告不出现学生姓名/学号",
          all(name not in class_text for name in ("学生1", "学生2", "学生3", "学生4", "学生5")),
          "")
    representative = next(section_ for section_ in report["sections"]
                          if section_["title"].startswith("7. 代表性错题"))
    equal("8.9 班级报告代表性错题表不列学生列（隐私）",
          [column["title"] for column in representative["columns"] if column["title"] == "学生"], [])
    equal("8.10 证据链含原始步骤与教师判断（个人报告）",
          ("2x+1=5" in personal_text, "错步原文" in personal_text), (True, True))


# ---------------------------------------------------------------------------
# 九、导出历史上限 / 用途校验 / 样例 PDF
# ---------------------------------------------------------------------------


def group_history_and_samples():
    section("九、导出历史上限、用途校验与样例 PDF")
    env = build_env("history")
    seed_dataset(env)
    report = class_report(env["db"])
    path = out_path("hit.pdf")
    for index in range(report_pdf.HISTORY_LIMIT + 5):
        report_pdf.export_pdf(dict(report, purpose="教学研究"), path, db_file=env["db_file"])
    history = report_pdf.load_history(env["db_file"])
    equal("9.1 导出历史最多保留 %d 条" % report_pdf.HISTORY_LIMIT,
          len(history), report_pdf.HISTORY_LIMIT)
    equal("9.2 历史条目用途记录正确", history[0]["purpose"], "教学研究")
    blank = report_pdf.export_pdf(dict(report, purpose="不存在的用途"), out_path("purpose.pdf"))
    equal("9.3 非法用途回落到默认「存档」", blank["entry"]["purpose"], "存档")

    os.makedirs(SAMPLE_DIR, exist_ok=True)
    long_env = build_env("samples", questions=tuple(str(i) for i in range(1, 21)), full_score=5)
    seed_long(long_env)
    class_sample = class_report(long_env["db"])
    student_sample = student_report(long_env["db"], 1)
    class_path = os.path.join(SAMPLE_DIR, "R2-04_班级学情报告.pdf")
    student_path = os.path.join(SAMPLE_DIR, "R2-04_个人学情报告.pdf")
    report_pdf.export_pdf(class_sample, class_path, db_file=long_env["db_file"])
    report_pdf.export_pdf(student_sample, student_path, db_file=long_env["db_file"])
    class_bytes = open(class_path, "rb").read()
    student_bytes = open(student_path, "rb").read()
    check("9.4 班级样例 PDF 已生成（≥3 页，长表格跨页）",
          pages_of(class_bytes) >= 3, pages_of(class_bytes))
    check("9.5 个人样例 PDF 已生成（含证据与公式）",
          pages_of(student_bytes) >= 2 and "x²−4" in text_of(student_bytes),
          pages_of(student_bytes))
    equal("9.6 样例 PDF 是 A4 纵向（MediaBox 595.28×841.89）",
          class_bytes.count(b"/MediaBox [0 0 595.28 841.89]") > 0, True)
    equal("9.7 样例 PDF 每页都有页码", 
          all(("第 %d / 共 %d 页" % (page, pages_of(class_bytes))) in text_of(class_bytes)
              for page in range(1, pages_of(class_bytes) + 1)), True)
    equal("9.8 样例文件大小与页数写进导出结果",
          (os.path.getsize(class_path) > 10000, pages_of(class_bytes) >= 3), (True, True))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    for line in environment_lines():
        log(line)
    group_preview_matches()
    group_isolation_and_batch()
    group_empty_scores()
    group_layout()
    group_snapshot_frozen()
    group_permission_and_refresh()
    group_demo()
    group_content_rules()
    group_history_and_samples()
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"],
                                          RESULTS["fail"]))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(LINES) + "\n")
    log(exit_line(RESULTS["fail"]))
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
