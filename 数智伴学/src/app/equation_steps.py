"""一元一次方程的确定性错步定位（作答核对契约 §2）。

为什么单独成模块：定位只回答一件事——相邻两行是不是同一个解集。它不推断学生为什么这样写，
也不调用模型，因此可以离线、可复现。原因候选属于已审错因库（B2），本轮不从这里猜。

做法：把每行解析成 Ax+B=Cx+D，A、B、C、D 都用 fractions.Fraction 记录，相邻两行解集相同
等价于 (A-C, B-D) 与 (A'-C', B'-D') 成比例；所有比较都用有理数交叉相乘，不引入浮点误差。
题型只限一元一次方程，超范围时原样返回并转人工，绝不硬判。
"""

import re
from fractions import Fraction

# 固定说明句：这里只记录观察到的现象，不写「学生可能……」「因为学生……」式推断。
REASON_FIRST_BAD = "这里只记录观察到的不等价，不推断学生为什么这样写。"
REASON_MISSING = "这条作答标为缺测，没有学生步骤可核对；这里不标错步，也不假设原因。"
REASON_MISSING_STEPS = "学生只写了最终答案，缺少可核对的过程；这里只记录缺少步骤，不猜错在哪一行。"
MANUAL_OUT_OF_SCOPE = "已转人工"
MANUAL_MISSING_STEPS = "缺少步骤：请补录过程后再核对。"

# 全角、数学减号、乘号等统一成 ASCII 再解析；位置不变，方便把下划线映射回学生原文。
_TRANSLATE = {
    "－": "-", "﹣": "-", "−": "-", "–": "-", "—": "-",
    "＋": "+", "＝": "=", "×": "*", "＊": "*", "·": "*", "•": "*", "∙": "*",
    "（": "(", "）": ")", "／": "/", "．": ".",
    "ｘ": "x", "Ｘ": "x", "ｙ": "y", "Ｙ": "y",
}
for _digit in range(10):
    _TRANSLATE[chr(0xFF10 + _digit)] = str(_digit)

_ALLOWED = set("0123456789xy+-*/().=")

# 只有结论行：x=1、x=-14/3、2=x 这类形态（已去掉空白与全角）。
_ANSWER_ONLY = re.compile(
    r"^(?:[xy]=[+-]?\d+(?:\.\d+)?(?:/[+-]?\d+(?:\.\d+)?)?"
    r"|[+-]?\d+(?:\.\d+)?(?:/[+-]?\d+(?:\.\d+)?)?=[xy])$"
)

# 单括号形式 k(a±b)，用来决定 contrast.note 用哪一句模板；k 带符号，-2(x-3) 的 k 是 -2。
_BRACKET_FORM = re.compile(
    r"([+-]?\d+(?:\.\d+)?)\s*\(\s*[+-]?\d*(?:\.\d+)?\s*[xy]\s*([+-]\s*\d+(?:\.\d+)?)\s*\)"
)


class _ParseFault(Exception):
    """这一行不是可核验的一元一次方程；对外统一转成 out_of_scope。"""


def _is_decoration(char):
    """汉字的说明文字与中文标点不参与运算，直接跳过（“解：”“去括号得”“元”）。"""
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
        or char in "“”‘’…"
    )


def _scan(text):
    """把一行整理成 (字符, 原文下标) 列表；遇到无法识别的符号返回 None。"""
    kept = []
    for index, char in enumerate(text or ""):
        char = _TRANSLATE.get(char, char)
        if char in _ALLOWED:
            kept.append((char, index))
        elif char.isspace() or _is_decoration(char):
            continue
        else:
            return None
    return kept


def _flat(text):
    """只保留参与运算的字符，用来比较「学生是不是把题干又抄了一遍」。"""
    chars = _scan(text)
    return None if chars is None else "".join(char for char, _ in chars)


def _split_lines(text):
    """按换行与分号拆步骤：老师的 Excel 里常把多步写在同一格。"""
    lines = []
    for chunk in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        for piece in re.split(r"[；;]", chunk):
            piece = piece.strip()
            if piece:
                lines.append(piece)
    return lines


def _tokens(chars):
    """把字符列表变成记号；相邻的「数/未知数/右括号」与「数/未知数/左括号」之间补乘号。"""
    tokens = []
    index = 0
    size = len(chars)
    while index < size:
        char, position = chars[index]
        if char.isdigit():
            end = index
            while end < size and chars[end][0].isdigit():
                end += 1
            if end < size and chars[end][0] == ".":
                dot = end + 1
                while dot < size and chars[dot][0].isdigit():
                    dot += 1
                if dot == end + 1:
                    raise _ParseFault()
                end = dot
            text = "".join(item[0] for item in chars[index:end])
            tokens.append(("num", Fraction(text), position, chars[end - 1][1] + 1))
            index = end
        elif char in "xy":
            tokens.append(("var", char, position, position + 1))
            index += 1
        elif char in "+-*/()":
            tokens.append((char, char, position, position + 1))
            index += 1
        else:
            raise _ParseFault()

    merged = []
    for token in tokens:
        if merged and merged[-1][0] in ("num", "var", ")") and token[0] in ("num", "var", "("):
            merged.append(("*", "*", merged[-1][3], token[2]))
        merged.append(token)
    return merged


class _Parser:
    """递归下降：只接受一次式，乘出 x² 或除以含 x 的式子都按超范围处理。"""

    def __init__(self, tokens):
        self.tokens = tokens
        self.position = 0

    def peek(self):
        if self.position < len(self.tokens):
            return self.tokens[self.position][0]
        return None

    def take(self):
        token = self.tokens[self.position]
        self.position += 1
        return token

    def parse(self):
        poly = self.expression()
        if self.position != len(self.tokens):
            raise _ParseFault()
        return poly

    def expression(self):
        poly = self.term()
        while self.peek() in ("+", "-"):
            operator = self.take()[0]
            right = self.term()
            if operator == "+":
                poly = (poly[0] + right[0], poly[1] + right[1])
            else:
                poly = (poly[0] - right[0], poly[1] - right[1])
        return poly

    def term(self):
        poly = self.factor()
        while self.peek() in ("*", "/"):
            operator = self.take()[0]
            right = self.factor()
            if operator == "*":
                left_x, left_c = poly
                right_x, right_c = right
                if left_x != 0 and right_x != 0:
                    raise _ParseFault()
                poly = (left_x * right_c + left_c * right_x, left_c * right_c)
            else:
                if right[0] != 0 or right[1] == 0:
                    raise _ParseFault()
                poly = (poly[0] / right[1], poly[1] / right[1])
        return poly

    def factor(self):
        kind = self.peek()
        if kind in ("+", "-"):
            self.take()
            value = self.factor()
            return value if kind == "+" else (-value[0], -value[1])
        if kind == "num":
            return (0, self.take()[1])
        if kind == "var":
            self.take()
            return (1, 0)
        if kind == "(":
            self.take()
            value = self.expression()
            if self.peek() != ")":
                raise _ParseFault()
            self.take()
            return value
        raise _ParseFault()


def _poly_text(poly):
    """把 (x 系数, 常数) 写成 3x-6 / -x+2 / 0 这样的规范形式。"""
    coefficient, constant = poly
    parts = []
    if coefficient != 0:
        if coefficient == 1:
            parts.append("x")
        elif coefficient == -1:
            parts.append("-x")
        else:
            parts.append("%sx" % _fraction_text(coefficient))
    if constant != 0:
        body = _fraction_text(abs(constant))
        if parts:
            parts.append(("-" if constant < 0 else "+") + body)
        else:
            parts.append(("-" if constant < 0 else "") + body)
    return "".join(parts) if parts else "0"


def _fraction_text(value):
    if value.denominator == 1:
        return str(value.numerator)
    return "%d/%d" % (value.numerator, value.denominator)


def _top_terms(chars):
    """在括号外按加减号切项，供下划线定位；切不出来就返回空表，调用方退回整行。"""
    spans = []
    depth = 0
    start = 0
    for index, (char, _position) in enumerate(chars):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char in "+-" and depth == 0 and index > 0:
            spans.append((start, index))
            start = index
    spans.append((start, len(chars)))
    terms = []
    for begin, end in spans:
        segment = chars[begin:end]
        if not segment:
            return []
        try:
            poly = _Parser(_tokens(segment)).parse()
        except _ParseFault:
            return []
        terms.append({"poly": poly, "start": segment[0][1], "end": segment[-1][1] + 1})
    return terms


class _Side:
    """等号一侧：整体一次式 + 按加减号切开的项 + 原文/规范文本。"""

    def __init__(self, chars):
        self.poly = _Parser(_tokens(chars)).parse()
        self.terms = _top_terms(chars)
        self.filtered = "".join(char for char, _ in chars)


def _parse_line(raw):
    """把一行化成 Ax+B=Cx+D；不是一元一次方程时返回 None。"""
    text = (raw or "").strip()
    chars = _scan(text)
    if not chars:
        return None
    if len({char for char, _ in chars if char in "xy"}) > 1:
        return None
    equals = [index for index, (char, _) in enumerate(chars) if char == "="]
    if len(equals) != 1:
        return None
    position = equals[0]
    left_chars = chars[:position]
    right_chars = chars[position + 1:]
    if not left_chars or not right_chars:
        return None
    try:
        left = _Side(left_chars)
        right = _Side(right_chars)
    except _ParseFault:
        return None
    return {
        "text": text,
        "left": left,
        "right": right,
        "a": left.poly[0] - right.poly[0],
        "b": left.poly[1] - right.poly[1],
        "canon": "%s=%s" % (_poly_text(left.poly), _poly_text(right.poly)),
    }


def _same_solution(first, second):
    """相邻两行解集是否相同：交叉相乘判比例，并单独处理无解、全体实数两种退化情形。"""
    first_a, first_b = first["a"], first["b"]
    second_a, second_b = second["a"], second["b"]
    if first_a == 0 and first_b == 0:
        return second_a == 0 and second_b == 0
    if second_a == 0 and second_b == 0:
        return False
    if first_a == 0:
        return second_a == 0 and second_b != 0
    if second_a == 0:
        return False
    return first_b * second_a == second_b * first_a


def _answer_only(line):
    """学生这一行是不是只有最终答案（不含任何变形过程）。"""
    chars = _scan(line["text"])
    if not chars:
        return False
    return bool(_ANSWER_ONLY.match("".join(char for char, _ in chars)))


def _bracket_form(side):
    """识别 k(a±b) 单括号形式，返回 (括号外系数, 括号内常数) 的文字。"""
    match = _BRACKET_FORM.search(side.filtered)
    if not match:
        return None
    return match.group(1), re.sub(r"\s+", "", match.group(2))


def _contrast(previous, bad, line_text):
    """给出对照，并只对差异项下划线。

    通用句式的两端逐字取自作答原文：化简结果学生并没有写，拿它当「学生这一行」等于替学生
    补写他没写的式子，评审明确禁止。单括号情形保留算术展开式（「展开后应为 3x-6」是算出来的
    结果，不是说学生写了它），其余情形一律原文对原文。
    """
    left_differs = previous["left"].poly != bad["left"].poly
    right_differs = previous["right"].poly != bad["right"].poly
    base_side = bad_side = None
    if left_differs and not right_differs:
        base_side, bad_side = previous["left"], bad["left"]
    elif right_differs and not left_differs:
        base_side, bad_side = previous["right"], bad["right"]

    span = None
    if base_side is not None:
        base_x, base_c = base_side.poly
        bad_x, bad_c = bad_side.poly
        term = None
        if bad_c != base_c:
            term = next((item for item in bad_side.terms if item["poly"][0] == 0), None)
        if term is None and bad_x != base_x:
            term = next((item for item in bad_side.terms if item["poly"][0] != 0), None)
        if term is not None:
            span = (term["start"], term["end"])

    form = _bracket_form(base_side) if base_side is not None else None
    # 划线片段逐字取自学生原文：全角减号、空格都原样保留，不做规范化。
    verbatim = line_text[span[0]:span[1]] if span else ''
    if span and form and "(" not in bad_side.filtered:
        expected = _poly_text(base_side.poly)
        # actual 必须逐字等于 first_bad_text 里的这一段（也因此恒等于 hint_text）：
        # 界面「学生这一行」不能显示学生没写过的半角形态（契约 §2.1）。
        actual = verbatim
        note = "展开后应为 %s；括号外的 %s 也要乘以 %s。" % (expected, form[0], form[1])
    else:
        # 同形态对照：expected/actual 与 note 里的两段都逐字取自作答原文，
        # actual 因此恒等于 first_bad_text，不会出现「学生这一行是 3x=3」这种改写。
        expected = previous["text"]
        actual = bad["text"]
        if expected and actual:
            note = "这两行不等价：上一行是 %s，学生这一行是 %s。" % (expected, actual)
        else:
            note = "这两行的解集不同；具体变形请对照标准解法的对应步骤。"

    if span:
        underline = {"start": span[0], "end": span[1]}
        hint = verbatim
    else:
        underline = {"start": 0, "end": len(line_text)}
        hint = line_text
    if not hint or line_text[underline["start"]:underline["end"]] != hint:
        underline = {"start": 0, "end": len(line_text)}
        hint = line_text
    return {
        "underline": underline,
        "hint_text": hint,
        "contrast": {"expected": expected, "actual": actual, "note": note},
    }


def _result(status, source, lines):
    return {
        "status": status,
        "source": source,
        "first_bad_index": None,
        "first_bad_text": None,
        "underline": None,
        "hint_text": None,
        "contrast": {"expected": None, "actual": None, "note": ""},
        "reason_note": "",
        "manual_note": "",
        "lines": lines,
    }


def locate(question_text, standard_solution, student_steps, missing=False, missing_reason="") -> dict:
    """把题干与学生步骤逐行比较，返回契约 §2 规定的那一份结果。

    只在相邻两行解集不同时给出首个错步；正确、只有答案、缺测、超范围都不标错步，
    也不写任何「为什么错」的推断。

    standard_solution 保留在签名里（契约 §2 接口固定），但定位只比较题干与学生自己的行：
    不拿标准解法替学生补话，对照句的两端一律取自作答原文。
    """
    question = (question_text or "").strip()
    student_texts = _split_lines(student_steps)
    given = _parse_line(question) if question else None

    # 学生常把题干原样抄在第一行：并入「已知」，界面上不重复显示同一个式子。
    shown_student = list(student_texts)
    if given is not None:
        given_flat = _flat(given["text"])
        while shown_student and _flat(shown_student[0]) == given_flat:
            shown_student.pop(0)

    lines = []
    if question:
        lines.append({"text": question, "role": "given", "bad": False})
    for text in shown_student:
        lines.append({"text": text, "role": "student", "bad": False})

    if missing:
        result = _result("missing", "none", lines)
        result["reason_note"] = REASON_MISSING
        return result

    parsed = []
    broken = given is None
    if not broken:
        for text in shown_student:
            item = _parse_line(text)
            if item is None:
                broken = True
                break
            parsed.append(item)
    if broken:
        result = _result("out_of_scope", "none", lines)
        result["manual_note"] = MANUAL_OUT_OF_SCOPE
        return result

    if not parsed or all(_answer_only(item) for item in parsed):
        result = _result("missing_steps", "rule", lines)
        result["reason_note"] = REASON_MISSING_STEPS
        result["manual_note"] = MANUAL_MISSING_STEPS
        return result

    chain = [given] + parsed
    bad_index = None
    for index in range(1, len(chain)):
        if not _same_solution(chain[index - 1], chain[index]):
            bad_index = index
            break

    if bad_index is None:
        return _result("correct", "rule", lines)

    bad_text = lines[bad_index]["text"]
    lines[bad_index]["bad"] = True
    result = _result("first_bad", "rule", lines)
    result["first_bad_index"] = bad_index
    result["first_bad_text"] = bad_text
    result["reason_note"] = REASON_FIRST_BAD
    contrast = _contrast(chain[bad_index - 1], chain[bad_index], bad_text)
    result.update(contrast)
    return result


__all__ = ["locate", "REASON_FIRST_BAD", "MANUAL_OUT_OF_SCOPE"]
