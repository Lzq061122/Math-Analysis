# R2-05b `pdf_canvas` 接口说明（给 R2-04 学情 PDF 导出用）

**模块**：`src/app/pdf_canvas.py`（纯标准库：只 import `os / re / struct`，不依赖 reportlab / fpdf /
weasyprint / PySide6 / pypdf）
**配套验收**：`tests/verify_r2_05b_pdf_canvas.py`（39 项断言，全部读回 PDF 文本与坐标）
**日志**：`docs/verify-r2-05b-pdf-canvas.txt`
**改造方**：`src/app/tutoring_pdf.py` 已改为调用本模块，对外行为不变（R2-05 两套验收仍 102/102、25/25）

> 为什么有这一层：本机两个解释器都没有 PDF 生成库，而派单要求「基础功能支持离线运行」，所以 PDF
> 只能自己产。R2-05 已经趟过「中文豆腐块 / 文本抽不回 / 字体子集 / 分页丢内容」这些坑，这一层是
> 那些坑的固化结果。**请不要在 `report_pdf.py` 里再写第二套 PDF 输出逻辑。**

---

## 1. 三分钟上手

```python
from app import pdf_canvas as PC

doc = PC.PdfCanvas()                       # A4 纵向（595.28 × 841.89pt），页脚「第 n / 共 m 页」
doc.new_page()                             # 显式开第一页（不调用也会在第一次绘制时自动开）
doc.heading("初一(3)班 学情简报")           # 1 级标题（默认 16pt，粗体）
doc.text("统计范围：第一次作业（2026-11-10）。本页数据只含已确认成绩。")
doc.text("缺分母的指标一律显示「暂无数据」，不用 0 冒充。")

doc.heading("一、知识点错误分布", level=2)
doc.bar_chart([{"label": "移项", "value": 7}, {"label": "去括号", "value": 4},
               {"label": "合并同类项", "value": 3}, {"label": "系数化为1", "value": 2}])

doc.heading("二、各题得分率", level=2)
doc.table(
    [{"no": "1", "rate": "82.5%", "count": 3},
     {"no": "2", "rate": "64.0%", "count": 12}],
    [{"title": "题号", "key": "no", "width": 60},
     {"title": "得分率", "key": "rate", "width": 90, "align": "right"},
     {"title": "错题数", "key": "count", "width": 80, "align": "right"}],
)

doc.empty_state("暂无数据（该批次未配置应评分题）")   # 缺分母就用它，居中带框
doc.save(r"D:\学情简报.pdf", "学情简报", "初一(3)班")  # 预览用 doc.to_bytes(...)
```

**预览与导出同源**：`to_bytes()` 与 `save()` 产出的字节**完全一致**（同一份排版对象），
`to_bytes()` 可以重复调用（页脚不会补画两遍）。R2-04 的「预览与导出同 snapshot」就靠这一条：
先 `data = doc.to_bytes(...)` 存快照，预览渲染 `data`，导出写同一份 `data`。

---

## 2. 坐标系与游标（最容易踩的地方）

```
(0, 841.89) ┌────────────────────────────────┐ (595.28, 841.89)
            │  margin_top = 64                │  ← 正文起点 y = 777.89
            │                                  │
            │   x 从 margin_left = 56 开始     │
            │   cursor_y 是「下一行文字基线」   │  ← 每画一块自动下降
            │                                  │
            │  margin_bottom = 56             │  ← 正文不得低于这里
(0, 0)      └────────────────────────────────┘
             页脚基线 = margin_bottom - 28 = 28
```

- 原点在**左下角**，y 轴向上（PDF 原生坐标）；`doc.cursor_y` 表示下一次写文字的基线纵坐标。
- `doc.margin_left / margin_top / margin_right / margin_bottom`、`doc.content_width`、
  `doc.content_height`、`doc.content_bottom` 都是只读属性/字段，不要手改。
- 想「往下空一点」用 `doc.spacer(h)`；想立即换页用 `doc.page_break()`。
- 直接画坐标：`draw_text(x, y, text, size=..., color=..., bold=False, align="left|center|right")`；
  `align` 以传入的 `x` 为左/中/右基准（`center`/`right` 会自动回退宽度）。
- `draw_line(x1, y1, x2, y2)`、`draw_rect(x, y, w, h, fill=(r,g,b), stroke=(r,g,b))` 是底层原语
  （`fill`/`stroke` 都是 0–1 的 RGB 三元组）。

---

## 3. 分页语义（重要）

| 场景 | 行为 | 你该怎么做 |
|---|---|---|
| 文本块超长 | `text()` 每写一行前调 `ensure_space(size+gap)`，不够就换页，**整段自然续写**，不截断 | 不用管 |
| 表格行放不下 | **整行挪到下一页**（行不拆页），新页自动重画表头 | 不用管；`repeat_header=False` 可关 |
| 图表放不下 | 整张图挪到下一页（先测高再画） | 不用管；若图很高，先 `page_break()` |
| 页脚 / 总页数 | 页脚在 `to_bytes()/save()` 时统一补画，所以「共 m 页」**永远准确** | 不要自己画页码 |
| 页眉（可选） | `PdfCanvas(header_text="初一(3)班 学情简报")`：第 2 页起每页顶部画 | 首页标题自己 `heading()` |

`ensure_space(h)` 的返回值是「是否刚换过页」——只有需要「换页后重画某个块」时才用它（表格内部就是这么做的）。

分页只认 `margin_bottom`：**正文永远不写进页脚区**（验收里有一条断言专门查这个）。

---

## 4. API 清单

### 4.1 构造

```python
PdfCanvas(width=595.28, height=841.89, margins=(56, 64, 56, 56), font=None,
          footer=True, footer_format="第 {n} / 共 {m} 页", header_text="",
          footer_size=9.0, footer_color=(0.45,0.45,0.45),
          footer_note="数智伴学 · 离线报告", on_page=None)
```

- `font=None` → 用进程内共享的 `default_font()`（本机按**字符覆盖面**挑到 `msyh.ttc`，含 ²/−/⁄）。
- `footer_note`：页脚左下角小字；传 `""` 关掉。
- `on_page(page_number)`：每开一页回调一次（做「第 N 部分」之类的分节提示时有用）。
- **一个 `PdfCanvas` 只能 `to_bytes()` 一次语义**（内容流是追加式的）：要出两份就用两份 canvas。

### 4.2 页面与游标

```python
new_page() -> int            # 开新页，返回页号（1 起）
page_break()                 # 同 new_page()
ensure_space(height) -> bool # 剩余高度不够就换页；返回是否换过页
space_left() -> float        # 当前页剩余可用高度
spacer(height)
```

### 4.3 文本块

```python
text(text, size=11.0, indent=0.0, gap=6.0, color=(0,0,0), bold=False,
     align="left", width=None)                       # 自动换行 + 跨页
paragraph(...)                                       # text 的旧名，等价
heading(text, size=None, gap=10.0, level=None, color=(0,0,0), rule=False)
                                                     # level 1/2/3 = 16/13/11.5pt；rule=True 加下划线
label_value(label, value, size=11.0, gap=10.0, label_color=(0.3,0.3,0.3))
                                                     # 「标签：值」一行
rule(width=None, color=..., gap=8.0)                 # 一条水平分隔线
space_lines(count, gap=26.0)                         # n 条横线（作答区/手写留白）
wrap(text, size=11.0, width=None) -> list[str]        # 只算换行，不画（要自己排时用）
```

### 4.4 表格

```python
table(rows, columns, header=True, repeat_header=True, font_size=9.5, header_size=None,
      padding=4.0, line_color=(0.78,0.80,0.84), header_fill=(0.93,0.95,0.985),
      zebra=None, grid=True, header_color=(0.1,0.1,0.1))
      -> {"pages": int, "rows": int, "height": float}
```

- `rows`：`[{"key": value}]` 或 `[(v1, v2, ...)]`（按列序）；值会被 `str()`，可换行。
- `columns`：`[{"title": "题号", "key": "no", "width": 60, "align": "right"}, ...]`
  - `width` 省略 → 均分剩余宽度；列宽总和超过版心 → **等比缩小**（不会画到纸外）；
  - `align`：`left`（默认）/`center`/`right`。**数字列务必 `right`**（R2-04 的显式验收项）；
  - 单元格默认左内边距 4pt，换行按列宽自动算。
- 长表格（R2-04 的重点）：表头每页重复、整行不拆页、行高按内容自适应、`zebra=(0.97,0.97,0.98)`
  可加斑马纹。
- 注意：`rows` 里不要放 `None`（会印成 "None"）；缺值传 `""`。

### 4.5 图表

```python
bar_chart(items, width=None, height=None, orientation="horizontal",
          color=(0.26,0.42,0.62), font_size=9.0, value_format="{:.0f}",
          gap=6.0, label_width=None, value_width=46.0, title=None, max_items=None)
          -> {"pages": int, "items": int, "max_value": float, "orientation": str}
```

- `items`：`[{"label": "移项", "value": 7}]` 或 `[("移项", 7)]`；值会被 `float()`。
- `orientation="horizontal"`：条形图（默认，标签在左、数值在条尾）——适合**知识点/错因分布**；
  `"vertical"`：柱状图（标签在下、数值在柱顶）——适合**各题得分率 / 完整成绩分布**。
- `value_format` 支持 Python format：`"{:.0f}"`、`"{:.1f}%"`、`"{:.0f} 人"`。
- **数值标签是真文本**，可以被 `extract_text()` 读回（验收就是这么断言的）；柱体是填充矩形，不是图片。
- 空列表 → 自动落成 `empty_state("暂无数据")`，不用你判空。
- `height`：条形图是「整组条的总高」，柱状图是「柱区高度」（下方标签另留约 2 行）。
- 一张图**不跨页**：放不下就整张挪到下一页。

### 4.6 空态

```python
empty_state(text="暂无数据", size=13.0, height=64.0, color=(0.52,0.54,0.58), border=True)
```

居中的带框提示。**缺分母的指标一律走它**，不要画 0（`r2_contract.STAT_EMPTY_TEXT` 的口径）。

### 4.7 输出

```python
to_bytes(title="", subject="", creator="数智伴学 1.9.0", producer="pdf_canvas") -> bytes
save(path, title="", subject="", creator="数智伴学 1.9.0") -> path   # 自动建目录
```

- 两者字节一致；`save()` 只是 `to_bytes()` + 写文件。
- 文件头 `%PDF-1.4`，字体作为 `FontFile2` 嵌入，带 `/ToUnicode`。

### 4.8 回读（验收/自检用）

```python
extract_text(pdf_bytes) -> str          # 所有 Tj 文本，按页面与绘制顺序，换行连接
extract_items(pdf_bytes) -> list[dict]  # [{"page", "x", "y", "size", "text"}]，带坐标
page_count(pdf_bytes) -> int            # 读 /Type /Page 个数
```

`extract_items` 的 `x` 是**文字左边缘**（居中/右对齐已经换算过），`y` 是**基线**。
断言「没越界」时用 `x + font.text_width(text, size) <= 595.28 - 56`。

---

## 5. 字体与符号

- 挑字体规则：`TUTORING_PDF_FONT` / `PDF_CANVAS_FONT` 环境变量 > 按**字符覆盖面**（`REQUIRED_GLYPHS`
  = `中×÷²≥≠−⁄`）从系统字体里挑 > 第一个能打开的候选。本机实际命中 `msyh.ttc`（雅黑）。
  黑体（simhei）缺 ²/−/⁄，所以不会被选。
- 已实测可回读的符号：`× ÷ ² ³ ≥ ≠ − ⁄ ％ · — ° （） %` 与中英混排 `/` 等。
- 粗体是**文字渲染模式 2（填充+描边）**合成的：不额外嵌粗体字体文件，一个 `Tj` 就是一段文本，
  回读不会重复。
- 只支持**一份**字体（不做多字体回退）：这样 `ToUnicode` 与 CID 一一对应，回读才不会串码。
  需要新字符时先确认它在 `REQUIRED_GLYPHS` 之外也能被选中字体覆盖，否则换字体（改 `FONT_CANDIDATES`
  顺序即可，但要复跑 `verify_r2_05b_pdf_canvas.py`）。

---

## 6. 常见坑（都是本轮踩过的）

1. **别用 `%` 拼接坐标的字符串**再交给 `draw_text`：`draw_text` 自己处理对齐与编码，直接给文本。
2. **不要自己算 y**：一律用 `text()/table()/bar_chart()` 让游标下降；手写 y 很容易写进页脚区。
3. **不要自己画页码**：总页数只有输出时才知道；`footer_format` 支持 `{n}` / `{m}`。
4. **表格行不要塞长段落**：单元格会换行、行高会变，但一行高到超过一页时（>700pt）就放不下——
   这种情况拆成多行/多条。
5. **数字不要自己加千分位**：`value_format` 里用 `"{:,.0f}"`，否则右对齐会算不准宽度。
6. **同一份 `PdfCanvas` 不要出两份文件**：内容流是追加的；要两份就建两个 canvas（或直接复制 bytes）。
7. **`to_bytes()` 之后不要再画**：之后的绘制会落在「不存在的当前页」上（要么重新 `new_page()`）。
8. **验收断言要读回**：`assert "82.5%" in PC.extract_text(data)`，不要断言 `doc.rows` 之类的返回值。

---

## 7. 与 R2-05 的关系（不要重复实现）

`src/app/tutoring_pdf.py` 现在只剩「纸笔任务版式」：`paper_payload()` 取数据 → 调 `PdfCanvas` 逐块写 →
`to_bytes()`。它的 `render_student/render_teacher/extract_text/student_text` 对外签名与行为未变
（R2-05 验收 102/102、界面 25/25 复跑一致）。

R2-04 写 `src/app/report_pdf.py` 时：**只 import `pdf_canvas`，不要改 `tutoring_pdf`**；
两者互不依赖，唯一共享的是 `pdf_canvas`。
