"""作答核对：两轴状态、队列、保存回执与追问记录（作答核对契约 §1–§3）。

为什么要和 answer_reviews 分开：answer_reviews 是模型给出的候选错因，answer_checks 是老师对
「这一份作答」的处理记录。两者混在一起会把「原题做错没有」和「老师判断原因没有」算成同一件事，
而 A3 要求这两条轴各自成立、各自统计，所以这里单独建表、单独读写。

版本口径：answer_checks.answer_version 只有等于 answers.fingerprint 时才算当前结论。重导后旧结论
自动退出队列、详情与统计——所有查询都带这个条件，不做任何清理批处理，历史记录原样留在库里。
"""

import json
import os
import re
from datetime import datetime

from . import answers as answers_mod
from . import equation_steps
from .db import DEMO_BATCH, DEMO_SOURCE, now_iso

MAX_CANDIDATES = 3

PROGRESS_PENDING = "pending"
PROGRESS_CHECKED = "checked"
PROGRESS_LABELS = {PROGRESS_PENDING: "待核对", PROGRESS_CHECKED: "已核对"}

# 原因轴三态（R1-02）：待判断 / 教师已判断 / 无需判断。
# 第三态是本次契约变更的核心：定位结论为「正确、缺测、缺少步骤」时，本题根本没有可归因的
# 错步，再记「待判断」就是永久挂账（R1-00 §15.3 实测「全部核对完但仍有 3 条处理不掉」）。
CAUSE_UNDECIDED = "undecided"
CAUSE_DECIDED = "decided"
CAUSE_NOT_APPLICABLE = "not_applicable"
CAUSE_STATES = (CAUSE_UNDECIDED, CAUSE_DECIDED, CAUSE_NOT_APPLICABLE)
CAUSE_LABELS = {
    CAUSE_UNDECIDED: "原因待判断",
    CAUSE_DECIDED: "教师已判断",
    CAUSE_NOT_APPLICABLE: "无需判断",
}

# 错因来源（R1-02「区分人工填写与候选采纳」）：
#   none   还没判断（对应 cause_state=undecided/not_applicable）
#   preset 采纳了本条作答的候选（只有合成演示记录才有候选，见 R1-01）
#   teacher 教师自己填的编码与说明（无候选也能完成判断）
#   legacy 历史行：R1-02 之前的记录没有来源字段，按历史标注而不是假装「未判断」
CAUSE_SOURCE_NONE = "none"
CAUSE_SOURCE_PRESET = "preset"
CAUSE_SOURCE_TEACHER = "teacher"
CAUSE_SOURCE_LEGACY = "legacy"
CAUSE_SOURCE_LABELS = {
    CAUSE_SOURCE_NONE: "未记录",
    CAUSE_SOURCE_PRESET: "采纳候选",
    CAUSE_SOURCE_TEACHER: "教师填写",
    CAUSE_SOURCE_LEGACY: "历史记录（来源未记录）",
}

# 归因适用性：能不能给这份作答判错因。
#   required  有首处错步 → 必须/应当判断（老师没判就是「待判断」，这是真欠账）
#   optional  超出核验范围 → 规则判不了，老师可判可不判；不判记「无需判断」，不欠账
#   forbidden 正确 / 缺测 / 缺少步骤 → 没有可归因的错步，只能记「无需判断」；提交 decided 属非法组合
APPLICABLE_REQUIRED = "required"
APPLICABLE_OPTIONAL = "optional"
APPLICABLE_FORBIDDEN = "forbidden"

# 追问来源三值（R1-06「来源区分 preset/teacher/curated」，写入规则见 `followup_origin`）：
#   preset  系统为本题给出的问句，且老师逐字使用（合成演示来自五项全匹配的预置记录；
#           真实作答来自按本题错步生成的建议问句），细分来源记在 answer_followups.source_detail；
#   teacher 老师自己写的问句（含把建议问句改过哪怕一个字）；
#   library 已审资源库里的问句（B2 预留，本轮 `_library_questions()` 恒返回空，命中才算 library）。
FOLLOWUP_SOURCES = ("preset", "teacher", "library")

# 没有可追问的错步时的唯一口径：接口与界面都说这一句，不许再回落成「你当时是怎么展开这个括号的？」
FOLLOWUP_UNAVAILABLE = "暂无适用追问"

# 建议问句的来源（与 answer_followups.source 的三值不同：这里是「建议从哪来」）：
#   preset 合成演示的预置问句 / library 已审资源库 / rule 按本条错步生成 / none 没有依据
SUGGEST_PRESET = "preset"
SUGGEST_LIBRARY = "library"
SUGGEST_RULE = "rule"
SUGGEST_NONE = "none"

# 人工核验轴（R1-05）：这条结论的证据到底是规则给的，还是老师人工给的。
VERIFY_RULE = "rule"
VERIFY_PRESET = "preset"
VERIFY_MANUAL = "manual"
VERIFY_SOURCE_LABELS = {
    "": "未记录来源",
    VERIFY_RULE: "规则定位",
    VERIFY_PRESET: "合成演示预置定位",
    VERIFY_MANUAL: "教师人工核验",
}

DEFAULT_QUESTION = "你当时是怎么展开这个括号的？"
"""**已停用为兜底常量**（R1-06，基线审查 §10 / §12）。

它现在只作为「合成演示案例 A 的预置记录里本来就写着这一句」的说明性常量，代码中没有任何
「拿它兜底」的路径：真实作答的问句一律由 `followup_suggestion()` 按本条的错步生成，
没有依据时给「暂无适用追问」（`FOLLOWUP_UNAVAILABLE`）。移项题不会再被问「展开括号」。
"""

# demo_answers 被裁掉时的兜底说明；正常路径一律用 demo_answers 里的三种说明之一。
FALLBACK_CAUSES_NOTE = "尚无已审错因库可用，保留待判断。"

# 候选与证据的适用范围：合成演示（五项全匹配）才有预置候选，真实作答一律没有。
SCOPE_SYNTHETIC = "synthetic_demo"
SCOPE_REAL = "real_answer"
CAUSES_NO_LIBRARY = "none"
CAUSES_PRESET = "preset"

# 文本上限：超限一律 400 明确拒绝，不再静默截断（R1-03「不得静默纠正」）。
TEXT_LIMITS = {
    "cause_code": 60,
    "cause_label": 120,
    "observed_step": 400,
    "teacher_note": 2000,
    "basis_note": 2000,
    "change_reason": 500,
    "followup.question": 300,
    "followup.answer_text": 4000,
}

# 单条补录/校正原始作答（R1-05）允许改的字段与上限。身份字段（学生、题号、记录编号、批次）
# 一律不许在这里改：那等于把一份作答悄悄搬到别人名下，也会绕过导入时的学生匹配与审核留痕。
CONTENT_LIMITS = {
    "question_text": 4000,
    "standard_solution": 4000,
    "student_steps": 4000,
    "unit": 40,
    "occurred_on": 40,
    "missing_reason": 200,
}
CONTENT_FIELD_LABELS = {
    "question_text": "题目内容",
    "standard_solution": "标准解法",
    "student_steps": "学生步骤",
    "unit": "单元",
    "occurred_on": "作答日期",
    "missing_reason": "缺测原因",
    "missing": "缺测",
}

# 定位结果里 T3 新增的四组结构字段的空壳：合成演示的预置定位没有这些键，
# 所有读取都必须走 `_locate_view()` 的 .get 兜底，不得因缺字段报错（T3 交付说明）。
EMPTY_STEPS = {
    "raw_line_count": 0, "copied_question_lines": 0, "step_count": 0, "process_count": 0,
    "answer_only_count": 0, "unparsed_count": 0, "has_process": False, "answer_only": False,
    "empty": True, "kind": "empty",
}
EMPTY_COMPLETION = {
    "state": "unknown", "has_result": False, "result_form": None, "final_line_index": None,
    "final_answer": None, "answer_value": None, "expected_value": None, "solution_kind": None,
    "result_matches": None, "note": "",
}
EMPTY_MANUAL = {"state": "none", "reason_code": "", "note": ""}
EMPTY_VERIFICATION = {
    "scope": "none", "total_lines": 0, "verified_indexes": [], "unverified_indexes": [],
    "checked_comparisons": 0, "failed_line_index": None, "failed_line_text": None,
    "failed_role": None, "failed_position": None, "note": "",
}


class AnswerCheckError(Exception):
    """可以直接展示给老师的错误；status/code 与接口错误体一致。"""

    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# --------------------------------------------------------------------------
# 通用辅助
# --------------------------------------------------------------------------
def _json_list(raw):
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _fault(name):
    """测试注入点：MISTAKE_FAULT 里列出的环节模拟故障；老师正常使用时不会触发。"""
    if name in (os.environ.get("MISTAKE_FAULT") or "").split(","):
        raise RuntimeError("模拟故障：%s" % name)


def _demo_module():
    """预置候选与预置定位的唯一来源是 demo_answers；导入失败时安静退化，不拖垮详情接口。"""
    try:
        from . import demo_answers
    except ImportError:
        return None
    return demo_answers


def table_ready(db):
    """只读判断新表是否已建好；桌面端打开旧库时没有这两张表，统计块要能安全退化。"""
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return "answer_checks" in names and "answer_followups" in names


def _require_tables(db):
    if not table_ready(db):
        raise AnswerCheckError(
            "当前数据目录还缺少「作答核对」的数据表，请先通过网页版重新打开一次以完成升级。",
            500,
            "schema_missing",
        )


def assignment_name(batch):
    return (batch or "").strip() or "未命名批次"


def resolve_batch(db, class_id, batch=""):
    """当前作业：优先用传入的批次；没传就取最近导入的那份**真实**作答所属批次。

    为什么要「真实优先」（R1-01 第 6 条）：旧实现只按 `imported_at` 取最新一行，
    载入合成演示数据后演示批次会抢占「当前作业」，真实作业的应交/待核对数字随之变成
    演示数字。现在：
    1) 有真实作答 → 一定返回真实批次，演示数据抢不走；
    2) 一条真实作答都没有（空班级在演示页载入合成数据）→ 才回落到演示批次，
       否则「载入演示 → 查看待核对作答」这条路走不通。
    imported_at 相同（同一秒导入）时取 id 较大者，保证任何调用顺序都得到同一个批次。
    """
    batch = (batch or "").strip()
    if batch:
        return batch
    row = db.query_one(
        "SELECT batch FROM answers WHERE class_id=? AND source<>?"
        " ORDER BY imported_at DESC, id DESC LIMIT 1",
        (class_id, DEMO_SOURCE),
    )
    if row and (row["batch"] or "").strip():
        return row["batch"]
    row = db.query_one(
        "SELECT batch FROM answers WHERE class_id=? AND source=?"
        " ORDER BY imported_at DESC, id DESC LIMIT 1",
        (class_id, DEMO_SOURCE),
    )
    return row["batch"] if row else ""


def _student_label(row):
    name = (row.get("student_name") or "").strip()
    if not name:
        name = (row.get("roster_name") or "").strip()
    return name or "学生"


def _missing(row):
    return bool(int(row.get("missing") or 0))


def _demo_batch():
    """演示批次名：以 demo_answers 为准（模块不可用时退回 db 里的同一份常量）。

    演示模块被裁掉时仍要能挡住合成数据，所以这里不返回空串。
    """
    module = _demo_module()
    return (getattr(module, "DEMO_BATCH", "") if module is not None else "") or DEMO_BATCH


def _is_demo_batch(batch):
    """这个批次是不是合成演示批次（真实作业只认非演示批次）。

    判据的单一来源在 `demo_answers.is_demo_batch`：演示数据可以分多个批次
    （答辩要演示「不同作业批次的核对与统计」），所以这里不再用「等于单个
    DEMO_BATCH」的严格相等。旧版演示模块没有这个函数时退回单批次相等判定，
    行为与升级前一致。

    刻意**不用批名前缀**判定：批名由老师在 CSV 里自由填写，靠「【合成演示】」
    前缀猜会把老师自己的真实批次误判成演示批次，从而被统计口径整批排除。
    """
    name = _demo_batch()
    batch = (batch or "")
    if not batch:
        return False
    module = _demo_module()
    checker = getattr(module, "is_demo_batch", None) if module is not None else None
    if checker is not None:
        try:
            return bool(checker(batch))
        except Exception:  # noqa: BLE001 — 演示模块出问题不能把真实批次判成演示批次
            return False
    return bool(name) and batch == name


def _source_scope(batch, alias="a"):
    """按批次区分 source 的过滤片段（R1-01 第 6 条）。

    只靠批名隔离不够：批名由老师在 CSV 的「来源批次」列自由填写，历史库里也可能留着
    旧版演示写入的行。所以每一条 check 查询都再按 `answers.source` 收一次口：

    - 演示批次：只数合成演示作答（真实作答即使撞了批名也不进演示口径）；
    - 其余批次：一律排除合成演示作答（演示数据进不了真实作业的应交/待核对/原题错误数）。

    返回 (sql 片段, 参数元组)，调用方把它接在 WHERE 后面即可。
    """
    if _is_demo_batch(batch):
        return " AND %s.source=?" % alias, (DEMO_SOURCE,)
    return " AND %s.source<>?" % alias, (DEMO_SOURCE,)


def _demo_record(answer):
    """合成演示作答的预置数据（含写死的预设定位、预置候选与预置问句）。

    单一闸门在 `demo_answers.match_answer`：`source + record_key + 题干 + 学生步骤 +
    版本（answers.fingerprint）` 五项全等才认这条记录。少任何一项都会让旧演示结果冒充
    当前作答，因此定位、候选、问句三条路径**都必须**走这里，不得各自按题干另开一口。
    """
    module = _demo_module()
    matcher = getattr(module, "match_answer", None) if module is not None else None
    if matcher is None:
        # 缺少这版演示模块（被裁掉或是旧版）：预置数据一律不认，由规则引擎接管。
        return None
    try:
        return matcher(answer)
    except Exception:  # noqa: BLE001 —— 演示数据出问题不能拖垮真实作答的详情接口
        return None


def _locate(answer):
    """真实导入作答一律走规则定位；预置定位只给「合成演示」用，二者不能互相冒充。"""
    record = _demo_record(answer)
    if record and isinstance(record.get("locate"), dict):
        return dict(record["locate"])
    return equation_steps.locate(
        answer.get("question_text") or "",
        answer.get("standard_solution") or "",
        answer.get("student_steps") or "",
        missing=_missing(answer),
        missing_reason=answer.get("missing_reason") or "",
    )


def candidates_for(answer):
    """候选错因：**只有**与当前演示内容五项全匹配的合成演示记录才有候选。

    真实作答一律返回空列表 —— 已审错因库属于 B2，本轮没有库可查；不许拿演示预置条目
    顶替（那会把演示学生的原话当成本条作答的证据）。候选由 `_demo_record` 同一道闸门
    放行，与预置定位同源同闸。
    """
    record = _demo_record(answer)
    if not record:
        return []
    return _candidate_rows(record)


def _candidate_rows(record):
    """把预置候选整理成接口口径：证据只保留确实是本条作答原文的切片。"""
    steps = (record or {}).get("student_steps") or ""
    rows = []
    for item in (record or {}).get("candidates") or ():
        row = dict(item)
        row["evidence"] = [text for text in (row.get("evidence") or ()) if text and text in steps]
        rows.append(row)
    return rows[:MAX_CANDIDATES]


def causes_scope(answer):
    """候选与证据的适用范围：合成演示作答 / 真实作答（要求 5 的「明确适用范围」）。"""
    return SCOPE_SYNTHETIC if (answer or {}).get("source") == DEMO_SOURCE else SCOPE_REAL


def _answer_evidence(answer, locate):
    """证据只取本条作答原文切片（R1-01 第 3 条）。

    演示学生原话、别的作答的写法、模板话术一律不进来：只有在本条 `student_steps`
    里逐字存在的那一刻错步行才算证据，否则返回空列表，界面只能显示「原因待判断」。
    """
    steps = (answer or {}).get("student_steps") or ""
    text = (locate or {}).get("first_bad_text") or ""
    if not text or text not in steps:
        return []
    span = (locate or {}).get("underline") or {}
    start, end = span.get("start"), span.get("end")
    slice_text = ""
    if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text):
        slice_text = text[start:end]
    return [{
        "field": "student_steps",
        "line": text,
        "slice": slice_text,
        "range": {"start": start, "end": end} if slice_text else None,
    }]


def causes_note(answer=None):
    """候选来源说明（契约 §3.2）：

    - 合成演示记录：说明候选来自本条演示作答的预置条目，且演示内容改动后失效；
    - 演示作答但内容已改：说明预置已失效、怎么重演；
    - 真实作答：说明未接已审错因库、系统不会套用演示条目、可人工判断；
    - 演示模块整个不可用：兜底说明。
    调用方不传 answer 时（兼容旧签名）返回演示说明或兜底句。
    """
    module = _demo_module()
    if module is None:
        return FALLBACK_CAUSES_NOTE
    note_for = getattr(module, "causes_note_for", None)
    if answer is None or note_for is None:
        return getattr(module, "CAUSES_NOTE", "") or FALLBACK_CAUSES_NOTE
    return note_for(answer) or getattr(module, "CAUSES_NOTE", "") or FALLBACK_CAUSES_NOTE


def _default_question(answer):
    """旧名字的兼容壳：等价于「这条作答的建议问句」，没有依据时是空串（不是写死的那句）。"""
    return followup_suggestion(answer)["question"]


# --------------------------------------------------------------------------
# 追问建议（R1-06）：问句必须由本条的错步/错因决定，没有依据就说「暂无适用追问」
# --------------------------------------------------------------------------
# 常量 DEFAULT_QUESTION 保留给「合成演示里本来就写着这句」的场景（案例 A 的预置记录），
# 它**不再**是任何真实作答的兜底问句：真实作答的问句一律由 `followup_suggestion` 生成。
_SIGN_TRANSLATE = {"－": "-", "−": "-", "–": "-", "—": "-", "＋": "+", "＝": "=",
                   "（": "(", "）": ")", "　": " ", "×": "*", "＊": "*"}
_BRACKET_FRAGMENT = re.compile(r"\d*\([^()]*\)")


def _normalize_math(text):
    """只做「猜错步类型」用的规范化；展示给学生/老师的文字一律用原文。"""
    text = str(text or "")
    for source, target in _SIGN_TRANSLATE.items():
        text = text.replace(source, target)
    return re.sub(r"\s+", "", text).replace("*", "")


def _quote(text, limit=32):
    """引用学生的原文片段：超长截断并加省略号，绝不改写学生的写法。"""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _split_sides(text):
    if text.count("=") != 1:
        return None
    left, right = text.split("=")
    return left, right


def _terms_of(side):
    """把等号一侧拆成带符号的项（只用于「是不是移项」的判断，不参与任何落库文字）。"""
    side = side.strip()
    if not side:
        return []
    items = re.findall(r"[+-]?[^+-]+", side.lstrip("+")) or []
    signed = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        signed.append(item if item[0] in "+-" else "+" + item)
    return signed


def _moved_terms(previous, bad):
    """找出「上一行在某一边、这一行整项出现在另一边」的项（移项类错步的关键证据）。

    只看原始项文本：判据是同一项换了边且写法没变（真实的移项要求变号，学生漏了变号才会
    出现「同一项原样换边」）。判断不了就返回空列表，不硬猜。
    """
    prev_sides = _split_sides(previous)
    bad_sides = _split_sides(bad)
    if not prev_sides or not bad_sides:
        return []
    prev_left, prev_right = set(_terms_of(prev_sides[0])), set(_terms_of(prev_sides[1]))
    bad_left, bad_right = set(_terms_of(bad_sides[0])), set(_terms_of(bad_sides[1]))
    moved = []
    for term in sorted(prev_left - bad_left):
        if term in bad_right:
            moved.append(term)
    for term in sorted(prev_right - bad_right):
        if term in bad_left:
            moved.append(term)
    return moved


def _error_kind(previous_text, bad_text):
    """按「本条作答自己的两行」判断错步类型：括号没展开 / 移项没变号 / 其它变形。"""
    previous = _normalize_math(previous_text)
    bad = _normalize_math(bad_text)
    if previous and "(" in previous and "(" not in bad:
        match = _BRACKET_FRAGMENT.search(previous)
        return "bracket_expand", {"bracket": match.group(0) if match else previous}
    moved = _moved_terms(previous, bad)
    if moved:
        return "term_move", {"term": moved[0]}
    return "rewrite", {}


def _previous_line(view):
    """首处错步的上一行（可能是题干行）；拿不到就返回空串。"""
    index = view.get("first_bad_index")
    lines = view.get("lines") or []
    if isinstance(index, int) and 0 < index < len(lines):
        return (lines[index - 1] or {}).get("text") or ""
    return ""


def _library_questions(answer):
    """已审资源库（B2）里的追问资源：本轮没有库，恒返回空列表。

    为什么留这个函数：R1-06 要求来源分出 `library` 一档，但已审资源库属于 B2，
    本轮不能编一份假库。这里返回空列表，`followup_origin()` 仍然按「逐字命中才算 library」
    实现——B2 接进来时只要让这个函数返回 [{id, question, note}]，
    分类、失效、来源标注三条链路都不用改。
    """
    return []


def _suggestion(question, source, source_detail, note, evidence=None):
    question = (question or "").strip()
    resolved = source if question else SUGGEST_NONE
    return {
        "available": bool(question),
        "question": question,
        "source": resolved,
        "source_detail": source_detail if question else "none",
        "source_label": {"preset": "预置问句", "library": "已审资源库", "rule": "按本条错步生成",
                         "none": FOLLOWUP_UNAVAILABLE}.get(resolved, ""),
        "note": note or "",
        "evidence": evidence or {},
    }


def _unavailable_note(status):
    """没有可追问的错步时说清楚为什么（接口与界面同一句，R1-06 第 3 条）。"""
    if status == "correct":
        return "这份作答全程等价，没有错步可问：%s。" % FOLLOWUP_UNAVAILABLE
    if status == "missing":
        return "这份标为缺测，没有可追问的过程：%s。" % FOLLOWUP_UNAVAILABLE
    if status == "missing_steps":
        return ("这份缺少可核对的过程（只写了结果或过程没解完），系统不猜错在哪一行："
                "先补录或校正学生过程再核对，%s。" % FOLLOWUP_UNAVAILABLE)
    if status == "out_of_scope":
        return ("这份超出规则核验范围，规则没有给出错步：先人工记录观察到的错误步骤并写下依据，"
                "%s。" % FOLLOWUP_UNAVAILABLE)
    return "规则没有定位到可追问的错步：%s。" % FOLLOWUP_UNAVAILABLE


def followup_suggestion(answer, locate=None):
    """这条作答「可以怎么问学生」：只由本条作答的错步与错因决定，没有依据就明确暂无。

    优先级（同时决定了 answer_followups.source 的三值）：
    1) 合成演示记录里逐字写着问句（五项全匹配才认，R1-01 闸门）→ preset / demo_record；
    2) 已审资源库命中 → library（B2 预留，本轮为空）；
    3) 规则定位到首处错步 → 按「括号没展开 / 移项没变号 / 其它变形」生成，preset / rule:xxx；
    4) 其余（正确 / 缺测 / 缺少步骤 / 超范围 / 没有定位）→ available=False，文案「暂无适用追问」。
    """
    view = locate if isinstance(locate, dict) and "status" in locate else _locate_view(locate or _locate(answer))
    status = view.get("status") or ""
    record = _demo_record(answer)
    if record:
        preset_question = ((record.get("followup") or {}).get("question") or "").strip()
        if preset_question and status not in ("correct", "missing"):
            return _suggestion(
                preset_question, SUGGEST_PRESET, "demo_record",
                "问句来自本条合成演示作答的预置记录（五项全匹配才生效）。",
            )
        if preset_question:
            # 正确作答 / 缺测不自动产生追问（R1-06 验收）：预置里那句仍然告诉你，但不替你问出去。
            return _suggestion(
                "", SUGGEST_NONE, "none",
                "这份作答%s，系统不自动给追问（%s）；预置记录里还写着一句「%s」，"
                "需要的话可以在下面的「线下提问」里自己填。" % (
                    "全程等价、没有错步" if status == "correct" else "标为缺测、没有过程",
                    FOLLOWUP_UNAVAILABLE, preset_question),
            )
    for item in _library_questions(answer):
        question = (item.get("question") or "").strip()
        if question:
            return _suggestion(question, SUGGEST_LIBRARY,
                               "library:%s" % (item.get("id") or "curated"),
                               item.get("note") or "问句来自已审资源库。")
    bad = (view.get("first_bad_text") or "").strip()
    if status != "first_bad" or not bad:
        return _suggestion("", SUGGEST_NONE, "none", _unavailable_note(status))
    previous = _previous_line(view)
    kind, evidence = _error_kind(previous, bad)
    if kind == "bracket_expand":
        question = "你当时是怎么展开「%s」这个括号的？" % _quote(evidence.get("bracket") or previous)
        note = "问句按本条作答的错步行生成：上一行有括号，这一行没有括号。"
    elif kind == "term_move":
        question = "从「%s」到「%s」，你把「%s」移到等号另一边时，符号变了吗？" % (
            _quote(previous), _quote(bad), _quote(evidence.get("term") or ""))
        note = "问句按本条作答的错步行生成：这一项在这一行换了边且写法没变（移项要变号）。"
    elif previous:
        question = "从「%s」到「%s」，这一步你是怎么算出来的？" % (_quote(previous), _quote(bad))
        note = "问句按本条作答的错步行生成：只引用这两行原文，不猜原因。"
    else:
        question = "「%s」这一步是怎么得来的？" % _quote(bad)
        note = "问句按本条作答的错步行生成：这条错步没有上一行可比，只引用这一行原文。"
    return _suggestion(question, SUGGEST_PRESET, "rule:%s" % kind, note,
                       {"kind": kind, "line": bad, "previous": previous,
                        "bracket": evidence.get("bracket"), "term": evidence.get("term")})


def _answer(db, class_id, answer_id):
    row = db.query_one(
        "SELECT a.*, s.name AS roster_name FROM answers a"
        " LEFT JOIN students s ON s.id = a.student_id"
        " WHERE a.id=? AND a.class_id=?",
        (answer_id, class_id),
    )
    if not row:
        raise AnswerCheckError("这条作答不存在，或不属于当前班级。", 404, "not_found")
    return row


def current_check(db, answer):
    """当前结论只认版本一致的那一条；旧版本的记录保留，但不参与任何判断。"""
    return db.query_one(
        "SELECT * FROM answer_checks WHERE answer_id=? AND answer_version=?"
        " ORDER BY id DESC LIMIT 1",
        (answer["id"], answer["fingerprint"] or ""),
    )


def _has_stale(db, answer):
    return bool(
        db.scalar(
            "SELECT COUNT(*) FROM answer_checks WHERE answer_id=? AND answer_version<>?",
            (answer["id"], answer["fingerprint"] or ""),
        )
    )


def queue_rows(db, class_id, batch):
    """待核对队列：该批次内、当前版本还没有有效 check 的作答，按 id 升序。

    总览的「待核对数」、详情的「第几份」、保存后的「下一份」都用这一份队列，
    避免三处各算一套导致数字对不上。
    批次里再按 source 收口（`_source_scope`）：真实作业的队列里不会有演示作答，
    演示批次的队列里也不会有真实作答。
    """
    scope_sql, scope_params = _source_scope(batch)
    return db.query(
        "SELECT a.id, a.batch, a.question_no, a.question_text, a.standard_solution,"
        " a.student_steps, a.missing, a.missing_reason, a.student_name, a.fingerprint,"
        " s.name AS roster_name"
        " FROM answers a LEFT JOIN students s ON s.id = a.student_id"
        " WHERE a.class_id=? AND a.batch=?" + scope_sql +
        " AND NOT EXISTS ("
        "   SELECT 1 FROM answer_checks c"
        "   WHERE c.answer_id=a.id AND c.answer_version=a.fingerprint)"
        " ORDER BY a.id ASC",
        (class_id, batch) + scope_params,
    )


def _real_answers(db, class_id):
    """本班真实作答数（排除合成演示）；班长与统计都按这个口径说话。"""
    return db.scalar(
        "SELECT COUNT(*) FROM answers WHERE class_id=? AND source<>?", (class_id, DEMO_SOURCE)
    ) or 0


def _batch_counts(db, class_id, batch):
    scope_sql, scope_params = _source_scope(batch)
    row = db.query_one(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN a.missing=1 THEN 1 ELSE 0 END), 0) AS missing,"
        " COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM answer_checks c"
        "   WHERE c.answer_id=a.id AND c.answer_version=a.fingerprint)"
        "   THEN 1 ELSE 0 END), 0) AS checked"
        " FROM answers a WHERE a.class_id=? AND a.batch=?" + scope_sql,
        (class_id, batch) + scope_params,
    ) or {"total": 0, "missing": 0, "checked": 0}
    total = int(row["total"] or 0)
    checked = int(row["checked"] or 0)
    return {
        "batch": batch,
        "name": assignment_name(batch),
        "total": total,
        "pending": total - checked,
        "checked": checked,
        "missing": int(row["missing"] or 0),
        "synthetic": _is_demo_batch(batch),
    }


# --------------------------------------------------------------------------
# 定位结果的统一视图（T3 四组结构字段的兜底）
# --------------------------------------------------------------------------
def _locate_view(locate):
    """把 `equation_steps.locate` / 预置定位统一成一份固定形状。

    T3 给定位结果加了 `steps` / `completion` / `manual` / `verification` 四组结构字段，
    但合成演示的预置定位（demo_answers）只有契约 §2 的固定键——**所有读取一律 .get 兜底**，
    缺字段时用空壳补齐，绝不因为缺少某组键而报错（也不去改 demo_answers）。
    """
    raw = locate or {}
    contrast = dict(raw.get("contrast") or {})
    contrast.setdefault("expected", None)
    contrast.setdefault("actual", None)
    contrast.setdefault("note", "")
    return {
        "status": raw.get("status") or "",
        "source": raw.get("source") or "",
        "first_bad_index": raw.get("first_bad_index"),
        "first_bad_text": raw.get("first_bad_text") or "",
        "hint_text": raw.get("hint_text") or "",
        "underline": raw.get("underline"),
        "contrast": contrast,
        "reason_note": raw.get("reason_note") or "",
        "manual_note": raw.get("manual_note") or "",
        "lines": list(raw.get("lines") or []),
        "steps": {**EMPTY_STEPS, **(raw.get("steps") or {})},
        "completion": {**EMPTY_COMPLETION, **(raw.get("completion") or {})},
        "manual": {**EMPTY_MANUAL, **(raw.get("manual") or {})},
        "verification": {**EMPTY_VERIFICATION, **(raw.get("verification") or {})},
    }


def _locate_detail(view):
    """③④⑤+核验范围的落库快照（JSON）：历史结论不随规则引擎改版而变样。"""
    return {
        "steps": view.get("steps") or {},
        "completion": view.get("completion") or {},
        "manual": view.get("manual") or {},
        "verification": view.get("verification") or {},
    }


# --------------------------------------------------------------------------
# §3.1 课前总览
# --------------------------------------------------------------------------
# 每份作答的「一句话结论」：按定位结论 + ③步骤完整性 / ④完成情况分句，
# 不再把「没有过程」「有过程但没解完」「只写了最终答案」混成同一句（R1-04 交付的
# steps.kind / completion.state 正是为这件事准备的）。
HEADLINES = {
    "first_bad": "去括号：是规则没掌握，还是这次漏写？",
    "correct": "这份作答每一步都等价，可以直接核对。",
    "missing_steps_answer_only": "这份只写了最终答案，先看缺少步骤。",
    "missing_steps_empty": "这份的步骤栏是空的，先补录过程再核对。",
    "missing_steps_unparsable": "这份写出的过程没能解析成可核对的式子，已转人工。",
    "missing_steps_unfinished": "这份有过程，但最后一行还不是结果（解题未完成）。",
    "missing": "这份标为缺测，没有可核对的过程。",
    "out_of_scope": "这份超出本次核对范围，已转人工。",
}


def locate_headline(locate):
    """一句话结论：用哪一句由定位结论与 ③/④ 共同决定（缺字段按空壳兜底）。"""
    view = _locate_view(locate)
    status = view["status"]
    if status == "first_bad":
        return HEADLINES["first_bad"]
    if status == "correct":
        return HEADLINES["correct"]
    if status == "missing":
        return HEADLINES["missing"]
    if status == "out_of_scope":
        return HEADLINES["out_of_scope"]
    if status == "missing_steps":
        kind = (view["steps"].get("kind") or "empty")
        if kind == "empty":
            return HEADLINES["missing_steps_empty"]
        if kind == "answer_only":
            return HEADLINES["missing_steps_answer_only"]
        if kind in ("unparsable", "unchecked"):
            return HEADLINES["missing_steps_unparsable"]
        if (view["completion"].get("state") or "") == "unfinished":
            return HEADLINES["missing_steps_unfinished"]
        return HEADLINES["missing_steps_answer_only"]
    return HEADLINES["out_of_scope"]


def locate_notice(locate, answer=None):
    """非 first_bad 的横幅文案（前端 `checkLocateNotice` 的同一套口径，服务端也给出）。"""
    view = _locate_view(locate)
    status = view["status"]
    if status == "first_bad":
        return ""
    if status == "correct":
        return "全程等价：从题干到学生最后一行都没有发现不等价，不标错步。"
    if status == "missing":
        return "缺测：这份作答没有提交可核验的步骤，系统不判对错、也不猜错因。"
    if status == "out_of_scope":
        return "超出本轮核验范围：已转人工，原步骤照常完整显示，不标错步。"
    if status == "missing_steps":
        kind = view["steps"].get("kind") or "empty"
        state = view["completion"].get("state") or ""
        if kind == "empty":
            return "缺少步骤：这份的步骤栏里没有可核对的过程，系统不猜错在哪一行。"
        if kind in ("unparsable", "unchecked"):
            return "缺少步骤：写出的过程没能解析成可核对的式子，系统不猜错在哪一行。"
        if kind == "answer_only":
            return "缺少步骤：学生只写了结论行，无法定位具体错在哪一行，系统不猜。"
        if state == "unfinished":
            return "解题未完成：有过程但最后一行还不是结果，系统不猜错在哪一行。"
        return "缺少步骤：这份没有可核对的过程，系统不猜错在哪一行。"
    return "定位结果没有返回，按人工核对处理：原步骤照常显示，不标错步。"


def _applicability(answer, locate):
    """这份作答能不能判错因（见 APPLICABLE_* 的说明）。"""
    status = _locate_view(locate)["status"]
    if status == "out_of_scope" and not _missing(answer):
        # 超范围 = 规则判不了，不等于不是错；老师可以人工判断（R1-05 的人工路径）。
        return APPLICABLE_OPTIONAL
    if _force_undecided(answer, status):
        return APPLICABLE_FORBIDDEN
    return APPLICABLE_REQUIRED


def overview(db, class_row, batch=""):
    class_id = class_row["id"]
    _require_tables(db)
    batch = resolve_batch(db, class_id, batch)
    # 本班真实作答数才是老师口中的「本班作答」；合成演示单独计数，不进真实分母。
    real_total = _real_answers(db, class_id)
    demo_total = db.scalar(
        "SELECT COUNT(*) FROM answers WHERE class_id=? AND source=?", (class_id, DEMO_SOURCE)
    ) or 0

    assignments = []
    for row in db.query(
        "SELECT batch, MAX(imported_at) AS last_at FROM answers WHERE class_id=? GROUP BY batch",
        (class_id,),
    ):
        item = _batch_counts(db, class_id, row["batch"])
        if not item["total"]:
            # 这一批在本口径下一条都数不到（例如只有旧版演示数据落在真实批次里）：
            # 不把它列成作业，避免真实作业表里出现 0/0/0 的幽灵行。
            continue
        item["_last_at"] = row["last_at"] or ""
        assignments.append(item)
    assignments.sort(key=lambda item: (item["_last_at"], item["batch"]), reverse=True)
    for item in assignments:
        item.pop("_last_at", None)

    assignment = _batch_counts(db, class_id, batch)
    if not (real_total or demo_total) or assignment["total"] == 0:
        # 班级还没有作答，或选中的批次里一条都没有：都按「尚未导入作答」处理，
        # 不要显示成「已核对完」，那会让老师以为数据已经处理过了。
        state = "no_answers"
    elif assignment["pending"] > 0:
        state = "has_pending"
    else:
        state = "all_checked"

    pending_rows = queue_rows(db, class_id, batch)
    next_item = None
    if pending_rows:
        first = pending_rows[0]
        located = _locate(first)
        next_item = {
            "answer_id": first["id"],
            "student_label": _student_label(first),
            "question_no": first["question_no"],
            "headline": locate_headline(located),
            "notice": locate_notice(located),
            "locate_status": _locate_view(located)["status"],
        }

    # 班级级计数只数真实作答：合成演示数据不进真实统计（R1-01 第 6 条）。
    checks_current = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint AND a.source<>?",
        (class_id, DEMO_SOURCE),
    ) or 0
    undecided = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint AND c.cause_state=?"
        " AND a.source<>?",
        (class_id, CAUSE_UNDECIDED, DEMO_SOURCE),
    ) or 0
    not_applicable = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint AND c.cause_state=?"
        " AND a.source<>?",
        (class_id, CAUSE_NOT_APPLICABLE, DEMO_SOURCE),
    ) or 0
    # R1-05：人工核验过的份数单独一档，与「规则定位出首处错步」的份数可区分、可对照。
    manual_verified = db.scalar(
        "SELECT COUNT(DISTINCT c.answer_id) FROM answer_checks c"
        " JOIN answers a ON a.id=c.answer_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint AND c.verify_source=?"
        " AND a.source<>?",
        (class_id, VERIFY_MANUAL, DEMO_SOURCE),
    ) or 0

    return {
        "state": state,
        "assignment": assignment,
        "assignments": assignments,
        "next": next_item,
        "counts": {
            "answers": real_total,
            "synthetic_answers": demo_total,
            "checks_current": checks_current,
            "undecided_cause": undecided,
            "not_applicable_cause": not_applicable,
            "manual_verified": manual_verified,
        },
    }


# --------------------------------------------------------------------------
# §3.2 单份详情
# --------------------------------------------------------------------------
def _judgement(check, stale=False, retracted=None):
    """两轴状态 + 判断依据 + 来源（保存响应与再 GET 详情共用同一份结构）。

    两条轴必须各自成立：`progress`（看过没有）与 `cause_state`（判没判原因）互不影响。
    `retracted` 是「撤回」留下的痕（谁、什么时候、为什么），撤回后本份回到待核对队列。
    """
    if not check:
        return {
            "progress": PROGRESS_PENDING,
            "cause_state": CAUSE_UNDECIDED,
            "progress_label": PROGRESS_LABELS[PROGRESS_PENDING],
            "cause_label": CAUSE_LABELS[CAUSE_UNDECIDED],
            "cause_code": "",
            "cause_label_text": "",
            "cause_source": CAUSE_SOURCE_NONE,
            "cause_source_label": CAUSE_SOURCE_LABELS[CAUSE_SOURCE_NONE],
            "observed_step": "",
            "first_bad_index": None,
            "locate_status": "",
            "locate_source": "",
            "teacher_note": "",
            "basis_note": "",
            "change_reason": "",
            "verify_source": "",
            "verify_source_label": VERIFY_SOURCE_LABELS[""],
            "teacher_error_text": "",
            "teacher_verified_at": "",
            "teacher_verified_by": None,
            "saved_at": "",
            "decided_at": "",
            "decided_by": None,
            "stale": bool(stale),
            "retracted": retracted,
        }
    cause_state = check.get("cause_state") or CAUSE_UNDECIDED
    cause_source = check.get("cause_source") or (
        CAUSE_SOURCE_LEGACY if check.get("cause_code") else CAUSE_SOURCE_NONE
    )
    verify_source = check.get("verify_source") or ""
    return {
        "progress": check.get("progress") or PROGRESS_PENDING,
        "cause_state": cause_state,
        "progress_label": PROGRESS_LABELS.get(check.get("progress"), "待核对"),
        "cause_label": CAUSE_LABELS.get(cause_state, "原因待判断"),
        "cause_code": check.get("cause_code") or "",
        "cause_label_text": check.get("cause_label") or "",
        "cause_source": cause_source,
        "cause_source_label": CAUSE_SOURCE_LABELS.get(cause_source, "未记录"),
        "observed_step": check.get("observed_step") or "",
        "first_bad_index": check.get("first_bad_index"),
        "locate_status": check.get("locate_status") or "",
        "locate_source": check.get("locate_source") or "",
        "teacher_note": check.get("teacher_note") or "",
        "basis_note": check.get("basis_note") or "",
        "change_reason": check.get("change_reason") or "",
        # R1-05：这条结论的证据是规则给的还是老师人工给的，与规则定位结果分列，统计里能区分。
        "verify_source": verify_source,
        "verify_source_label": VERIFY_SOURCE_LABELS.get(verify_source, VERIFY_SOURCE_LABELS[""]),
        "teacher_error_text": check.get("teacher_error_text") or "",
        "teacher_verified_at": check.get("teacher_verified_at") or "",
        "teacher_verified_by": check.get("teacher_verified_by"),
        "saved_at": check.get("updated_at") or "",
        "decided_at": check.get("decided_at") or "",
        "decided_by": check.get("decided_by"),
        "stale": bool(stale),
        "retracted": retracted,
    }


def _retracted_of(db, answer):
    """本份最近一次「撤回」的痕（没有则 None）。撤回行 answer_version 置空。"""
    if not table_ready(db):
        return None
    row = db.query_one(
        "SELECT * FROM answer_checks WHERE answer_id=? AND answer_version=''"
        " AND invalidated_at<>'' ORDER BY id DESC LIMIT 1",
        (answer["id"],),
    )
    if not row:
        return None
    return {
        "at": row.get("invalidated_at") or "",
        "reason": row.get("invalidated_reason") or "",
        "by": row.get("invalidated_by"),
        "previous_version": _retracted_version(row),
    }


def _retracted_version(row):
    """撤回行里记着「当时判的是哪一版」：从 cause_history 的 retract 事件里取。"""
    for event in reversed(_json_list(row.get("cause_history"))):
        if isinstance(event, dict) and event.get("action") == "retract":
            return event.get("version") or ""
    return ""


def _answer_view(answer):
    """作答身份（保存响应与详情共用）：ID、版本、批次、数据来源。"""
    synthetic = (answer.get("source") or "") == DEMO_SOURCE
    return {
        "id": answer["id"],
        "record_key": answer["record_key"],
        "batch": answer["batch"],
        "question_no": answer["question_no"],
        "question_text": answer["question_text"],
        "unit": answer["unit"],
        "standard_solution": answer["standard_solution"],
        "student_steps": answer["student_steps"],
        "occurred_on": answer["occurred_on"],
        "missing": int(answer["missing"] or 0),
        "missing_reason": answer["missing_reason"],
        "student_label": _student_label(answer),
        "data_source": "synthetic" if synthetic else "imported",
        "data_source_label": "合成演示数据" if synthetic else "真实导入",
        "version": answer["fingerprint"] or "",
    }


def _queue_state(db, answer):
    rows = queue_rows(db, answer["class_id"], answer["batch"])
    ids = [row["id"] for row in rows]
    if answer["id"] in ids:
        position = ids.index(answer["id"]) + 1
        total = len(ids)
    else:
        # 已核对过的作答仍按它离开队列时的位置显示；total 把这一份自己算回来，
        # 所以保存前后详情里的「第几份 / 共几份」不会跳。
        position = sum(1 for item in ids if item < answer["id"]) + 1
        total = len(ids) + 1
    others = [row for row in rows if row["id"] != answer["id"]]
    following = others[0] if others else None
    return {
        "position": position,
        "total": total,
        "is_last": position == total,
        "batch": answer["batch"],
        "remaining": len(others),
        "next_answer_id": following["id"] if following else None,
        "next": None if not following else {
            "answer_id": following["id"],
            "student_label": _student_label(following),
            "question_no": following["question_no"],
        },
    }


FOLLOWUP_SOURCE_LABELS = {
    "preset": "预置问句",
    "teacher": "教师自拟",
    "library": "已审资源库",
}

# 「关键追问证据」= 这条追问是拿哪几条结论问的。只要其中任何一项变了，旧的「已记录学生回答」
# 就不再是当前证据下的确认（R1-06 第 5 条），要标成待复核而不是继续冒充已确认。
EVIDENCE_FIELDS = ("answer_version", "locate_status", "locate_source", "verify_source",
                   "first_bad_index", "observed_step", "cause_state", "cause_code")


def _evidence_of(check):
    """证据快照：结论行里决定「问的是哪一步、为什么问」的那几列。"""
    row = check or {}
    return {name: row.get(name) for name in EVIDENCE_FIELDS}


def _evidence_reason(snapshot, current):
    """两套证据的差异说明（给人看的，用来解释「为什么这次要重新确认」）。"""
    reasons = []
    if not snapshot:
        reasons.append("这条追问记录早于「证据快照」，无法确认它问的仍是这一处错步")
        return reasons
    if (snapshot.get("observed_step") or "") != (current.get("observed_step") or ""):
        reasons.append("观察到的错步已从「%s」改成「%s」"
                       % (snapshot.get("observed_step") or "（空）", current.get("observed_step") or "（空）"))
    if (snapshot.get("cause_state") or "") != (current.get("cause_state") or ""):
        reasons.append("原因判断状态已变（%s → %s）"
                       % (snapshot.get("cause_state") or "空", current.get("cause_state") or "空"))
    if (snapshot.get("cause_code") or "") != (current.get("cause_code") or ""):
        reasons.append("原因编码已变（%s → %s）"
                       % (snapshot.get("cause_code") or "空", current.get("cause_code") or "空"))
    if (snapshot.get("verify_source") or "") != (current.get("verify_source") or ""):
        reasons.append("核验来源已变（%s → %s）"
                       % (snapshot.get("verify_source") or "空", current.get("verify_source") or "空"))
    if (snapshot.get("first_bad_index") or None) != (current.get("first_bad_index") or None):
        reasons.append("错步位置已变（第 %s 行 → 第 %s 行）"
                       % (snapshot.get("first_bad_index"), current.get("first_bad_index")))
    if (snapshot.get("answer_version") or "") != (current.get("answer_version") or ""):
        reasons.append("作答内容已更新（版本变化），旧版本上的回答不再对应当前作答")
    if not reasons:
        reasons.append("这条追问依据的结论已变化")
    return reasons


def _followup_history(db, answer, current_snapshot):
    """这条作答的全部追问（含失效的）：历史只读，不参与「当前证据」的判断。"""
    if not table_ready(db):
        return []
    rows = db.query(
        "SELECT * FROM answer_followups WHERE answer_id=? ORDER BY id DESC LIMIT 20",
        (answer["id"],),
    )
    items = []
    for row in rows:
        snapshot = _load_snapshot(row.get("evidence_snapshot"))
        stale = bool(snapshot) and snapshot != current_snapshot
        items.append({
            "id": row["id"],
            "version": row.get("answer_version") or "",
            "question": row.get("question") or "",
            "answer_text": row.get("answer_text") or "",
            "source": row.get("source") or "",
            "source_label": FOLLOWUP_SOURCE_LABELS.get(row.get("source") or "", ""),
            "asked_at": row.get("asked_at") or "",
            "invalidated_at": row.get("invalidated_at") or "",
            "invalidated_reason": row.get("invalidated_reason") or "",
            "current_version": (row.get("answer_version") or "") == (answer["fingerprint"] or ""),
            "needs_review": bool(stale and not (row.get("invalidated_at") or "")),
        })
    return items


def _load_snapshot(raw):
    """读一行的 evidence_snapshot；坏数据/旧行一律当「没有快照」处理，不抛异常。"""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _review_reason_text(reason):
    """把 `code：说明` 拆成给人看的说明（没有前缀就原样返回）。"""
    for code in ("evidence_changed：", "conclusion_retracted：", "answer_updated："):
        if reason.startswith(code):
            return reason[len(code):]
    return reason


def _followup_state(db, answer, locate=None, check=None):
    """追问卡的全部口径：建议问句（无依据=暂无）、已记录的原话、失效与待复核、历史。

    `recorded` 只在「当前版本 + 未被失效 + 证据快照与当前结论一致」时才为 True：
    证据一变，旧确认就不再生效（R1-06 第 5 条），而不是继续显示成已记录。
    """
    suggestion = followup_suggestion(answer, locate)
    empty = {
        "question": suggestion["question"],
        "available": suggestion["available"],
        "kind": suggestion["source_detail"],
        "note": suggestion["note"],
        "suggestion": suggestion,
        "recorded": False,
        "answer_text": "",
        "asked_at": "",
        "source": "",
        "source_label": "",
        "source_detail": "",
        "check_id": None,
        "invalidated": False,
        "invalidated_reason": "",
        "needs_review": False,
        "review_note": "",
        "history": [],
    }
    if not table_ready(db):
        return empty
    version = answer["fingerprint"] or ""
    rows = db.query(
        "SELECT * FROM answer_followups WHERE answer_id=? AND answer_version=?"
        " ORDER BY id DESC",
        (answer["id"], version),
    )
    current = _evidence_of(check if check is not None else current_check(db, answer))
    if not rows:
        # 没有当前版本的追问：旧版本上的记录仍在历史里，明确标「需重新确认」。
        history = _followup_history(db, answer, current)
        others = [item for item in history if not item["current_version"]]
        empty["history"] = history
        if others:
            empty["needs_review"] = True
            empty["review_note"] = "这份作答已更新为新的版本，旧版本上记录的学生回答不再对应当前作答，需要重新确认。"
        return empty
    valid = next((row for row in rows if not (row.get("invalidated_at") or "")), None)
    latest = rows[0]
    snapshot = _load_snapshot(valid.get("evidence_snapshot")) if valid else {}
    stale = bool(valid) and bool(snapshot) and snapshot != current
    needs_review = bool(stale) or (valid is None)
    reason = ""
    if stale:
        reason = "；".join(_evidence_reason(snapshot, current))
    elif valid is None:
        reason = _review_reason_text(latest.get("invalidated_reason") or "") or "这条追问已被失效"
    source = (latest or {}).get("source") or ""
    state = {
        "question": suggestion["question"],
        "available": suggestion["available"],
        "kind": suggestion["source_detail"],
        "note": suggestion["note"],
        "suggestion": suggestion,
        # 只有「当前版本 + 未失效 + 证据一致」才算已记录原话；失效的问句仍显示，但不冒充当前证据。
        "recorded": bool(valid and (valid.get("answer_text") or "").strip() and not stale),
        "answer_text": valid["answer_text"] if valid else "",
        "asked_at": latest.get("asked_at") or "",
        "source": source,
        "source_label": FOLLOWUP_SOURCE_LABELS.get(source, ""),
        "source_detail": (latest or {}).get("source_detail") or "",
        "check_id": latest.get("check_id"),
        "invalidated": bool(rows and not valid),
        "invalidated_reason": (latest.get("invalidated_reason") or "") if not valid else "",
        "needs_review": needs_review,
        "review_note": ("上一次记录的学生回答依据的是：%s。请重新确认后再当作当前证据。"
                        % reason) if reason else "",
        "history": _followup_history(db, answer, current),
    }
    if not state["question"] and state["recorded"]:
        # 已记录过原话：把当时实际问的那句显示出来（不是当前建议，也不是兜底常量）。
        state["question"] = latest.get("question") or ""
    return state


def detail(db, class_row, answer_id):
    """单份详情：保存响应里的每一项都能在这里原样再取到（R1-03 的「同源」要求）。"""
    _require_tables(db)
    answer = _answer(db, class_row["id"], answer_id)
    locate = _locate_view(_locate(answer))
    check = current_check(db, answer)
    record = _demo_record(answer)
    candidates = _candidate_rows(record)
    retracted = _retracted_of(db, answer) if check is None else None

    return {
        "answer": _answer_view(answer),
        "locate": locate,
        "locate_detail": _locate_detail(locate),
        "notice": locate_notice(locate, answer),
        "headline": locate_headline(locate),
        # 候选与证据的适用范围（要求 5）：只有五项全匹配的合成演示记录才可能是 preset。
        "candidates": candidates,
        "causes_source": CAUSES_PRESET if candidates else CAUSES_NO_LIBRARY,
        "causes_scope": causes_scope(answer),
        "causes_note": causes_note(answer),
        # 证据只取本条作答原文切片（要求 3）：真实作答也有证据，但那是它自己的原文，
        # 不是演示学生的原话，也不含任何通用错因描述。
        "causes_evidence": _answer_evidence(answer, locate),
        "applicability": _applicability(answer, locate),
        "judgement": _judgement(
            check, stale=_has_stale(db, answer) and check is None, retracted=retracted
        ),
        "followup": _followup_state(db, answer, locate, check),
        "queue": _queue_state(db, answer),
    }


# --------------------------------------------------------------------------
# §3.3 保存
# --------------------------------------------------------------------------
FIELD_LABELS = {
    "cause_code": "原因编码",
    "cause_label": "原因说明",
    "observed_step": "观察到的错步",
    "teacher_note": "教师备注",
    "basis_note": "判断依据",
    "change_reason": "改判/撤回理由",
    "followup.question": "追问问句",
    "followup.answer_text": "学生原话",
}


def _force_undecided(answer, status):
    """这一份有没有「可归因的错步」：没有（缺测 / 正确 / 缺少步骤 / 超范围）就返回 True。

    名字沿用旧称，语义已随 R1-02 契约变更而变：以前它表示「强制压回待判断」（结果是把
    out_of_scope / missing_steps 永久挂在待判断上，R1-00 §15.3 实测 3 条永远处理不掉）；
    现在它只回答「本题有没有可归因的错步」，**不再改写老师提交的判断**。

    T6 死代码取证：**仍然在线上**，唯一调用点是 `_applicability()`
    （`answer_check.py` 里 `if _force_undecided(answer, status): return APPLICABLE_FORBIDDEN`），
    它在「保存」的每一条路径上都会被 `_read_cause()` 通过 `applicability` 用到
    （决定这次能不能记 decided、以及要不要把「没判原因」收口成 not_applicable）。
    因此保留；曾有的别名 `_no_attributable_error` 无人调用，已在 T6 删除。
    """
    return _missing(answer) or status in ("correct", "missing_steps", "out_of_scope")


def _field_text(body, field, key=None):
    """取一个文本字段：超限一律 400 明确拒绝（R1-03 不允许静默截断）。None = 请求里没这个键。"""
    key = key or field
    if key not in body:
        return None
    raw = body.get(key)
    if raw is None:
        return ""
    text = str(raw).strip()
    limit = TEXT_LIMITS.get(field, 2000)
    if len(text) > limit:
        raise AnswerCheckError(
            "%s 超过 %d 字（现在 %d 字），请精简后再保存；本次没有写入任何记录。"
            % (FIELD_LABELS.get(field, field), limit, len(text)),
            400,
            "text_too_long",
        )
    return text


def _first_bad_index(value, locate, applicability):
    """错步位置：只对 first_bad 有意义；其余状态提交了就是非法组合（400，不静默丢弃）。"""
    view = _locate_view(locate)
    status = view["status"]
    if status != "first_bad":
        if value not in (None, ""):
            raise AnswerCheckError(
                "这份作答定位结果不是「首处不等价」，不能提交错步位置；本次没有写入任何记录。",
                400,
                "bad_index",
            )
        return None
    if value is None or value == "":
        return view["first_bad_index"]
    try:
        index = int(value)
    except (TypeError, ValueError):
        raise AnswerCheckError("错步位置不正确，请重新选择。", 400, "bad_index")
    lines = view["lines"] or []
    # 第 0 行是题干（原题），不是学生行；允许指到末行（学生最后一行）。
    if index < 1 or index >= len(lines):
        raise AnswerCheckError(
            "错步位置不正确：只能选第 1 行到第 %d 行（第 0 行是题干，不是学生写的）。"
            % max(len(lines) - 1, 1),
            400,
            "bad_index",
        )
    return index


def followup_origin(question, answer, suggestion=None):
    """追问来源三值的**写入规则**（R1-06 第 4 条）。返回 (source, source_detail)。

    - `preset`：问句逐字等于系统为**本条作答**给出的建议问句（合成演示=五项全匹配的预置记录；
      真实作答=按本条错步生成的建议句）。判据从「等于一句写死的常量」改成「等于本条的判断结果」，
      移项题因此不会被标成去括号问句，来源标签也不会失真。
    - `library`：问句逐字等于已审资源库里的追问（`_library_questions`，B2 预留，本轮恒空）。
    - `teacher`：其余一切（老师自拟，或把建议问句改过哪怕一个字）。
    """
    text = (question or "").strip()
    if not text:
        return "teacher", "teacher:empty"
    suggestion = suggestion if suggestion is not None else followup_suggestion(answer)
    if suggestion.get("available") and text == suggestion["question"]:
        return ("library" if suggestion["source"] == SUGGEST_LIBRARY else "preset"), \
            suggestion["source_detail"]
    for item in _library_questions(answer):
        if (item.get("question") or "").strip() == text:
            return "library", "library:%s" % (item.get("id") or "curated")
    return "teacher", "teacher"


def _followup_source(question, answer):
    """旧名字的兼容壳：只返回来源值（写入规则见 `followup_origin`）。"""
    return followup_origin(question, answer)[0]


def _history_snapshot(check, action, at, user_id):
    """把一条结论写成可追溯的历史事件（改判、撤回、合并都共用这个形状）。"""
    return {
        "action": action,
        "cause_state": (check.get("cause_state") or ""),
        "code": (check.get("cause_code") or ""),
        "label": (check.get("cause_label") or ""),
        "source": (check.get("cause_source") or ""),
        "note": (check.get("teacher_note") or ""),
        "basis": (check.get("basis_note") or ""),
        "change_reason": (check.get("change_reason") or ""),
        # R1-05：历史里也留住「当时观察到的错步」与「证据来自规则还是人工核验」，
        # 否则人工核验被改写后，旧结论怎么来的就查不到了。
        "observed_step": (check.get("observed_step") or ""),
        "first_bad_index": check.get("first_bad_index"),
        "verify_source": (check.get("verify_source") or ""),
        "teacher_error_text": (check.get("teacher_error_text") or ""),
        "decided_at": (check.get("decided_at") or check.get("updated_at") or ""),
        "by": user_id,
        "at": at,
    }


def save(db, class_row, user_id, answer_id, body):
    """保存一次核对：任何非预期异常都收敛成 500 save_failed，前端要能留在当前页重试。

    版本冲突、参数不合法这类「业务上说得清」的错误仍按 AnswerCheckError 原样抛出（4xx），
    绝不「改一点、回 200」——那正是 R1-03 要修的静默纠正。
    """
    try:
        return _save(db, class_row, user_id, answer_id, body)
    except AnswerCheckError:
        raise
    except Exception:
        raise AnswerCheckError(
            "保存失败，这次没有写入任何记录；本页内容还在，可以直接重试。",
            500,
            "save_failed",
        )


def _save(db, class_row, user_id, answer_id, body):
    _require_tables(db)
    answer = _answer(db, class_row["id"], answer_id)
    version = answer["fingerprint"] or ""
    expected = str(body.get("expected_version") or "")
    if expected != version:
        # 这一条必须在任何写入之前：旧版本判断不能覆盖新内容，也不应该留下半条记录。
        raise AnswerCheckError(
            "这份作答的内容已经更新，旧判断不再作为当前结论；请刷新后重新核对。",
            409,
            "version_conflict",
        )

    locate = _locate(answer)
    view = _locate_view(locate)
    applicability = _applicability(answer, view)
    candidates = _candidate_rows(_demo_record(answer))
    candidate_codes = {item.get("code") for item in candidates if item.get("code")}

    progress = str(body.get("progress") or PROGRESS_CHECKED).strip().lower()
    if progress not in (PROGRESS_PENDING, PROGRESS_CHECKED):
        raise AnswerCheckError(
            "处理进度只能是「待核对」或「已核对」。", 400, "bad_progress"
        )
    changes = _read_cause(body, view, applicability, candidate_codes)
    if progress == PROGRESS_PENDING and (
        changes["requested_state"] in (CAUSE_DECIDED, CAUSE_NOT_APPLICABLE)
        or changes["code"] or changes["label"]
    ):
        # 撤回是作废整条结论，不能顺带提交一个新判断；带了要明确拒绝，不能默默丢掉。
        raise AnswerCheckError(
            "撤回本次结论时不能同时提交原因判断（已判断/无需判断或原因编码）；"
            "要改判请按普通保存提交。本次没有写入任何记录。",
            400,
            "cause_state_mismatch",
        )
    observed_step = _field_text(body, "observed_step")
    observed_given = observed_step is not None
    first_bad_index = _first_bad_index(body.get("first_bad_index"), view, applicability)
    change_reason = _field_text(body, "change_reason") or ""
    clear_notes = bool(body.get("clear_notes"))
    followup_question = None
    followup_text = None
    raw_followup = body.get("followup")
    if isinstance(raw_followup, dict):
        followup_question = _field_text(raw_followup, "followup.question", "question")
        followup_text = _field_text(raw_followup, "followup.answer_text", "answer_text")
    suggestion = followup_suggestion(answer, view)
    if progress == PROGRESS_CHECKED and (followup_text or "").strip():
        # 「记录学生回答」必须说清问的是哪一句：没有适用问句时，老师要写下自己实际问的那句
        # （线下提问），否则这条原话就无从对应错步（R1-06 第 3、4 条）。
        if not (followup_question or "").strip():
            raise AnswerCheckError(
                "这条作答%s；要记录学生回答，请先填「实际问的那一句」（线下提问也可以）。"
                "本次没有写入任何记录。" % (
                    "" if suggestion["available"] else "没有适用追问"),
                400,
                "followup_question_required",
            )
    now = now_iso()
    # 事务外只读一次当前结论：只用于「解释」请求（是不是人工核验、判断依据够不够），
    # 真正的写入前会在事务内按版本重新读一次（version_conflict 仍然只认那一次）。
    existing_pre = current_check(db, answer)
    manual = _manual_verification(answer, view, observed_step, observed_given,
                                  changes["state"], applicability, existing_pre, clear_notes)

    try:
        with db.tx() as conn:
            answer = _answer(db, class_row["id"], answer_id)
            if (answer["fingerprint"] or "") != version:
                raise AnswerCheckError(
                    "这份作答的内容刚刚被更新，旧判断不再作为当前结论；请刷新后重新核对。",
                    409,
                    "version_conflict",
                )
            _fault("answer_check_save")
            existing = current_check(db, answer)
            latest = db.query_one(
                "SELECT * FROM answer_checks WHERE answer_id=? ORDER BY id DESC LIMIT 1",
                (answer["id"],),
            )
            if progress == PROGRESS_PENDING:
                check_id, check = _retract(
                    db, conn, answer, existing, latest, user_id, change_reason, now
                )
            else:
                # 人工核验必须留依据：老师自己给定错步/在规则判不了的作答上下判断时，
                # 「凭什么」只能是他写下来的那句依据，不写就是「绕过证据」。
                _require_manual_evidence(manual, changes, clear_notes, existing)
                check_id, check = _write_check(
                    db, conn, answer, existing, latest, view, changes, manual["observed_step"],
                    first_bad_index, change_reason, clear_notes, user_id, now, manual,
                )
                # R1-06 第 5 条：先让「依据旧证据」的确认失效，再写入/刷新本次这条。
                _invalidate_evidence_changed(db, conn, answer, check, now)
                _write_followup(
                    db, conn, answer, check_id, version, followup_question, followup_text,
                    change_reason, now, suggestion,
                )
    except AnswerCheckError:
        raise
    except Exception:
        # 保存失败必须让前端留在当前页、保留输入；这里不把底层异常原文透给老师。
        raise AnswerCheckError(
            "保存失败，这次没有写入任何记录；本页内容还在，可以直接重试。",
            500,
            "save_failed",
        )

    check = db.query_one("SELECT * FROM answer_checks WHERE id=?", (check_id,))
    action = "retracted" if progress == PROGRESS_PENDING else "saved"
    return _save_response(db, class_row, answer, check, changes["adjustments"], action)


def _manual_verification(answer, view, observed_step, observed_given, cause_state,
                         applicability, existing, clear_notes=False):
    """R1-05 的人工核验轴：这次结论的证据来自规则，还是老师人工给的？

    与规则结果**分开存**（col. verify_source / teacher_error_text）：
    - 老师明确写了「观察到的错步」，且和规则给的首处错步逐字不同 → manual；
    - 规则判不了（out_of_scope 等可判可不判的情形）却形成了原因判断 → 证据只能来自人工 → manual；
    - 合成演示记录的预置定位 → preset；其余 → rule。

    「这次没填」的两种写法都算「不改这条人工记录」（与 teacher_note/basis_note 同一口径）：
    省略字段、或传空串。要真的把人工记录撤掉，必须 `clear_notes=true`。
    """
    rule_text = (view.get("first_bad_text") or "").strip()
    previous_error = ((existing or {}).get("teacher_error_text") or "").strip()
    given_text = (observed_step or "").strip() if observed_given else ""
    if clear_notes:
        effective = ""
        observed_out = rule_text
    elif given_text:
        effective = given_text
        observed_out = given_text
    else:
        effective = previous_error
        observed_out = previous_error or rule_text
    manual_error = bool(effective) and effective != rule_text
    manual_decision = (cause_state == CAUSE_DECIDED and applicability != APPLICABLE_REQUIRED)
    if manual_error or manual_decision:
        verify_source = VERIFY_MANUAL
    elif _demo_record(answer) is not None:
        verify_source = VERIFY_PRESET
    else:
        verify_source = VERIFY_RULE if (view.get("status") or "") else ""
    if verify_source != VERIFY_MANUAL:
        return {"verify_source": verify_source, "teacher_error_text": "",
                "teacher_verified_at": "", "teacher_verified_by": None,
                "observed_step": observed_out,
                "manual_error": False, "manual_decision": False, "carried": False}
    carried = bool(previous_error) and effective == previous_error
    return {
        "verify_source": VERIFY_MANUAL,
        "teacher_error_text": effective,
        "teacher_verified_at": (existing or {}).get("teacher_verified_at") if carried else "",
        "teacher_verified_by": (existing or {}).get("teacher_verified_by") if carried else None,
        "observed_step": observed_out,
        "manual_error": manual_error,
        "manual_decision": manual_decision,
        "carried": carried,
    }


def _require_manual_evidence(manual, changes, clear_notes, existing):
    """人工核验必须带判断依据（R1-05「保存核验依据、不得绕过证据」+ §3.10 的 out_of_scope 收口）。

    只拦「人工核验」这一档：规则定位出首处错步、老师照它判原因时，证据本来就是规则给的，
    不要求他额外写依据（那是 A5「保存错步，原因待判断」的快路径）。

    `out_of_scope` 的教师改判（`cause_state=decided`）在 `_manual_verification()` 里同样记为
    `verify_source=manual`（规则判不了、证据只能来自人），因此**也走这一道闸门**：
    optional + decided + 有效依据为空 → 400 `basis_required`（契约 §3.10，T11 落地）。
    之前已经写过的依据仍然算数——只在这次既没填、旧行也没有依据时才拒绝。
    """
    if manual["verify_source"] != VERIFY_MANUAL:
        return
    # 与 teacher_note 同一口径：省略或空串都表示「这次不改这条依据」，所以要看旧值。
    basis = (changes.get("basis_note") or "").strip() or (
        ((existing or {}).get("basis_note") or "").strip())
    if clear_notes:
        basis = ""
    if not basis:
        raise AnswerCheckError(
            "人工核验要留下判断依据：请填「判断依据」（例如「第 2 行把 −6 写成 −2，"
            "作业本原图上就是 −2」）。本次没有写入任何记录。",
            400,
            "basis_required",
        )


def _read_cause(body, view, applicability, candidate_codes):
    """解释请求里的原因轴：状态、来源、编码与说明，以及服务端为收口做的唯一一处调整。

    全部校验在这里完成，任何非法组合都在写库之前抛 4xx：
    - 状态取值非法 → 400 bad_cause_state
    - decided 缺编码/说明 → 400 cause_required
    - undecided/not_applicable 却带了编码或说明 → 400 cause_state_mismatch（旧实现原样落库）
    - 正确 / 缺测 / 缺少步骤 提交 decided → 400 cause_not_applicable（旧实现静默压回）
    - cause_source=preset 但编码不在候选里 → 400 cause_not_in_candidates
    """
    status = view["status"]
    adjustments = []
    requested = str(body.get("cause_state") or "").strip().lower()
    if requested and requested not in CAUSE_STATES:
        raise AnswerCheckError(
            "原因判断状态只能是「原因待判断」「教师已判断」或「无需判断」。",
            400,
            "bad_cause_state",
        )
    cause_code = _field_text(body, "cause_code") or ""
    cause_label = _field_text(body, "cause_label") or ""
    has_cause_text = bool(cause_code or cause_label)
    requested_source = str(body.get("cause_source") or "").strip().lower()
    if requested_source and requested_source not in (CAUSE_SOURCE_PRESET, CAUSE_SOURCE_TEACHER):
        raise AnswerCheckError(
            "错因来源只能是「采纳候选」或「教师填写」。", 400, "bad_cause_source"
        )
    if not requested:
        requested = CAUSE_DECIDED if cause_label else CAUSE_UNDECIDED
    if requested == CAUSE_DECIDED:
        # 教师人工判断只要求「原因说明」：真实作答没有候选，老师也不一定知道编码，
        # 硬要求编码等于逼人现编一个假编码。采纳候选则必须落在本条候选里（下面校验）。
        if not cause_label:
            raise AnswerCheckError(
                "选了「教师已判断」就要填原因说明（编码可留空）。", 400, "cause_required"
            )
        if requested_source == CAUSE_SOURCE_PRESET and not cause_code:
            raise AnswerCheckError(
                "标记为「采纳候选」时必须给出候选编码。", 400, "cause_required"
            )
        if applicability == APPLICABLE_FORBIDDEN:
            raise AnswerCheckError(
                "这份作答没有可归因的错步（正确 / 缺测 / 缺少步骤），不能记为「教师已判断」；"
                "它只能记「无需判断」，或者先补录过程再核对。本次没有写入任何记录。",
                400,
                "cause_not_applicable",
            )
    else:
        if has_cause_text:
            raise AnswerCheckError(
                "原因状态是「%s」时不能同时填原因编码或说明；要判原因请选「教师已判断」。"
                "本次没有写入任何记录。" % CAUSE_LABELS.get(requested, requested),
                400,
                "cause_state_mismatch",
            )

    # 收口：老师没判原因，而这本题又没有「待判断」的欠账（无需判断 / 判不了但也没判），
    # 就记 not_applicable —— 这是唯一一处服务端替老师补状态，且必须回给前端一条 adjustment。
    state = requested
    if requested == CAUSE_UNDECIDED and applicability in (APPLICABLE_FORBIDDEN, APPLICABLE_OPTIONAL):
        state = CAUSE_NOT_APPLICABLE
        adjustments.append({
            "field": "cause_state",
            "from": requested,
            "to": state,
            "reason": "本题没有可归因的错步（或超出核验范围而老师未判断），记「无需判断」，"
                      "不再挂在「原因待判断」上。",
        })

    source = CAUSE_SOURCE_NONE
    if state == CAUSE_DECIDED:
        if requested_source == CAUSE_SOURCE_PRESET:
            if cause_code not in candidate_codes:
                raise AnswerCheckError(
                    "这条原因不是本份作答的候选，不能标成「采纳候选」；请改成「教师填写」。"
                    "本次没有写入任何记录。",
                    400,
                    "cause_not_in_candidates",
                )
            source = CAUSE_SOURCE_PRESET
        elif requested_source == CAUSE_SOURCE_TEACHER:
            source = CAUSE_SOURCE_TEACHER
        else:
            source = CAUSE_SOURCE_PRESET if cause_code and cause_code in candidate_codes else CAUSE_SOURCE_TEACHER
    return {
        "state": state,
        "requested_state": requested,
        "code": cause_code if state == CAUSE_DECIDED else "",
        "label": cause_label if state == CAUSE_DECIDED else "",
        "source": source,
        "teacher_note": _field_text(body, "teacher_note"),
        "basis_note": _field_text(body, "basis_note"),
        "adjustments": adjustments,
        "status": status,
    }


def _merged_history(latest, now, user_id, changes, existing):
    """历史事件：改判（旧结论被替换）、说明/依据被改写、撤回都留痕，旧内容一字不丢。

    只有「上一条确实有内容」时才记：把空的「原因待判断」换成「教师已判断」是第一次判断，
    不是改判，不该在历史里留一条空事件（13.7.3 的语义）。
    """
    history = _json_list((latest or {}).get("cause_history"))
    if existing:
        previous_note = existing.get("teacher_note") or ""
        previous_basis = existing.get("basis_note") or ""
        next_note = changes["teacher_note"] if changes["teacher_note"] is not None else previous_note
        next_basis = changes["basis_note"] if changes["basis_note"] is not None else previous_basis
        changed = (
            (existing.get("cause_code") or "") != changes["code"]
            or (existing.get("cause_label") or "") != changes["label"]
            or (existing.get("cause_state") or "") != changes["state"]
            or (existing.get("cause_source") or "") != changes["source"]
            or previous_note != next_note
            or previous_basis != next_basis
        )
        has_content = bool(
            (existing.get("cause_code") or "")
            or (existing.get("cause_label") or "")
            or (existing.get("cause_state") or "") in (CAUSE_DECIDED, CAUSE_NOT_APPLICABLE)
            or previous_note
            or previous_basis
        )
        if changed and has_content:
            history.append(_history_snapshot(existing, "revise", now, user_id))
    return history


def _write_check(db, conn, answer, existing, latest, view, changes, observed_step,
                 first_bad_index, change_reason, clear_notes, user_id, now, manual=None):
    """写入（或更新）当前版本的那一条结论；返回 (check_id, check_row)。"""
    manual = manual or {"verify_source": "", "teacher_error_text": "",
                        "teacher_verified_at": "", "teacher_verified_by": None}
    history = _merged_history(latest, now, user_id, changes, existing)
    history_text = json.dumps(history, ensure_ascii=False)
    decided = changes["state"] == CAUSE_DECIDED
    decided_at = (existing.get("decided_at") or now) if (existing and decided) else (now if decided else "")
    teacher_note = changes["teacher_note"]
    basis_note = changes["basis_note"]
    if clear_notes:
        teacher_note = ""
        basis_note = ""
    if existing:
        # 省略（None）与空串都表示「这次不动这条备注」——绝不因为没填就静默清空（R1-02 契约 §3.3）。
        # R1-05 补：空串也要按「不改」处理。旧代码只判了 None，界面每次都把（可能为空的）
        # 判断依据原样回传，于是「只改错步」的一次保存会把上一次写下的核验依据清空——
        # 人工核验的证据就是这么丢的（本文件 §3.3.1 的「不得绕过证据」要求它必须留住）。
        if not (teacher_note or "").strip():
            teacher_note = existing.get("teacher_note") or ""
        if not (basis_note or "").strip():
            basis_note = existing.get("basis_note") or ""
    else:
        teacher_note = teacher_note or ""
        basis_note = basis_note or ""
    detail_text = json.dumps(_locate_detail(view), ensure_ascii=False)
    decided_by = user_id if decided else (existing.get("decided_by") if existing else None)
    manual_source = manual.get("verify_source") or ""
    verified_at = manual.get("teacher_verified_at") or ""
    verified_by = manual.get("teacher_verified_by")
    if manual_source == VERIFY_MANUAL and not verified_at:
        # 本次（或第一次）人工核验：记谁在什么时候核验的；沿用旧文字时保留原来的痕迹。
        verified_at = now
        verified_by = user_id
    # 列 → 值用字典写，INSERT/UPDATE 共用同一份，避免两处列序漂移（上一版就是这么错的）。
    row = {
        "progress": PROGRESS_CHECKED,
        "cause_state": changes["state"],
        "cause_source": changes["source"],
        "observed_step": observed_step,
        "first_bad_index": first_bad_index,
        "locate_status": view["status"],
        "locate_source": view["source"],
        "contrast_note": view["contrast"].get("note") or "",
        "completion_state": view["completion"].get("state") or "",
        "failed_line_index": view["verification"].get("failed_line_index"),
        "verification_scope": view["verification"].get("scope") or "",
        "manual_state": view["manual"].get("state") or "",
        "manual_reason": view["manual"].get("reason_code") or "",
        "locate_detail": detail_text,
        "cause_code": changes["code"],
        "cause_label": changes["label"],
        "cause_history": history_text,
        "teacher_note": teacher_note,
        "basis_note": basis_note,
        # R1-05：人工核验轴（与规则结果分列）。
        "verify_source": manual_source,
        "teacher_error_text": manual.get("teacher_error_text") or "",
        "teacher_verified_at": verified_at,
        "teacher_verified_by": verified_by,
        "change_reason": change_reason,
        "updated_at": now,
        "decided_at": decided_at,
        "decided_by": decided_by,
    }
    columns = list(row.keys())
    if existing:
        assignments = ", ".join("%s=?" % name for name in columns)
        conn.execute(
            "UPDATE answer_checks SET " + assignments
            + ", invalidated_at='', invalidated_reason='', invalidated_by=NULL WHERE id=?",
            tuple(row[name] for name in columns) + (existing["id"],),
        )
        check_id = existing["id"]
    else:
        head = ["class_id", "student_id", "answer_id", "batch", "answer_version"]
        head_values = [answer["class_id"], answer["student_id"], answer["id"],
                       answer["batch"], answer["fingerprint"] or ""]
        tail = ["created_by", "created_at"]
        tail_values = [user_id, now]
        all_columns = head + columns + tail
        conn.execute(
            "INSERT INTO answer_checks(%s) VALUES (%s)"
            % (", ".join(all_columns), ", ".join("?" * len(all_columns))),
            tuple(head_values + [row[name] for name in columns] + tail_values),
        )
        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return check_id, db.query_one("SELECT * FROM answer_checks WHERE id=?", (check_id,))


def _retract(db, conn, answer, existing, latest, user_id, reason, now):
    """撤回：把当前版本的结论作废（answer_version 置空），本份回到待核对队列。

    为什么用「版本置空」而不是加一列布尔：队列、总览、记录、统计（含 stats.py 那侧）
    一律以 `answer_version == answers.fingerprint` 判定当前结论，置空后这一行自动退出
    全部当前视图，四条口径天然一致；行本身、成果与 cause_history 全部留着，可追溯。
    """
    if not existing:
        raise AnswerCheckError(
            "这份作答当前没有可撤回的结论。", 400, "nothing_to_retract"
        )
    history = _json_list(latest.get("cause_history") if latest else existing.get("cause_history"))
    history.append(_history_snapshot(existing, "retract", now, user_id))
    history[-1]["version"] = existing.get("answer_version") or ""
    conn.execute(
        "UPDATE answer_checks SET answer_version='', progress=?, cause_state=?, cause_source=?,"
        " cause_code='', cause_label='', observed_step='', first_bad_index=NULL,"
        " cause_history=?, change_reason=?, updated_at=?, decided_at='', decided_by=NULL,"
        " verify_source='', teacher_error_text='', teacher_verified_at='', teacher_verified_by=NULL,"
        " invalidated_at=?, invalidated_reason=?, invalidated_by=? WHERE id=?",
        (
            PROGRESS_PENDING, CAUSE_UNDECIDED, CAUSE_SOURCE_NONE, json.dumps(history, ensure_ascii=False),
            reason, now, now, reason, user_id, existing["id"],
        ),
    )
    # 结论作废 ⇒ 建立在它上面的追问确认同时失效（R1-06 第 5 条），原话仍留在历史里。
    _invalidate_followups(db, conn, answer["id"], "conclusion_retracted", now,
                          reason="结论已撤回，这条追问依据的错步与原因不再成立")
    return existing["id"], db.query_one("SELECT * FROM answer_checks WHERE id=?", (existing["id"],))


def _invalidate_followups(db, conn, answer_id, code, now, reason="", version=None):
    """把这条作答上「还挂在当前证据上」的追问确认标记失效；行与原文都留着。"""
    if not table_ready(db):
        return 0
    sql = ("SELECT id FROM answer_followups WHERE answer_id=? AND invalidated_at=''")
    params = [answer_id]
    if version is not None:
        sql += " AND answer_version=?"
        params.append(version)
    ids = [row["id"] for row in db.query(sql, params)]
    for followup_id in ids:
        conn.execute(
            "UPDATE answer_followups SET invalidated_at=?, invalidated_reason=?, updated_at=?"
            " WHERE id=?",
            (now, "%s：%s" % (code, reason) if reason else code, now, followup_id),
        )
    return len(ids)


def _invalidate_evidence_changed(db, conn, answer, check, now):
    """R1-06 第 5 条：关键追问证据一变，依据旧证据的「已记录学生回答」立即失效（待复核）。

    判据是 `answer_followups.evidence_snapshot` 与刚写入的这条结论的证据快照是否一致：
    不一致就标 invalidated_at + 原因，原话一字不删；下一次详情里它只作为「历史」与「需重新确认」出现。
    """
    if not table_ready(db):
        return 0
    current = _evidence_of(check)
    rows = db.query(
        "SELECT * FROM answer_followups WHERE answer_id=? AND answer_version=?"
        " AND invalidated_at=''",
        (answer["id"], answer["fingerprint"] or ""),
    )
    changed = 0
    for row in rows:
        snapshot = _load_snapshot(row.get("evidence_snapshot"))
        if snapshot and snapshot == current:
            continue
        note = "；".join(_evidence_reason(snapshot, current))
        conn.execute(
            "UPDATE answer_followups SET invalidated_at=?, invalidated_reason=?, updated_at=?"
            " WHERE id=?",
            (now, "evidence_changed：%s" % note, now, row["id"]),
        )
        changed += 1
    return changed


def _write_followup(db, conn, answer, check_id, version, question, answer_text, reason, now,
                    suggestion=None):
    """当面追问原话：answer_text 为空不写（「暂时跳过」不产生空记录）。

    同问同答不重复插入，但会在新证据下「刷新」这条记录：快照换成当前结论、失效标记清掉——
    因为老师这次是在新的证据上重新确认了同一句问句与同一句原话。
    """
    if not answer_text:
        return None
    question = (question or "").strip()
    if not question:
        # 调用方（_save）已在写入前拦过；这里守住「不许写一条没有问句的空记录」。
        return None
    check = db.query_one("SELECT * FROM answer_checks WHERE id=?", (check_id,))
    snapshot = json.dumps(_evidence_of(check), ensure_ascii=False, sort_keys=True)
    latest = db.query_one(
        "SELECT * FROM answer_followups WHERE answer_id=? AND answer_version=?"
        " AND question=? AND answer_text=? ORDER BY id DESC LIMIT 1",
        (answer["id"], version, question, answer_text),
    )
    source, source_detail = followup_origin(question, answer, suggestion)
    if latest:
        conn.execute(
            "UPDATE answer_followups SET check_id=?, source=?, source_detail=?,"
            " evidence_snapshot=?, invalidated_at='', invalidated_reason='', updated_at=?"
            " WHERE id=?",
            (check_id, source, source_detail, snapshot, now, latest["id"]),
        )
        return latest["id"]
    cursor = conn.execute(
        "INSERT INTO answer_followups(class_id, student_id, answer_id, check_id,"
        " answer_version, question, source, source_detail, answer_text, asked_at, answered_at,"
        " evidence_snapshot, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            answer["class_id"], answer["student_id"], answer["id"], check_id, version,
            question, source, source_detail, answer_text, now, now, snapshot, now, now,
        ),
    )
    return cursor.lastrowid


def _save_response(db, class_row, answer, check, adjustments, action, extra=None):
    """保存响应：与 `detail()` 同源同结构（11 项），前端只许照着这里渲染。"""
    view = _locate_view(_locate(answer))
    check = check or current_check(db, answer)
    retracted = _retracted_of(db, answer) if (check is None or action == "retracted") else None
    judgement = _judgement(
        check,
        stale=_has_stale(db, answer) and (check is None or action == "retracted"),
        retracted=retracted,
    )
    candidates = _candidate_rows(_demo_record(answer))
    labels = {"retracted": "已撤回，回到待核对", "updated": "已按新内容重新分析"}
    receipt = {
        "class_name": class_row["name"],
        "assignment_name": assignment_name(answer["batch"]),
        "saved_at": (check or {}).get("updated_at") or "",
        "status_line": "%s · %s" % (judgement["progress_label"], judgement["cause_label"]),
        "action": action,
        "action_label": labels.get(action, "已保存"),
        "answer_label": "%s · 第 %s 题" % (judgement_answer_label(answer), answer["question_no"]),
        "verify_source_line": ("核验来源：%s" % judgement["verify_source_label"])
        if judgement["verify_source"] else "",
    }
    body = {
        "ok": True,
        "action": action,
        "adjustments": adjustments,
        "receipt": receipt,
        "answer": _answer_view(answer),
        "locate": view,
        "locate_detail": _locate_detail(view),
        "notice": locate_notice(view, answer),
        "headline": locate_headline(view),
        "applicability": _applicability(answer, view),
        "candidates": candidates,
        "causes_source": CAUSES_PRESET if candidates else CAUSES_NO_LIBRARY,
        "causes_scope": causes_scope(answer),
        "causes_note": causes_note(answer),
        "causes_evidence": _answer_evidence(answer, view),
        "judgement": judgement,
        "followup": _followup_state(db, answer, view, check),
        "queue": _queue_state(db, answer),
    }
    if extra:
        body.update(extra)
    return body


def judgement_answer_label(answer):
    return _student_label(answer)


# --------------------------------------------------------------------------
# §3.3.1 单条补录 / 校正原始作答（R1-05）
# --------------------------------------------------------------------------
CONTENT_TEXT_FIELDS = ("question_text", "standard_solution", "student_steps", "unit", "occurred_on")
_TRUE_WORDS = ("是", "1", "true", "yes", "y")
_FALSE_WORDS = ("否", "0", "false", "no", "n")


def _content_text(body, field):
    """取一个可校正字段：None = 这次不改；超限 400 明确拒绝（与导入时的字段上限一致）。"""
    if field not in body:
        return None
    raw = body.get(field)
    text = "" if raw is None else str(raw).strip()
    limit = CONTENT_LIMITS.get(field, 4000)
    if len(text) > limit:
        raise AnswerCheckError(
            "%s 超过 %d 字（现在 %d 字），请精简后再保存；本次没有改动任何作答。"
            % (CONTENT_FIELD_LABELS.get(field, field), limit, len(text)),
            400,
            "text_too_long",
        )
    return text


def _content_missing(body, value):
    """缺测标记：只接受「是/否」这一档明确的写法，别的一律拒绝（不猜老师的意思）。"""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return 1
    if text in _FALSE_WORDS:
        return 0
    raise AnswerCheckError("缺测只能填「是」或「否」。本次没有改动任何作答。", 400, "bad_missing")


def update_content(db, class_row, user_id, answer_id, body):
    """补录/校正单条作答的原始文字，并按新内容重新分析（R1-05 第 2 条）。

    为什么必须做接口而不是让老师整批重导：整批重导会把同批次其他作答的判断一起推成旧版本，
    而这批里只有一份需要补步骤。这里只动这一条：
    1) `expected_version` 必填（与保存同一道版本闸门），版本不符 409，一个字都不改；
    2) 只许改题面/标准解/学生步骤/单元/日期/缺测；学生、题号、记录编号、批次这些身份字段不许改，
       否则等于把一份作答悄悄搬到别人名下；
    3) 内容指纹按新文字重算 → 旧结论自动变成「旧版本」（stale），本份回到待核对队列重新分析；
    4) 依据旧内容记录的追问原话一并标记失效（`answer_updated`），原话不删。

    合成演示作答**也允许**校正（t12 要求界面上能演示「单条作答补录与校正」）：改完指纹变化，
    预置定位与候选随旧内容失效、退回规则定位，返回体带 `demo_preset_invalidated` 与说明；
    学生/批次/记录编号不动，隔离不变，「重置合成数据」可恢复基线。
    """
    _require_tables(db)
    if not isinstance(body, dict):
        raise AnswerCheckError("请求内容不完整（应为 JSON 对象）。", 400, "invalid")
    answer = _answer(db, class_row["id"], answer_id)
    # 合成演示作答允许校正（答辩要在界面上演示「单条作答补录与校正」，不能要求现场导 CSV）：
    # 演示数据是「五项全匹配」的预置基线，内容一改指纹就变，预置定位与候选自然失效，
    # 这份作答退回规则定位——这本身就是要演示的效果。source / record_key / 批次一律不动，
    # 所以证据隔离不变；「重置合成数据」可以整批恢复基线。
    is_demo_answer = (answer.get("source") or "") == DEMO_SOURCE
    version = answer["fingerprint"] or ""
    expected = str(body.get("expected_version") or "")
    if expected != version:
        raise AnswerCheckError(
            "这份作答的内容已经更新，旧判断不再作为当前结论；请刷新后重新核对。",
            409,
            "version_conflict",
        )
    changed = {}
    for field in CONTENT_TEXT_FIELDS:
        text = _content_text(body, field)
        if text is None:
            continue
        if text != (answer[field] or ""):
            changed[field] = text
    if "occurred_on" in changed and changed["occurred_on"]:
        if not answers_mod.DATE_RE.match(changed["occurred_on"]):
            raise AnswerCheckError("作答日期要写成 2026-09-01 这样的格式。本次没有改动任何作答。",
                                   400, "bad_date")
        try:
            datetime.strptime(changed["occurred_on"], "%Y-%m-%d")
        except (TypeError, ValueError):
            raise AnswerCheckError("作答日期不是有效日期，请写成 2026-09-01 这样的格式。"
                                   "本次没有改动任何作答。", 400, "bad_date")
    missing = _content_missing(body, body.get("missing")) if "missing" in body else None
    reason = _content_text(body, "missing_reason")
    if missing is not None and missing != int(answer["missing"] or 0):
        changed["missing"] = missing
    if reason is not None and reason != (answer["missing_reason"] or ""):
        changed["missing_reason"] = reason
    if not changed:
        raise AnswerCheckError(
            "这次没有改动任何内容（题面、标准解、学生步骤、日期、缺测都与当前值相同）。"
            "要改的是哪一项，请填上新的内容。",
            400,
            "nothing_to_update",
        )
    row = {
        "question_no": answer["question_no"],
        "question_text": answer["question_text"] or "",
        "unit": answer["unit"] or "",
        "standard_solution": answer["standard_solution"] or "",
        "student_steps": answer["student_steps"] or "",
        "occurred_on": answer["occurred_on"] or "",
        "missing": int(answer["missing"] or 0),
        "missing_reason": answer["missing_reason"] or "",
    }
    row.update(changed)
    steps = (row["student_steps"] or "").strip()
    if row["missing"] and steps:
        raise AnswerCheckError(
            "标成缺测，但又填写了学生步骤：缺测不能有步骤，请去掉「缺测」或删掉步骤。"
            "本次没有改动任何作答。",
            400,
            "bad_missing",
        )
    if not row["missing"] and not steps:
        raise AnswerCheckError(
            "学生步骤是空的：要么补上学生写的过程，要么把这份标为缺测（缺测=是）。"
            "本次没有改动任何作答。",
            400,
            "bad_missing",
        )
    if row["missing"] and not (row["missing_reason"] or "").strip():
        row["missing_reason"] = "未提供学生步骤"
    new_fingerprint = answers_mod.fingerprint(row, answer["student_id"])
    if new_fingerprint == version:
        raise AnswerCheckError(
            "内容没有实际变化（指纹与当前版本一致），不需要新建版本。本次没有改动任何作答。",
            400,
            "content_unchanged",
        )
    now = now_iso()
    with db.tx() as conn:
        current = _answer(db, class_row["id"], answer_id)
        if (current["fingerprint"] or "") != version:
            raise AnswerCheckError(
                "这份作答的内容刚刚被更新，旧判断不再作为当前结论；请刷新后重新核对。",
                409,
                "version_conflict",
            )
        _fault("answer_check_content")
        conn.execute(
            "UPDATE answers SET question_text=?, unit=?, standard_solution=?, student_steps=?,"
            " occurred_on=?, missing=?, missing_reason=?, fingerprint=?, updated_at=? WHERE id=?",
            (row["question_text"], row["unit"], row["standard_solution"], row["student_steps"],
             row["occurred_on"], row["missing"], row["missing_reason"], new_fingerprint, now,
             answer["id"]),
        )
        invalidated = _invalidate_followups(
            db, conn, answer["id"], "answer_updated", now,
            reason="作答内容已补录/校正，基于旧内容的追问回答不再对应当前作答",
        )
        stale_checks = db.scalar(
            "SELECT COUNT(*) FROM answer_checks WHERE answer_id=? AND answer_version<>''"
            " AND answer_version<>?",
            (answer["id"], new_fingerprint),
        ) or 0
    answer = _answer(db, class_row["id"], answer_id)
    adjustments = [{
        "field": name,
        "from": "旧内容",
        "to": "新内容",
        "reason": "已按老师提交的内容校正「%s」，作答指纹随之变化。" % CONTENT_FIELD_LABELS.get(name, name),
    } for name in changed]
    extra = {
        "previous_version": version,
        "version": answer["fingerprint"] or "",
        "changed_fields": sorted(changed.keys()),
        "invalidated_followups": invalidated,
        "stale_conclusions": stale_checks,
        "reanalysed": True,
    }
    if is_demo_answer:
        extra["demo_preset_invalidated"] = True
        extra["note"] = (
            "已校正演示作答：内容指纹已变化，预置定位与候选随旧内容失效，本份改为按当前内容"
            "规则定位；学生、批次、记录编号未改动，隔离不变；「重置合成数据」可恢复演示基线。"
        )
    return _save_response(db, class_row, answer, None, adjustments, "updated", extra)


# --------------------------------------------------------------------------
# §3.4 记录找回
# --------------------------------------------------------------------------
def records(db, class_row, batch="", cause_state=CAUSE_UNDECIDED, verify_source=""):
    """记录找回：只列当前版本的有效 check（撤回的行 answer_version 已置空，自动退出）。

    `cause_state` 支持 undecided / decided / not_applicable / all：第三态「无需判断」也要能
    找回并解释为什么没判（R1-02）。
    `verify_source` 支持 rule / preset / manual / all（R1-05）：人工核验与规则结果要在记录里
    分得开、能单独筛出来看，而不是只靠一条自由文本去猜。
    """
    _require_tables(db)
    cause_state = (cause_state or CAUSE_UNDECIDED).strip() or CAUSE_UNDECIDED
    if cause_state not in CAUSE_STATES + ("all",):
        raise AnswerCheckError(
            "原因判断状态只能是 undecided、decided、not_applicable 或 all。",
            400,
            "bad_cause_state",
        )
    verify_source = (verify_source or "").strip().lower()
    if verify_source and verify_source not in (VERIFY_RULE, VERIFY_PRESET, VERIFY_MANUAL, "all"):
        raise AnswerCheckError(
            "核验来源只能是 rule、preset、manual 或 all。", 400, "bad_verify_source"
        )
    sql = (
        "SELECT c.*, a.batch, a.question_no, a.student_name, s.name AS roster_name"
        " FROM answer_checks c JOIN answers a ON a.id=c.answer_id"
        " LEFT JOIN students s ON s.id=a.student_id"
        " WHERE c.class_id=? AND c.answer_version=a.fingerprint"
    )
    params = [class_row["id"]]
    batch = (batch or "").strip()
    if batch:
        sql += " AND a.batch=?"
        params.append(batch)
        # 指定批次时再按 source 收口：查真实批次不可能查出演示记录，反之亦然（R1-01 第 6 条）。
        scope_sql, scope_params = _source_scope(batch)
        sql += scope_sql
        params.extend(scope_params)
    if cause_state != "all":
        sql += " AND c.cause_state=?"
        params.append(cause_state)
    if verify_source and verify_source != "all":
        sql += " AND c.verify_source=?"
        params.append(verify_source)
    sql += " ORDER BY c.updated_at DESC, c.id DESC"
    items = []
    for row in db.query(sql, params):
        source = row.get("cause_source") or (
            CAUSE_SOURCE_LEGACY if row.get("cause_code") else CAUSE_SOURCE_NONE
        )
        verify = row.get("verify_source") or ""
        items.append({
            "answer_id": row["answer_id"],
            "student_label": _student_label(row),
            "question_no": row["question_no"],
            "observed_step": row["observed_step"],
            "progress": row["progress"],
            "cause_state": row["cause_state"],
            "cause_state_label": CAUSE_LABELS.get(row["cause_state"], "原因待判断"),
            "cause_code": row["cause_code"],
            "cause_label_text": row["cause_label"],
            "cause_source": source,
            "cause_source_label": CAUSE_SOURCE_LABELS.get(source, "未记录"),
            "teacher_note": row.get("teacher_note") or "",
            "basis_note": row.get("basis_note") or "",
            "locate_status": row.get("locate_status") or "",
            # R1-05：人工核验结果与规则结果分列可辨认（记录页可直接筛 verify_source=manual）。
            "verify_source": verify,
            "verify_source_label": VERIFY_SOURCE_LABELS.get(verify, VERIFY_SOURCE_LABELS[""]),
            "teacher_error_text": row.get("teacher_error_text") or "",
            "teacher_verified_at": row.get("teacher_verified_at") or "",
            "saved_at": row["updated_at"],
            "batch": row["batch"],
        })
    return {"total": len(items), "items": items}


__all__ = [
    "APPLICABLE_FORBIDDEN", "APPLICABLE_OPTIONAL", "APPLICABLE_REQUIRED",
    "AnswerCheckError", "CAUSE_DECIDED", "CAUSE_LABELS", "CAUSE_NOT_APPLICABLE",
    "CAUSE_SOURCE_LABELS", "CAUSE_SOURCE_NONE", "CAUSE_SOURCE_PRESET", "CAUSE_SOURCE_TEACHER",
    "CAUSE_STATES", "CAUSE_UNDECIDED", "CONTENT_LIMITS", "DEMO_SOURCE",
    "FOLLOWUP_SOURCES", "FOLLOWUP_SOURCE_LABELS", "FOLLOWUP_UNAVAILABLE", "HEADLINES",
    "MAX_CANDIDATES", "PROGRESS_CHECKED", "PROGRESS_LABELS", "PROGRESS_PENDING",
    "SCOPE_REAL", "SCOPE_SYNTHETIC", "SUGGEST_LIBRARY", "SUGGEST_NONE", "SUGGEST_PRESET",
    "SUGGEST_RULE", "TEXT_LIMITS", "VERIFY_MANUAL", "VERIFY_PRESET", "VERIFY_RULE",
    "VERIFY_SOURCE_LABELS",
    "assignment_name", "candidates_for", "causes_note", "causes_scope", "current_check", "detail",
    "followup_origin", "followup_suggestion",
    "locate_headline", "locate_notice", "overview", "queue_rows", "records", "resolve_batch",
    "save", "table_ready", "update_content",
]
