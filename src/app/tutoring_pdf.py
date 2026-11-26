"""R2-05 纸笔任务 PDF：学生版（题目 + 作答空间）与教师版（题目 + 答案 + 解析 + 教学提示）。

为什么自己写 PDF：本工作副本的运行环境（`C:\\Python314\\python.exe` 与 `venv-desktop`）
没有 reportlab / fpdf，加依赖会让 1.9.0 的打包链路多一份第三方风险。「学生版不含答案与解析」
这条验收要**读回 PDF 文本**，所以中文必须是真正的文本对象（不是图片），做法是：

1. 从系统字体（simhei / msyh / simsun）里取一份 TrueType，按用到的字形做**保留字形编号**的子集
   （不用的字形置空，CID = GID，`/CIDToGIDMap /Identity`），体积只有几十 KB；
2. 写 `/ToUnicode` CMap，让任何阅读器与 `extract_text()` 都能把字形还原成中文；
3. 内容流不压缩（体积换可读性），逐条 `Tj` 便于回读断言。

三个对外函数：
- `render_student(payload)` / `render_teacher(payload)` → PDF 字节；
- `paper_payload(db, task_id)` → 组版数据（题目、答案、解析、三级提示都按**任务记录的版本**取）；
- `extract_text(pdf_bytes)` → 回读文本（验收与界面自检都用它，不许拿函数返回值冒充）。
"""

import os
import struct

# ---------------------------------------------------------------------------
# 页面常量（A4，单位 pt）
# ---------------------------------------------------------------------------
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN_LEFT = 56.0
MARGIN_RIGHT = 56.0
MARGIN_TOP = 64.0
MARGIN_BOTTOM = 56.0
BODY_SIZE = 11.0
TITLE_SIZE = 16.0
SMALL_SIZE = 9.0
LINE_GAP = 6.0
ANSWER_LINE_GAP = 26.0

FONT_CANDIDATES = (
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simhei.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "Deng.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simsunb.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simsun.ttc"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc"),
)

STUDENT_FORBIDDEN_LABELS = ("答案", "解析", "教学提示")
ANSWER_SPACE_MARK = "作答区"


def _font_path():
    override = os.environ.get("TUTORING_PDF_FONT", "")
    for path in ([override] if override else []) + list(FONT_CANDIDATES):
        if path and os.path.exists(path):
            return path
    raise RuntimeError("找不到可用的中文字体（试过 %s）" % ", ".join(
        [path for path in FONT_CANDIDATES]))


# ---------------------------------------------------------------------------
# TrueType 解析（只取组版需要的表；不依赖第三方库）
# ---------------------------------------------------------------------------
class TrueTypeFont:
    """读一份 TrueType 的 cmap / hmtx / glyf / loca，并生成「保留字形编号」的子集。"""

    def __init__(self, path=None):
        self.path = path or _font_path()
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
# PDF 文档：页对象 + 内容流 + 字体
# ---------------------------------------------------------------------------
class _PdfBuilder:
    def __init__(self, font):
        self.font = font
        self.pages = []          # [(content_bytes)]
        self.current = None
        self.used_gids = {0}
        self.cursor_y = 0.0
        self.page_number = 0
        self.headings = []

    # -- 页面 ------------------------------------------------------------
    def new_page(self):
        if self.current is not None:
            self.pages.append(self.current)
        self.current = bytearray()
        self.page_number += 1
        self.cursor_y = PAGE_HEIGHT - MARGIN_TOP
        self._footer()

    def _footer(self):
        text = "第 %d 页" % self.page_number
        self.draw_text(MARGIN_LEFT, 28.0, text, SMALL_SIZE, color=(0.45, 0.45, 0.45))

    # -- 绘制 ------------------------------------------------------------
    def _encode(self, text):
        chunks = []
        for char in text:
            gid = self.font.gid(char)
            self.used_gids.add(gid)
            chunks.append("%04X" % gid)
        return "".join(chunks)

    def draw_text(self, x, y, text, size=BODY_SIZE, color=(0, 0, 0)):
        if not text:
            return
        hex_text = self._encode(text)
        if not hex_text:
            return
        self.current.extend(
            ("BT %.3f %.3f %.3f rg /F1 %.2f Tf 1 0 0 1 %.2f %.2f Tm <%s> Tj ET\n"
             % (color[0], color[1], color[2], size, x, y, hex_text)).encode("latin-1"))

    def draw_line(self, x1, y1, x2, y2, width=0.6, color=(0.55, 0.55, 0.55)):
        self.current.extend(
            ("%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S\n"
             % (color[0], color[1], color[2], width, x1, y1, x2, y2)).encode("latin-1"))

    def ensure_space(self, height):
        if self.cursor_y - height < MARGIN_BOTTOM:
            self.new_page()
            return True
        return False

    # -- 排版 ------------------------------------------------------------
    def wrap(self, text, size, width):
        """按字体实际宽度换行；中文按字断行，英文按空格优先。"""
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

    def paragraph(self, text, size=BODY_SIZE, indent=0.0, gap=LINE_GAP, color=(0, 0, 0)):
        width = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT - indent
        for line in self.wrap(text, size, width):
            self.ensure_space(size + gap)
            self.cursor_y -= size
            self.draw_text(MARGIN_LEFT + indent, self.cursor_y, line, size, color=color)
            self.cursor_y -= gap

    def heading(self, text, size=TITLE_SIZE, gap=10.0):
        self.ensure_space(size + gap + 6)
        self.cursor_y -= size + 4
        self.draw_text(MARGIN_LEFT, self.cursor_y, text, size)
        self.cursor_y -= gap

    def answer_space(self, lines, size=BODY_SIZE):
        for _ in range(max(1, int(lines))):
            self.ensure_space(ANSWER_LINE_GAP)
            self.cursor_y -= ANSWER_LINE_GAP
            self.draw_line(MARGIN_LEFT, self.cursor_y,
                           PAGE_WIDTH - MARGIN_RIGHT, self.cursor_y)
        self.cursor_y -= 6

    def label_value(self, label, value, size=BODY_SIZE):
        self.ensure_space(size + 10)
        self.cursor_y -= size + 2
        self.draw_text(MARGIN_LEFT, self.cursor_y, label, size)
        self.draw_text(MARGIN_LEFT + self.font.text_width(label, size) + 4,
                       self.cursor_y, value, size)
        self.cursor_y -= 10

    # -- 输出 ------------------------------------------------------------
    def to_bytes(self, title, subject, creator="数智伴学 1.9.0"):
        if self.current is not None:
            self.pages.append(self.current)
            self.current = None
        if not self.pages:
            self.new_page()
            self.pages.append(self.current)
            self.current = None
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
        for index, content in enumerate(self.pages):
            content_obj = add(self._stream(bytes(content)))
            page_objects.append(add(
                ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f]"
                 " /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                 % (PAGE_WIDTH, PAGE_HEIGHT, type0, content_obj)).encode("latin-1")))
        kids = " ".join("%d 0 R" % item for item in page_objects)
        objects[catalog - 1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[pages_obj - 1] = (
            "<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_objects))
        ).encode("latin-1")
        info = "<< /Title (%s) /Subject (%s) /Creator (%s) /Producer (tutoring_pdf) >>" % (
            _pdf_string(title), _pdf_string(subject), _pdf_string(creator))
        info_obj = add(info.encode("latin-1"))
        return self._assemble(objects, catalog, info_obj)

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
        if not hasattr(self, "_reverse"):
            self._reverse = {}
            for code, value in self.font.cmap.items():
                self._reverse.setdefault(value, code)
        code = self._reverse.get(gid)
        if code is None:
            return "003F"
        if code <= 0xFFFF:
            return "%04X" % code
        return ("%08X" % code)

    def _font_descriptor(self, font_file_obj):
        units = float(self.font.units_per_em or 1000)
        ascent = int(self.font.ascent * 1000 / units)
        descent = int(self.font.descent * 1000 / units)
        bbox = " ".join("%d" % int(value * 1000 / units) for value in self.font.bbox)
        return ("<< /Type /FontDescriptor /FontName /R2TutoringFont /Flags 4"
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
        return ("<< /Type /Font /Subtype /CIDFontType2 /BaseFont /R2TutoringFont"
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity)"
                " /Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 /W %s"
                " /CIDToGIDMap /Identity >>" % (descriptor_obj, widths)).encode("latin-1")

    def _type0_font(self, cid_obj, to_unicode_obj):
        return ("<< /Type /Font /Subtype /Type0 /BaseFont /R2TutoringFont"
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


def _header(builder, payload, variant_label):
    builder.heading(payload.get("title") or "纸笔辅导任务", TITLE_SIZE)
    line = "班级：%s　　学生：%s　　任务编号：%d　　%s" % (
        payload.get("class_name") or "（未记录）",
        payload.get("student_name") or ("全班" if payload.get("kind") == "class" else "（未记录）"),
        payload.get("task_id", 0), variant_label)
    builder.paragraph(line, SMALL_SIZE, color=(0.35, 0.35, 0.35))
    metas = []
    if payload.get("unit"):
        metas.append("单元：%s" % payload["unit"])
    if payload.get("knowledge_point"):
        metas.append("知识点：%s" % payload["knowledge_point"])
    if payload.get("cause_label"):
        metas.append("已确认错因：%s" % payload["cause_label"])
    if metas:
        builder.paragraph("　".join(metas), SMALL_SIZE, color=(0.35, 0.35, 0.35))
    if payload.get("review_state") == "needs_review":
        builder.paragraph("注意：本任务依据已变，待复核（依据变更不影响已布置的纸笔任务）。",
                          SMALL_SIZE, color=(0.7, 0.35, 0.05))
    builder.paragraph("姓名：____________　　日期：____________　　用时：________",
                      BODY_SIZE)
    builder.cursor_y -= 4


def render_student(payload):
    """学生版：只有题目与作答空间——**绝不**出现答案、解析、教学提示。"""
    font = TrueTypeFont()
    builder = _PdfBuilder(font)
    builder.new_page()
    _header(builder, payload, "学生版")
    builder.paragraph("作答说明：先把每一步写清楚，再写结论；遇到卡住的地方先自己试，"
                      "不要翻参考答案。", SMALL_SIZE, color=(0.35, 0.35, 0.35))
    builder.cursor_y -= 2
    exercises = payload.get("exercises") or []
    if not exercises:
        builder.paragraph("（这个任务还没有选练习。）")
    for index, item in enumerate(exercises, 1):
        builder.ensure_space(70)
        builder.paragraph("%d. %s" % (index, item.get("stem", "")), BODY_SIZE)
        builder.paragraph("（难度：%s　用途：%s）" % (item.get("difficulty") or "-",
                                                item.get("purpose_label") or "-"),
                          SMALL_SIZE, color=(0.45, 0.45, 0.45))
        builder.answer_space(item.get("answer_lines", 3))
    builder.paragraph("自我检查：把每一步重新算一遍，标出最没有把握的一步。", SMALL_SIZE,
                      color=(0.45, 0.45, 0.45))
    return builder.to_bytes(payload.get("title") or "纸笔任务（学生版）", "学生版题目与作答空间")


def render_teacher(payload):
    """教师版：题目 + 答案 + 解析 + 教学提示（三级提示按任务记录的版本）。"""
    font = TrueTypeFont()
    builder = _PdfBuilder(font)
    builder.new_page()
    _header(builder, payload, "教师版")
    if payload.get("teacher_note"):
        builder.paragraph("教师说明：%s" % payload["teacher_note"], SMALL_SIZE,
                          color=(0.3, 0.3, 0.3))
    exercises = payload.get("exercises") or []
    if not exercises:
        builder.paragraph("（这个任务还没有选练习。）")
    for index, item in enumerate(exercises, 1):
        builder.ensure_space(90)
        builder.paragraph("%d. %s" % (index, item.get("stem", "")), BODY_SIZE)
        builder.paragraph("难度：%s　用途：%s　知识点：%s"
                          % (item.get("difficulty") or "-",
                             item.get("purpose_label") or "-",
                             payload.get("knowledge_point") or "-"),
                          SMALL_SIZE, color=(0.45, 0.45, 0.45))
        builder.paragraph("答案：%s" % (item.get("answer") or "（未提供）"), BODY_SIZE,
                          color=(0.55, 0.15, 0.1) if False else (0.1, 0.1, 0.1))
        if item.get("solution"):
            builder.paragraph("解析：%s" % item["solution"], BODY_SIZE)
        builder.cursor_y -= 6
    if payload.get("hints"):
        builder.ensure_space(60)
        builder.heading("教学提示（按学生卡住的层级逐级给）", size=13.0)
        for level in payload["hints"]:
            builder.paragraph("%s　%s" % (level.get("level", ""), level.get("text", "")),
                              BODY_SIZE)
        builder.paragraph("提醒：完整解析必须在学生独立尝试之后才给；教师提前看答案不算学生完成。",
                          SMALL_SIZE, color=(0.45, 0.45, 0.45))
    return builder.to_bytes(payload.get("title") or "纸笔任务（教师版）",
                            "教师版含答案、解析与教学提示")


# ---------------------------------------------------------------------------
# 回读：把 PDF 里的文本抽出来（验收用，不许拿生成函数的返回值冒充）
# ---------------------------------------------------------------------------
def extract_text(pdf_bytes):
    """解出 PDF 里所有 `Tj` 文本；用文件自带的 /ToUnicode CMap 反查字符。"""
    data = pdf_bytes if isinstance(pdf_bytes, bytes) else bytes(pdf_bytes)
    cid_map = _read_to_unicode(data)
    chunks = []
    for stream in _streams(data):
        if b"BT" not in stream or b"Tj" not in stream:
            continue
        for match in _iter_tj(stream):
            chars = []
            for index in range(0, len(match) - 3, 4):
                gid = int(match[index:index + 4], 16)
                chars.append(cid_map.get(gid, ""))
            text = "".join(chars)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


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


def student_text(pdf_bytes):
    """学生版回读检查：返回 (text, violations)；violations 是混进去的答案/解析/提示片段。"""
    text = extract_text(pdf_bytes)
    violations = [item for item in STUDENT_FORBIDDEN_LABELS if item + "：" in text
                  or ("\n" + item + " ") in text]
    return text, violations
