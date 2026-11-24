"""合成演示作答数据：教师端「作答核对与追问」的演示样本（契约 §5 / 任务清单 A7）。

为什么单独一个模块：
- 演示不能现场选文件、配模型或等网络。这里的数据是写死的合成数据，
  整份文件只读常量、只写数据库，不发起任何外部调用，也不打开任何文件。
- 合成作答与真实导入作答必须能干净分开：`answers.source` 统一写成「合成演示」，
  重置时只按这个来源删，真实导入的作答一条都不动，并用 kept.imported_answers 给数字。

数学内容直接复用《数智伴学_Agent开发规格.md》§7 的案例 A（值得追问）、
案例 B（证据足够、无需追问）、案例 C（只有最终答案），另加一份正确作答作对照：
只有对比着看，老师才信得过「正确作答不标错」。

本模块只提供数据与预置定位结果，不冒充通用定位能力：
`locate.source` 固定为 `preset`，界面必须显示「预置演示 · 不代表已实现通用定位」。
真正的规则定位在 `equation_steps.py`（B2 范围）。
"""

from copy import deepcopy

from .answers import fingerprint as _fingerprint
from .db import now_iso

# 合成作答的来源标记。界面据此显示「合成演示数据」，重置据此只删自己。
DEMO_SOURCE = "合成演示"

# 演示统一挂在一个作业批次下，便于「课前总览」按作业统计待核对数。
DEMO_BATCH = "9月第2次作业"

# 若班级里没有学生，seed 会补建这几个合成学生；note 用这段标记，
# 重置时只清理「带标记且已经没有任何记录」的学生，绝不碰真实名单。
STUDENT_NOTE = "合成演示数据 · 可重置"

# 预置候选来源说明：本轮不接已审错因库（那是 B2），所以要说清候选是哪来的。
CAUSES_NOTE = "候选来自预置演示条目；已审错因库检索属于 B2，本轮未做。"

# 预置定位结果的固定免责说明，和契约 §2 第 9 条一致的句式。
REASON_NOTE = "这里只记录观察到的不等价，不推断学生为什么这样写。"

QUESTION_UNIT = "一元一次方程"


def _student(key, name):
    return {"key": key, "name": name, "note": STUDENT_NOTE}


STUDENTS = (
    _student("jia", "学生甲"),
    _student("yi", "学生乙"),
    _student("bing", "学生丙"),
    _student("ding", "学生丁"),
)


def _cand(code, label, support, rebut, pending, selected=False):
    return {
        "code": code,
        "label": label,
        "support": list(support),
        "rebut": list(rebut),
        "pending": list(pending),
        "selected": bool(selected),
    }


# 预置候选：按题目文本（归一化后）取，最多 3 条，每条都要有支持、反驳、待补三类信息。
# 只有案例 A 的这道题带着完整的三类证据；其它题目本轮不给候选，界面显示「原因待判断」。
PRESET_CAUSES = {
    "3(x-2)=12": [
        _cand(
            "E-DIST-01",
            "只把系数乘以括号里的第一项（常数项没乘）",
            [
                "学生这一行只把 3 乘给了 x，常数项停在 −2，写成 3x−2 而不是 3x−6。",
                "预置学生原话「2 只要乘 y 就好了」：同一套规则用到 2(y−4) 上，−4 也会被留在括号外。",
            ],
            [
                "这条只与观察到的写法一致；同一份作答里没有第二处漏乘，还不能排除这次是一次性漏写。",
            ],
            [
                "还没有学生独立重做 2(y−4) 或完整口述去括号规则的记录。",
            ],
            selected=True,
        ),
        _cand(
            "E-DIST-02",
            "括号外系数与括号内符号的处理偏差",
            [
                "学生原话提到括号里的 −4「不用管」，可能把符号判断和乘法分开处理。",
            ],
            [
                "本题括号外是正数 3，正系数去括号不会发生符号翻转，与这条的典型表现不符。",
                "若是符号规则偏差，−2(x−3) 一类题会稳定出错；现有记录里没有这种稳定表现。",
            ],
            [
                "需要 2(y−4)、(y−4)×2 之类的独立作答或口述作为依据。",
            ],
        ),
        _cand(
            "E-WRITE-01",
            "书写遗漏（抄写或跳步时漏写）",
            [
                "下一步 x=14/3 正好由 3x−2=12 推出，过程自洽，像是把「×(−2)」抄漏了。",
            ],
            [
                "同一份作答的题干一行 3(x−2)=12 抄写无误，没有第二处漏写可以相互印证。",
            ],
            [
                "还没有本人确认是否抄错的记录。",
            ],
        ),
    ],
    "5x-3=2x+6": [
        _cand(
            "E-TRANS-01",
            "移项没有变号",
            [
                "5x−3=2x+6 移项后应为 5x−2x=6+3，学生写成 5x−2x=6−3，常数项符号没跟着移。",
            ],
            [
                "若只是这一处笔误，后面 x=1 会与 6−3 的结果自洽；现象上仍分不清是规则错还是笔误。",
            ],
            [
                "证据足够时可当场问一句确认；本轮不追问也允许保留待判断。",
            ],
            selected=True,
        ),
    ],
}


def normalize_question(text):
    """题目文本归一化：全角转半角、去空格，用于按题匹配预置候选与预置定位。"""
    if not text:
        return ""
    table = {
        "（": "(",
        "）": ")",
        "－": "-",
        "﹣": "-",
        "−": "-",
        "–": "-",
        "×": "*",
        "＝": "=",
        "　": "",
        " ": "",
        "\t": "",
    }
    out = []
    for char in str(text):
        out.append(table.get(char, char))
    return "".join(out).strip()


def preset_causes(question_text):
    """按题目取预置候选；匹配不到返回空列表（界面显示「原因待判断」）。"""
    return [dict(item) for item in PRESET_CAUSES.get(normalize_question(question_text), [])]


def _locate_correct(lines):
    return {
        "status": "correct",
        "source": "preset",
        "first_bad_index": None,
        "first_bad_text": None,
        "underline": None,
        "hint_text": None,
        "contrast": {"expected": None, "actual": None, "note": ""},
        "reason_note": "各行解集一致，没有观察到不等价的步骤。",
        "manual_note": "",
        "lines": lines,
    }


def _locate_first_bad(lines, index, hint, expected, actual, note):
    bad_text = lines[index]["text"]
    start = bad_text.find(hint)
    if start < 0:
        underline = {"start": 0, "end": len(bad_text)}
        hint = bad_text
    else:
        underline = {"start": start, "end": start + len(hint)}
    return {
        "status": "first_bad",
        "source": "preset",
        "first_bad_index": index,
        "first_bad_text": bad_text,
        "underline": underline,
        "hint_text": hint,
        "contrast": {"expected": expected, "actual": actual, "note": note},
        "reason_note": REASON_NOTE,
        "manual_note": "",
        "lines": lines,
    }


def _locate_missing_steps(lines, note):
    return {
        "status": "missing_steps",
        "source": "preset",
        "first_bad_index": None,
        "first_bad_text": None,
        "underline": None,
        "hint_text": None,
        "contrast": {"expected": None, "actual": None, "note": ""},
        "reason_note": "学生正文只有结论行，没有中间步骤可以逐行比对。",
        "manual_note": note,
        "lines": lines,
    }


def _line(text, role, bad=False):
    return {"text": text, "role": role, "bad": bool(bad)}


# 四份合成作答。顺序就是「查看下一份」的顺序（按 id 升序）：
# 先给值得追问的案例 A，再给证据足够的案例 B，再给只有答案的案例 C，
# 最后用一份正确作答验证「正确作答不标错」。
ANSWERS = (
    {
        "case": "A",
        "record_key": "DEMO-A-01",
        "student": "jia",
        "question_no": "1",
        "question_text": "3(x-2)=12",
        "standard_solution": "3(x-2)=12；3x-6=12；3x=18；x=6",
        "student_steps": "3(x-2)=12\n3x-2=12\nx=14/3",
        "occurred_on": "2026-11-20",
        "missing": 0,
        "missing_reason": "",
        "locate": _locate_first_bad(
            [
                _line("3(x-2)=12", "given"),
                _line("3x-2=12", "student", bad=True),
                _line("x=14/3", "student"),
            ],
            1,
            "-2",
            "3x-6",
            "-2",
            "展开后应为 3x-6；括号外的 3 也要乘以 -2。",
        ),
        "causes_source": "preset",
        "causes_note": CAUSES_NOTE,
        "followup": {
            "question": "你当时是怎么展开这个括号的？",
            "preset_answer_text": "2 只要乘 y 就好了。",
            "asked_at": "",
        },
        "expects": "首处错步在 3x-2=12，划到 -2；可记录学生原话；保存后原题错误数 +1，已确认错因数不变",
    },
    {
        "case": "B",
        "record_key": "DEMO-B-01",
        "student": "yi",
        "question_no": "1",
        "question_text": "5x-3=2x+6",
        "standard_solution": "5x-3=2x+6；5x-2x=6+3；3x=9；x=3",
        "student_steps": "5x-3=2x+6\n5x-2x=6-3\nx=1",
        "occurred_on": "2026-11-20",
        "missing": 0,
        "missing_reason": "",
        # 预置值必须与 equation_steps.locate 逐字段一致（见 verify_preset_matches_rules）：
        # 对照句的两端逐字取自原文——上一行原文是 5x-3=2x+6，学生这一行是 5x-2x=6-3；
        # 这一行的不等价不是某个常量项写错，所以划线退回整行（取不到单点时用整行）。
        "locate": _locate_first_bad(
            [
                _line("5x-3=2x+6", "given"),
                _line("5x-2x=6-3", "student", bad=True),
                _line("x=1", "student"),
            ],
            1,
            "5x-2x=6-3",
            "5x-3=2x+6",
            "5x-2x=6-3",
            "这两行不等价：上一行是 5x-3=2x+6，学生这一行是 5x-2x=6-3。",
        ),
        "causes_source": "preset",
        "causes_note": CAUSES_NOTE,
        "followup": {
            "question": "这条等式是你把哪一项移到另一边了？",
            "preset_answer_text": "",
            "asked_at": "",
        },
        "expects": "证据足够，可以不追问直接保存待判断；追问链仍可用",
    },
    {
        "case": "C",
        "record_key": "DEMO-C-01",
        "student": "bing",
        "question_no": "2",
        "question_text": "4(x-1)=8",
        "standard_solution": "4(x-1)=8；x-1=2；x=3",
        "student_steps": "x=1",
        "occurred_on": "2026-11-20",
        "missing": 0,
        "missing_reason": "",
        "locate": _locate_missing_steps(
            [
                _line("4(x-1)=8", "given"),
                _line("x=1", "student"),
            ],
            "学生只写了最终答案，缺少中间步骤：这里只提示补录，不判断错在哪一行。",
        ),
        "causes_source": "none",
        "causes_note": "定位为「缺少步骤」，本轮不给候选错因，保留待判断。",
        "followup": {
            "question": "能把这道题中间的两步补上吗？",
            "preset_answer_text": "",
            "asked_at": "",
        },
        "expects": "显示「缺少步骤」，不标错步、不给错因；保存后不生成错题、不计原题错误数",
    },
    {
        "case": "对照",
        "record_key": "DEMO-OK-01",
        "student": "ding",
        "question_no": "3",
        "question_text": "2(x+3)=10",
        "standard_solution": "2(x+3)=10；2x+6=10；2x=4；x=2",
        "student_steps": "2(x+3)=10\n2x+6=10\n2x=4\nx=2",
        "occurred_on": "2026-11-20",
        "missing": 0,
        "missing_reason": "",
        "locate": _locate_correct(
            [
                _line("2(x+3)=10", "given"),
                _line("2x+6=10", "student"),
                _line("2x=4", "student"),
                _line("x=2", "student"),
            ]
        ),
        "causes_source": "none",
        "causes_note": "各行等价，没有候选错因。",
        "followup": {
            "question": "你验算过 x=2 吗？",
            "preset_answer_text": "",
            "asked_at": "",
        },
        "expects": "不标错、不给候选错因，可直接核对完",
    },
)


def build():
    """纯数据：不碰数据库、不读文件。返回 {"students": [...], "answers": [...]}。

    answers 里每条都带 `locate`（契约 §2 的键名，source 固定 preset）、
    `candidates`（预置候选，最多 3 条）、`followup`（预置问句与预置学生原话）。

    预置 locate **不许和规则引擎说两套话**：同一道题由 `equation_steps.locate`
    重算必须逐字段相同，由 `assert_preset_matches_rules()` 守住（见该函数说明）。
    """
    students = [dict(item) for item in STUDENTS]
    answers = []
    for item in ANSWERS:
        record = {key: value for key, value in item.items() if key not in ("locate", "followup")}
        record["batch"] = DEMO_BATCH
        record["unit"] = QUESTION_UNIT
        record["source"] = DEMO_SOURCE
        record["candidates"] = preset_causes(item["question_text"])
        # locate 里还有 contrast/underline/lines 三层，浅拷贝会让调用方改了返回值就污染常量，
        # 所以这里深拷贝一份再交出去。
        record["locate"] = deepcopy(item["locate"])
        record["followup"] = dict(item["followup"])
        answers.append(record)
    return {"students": students, "answers": answers}


# 防漂移不变式要比对的字段：判错的那几个键，外加对照句的三段。
_LOCATE_FIELDS = ("status", "first_bad_index", "first_bad_text", "underline", "hint_text")
_CONTRAST_FIELDS = ("expected", "actual", "note")


def verify_preset_matches_rules():
    """防漂移不变式：预置定位必须与 `equation_steps.locate` 逐字段相同。

    为什么需要它：预置条目是手写常量，规则引擎是代码；两边各改各的，就会出现
    「同一道题，演示页说一套、自动定位说另一套」，老师看到的解释会随数据来源变化。
    这份清单把预置值钉在规则输出上：以后谁改了其中一个而没同步另一个，这里立刻报差异。

    返回问题列表（空列表 = 全部一致）；每项带 record_key、field、preset、rule 四个键，
    方便直接打印差异。注意两边键名相同（契约 §2），所以可以逐字段比对。
    """
    from . import equation_steps

    problems = []
    for record in build()["answers"]:
        preset = record.get("locate") or {}
        if not preset:
            continue
        rule = equation_steps.locate(
            record["question_text"],
            record["standard_solution"],
            record["student_steps"],
            missing=bool(record.get("missing")),
        )
        preset_contrast = preset.get("contrast") or {}
        rule_contrast = rule.get("contrast") or {}
        for field in _LOCATE_FIELDS:
            if preset.get(field) != rule.get(field):
                problems.append(
                    {
                        "record_key": record["record_key"],
                        "field": field,
                        "preset": preset.get(field),
                        "rule": rule.get(field),
                    }
                )
        for field in _CONTRAST_FIELDS:
            if preset_contrast.get(field) != rule_contrast.get(field):
                problems.append(
                    {
                        "record_key": record["record_key"],
                        "field": "contrast." + field,
                        "preset": preset_contrast.get(field),
                        "rule": rule_contrast.get(field),
                    }
                )
    return problems


def assert_preset_matches_rules():
    """不变式的断言版本：不一致就抛 AssertionError，并把每一处差异打出来。

    给模块自测与后端自检调用；build() 本身不调用它，避免把演示数据变成对规则引擎的
    运行时依赖（合成数据必须能在没有规则引擎时照常演示）。
    """
    problems = verify_preset_matches_rules()
    if problems:
        lines = [
            "  %s 的 %s：预置 %r ≠ 规则 %r"
            % (item["record_key"], item["field"], item["preset"], item["rule"])
            for item in problems
        ]
        raise AssertionError(
            "预置定位与规则引擎不一致（防漂移不变式失败，共 %d 处）：\n%s"
            % (len(problems), "\n".join(lines))
        )
    return True


def catalog():
    """给文档与自检用的清单：一份作答一行，标出案例号与预期界面行为。"""
    rows = []
    for record in build()["answers"]:
        rows.append(
            {
                "case": record["case"],
                "record_key": record["record_key"],
                "student": record["student"],
                "question_no": record["question_no"],
                "question_text": record["question_text"],
                "student_steps": record["student_steps"],
                "locate_status": record["locate"]["status"],
                "first_bad_text": record["locate"]["first_bad_text"],
                "hint_text": record["locate"]["hint_text"],
                "candidate_codes": [item["code"] for item in record["candidates"]],
                "followup_question": record["followup"]["question"],
                "preset_answer_text": record["followup"]["preset_answer_text"],
                "expects": record["expects"],
                "source_label": DEMO_SOURCE,
            }
        )
    return rows


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _resolve_students(db, class_id, student_ids, needed, now):
    """挑学生：优先用调用方给的 id（backend 已经配好名单），否则用班级现有学生；
    班级里一个学生都没有时才补建合成学生。返回 (按顺序可用的 id 列表, 新建的学生)。"""
    if student_ids:
        picked = [int(value) for value in student_ids]
        if picked:
            return picked, []
    rows = db.query(
        "SELECT id FROM students WHERE class_id=? AND active=1 ORDER BY id", (class_id,)
    )
    picked = [row["id"] for row in rows]
    if not picked:
        rows = db.query("SELECT id FROM students WHERE class_id=? ORDER BY id", (class_id,))
        picked = [row["id"] for row in rows]
    created = []
    index = 0
    while len(picked) < needed:
        student = STUDENTS[index % len(STUDENTS)]
        cur = db.conn.execute(
            "INSERT INTO students(class_id, name, code, note, active, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (class_id, student["name"], "", STUDENT_NOTE, 1, now, now),
        )
        picked.append(cur.lastrowid)
        created.append({"id": cur.lastrowid, "name": student["name"]})
        index += 1
    return picked, created


def seed(db, uploads, class_id, student_ids=None):
    """写入合成作答（幂等）。

    判重口径：同一班级里 `record_key` + `source` 相同就视为已经写入，直接跳过。
    因此老师在演示页连点两次「载入合成数据」，作答行数不会增加。
    如果同一个 `record_key` 已被真实导入占用（source 不是「合成演示」），绝不覆盖，
    只记为 skipped_conflict —— 演示数据没有资格动真实记录。

    `uploads` 只为与接口契约签名一致而保留：合成作答不带图片，本函数不写任何文件。
    """
    data = build()
    now = now_iso()
    needed = len(data["answers"])
    inserted = []
    skipped_existing = 0
    skipped_conflict = 0

    with db.tx() as conn:
        existing = {
            row["record_key"]: row
            for row in db.query(
                "SELECT id, record_key, source FROM answers WHERE class_id=?", (class_id,)
            )
        }
        picked, created = _resolve_students(db, class_id, student_ids, needed, now)

        for index, record in enumerate(data["answers"]):
            key = record["record_key"]
            if key in existing:
                if existing[key]["source"] == DEMO_SOURCE:
                    skipped_existing += 1
                else:
                    skipped_conflict += 1
                continue
            student_id = picked[index % len(picked)]
            row = {
                "question_no": record["question_no"],
                "question_text": record["question_text"],
                "unit": record["unit"],
                "standard_solution": record["standard_solution"],
                "student_steps": record["student_steps"],
                "occurred_on": record["occurred_on"],
                "missing": record["missing"],
                "missing_reason": record["missing_reason"],
            }
            cur = conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution,"
                " student_steps, source, occurred_on, missing, missing_reason, fingerprint,"
                " imported_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    class_id,
                    student_id,
                    key,
                    DEMO_BATCH,
                    "",
                    "",
                    record["question_no"],
                    record["question_text"],
                    record["unit"],
                    record["standard_solution"],
                    record["student_steps"],
                    DEMO_SOURCE,
                    record["occurred_on"],
                    int(record["missing"]),
                    record["missing_reason"],
                    _fingerprint(row, student_id),
                    now,
                    now,
                    now,
                ),
            )
            inserted.append(
                {
                    "record_key": key,
                    "answer_id": cur.lastrowid,
                    "case": record["case"],
                    "question_no": record["question_no"],
                }
            )

    return {
        "ok": True,
        "class_id": class_id,
        "batch": DEMO_BATCH,
        "seeded": {
            "answers": len(inserted),
            "synthetic": True,
            "skipped_existing": skipped_existing,
            "skipped_conflict": skipped_conflict,
            "students_created": len(created),
        },
        "records": inserted,
        "students_created": created,
    }


def _delete_for_answers(conn, table, column, ids):
    """按作答 id 删关联记录；表还没建（例如本轮迁移未跑）就跳过，不报错。"""
    if not ids or not _table_exists(conn, table):
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute("DELETE FROM %s WHERE %s IN (%s)" % (table, column, marks), tuple(ids))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def reset(db, uploads, class_id):
    """只删 `source='合成演示'` 的作答及挂在它们身上的核对/追问/候选记录。

    真实导入的作答一条都不动；`kept.imported_answers` 用数字把这件事说清楚。
    没有这一步，老师做完演示再想用自己的班级，就得手工挑数据删。
    """
    ids = [
        row["id"]
        for row in db.query(
            "SELECT id FROM answers WHERE class_id=? AND source=?", (class_id, DEMO_SOURCE)
        )
    ]
    deleted_checks = deleted_followups = deleted_reviews = 0
    students_removed = []
    if ids:
        with db.tx() as conn:
            deleted_followups = _delete_for_answers(conn, "answer_followups", "answer_id", ids)
            deleted_checks = _delete_for_answers(conn, "answer_checks", "answer_id", ids)
            deleted_reviews = _delete_for_answers(conn, "answer_reviews", "answer_id", ids)
            marks = ",".join("?" * len(ids))
            conn.execute("DELETE FROM answers WHERE id IN (%s)" % marks, tuple(ids))
            # 只是 seed 当时补建的合成学生：带标记、且已经没有任何记录，才清理。
            for row in db.query(
                "SELECT id FROM students WHERE class_id=? AND note=?", (class_id, STUDENT_NOTE)
            ):
                student_id = row["id"]
                busy = (
                    db.scalar("SELECT COUNT(*) FROM answers WHERE student_id=?", (student_id,)) or 0
                ) + (
                    db.scalar("SELECT COUNT(*) FROM mistakes WHERE student_id=?", (student_id,)) or 0
                )
                if busy:
                    continue
                conn.execute("DELETE FROM students WHERE id=?", (student_id,))
                students_removed.append(student_id)

    kept = (
        db.scalar(
            "SELECT COUNT(*) FROM answers WHERE class_id=? AND source<>?", (class_id, DEMO_SOURCE)
        )
        or 0
    )
    left = (
        db.scalar(
            "SELECT COUNT(*) FROM answers WHERE class_id=? AND source=?", (class_id, DEMO_SOURCE)
        )
        or 0
    )
    return {
        "ok": True,
        "class_id": class_id,
        "deleted": {
            "answers": len(ids),
            "followups": deleted_followups,
            "checks": deleted_checks,
            "reviews": deleted_reviews,
            "students": len(students_removed),
        },
        "kept": {"imported_answers": kept, "synthetic_answers": left},
    }
