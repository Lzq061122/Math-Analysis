"""通用 PDF 画布（纯标准库）：A4 纵向、游标排版、自动分页、中文字体子集、长表格、条形/柱状图。

**为什么自己写**：本机两个解释器（`C:\\Python314\\python.exe` 与 `venv-desktop`）都没有
reportlab / fpdf / weasyprint，而派单要求「基础功能支持离线运行」。R2-04 的学情报告必须在
老师机器上离线生成，所以 PDF 只能自己产出。本模块是 R2-05（纸笔任务）与 R2-04（学情报告）
**共用**的唯一底层——两边都不要再写第二套。

设计要点
--------
1. **中文必须可回读**：从系统字体取 TrueType，按用到的字形做「保留字形编号」子集
   （不用字形置空，CID = GID，`/CIDToGIDMap /Identity`），再写 `/ToUnicode` CMap。
   这样任何阅读器都能显示中文，`extract_text()` / `extract_items()` 也能把文本抽回来
   （验收断言必须读回 PDF，不许拿函数返回值冒充）。
2. **内容流不压缩**：体积换可读性与可断言性；一页报告量级只有几十 KB。
3. **坐标系是「游标式」的**：原点在页面左下角（PDF 原生坐标，y 向上），
   `canvas.cursor_y` 是**下一行文字基线**的纵坐标；每画一个块，游标自动下降。
   所有排版方法都在内部处理换行、分页与页脚保留区，调用方只需要按顺序「往下写」。
4. **分页是自动的**：`ensure_space(h)` 发现剩余高度不够就换页；表格换页会重画表头；
   页脚（`第 n / 共 m 页`）在输出时统一补画，所以总页数一定准确。
5. **零第三方依赖**：只 import `os / struct`。

坐标与语义速查（详见 `docs/R2-05b_pdf_canvas接口说明.md`）：

```
(0, 841.89) ┌──────────────────────────┐ (595.28, 841.89)
            │  margin_top=64            │
            │  ← content_width →        │
            │  cursor_y ↓               │
            │  margin_bottom=56         │
(0,0)       └──────────────────────────┘ 页脚基线 = margin_bottom - 28
```

快速上手：

```python
from app import pdf_canvas as PC

doc = PC.PdfCanvas()                       # A4 纵向 + 默认页脚「第 n / 共 m 页」
doc.new_page()
doc.heading("初一(3)班 学情简报")
doc.text("本页为说明文字，超长会自动换行并跨页。")
doc.table(rows, [{"title": "题号", "key": "no", "width": 60},
                 {"title": "得分率", "key": "rate", "align": "right"}])
doc.bar_chart([{"label": "移项", "value": 7}, {"label": "去括号", "value": 4}])
doc.empty_state("暂无数据")
doc.save("out.pdf", "学情简报", "初一(3)班")
```

干预点（给上层留的钩子）：`header_text`（每页页眉）、`footer_format`（页码格式）、
`on_page` 回调（每换一页调用一次，参数为页码与总页数未知时的当前页）。
"""

import os
import re
import struct

# ---------------------------------------------------------------------------
# 页面常量（A4，单位 pt = 1/72 英寸）
# ---------------------------------------------------------------------------
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN_LEFT = 56.0
MARGIN_TOP = 64.0
MARGIN_RIGHT = 56.0
MARGIN_BOTTOM = 56.0

BODY_SIZE = 11.0
TITLE_SIZE = 16.0
SMALL_SIZE = 9.0
TABLE_SIZE = 9.5
CHART_SIZE = 9.0
LINE_GAP = 6.0
LINE_HEIGHT_RATIO = 1.28
ANSWER_LINE_GAP = 26.0

FONT_CANDIDATES = (
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simhei.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "Deng.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simsunb.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simsun.ttc"),
)

# 报告里会出现的中英混排 + 数学符号。挑字体时按「覆盖面」优先，而不是按文件名：
# 黑体（simhei）够常用，但缺 ²/−/⁄；雅黑（msyh）三者都有，所以本机实际会挑到 msyh。
REQUIRED_GLYPHS = "中×÷²≥≠−⁄"

_FONT_PATH_CACHE = {}
_DEFAULT_FONT_CACHE = {}


def font_path(require=REQUIRED_GLYPHS):
    """挑一份可用且**覆盖必需字符**的中文字体。

    顺序：`TUTORING_PDF_FONT` / `PDF_CANVAS_FONT` 覆盖 > 按覆盖面挑候选 > 第一个能开的候选。
    只挑一份字体（不做多字体回退）：报告里所有字符都能在这一份里找到字形，回读才不会出「?」。
    """
    override = os.environ.get("TUTORING_PDF_FONT", "") or os.environ.get("PDF_CANVAS_FONT", "")
    candidates = ([override] if override else []) + list(FONT_CANDIDATES)
    cache_key = (override, str(require))
    if cache_key in _FONT_PATH_CACHE:
        return _FONT_PATH_CACHE[cache_key]
    first_open = None
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            probe = TrueTypeFont(path)
        except Exception:                                  # noqa: BLE001
            continue
        first_open = first_open or path
        if all(probe.gid(char) for char in str(require or "")):
            _FONT_PATH_CACHE[cache_key] = path
            return path
    if first_open:
        _FONT_PATH_CACHE[cache_key] = first_open
        return first_open
    raise RuntimeError("找不到可用的中文字体（试过 %s）" % ", ".join(FONT_CANDIDATES))


# 旧名保留（tutoring_pdf 曾用 `_font_path`）
_font_path = font_path


def default_font():
    """进程内共用的字体对象（解析一次就够：cmap/hmtx/glyf 只读，子集按用到的字形现算）。"""
    if "font" not in _DEFAULT_FONT_CACHE:
        _DEFAULT_FONT_CACHE["font"] = TrueTypeFont(font_path())
    return _DEFAULT_FONT_CACHE["font"]


# ---------------------------------------------------------------------------
# TrueType 解析（只取组版需要的表；不依赖第三方库）
# ---------------------------------------------------------------------------
class TrueTypeFont:
    """读一份 TrueType 的 cmap / hmtx / glyf / loca，并生成「保留字形编号」的子集。"""

    def __init__(self, path=None):
        self.path = path or font_path()
        with open(self.path, "rb") as handle:
            self.data = handle.read()
        self.tables = {}
        self._read_directory()
        self._read_head()
        self._read_hhea()
        self._read_maxp()
        self._read_hmtx()
        self._read_loca()
        self.cmap = self._read_cmap()

    # -- 解析 ------------------------------------------------------------
    def _read_directory(self):
        data = self.data
        tag = data[:4]
        if tag == b"ttcf":
            # TrueType Collection：取第一套字体表（simsun.ttc / msyh.ttc 都是这种）
            num = struct.unpack(">I", data[8:12])[0]
            if num < 1:
                raise ValueError("字体集合为空：%s" % self.path)
            offset = struct.unpack(">I", data[12:16])[0]
        else:
            offset = 0
        sfnt = data[offset:offset + 4]
        if sfnt not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            raise ValueError("不是可用的 TrueType 文件：%s" % self.path)
        num_tables = struct.unpack(">H", data[offset + 4:offset + 6])[0]
        pos = offset + 12
        for _ in range(num_tables):
            name = data[pos:pos + 4].decode("latin-1").strip()
            table_offset, length = struct.unpack(">II", data[pos + 8:pos + 16])
            self.tables[name] = (table_offset, length)
            pos += 16
        for required in ("head", "hhea", "maxp", "hmtx", "loca", "glyf", "cmap"):
            if required not in self.tables:
                raise ValueError("字体缺少 %s 表：%s" % (required, self.path))

    def _read_head(self):
        start = self.tables["head"][0]
        self.units_per_em = struct.unpack(">H", self.data[start + 18:start + 20])[0]
        self.index_to_loc_format = struct.unpack(">h", self.data[start + 50:start + 52])[0]
        self.bbox = struct.unpack(">4h", self.data[start + 36:start + 44])

    def _read_hhea(self):
        start = self.tables["hhea"][0]
        self.ascent = struct.unpack(">h", self.data[start + 4:start + 6])[0]
        self.descent = struct.unpack(">h", self.data[start + 6:start + 8])[0]
        self.number_of_hmetrics = struct.unpack(">H", self.data[start + 34:start + 36])[0]

    def _read_maxp(self):
        start = self.tables["maxp"][0]
        self.num_glyphs = struct.unpack(">H", self.data[start + 4:start + 6])[0]

    def _read_hmtx(self):
        start = self.tables["hmtx"][0]
        widths = []
        for index in range(self.number_of_hmetrics):
            offset = start + index * 4
            widths.append(struct.unpack(">H", self.data[offset:offset + 2])[0])
        self.advance_widths = widths

    def _read_loca(self):
        start, length = self.tables["loca"]
        count = self.num_glyphs + 1
        if self.index_to_loc_format == 0:
            self.loca = [value * 2 for value in struct.unpack(
                ">%dH" % count, self.data[start:start + count * 2])]
        else:
            self.loca = list(struct.unpack(">%dI" % count,
                                           self.data[start:start + count * 4]))

    def _read_cmap(self):
        start = self.tables["cmap"][0]
        num_tables = struct.unpack(">H", self.data[start + 2:start + 4])[0]
        best = None
        for index in range(num_tables):
            pos = start + 4 + index * 8
            platform, encoding, offset = struct.unpack(">HHI", self.data[pos:pos + 8])
            if (platform, encoding) in ((3, 1), (3, 10), (0, 3), (0, 4)):
                best = start + offset
                if (platform, encoding) == (3, 10):
                    break
        if best is None:
            for index in range(num_tables):
                pos = start + 4 + index * 8
                _, _, offset = struct.unpack(">HHI", self.data[pos:pos + 8])
                candidate = start + offset
                fmt = struct.unpack(">H", self.data[candidate:candidate + 2])[0]
                if fmt in (4, 12):
                    best = candidate
                    break
        if best is None:
            raise ValueError("字体没有可用的 Unicode cmap：%s" % self.path)
        return self._parse_cmap_at(best)

    def _parse_cmap_at(self, offset):
        data = self.data
        fmt = struct.unpack(">H", data[offset:offset + 2])[0]
        mapping = {}
        if fmt == 4:
            seg_count = struct.unpack(">H", data[offset + 6:offset + 8])[0] // 2
            ends = struct.unpack(">%dH" % seg_count, data[offset + 14:offset + 14 + seg_count * 2])
            starts_pos = offset + 16 + seg_count * 2
            starts = struct.unpack(">%dH" % seg_count, data[starts_pos:starts_pos + seg_count * 2])
            deltas_pos = starts_pos + seg_count * 2
            deltas = struct.unpack(">%dh" % seg_count,
                                   data[deltas_pos:deltas_pos + seg_count * 2])
            ranges_pos = deltas_pos + seg_count * 2
            ranges = struct.unpack(">%dH" % seg_count,
                                   data[ranges_pos:ranges_pos + seg_count * 2])
            for index in range(seg_count):
                start_code, end_code = starts[index], ends[index]
                if start_code == 0xFFFF:
                    continue
                for code in range(start_code, min(end_code, 0xFFFE) + 1):
                    if ranges[index] == 0:
                        gid = (code + deltas[index]) & 0xFFFF
                    else:
                        glyph_pos = (ranges_pos + index * 2 + ranges[index]
                                     + (code - start_code) * 2)
                        if glyph_pos + 2 > len(data):
                            continue
                        gid = struct.unpack(">H", data[glyph_pos:glyph_pos + 2])[0]
                        if gid:
                            gid = (gid + deltas[index]) & 0xFFFF
                    if gid:
                        mapping[code] = gid
            return mapping
        if fmt == 12:
            groups = struct.unpack(">I", data[offset + 12:offset + 16])[0]
            pos = offset + 16
            for _ in range(groups):
                start_code, end_code, start_gid = struct.unpack(">III", data[pos:pos + 12])
                for code in range(start_code, min(end_code, 0x10FFFF) + 1):
                    mapping[code] = start_gid + (code - start_code)
                pos += 12
            return mapping
        raise ValueError("不支持的 cmap 格式 %s" % fmt)

    # -- 查询 ------------------------------------------------------------
    def gid(self, char):
        return self.cmap.get(ord(char), 0)

    def width_units(self, gid):
        if not self.advance_widths:
            return self.units_per_em // 2
        if gid < len(self.advance_widths):
            return self.advance_widths[gid]
        return self.advance_widths[-1]

    def text_width(self, text, size):
        total = sum(self.width_units(self.gid(char)) for char in text)
        return total * size / float(self.units_per_em)

    # -- 子集 ------------------------------------------------------------
    def _glyph_bytes(self, gid):
        if gid + 1 >= len(self.loca):
            return b""
        start, end = self.loca[gid], self.loca[gid + 1]
        if end <= start:
            return b""
        base = self.tables["glyf"][0]
        return self.data[base + start:base + end]

    def _components(self, gid):
        raw = self._glyph_bytes(gid)
        if len(raw) < 10:
            return ()
        contours = struct.unpack(">h", raw[:2])[0]
        if contours >= 0:
            return ()
        out = []
        pos = 10
        while pos + 4 <= len(raw):
            flags, component = struct.unpack(">HH", raw[pos:pos + 4])
            out.append(component)
            pos += 4
            pos += 4 if flags & 0x0001 else 2          # ARG_1_AND_2_ARE_WORDS
            if flags & 0x0008:                          # WE_HAVE_A_SCALE
                pos += 2
            elif flags & 0x0040:                        # WE_HAVE_AN_X_AND_Y_SCALE
                pos += 4
            elif flags & 0x0080:                        # WE_HAVE_A_TWO_BY_TWO
                pos += 8
            if not flags & 0x0020:                      # MORE_COMPONENTS
                break
        return tuple(out)

    def subset(self, used_gids):
        """保留字形编号的子集：不用的字形置空，GID/CID 不变（CIDToGIDMap 才能是 Identity）。"""
        keep = set(used_gids) | {0}
        pending = list(keep)
        while pending:
            gid = pending.pop()
            for component in self._components(gid):
                if component not in keep:
                    keep.add(component)
                    pending.append(component)
        glyf = bytearray()
        offsets = []
        for gid in range(self.num_glyphs):
            offsets.append(len(glyf))
            if gid in keep:
                raw = self._glyph_bytes(gid)
                if raw:
                    glyf.extend(raw)
                    while len(glyf) % 4:
                        glyf.append(0)
        offsets.append(len(glyf))

        head = bytearray(self._table("head"))
        struct.pack_into(">I", head, 8, 0)                       # checkSumAdjustment 稍后补
        struct.pack_into(">h", head, 50, 1)                      # indexToLocFormat = long
        tables = {
            "head": bytes(head),
            "hhea": self._table("hhea"),
            "maxp": self._table("maxp"),
            "hmtx": self._table("hmtx"),
            "loca": b"".join(struct.pack(">I", value) for value in offsets),
            "glyf": bytes(glyf),
            "cmap": self._table("cmap"),
        }
        for optional in ("name", "OS/2", "post", "cvt ", "fpgm", "prep"):
            if optional in self.tables:
                tables[optional] = self._table(optional)
        return _build_sfnt(tables)

    def _table(self, name):
        if name not in self.tables:
            return b""
        start, length = self.tables[name]
        return self.data[start:start + length]


def _checksum(data):
    padded = data + b"\x00" * ((4 - len(data) % 4) % 4)
    total = 0
    for index in range(0, len(padded), 4):
        total = (total + struct.unpack(">I", padded[index:index + 4])[0]) & 0xFFFFFFFF
    return total


def _build_sfnt(tables):
    """按 sfnt 规范拼一份字体文件（含表目录校验和与 head.checkSumAdjustment）。"""
    names = sorted(tables)
    num_tables = len(names)
    entry_selector = 0
    while (1 << (entry_selector + 1)) <= num_tables:
        entry_selector += 1
    search_range = (1 << entry_selector) * 16
    range_shift = num_tables * 16 - search_range
    header = struct.pack(">IHHHH", 0x00010000, num_tables, search_range,
                         entry_selector, range_shift)
    offset = 12 + 16 * num_tables
    records = []
    body = bytearray()
    for name in names:
        data = tables[name]
        records.append((name, _checksum(data), offset, len(data)))
        body.extend(data)
        while len(body) % 4:
            body.append(0)
        offset = 12 + 16 * num_tables + len(body)
    directory = b"".join(
        struct.pack(">4sIII", name.encode("latin-1"), checksum, table_offset, length)
        for name, checksum, table_offset, length in records)
    font = header + directory + bytes(body)
    total = _checksum(font) & 0xFFFFFFFF
    adjust = (0xB1B0AFBA - total) & 0xFFFFFFFF
    head_offset = next(item[2] for item in records if item[0] == "head")
    font = font[:head_offset + 8] + struct.pack(">I", adjust) + font[head_offset + 12:]
    return font


# ---------------------------------------------------------------------------
# 画布：页面 / 游标 / 绘制 / 文本块 / 表格 / 图表 / 空态 / 输出
# ---------------------------------------------------------------------------
class PdfCanvas:
    """A4 纵向画布（可改尺寸）。所有排版方法都从 `cursor_y` 往下写，需要时自动换页。"""

    def __init__(self, width=PAGE_WIDTH, height=PAGE_HEIGHT, margins=None, font=None,
                 footer=True, footer_format="第 {n} / 共 {m} 页", header_text="",
                 footer_size=SMALL_SIZE, footer_color=(0.45, 0.45, 0.45),
                 footer_note="数智伴学 · 离线报告", on_page=None):
        self.width = float(width)
        self.height = float(height)
        margins = margins or (MARGIN_LEFT, MARGIN_TOP, MARGIN_RIGHT, MARGIN_BOTTOM)
        self.margin_left, self.margin_top, self.margin_right, self.margin_bottom = [
            float(value) for value in margins]
        self.font = font or default_font()
        self.footer_enabled = bool(footer)
        self.footer_format = footer_format
        self.footer_note = footer_note
        self.footer_size = footer_size
        self.footer_color = tuple(footer_color)
        self.header_text = header_text
        self.on_page = on_page

        self.pages = []                # [bytearray] 每页内容流
        self.current = None            # 当前页内容流（None = 还没开页）
        self.used_gids = {0}
        self.page_number = 0
        self.cursor_y = self.height - self.margin_top
        self._reverse = {}
        self._marks_drawn = False      # 页脚/页眉只补画一次（to_bytes 可重复调用）

    # -- 几何 ------------------------------------------------------------
    @property
    def content_width(self):
        return self.width - self.margin_left - self.margin_right

    @property
    def content_height(self):
        return self.height - self.margin_top - self.margin_bottom

    @property
    def content_bottom(self):
        return self.margin_bottom

    def space_left(self):
        return self.cursor_y - self.margin_bottom

    # -- 页面 ------------------------------------------------------------
    def new_page(self):
        """开一页新页；返回新页序号（1 起）。页脚在输出时统一补画，所以本地不做「共 m 页」。"""
        if self.current is not None:
            self.pages.append(self.current)
        self.current = bytearray()
        self.page_number += 1
        self.cursor_y = self.height - self.margin_top
        if self.header_text and self.page_number > 1:
            self._emit_text(self.current, self.margin_left,
                            self.height - self.margin_top + self.footer_size + 6,
                            self.header_text, self.footer_size, (0.45, 0.45, 0.45))
        if callable(self.on_page):
            self.on_page(self.page_number)
        return self.page_number

    def _ensure_page(self):
        if self.current is None:
            self.new_page()

    def ensure_space(self, height):
        """剩余高度不够就换页；返回是否换过页（表格与手绘块用它保持整体不跨页）。"""
        self._ensure_page()
        if self.cursor_y - float(height) < self.margin_bottom:
            self.new_page()
            return True
        return False

    def spacer(self, height=LINE_GAP):
        self._ensure_page()
        self.cursor_y -= float(height)

    def page_break(self):
        self.new_page()

    # -- 底层绘制 --------------------------------------------------------
    def _encode(self, text):
        chunks = []
        for char in text:
            gid = self.font.gid(char)
            self.used_gids.add(gid)
            chunks.append("%04X" % gid)
        return "".join(chunks)

    def _emit_text(self, buffer, x, y, text, size=BODY_SIZE, color=(0, 0, 0), bold=False):
        if not text:
            return
        hex_text = self._encode(text)
        if not hex_text:
            return
        red, green, blue = color[0], color[1], color[2]
        if bold:
            # 粗体用文字渲染模式 2（填充+描边）合成：一个 Tj 就是一段文本，回读不会重复，
            # 也不用额外嵌一份粗体字体文件。描边宽度按字号取，画完立刻复位成 0 Tr。
            buffer.extend(
                ("BT %.3f %.3f %.3f rg %.3f %.3f %.3f RG %.2f w 2 Tr"
                 " /F1 %.2f Tf 1 0 0 1 %.3f %.3f Tm <%s> Tj 0 Tr ET\n"
                 % (red, green, blue, red, green, blue, max(0.18, float(size) * 0.028),
                    float(size), x, y, hex_text)).encode("latin-1"))
            return
        buffer.extend(
            ("BT %.3f %.3f %.3f rg /F1 %.2f Tf 1 0 0 1 %.3f %.3f Tm <%s> Tj ET\n"
             % (red, green, blue, float(size), x, y, hex_text)).encode("latin-1"))

    def draw_text(self, x, y, text, size=BODY_SIZE, color=(0, 0, 0), bold=False, align="left"):
        """在页面坐标 (x, y) 画一行文本（不做换行）。`align` 以 `x` 为左/中/右基准。"""
        if not text:
            return
        text = str(text)
        width = self.font.text_width(text, size)
        if align == "right":
            x = x - width
        elif align == "center":
            x = x - width / 2.0
        self._ensure_page()
        self._emit_text(self.current, x, y, text, size, color, bold)

    def draw_line(self, x1, y1, x2, y2, width=0.6, color=(0.55, 0.55, 0.55)):
        self._ensure_page()
        self.current.extend(
            ("%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S\n"
             % (color[0], color[1], color[2], width, x1, y1, x2, y2)).encode("latin-1"))

    def draw_rect(self, x, y, width, height, fill=None, stroke=None, line_width=0.6):
        """左下角 (x, y)、宽高 (width, height) 的矩形；fill/stroke 为 RGB 元组或 None。"""
        self._ensure_page()
        if fill is not None:
            self.current.extend(
                ("%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f\n"
                 % (fill[0], fill[1], fill[2], x, y, width, height)).encode("latin-1"))
        if stroke is not None:
            self.current.extend(
                ("%.3f %.3f %.3f RG %.2f w %.2f %.2f %.2f %.2f re S\n"
                 % (stroke[0], stroke[1], stroke[2], line_width, x, y, width, height)
                 ).encode("latin-1"))

    # -- 文本块 ----------------------------------------------------------
    def wrap(self, text, size=BODY_SIZE, width=None):
        """按字体实际宽度换行：中文逐字断行，英文优先在空格处断行。"""
        width = self.content_width if width is None else float(width)
        lines = []
        for raw_line in str(text or "").split("\n"):
            if not raw_line:
                lines.append("")
                continue
            buffer = ""
            for char in raw_line:
                candidate = buffer + char
                if self.font.text_width(candidate, size) > width and buffer:
                    cut = buffer.rfind(" ")
                    if cut > 0 and " " in buffer:
                        lines.append(buffer[:cut])
                        buffer = buffer[cut + 1:] + char
                    else:
                        lines.append(buffer)
                        buffer = char
                else:
                    buffer = candidate
            if buffer:
                lines.append(buffer)
        return lines

    def text(self, text, size=BODY_SIZE, indent=0.0, gap=LINE_GAP, color=(0, 0, 0),
             bold=False, align="left", width=None):
        """一段自动换行的文字；超长会自然跨页（每行前都会检查剩余高度）。"""
        self._ensure_page()
        usable = (self.content_width if width is None else float(width)) - indent
        for line in self.wrap(text, size, usable):
            self.ensure_space(size + gap)
            self.cursor_y -= size
            if align == "left":
                self.draw_text(self.margin_left + indent, self.cursor_y, line, size,
                               color=color, bold=bold)
            else:
                base = self.margin_left + indent + usable / 2.0
                self.draw_text(base, self.cursor_y, line, size, color=color, bold=bold,
                               align="center" if align == "center" else "right")
            self.cursor_y -= gap
        return self

    # 旧名（tutoring_pdf 用过 paragraph）：行为与 text() 完全一致
    paragraph = text

    def heading(self, text, size=None, gap=10.0, level=None, color=(0, 0, 0), rule=False):
        """标题层级：level 1/2/3 对应 16/13/11.5pt（也可直接给 size 覆盖）。"""
        if size is None:
            size = {1: TITLE_SIZE, 2: 13.0, 3: BODY_SIZE + 0.5}.get(level or 1, TITLE_SIZE)
        self.ensure_space(size + gap + 6)
        self.cursor_y -= size + 4
        self.draw_text(self.margin_left, self.cursor_y, str(text), size, color=color, bold=True)
        self.cursor_y -= gap
        if rule:
            self.draw_line(self.margin_left, self.cursor_y + gap - 2,
                           self.width - self.margin_right, self.cursor_y + gap - 2,
                           color=(0.8, 0.82, 0.86))
        return self

    def label_value(self, label, value, size=BODY_SIZE, gap=10.0, label_color=(0.3, 0.3, 0.3)):
        """「标签：值」一行；标签固定颜色，值用正文色。"""
        self.ensure_space(size + gap)
        self.cursor_y -= size + 2
        self.draw_text(self.margin_left, self.cursor_y, str(label), size, color=label_color)
        offset = self.font.text_width(str(label), size) + 4
        self.draw_text(self.margin_left + offset, self.cursor_y, str(value), size)
        self.cursor_y -= gap
        return self

    def space_lines(self, count, gap=ANSWER_LINE_GAP, color=(0.62, 0.62, 0.62)):
        """画 n 条横线（学生作答区 / 手写留白）。"""
        for _ in range(max(1, int(count))):
            self.ensure_space(gap)
            self.cursor_y -= gap
            self.draw_line(self.margin_left, self.cursor_y,
                           self.width - self.margin_right, self.cursor_y, color=color)
        self.cursor_y -= 6
        return self

    # 旧名（tutoring_pdf 用过 answer_space）
    answer_space = space_lines

    # -- 表格 ------------------------------------------------------------
    def _normalize_columns(self, columns):
        out = []
        for index, column in enumerate(columns or []):
            if isinstance(column, dict):
                item = {
                    "key": column.get("key", index),
                    "title": str(column.get("title", "")),
                    "width": column.get("width"),
                    "align": column.get("align", "left"),
                }
            else:                                    # ("题号", 60) 或 ("题号", 60, "right")
                title = str(column[0])
                width = column[1] if len(column) > 1 else None
                align = column[2] if len(column) > 2 else "left"
                item = {"key": index, "title": title, "width": width, "align": align}
            if item["align"] not in ("left", "center", "right"):
                item["align"] = "left"
            out.append(item)
        if not out:
            raise ValueError("表格至少要有一列")
        fixed = sum(float(item["width"]) for item in out if item["width"])
        flexible = [item for item in out if not item["width"]]
        if flexible:
            remain = max(30.0, self.content_width - fixed)
            share = remain / len(flexible)
            for item in flexible:
                item["width"] = share
        total = sum(float(item["width"]) for item in out)
        if total > self.content_width:                # 列宽超宽就等比缩，不画到纸外
            scale = self.content_width / total
            for item in out:
                item["width"] = float(item["width"]) * scale
        return out

    def _cell_value(self, row, index, column):
        if isinstance(row, dict):
            return row.get(column["key"], "")
        return row[index] if index < len(row) else ""

    def _row_plan(self, row, columns, font_size, padding):
        widths = [float(item["width"]) for item in columns]
        cells = []
        for index, column in enumerate(columns):
            lines = self.wrap(self._cell_value(row, index, column), font_size,
                              widths[index] - 2 * padding)
            cells.append(lines or [""])
        line_height = font_size * LINE_HEIGHT_RATIO
        height = max(len(lines) for lines in cells) * line_height + 2 * padding
        return cells, height

    def _draw_row(self, cells, columns, height, font_size, padding, bold=False,
                  fill=None, color=(0.1, 0.1, 0.1)):
        widths = [float(item["width"]) for item in columns]
        top = self.cursor_y
        if fill is not None:
            self.draw_rect(self.margin_left, top - height, sum(widths), height, fill=fill)
        line_height = font_size * LINE_HEIGHT_RATIO
        x = self.margin_left
        for index, column in enumerate(columns):
            width = widths[index]
            y = top - padding - font_size
            for line in cells[index]:
                if column["align"] == "right":
                    self.draw_text(x + width - padding, y, line, font_size, color=color,
                                   bold=bold, align="right")
                elif column["align"] == "center":
                    self.draw_text(x + width / 2.0, y, line, font_size, color=color,
                                   bold=bold, align="center")
                else:
                    self.draw_text(x + padding, y, line, font_size, color=color, bold=bold)
                y -= line_height
            x += width
        self.cursor_y = top - height

    def table(self, rows, columns, header=True, repeat_header=True, font_size=TABLE_SIZE,
              header_size=None, padding=4.0, line_color=(0.78, 0.80, 0.84),
              header_fill=(0.93, 0.95, 0.985), zebra=None, grid=True,
              header_color=(0.1, 0.1, 0.1)):
        """长表格：表头自动重复、整行不拆页、单元格内自动换行、数字列可右对齐。

        返回 `{"pages": 跨了几页, "rows": 行数, "height": 总高度}`（`pages` 用页码差算，
        因此可被验收断言「≥3 页」）。
        """
        columns = self._normalize_columns(columns)
        header_size = header_size or font_size
        rows = [list(row) if isinstance(row, (list, tuple)) else dict(row) for row in (rows or [])]
        self._ensure_page()
        first_page = self.page_number
        header_cells, header_height = ([], 0.0)
        if header and columns:
            header_row = {column["key"]: column["title"] for column in columns}
            header_cells, header_height = self._row_plan(header_row, columns, header_size, padding)

        def draw_header():
            self.ensure_space(header_height + 1)
            self._draw_row(header_cells, columns, header_height, header_size, padding,
                           bold=True, fill=header_fill, color=header_color)
            if grid:
                self.draw_line(self.margin_left, self.cursor_y,
                               self.margin_left + sum(float(c["width"]) for c in columns),
                               self.cursor_y, color=line_color)

        if header:
            draw_header()
        total_height = header_height
        for row_index, row in enumerate(rows):
            cells, height = self._row_plan(row, columns, font_size, padding)
            # 整行放不下就换页（行不跨页，避免半行被切走）；新页重画表头。
            if self.cursor_y - height < self.margin_bottom:
                self.new_page()
                if header and repeat_header:
                    draw_header()
            fill = None
            if zebra and row_index % 2 == 1:
                fill = zebra
            self._draw_row(cells, columns, height, font_size, padding, fill=fill)
            total_height += height
            if grid:
                self.draw_line(self.margin_left, self.cursor_y,
                               self.margin_left + sum(float(c["width"]) for c in columns),
                               self.cursor_y, color=line_color)
        if grid:
            left = self.margin_left
            top = self.cursor_y
            total_width = sum(float(c["width"]) for c in columns)
            self.draw_rect(left, top, total_width, total_height, stroke=line_color)
            x = left
            for column in columns[:-1]:
                x += float(column["width"])
                self.draw_line(x, top, x, top + total_height, color=line_color)
        return {"pages": self.page_number - first_page + 1, "rows": len(rows),
                "height": total_height}

    # -- 图表 ------------------------------------------------------------
    @staticmethod
    def _chart_items(items):
        out = []
        for item in items or []:
            if isinstance(item, dict):
                label = str(item.get("label", ""))
                value = item.get("value", 0) or 0
            else:
                label = str(item[0])
                value = item[1] if len(item) > 1 else 0
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = 0.0
            out.append((label, value))
        return out

    def bar_chart(self, items, width=None, height=None, orientation="horizontal",
                  color=(0.26, 0.42, 0.62), font_size=CHART_SIZE, value_format="{:.0f}",
                  gap=6.0, label_width=None, value_width=46.0, title=None, max_items=None):
        """条形图（horizontal，默认）或柱状图（vertical）：单色填充 + 轴标签 + 数值标签。

        - `items`：`[{"label": "移项", "value": 7}, ...]` 或 `[(label, value), ...]`；
        - 图整体不跨页（先 `ensure_space`，不够就换页再画）；
        - 数值标签是**真文本**，可被 `extract_text()` 读回（验收就是这么断言的）。
        """
        pairs = self._chart_items(items)
        if max_items:
            pairs = pairs[:int(max_items)]
        if not pairs:
            self.empty_state("暂无数据")
            return {"pages": 0, "items": 0, "max_value": 0.0, "orientation": orientation}
        self._ensure_page()
        first_page = self.page_number
        if title:
            self.heading(title, size=font_size + 2.0, gap=4.0)
        width = float(width or self.content_width)
        max_value = max(value for _, value in pairs) or 1.0
        if orientation == "vertical":
            self._column_chart(pairs, width, height or 150.0, color, font_size,
                               value_format, gap, max_value)
        else:
            self._bar_chart_horizontal(pairs, width, height, color, font_size,
                                       value_format, gap, label_width, value_width, max_value)
        return {"pages": self.page_number - first_page + 1, "items": len(pairs),
                "max_value": max_value, "orientation": orientation}

    def _bar_chart_horizontal(self, pairs, width, height, color, font_size, value_format,
                              gap, label_width, value_width, max_value):
        count = len(pairs)
        label_width = float(label_width or min(
            170.0, max([self.font.text_width(label, font_size) for label, _ in pairs] + [24.0]) + 6))
        bar_area = max(40.0, width - label_width - value_width)
        if height:
            bar_height = max(5.0, (float(height) - gap * (count - 1)) / count)
        else:
            bar_height = 13.0
        total = count * bar_height + gap * (count - 1)
        self.ensure_space(total + 4)
        top = self.cursor_y
        for index, (label, value) in enumerate(pairs):
            bar_top = top - index * (bar_height + gap)
            center = bar_top - bar_height / 2.0 - font_size * 0.35
            self.draw_text(self.margin_left + label_width - 6, center, label, font_size,
                           align="right")
            length = max(0.6, (value / max_value) * bar_area)
            self.draw_rect(self.margin_left + label_width, bar_top - bar_height,
                           length, bar_height, fill=color)
            self.draw_text(self.margin_left + label_width + length + 4, center,
                           value_format.format(value), font_size, color=(0.25, 0.25, 0.25))
        self.cursor_y = top - total - 6
        self.draw_line(self.margin_left + label_width, self.cursor_y + 3,
                       self.margin_left + label_width, top + 2, color=(0.7, 0.72, 0.76))

    def _column_chart(self, pairs, width, height, color, font_size, value_format, gap, max_value):
        count = len(pairs)
        label_height = font_size * LINE_HEIGHT_RATIO * 2 + 4
        total = float(height) + label_height + font_size * 1.6
        self.ensure_space(total + 4)
        top = self.cursor_y
        base = top - float(height) - 2
        slot = width / count
        bar_width = max(6.0, min(46.0, slot * 0.55))
        self.draw_line(self.margin_left, base, self.margin_left + width, base,
                       color=(0.55, 0.57, 0.62))
        for index, (label, value) in enumerate(pairs):
            center = self.margin_left + slot * (index + 0.5)
            bar_height = max(0.6, (value / max_value) * float(height))
            self.draw_rect(center - bar_width / 2.0, base, bar_width, bar_height, fill=color)
            self.draw_text(center, base + bar_height + 3, value_format.format(value),
                           font_size, color=(0.25, 0.25, 0.25), align="center")
            line_y = base - font_size - 2
            for line in self.wrap(label, font_size, slot - 4)[:2]:
                self.draw_text(center, line_y, line, font_size, align="center")
                line_y -= font_size * LINE_HEIGHT_RATIO
        self.cursor_y = top - total - 6

    def empty_state(self, text="暂无数据", size=13.0, height=64.0,
                     color=(0.52, 0.54, 0.58), border=True):
        """居中「暂无数据」框：缺分母时用它，不要拿 0 冒充（R2-03/R2-04 硬口径）。"""
        self.ensure_space(height + 8)
        top = self.cursor_y
        if border:
            self.draw_rect(self.margin_left, top - height, self.content_width, height,
                           stroke=(0.85, 0.86, 0.9))
        self.draw_text(self.margin_left + self.content_width / 2.0,
                       top - height / 2.0 - size * 0.35, str(text), size, color=color,
                       align="center")
        self.cursor_y = top - height - 8
        return self

    def rule(self, width=None, color=(0.78, 0.80, 0.84), gap=8.0):
        self.ensure_space(gap + 2)
        self.cursor_y -= gap
        line_width = self.content_width if width is None else float(width)
        self.draw_line(self.margin_left, self.cursor_y,
                       self.margin_left + line_width, self.cursor_y, color=color)
        return self

    # -- 输出 ------------------------------------------------------------
    def to_bytes(self, title="", subject="", creator="数智伴学 1.9.0",
                 producer="pdf_canvas"):
        """输出 PDF 字节。页脚（第 n / 共 m 页）在这里统一补画，所以总页数准确。"""
        if self.current is not None:
            self.pages.append(self.current)
            self.current = None
        if not self.pages:
            self.new_page()
            self.pages.append(self.current)
            self.current = None
        if self.footer_enabled or self.header_text:
            self._draw_running_marks()
        font_file = self.font.subset(self.used_gids)
        objects = []

        def add(data):
            objects.append(data)
            return len(objects)

        catalog = add(b"")          # 1
        pages_obj = add(b"")        # 2
        to_unicode = self._to_unicode_stream()
        to_unicode_obj = add(to_unicode)
        font_file_obj = add(self._stream(font_file, extra=b"/Length1 %d" % len(font_file)))
        descriptor = add(self._font_descriptor(font_file_obj))
        cid_font = add(self._cid_font(descriptor))
        type0 = add(self._type0_font(cid_font, to_unicode_obj))
        page_objects = []
        for content in self.pages:
            content_obj = add(self._stream(bytes(content)))
            page_objects.append(add(
                ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f]"
                 " /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                 % (self.width, self.height, type0, content_obj)).encode("latin-1")))
        kids = " ".join("%d 0 R" % item for item in page_objects)
        objects[catalog - 1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[pages_obj - 1] = (
            "<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_objects))
        ).encode("latin-1")
        info = "<< /Title (%s) /Subject (%s) /Creator (%s) /Producer (%s) >>" % (
            _pdf_string(title), _pdf_string(subject), _pdf_string(creator),
            _pdf_string(producer))
        info_obj = add(info.encode("latin-1"))
        return self._assemble(objects, catalog, info_obj)

    def save(self, path, title="", subject="", creator="数智伴学 1.9.0"):
        """把同一份排版结果直接写到文件（预览用 to_bytes、导出用 save，二者同源）。"""
        data = self.to_bytes(title, subject, creator)
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _draw_running_marks(self):
        if self._marks_drawn:            # 允许 to_bytes() 重复调用（预览 + 导出同源）
            return
        self._marks_drawn = True
        total = len(self.pages)
        footer_y = self.margin_bottom - 28.0
        for index, buffer in enumerate(self.pages, 1):
            if self.footer_enabled:
                text = self.footer_format.format(n=index, m=total)
                self._emit_text(buffer, self.width / 2.0 - self.font.text_width(
                    text, self.footer_size) / 2.0, footer_y, text, self.footer_size,
                    self.footer_color)
                if self.footer_note:
                    self._emit_text(buffer, self.margin_left, footer_y, self.footer_note,
                                    self.footer_size - 1.0, (0.62, 0.64, 0.68))
            if self.header_text and index > 1:
                self._emit_text(buffer, self.margin_left,
                                self.height - self.margin_top + self.footer_size + 6,
                                self.header_text, self.footer_size, (0.45, 0.45, 0.45))

    def _stream(self, data, extra=b""):
        head = b"<< /Length %d" % len(data)
        if extra:
            head += b" " + extra
        return head + b" >>\nstream\n" + data + b"\nendstream"

    def _to_unicode_stream(self):
        pairs = sorted((gid, self._unicode_of(gid)) for gid in self.used_gids)
        lines = [
            b"/CIDInit /ProcSet findresource begin",
            b"12 dict begin",
            b"begincmap",
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
            b"/CMapName /Adobe-Identity-UCS def",
            b"/CMapType 2 def",
            b"1 begincodespacerange",
            b"<0000> <FFFF>",
            b"endcodespacerange",
        ]
        chunks = [pairs[index:index + 100] for index in range(0, len(pairs), 100)]
        for chunk in chunks:
            lines.append(b"%d beginbfchar" % len(chunk))
            for gid, code in chunk:
                lines.append(("<%04X> <%s>" % (gid, code)).encode("latin-1"))
            lines.append(b"endbfchar")
        lines.extend([b"endcmap", b"CMapName currentdict /CMap defineresource pop",
                      b"end", b"end"])
        return self._stream(b"\n".join(lines))

    def _unicode_of(self, gid):
        if not self._reverse:
            for code, value in self.font.cmap.items():
                self._reverse.setdefault(value, code)
        code = self._reverse.get(gid)
        if code is None:
            return "003F"
        if code <= 0xFFFF:
            return "%04X" % code
        return "%08X" % code

    def _font_descriptor(self, font_file_obj):
        units = float(self.font.units_per_em or 1000)
        ascent = int(self.font.ascent * 1000 / units)
        descent = int(self.font.descent * 1000 / units)
        bbox = " ".join("%d" % int(value * 1000 / units) for value in self.font.bbox)
        return ("<< /Type /FontDescriptor /FontName /R2CanvasFont /Flags 4"
                " /FontBBox [%s] /ItalicAngle 0 /Ascent %d /Descent %d /CapHeight %d"
                " /StemV 80 /FontFile2 %d 0 R >>"
                % (bbox, ascent, descent, ascent, font_file_obj)).encode("latin-1")

    def _cid_font(self, descriptor_obj):
        units = float(self.font.units_per_em or 1000)
        pairs = []
        for gid in sorted(self.used_gids):
            width = int(round(self.font.width_units(gid) * 1000 / units))
            pairs.append("%d [%d]" % (gid, width))
        widths = "[%s]" % " ".join(pairs)
        return ("<< /Type /Font /Subtype /CIDFontType2 /BaseFont /R2CanvasFont"
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity)"
                " /Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 /W %s"
                " /CIDToGIDMap /Identity >>" % (descriptor_obj, widths)).encode("latin-1")

    def _type0_font(self, cid_obj, to_unicode_obj):
        return ("<< /Type /Font /Subtype /Type0 /BaseFont /R2CanvasFont"
                " /Encoding /Identity-H /DescendantFonts [%d 0 R]"
                " /ToUnicode %d 0 R >>" % (cid_obj, to_unicode_obj)).encode("latin-1")

    def _assemble(self, objects, catalog, info_obj):
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, data in enumerate(objects, 1):
            offsets.append(len(out))
            out.extend(("%d 0 obj\n" % index).encode("latin-1"))
            out.extend(data)
            out.extend(b"\nendobj\n")
        xref = len(out)
        out.extend(("xref\n0 %d\n" % (len(objects) + 1)).encode("latin-1"))
        out.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            out.extend(("%010d 00000 n \n" % offset).encode("latin-1"))
        out.extend(
            ("trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
             % (len(objects) + 1, catalog, info_obj, xref)).encode("latin-1"))
        return bytes(out)


def _pdf_string(text):
    raw = str(text or "").replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return "".join(char if 32 <= ord(char) < 127 else "?" for char in raw)


# ---------------------------------------------------------------------------
# 回读：把 PDF 里的文本抽出来（验收断言用它，不许拿生成函数的返回值冒充）
# ---------------------------------------------------------------------------
def extract_text(pdf_bytes):
    """解出 PDF 里所有 `Tj` 文本（按页面、按绘制顺序，用换行连接）。"""
    data = pdf_bytes if isinstance(pdf_bytes, bytes) else bytes(pdf_bytes)
    cid_map = _read_to_unicode(data)
    chunks = []
    for stream in _streams(data):
        if b"BT" not in stream or b"Tj" not in stream:
            continue
        for match in _iter_tj(stream):
            text = _decode_hex(match, cid_map)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def extract_items(pdf_bytes):
    """回读每一条文本及其位置：`[{"page", "x", "y", "size", "text"}]`。

    用来断言「换行不越界」「页脚位置」这类需要坐标的证据——只看文本本身证明不了排版。
    """
    data = pdf_bytes if isinstance(pdf_bytes, bytes) else bytes(pdf_bytes)
    cid_map = _read_to_unicode(data)
    out = []
    for page_index, stream in enumerate(_content_streams(data), 1):
        if b"BT" not in stream:
            continue
        for hex_text, x, y, size in _iter_text_items(stream):
            text = _decode_hex(hex_text, cid_map)
            if text:
                out.append({"page": page_index, "x": x, "y": y, "size": size, "text": text})
    return out


def page_count(pdf_bytes):
    """页数（读 `/Type /Page` 对象个数，不依赖生成函数的返回值）。"""
    data = pdf_bytes if isinstance(pdf_bytes, bytes) else bytes(pdf_bytes)
    return data.count(b"/Type /Page ") + data.count(b"/Type /Page\n")


def _decode_hex(match, cid_map):
    chars = []
    for index in range(0, len(match) - 3, 4):
        gid = int(match[index:index + 4], 16)
        chars.append(cid_map.get(gid, ""))
    return "".join(chars)


def _streams(data):
    out = []
    pos = 0
    while True:
        start = data.find(b"stream", pos)
        if start < 0:
            break
        if data[start + 6:start + 8] == b"\r\n":
            body_start = start + 8
        elif data[start + 6:start + 7] == b"\n":
            body_start = start + 7
        else:
            # 「stream」后面必须是换行才算流开始；否则这是别处的同名串，跳过。
            pos = start + 6
            continue
        end = data.find(b"endstream", body_start)
        if end < 0:
            break
        out.append(data[body_start:end])
        pos = end + len(b"endstream")
    return out


def _objects(data):
    """按 `/Length` 精确切出每个间接对象的内容（不靠找 endobj，字体二进制里也可能出现它）。"""
    objects = {}
    for match in re.finditer(rb"(?m)^(\d+) 0 obj\n", data):
        number = int(match.group(1))
        start = match.end()
        stream_pos = data.find(b"stream\n", start)
        endobj_pos = data.find(b"\nendobj", start)
        head_end = stream_pos if stream_pos >= 0 else endobj_pos
        head = data[start:head_end] if head_end >= 0 else b""
        if stream_pos >= 0 and endobj_pos > stream_pos and b"/Length" in head:
            matched = re.search(rb"/Length (\d+)", head)
            if matched:
                length = int(matched.group(1))
                body_start = stream_pos + len(b"stream\n")
                objects[number] = data[body_start:body_start + length]
                continue
        if endobj_pos < 0:
            continue
        objects[number] = data[start:endobj_pos]
    return objects


def _page_streams(data):
    """按页面顺序取内容流：先从 Page 对象的 `/Contents` 拿对象号，再取那些对象。"""
    objects = _objects(data)
    out = []
    for number in sorted(objects):
        body = objects[number]
        if b"/Type /Page " not in body and b"/Type /Page\n" not in body:
            continue
        matched = re.search(rb"/Contents (\d+) 0 R", body)
        if not matched:
            continue
        content = objects.get(int(matched.group(1)))
        if content is not None:
            out.append(content)
    return out


def _content_streams(data):
    """只要页面内容流（字体程序等二进制流一律不算页）。"""
    return _page_streams(data)


def _iter_tj(stream):
    pos = 0
    while True:
        start = stream.find(b"<", pos)
        if start < 0:
            return
        end = stream.find(b">", start)
        if end < 0:
            return
        candidate = stream[start + 1:end].decode("latin-1", "replace")
        after = stream[end + 1:end + 80]
        if b"Tj" in after or b"TJ" in after:
            if candidate and all(char in "0123456789abcdefABCDEF" for char in candidate) \
                    and len(candidate) % 4 == 0:
                yield candidate
        pos = end + 1


def _iter_text_items(stream):
    """解出 `BT ... Tf 1 0 0 1 x y Tm <hex> Tj ET` 里的 (hex, x, y, size)。"""
    pos = 0
    size = 0.0
    x = y = 0.0
    while pos < len(stream):
        tf = stream.find(b"/F1 ", pos)
        tm = stream.find(b" Tm ", pos)
        tj = stream.find(b" Tj", pos)
        marks = [value for value in (tf, tm, tj) if value >= 0]
        if not marks:
            return
        first = min(marks)
        if first == tf:
            end = stream.find(b" Tf", tf)
            if end < 0:
                return
            try:
                size = float(stream[tf + 4:end].decode("latin-1").strip())
            except ValueError:
                size = 0.0
            pos = end + 3
            continue
        if first == tm:
            start = stream.rfind(b"1 0 0 1 ", 0, tm)
            if start < 0:
                pos = tm + 4
                continue
            try:
                parts = stream[start + 8:tm].decode("latin-1").split()
                x, y = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                x = y = 0.0
            pos = tm + 4
            continue
        # Tj：往回找最近的 <hex>
        start = stream.rfind(b"<", 0, tj)
        end = stream.rfind(b">", 0, tj)
        if start < 0 or end < start:
            pos = tj + 3
            continue
        candidate = stream[start + 1:end].decode("latin-1", "replace")
        pos = tj + 3
        if candidate and all(char in "0123456789abcdefABCDEF" for char in candidate) \
                and len(candidate) % 4 == 0:
            yield candidate, x, y, size


def _read_to_unicode(data):
    mapping = {}
    for stream in _streams(data):
        if b"beginbfchar" not in stream and b"beginbfrange" not in stream:
            continue
        text = stream.decode("latin-1", "replace")
        for block in text.split("beginbfchar")[1:]:
            body = block.split("endbfchar")[0]
            for line in body.strip().splitlines():
                parts = line.split(">")
                if len(parts) < 2:
                    continue
                src = parts[0].strip().lstrip("<")
                dst = parts[1].strip().lstrip("<")
                try:
                    gid = int(src, 16)
                except ValueError:
                    continue
                mapping[gid] = _utf16_hex(dst)
        for block in text.split("beginbfrange")[1:]:
            body = block.split("endbfrange")[0]
            for line in body.strip().splitlines():
                pieces = line.strip().strip("[]").split()
                if len(pieces) < 3:
                    continue
                try:
                    low = int(pieces[0].strip("<>"), 16)
                    high = int(pieces[1].strip("<>"), 16)
                    base = int(pieces[2].strip("<>"), 16)
                except ValueError:
                    continue
                for offset in range(0, min(high - low, 255) + 1):
                    mapping[low + offset] = _utf16_hex("%04X" % (base + offset))
    return mapping


def _utf16_hex(raw):
    if len(raw) == 4:
        return chr(int(raw, 16))
    if len(raw) == 8:
        try:
            return bytes.fromhex(raw).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return ""
    if len(raw) == 2:
        return chr(int(raw, 16))
    return ""
