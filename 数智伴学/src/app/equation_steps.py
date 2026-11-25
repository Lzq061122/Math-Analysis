"""一元一次方程的确定性错步定位（作答核对契约 §2）。

为什么单独成模块：定位只回答一件事——相邻两行是不是同一个解集。它不推断学生为什么这样写，
也不调用模型，因此可以离线、可复现。原因候选属于已审错因库（B2），本轮不从这里猜。

做法：把每行解析成 Ax+B=Cx+D，A、B、C、D 都用 fractions.Fraction 记录，相邻两行解集相同
等价于 (A-C, B-D) 与 (A'-C', B'-D') 成比例；所有比较都用有理数交叉相乘，不引入浮点误差。
题型只限一元一次方程，超范围时原样返回并转人工，绝不硬判。

五个概念分开表达，互不顶替（对应返回值里的键）：

  ① 数学等价性   status（相邻两行解集是否相同）+ contrast
  ② 错步定位     first_bad_index / first_bad_text / underline / hint_text
  ③ 步骤完整性   steps{raw_line_count, copied_question_lines, step_count, process_count,
                       answer_only_count, unparsed_count, has_process, answer_only, empty,
                       kind}
  ④ 作答完成情况 completion{state, has_result, result_form, final_line_index, final_answer,
                            answer_value, expected_value, solution_kind, result_matches, note}
  ⑤ 人工处理状态 manual{state, reason_code, note}（manual_note 保留为同一句话的文本形态）

  另加 verification{scope, total_lines, verified_indexes, unverified_indexes,
                    checked_comparisons, failed_line_index, failed_line_text,
                    failed_role, failed_position, note} 记录核验范围：哪些行核验过、第几行
  解析失败、从哪一行起没有核验。**未核验的行不给任何保证**：不标错，也不当作正确。

status 仍只用契约 §2 已发布的 5 个取值（不新增取值，避免与已发布的契约文档、前端文案和
`answer_checks.locate_status` 的落库口径冲突），但每个取值现在有唯一、可复述的条件：

  missing        缺测：整份不作核对
  out_of_scope   题干或某一步解析不出一元一次方程 → 转人工，并报失败行与核验范围
  first_bad      相邻两行解集不同：报首个错步行
  missing_steps  没有观察到不等价，但也没有可判的结果：
                   · 步骤栏是空的，或只有结论行（steps.kind ∈ empty / answer_only）
                   · 各行都等价却没有走到结果（steps.has_process 且 completion.state=unfinished）
  correct        各行都等价，且末行是一个结果形态（completion.has_result）

「求解未完成」因此不再落进 correct：`2x+4=10 / 2x=6` 判 missing_steps +
completion.state=unfinished，与「只写了最终答案」由 steps.kind / reason_code 区分开来。

**standard_solution 不参与判定**：它保留在签名里只为固定契约 §2 的接口，函数体从不读取它。
定位只比较题干与学生自己的行，不拿标准解法替学生补话，对照句的两端一律取自作答原文。
同理 `missing_reason` 只是调用方带上来的上下文（缺测原因本身由 answers 表列承担），不参与判定。
"""

import re
from fractions import Fraction

# 固定说明句：这里只记录观察到的现象，不写「学生可能……」「因为学生……」式推断。
REASON_FIRST_BAD = "这里只记录观察到的不等价，不推断学生为什么这样写。"
REASON_MISSING = "这条作答标为缺测，没有学生步骤可核对；这里不标错步，也不假设原因。"
REASON_MISSING_STEPS = "学生只写了最终答案，缺少可核对的过程；这里只记录缺少步骤，不猜错在哪一行。"
REASON_EMPTY_STEPS = "步骤栏里没有可核对的过程；这里只记录缺少步骤，不猜错在哪一行。"
REASON_UNFINISHED = "各行都等价，但最后一行还不是结果，没有写出未知数的值；这里只记录未解完，不猜原因。"
REASON_OUT_OF_SCOPE_QUESTION = "题干没能解析成一元一次方程，这份不作核对。"
REASON_OUT_OF_SCOPE_STEP = "有一步没能解析成一元一次方程，它和它之后的步骤没有核验。"
MANUAL_OUT_OF_SCOPE = "已转人工"
MANUAL_MISSING_STEPS = "缺少步骤：请补录过程后再核对。"
MANUAL_UNFINISHED = "解题未完成：请补录最后一步后再核对。"

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


def _answer_only(text):
    """这一行是不是只有最终答案（不含任何变形过程）。"""
    chars = _scan(text)
    if not chars:
        return False
    return bool(_ANSWER_ONLY.match("".join(char for char, _ in chars)))


def _solution_of(poly):
    """(x 系数, 常数) → (解集类型, 唯一解)；类型只有 unique / all / none。"""
    coefficient, constant = poly
    if coefficient == 0:
        return ("all" if constant == 0 else "none"), None
    return "unique", (-constant) / coefficient


def _result_form(item):
    """末行是不是一个「作答结果」形态。

    value          x=3 / 3=x 这类把未知数单独留在一边的结论行
    identity       0=0 / x=x：恒等式，对任何实数都成立
    contradiction  1=5 / 2=3：矛盾式，对任何实数都不成立
    None           还不是结果（2x=6、3x-6=12 这类中间变形）
    """
    if _answer_only(item["text"]):
        return "value"
    if item["a"] == 0 and item["b"] == 0:
        return "identity"
    if item["a"] == 0:
        return "contradiction"
    return None


def _value_of(item):
    """结果行里未知数的值：x=v 与 v=x 都按 -b/a 取到 v。"""
    if item["a"] == 0:
        return None
    return (-item["b"]) / item["a"]


def _position(index, total):
    """失败行在一串学生行里的位置，供人工核对时按位置描述。"""
    if total <= 1:
        return "only"
    if index == 0:
        return "first"
    if index == total - 1:
        return "last"
    return "middle"


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


# --------------------------------------------------------------------------
# ③ 步骤完整性 / ④ 作答完成情况 / ⑤ 人工处理状态 / 核验范围
# --------------------------------------------------------------------------
def _steps_block(raw_count, shown_student, answer_flags, broken_line=False):
    """③ 步骤完整性：给出可用的量，不只 missing_steps 一档。

    raw_count      拆行后的原始行数（含被认定为「抄题干」而去重掉的行）
    shown_student  去重后真正展示与核对的学生行
    answer_flags   与 shown_student 等长：True=结论行，False=有变形过程的行，None=没解析
    broken_line    这一串学生行里是否有行真的解析失败过（区别于「压根没解析」，如题干就没解析出来）
    """
    step_count = len(shown_student)
    process_count = sum(1 for flag in answer_flags if flag is False)
    answer_only_count = sum(1 for flag in answer_flags if flag is True)
    unparsed_count = sum(1 for flag in answer_flags if flag is None)
    has_process = process_count > 0
    if step_count == 0:
        kind = "empty"
    elif broken_line:
        kind = "unparsable"
    elif unparsed_count and step_count == unparsed_count:
        kind = "unchecked"
    elif has_process and answer_only_count:
        kind = "process_and_answer"
    elif has_process:
        kind = "process_only"
    else:
        kind = "answer_only"
    return {
        "raw_line_count": raw_count,
        "copied_question_lines": max(raw_count - step_count, 0),
        "step_count": step_count,
        "process_count": process_count,
        "answer_only_count": answer_only_count,
        "unparsed_count": unparsed_count,
        "has_process": has_process,
        "answer_only": bool(step_count) and not has_process and not unparsed_count,
        "empty": step_count == 0,
        "kind": kind,
    }


def _empty_completion(note=""):
    return {
        "state": "unknown",
        "has_result": False,
        "result_form": None,
        "final_line_index": None,
        "final_answer": None,
        "answer_value": None,
        "expected_value": None,
        "solution_kind": None,
        "result_matches": None,
        "note": note,
    }


def _completion_block(given, parsed):
    """④ 作答完成情况：末行是不是结果，以及这个结果与题干方程的解是否一致。

    这一档只回答「作答有没有走到结果、这个结果对不对」，与 ① 的行间等价、③ 的过程完整
    分开记：只有结论行也能是「解对了但没过程」，过程齐全也能是「等价但没解完」。
    """
    block = _empty_completion()
    if given is None:
        block["note"] = "题干没有解析成一元一次方程，核对不了结果。"
        return block
    solution_kind, expected = _solution_of((given["a"], given["b"]))
    block["solution_kind"] = solution_kind
    block["expected_value"] = _fraction_text(expected) if expected is not None else None
    if not parsed:
        block["note"] = "没有可核验的学生步骤行，结果未核对。"
        return block

    terminal = parsed[-1]
    form = _result_form(terminal)
    # lines 里题干占第 0 位，学生第 k 行（0 起）落在下标 k+1。
    block["final_line_index"] = len(parsed)
    block["final_answer"] = terminal["text"]
    block["result_form"] = form
    if form is None:
        block["state"] = "unfinished"
        block["note"] = "最后一行「%s」还不是结果，没有写出未知数的值。" % terminal["text"]
        return block

    block["has_result"] = True
    value = _value_of(terminal) if form == "value" else None
    block["answer_value"] = _fraction_text(value) if value is not None else None
    if form == "value":
        matches = solution_kind == "unique" and value == expected
    elif form == "identity":
        matches = solution_kind == "all"
    else:
        matches = solution_kind == "none"
    block["result_matches"] = bool(matches)
    block["state"] = "solved" if matches else "wrong_result"

    answer = terminal["text"]
    if matches:
        if solution_kind == "unique":
            block["note"] = "最后一行「%s」与题干方程的解 x=%s 一致。" % (
                answer, _fraction_text(expected))
        elif solution_kind == "all":
            block["note"] = "题干方程是恒等式（解为全体实数），最后一行「%s」与它同解。" % answer
        else:
            block["note"] = "题干方程无解，最后一行「%s」与它同解。" % answer
    else:
        if form == "identity":
            block["note"] = "最后一行「%s」是恒等式（对任何实数都成立），与题干方程「%s」不同解。" % (
                answer, given["canon"])
        elif form == "contradiction":
            block["note"] = "最后一行「%s」是矛盾式（对任何实数都不成立），与题干方程「%s」不同解。" % (
                answer, given["canon"])
        elif solution_kind == "unique":
            block["note"] = "最后一行「%s」与题干方程的解 x=%s 不一致。" % (
                answer, _fraction_text(expected))
        elif solution_kind == "all":
            block["note"] = "题干方程是恒等式（解为全体实数），最后一行「%s」与它不同解。" % answer
        else:
            block["note"] = "题干方程无解，最后一行「%s」与它不同解。" % answer
    return block


def _manual(state, reason_code, note):
    """⑤ 人工处理状态：转人工 与 步骤不足待补录 是两种不同的语义，分开列。"""
    return {"state": state, "reason_code": reason_code, "note": note}


def _verification_block(total, verified_indexes, comparisons, note,
                        failed_index=None, failed_text=None, failed_role=None,
                        failed_position=None):
    """核验范围：核验过哪些行、哪一行解析失败、从哪一行起没有核验。"""
    verified = sorted(int(index) for index in verified_indexes)
    seen = set(verified)
    unverified = [index for index in range(total) if index not in seen]
    if not verified:
        scope = "none"
    elif len(verified) >= total:
        scope = "full"
    else:
        scope = "partial"
    return {
        "scope": scope,
        "total_lines": total,
        "verified_indexes": verified,
        "unverified_indexes": unverified,
        "checked_comparisons": comparisons,
        "failed_line_index": failed_index,
        "failed_line_text": failed_text,
        "failed_role": failed_role,
        "failed_position": failed_position,
        "note": note,
    }


def _result(status, source, lines):
    """契约 §2 的固定键原样保留；新增结构键先给空壳，由 locate 按分支填。"""
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
        "steps": _steps_block(0, [], []),
        "completion": _empty_completion(),
        "manual": _manual("none", "", ""),
        "verification": _verification_block(len(lines), [], 0, ""),
    }


def _finish(status, source, lines, steps, completion, manual, verification, reason_note=""):
    result = _result(status, source, lines)
    result["steps"] = steps
    result["completion"] = completion
    result["manual"] = manual
    # manual_note 是 manual["note"] 的文本形态，二者不许说两套话。
    result["manual_note"] = manual["note"]
    result["verification"] = verification
    result["reason_note"] = reason_note
    return result


def locate(question_text, standard_solution, student_steps, missing=False, missing_reason="") -> dict:
    """把题干与学生步骤逐行比较，返回契约 §2 规定的那一份结果。

    只在相邻两行解集不同时给出首个错步；缺测、超范围都不标错步，也不写任何「为什么错」的推断。
    返回值在契约 §2 的固定键之外，另带 steps / completion / manual / verification 四组结构字段
    （含义见模块 docstring ③④⑤ 与「核验范围」）。

    **字段分工（不许混着读）**：`status` 只表达 ① 定位结论——相邻两行是否等价、有没有错步、
    这份算不算判得了；`steps` / `completion` / `manual` / `verification` 分别表达其余四概念
    （③ 步骤完整性 / ④ 作答完成情况 / ⑤ 人工处理状态 / 核验范围）。任何一档都不能拿 `status`
    顶替，也不许把它们的结论塞回 `status`（落库列 `locate_status` 因此只记定位结论）。
    例：`status='missing_steps'` 既可能是「没有过程」（`steps.kind ∈ empty / answer_only`），
    也可能是「有过程却没解完」（`completion.state='unfinished'`），必须看补充字段才能区分；
    反过来 `completion.state='solved'` 也不等于判对（步骤不足时 `status` 仍是 `missing_steps`）。

    standard_solution **不参与判定**：它保留在签名里只为固定契约 §2 的接口，函数体从不读取它。
    定位只比较题干与学生自己的行，不拿标准解法替学生补话，对照句的两端一律取自作答原文。
    missing_reason 同理只作上下文，不参与判定。
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
        steps = _steps_block(len(student_texts), shown_student,
                             [None] * len(shown_student))
        completion = _completion_block(given, [])
        completion["note"] = "这份标为缺测，没有学生步骤可核对。"
        verification = _verification_block(
            total=len(lines), verified_indexes=[], comparisons=0,
            note="这份标为缺测：整份不作核对，不对任何一步作保证。")
        return _finish("missing", "none", lines, steps, completion,
                       _manual("none", "", ""), verification, REASON_MISSING)

    parsed = []
    broken = given is None
    failed_student_index = None
    if not broken:
        for index, text in enumerate(shown_student):
            item = _parse_line(text)
            if item is None:
                broken = True
                failed_student_index = index
                break
            parsed.append(item)

    answer_flags = []
    for index, text in enumerate(shown_student):
        answer_flags.append(_answer_only(text) if index < len(parsed) else None)
    steps = _steps_block(len(student_texts), shown_student, answer_flags,
                         broken_line=failed_student_index is not None)

    if broken:
        completion = _completion_block(given, [])
        if given is None:
            completion["note"] = "题干没有解析成一元一次方程，核对不了结果。"
            if question:
                verification = _verification_block(
                    total=len(lines), verified_indexes=[], comparisons=0,
                    failed_index=0, failed_text=question, failed_role="given",
                    note="题干（第 1 行）没能解析成一元一次方程；它之后的 %d 行没有核验，"
                         "不对它们作保证。" % (len(lines) - 1))
            else:
                verification = _verification_block(
                    total=len(lines), verified_indexes=[], comparisons=0,
                    failed_role="given",
                    note="题干是空的，解析不出一元一次方程；全部 %d 行学生步骤都没有核验，"
                         "不对它们作保证。" % len(lines))
            manual = _manual("referred", "question_unparsable", MANUAL_OUT_OF_SCOPE)
            return _finish("out_of_scope", "none", lines, steps, completion, manual,
                           verification, REASON_OUT_OF_SCOPE_QUESTION)

        index = failed_student_index
        completion["note"] = "第 %d 行（学生第 %d 行）没能解析，它和它之后的步骤没有核对，结果不作判定。" % (
            index + 2, index + 1)
        if index:
            prefix = "题干与第 1–%d 行已读入（只做解析，没有做等价比较）；" % (index + 1)
        else:
            prefix = "题干之后的第一行就解析失败；"
        verification = _verification_block(
            total=len(lines), verified_indexes=list(range(index + 1)), comparisons=0,
            failed_index=index + 1, failed_text=shown_student[index], failed_role="student",
            failed_position=_position(index, len(shown_student)),
            note=prefix + "第 %d 行「%s」解析失败，本行及之后的 %d 行没有核验，不对它们作保证。"
                 % (index + 2, shown_student[index], len(lines) - (index + 1)))
        manual = _manual("referred", "step_unparsable", MANUAL_OUT_OF_SCOPE)
        return _finish("out_of_scope", "none", lines, steps, completion, manual,
                       verification, REASON_OUT_OF_SCOPE_STEP)

    completion = _completion_block(given, parsed)

    if not parsed or all(flag for flag in answer_flags):
        if not parsed:
            if given is not None:
                verified = []
                note = "步骤栏是空的：没有可核验的学生行，0 组等价比较。"
            else:
                verified = []
                note = "步骤栏是空的：没有可核对的步骤行。"
            manual = _manual("need_steps", "empty_steps", MANUAL_MISSING_STEPS)
            reason = REASON_EMPTY_STEPS
        else:
            verified = list(range(len(lines)))
            note = "只有结论行：没有相邻两步可比，0 组等价比较；结论本身按 ④ 单独核验。"
            manual = _manual("need_steps", "answer_only", MANUAL_MISSING_STEPS)
            reason = REASON_MISSING_STEPS
        verification = _verification_block(
            total=len(lines), verified_indexes=verified, comparisons=0, note=note)
        return _finish("missing_steps", "rule", lines, steps, completion, manual,
                       verification, reason)

    chain = [given] + parsed
    bad_index = None
    for index in range(1, len(chain)):
        if not _same_solution(chain[index - 1], chain[index]):
            bad_index = index
            break

    if bad_index is None:
        comparisons = len(chain) - 1
        if completion["has_result"]:
            verification = _verification_block(
                total=len(lines), verified_indexes=list(range(len(lines))),
                comparisons=comparisons,
                note="题干到最后一行逐行核验，%d 组相邻比较都同解。" % comparisons)
            return _finish("correct", "rule", lines, steps, completion,
                           _manual("none", "", ""), verification)
        # 各行等价却没有走到结果：记「未解完」，不判 correct，也不猜错在哪一行。
        verification = _verification_block(
            total=len(lines), verified_indexes=list(range(len(lines))),
            comparisons=comparisons,
            note="题干到最后一行逐行等价（%d 组相邻比较）；末行不是结果，完成情况见 ④。" % comparisons)
        return _finish("missing_steps", "rule", lines, steps, completion,
                       _manual("need_steps", "unfinished", MANUAL_UNFINISHED),
                       verification, REASON_UNFINISHED)

    bad_text = lines[bad_index]["text"]
    lines[bad_index]["bad"] = True
    rest = len(lines) - (bad_index + 1)
    if rest:
        note = ("相邻等价只比到第 %d 行（本行是首个错步行）；它之后的 %d 行没有核验，"
                "不对它们作保证。" % (bad_index + 1, rest))
    else:
        note = "相邻等价只比到第 %d 行（本行是首个错步行，也是最后一行）。" % (bad_index + 1)
    verification = _verification_block(
        total=len(lines), verified_indexes=list(range(bad_index + 1)),
        comparisons=bad_index, note=note)
    result = _finish("first_bad", "rule", lines, steps, completion,
                     _manual("none", "", ""), verification, REASON_FIRST_BAD)
    result["first_bad_index"] = bad_index
    result["first_bad_text"] = bad_text
    contrast = _contrast(chain[bad_index - 1], chain[bad_index], bad_text)
    result.update(contrast)
    return result


__all__ = [
    "locate",
    "REASON_FIRST_BAD",
    "REASON_MISSING",
    "REASON_MISSING_STEPS",
    "REASON_EMPTY_STEPS",
    "REASON_UNFINISHED",
    "REASON_OUT_OF_SCOPE_QUESTION",
    "REASON_OUT_OF_SCOPE_STEP",
    "MANUAL_OUT_OF_SCOPE",
    "MANUAL_MISSING_STEPS",
    "MANUAL_UNFINISHED",
]
