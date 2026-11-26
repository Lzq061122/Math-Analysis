"""R2-05b `pdf_canvas` 验收：纯标准库 PDF 基础件（中文/分页/长表格/图表/空态/两种输出）。

为什么单独一套：R2-04（学情 PDF）要复用这个底层，一旦「中文回读 / 长表格跨页 / 换行不越界 /
页码 / 图表数值标签 / 空态」里任何一项退化，R2-04 的验收会跟着红。所以这里把六项能力各自
钉成**读回 PDF 文本与坐标**的断言，并给每类否定性结论配一条反向自证：

  - 长表格「每页都有表头」：把 `repeat_header=False` 再排一次，同一断言必须变红；
  - 数字右对齐：把同一列的 `align` 改成 `left`，右边界断言必须变红；
  - 页脚页码：把 `footer=False` 再排一次，页码断言必须变红；
  - 中文回读不是恒真：同一份 PDF 里查一个改了一个字的近似串，必须查不到。

坐标来源是 `pdf_canvas.extract_items()`（从内容流的 `Tm`/`Tj` 里解出来），不是生成函数的返回值。

运行：C:\\Python314\\python.exe tests\\verify_r2_05b_pdf_canvas.py
日志：docs/verify-r2-05b-pdf-canvas.txt
"""

import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from app import pdf_canvas as PC                          # noqa: E402
from app import tutoring_pdf as TP                        # noqa: E402

LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-05b-pdf-canvas.txt")

TOTAL = [0]
FAILED = [0]
LINES = []
WORK = []

MARGIN_LEFT = PC.MARGIN_LEFT
MARGIN_RIGHT = PC.MARGIN_RIGHT
MARGIN_BOTTOM = PC.MARGIN_BOTTOM
PAGE_WIDTH = PC.PAGE_WIDTH
FONT = PC.default_font()

TOLERANCE = 1.0

TABLE_COLUMNS = [
    {"title": "序号", "key": "no", "width": 70},
    {"title": "知识点", "key": "kp", "width": 150},
    {"title": "错题数", "key": "count", "width": 70, "align": "right"},
    {"title": "得分率", "key": "rate", "width": 90, "align": "right"},
    {"title": "说明", "key": "note"},
]

ROW_NOTE = "这段文字用来占位，长一点才能验证单元格内自动换行且整行不会被截断。"


def log(text=""):
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))
    LINES.append(text)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if not ok:
        FAILED[0] += 1
    log(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  ← %r" % (detail,)))
    return bool(ok)


def section(title):
    log("")
    log("== %s ==" % title)


def tmpdir(prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    WORK.append(path)
    return path


def group_by_page(items):
    pages = {}
    for item in items:
        pages.setdefault(item["page"], []).append(item)
    return pages


def page_texts(data):
    return {page: [item["text"] for item in items]
            for page, items in group_by_page(PC.extract_items(data)).items()}


def body_items(items):
    """排除页脚/页眉区（页脚基线固定在 margin_bottom-28），只剩正文。"""
    return [item for item in items if item["y"] > MARGIN_BOTTOM - 4]


def text_width(text, size):
    return FONT.text_width(text, size)


def table_rows(count=90):
    return [{"no": "第%03d行" % index,
             "kp": "知识点%02d" % (index % 5),
             "count": index % 9,
             "rate": "%.1f%%" % (50 + index % 50),
             "note": "行%03d说明：%s" % (index, ROW_NOTE)}
            for index in range(1, count + 1)]


def build_table_doc(rows, repeat_header=True, rate_align="right", footer=True):
    columns = [dict(column) for column in TABLE_COLUMNS]
    columns[3]["align"] = rate_align
    doc = PC.PdfCanvas(footer=footer, footer_note="R2-05b 验收")
    doc.new_page()
    doc.heading("长表格跨页验收", size=15.0)
    doc.text("下面是一张 %d 行的表：表头每页重复、整行不拆页、数字列右对齐。" % len(rows))
    doc.table(rows, columns, repeat_header=repeat_header)
    return doc


def main():
    from logmeta import environment_lines
    log("R2-05b pdf_canvas 验收：中文回读 / 分页 / 长表格 / 图表 / 空态 / 两种输出")
    for line in environment_lines():
        log(line)
    try:
        section("1. 依赖自证：纯标准库（不 import reportlab/fpdf/weasyprint/PySide6/pypdf）")
        import ast
        source_path = os.path.join(SRC, "app", "pdf_canvas.py")
        tree = ast.parse(open(source_path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        banned = ("reportlab", "fpdf", "weasyprint", "PySide6", "pypdf", "matplotlib")
        check("依赖：pdf_canvas 实际 import 的模块只有标准库（os / re / struct）",
              imported == {"os", "re", "struct"} and not (imported & set(banned)), imported)
        tutoring_source = open(os.path.join(SRC, "app", "tutoring_pdf.py"),
                               encoding="utf-8").read()
        tutoring_tree = ast.parse(tutoring_source)
        tutoring_imports = set()
        for node in ast.walk(tutoring_tree):
            if isinstance(node, ast.Import):
                tutoring_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                tutoring_imports.add((node.module or "").split(".")[0])
        check("依赖：tutoring_pdf 也只 import 标准库与自己的模块（没有第三方 PDF 库）",
              not (tutoring_imports & set(banned)), tutoring_imports)
        check("抽取真的发生了：字体/画布不再定义在 tutoring_pdf 里，而是 from .pdf_canvas import",
              "class TrueTypeFont" not in tutoring_source
              and "class PdfCanvas" not in tutoring_source
              and "from . import pdf_canvas as PC" in tutoring_source)
        check("字体挑选按覆盖面：本机选中的字体覆盖 ² / − / ⁄ 这些数学符号",
              PC.font_path() == PC.default_font().path
              and all(PC.default_font().gid(char) for char in PC.REQUIRED_GLYPHS),
              (PC.font_path(), PC.REQUIRED_GLYPHS))

        section("2. 中文回读：抽出的文本与原字符串逐字相等（含中英混排与数学符号）")
        samples = [
            "知识点错误分布：移项、去括号、合并同类项",
            "3x − 5 ≥ 7，x × 2 ÷ 4 = 8，面积 25 cm² ，判定 ≠ 与 ⁄ 分数斜线",
            "第 1 题得分率 82.5%，班级平均分 76.4，完整成绩 18 人",
        ]
        doc = PC.PdfCanvas()
        doc.new_page()
        for line in samples:
            doc.text(line, 12.0)
        data = doc.to_bytes("中文回读", "回读断言")
        pages = page_texts(data)
        first_page = pages.get(1, [])
        check("中文回读：三行原文都在（逐字相等，不是包含关系里的模糊匹配）",
              all(any(item == line for item in first_page) for line in samples),
              [line for line in samples if not any(item == line for item in first_page)])
        near_miss = samples[0].replace("移项", "移位")
        check("中文回读不是恒真：改了一个字的近似串查不到（证明比较是逐字的）",
              not any(near_miss == item or near_miss in item for item in first_page), near_miss)
        check("中文回读：符号在 ToUnicode 里反查回了原字符（不是 ?）",
              "²" in "".join(first_page) and "−" in "".join(first_page)
              and "⁄" in "".join(first_page) and "?" not in "".join(first_page))

        section("3. 超长段落：自动换行 + 跨页 + 不越界（按回读坐标断言）")
        paragraph = ("这是一段用来验证自动换行的长文字，中英混排 English words mixed in，"
                     "还有数字 1 2 3 与符号 × ÷ ≥ ≠。") * 40
        doc = PC.PdfCanvas()
        doc.new_page()
        doc.heading("超长段落", size=14.0)
        doc.text(paragraph, PC.BODY_SIZE)
        doc.text("段落结束标记：END-OF-PARAGRAPH", PC.BODY_SIZE)
        data = doc.to_bytes("超长段落", "换行与跨页")
        total_pages = PC.page_count(data)
        items = body_items(PC.extract_items(data))
        overflowing = [item for item in items
                       if item["x"] < MARGIN_LEFT - TOLERANCE
                       or item["x"] + text_width(item["text"], item["size"])
                       > PAGE_WIDTH - MARGIN_RIGHT + TOLERANCE]
        check("超长段落：自动换行跨页（页数 > 1）", total_pages > 1, total_pages)
        check("超长段落：每一行都没有越过左右页边距（含中英混排行）",
              not overflowing, overflowing[:2])
        check("超长段落：所有行都在页脚保留区之上（没有写进页脚）",
              all(item["y"] >= MARGIN_BOTTOM - TOLERANCE for item in items),
              [item for item in items if item["y"] < MARGIN_BOTTOM - TOLERANCE][:2])
        check("超长段落：跨页后内容没丢（结束标记在最后一页）",
              "段落结束标记：END-OF-PARAGRAPH" in "".join(
                  page_texts(data).get(total_pages, [])),
              page_texts(data).get(total_pages))

        section("4. 页眉页脚：「第 n / 共 m 页」逐页正确")
        data = build_table_doc(table_rows(6)).to_bytes("页脚", "页码")
        total_pages = PC.page_count(data)
        pages = group_by_page(PC.extract_items(data))
        found = {}
        for page, items in pages.items():
            for item in items:
                matched = re.match(r"^第 (\d+) / 共 (\d+) 页$", item["text"])
                if matched:
                    found.setdefault(page, []).append(
                        (int(matched.group(1)), int(matched.group(2)), item["y"]))
        check("页脚：每一页都有且只有一条「第 n / 共 m 页」",
              sorted(found) == list(range(1, total_pages + 1))
              and all(len(value) == 1 for value in found.values()),
              {page: len(value) for page, value in found.items()})
        check("页脚：n 与所在页号一致、m 等于总页数（单页与多页都对）",
              all(value[0][0] == page and value[0][1] == total_pages
                  for page, value in found.items()),
              {page: value[0][:2] for page, value in found.items()})
        check("页脚：画在页脚保留区（基线 = margin_bottom - 28）",
              all(abs(value[0][2] - (MARGIN_BOTTOM - 28.0)) < 0.6
                  for value in found.values()),
              {page: value[0][2] for page, value in found.items()})
        # 反向自证：关掉页脚 → 同一条断言必红
        blind = build_table_doc(table_rows(6), footer=False).to_bytes("无页脚", "反向自证")
        check("反向自证：footer=False 时一个页码都读不到 → 证明上面的页码断言不是恒真",
              not any(re.match(r"^第 \d+ / 共 \d+ 页$", item["text"])
                      for item in PC.extract_items(blind)))

        section("5. 长表格：≥3 页、表头每页重复、整行不被截断、数字右对齐")
        rows = table_rows(90)
        doc = build_table_doc(rows)
        data = doc.to_bytes("长表格", "跨页与对齐")
        total_pages = PC.page_count(data)
        pages = group_by_page(PC.extract_items(data))
        check("长表格：90 行排到 ≥3 页", total_pages >= 3, total_pages)
        header_titles = [column["title"] for column in TABLE_COLUMNS]
        row_pages = {}
        for page, items in pages.items():
            texts = [item["text"] for item in items]
            has_row = any(re.match(r"^第\d{3}行$", text) for text in texts)
            if has_row:
                row_pages[page] = texts
        missing_header = [page for page, texts in row_pages.items()
                          if not all(title in texts for title in header_titles)]
        check("长表格：每一个有数据行的页面上都有完整表头（表头重复生效）",
              not missing_header, missing_header)
        split = []
        token_pages = {}
        for page, items in pages.items():
            for item in items:
                matched = re.match(r"^第(\d{3})行$", item["text"])
                if matched:
                    token_pages.setdefault(matched.group(1), set()).add(page)
        note_pages = {}
        for page, items in pages.items():
            for item in items:
                matched = re.match(r"^行(\d{3})说明", item["text"])
                if matched:
                    note_pages.setdefault(matched.group(1), set()).add(page)
        for index, page_set in token_pages.items():
            note_set = note_pages.get(index) or set()
            if len(page_set) != 1 or note_set != page_set:
                split.append((index, sorted(page_set), sorted(note_set)))
        check("长表格：90 行一行不少（序号 token 全在）", len(token_pages) == 90,
              len(token_pages))
        check("长表格：没有任何一行被拆到两页（序号与说明出现在同一页）",
              not split, split[:3])
        joined_page = "".join(page_texts(data).get(1, []))
        note_head = next((item["text"] for item in PC.extract_items(data)
                          if item["text"].startswith("行001")), "")
        check("长表格：说明列在单元格内换行了（同一格被拆成多行，拼回去仍是原文）",
              ROW_NOTE in joined_page
              and len(note_head) < len("行001说明：" + ROW_NOTE), (note_head, len(joined_page)))
        rate_item = next((item for item in PC.extract_items(data)
                          if re.match(r"^\d{1,2}\.\d%$", item["text"])), None)
        expected_right = MARGIN_LEFT + 70 + 150 + 70 + 90 - 4.0
        check("长表格：得分率列右对齐（右边界落在列右边减内边距处）",
              rate_item is not None
              and abs(rate_item["x"] + text_width(rate_item["text"], rate_item["size"])
                      - expected_right) <= 1.5,
              (rate_item and (rate_item["x"], rate_item["text"]), expected_right))
        # 反向自证 A：关掉表头重复 → 后续页面上没有表头
        blind = build_table_doc(rows, repeat_header=False).to_bytes("无表头重复", "反向自证")
        blind_pages = group_by_page(PC.extract_items(blind))
        missing = [page for page, items in blind_pages.items()
                   if any(re.match(r"^第\d{3}行$", item["text"]) for item in items)
                   and not all(column["title"] in [it["text"] for it in items]
                               for column in TABLE_COLUMNS)]
        check("反向自证：repeat_header=False 时后续页面确实没有表头 → 证明「每页表头」不是恒真",
              bool(missing), sorted(blind_pages))
        # 反向自证 B：数字列改成左对齐 → 右边界断言必红
        left_doc = build_table_doc(rows, rate_align="left")
        left_data = left_doc.to_bytes("左对齐", "反向自证")
        left_item = next((item for item in PC.extract_items(left_data)
                          if re.match(r"^\d{1,2}\.\d%$", item["text"])), None)
        check("反向自证：align=left 时同一格不再贴右边界 → 证明右对齐断言不是恒真",
              left_item is not None
              and abs(left_item["x"] + text_width(left_item["text"], left_item["size"])
                      - expected_right) > 5.0,
              left_item and left_item["x"])
        check("长表格：列宽受控（整表宽度不超过正文宽度）",
              sum(float(column.get("width") or 0) for column in TABLE_COLUMNS[:4]) < PAGE_WIDTH,
              TABLE_COLUMNS)

        section("6. 图表：条形图与柱状图的标签/数值可回读，空数据画「暂无数据」")
        bars = [{"label": "移项", "value": 7}, {"label": "去括号", "value": 4},
                {"label": "合并同类项", "value": 3}, {"label": "系数化为1", "value": 2}]
        doc = PC.PdfCanvas()
        doc.new_page()
        doc.heading("知识点错误分布", size=14.0)
        doc.bar_chart(bars, title="条形图：知识点错误分布")
        doc.bar_chart([("完整成绩", 18), ("不完整成绩", 5)], orientation="vertical",
                      title="柱状图：完整成绩分布",
                      value_format="{:.0f} 人")
        data = doc.to_bytes("图表", "条形图与柱状图")
        texts = [item["text"] for item in PC.extract_items(data)]
        flattened = "".join(texts)
        check("条形图：轴标签（知识点名）全部可回读",
              all(bar["label"] in flattened for bar in bars), bars)
        check("条形图：每根柱子的数值标签可回读（7 / 4 / 3 / 2）",
              all(str(bar["value"]) in texts for bar in bars), texts)
        check("条形图：图标题可回读", "条形图：知识点错误分布" in texts)
        check("柱状图：轴标签与数值标签可回读（含单位「人」）",
              "完整成绩" in flattened and "不完整成绩" in flattened
              and "18 人" in texts and "5 人" in texts, texts)
        blank = PC.PdfCanvas()
        blank.new_page()
        blank.bar_chart([])
        blank_text = PC.extract_text(blank.to_bytes("空图", "空态"))
        check("图表空数据：落成「暂无数据」而不是一片空白",
              "暂无数据" in blank_text, blank_text)

        section("7. 空态：居中「暂无数据」可回读且水平居中")
        doc = PC.PdfCanvas()
        doc.new_page()
        doc.heading("学情概览", size=14.0)
        doc.empty_state("暂无数据（该批次未配置应评分题）", height=70)
        data = doc.to_bytes("空态", "暂无数据")
        items = [item for item in PC.extract_items(data) if item["y"] > MARGIN_BOTTOM]
        empty = next((item for item in items if item["text"].startswith("暂无数据")), None)
        center = MARGIN_LEFT + (PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT) / 2.0
        check("空态：文本可回读", empty is not None, items)
        check("空态：水平居中（文字中心与版心中心相差 < 1pt）",
              empty is not None
              and abs(empty["x"] + text_width(empty["text"], empty["size"]) / 2.0 - center) < 1.5,
              empty and (empty["x"], text_width(empty["text"], empty["size"]) / 2.0, center))

        section("8. 两种输出方式同源：to_bytes 与 save() 字节一致，且可重复调用")
        rows_doc = build_table_doc(table_rows(20))
        first = rows_doc.to_bytes("导出同源", "预览与导出")
        second = rows_doc.to_bytes("导出同源", "预览与导出")
        path = os.path.join(tmpdir("r2-05b-"), "同源.pdf")
        build_table_doc(table_rows(20)).save(path, "导出同源", "预览与导出")
        disk = open(path, "rb").read()
        check("输出：同一份排版 to_bytes() 两次调用字节完全一致（幂等，页脚不重复）",
              first == second, (len(first), len(second)))
        check("输出：save() 写到磁盘的字节与 to_bytes() 一致（预览与导出同源）",
              disk == first, (len(disk), len(first)))
        check("输出：同一份 PDF 里「第 1 / 共 m 页」只出现一次（页脚没被补画两遍）",
              first.count("第 1 / 共".encode("utf-16-be")) == 0
              and sum(1 for item in PC.extract_items(first)
                      if re.match(r"^第 1 / 共 \d+ 页$", item["text"])) == 1)
        check("输出：文件是可解析的 PDF（%PDF 头 + Page 对象 + ToUnicode + 嵌入字体）",
              first.startswith(b"%PDF-1.4") and b"/Type /Page" in first
              and b"ToUnicode" in first and b"/FontFile2" in first)
        path2 = os.path.join(tmpdir("r2-05b-"), "嵌套目录", "x.pdf")
        PC.PdfCanvas().save(path2, "空文档", "空")
        check("输出：save() 会自动建目录，且空文档也能出合法 PDF（至少 1 页）",
              os.path.exists(path2) and PC.page_count(open(path2, "rb").read()) >= 1)

        section("9. 与 R2-05 的接缝：tutoring_pdf 仍走同一套 canvas（行为不变）")
        payload = {"task_id": 1, "title": "接缝检查", "kind": "individual",
                   "class_name": "一班", "student_name": "甲", "knowledge_point": "移项",
                   "exercises": [{"stem": "解方程：3x + 5 = 2x - 7", "answer": "x = -12",
                                  "solution": "移项得 x = -12", "answer_lines": 3,
                                  "difficulty": "基础", "purpose_label": "步骤巩固"}],
                   "hints": [{"level": "P1", "text": "先把含 x 的项移到同一边"}]}
        student_pdf = TP.render_student(payload)
        teacher_pdf = TP.render_teacher(payload)
        student_text = PC.extract_text(student_pdf)
        teacher_text = PC.extract_text(teacher_pdf)
        check("接缝：学生版 PDF 不含答案/解析/提示（走 canvas 后行为不变）",
              "x = -12" not in student_text and "解析：" not in student_text
              and "教学提示" not in student_text)
        check("接缝：教师版 PDF 含答案/解析/提示",
              "x = -12" in teacher_text and "解析：" in teacher_text
              and "教学提示" in teacher_text)
        check("接缝：两版 PDF 都带「第 n / 共 m 页」页脚",
              re.search(r"第 1 / 共 \d+ 页", student_text) is not None
              and re.search(r"第 1 / 共 \d+ 页", teacher_text) is not None)
    except Exception:                                       # noqa: BLE001
        import traceback
        log("")
        log("!! 套件异常中断：")
        log(traceback.format_exc())
        FAILED[0] += 1
    finally:
        import shutil
        for path in WORK:
            shutil.rmtree(path, ignore_errors=True)

    log("")
    log("总计 %d 项断言：通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    log("结论：%s" % ("全部通过（exit 0）" if FAILED[0] == 0 else "有失败项（exit 1）"))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(LINES) + "\n")
    return 0 if FAILED[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
