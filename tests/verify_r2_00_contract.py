"""R2-00 公共契约与数据入口验收（1.9.0）。

覆盖：
  1. 契约文档与代码双向核对（文档缺 key / 代码多 key 都红）；
  2. 四维状态枚举完整、标签唯一、每态都有合法迁移；
  3. 状态机：合法迁移放行、非法迁移拒绝（含反向自证）；
  4. R1 → R2 状态映射与「R1 既有字段语义不变」清单；
  5. 批次配置：未提交 / 缺测 / 内容不足三态、完整成绩判定、评分范围与小数位、
     步骤扣分一致性（含反向自证）；
  6. 失效规则 11 条逐条断言（含把规则摘掉后同一断言必须变红的反向自证）；
  7. 数据隔离与 AI 外发边界（R2 流程恒拒绝，开关无效；含反向自证）；
  8. 18 项统计口径（数量、key 唯一、缺数据文案、演示隔离口径）；
  9. 迁移：新库自动建 R2 表（含导出历史）、旧库（1.6.0 夹具）迁移不丢数据、幂等、重复迁移不报错、
     R1 既有表结构逐列不变、同名历史批次维持原归属、没有预留表（含反向自证）。

运行：C:\\Python314\\python.exe tests\\verify_r2_00_contract.py
日志：docs/verify-r2-00-contract.txt（原始输出，含命令与环境抬头）
"""

import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from app import db as DB                      # noqa: E402
from app import r2_contract as C              # noqa: E402

CONTRACT_DOC = os.path.join(ROOT, "docs", "R2_接口契约.md")
R1_CONTRACT_DOC = os.path.join(ROOT, "docs", "作答核对_接口契约.md")
LEGACY_FIXTURE = os.path.join(ROOT, "tests", "fixtures", "legacy160", "app.db")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-00-contract.txt")

TOTAL = [0]
FAILED = [0]
LINES = []
WORK = []


def log(text=""):
    # Windows 控制台默认 GBK，圈码/数学符号会抛 UnicodeEncodeError；日志文件统一 UTF-8，
    # 控制台能显示就显示、显示不了用 ? 顶替，绝不让「打印失败」把套件跑挂。
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


def table_snapshot(db_file):
    """整库结构与行数快照：{表名: (列清单, 行数)}。用于证明迁移没动 R1 既有表。"""
    conn = sqlite3.connect(db_file)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        out = {}
        for table in tables:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]
            count = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            out[table] = (columns, count)
        return out
    finally:
        conn.close()


def index_names(db_file):
    conn = sqlite3.connect(db_file)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()


def app_meta_version(db_file):
    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


PENDING_RESTORES = {}


def patch_bytes(path, replacements):
    """把源码里的锚点替换掉（变异体），返回**原始字节**用于恢复。

    锚点必须**恰好出现一次**：只改一处，才不会把无关逻辑一起变异掉。
    原始字节同时登记在 `PENDING_RESTORES`：即便断言过程抛异常，`main()` 的 finally
    也会把源码恢复原样（变异体绝不能留在仓库里）。
    """
    original = open(path, "rb").read()
    data = original
    for old, new in replacements:
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        hits = data.count(old_b)
        if hits != 1:
            raise AssertionError("变异锚点在 %s 里出现 %d 次（必须恰好 1 次）：%r"
                                 % (os.path.basename(path), hits, old))
        data = data.replace(old_b, new_b)
    PENDING_RESTORES[path] = original
    with open(path, "wb") as fh:
        fh.write(data)
    return original


def restore_bytes(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    PENDING_RESTORES.pop(path, None)
    importlib.reload(DB if os.path.basename(path) == "db.py" else C)


def emergency_restore():
    """收尾兜底：把还挂着的变异体源码恢复原样（不重载模块，只保证文件干净）。"""
    restored = []
    for path, data in list(PENDING_RESTORES.items()):
        with open(path, "wb") as fh:
            fh.write(data)
        restored.append(os.path.basename(path))
    PENDING_RESTORES.clear()
    return restored


# ---------------------------------------------------------------------------
# 一、契约文档与代码双向核对
# ---------------------------------------------------------------------------
def section_contract_doc():
    section("一、契约文档与代码双向核对")
    check("1.1 契约正文存在：docs/R2_接口契约.md", os.path.exists(CONTRACT_DOC), CONTRACT_DOC)
    check("1.2 R1 契约仍在（本轮只读，不许删）", os.path.exists(R1_CONTRACT_DOC), R1_CONTRACT_DOC)
    if not os.path.exists(CONTRACT_DOC):
        return
    text = open(CONTRACT_DOC, encoding="utf-8").read()

    check("1.3 文档写着同一个契约版本号 %s" % C.R2_CONTRACT_VERSION,
          C.R2_CONTRACT_VERSION in text, C.R2_CONTRACT_VERSION)
    check("1.4 代码里没有偷偷升 SCHEMA_VERSION（app_meta 口径仍是 %s）"
          % C.R2_SCHEMA_VERSION_UNCHANGED,
          DB.SCHEMA_VERSION == C.R2_SCHEMA_VERSION_UNCHANGED == "4", DB.SCHEMA_VERSION)

    missing_rules = [rid for rid in C.INVALIDATION_RULE_IDS if rid not in text]
    check("1.5 11 条失效规则 id 全部写进文档", not missing_rules and len(C.INVALIDATION_RULE_IDS) == 11,
          missing_rules)
    missing_triggers = [name for name in C.INVALIDATION_TRIGGERS if name not in text]
    check("1.6 10 个触发词全部写进文档", not missing_triggers, missing_triggers)

    missing_metrics = [row["key"] for row in C.R2_STAT_METRICS if row["key"] not in text]
    check("1.7 18 项统计口径 key 全部写进文档", not missing_metrics and len(C.R2_STAT_METRICS) == 18,
          missing_metrics)
    missing_rank = [row["key"] for row in C.R2_STAT_RANKINGS if row["key"] not in text]
    check("1.8 2 项排序结论 key 全部写进文档", not missing_rank, missing_rank)

    missing_tables = [name for name in C.R2_TABLE_NAMES if name not in text]
    check("1.9 %d 张新表名全部写进文档" % len(C.R2_TABLE_NAMES), not missing_tables,
          missing_tables)

    states = [state for mapping in C.STATES.values() for state in mapping]
    missing_states = [state for state in states if state not in text]
    check("1.10 四维状态值全部写进文档", not missing_states, missing_states)

    missing_legacy = [row["id"] for row in C.R1_LEGACY_OWNERSHIP if row["id"] not in text]
    check("1.11 R1 遗留归属编号全部写进文档", not missing_legacy, missing_legacy)

    must_have = ["§2.1", "§2.2", "§2.3", "§2.4", "§2.5", "§2.6", "§2.7", "§2.8",
                 "迁移", "18 项", "SCHEMA_VERSION", "作答核对_接口契约.md"]
    missing = [item for item in must_have if item not in text]
    check("1.12 派单 §二 的八条与迁移/统计/兼容小节都在正文里", not missing, missing)

    # 反向核对：代码里的每个**函数**都必须能在文档里按名查到（常量按值核，见 1.5–1.10）
    functions = [name for name in C.__all__ if callable(getattr(C, name, None))]
    missing_functions = [name for name in functions if name not in text]
    check("1.13 代码 __all__ 里的每个函数都在文档 §8.1 出现过（防漂移）",
          not missing_functions, missing_functions)


# ---------------------------------------------------------------------------
# 二、四维状态枚举
# ---------------------------------------------------------------------------
def section_states():
    section("二、四维状态枚举与状态机")
    check("2.1 恰好四个维度", C.DIMENSIONS == ("answer", "verify", "cause", "score"),
          C.DIMENSIONS)
    for dim in C.DIMENSIONS:
        mapping = C.STATES.get(dim) or {}
        check("2.2.%s 有枚举且每个都有中文标签" % dim,
              bool(mapping) and all(mapping.values()), mapping)
        check("2.2.%s+ 状态机里覆盖了它的每个状态" % dim,
              all(state in C.STATE_TRANSITIONS[dim] for state in mapping),
              [state for state in mapping if state not in C.STATE_TRANSITIONS[dim]])

    required = {
        "answer": {"unsubmitted", "missing", "insufficient", "valid"},
        "verify": {"unverified", "verified_rule", "verified_manual", "verified_preset", "stale"},
        "cause": {"undecided", "decided", "not_applicable"},
        "score": {"unscored", "draft", "confirmed", "invalidated"},
    }
    for dim, states in required.items():
        check("2.3.%s 必备状态齐全（%s）" % (dim, "、".join(sorted(states))),
              states <= set(C.STATES[dim]), sorted(set(C.STATES[dim]) - states))

    check("2.4 错因三态与 R1 逐字一致（不改 R1 语义）",
          set(C.CAUSE_STATES) == {"undecided", "decided", "not_applicable"}, C.CAUSE_STATES)
    labels = list(C.STATE_LABELS.values())
    check("2.5 状态标签不重复（下拉/图例不会歧义）", len(labels) == len(set(labels)), labels)
    check("2.6 状态标签里没有空串", all(label.strip() for label in labels), "")

    # 状态机：合法迁移
    legal = [
        ("answer", "unsubmitted", "valid"),
        ("answer", "missing", "valid"),
        ("answer", "valid", "insufficient"),
        ("verify", "unverified", "verified_rule"),
        ("verify", "verified_rule", "stale"),
        ("verify", "stale", "verified_manual"),
        ("cause", "undecided", "decided"),
        ("cause", "decided", "undecided"),
        ("score", "unscored", "draft"),
        ("score", "draft", "confirmed"),
        ("score", "confirmed", "invalidated"),
        ("score", "invalidated", "draft"),
    ]
    bad_legal = [(dim, a, b) for dim, a, b in legal if not C.transition_allowed(dim, a, b)]
    check("2.7 12 条合法迁移全部放行", not bad_legal, bad_legal)

    illegal = [
        ("answer", "unsubmitted", "stale"),      # 跨维状态
        ("verify", "unverified", "confirmed"),   # 跨维状态
        ("score", "unscored", "invalidated"),    # 从未评分不可能直接失效
        ("score", "invalidated", "confirmed"),   # 失效行必须重评（先 draft）
        ("cause", "decided", "verified_rule"),   # 跨维状态
        ("answer", "valid", "unsubmitted"),      # 有记录不会退回「未提交」
    ]
    wrong = [(dim, a, b) for dim, a, b in illegal if C.transition_allowed(dim, a, b)]
    check("2.8 6 条非法迁移全部拒绝", not wrong, wrong)

    raised = False
    try:
        C.transition_state("score", "unscored", "invalidated")
    except ValueError as exc:
        raised = "非法的评分状态迁移" in str(exc)
    check("2.9 transition_state() 对非法迁移抛中文 ValueError", raised, "")

    # 反向自证：把被拒的那条迁移塞进状态机，同一断言必须变红
    saved = C.STATE_TRANSITIONS["score"]["unscored"]
    C.STATE_TRANSITIONS["score"]["unscored"] = frozenset(saved | {"invalidated"})
    mutant_allowed = C.transition_allowed("score", "unscored", "invalidated")
    C.STATE_TRANSITIONS["score"]["unscored"] = saved
    check("2.10 反向自证：补上这条迁移后 2.8 的断言确实不再成立、恢复后重新成立",
          mutant_allowed is True and not C.transition_allowed("score", "unscored", "invalidated"),
          mutant_allowed)

    check("2.11 四维之间不许互相冒充：四个维度的状态集合两两不相等",
          len({frozenset(C.STATES[dim]) for dim in C.DIMENSIONS}) == 4,
          {dim: sorted(C.STATES[dim]) for dim in C.DIMENSIONS})


# ---------------------------------------------------------------------------
# 三、R1 → R2 映射与不变清单
# ---------------------------------------------------------------------------
def section_legacy_map():
    section("三、R1 → R2 状态映射与 R1 语义不变清单")
    dims = {row["dimension"] for row in C.LEGACY_STATE_MAP}
    check("3.1 映射表覆盖四个维度", dims == set(C.DIMENSIONS), dims)
    unknown = [row for row in C.LEGACY_STATE_MAP
               if row["r2"] not in C.STATE_LABELS and not row["r2"].startswith("（")]
    check("3.2 映射到的 R2 状态都是合法状态值（或明确标注为「来源」）", not unknown, unknown)
    legacy_cause = [row for row in C.LEGACY_STATE_MAP if row["source"] == "answer_checks.cause_state"]
    check("3.3 错因三个 R1 取值逐个映射且目标同名（不改语义）",
          {row["legacy"] for row in legacy_cause} == set(C.CAUSE_STATES)
          and all(row["legacy"] == row["r2"] for row in legacy_cause), legacy_cause)
    verify_sources = {row["legacy"] for row in C.LEGACY_STATE_MAP
                      if row["source"] == "answer_checks.answer_version"}
    check("3.4 核验维覆盖 rule/manual/preset 三种证据来源与「版本不一致」",
          any("rule" in item for item in verify_sources)
          and any("manual" in item for item in verify_sources)
          and any("preset" in item for item in verify_sources)
          and any("≠" in item for item in verify_sources), verify_sources)

    unchanged = set(C.R1_UNCHANGED_FIELDS)
    must = {"answer_checks.answer_version", "answer_checks.cause_state",
            "answer_checks.verify_source", "answers.missing", "answers.fingerprint",
            "mistakes.answer_id", "users.ai_individual_allowed"}
    check("3.5 R1 语义不变清单包含关键字段", must <= unchanged, sorted(must - unchanged))
    check("3.6 本套件不改 R1 契约正文（只读校验：原文件仍有 §1.2 当前版本判定）",
          "当前版本判定" in open(R1_CONTRACT_DOC, encoding="utf-8").read(), "")

    check("3.7 版本闸门：空版本永远不是当前版本",
          not C.is_current_version("", "abc") and C.is_current_version("abc", "abc")
          and not C.is_current_version("abc", "abd"), "")


# ---------------------------------------------------------------------------
# 四、批次配置 / 作答维 / 完整成绩 / 评分校验
# ---------------------------------------------------------------------------
def section_batch_and_score():
    section("四、批次配置、作答四态、完整成绩与评分校验")
    raw = {
        "expected_students": [{"key": "S01", "name": "张三"}, {"key": "S02", "name": "李四"},
                              {"key": "S01", "name": "张三（重复）"}],
        "expected_questions": ["1", "2", "3", 4],
        "scored_questions": ["1", "2"],
        "full_scores": {"1": 10, "2": "5"},
        "rubric": {"1": "列式正确 4 分", "2": "移项正确"},
        "rubric_version": "r1",
        "occurred_on": "2026-11-20",
    }
    config, errors = C.normalize_batch_config(raw, batch="9月第2次作业")
    check("4.1 规范化：名单去重（3 条 → 2 条）", len(config["expected_students"]) == 2,
          config["expected_students"])
    check("4.2 规范化：题号一律字符串（4 → '4'）", config["expected_questions"] == ("1", "2", "3", "4")
          if isinstance(config["expected_questions"], tuple)
          else config["expected_questions"] == ["1", "2", "3", "4"],
          config["expected_questions"])
    check("4.3 规范化：满分转 float", config["full_scores"]["2"] == 5.0, config["full_scores"])
    check("4.4 合法配置的 errors 为空、状态为 configured",
          not errors and config["config_state"] == C.BATCH_CONFIG_STATE_CONFIGURED,
          (errors, config["config_state"]))

    bad = dict(raw)
    bad["scored_questions"] = ["1", "9"]
    _cfg, bad_errors = C.normalize_batch_config(bad, batch=" B ")
    codes = [item[0] for item in bad_errors]
    check("4.5 应评分题不在应作答范围 → scored_not_in_expected",
          "scored_not_in_expected" in codes, bad_errors)

    bad2 = dict(raw)
    bad2["full_scores"] = {"1": 10}
    _cfg2, bad2_errors = C.normalize_batch_config(bad2, batch="B")
    check("4.6 缺满分 → full_score_missing", "full_score_missing" in
          [item[0] for item in bad2_errors], bad2_errors)

    bad3 = dict(raw)
    bad3["full_scores"] = {"1": 10, "2": 0}
    _cfg3, bad3_errors = C.normalize_batch_config(bad3, batch="B")
    check("4.7 满分必须 > 0 → full_score_invalid", "full_score_invalid" in
          [item[0] for item in bad3_errors], bad3_errors)

    bad4 = dict(raw)
    bad4["occurred_on"] = "2026/11/20"
    _cfg4, bad4_errors = C.normalize_batch_config(bad4, batch="B")
    check("4.8 日期格式错 → bad_date", "bad_date" in [item[0] for item in bad4_errors],
          bad4_errors)

    bad5 = dict(raw)
    bad5["rubric_version"] = ""
    _cfg5, bad5_errors = C.normalize_batch_config(bad5, batch="B")
    check("4.9 给了评分标准却没版本 → rubric_version_missing",
          "rubric_version_missing" in [item[0] for item in bad5_errors], bad5_errors)

    empty, empty_errors = C.normalize_batch_config({}, batch="历史批次")
    check("4.10 空配置是合法的「未配置」（errors 为空、状态 unconfigured）",
          not [item for item in empty_errors if item[0] != "missing_batch"]
          and empty["config_state"] == C.BATCH_CONFIG_STATE_UNCONFIGURED, empty_errors)

    check("4.11 未配置名单/题号时，满分合计与应作答格子都返回 None（不拿 0 冒充）",
          C.batch_full_score(empty) is None and C.roster_status(empty, [])["expected"] is None,
          (C.batch_full_score(empty), C.roster_status(empty, [])))
    check("4.12 满分合计 = 应评分题满分之和（10 + 5 = 15；非得分，见 4.25d）",
          C.batch_full_score(config) == 15.0, C.batch_full_score(config))
    check("4.13 缺任一题满分 → 满分合计 None（不做部分相加）",
          C.batch_full_score({"scored_questions": ["1", "2"], "full_scores": {"1": 10}}) is None,
          "")

    # 作答维：未提交 / 缺测 / 内容不足 / 有效
    check("4.14 missing=1 优先于内容量 → missing",
          C.classify_record_state(True, "请假", has_text=True) == ("missing", "请假"), "")
    check("4.15 有文本 → valid", C.classify_record_state(False, "", has_text=True) == ("valid", ""), "")
    check("4.16 有步骤 → valid", C.classify_record_state(False, "", has_steps=True) == ("valid", ""), "")
    check("4.17 只有照片 → insufficient（不实现手写 OCR）",
          C.classify_record_state(False, "", image_count=2) == ("insufficient", "image_only"), "")
    check("4.18 空记录 → insufficient（不冒充 valid）",
          C.classify_record_state(False, "") == ("insufficient", "empty"), "")

    # 合成口径：10 应作答 × 5 题 = 50 格，45 有效 + 5 缺测
    students = [{"key": "S%02d" % index, "name": "学生%d" % index} for index in range(1, 11)]
    cfg50 = {"expected_students": students,
             "expected_questions": ["1", "2", "3", "4", "5"],
             "scored_questions": ["1", "2", "3", "4", "5"],
             "full_scores": {str(q): 20 for q in range(1, 6)}}
    rows = []
    for index, student in enumerate(students):
        for question in range(1, 6):
            if index == 9 and question > 0:
                continue           # 第 10 名学生一条记录都没有 → 全未提交
            if index == 8 and question == 5:
                rows.append({"student_key": student["key"], "question_no": str(question),
                             "state": "missing"})
                continue
            rows.append({"student_key": student["key"], "question_no": str(question),
                         "state": "valid"})
    roster = C.roster_status(cfg50, rows)
    check("4.19 应作答格子 = 10 × 5 = 50", roster["expected"] == 50, roster)
    check("4.20 缺测与未提交分开计数（缺测 1；学生10 的 5 格全未提交）",
          roster["missing"] == 1 and roster["unsubmitted"] == 5, roster)
    check("4.21 识别「全未提交学生」（学生10）",
          roster["unsubmitted_students"] == ["S10"], roster["unsubmitted_students"])
    check("4.22 有记录格子数 = 45（9 名 × 5 题），其中 valid 44、缺测 1",
          roster["recorded"] == 45 and roster["valid"] == 44, roster)

    # §5.6 合成数据集口径（captain 按派单原文裁定）：5 条缺测集中在 S09 身上 →
    # 作答人数 = 9 而不是 10（S09 只有缺测记录，不进作答人数）
    section_rows = []
    for index, student in enumerate(students):
        for question in range(1, 6):
            if index == 9:
                section_rows.append({"student_key": student["key"],
                                     "question_no": str(question), "state": "missing"})
                continue
            section_rows.append({"student_key": student["key"],
                                 "question_no": str(question), "state": "valid"})
    submitted = C.submitted_student_keys(section_rows)
    check("4.22a 作答人数 = 至少一条非缺测的去重学生数：S10 五题全缺测 → 9 人（不是 10）",
          len(submitted) == 9 and "S10" not in submitted, submitted)
    section_roster = C.roster_status(cfg50, section_rows)
    check("4.22b 只有缺测记录也不等于未提交：S10 有记录，未提交格子 = 0",
          section_roster["unsubmitted"] == 0 and section_roster["missing"] == 5,
          section_roster)
    check("4.22c 有效作答数只数非缺测 valid 记录（50 条里 5 条缺测 → 45）",
          section_roster["valid"] == 45, section_roster["valid"])

    # 完整成绩判定
    check("4.23 缺一题 → 不完整，total 为 None",
          C.score_completeness(cfg50, {"1": 18, "2": 20, "3": 20, "4": 15})["complete"] is False,
          C.score_completeness(cfg50, {"1": 18}))
    complete = C.score_completeness(cfg50, {"1": 18, "2": 20, "3": 20, "4": 15, "5": 17})
    check("4.24 五题齐 → 完整，总分 90", complete["complete"] and complete["total"] == 90.0,
          complete)
    check("4.25 批次没配应评分题 → batch_unscored（显示暂无）",
          C.score_completeness({}, {"1": 10})["reason"] == "batch_unscored", "")

    # F2：作业总分是「逐生得分」，不是满分之和；不完整不给值
    full_confirmed = {"1": 18, "2": 20, "3": 20, "4": 15, "5": 17}
    partial_confirmed = {"1": 18, "2": 20}
    check("4.25a 作业总分（完整）＝ 该生有效确认得分之和 90",
          C.assignment_total_score(cfg50, full_confirmed)["total"] == 90.0,
          C.assignment_total_score(cfg50, full_confirmed))
    incomplete_score = C.assignment_total_score(cfg50, partial_confirmed)
    check("4.25b 不完整学生不给值：total 为 None、complete=False、missing 列出缺的题",
          incomplete_score["total"] is None and incomplete_score["complete"] is False
          and incomplete_score["missing"] == ["3", "4", "5"], incomplete_score)
    check("4.25c 不完整也不拿 0 顶替（None 与 0 不是一回事）",
          incomplete_score["total"] is None and incomplete_score["total"] != 0,
          incomplete_score["total"])
    check("4.25d 满分合计是另一项，不等于作业总分（100 vs 90）",
          C.batch_full_score(cfg50) == 100.0
          and C.assignment_total_score(cfg50, full_confirmed)["total"] == 90.0,
          (C.batch_full_score(cfg50), C.assignment_total_score(cfg50, full_confirmed)))
    per_student = C.student_total_scores(
        cfg50, {"S01": full_confirmed, "S02": partial_confirmed})
    check("4.25e 逐生作业总分：完整给分、不完整给 None",
          per_student.get("S01") == 90.0 and per_student.get("S02") is None, per_student)
    per_student_filtered = C.student_total_scores(
        cfg50, {"S01": full_confirmed, "S02": full_confirmed},
        comparable=lambda student_id, scores: student_id == "S01")
    check("4.25f 不可比的学生不合并（comparable 判假 → 不给值）",
          per_student_filtered.get("S01") == 90.0
          and per_student_filtered.get("S02") is None, per_student_filtered)

    # F3：重复错误 = 知识点重复对 + 错因重复对，两类分别计数
    knowledge_items = [
        {"student_key": "S01", "category": "去括号"},
        {"student_key": "S01", "category": "去括号"},
        {"student_key": "S01", "category": "移项"},
        {"student_key": "S02", "category": "去括号"},
    ]
    cause_items = [
        {"student_key": "S01", "category": "B01"},
        {"student_key": "S01", "category": "B01"},
        {"student_key": "S01", "category": "B02"},
    ]
    knowledge_pairs = C.repeated_pairs(knowledge_items)
    cause_pairs = C.repeated_pairs(cause_items)
    check("4.25g 知识点重复对：S01×去括号 计 1 对（S02 只有 1 次不算）",
          knowledge_pairs == [{"student_key": "S01", "category": "去括号", "count": 2}],
          knowledge_pairs)
    check("4.25h 错因重复对：S01×B01 计 1 对", cause_pairs == [
        {"student_key": "S01", "category": "B01", "count": 2}], cause_pairs)
    check("4.25i 两类分开计数，合成总数 = 两类对数之和（1 + 1 = 2）",
          len(knowledge_pairs) + len(cause_pairs) == 2, (knowledge_pairs, cause_pairs))
    check("4.25j 重复错误指标的定义写明两类（knowledge_pairs / cause_pairs）",
          "knowledge_pairs" in next(row for row in C.R2_STAT_METRICS
                                    if row["key"] == "repeated_errors")["definition"]
          and "cause_pairs" in next(row for row in C.R2_STAT_METRICS
                                    if row["key"] == "repeated_errors")["definition"], "")

    # F1/F5/F6/F7：口径文本与原文一致（analytics/R2-04 照它实现）
    metric_by_key = {row["key"]: row for row in C.R2_STAT_METRICS}
    check("4.25k 班级人数写入两条可断言约束（知识点/错因不变、学生筛选变）",
          "知识点" in metric_by_key["class_students"]["definition"]
          and "不改变" in metric_by_key["class_students"]["definition"]
          and "学生筛选" in metric_by_key["class_students"]["definition"], "")
    check("4.25l 有效作答数写明「非缺测」",
          "非缺测" in metric_by_key["valid_answers"]["definition"], "")
    check("4.25m 作答人数写明「只有缺测记录的学生不计入」",
          "只有缺测记录" in metric_by_key["submitted_students"]["definition"], "")

    # 评分范围与小数位
    check("4.26 满分 10 录 7.5 → 通过", C.validate_score(7.5, 10)["ok"] is True, "")
    check("4.27 录 11 分 → 拒绝（above_full）", C.validate_score(11, 10)["code"] == "above_full", "")
    check("4.28 录 -1 分 → 拒绝（out_of_range）", C.validate_score(-1, 10)["code"] == "out_of_range", "")
    check("4.29 录 7.555 → 拒绝（too_many_decimals）",
          C.validate_score(7.555, 10)["code"] == "too_many_decimals", "")
    check("4.30 满分没配置 → 拒绝（full_score_invalid）",
          C.validate_score(5, None)["code"] == "full_score_invalid", "")
    check("4.31 无依据留空（None）→ 拒绝落库（not_number，等教师确认）",
          C.validate_score(None, 10)["code"] == "not_number", "")

    # 反向自证 1：摘掉「不能超过满分」这道闸，4.27 必须变红
    contract_path = os.path.join(SRC, "app", "r2_contract.py")
    original = patch_bytes(contract_path, [
        ("    if number > full:\n", "    if False:  # 变异体：摘掉满分上限闸门\n")])
    try:
        importlib.reload(C)
        mutant_ok, mutant_code = C.validate_score(11, 10)["ok"], C.validate_score(11, 10)["code"]
    finally:
        restore_bytes(contract_path, original)
    check("4.32 反向自证：摘掉满分闸门后 11 分被放行（4.27 断言不再成立）",
          mutant_ok is True and mutant_code == "ok", (mutant_ok, mutant_code))
    check("4.33 恢复实现后 11 分重新被拒（同一断言重新成立）",
          C.validate_score(11, 10)["code"] == "above_full", C.validate_score(11, 10))

    # 步骤扣分一致性
    good = C.step_deductions_consistent(10, 7.5, [{"step_index": 1, "deduction": 2.5}])
    bad = C.step_deductions_consistent(10, 7.5, [{"step_index": 1, "deduction": 1}])
    check("4.34 满分 10、扣 2.5、录 7.5 → 一致", good["ok"] is True, good)
    check("4.35 满分 10、扣 1、却录 7.5 → 不一致并被拒（deduction_mismatch）",
          bad["ok"] is False and bad["code"] == "deduction_mismatch", bad)
    check("4.36 扣分值为负 → 拒绝", C.step_deductions_consistent(
        10, 10, [{"deduction": -1}])["code"] == "not_number", "")

    # 评分覆盖率的分母口径（captain 2026-11-26 核对 §5.1 原文后修正）
    scoring_cfg = {
        "expected_students": [{"key": "S01"}, {"key": "S02"}, {"key": "S03"}],
        "expected_questions": ["1", "2"],
        "scored_questions": ["1"],
        "full_scores": {"1": 10},
    }
    scoring_rows = [
        {"student_key": "S01", "question_no": "1"},
        {"student_key": "S01", "question_no": "2"},
        {"student_key": "S02", "question_no": "2"},     # 只有不参与评分的题目
    ]
    students_scored = C.scoring_students(scoring_cfg, scoring_rows)
    check("4.37 应参与评分学生数：只有非评分题的学生不计入（S01 计入、S02 不计入）",
          students_scored == ["S01"], students_scored)
    check("4.38 没有应评分题 → 应参与评分学生数为空（覆盖率显示无法计算）",
          C.scoring_students({"expected_questions": ["1"], "scored_questions": []},
                             scoring_rows) == ["S01"], "")
    coverage_row = next(row for row in C.R2_STAT_METRICS if row["key"] == "score_coverage")
    check("4.39 评分覆盖率分母写的是「应参与评分学生数」，不是应作答人数",
          coverage_row["denominator"] == "应参与评分学生数"
          and "应参与评分学生数" in coverage_row["definition"], coverage_row)

    # F10：分母必须按「在册名单 × 应评分题」全格子传，不能只传已有作答行
    roster_cfg = {
        "expected_students": [{"key": "S%02d" % index} for index in range(1, 11)],
        "expected_questions": ["1", "2", "3", "4", "5"],
        "scored_questions": ["1", "2", "3", "4", "5"],
        "full_scores": {str(q): 20 for q in range(1, 6)},
    }
    grid_rows = [{"student_key": s["key"], "question_no": q}
                 for s in roster_cfg["expected_students"] for q in range(1, 6)]
    answered_rows = [item for item in grid_rows if item["student_key"] != "S10"]
    check("4.39a 分母按「名单 × 应评分题」全格子传 → 10 人（含完全没有记录的 S10）",
          len(C.scoring_students(roster_cfg, grid_rows)) == 10,
          len(C.scoring_students(roster_cfg, grid_rows)))
    check("4.39b 只传已有作答行 → 分母少 1 人（覆盖率会被算高，这就是必须传全格子的原因）",
          len(C.scoring_students(roster_cfg, answered_rows)) == 9,
          len(C.scoring_students(roster_cfg, answered_rows)))
    check("4.39c docstring 写清了「只传已有作答行会让未提交学生漏出分母」",
          "只传" in C.scoring_students.__doc__
          and "漏出分母" in C.scoring_students.__doc__, C.scoring_students.__doc__)

    # F8 / F9：两项排序结论的口径（不是数值指标，但 definition 是 R2-04/R2-06 的引用源）
    ranking = {row["key"]: row for row in C.R2_STAT_RANKINGS}
    common = ranking["class_common_issues"]["definition"]
    check("4.39d 班级共性问题主排序键 = 涉及学生人数，频次只作辅助",
          "涉及学生人数" in common and "主排序" in common
          and "频次只作辅助" in common, common)
    priority = ranking["student_priority_issues"]["definition"]
    check("4.39e 个人重点问题写明复用 app/tutor.py 近期加权（3/2/1，30/90 天）",
          "app/tutor.py" in priority and "WEIGHT_RECENT=3" in priority
          and "RECENT_DAYS=30" in priority and "MID_DAYS=90" in priority, priority)
    tutor_text = open(os.path.join(SRC, "app", "tutor.py"), encoding="utf-8").read()
    check("4.39f 契约里引用的 tutor 常量与原版逐字一致（原版调值会立刻红）",
          all(token in tutor_text for token in (
              "WEIGHT_RECENT = 3", "WEIGHT_MID = 2", "WEIGHT_OLD = 1",
              "RECENT_DAYS = 30", "MID_DAYS = 90")), "")

    # 反向自证 2：摘掉一致性校验，4.35 必须变红
    original = patch_bytes(contract_path, [
        ("    if abs(diff) > 10 ** -MAX_SCORE_DECIMALS:\n",
         "    if False:  # 变异体：摘掉扣分一致性闸门\n")])
    try:
        importlib.reload(C)
        mutant_ok = C.step_deductions_consistent(10, 7.5, [{"deduction": 1}])["ok"]
    finally:
        restore_bytes(contract_path, original)
    check("4.40 反向自证：摘掉一致性闸门后不一致也被放行（4.35 断言不再成立）",
          mutant_ok is True, mutant_ok)
    check("4.41 恢复实现后不一致重新被拒",
          C.step_deductions_consistent(10, 7.5, [{"deduction": 1}])["ok"] is False, "")


# ---------------------------------------------------------------------------
# 五、失效规则 11 条
# ---------------------------------------------------------------------------
def section_invalidation_rules():
    section("五、更新与失效规则 11 条（逐条断言 + 反向自证）")
    check("5.1 规则恰好 11 条", len(C.INVALIDATION_RULES) == 11, len(C.INVALIDATION_RULES))
    ids = [rule["id"] for rule in C.INVALIDATION_RULES]
    check("5.2 规则 id 唯一且按序编号",
          ids == ["R2-INV-%02d" % index for index in range(1, 12)], ids)
    check("5.3 每条规则都有 statement / executor / counter_proof（可核验的说明）",
          all(rule.get("statement") and rule.get("executor") and rule.get("counter_proof")
              for rule in C.INVALIDATION_RULES),
          [rule["id"] for rule in C.INVALIDATION_RULES
           if not (rule.get("statement") and rule.get("executor") and rule.get("counter_proof"))])

    def state_for(answer_fp="fp-old", verify="verified_rule", cause="decided",
                  score="confirmed", rubric_version="r1"):
        state = C.empty_state()
        state["answer"]["fingerprint"] = answer_fp
        state["answer"]["state"] = "valid"
        state["verify"]["state"] = verify
        state["verify"]["version"] = answer_fp
        state["cause"]["state"] = cause
        state["score"]["state"] = score
        state["score"]["rubric_version"] = rubric_version
        state["score"]["formal"] = score == "confirmed"
        return state

    # 01 内容变化
    base = state_for()
    same = C.apply_event("answer_content_changed", base, {"fingerprint": "fp-old"})
    changed = C.apply_event("answer_content_changed", base, {"fingerprint": "fp-new"})
    check("5.4 ① 同指纹（内容没变）不失效",
          same["rules"] == () and same["state"]["verify"]["state"] == "verified_rule", same)
    check("5.5 ① 指纹变化 → 核验 stale / 错因 undecided / 成绩 invalidated",
          changed["state"]["verify"]["state"] == "stale"
          and changed["state"]["cause"]["state"] == "undecided"
          and changed["state"]["score"]["state"] == "invalidated", changed["state"])
    check("5.6 ① 动作清单含追问证据失效与历史保留",
          "answer_followups.invalidate:answer_updated" in changed["actions"]
          and "history.keep" in changed["actions"], changed["actions"])
    no_rule = C.apply_event("answer_content_changed", base, {"fingerprint": "fp-new"},
                            rules=[rule for rule in C.INVALIDATION_RULES
                                   if rule["id"] != "R2-INV-01"])
    check("5.7 ① 反向自证：摘掉 R2-INV-01 后同一事件不再失效（5.5 断言不恒真）",
          no_rule["rules"] == () and no_rule["state"]["verify"]["state"] == "verified_rule",
          no_rule["rules"])

    # 02 撤回核验
    retract = C.apply_event("check_retracted", state_for())
    check("5.8 ② 撤回核验 → 核验 unverified、错因回待判断",
          retract["state"]["verify"]["state"] == "unverified"
          and retract["state"]["cause"]["state"] == "undecided", retract["state"])
    check("5.9 ② 撤回核验不动评分（四维独立）",
          retract["state"]["score"]["state"] == "confirmed", retract["state"]["score"])
    check("5.10 ② 撤回动作写 invalidated_* 且版本置空",
          any("answer_version=''" in action for action in retract["actions"])
          and "answer_checks.invalidated_*" in retract["actions"], retract["actions"])
    check("5.11 ② 未核验时再撤回不产生动作（空操作）",
          C.apply_event("check_retracted", state_for(verify="unverified"))["rules"] == (), "")

    # 03 撤回错因
    cause_retract = C.apply_event("cause_retracted", state_for())
    check("5.12 ③ 撤回错因 → cause undecided，核验与成绩都保留",
          cause_retract["state"]["cause"]["state"] == "undecided"
          and cause_retract["state"]["verify"]["state"] == "verified_rule"
          and cause_retract["state"]["score"]["state"] == "confirmed", cause_retract["state"])
    check("5.13 ③ 错因本来就没判断 → 空操作",
          C.apply_event("cause_retracted", state_for(cause="undecided"))["rules"] == (), "")

    # 04 评分草稿口径
    draft = C.apply_event("score_saved", state_for(), {"new_state": "draft"})
    confirm = C.apply_event("score_saved", state_for(), {"new_state": "confirmed"})
    check("5.14 ④ 草稿 → formal=False 且动作含 exclude_draft",
          draft["state"]["score"]["formal"] is False
          and "stats.exclude_draft" in draft["actions"], draft)
    check("5.15 ④ 确认 → formal=True（唯一计入正式统计的状态）",
          confirm["state"]["score"]["formal"] is True
          and "stats.formal_scores_only" in confirm["actions"], confirm)

    # 05 标准变化
    rubric = C.apply_event("rubric_changed", state_for(rubric_version="r1"),
                           {"rubric_version": "r2"})
    rubric_same = C.apply_event("rubric_changed", state_for(rubric_version="r2"),
                                {"rubric_version": "r2"})
    check("5.16 ⑤ 标准版本变化 → 成绩 invalidated（需重评）",
          rubric["state"]["score"]["state"] == "invalidated"
          and rubric["state"]["score"]["reason"] == "rubric_changed", rubric["state"]["score"])
    check("5.17 ⑤ 标准版本没变 → 不失效",
          rubric_same["rules"] == () and rubric_same["state"]["score"]["state"] == "confirmed", "")
    check("5.18 ⑤ 反向自证：摘掉 R2-INV-05 后改标准不再失效",
          C.apply_event("rubric_changed", state_for(rubric_version="r1"),
                        {"rubric_version": "r2"},
                        rules=[rule for rule in C.INVALIDATION_RULES
                               if rule["id"] != "R2-INV-05"])["state"]["score"]["state"]
          == "confirmed", "")

    # 06 撤回成绩
    score_retract = C.apply_event("score_retracted", state_for())
    check("5.19 ⑥ 撤回成绩 → 回未评分且 formal=False",
          score_retract["state"]["score"]["state"] == "unscored"
          and score_retract["state"]["score"]["formal"] is False, score_retract["state"]["score"])
    check("5.20 ⑥ 只有已确认的成绩能撤回（草稿/未评分 → 空操作）",
          C.apply_event("score_retracted", state_for(score="draft"))["rules"] == ()
          and C.apply_event("score_retracted", state_for(score="unscored"))["rules"] == (), "")

    # 07 步骤扣分不一致（校验类，无状态变更）
    rule07 = [rule for rule in C.INVALIDATION_RULES if rule["id"] == "R2-INV-07"][0]
    check("5.21 ⑦ 该校验类规则没有触发词（不靠事件，靠写入前校验）",
          rule07["trigger"] is None, rule07["trigger"])
    check("5.22 ⑦ 一致性校验函数与规则绑定（不一致必拒）",
          C.step_deductions_consistent(10, 6, [{"deduction": 2}])["ok"] is False, "")

    # 08 资源版本
    resource = C.apply_event("resource_version_changed", state_for(),
                             {"resource_version": "v1"})
    check("5.23 ⑧ 资源版本变化 → 标 stale 并让任务待复核",
          resource["state"]["resource"]["state"] == "stale"
          and "teaching_tasks.mark_review:resource_version_changed" in resource["actions"],
          resource)
    check("5.24 ⑧ 没有给版本时不做任何事（不空转）",
          C.apply_event("resource_version_changed", state_for())["rules"] == (), "")

    # 09 任务依据
    task = C.apply_event("task_basis_changed", state_for(), {"basis_changed": True})
    check("5.25 ⑨ 依据变化 → 只打 needs_review，任务状态字段不被改写",
          task["state"]["task"]["review"] == "needs_review"
          and "teaching_tasks.mark_review:basis_changed" in task["actions"], task)
    check("5.26 ⑨ 依据没变 → 空操作",
          C.apply_event("task_basis_changed", state_for())["rules"] == (), "")

    # 10 批次配置
    batch_cfg = C.apply_event("batch_config_changed", state_for(), {"config_changed": True})
    check("5.27 ⑩ 批次配置变化 → 重算完整成绩，但不重复失效成绩",
          batch_cfg["state"]["score"]["state"] == "confirmed"
          and "score_completeness.recompute" in batch_cfg["actions"], batch_cfg)
    check("5.28 ⑩ 配置没变 → 空操作",
          C.apply_event("batch_config_changed", state_for())["rules"] == (), "")

    # 11 批次作用域
    scope = C.apply_event("batch_scope_changed", state_for(), {"scope_changed": True})
    check("5.29 ⑪ 作用域重算 → 按批次重算且显式排除演示数据",
          "stats.recount:per_batch" in scope["actions"]
          and "stats.exclude_demo" in scope["actions"], scope["actions"])
    check("5.30 ⑪ 反向自证：摘掉 R2-INV-11 后重算动作消失",
          C.apply_event("batch_scope_changed", state_for(), {"scope_changed": True},
                        rules=[rule for rule in C.INVALIDATION_RULES
                               if rule["id"] != "R2-INV-11"])["actions"] == (), "")

    check("5.31 触发词表与规则表一一对应（没有登记了却用不到的触发词）",
          {rule["trigger"] for rule in C.INVALIDATION_RULES if rule["trigger"]}
          == set(C.INVALIDATION_TRIGGERS), C.INVALIDATION_TRIGGERS)
    check("5.32 apply_event 不修改传入的 state（纯函数）",
          base["verify"]["state"] == "verified_rule" and base["score"]["state"] == "confirmed",
          base)


# ---------------------------------------------------------------------------
# 六、数据隔离与 AI 外发
# ---------------------------------------------------------------------------
def section_isolation():
    section("六、数据隔离与 AI 外发边界")
    r2_flows = ["answer_check_grading", "answer_check_cause", "class_stats_summary",
                "teaching_resource_match"]
    denied = [flow for flow in r2_flows
              if C.egress_decision(flow, individual_allowed=True)["allowed"] is False]
    check("6.1 4 个 R2 流程即使开关打开也一律拒绝外发", len(denied) == 4, denied)
    check("6.2 拒绝码统一为 ai_isolation",
          {C.egress_decision(flow, individual_allowed=True)["code"] for flow in r2_flows}
          == {"ai_isolation"}, "")
    legacy_on = C.egress_decision("mistake_photo", individual_allowed=True)
    legacy_off = C.egress_decision("mistake_photo", individual_allowed=False)
    check("6.3 R1 既有错题照片链路口径不变（开关开=允许、关=拒绝）",
          legacy_on["allowed"] is True and legacy_off["allowed"] is False,
          (legacy_on, legacy_off))
    check("6.4 未知流程一律拒绝（不默认放行）",
          C.egress_decision("whatever_flow", individual_allowed=True)["allowed"] is False, "")

    check("6.5 禁止外发字段清单覆盖身份字段与作答原文",
          {"student_name", "student_code", "class_name", "student_steps",
           "answer_text", "image_bytes"} <= set(C.EGRESS_FORBIDDEN_FIELDS),
          C.EGRESS_FORBIDDEN_FIELDS)
    check("6.6 字段检查能查出违规字段、放过安全字段",
          C.egress_field_problem(["system_prompt", "student_name"]) == ["student_name"]
          and C.egress_field_problem(["message_shape"]) == [], "")

    check("6.7 演示数据不计入正式统计",
          C.include_in_formal_stats(C.DEMO_SOURCE, False) is False
          and C.include_in_formal_stats("", True) is False
          and C.include_in_formal_stats("", False) is True, "")
    check("6.8 真实作答撞了演示批名时仍计入（判据是内容不是名字）",
          C.include_in_formal_stats("", False) is True, "")
    check("6.9 与 db/demo 的来源标记逐字一致（三处不许各写一个）",
          C.DEMO_SOURCE == DB.DEMO_SOURCE, (C.DEMO_SOURCE, DB.DEMO_SOURCE))

    # 反向自证：把 R2 流程改成「沿用开关」，6.1 必须变红
    saved = dict(C.AI_FLOWS["answer_check_grading"])
    C.AI_FLOWS["answer_check_grading"] = {"decision": C.EGRESS_DECISION_LEGACY_SWITCH,
                                          "note": "变异体"}
    mutant_allowed = C.egress_decision("answer_check_grading", individual_allowed=True)["allowed"]
    C.AI_FLOWS["answer_check_grading"] = saved
    check("6.10 反向自证：把该流程改成沿用开关后它会放行（6.1 断言不恒真）",
          mutant_allowed is True, mutant_allowed)
    check("6.11 恢复后重新拒绝",
          C.egress_decision("answer_check_grading", individual_allowed=True)["allowed"] is False, "")


# ---------------------------------------------------------------------------
# 七、18 项统计口径
# ---------------------------------------------------------------------------
def section_stats():
    section("七、18 项统计口径")
    check("7.1 指标恰好 18 项", len(C.R2_STAT_METRICS) == 18, len(C.R2_STAT_METRICS))
    keys = [row["key"] for row in C.R2_STAT_METRICS]
    check("7.2 key 不重复", len(keys) == len(set(keys)), keys)
    required = ["class_students", "expected_students", "submitted_students", "valid_answers",
                "insufficient_answers", "missing_answers", "unsubmitted_answers",
                "unverified_answers", "confirmed_mistakes", "knowledge_error_freq",
                "affected_students", "cause_freq", "undecided_causes", "question_score_rate",
                "assignment_total_score", "average_score", "score_coverage", "repeated_errors"]
    check("7.3 派单枚举的 18 项一个不少（顺序可比对）", keys == required,
          [key for key in required if key not in keys])
    check("7.4 每项都有中文名、定义、分子、空态文案",
          all(row.get("label") and row.get("definition") and row.get("numerator")
              and row.get("empty") for row in C.R2_STAT_METRICS),
          [row["key"] for row in C.R2_STAT_METRICS if not row.get("empty")])
    check("7.5 两项排序结论（共性问题 / 重点问题）单独登记",
          [row["key"] for row in C.R2_STAT_RANKINGS] == ["class_common_issues",
                                                        "student_priority_issues"],
          [row["key"] for row in C.R2_STAT_RANKINGS])
    check("7.6 缺分母统一文案不是数字 0",
          C.STAT_EMPTY_TEXT == "暂无" and C.STAT_UNDEFINED_TEXT == "无法计算",
          (C.STAT_EMPTY_TEXT, C.STAT_UNDEFINED_TEXT))
    check("7.7 平均分/得分率/覆盖率都只认已确认成绩",
          all("score=confirmed" in row["states"] for row in C.R2_STAT_METRICS
              if row["key"] in ("question_score_rate", "average_score", "score_coverage")), "")
    check("7.8 未提交数带分母（应作答格子）且空态是暂无",
          next(row for row in C.R2_STAT_METRICS if row["key"] == "unsubmitted_answers")["empty"]
          == "未配置名单/题号 → 暂无", "")
    check("7.9 筛选口径写明知识点/错因不改变班级人数、总分、平均分与覆盖率",
          any(row["filter"] == "knowledge_point" and "不" in row["note"]
              for row in C.R2_STAT_FILTER_RULES)
          and any(row["filter"] == "batch" for row in C.R2_STAT_FILTER_RULES), C.R2_STAT_FILTER_RULES)
    check("7.10 硬口径写进了 student_priority_issues（历史错题不推算掌握率）",
          "历史错题" in next(row for row in C.R2_STAT_METRICS
                             if row["key"] == "repeated_errors")["definition"], "")


# ---------------------------------------------------------------------------
# 八、迁移
# ---------------------------------------------------------------------------
def section_migration():
    section("八、增量迁移（新库 / 旧库 / 幂等 / R1 表不变 / 同名批次归属）")
    work = tmpdir("r2-00-mig-")
    fresh = os.path.join(work, "fresh", "app.db")
    DB.Database(fresh)
    snapshot = table_snapshot(fresh)
    new_tables = set(snapshot) - {
        "users", "sessions", "classes", "students", "mistakes", "advice", "app_meta",
        "answers", "answer_reviews", "tutor_advice", "followups", "followup_updates",
        "answer_checks", "answer_followups", "ai_egress_audit",
        "sqlite_sequence",     # SQLite 给 AUTOINCREMENT 自动建，不算我们的表
    }
    check("8.1 新库直接建好 %d 张 R2 表且没有多建（无预留表）" % len(C.R2_TABLE_NAMES),
          new_tables == set(C.R2_TABLE_NAMES), sorted(new_tables))
    check("8.2 新库 r2_schema_ready 为真", DB.r2_schema_ready(fresh) is True, "")
    check("8.3 新库 ensure_r2_schema 返回 None（已就绪不写库）",
          DB.ensure_r2_schema(fresh) is None, "")
    check("8.4 新库结构版本仍是 4（不升版，1.8.1 客户端仍可打开）",
          app_meta_version(fresh) == C.R2_SCHEMA_VERSION_UNCHANGED, app_meta_version(fresh))

    # 旧库：1.6.0 夹具（没有 R2 表）
    legacy = os.path.join(work, "legacy", "app.db")
    os.makedirs(os.path.dirname(legacy))
    shutil.copyfile(LEGACY_FIXTURE, legacy)
    before = table_snapshot(legacy)
    version_before = app_meta_version(legacy)
    conn = sqlite3.connect(legacy)
    try:
        answers_batches_before = sorted(row[0] for row in conn.execute(
            "SELECT DISTINCT batch FROM answers"))
    finally:
        conn.close()
    check("8.5 夹具确实是旧库（没有 R2 表）",
          not (set(C.R2_TABLE_NAMES) & set(before)) and DB.r2_schema_ready(legacy) is False,
          sorted(before))
    backup = DB.ensure_r2_schema(legacy)
    check("8.6 旧库迁移成功并留下一致性备份（文件名带时间戳+随机后缀）",
          bool(backup) and os.path.exists(backup) and ".bak-" in backup, backup)
    after = table_snapshot(legacy)
    added_tables = set(after) - set(before) - {"sqlite_sequence"}
    check("8.7 迁移后 R2 表齐备（%d 张，一张不多）" % len(C.R2_TABLE_NAMES),
          added_tables == set(C.R2_TABLE_NAMES), sorted(added_tables))
    check("8.8 迁移后 r2_schema_ready 为真", DB.r2_schema_ready(legacy) is True, "")

    changed = []
    for table, (columns, count) in before.items():
        new_columns, new_count = after.get(table, ([], -1))
        if columns != new_columns or count != new_count:
            changed.append({"table": table, "columns_same": columns == new_columns,
                            "rows_before": count, "rows_after": new_count})
    check("8.9 R1/1.8.1 既有表的结构与行数逐表不变（不删列、不改语义）", not changed, changed)

    expected_indexes = set(DB.R2_REQUIRED_INDEXES)
    check("8.10 24 个 R2 索引全部建好", expected_indexes <= index_names(legacy),
          sorted(expected_indexes - index_names(legacy)))
    check("8.11 迁移不改结构版本号（旧库写的是 %r，迁移后仍是它，不升版也不降版）"
          % version_before,
          app_meta_version(legacy) == version_before, app_meta_version(legacy))

    # 幂等：再跑一次不报错、不写库
    again = DB.ensure_r2_schema(legacy)
    stable = table_snapshot(legacy)
    check("8.12 重复迁移返回 None 且不报错", again is None, again)
    check("8.13 重复迁移后结构与行数完全不变（幂等）", stable == after, "")

    # 同名历史批次维持原归属
    conn = sqlite3.connect(legacy)
    try:
        batches = sorted(row[0] for row in conn.execute(
            "SELECT DISTINCT batch FROM answers WHERE TRIM(batch)<>''"))
        configs = conn.execute(
            "SELECT class_id, batch, config_state, origin, expected_students, full_scores"
            " FROM assignment_batches ORDER BY batch").fetchall()
        answers_batches = sorted(row[0] for row in conn.execute(
            "SELECT DISTINCT batch FROM answers"))
    finally:
        conn.close()
    check("8.14 历史批次逐个登记成配置行（同名 = 同作用域）",
          sorted(row[1] for row in configs) == batches, (configs, batches))
    check("8.15 历史批次一律标记 unconfigured + origin=legacy（不猜名单/满分）",
          all(row[2] == "unconfigured" and row[3] == "legacy" and row[4] == "[]"
              and row[5] == "{}" for row in configs), configs)
    check("8.16 迁移不改写 answers.batch（原归属原样保留）",
          answers_batches == answers_batches_before, (answers_batches_before, answers_batches))
    check("8.17 同名作用域判据：同班同名=同批次，不同班或不同名=不同批次",
          C.same_batch_scope(1, "九月作业", 1, "九月作业") is True
          and C.same_batch_scope(1, "九月作业", 2, "九月作业") is False
          and C.same_batch_scope(1, "九月作业", 1, "十月作业") is False, "")

    # 批次配置读写
    database = DB.Database(legacy, create=False)
    try:
        loaded = DB.load_batch_config(database, 1, batches[0] if batches else "x")
        check("8.18 读未配置的历史批次 → config_state=unconfigured（不抛异常）",
              loaded["config_state"] == "unconfigured", loaded["config_state"])
        saved = DB.save_batch_config(database, 1, batches[0] if batches else "x", {
            "expected_students": [{"key": "S01", "name": "张三"}],
            "expected_questions": ["1", "2"],
            "scored_questions": ["1"],
            "full_scores": {"1": 10},
            "rubric": {"1": "列式正确"},
            "rubric_version": "r1",
            "occurred_on": "2026-11-20",
        })
        check("8.19 保存批次配置 → configured 且满分/题号按字符串规范化",
              saved["config_state"] == "configured" and saved["full_scores"].get("1") == 10.0
              and C.expected_question_nos(saved) == ("1", "2")
              and C.batch_full_score(saved) == 10.0, saved)
        rejected = False
        try:
            DB.save_batch_config(database, 1, batches[0] if batches else "x",
                                 {"scored_questions": ["3"], "expected_questions": ["1"],
                                  "full_scores": {"3": 10}})
        except ValueError as exc:
            rejected = "scored_not_in_expected" in str(exc)
        check("8.20 非法配置被拒（ValueError 带错误码，不写库）", rejected, "")
        kept = DB.ensure_batch_config(database, 1, batches[0] if batches else "x")
        check("8.21 再次导入同一批次不覆盖老师已有配置（ensure 不 clobber）",
              kept["config_state"] == "configured", kept["config_state"])
        listed = DB.list_batch_configs(database, 1)
        check("8.22 批次配置可列出（供 R2-01/R2-03 下拉）", bool(listed), listed)
    finally:
        database.conn.close()

    # create=False 不会自动迁移（这正是必须由 captain 在 service 里接一行的原因）
    plain = os.path.join(work, "plain", "app.db")
    os.makedirs(os.path.dirname(plain))
    shutil.copyfile(LEGACY_FIXTURE, plain)
    DB.Database(plain, create=False).conn.close()
    check("8.23 create=False 打开旧库不迁移（迁移必须显式调用，接线片段见契约 §12）",
          DB.r2_schema_ready(plain) is False, "")

    # 反向自证：把 R2_DDL 里的一张表摘掉，同一份夹具的迁移必须失败
    db_path = os.path.join(SRC, "app", "db.py")
    original = patch_bytes(db_path, [("    + HINT_USAGES_DDL\n", "")])
    try:
        importlib.reload(DB)
        mutant_target = os.path.join(work, "mutant", "app.db")
        os.makedirs(os.path.dirname(mutant_target))
        shutil.copyfile(LEGACY_FIXTURE, mutant_target)
        raised = ""
        try:
            DB.ensure_r2_schema(mutant_target)
        except DB.MigrationError as exc:
            raised = str(exc)
        mutant_ready = DB.r2_schema_ready(mutant_target)
    finally:
        restore_bytes(db_path, original)
    check("8.24 反向自证：少建一张表时迁移必须抛 MigrationError（8.7 断言不恒真）",
          "没有生效" in raised and mutant_ready is False, raised)
    check("8.25 恢复实现后同一份夹具迁移成功、%d 张表齐备" % len(C.R2_TABLE_NAMES),
          bool(DB.ensure_r2_schema(os.path.join(work, "mutant", "app.db")))
          and DB.r2_schema_ready(os.path.join(work, "mutant", "app.db")) is True, "")

    # 迁移失败不损坏原库：注入备份故障
    fault_target = os.path.join(work, "fault", "app.db")
    os.makedirs(os.path.dirname(fault_target))
    shutil.copyfile(LEGACY_FIXTURE, fault_target)
    fault_before = table_snapshot(fault_target)
    os.environ["MISTAKE_FAULT"] = "r2_backup"
    fault_raised = ""
    try:
        DB.ensure_r2_schema(fault_target)
    except DB.MigrationError as exc:
        fault_raised = str(exc)
    finally:
        os.environ.pop("MISTAKE_FAULT", None)
    check("8.26 备份环节故障 → 抛 MigrationError 且原库一行未动",
          bool(fault_raised) and table_snapshot(fault_target) == fault_before, fault_raised)
    check("8.27 故障注入失败时不留残缺备份文件",
          not [name for name in os.listdir(os.path.dirname(fault_target)) if ".bak-" in name],
          os.listdir(os.path.dirname(fault_target)))


# ---------------------------------------------------------------------------
# 九、题级评分标准版本（R2-00.4）
# ---------------------------------------------------------------------------
def section_rubric_versions():
    section("九、题级评分标准版本（R2-00.4：改某题只失效该题）")
    base = {
        "expected_students": [{"key": "S1"}],
        "expected_questions": ["1", "2", "3"],
        "scored_questions": ["1", "2", "3"],
        "full_scores": {"1": 10, "2": 10, "3": 10},
        "rubrics": {
            "1": {"criteria": "列式正确", "rubric_version": "q1-r1"},
            "2": {"criteria": "移项变号", "rubric_version": "q2-r1"},
            "3": {"criteria": "合并同类项", "rubric_version": "q3-r1"},
        },
    }
    config, errors = C.normalize_batch_config(base, batch="B")
    check("9.1 题级配置合法（每道题带自己的 rubric_version，无需批次级版本）",
          not errors and C.rubric_versions(config)
          == {"1": "q1-r1", "2": "q2-r1", "3": "q3-r1"}, (errors, C.rubric_versions(config)))

    changed = {key: value for key, value in base.items()}
    changed["rubrics"] = dict(base["rubrics"])
    changed["rubrics"]["3"] = {"criteria": "合并同类项（改）", "rubric_version": "q3-r2"}
    changed_cfg, changed_errors = C.normalize_batch_config(changed, batch="B")
    versions = C.rubric_versions(changed_cfg)
    check("9.2 改第 3 题 → 只有第 3 题版本变，第 1/2 题版本不变",
          not changed_errors and versions["3"] == "q3-r2"
          and versions["1"] == "q1-r1" and versions["2"] == "q2-r1", versions)

    # 按题失效：改第 3 题只让第 3 题的旧成绩失效
    def score_state(question, version):
        state = C.empty_state()
        state["answer"].update({"state": "valid", "fingerprint": "fp-1"})
        state["verify"].update({"state": "verified_rule", "version": "fp-1"})
        state["cause"].update({"state": "decided"})
        state["score"].update({"state": "confirmed", "formal": True,
                               "question_no": question, "rubric_version": version})
        return state

    hit = C.apply_event("rubric_changed", score_state("3", "q3-r1"),
                        {"question_no": "3", "rubric_version": "q3-r2"})
    other = C.apply_event("rubric_changed", score_state("1", "q1-r1"),
                          {"question_no": "3", "rubric_version": "q3-r2"})
    same = C.apply_event("rubric_changed", score_state("3", "q3-r2"),
                         {"question_no": "3", "rubric_version": "q3-r2"})
    check("9.3 改第 3 题 → 第 3 题旧成绩 invalidated",
          hit["state"]["score"]["state"] == "invalidated"
          and "answer_scores.invalidate:rubric_changed:question" in hit["actions"], hit)
    check("9.4 改第 3 题 → 第 1 题成绩**不受影响**（仍是 confirmed）",
          other["rules"] == () and other["state"]["score"]["state"] == "confirmed", other)
    check("9.5 第 3 题版本没变 → 不失效（同一题也不乱撤）",
          same["rules"] == () and same["state"]["score"]["state"] == "confirmed", same)

    # 反向自证：用批次级版本比较（旧实现）会让未改动的题一起失效
    saved = C.INVALIDATION_RULES
    mutant_rules = tuple(
        dict(rule, effect=(lambda state, event: C._plan(
            {C.DIM_SCORE: {"state": "invalidated", "reason": "rubric_changed"}},
            ("answer_scores.invalidate:rubric_changed",))))
        if rule["id"] == "R2-INV-05" else rule
        for rule in saved)
    mutant = C.apply_event("rubric_changed", score_state("1", "q1-r1"),
                           {"question_no": "3", "rubric_version": "q3-r2"},
                           rules=mutant_rules)
    check("9.6 反向自证：拿批次级版本比较（变异体）会让第 1 题也被失效（9.4 不恒真）",
          mutant["state"]["score"]["state"] == "invalidated", mutant)

    # 题级缺失 → 回退批次级版本（只作历史兼容）
    legacy_raw = {
        "expected_questions": ["1"], "scored_questions": ["1"],
        "full_scores": {"1": 10}, "rubric": {"1": "旧标准原文"},
        "rubric_version": "batch-r1",
    }
    legacy_cfg, legacy_errors = C.normalize_batch_config(legacy_raw, batch="Old")
    check("9.7 题级缺失时回退批次级版本（旧形态仍可读，且不报错）",
          not legacy_errors
          and C.question_rubric_version(legacy_cfg, "1") == "batch-r1"
          and C.question_rubric(legacy_cfg, "1")["criteria"] == "旧标准原文",
          (legacy_errors, C.question_rubric(legacy_cfg, "1")))
    check("9.8 回退只作兼容：题级给了版本时批次级旧值不覆盖它",
          C.question_rubric_version(
              {"rubrics": {"1": {"criteria": "x", "rubric_version": "q1-r9"}},
               "rubric_version": "batch-r1"}, "1") == "q1-r9", "")
    bad_raw = {"expected_questions": ["1"], "scored_questions": ["1"],
               "full_scores": {"1": 10}, "rubrics": {"1": {"criteria": "没版本"}}}
    _bad_cfg, bad_errors = C.normalize_batch_config(bad_raw, batch="Bad")
    check("9.9 有标准但题级与批次级都没版本 → rubric_version_missing（不允许静默）",
          [item[0] for item in bad_errors] == ["rubric_version_missing"], bad_errors)
    check("9.10 满分权威在题级：rubrics[q].full_score 覆盖 full_scores 的旧值",
          C.full_score_of({"full_scores": {"1": 5},
                           "rubrics": {"1": {"full_score": 12}}}, "1") == 12.0, "")

    # 契约文本防回流：写明「按题失效」，禁止拿批次级比较
    text = open(CONTRACT_DOC, encoding="utf-8").read()
    check("9.11 契约写明「修改某题 → 只失效该题」并点明批次级比较属实现错误",
          "只" in text and "按题" in text and "实现错误" in text
          and "改**某题**评分标准" in text, "")
    check("9.12 契约版本已升到 %s（版本记录含 R2-00.4/00.5 与受影响任务）"
          % C.R2_CONTRACT_VERSION,
          C.R2_CONTRACT_VERSION == "R2-00.5" and "R2-00.4" in text and "R2-00.5" in text
          and "受影响任务" in text, C.R2_CONTRACT_VERSION)


# ---------------------------------------------------------------------------
# 十、导出历史入库（R2-00.5）
# ---------------------------------------------------------------------------
def section_report_exports():
    section("十、学情报告导出历史入库（R2-00.5：随 app.db 一起备份）")
    work = tmpdir("r2-00-exports-")
    path = os.path.join(work, "app.db")
    database = DB.Database(path)
    try:
        with database.tx() as conn:
            conn.execute("INSERT INTO users(username,display_name,pwd_salt,pwd_hash,iterations,"
                         "created_at,backup_key) VALUES('u1','师1','s','h',1,'now','')")
            conn.execute("INSERT INTO users(username,display_name,pwd_salt,pwd_hash,iterations,"
                         "created_at,backup_key) VALUES('u2','师2','s','h',1,'now','')")
            conn.execute("INSERT INTO classes(user_id,name,created_at,updated_at)"
                         " VALUES(1,'初三1班','now','now')")

        snapshot = table_snapshot(path)
        columns = list(snapshot.get("r2_report_exports", ([], 0))[0])
        check("10.1 新库直接建好 r2_report_exports 表", "r2_report_exports" in snapshot,
              sorted(snapshot))
        check("10.2 字段齐全且与 R2_REQUIRED_COLUMNS 一致（15 列，含 title / data_version）",
              set(DB.R2_REQUIRED_COLUMNS["r2_report_exports"]) == set(columns)
              and len(columns) == 15, columns)
        indexes = index_names(path)
        check("10.3 两个索引都在（(user_id, created_at DESC) 与 (class_id, kind)）",
              {"idx_report_exports_user", "idx_report_exports_class"} <= indexes,
              sorted(indexes))
        check("10.4 契约表名清单与 db 判定一致（R2_TABLE_NAMES 含本表）",
              "r2_report_exports" in C.R2_TABLE_NAMES
              and "r2_report_exports" in DB.R2_REQUIRED_COLUMNS, C.R2_TABLE_NAMES)

        first = DB.add_report_export(
            database, 1, 1, "class", purpose="家长会",
            scope={"batch": "9月", "kp": ["去括号"]}, batch="9月第1次作业",
            path=r"D:\导出\班级.pdf", sha256="a" * 64, bytes=2048,
            generated_at="2026-11-26T10:00:00+08:00", title="初三1班 · 学情分析报告",
            data_version="统计 R2-03.1 / 契约 R2-00.4",
            created_at="2026-11-26T10:00:01+08:00")
        second = DB.add_report_export(
            database, 1, 1, "student", student_id=2, purpose="约谈", scope="{}",
            path=r"D:\导出\个人.pdf", sha256="b" * 64, bytes=1024,
            generated_at="2026-11-26T11:00:00+08:00", title="李四 · 学情分析报告",
            data_version="统计 R2-03.1", created_at="2026-11-26T11:00:01+08:00")
        rows = DB.list_report_exports(database, 1)
        check("10.5 插入返回自增 id，按 (created_at DESC, id DESC) 倒序读回 2 条",
              first == 1 and second == 2 and [row["id"] for row in rows] == [2, 1],
              [row["id"] for row in rows])
        newest = rows[0]
        check("10.6 读回字段与写入一一相等（title / data_version / bytes / sha256 / path）",
              newest["kind"] == "student" and newest["student_id"] == 2
              and newest["title"] == "李四 · 学情分析报告"
              and newest["data_version"] == "统计 R2-03.1"
              and newest["bytes"] == 1024 and newest["sha256"] == "b" * 64
              and newest["path"] == r"D:\导出\个人.pdf"
              and newest["generated_at"] == "2026-11-26T11:00:00+08:00", newest)
        oldest = rows[1]
        check("10.7 scope 传 dict 时存成 JSON 串（读回可解析，键不丢）",
              oldest["scope"] == '{"batch": "9月", "kp": ["去括号"]}'
              and json.loads(oldest["scope"]) == {"batch": "9月", "kp": ["去括号"]},
              oldest["scope"])
        check("10.8 班级报告的 student_id 存 0（不用 NULL，省掉三值判断）",
              oldest["kind"] == "class" and oldest["student_id"] == 0, oldest)

        third = DB.add_report_export(database, 2, 1, "class", purpose="别人",
                                     created_at="2026-11-26T12:00:00+08:00")
        mine = [row["id"] for row in DB.list_report_exports(database, 1)]
        others = [row["id"] for row in DB.list_report_exports(database, 2)]
        check("10.9 不同账号互相读不到（u1 只见 1/2，u2 只见 3）",
              mine == [2, 1] and others == [3], (mine, others, third))
        check("10.10 class_id / kind 可在同账号内再过滤",
              [row["id"] for row in DB.list_report_exports(database, 1, class_id=1,
                                                           kind="student")] == [2], "")
        check("10.11 limit 只限制读取条数、不删除任何行",
              [row["id"] for row in DB.list_report_exports(database, 1, limit=1)] == [2]
              and database.scalar("SELECT COUNT(*) FROM r2_report_exports") == 3, "")

        legacy_work = tmpdir("r2-00-exports-legacy-")
        legacy = os.path.join(legacy_work, "app.db")
        legacy_db = DB.Database(legacy)
        with legacy_db.tx() as conn:
            conn.execute("INSERT INTO users(username,display_name,pwd_salt,pwd_hash,iterations,"
                         "created_at,backup_key) VALUES('u9','师9','s','h',1,'now','')")
            conn.execute("INSERT INTO classes(user_id,name,created_at,updated_at)"
                         " VALUES(1,'旧班','now','now')")
            conn.execute("INSERT INTO assignment_batches(class_id, batch, name, created_at,"
                         " updated_at) VALUES(1,'历史批次','历史批次','now','now')")
        legacy_db.conn.close()
        with sqlite3.connect(legacy) as conn:
            conn.execute("DROP TABLE r2_report_exports")
        before = table_snapshot(legacy)
        check("10.12 老 R2 库被判「未就绪」（缺本表 → r2_schema_ready False）",
              "r2_report_exports" not in before and DB.r2_schema_ready(legacy) is False,
              sorted(before))
        backup = DB.ensure_r2_schema(legacy)
        after = table_snapshot(legacy)
        check("10.13 迁移后表已建好、既有 assignment_batches 行一条没丢",
              bool(backup) and "r2_report_exports" in after
              and after["assignment_batches"][1] == before["assignment_batches"][1] == 1,
              (bool(backup), after.get("assignment_batches")))
        check("10.14 重复迁移幂等（第二次返回 None 且结构不变）",
              DB.ensure_r2_schema(legacy) is None and table_snapshot(legacy) == after, "")
        legacy_db2 = DB.Database(legacy, create=False)
        try:
            exports_legacy = DB.list_report_exports(legacy_db2, 1)
            check("10.15 迁移后读写可用（空历史读回 []，写入后能读回）",
                  exports_legacy == []
                  and DB.add_report_export(legacy_db2, 1, 1, "class", title="补建后写入") == 1
                  and len(DB.list_report_exports(legacy_db2, 1)) == 1, exports_legacy)
        finally:
            legacy_db2.conn.close()

        probe_work = tmpdir("r2-00-exports-probe-")
        probe = os.path.join(probe_work, "app.db")
        DB.Database(probe).conn.close()
        with sqlite3.connect(probe) as conn:
            conn.execute("DROP TABLE r2_report_exports")
        check("10.16 删掉本表后 r2_schema_ready 立刻为 False（就绪判定真的看这张表）",
              DB.r2_schema_ready(probe) is False, "")
        probe_db = DB.Database(probe, create=False)
        try:
            check("10.17 表不存在时 list_report_exports 返回 []（读侧安全退化，不抛异常）",
                  DB.list_report_exports(probe_db, 1) == [], "")
        finally:
            probe_db.conn.close()
    finally:
        database.conn.close()


# ---------------------------------------------------------------------------
def main():
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from logmeta import environment_lines

    for line in environment_lines():
        log(line)
    log("套件：R2-00 公共契约与数据入口（契约版本 %s）" % C.R2_CONTRACT_VERSION)
    log("")

    section_contract_doc()
    section_states()
    section_legacy_map()
    try:
        section_batch_and_score()
        section_invalidation_rules()
        section_isolation()
        section_stats()
        section_migration()
        section_rubric_versions()
        section_report_exports()
    finally:
        leftover = emergency_restore()
        if leftover:
            log("")
            log("注意：收尾时发现未恢复的变异体源码，已还原：%s" % "、".join(leftover))

    log("")
    log("合计 %d 项，通过 %d 项，失败 %d 项"
        % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    code = 1 if FAILED[0] else 0
    log("退出码：%d" % code)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    for path in WORK:
        shutil.rmtree(path, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
