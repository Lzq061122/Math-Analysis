"""R2（1.9.0）公共契约：常量、四维状态、状态机、批次配置、失效规则、统计口径。

**本模块无副作用**：不打开数据库、不读写文件、不读环境变量、不联网，也不 import
app 层其它模块（只用标准库 `re`）。R2-01/02/03/04/05 与桌面端都 import 它，所以它必须是
「谁都能安全调用」的一层：

- `db.py` 用它的常量与迁移口径（批次归属、演示隔离、表名清单）；
- `answer_import_zip.py`（R2-01）用 `normalize_batch_config` / `classify_record_state`；
- `grading.py`（R2-02）用四维状态机、`validate_score` / `step_deductions_consistent`、
  `apply_event`（失效规则）；
- `r2_stats.py`（R2-03）用 `R2_STAT_METRICS` 的 18 项口径与作答/核验/评分状态；
- `teaching_resources.py`（R2-05）用资源状态、版本与三级提示级别；
- `ui_*.py` 只读 `STATE_LABELS` 出中文文案，不自己写死字符串。

契约正文见 `docs/R2_接口契约.md`；本模块每个常量/函数在正文里都有同名小节，
`tests/verify_r2_00_contract.py` 双向核对（文档缺 key 或代码多 key 都判红）。

改动纪律：契约冻结后，任何语义变更先在 captain 处协调，再改正文与代码，不得只改代码。
"""

import re

# ---------------------------------------------------------------------------
# 一、版本与结构口径
# ---------------------------------------------------------------------------
# R2 目标应用版本。真正的 `src/app/version.py` 由 captain 在 R2-06 统一改，
# 本模块只记录契约面向的版本，供各模块生成文案/报告抬头使用。
TARGET_APP_VERSION = "1.9.0"

# 契约版本：正文与代码同步修改时必须一起升。测试断言正文里出现同一字符串。
R2_CONTRACT_VERSION = "R2-00.2"

# **不改** `app.db.SCHEMA_VERSION`（R1 起一直是 "4"）。理由（这是硬口径，别动）：
# `src/desktop/data_dir.py::_db_problem()` 用 `version != SCHEMA_VERSION` 判断旧库
# 「结构不兼容」并**拒绝打开**。1.8.1/R1 的库写的是 "4"，若 R2 升成 "5"，老师双击
# 新版桌面端会先被挡在门外，迁移根本没机会执行。R2 的结构就绪由 `db.r2_schema_ready()`
# 单独判定，不依赖结构版本号；R2 库对 1.8.1 客户端仍读作 "4"，因此可回退。
R2_SCHEMA_VERSION_UNCHANGED = "4"

# R2 新增表（db.py 建，本清单供跨模块引用与文档核对；不改任何既有表）
R2_TABLE_NAMES = (
    "assignment_batches",       # 批次配置（应作答名单/应作答题号/应评分题范围/满分/评分标准/名称日期）
    "answer_images",            # 作答图片多图（ZIP 导入的 images/，一张作答 1..n 张）
    "answer_scores",            # 教师评分（同批次同题统一满分；草稿/已确认/已失效）
    "score_step_deductions",    # 步骤扣分项（扣分项 + 扣分值 + 说明）
    "teaching_resources",       # 教学资源（错因/追问/三级提示/练习/教学建议）
    "teaching_resource_versions",   # 资源版本（新版本重回待审核，历史可追溯）
    "teaching_resource_reviews",    # 双审核记录（数学正确性 / 教学适切性）
    "teaching_tasks",           # 教学任务（个人/班级辅导任务与状态）
    "hint_usages",              # 三级提示使用记录（P1/P2/P3 + 独立尝试/修正结果）
)

# 演示数据的来源标记（与 `db.DEMO_SOURCE`、`demo_answers` 同一份口径）。
# 本模块不 import db（纯函数层不许反向依赖），测试会断言三处字符串逐字相等。
DEMO_SOURCE = "合成演示"
DEMO_RECORD_PREFIX = "DEMO-"

# 批次归属：迁移不改变任何既有 answers 行的归属，只按 (class_id, batch) 登记配置。
BATCH_ORIGIN_LEGACY = "legacy"        # 迁移期由历史作答登记出来的批次
BATCH_ORIGIN_IMPORT = "import"        # R2-01 压缩包/CSV 导入时创建
BATCH_ORIGIN_MANUAL = "manual"        # 教师在界面上手工补建
BATCH_ORIGINS = (BATCH_ORIGIN_LEGACY, BATCH_ORIGIN_IMPORT, BATCH_ORIGIN_MANUAL)

BATCH_CONFIG_STATE_CONFIGURED = "configured"      # 名单/题号/满分齐备，可算成绩与未提交
BATCH_CONFIG_STATE_UNCONFIGURED = "unconfigured"  # 历史批次：只登记归属，口径显示「暂无」
BATCH_CONFIG_STATES = (BATCH_CONFIG_STATE_CONFIGURED, BATCH_CONFIG_STATE_UNCONFIGURED)

# ---------------------------------------------------------------------------
# 二、四维独立状态（派单 §2.4）
# ---------------------------------------------------------------------------
# 四维是**彼此独立**的轴：一份作答可以「作答有效 + 未核验 + 原因待判断 + 未评分」，
# 也可以「核验已撤回 + 错因仍已判断（历史） + 成绩仍已确认」——任一条的变动都不许
# 连带改写另外三条，除非失效规则（§2.5）显式规定。
DIM_ANSWER = "answer"
DIM_VERIFY = "verify"
DIM_CAUSE = "cause"
DIM_SCORE = "score"
DIMENSIONS = (DIM_ANSWER, DIM_VERIFY, DIM_CAUSE, DIM_SCORE)

# ① 作答维：这一份作答「有没有、算不算可核验的内容」。记录级。
ANSWER_STATES = {
    "unsubmitted": "未提交",      # 应作答名单里有、但该题没有任何作答记录
    "missing": "缺测",            # 有记录但标记 missing=1（请假/未测），与未提交不是一回事
    "insufficient": "内容不足",    # 仅照片或没有可核验过程（不实现手写 OCR，不去猜）
    "valid": "有效",              # 有可核验的文本/步骤内容
}

# ② 核验维：这一份作答的「数学核验结论」是否当前有效。版本闸门 = answers.fingerprint。
# 与 R1 的关系：R1 的 locate_status（correct/first_bad/…）是核验**结论内容**，
# 这里的状态是**结论的有效性**；两者不是一套值，映射见 LEGACY_STATE_MAP。
VERIFY_STATES = {
    "unverified": "未核验",              # 当前版本没有任何核验结论
    "verified_rule": "已核验（规则）",     # 当前版本有结论，证据来自规则
    "verified_manual": "已核验（人工）",   # 当前版本有结论，证据由教师人工给出
    "verified_preset": "已核验（演示预置）",  # 合成演示的预置定位
    "stale": "旧版本（已失效）",           # 只有旧版本结论，版本不一致 → 自动退出当前视图
}

# ③ 错因维：与 R1 `cause_state` 逐字一致（不改 R1 语义）。
CAUSE_STATES = {
    "undecided": "原因待判断",
    "decided": "教师已判断",
    "not_applicable": "无需判断",
}

# ④ 评分维：R2 新增。正式统计只认 `confirmed`，与 ④ 的其它值严格区分。
SCORE_STATES = {
    "unscored": "未评分",
    "draft": "草稿",
    "confirmed": "已确认",
    "invalidated": "已失效",
}

STATES = {
    DIM_ANSWER: ANSWER_STATES,
    DIM_VERIFY: VERIFY_STATES,
    DIM_CAUSE: CAUSE_STATES,
    DIM_SCORE: SCORE_STATES,
}

STATE_LABELS = {}
for _dim, _mapping in STATES.items():
    for _state, _label in _mapping.items():
        STATE_LABELS[_state] = _label

# 辅助枚举（不属于四维，但同样不许各模块自己写字符串）
RESOURCE_STATES = {
    "draft": "草稿",
    "pending_review": "待审核",
    "published": "已发布",
    "disabled": "停用",
}
RESOURCE_KINDS = {
    "cause": "错因",
    "followup": "追问",
    "hint": "三级提示",
    "exercise": "练习",
    "advice": "教学建议",
}
RESOURCE_REVIEW_KINDS = {
    "math": "数学正确性",
    "teaching": "教学适切性",
}
TASK_STATES = {
    "draft": "草稿",
    "scheduled": "已安排",
    "completed": "已完成",
    "cancelled": "已取消",
}
# 任务的「依据是否还成立」与任务状态正交：依据变了任务状态不动，只打待复核标记（§2.5 第 9 条）。
TASK_REVIEW_STATES = {
    "ok": "依据有效",
    "needs_review": "依据已变，待复核",
}
HINT_LEVELS = {
    "P1": "关键步骤提示",
    "P2": "规则提示",
    "P3": "局部示例提示",
}
HINT_LEVEL_ORDER = ("P1", "P2", "P3")

# ---------------------------------------------------------------------------
# 三、状态机（合法迁移表）
# ---------------------------------------------------------------------------
# 键 = 当前状态，值 = 允许迁移到的状态集合（含自迁移）。不在表里的迁移一律非法。
# 判据来源：R1 既有语义（撤回 = 版本置空 + invalidated_*，不新增「失效布尔」）与
# R2 派单 §2.4/§2.5。
STATE_TRANSITIONS = {
    DIM_ANSWER: {
        "unsubmitted": frozenset({"unsubmitted", "missing", "insufficient", "valid"}),
        "missing": frozenset({"missing", "insufficient", "valid"}),      # 补录/校正后不再是缺测
        "insufficient": frozenset({"insufficient", "valid"}),
        "valid": frozenset({"valid", "insufficient"}),                   # 校正成「仅照片」时降级
    },
    DIM_VERIFY: {
        "unverified": frozenset({"unverified", "verified_rule", "verified_manual", "verified_preset"}),
        "verified_rule": frozenset({"verified_rule", "verified_manual", "unverified", "stale"}),
        "verified_manual": frozenset({"verified_manual", "verified_rule", "unverified", "stale"}),
        "verified_preset": frozenset({"verified_preset", "verified_rule", "verified_manual",
                                      "unverified", "stale"}),
        "stale": frozenset({"stale", "verified_rule", "verified_manual", "verified_preset",
                            "unverified"}),
    },
    DIM_CAUSE: {
        "undecided": frozenset({"undecided", "decided", "not_applicable"}),
        "decided": frozenset({"decided", "undecided", "not_applicable"}),
        "not_applicable": frozenset({"not_applicable", "decided", "undecided"}),
    },
    DIM_SCORE: {
        "unscored": frozenset({"unscored", "draft", "confirmed"}),
        "draft": frozenset({"draft", "confirmed", "unscored", "invalidated"}),
        "confirmed": frozenset({"confirmed", "draft", "unscored", "invalidated"}),
        "invalidated": frozenset({"invalidated", "draft", "unscored"}),
    },
}

# 状态进入条件里的硬闸门（供 R2-02 校验；不满足就拒绝写库，而不是写进去再后悔）
STATE_GATES = {
    ("score", "confirmed"): "教师显式确认 + 步骤扣分与总分一致 + 0 ≤ 分 ≤ 满分 + ≤2 位小数",
    ("score", "draft"): "允许教师先存草稿；草稿不计入任何正式统计与平均分",
    ("verify", "verified_manual"): "必须留下判断依据（R1 basis_required 闸门沿用）",
    ("cause", "decided"): "必须已有当前版本的核验结论（不允许凭旧版本判原因）",
}


def transition_allowed(dimension, from_state, to_state):
    """这条状态迁移是否合法（纯判定，不抛异常）。未知维度/未知状态一律 False。"""
    table = STATE_TRANSITIONS.get(dimension) or {}
    allowed = table.get(from_state)
    return bool(allowed) and to_state in allowed


def transition_state(dimension, from_state, to_state):
    """按状态机迁移；非法迁移抛 ValueError（消息里写清两个状态的中文名）。"""
    if not transition_allowed(dimension, from_state, to_state):
        raise ValueError(
            "非法的%s迁移：%s → %s（契约 STATE_TRANSITIONS 未允许）"
            % (
                {"answer": "作答状态", "verify": "核验状态", "cause": "错因状态",
                 "score": "评分状态"}.get(dimension, "状态"),
                STATE_LABELS.get(from_state, from_state or "（空）"),
                STATE_LABELS.get(to_state, to_state or "（空）"),
            )
        )
    return to_state


# ---------------------------------------------------------------------------
# 四、R1 → R2 状态映射（不改 R1 既有字段语义，只做读侧映谢）
# ---------------------------------------------------------------------------
# 每一行 = 「R1 库里的哪个值」→「R2 四维里的哪个状态」。R1 的列一个都不删、不改默认值，
# 也不改写入规则；R2 只在查询侧按这张表读，必要时新增自己的列（评分等）。
LEGACY_STATE_MAP = (
    # 作答维
    {"dimension": DIM_ANSWER, "source": "answers.missing", "legacy": "0", "r2": "valid",
     "note": "有记录且未标缺测；再按内容细分为 valid / insufficient 由 classify_record_state 判"},
    {"dimension": DIM_ANSWER, "source": "answers.missing", "legacy": "1", "r2": "missing",
     "note": "R1 的缺测标记原样沿用（missing_reason 一起读）"},
    {"dimension": DIM_ANSWER, "source": "无记录（应作答名单里有）", "legacy": "—", "r2": "unsubmitted",
     "note": "R1 没有名单概念，R2 由 assignment_batches.expected_students 判定；未配置名单时不出这个值"},
    # 核验维
    {"dimension": DIM_VERIFY, "source": "answer_checks.answer_version",
     "legacy": "= answers.fingerprint 且 verify_source=rule", "r2": "verified_rule",
     "note": "R1 的「当前结论」判据原样沿用"},
    {"dimension": DIM_VERIFY, "source": "answer_checks.answer_version",
     "legacy": "= answers.fingerprint 且 verify_source=manual", "r2": "verified_manual", "note": "R1-05 人工核验"},
    {"dimension": DIM_VERIFY, "source": "answer_checks.answer_version",
     "legacy": "= answers.fingerprint 且 verify_source=preset", "r2": "verified_preset", "note": "合成演示预置定位"},
    {"dimension": DIM_VERIFY, "source": "answer_checks.answer_version",
     "legacy": "≠ answers.fingerprint（含撤回后置空）", "r2": "stale",
     "note": "版本闸门：旧结论自动退出当前视图，历史行保留"},
    {"dimension": DIM_VERIFY, "source": "answer_checks 无行", "legacy": "—", "r2": "unverified", "note": ""},
    # 错因维（逐字一致）
    {"dimension": DIM_CAUSE, "source": "answer_checks.cause_state", "legacy": "undecided", "r2": "undecided", "note": "不改语义"},
    {"dimension": DIM_CAUSE, "source": "answer_checks.cause_state", "legacy": "decided", "r2": "decided", "note": "不改语义"},
    {"dimension": DIM_CAUSE, "source": "answer_checks.cause_state", "legacy": "not_applicable", "r2": "not_applicable", "note": "不改语义"},
    {"dimension": DIM_CAUSE, "source": "answer_checks.cause_source", "legacy": "none / preset / teacher / legacy",
     "r2": "（来源，不是状态）", "note": "R1 四值保留，R2 不改；统计按它区分人工/规则/历史"},
    # 评分维（R2 新增）
    {"dimension": DIM_SCORE, "source": "answer_scores 无行", "legacy": "—", "r2": "unscored", "note": "1.8.1/R1 没有评分"},
    {"dimension": DIM_SCORE, "source": "answer_scores.state", "legacy": "draft", "r2": "draft", "note": "不计入正式统计"},
    {"dimension": DIM_SCORE, "source": "answer_scores.state", "legacy": "confirmed", "r2": "confirmed", "note": "唯一计入正式统计的状态"},
    {"dimension": DIM_SCORE, "source": "answer_scores.state", "legacy": "invalidated", "r2": "invalidated", "note": "改内容/改标准/撤回后保留历史"},
)

# 直接原样沿用的 R1 字段语义（R2 不得改）：写进契约正文，测试断言这些名字还在代码里。
R1_UNCHANGED_FIELDS = (
    "answer_checks.answer_version",
    "answer_checks.cause_state",
    "answer_checks.cause_source",
    "answer_checks.verify_source",
    "answer_checks.manual_state",
    "answer_checks.locate_detail",
    "answer_checks.basis_note",
    "answer_checks.invalidated_at",
    "answers.missing",
    "answers.missing_reason",
    "answers.fingerprint",
    "mistakes.answer_id",
    "users.ai_individual_allowed",
)


# ---------------------------------------------------------------------------
# 五、批次配置（派单 §2.3）
# ---------------------------------------------------------------------------
# 一份作业批次的配置。存 `assignment_batches` 一行，`*_json` 列放下面这些结构。
# 关键区分（派单要求）：
#   - **未提交**：应作答名单里有这个学生，但该题没有任何作答记录（unsubmitted）；
#   - **缺测**：有记录、但这条记录带 missing=1（请假/未测），行本身存在（missing）；
#   - **内容不足**：行存在、没标缺测，但只有照片/没有可核验过程（insufficient）；
#   - **完整成绩**：应评分题范围里**每一题**都有 `confirmed` 成绩，缺一题都不是完整成绩。
BATCH_CONFIG_KEYS = (
    "batch",             # 批次名（与 answers.batch 同一字符串，不改写历史归属）
    "name",              # 展示名称（默认等于 batch）
    "occurred_on",       # 日期 YYYY-MM-DD，可为空
    "expected_students",  # [{"key","name","code"}] 应作答学生名单
    "expected_questions",  # ["1","2",...] 应作答题号
    "scored_questions",    # ["1","2",...] 应评分题范围（必须是 expected_questions 的子集）
    "full_scores",         # {"1":10,...} 各题满分（scored_questions 每题必填，>0）
    "rubric",              # {"1":"评分标准原文",...} 各题评分标准（可空，空则成绩不失效）
    "rubric_version",      # 评分标准版本串；变了才让旧成绩失效（§2.5 第 5 条）
    "config_state",        # configured / unconfigured
    "origin",              # legacy / import / manual
)

DEFAULT_BATCH_CONFIG = {
    "batch": "",
    "name": "",
    "occurred_on": "",
    "expected_students": [],
    "expected_questions": [],
    "scored_questions": [],
    "full_scores": {},
    "rubric": {},
    "rubric_version": "",
    "config_state": BATCH_CONFIG_STATE_UNCONFIGURED,
    "origin": BATCH_ORIGIN_LEGACY,
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 批次配置错误码（R2-01 预览与 R2-02 评分录入都按这套码出中文提示，不各写一套）
BATCH_CONFIG_CODES = {
    "missing_batch": "批次名为空",
    "bad_date": "日期必须是 YYYY-MM-DD",
    "bad_origin": "来源取值非法",
    "bad_config_state": "配置状态取值非法",
    "students_not_list": "应作答名单必须是列表",
    "student_key_missing": "应作答名单里有条目缺少稳定标识",
    "student_key_duplicate": "应作答名单里有重复的学生",
    "questions_not_list": "应作答题号必须是列表",
    "question_no_duplicate": "应作答题号重复",
    "scored_not_in_expected": "应评分题号不在应作答题号里",
    "full_score_missing": "应评分题缺少满分",
    "full_score_invalid": "满分必须是大于 0 的数值",
    "rubric_version_missing": "同一批次同一题的评分标准要么都给版本，要么都不给",
    "config_incomplete": "批次配置未完成（缺名单/题号/满分），统计按「暂无」处理",
}


def _as_question_list(value):
    """题号一律按字符串存（"1" vs 1 不许出现两种 key）。"""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [item.strip() for item in str(value).split(",") if item.strip()]
    items = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def _as_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def normalize_batch_config(raw, batch=""):
    """把各种来源（CSV/界面/JSON 列）的批次配置规范化，返回 `(config, errors)`。

    纯函数：不写库、不抛异常。errors 是 [(code, detail)]，code 见 `BATCH_CONFIG_CODES`。
    规范化规则（各模块必须共用这一份，不许自己 str/int 混着比）：
      - 题号/学生 key 一律转字符串并去空白；
      - 名单去重（保留首次出现的顺序，稳定，便于界面回放）；
      - 满分转 float，`full_scores` 的 key 一律字符串题号；
      - 未给 `batch` 时用参数补；未给 `name` 时等于 `batch`。
    """
    raw = dict(raw or {})
    config = dict(DEFAULT_BATCH_CONFIG)
    config["batch"] = str(batch or raw.get("batch") or "").strip()
    config["name"] = str(raw.get("name") or config["batch"]).strip() or config["batch"]
    config["occurred_on"] = str(raw.get("occurred_on") or "").strip()
    config["rubric_version"] = str(raw.get("rubric_version") or "").strip()
    config["origin"] = str(raw.get("origin") or BATCH_ORIGIN_LEGACY).strip() or BATCH_ORIGIN_LEGACY
    config["config_state"] = str(raw.get("config_state") or "").strip() or BATCH_CONFIG_STATE_UNCONFIGURED

    students = raw.get("expected_students") or []
    normalized_students = []
    seen = set()
    if isinstance(students, (list, tuple)):
        for item in students:
            if isinstance(item, dict):
                key = str(item.get("key") or item.get("code") or item.get("name") or "").strip()
                entry = {
                    "key": key,
                    "name": str(item.get("name") or "").strip(),
                    "code": str(item.get("code") or "").strip(),
                }
            else:
                key = str(item or "").strip()
                entry = {"key": key, "name": key, "code": ""}
            if not key or key in seen:
                continue
            seen.add(key)
            normalized_students.append(entry)
    config["expected_students"] = normalized_students

    config["expected_questions"] = _dedupe(_as_question_list(raw.get("expected_questions")))
    config["scored_questions"] = _dedupe(_as_question_list(raw.get("scored_questions")))

    full_scores = {}
    for key, value in (raw.get("full_scores") or {}).items():
        number = _as_float(value)
        full_scores[str(key).strip()] = number if number is not None else value
    config["full_scores"] = full_scores

    rubric = {}
    for key, value in (raw.get("rubric") or {}).items():
        rubric[str(key).strip()] = str(value or "")
    config["rubric"] = rubric

    errors = batch_config_errors(config)
    if not errors and config["expected_students"] and config["expected_questions"]:
        config["config_state"] = BATCH_CONFIG_STATE_CONFIGURED
    return config, errors


def _dedupe(items):
    out = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def batch_config_errors(config):
    """校验批次配置，返回 [(code, detail)]；空列表 = 合法。

    只校验「能不能据此出统计」的硬条件，不替老师做教学决策：
      - 应评分题必须在应作答题号里；
      - 每个应评分题必须有 > 0 的满分；
      - 评分标准给了就要有版本号（否则无法判断「标准变没变」）。
    名单/题号可以为空：那是「历史批次未配置」，统计显示「暂无」而不是报错。
    """
    errors = []
    config = config or {}
    batch = str(config.get("batch") or "").strip()
    if not batch:
        errors.append(("missing_batch", ""))
    occurred_on = str(config.get("occurred_on") or "").strip()
    if occurred_on and not DATE_RE.match(occurred_on):
        errors.append(("bad_date", occurred_on))
    origin = str(config.get("origin") or BATCH_ORIGIN_LEGACY)
    if origin not in BATCH_ORIGINS:
        errors.append(("bad_origin", origin))
    state = str(config.get("config_state") or BATCH_CONFIG_STATE_UNCONFIGURED)
    if state not in BATCH_CONFIG_STATES:
        errors.append(("bad_config_state", state))

    students = config.get("expected_students")
    if students is not None and not isinstance(students, (list, tuple)):
        errors.append(("students_not_list", type(students).__name__))
        students = []
    keys = []
    for item in students or []:
        key = str((item or {}).get("key") or "").strip() if isinstance(item, dict) else str(item or "").strip()
        if not key:
            errors.append(("student_key_missing", ""))
            continue
        if key in keys:
            errors.append(("student_key_duplicate", key))
            continue
        keys.append(key)

    expected = _as_question_list(config.get("expected_questions"))
    if config.get("expected_questions") is not None and not isinstance(
            config.get("expected_questions"), (list, tuple, str)):
        errors.append(("questions_not_list", type(config.get("expected_questions")).__name__))
    if len(expected) != len(set(expected)):
        errors.append(("question_no_duplicate", ",".join(expected)))

    scored = _as_question_list(config.get("scored_questions"))
    for question in scored:
        if expected and question not in expected:
            errors.append(("scored_not_in_expected", question))
    full_scores = config.get("full_scores") or {}
    if not isinstance(full_scores, dict):
        errors.append(("full_score_invalid", "full_scores 必须是 {题号: 满分}"))
        full_scores = {}
    for question in scored:
        if question not in {str(key).strip() for key in full_scores}:
            errors.append(("full_score_missing", question))
            continue
        number = _as_float(full_scores.get(question, full_scores.get(str(question))))
        if number is None or number <= 0:
            errors.append(("full_score_invalid", "%s=%r" % (question, full_scores.get(question))))

    rubric = config.get("rubric") or {}
    if isinstance(rubric, dict) and rubric and not str(config.get("rubric_version") or "").strip():
        errors.append(("rubric_version_missing", ""))
    return errors


def expected_question_nos(config):
    """应作答题号（字符串元组，顺序即配置顺序）。"""
    return tuple(_as_question_list((config or {}).get("expected_questions")))


def scored_question_nos(config):
    """应评分题号：未显式配置时按「应作答题号」全评（派单口径：应评分题范围可小于应作答）。"""
    scored = _as_question_list((config or {}).get("scored_questions"))
    if scored:
        return tuple(scored)
    return expected_question_nos(config)


def expected_student_keys(config):
    """应作答学生标识（字符串元组，去重后顺序稳定）。"""
    keys = []
    for item in (config or {}).get("expected_students") or []:
        key = str((item or {}).get("key") or "").strip() if isinstance(item, dict) else str(item or "").strip()
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def full_score_of(config, question_no):
    """某题满分；未配置返回 None（统计按「暂无」处理，不猜 100）。"""
    scores = (config or {}).get("full_scores") or {}
    if not isinstance(scores, dict):
        return None
    value = scores.get(str(question_no))
    if value is None:
        value = scores.get(question_no)
    return _as_float(value)


def batch_full_score(config):
    """【非得分】应评分题满分之和；缺任一题满分 → None（显示「暂无」而不是部分相加）。

    这是**满分合计**，只用于「本卷共多少分」这类展示，不参与平均分/得分率/覆盖率。
    R2 派单 §5.1 第 15 项「作业总分」指的是**逐生得分之和**，见 `assignment_total_score()`。
    """
    scored = scored_question_nos(config)
    if not scored:
        return None
    total = 0.0
    for question in scored:
        value = full_score_of(config, question)
        if value is None:
            return None
        total += value
    return round(total, 2)


def assignment_total_score(config, confirmed_scores):
    """【作业总分】某一学生「全部应评分题目的有效确认得分之和」（派单 §5.1 第 15 项）。

    返回 `{"total", "complete", "missing", "reason"}`：
      - 该生应评分题**全部**有 `confirmed` 成绩 → `total` 为得分之和；
      - 缺任何一题 → `total=None`、`complete=False`、`missing=[题号]`；
        **不完整不给值**：不许用 0 或「已判题目的部分和」顶替，否则平均分被拉低；
      - 批次没有应评分题 → `reason="batch_unscored"`，值同样为 None。
    调用方按 student_id 建索引，得到的就是「逐生作业总分」字典（见 `student_total_scores()`）。
    """
    result = score_completeness(config, confirmed_scores)
    return {
        "total": result["total"],
        "complete": result["complete"],
        "missing": result["missing"],
        "reason": result["reason"],
    }


def student_total_scores(config, scores_by_student, comparable=None):
    """【逐生作业总分】把 `assignment_total_score` 摊成 `{student_id: 分数或 None}`。

    `scores_by_student` = {student_id: {题号: 分数}}（只传 `confirmed` 的行）。
    `comparable`（可选）= 可合并的判定：返回 True 的学生才给值——不同批次/不同满分/
    不同评分标准的学生不合并（派单 §5.1 硬口径「不合并不可比」）。缺省视为全部可比。
    不完整或不可比的学生在结果里是 `None`（界面显示「暂无」），**不是 0**。
    """
    result = {}
    for student_id, scores in (scores_by_student or {}).items():
        if comparable is not None and not comparable(student_id, scores):
            result[student_id] = None
            continue
        result[student_id] = assignment_total_score(config, scores)["total"]
    return result


def classify_record_state(missing=False, missing_reason="", has_text=False, has_steps=False,
                          image_count=0):
    """一份作答记录属于作答维的哪个状态，返回 `(state, reason_code)`。

    顺序即判据优先级（不许颠倒）：
      1. `missing=1` → missing（R1 的缺测标记优先，别拿内容量去推翻它）；
      2. 有可核验文本/步骤 → valid；
      3. 只有图片 → insufficient（这是「仅照片作答保存为内容不足」的唯一落点，
         不实现手写 OCR，所以不去猜内容够不够）；
      4. 什么都没有 → insufficient（空记录也不当 valid）。
    """
    if bool(int(missing or 0)):
        return "missing", str(missing_reason or "")
    if has_text or has_steps:
        return "valid", ""
    if int(image_count or 0) > 0:
        return "insufficient", "image_only"
    return "insufficient", "empty"


def roster_status(config, records):
    """按批次配置算「应作答 vs 实际记录」的格子级统计（纯函数，R2-03 直接用）。

    `records` 是 [{"student_key","question_no","state"}]，state 取自作答维四值。
    返回：
      expected          应作答格子数（名单人数 × 应作答题号数；名单或题号未配置 → None）
      recorded          有记录的格子数
      unsubmitted       未提交格子数（= expected - recorded；未配置 → None）
      missing / insufficient / valid  各状态格子数（记录级，含不在应作答范围内的记录）
      unsubmitted_students  完全没有任何记录的学生 key 列表（「识别全未提交学生」）
    """
    config = config or {}
    students = expected_student_keys(config)
    questions = expected_question_nos(config)
    counts = {"missing": 0, "insufficient": 0, "valid": 0}
    seen_cells = set()
    per_student = {}
    for row in records or []:
        student = str((row or {}).get("student_key") or "")
        question = str((row or {}).get("question_no") or "")
        state = str((row or {}).get("state") or "")
        if state in counts:
            counts[state] += 1
        seen_cells.add((student, question))
        per_student.setdefault(student, 0)
        per_student[student] += 1
    expected_cells = None
    unsubmitted = None
    unsubmitted_students = []
    if students and questions:
        expected_cells = len(students) * len(questions)
        unsubmitted = 0
        for student in students:
            if per_student.get(student, 0) == 0:
                unsubmitted_students.append(student)
            for question in questions:
                if (student, question) not in seen_cells:
                    unsubmitted += 1
    return {
        "expected": expected_cells,
        "recorded": len(records or []),
        "unsubmitted": unsubmitted,
        "missing": counts["missing"],
        "insufficient": counts["insufficient"],
        "valid": counts["valid"],
        "unsubmitted_students": unsubmitted_students,
        "configured": bool(students and questions),
    }


def scoring_students(config, records):
    """「应参与评分学生数」的口径（派单 §5.1 第 17 项的分母，纯函数）。

    `records` 必须是**统计范围内在册名单 × 应评分题的全部组合**（含未提交格子）：
    `{"student_key","question_no"}` 列表。**只传「已有作答行」会让完全未提交的学生
    漏出分母、把覆盖率算高**（analytics 的 v6 变体：名单加一名无记录学生时，分母应 11，
    只传作答行会得 10）。
    判定：该学生在应评分题范围（`scored_question_nos`）里至少有一道题（不论交没交）。
    批次没有应评分题 → 返回空列表（覆盖率显示「无法计算」，不拿应作答人数顶替）。
    为什么不能拿「应作答人数」当分母：某学生只有不参与评分的题目时，分母会变大、覆盖率被算低。
    """
    scored = set(scored_question_nos(config or {}))
    if not scored:
        return []
    students = []
    for row in records or []:
        student = str((row or {}).get("student_key") or "").strip()
        question = str((row or {}).get("question_no") or "").strip()
        if student and question in scored and student not in students:
            students.append(student)
    return students


def submitted_student_keys(records):
    """【作答人数】至少有一条**非缺测**记录的去重学生 key（派单 §5.1 第 3 项）。

    `records` = [{"student_key","state"}]，state 取作答维四值。
    只有缺测记录的学生**不算作答**（例如 5 题全标缺测 → 他不进作答人数）；
    内容不足（仅照片）算作答但不进有效作答数。这是「作答人数 ≠ 有记录人数」的唯一实现，
    R2-03 直接调用，别在统计里另写一遍条件。
    """
    students = []
    for row in records or []:
        student = str((row or {}).get("student_key") or "").strip()
        state = str((row or {}).get("state") or "").strip()
        if student and state in ("valid", "insufficient") and student not in students:
            students.append(student)
    return students


def repeated_pairs(items, student_field="student_key", category_field="category", minimum=2):
    """【重复错误】同一学生同类目出现 ≥ `minimum` 次的对，返回按次数降序的列表。

    派单 §5.1 第 18 项要求「同一学生不同作答中重复出现的**知识点或确认错因**」：
    两类各算一遍（`knowledge_pairs` / `cause_pairs`），明细必须能分开看，
    合成总数只能是「两类对数之和」——所以这里做成一个通用函数，两类各调一次。
    同一错题多知识点/多错因仍按类别分别计；**同一（学生, 类别）只计一次对数**，
    不因为一条错题里重复写了两遍同类别就多算。
    """
    counts = {}
    order = []
    for item in items or []:
        student = str((item or {}).get(student_field) or "").strip()
        category = str((item or {}).get(category_field) or "").strip()
        if not student or not category:
            continue
        key = (student, category)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1
    pairs = [
        {"student_key": student, "category": category, "count": counts[(student, category)]}
        for student, category in order
        if counts[(student, category)] >= int(minimum or 2)
    ]
    pairs.sort(key=lambda row: (-row["count"], row["student_key"], row["category"]))
    return pairs


def score_completeness(config, confirmed_scores):
    """「完整成绩」判定（派单 §2.3）：应评分题范围里每一题都有已确认成绩才算完整。

    `confirmed_scores` = {题号: 分数}（只传 `confirmed` 的行）。
    返回 {"complete": bool, "scored": [...], "missing": [...], "total": 分数或 None,
          "reason": "" | "batch_unscored" | "incomplete"}
    只有完整成绩才进平均分/完整成绩人数；不完整的一分都不能凑（避免用半份成绩拉低平均）。
    """
    scored = scored_question_nos(config or {})
    confirmed = {str(key): value for key, value in (confirmed_scores or {}).items()}
    if not scored:
        return {"complete": False, "scored": [], "missing": [],
                "total": None, "reason": "batch_unscored"}
    present = [question for question in scored if question in confirmed]
    missing = [question for question in scored if question not in confirmed]
    if missing:
        return {"complete": False, "scored": present, "missing": missing,
                "total": None, "reason": "incomplete"}
    total = 0.0
    for question in scored:
        number = _as_float(confirmed[question])
        if number is None:
            return {"complete": False, "scored": present, "missing": [question],
                    "total": None, "reason": "incomplete"}
        total += number
    return {"complete": True, "scored": present, "missing": [],
            "total": round(total, 2), "reason": ""}


# ---------------------------------------------------------------------------
# 六、评分校验（派单 §2.3/§2.4 ④）
# ---------------------------------------------------------------------------
MAX_SCORE_DECIMALS = 2
SCORE_ERROR_CODES = {
    "ok": "",
    "not_number": "分数必须是数值",
    "full_score_invalid": "该题没有配置有效满分，不能录入分数",
    "out_of_range": "分数不能小于 0",
    "above_full": "分数不能超过该题满分",
    "too_many_decimals": "分数最多保留两位小数",
    "no_evidence": "没有依据时不得给分（建议得分留空，等教师确认）",
}


def validate_score(score, full_score):
    """校验一个教师录入的分数，返回 {"ok","code","message","value"}。

    口径（派单验收原文）：0 ≤ 分 ≤ 满分、最多两位小数；满分来自同批次同题的统一配置，
    不许在前端另传一个「满分」把它绕过去。
    """
    full = _as_float(full_score)
    if full is None or full <= 0:
        return {"ok": False, "code": "full_score_invalid",
                "message": SCORE_ERROR_CODES["full_score_invalid"], "value": None}
    if score is None or (isinstance(score, str) and not score.strip()):
        return {"ok": False, "code": "not_number",
                "message": SCORE_ERROR_CODES["not_number"], "value": None}
    number = _as_float(score)
    if number is None:
        return {"ok": False, "code": "not_number",
                "message": SCORE_ERROR_CODES["not_number"], "value": None}
    if number < 0:
        return {"ok": False, "code": "out_of_range",
                "message": SCORE_ERROR_CODES["out_of_range"], "value": number}
    if number > full:
        return {"ok": False, "code": "above_full",
                "message": "%s（本题满分 %s）" % (SCORE_ERROR_CODES["above_full"], _fmt(full)),
                "value": number}
    if _decimals(number) > MAX_SCORE_DECIMALS:
        return {"ok": False, "code": "too_many_decimals",
                "message": SCORE_ERROR_CODES["too_many_decimals"], "value": number}
    return {"ok": True, "code": "ok", "message": "", "value": round(number, MAX_SCORE_DECIMALS)}


def step_deductions_consistent(full_score, score, deductions):
    """步骤扣分与总分一致性校验：`满分 - Σ扣分` 必须等于录入分数。

    `deductions` = [{"step_index":0/None,"label":"...","deduction":2.5,"note":"..."}]。
    返回 {"ok","expected","actual","diff","code","message"}；不一致就不许保存（§2.5 第 7 条）。
    """
    full = _as_float(full_score)
    actual = _as_float(score)
    if full is None or actual is None:
        return {"ok": False, "expected": None, "actual": actual, "diff": None,
                "code": "not_number", "message": SCORE_ERROR_CODES["not_number"]}
    total = 0.0
    for item in deductions or []:
        value = _as_float((item or {}).get("deduction"))
        if value is None or value < 0:
            return {"ok": False, "expected": None, "actual": actual, "diff": None,
                    "code": "not_number", "message": "扣分值必须是 ≥0 的数值"}
        total += value
    expected = round(full - total, MAX_SCORE_DECIMALS)
    diff = round(actual - expected, MAX_SCORE_DECIMALS)
    if abs(diff) > 10 ** -MAX_SCORE_DECIMALS:
        return {"ok": False, "expected": expected, "actual": actual, "diff": diff,
                "code": "deduction_mismatch",
                "message": "步骤扣分合计 %s，按「满分−扣分」应为 %s，与录入分数 %s 不一致"
                           % (_fmt(total), _fmt(expected), _fmt(actual))}
    return {"ok": True, "expected": expected, "actual": actual, "diff": 0.0,
            "code": "ok", "message": ""}


def _decimals(number):
    text = ("%.10f" % float(number)).rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _fmt(number):
    """分数文案：整数不带小数点，其余最多两位。"""
    value = float(number)
    if abs(value - round(value)) < 1e-9:
        return "%d" % round(value)
    return ("%.2f" % value).rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# 七、更新与失效规则（派单 §2.5，共 11 条）
# ---------------------------------------------------------------------------
# 每条规则 = 「什么事件」→「哪些维失效/保留」+「要落库的动作」。`apply_event()` 是
# 唯一的执行入口（纯函数，返回新的状态快照与动作清单，不碰库）。之所以做成纯函数：
#   1. 同一套规则 R2-02（评分/核验）、R2-05（资源/任务）、R2-01（重导覆盖）共用，
#      不会三处各写一遍「失效」；
#   2. 反向自证：测试可以把某条规则从 rules 里摘掉，同一断言必须变红，证明断言不是恒真。
#
# 约定：`effect(state, event)` 返回 None 表示「本事件对本状态不生效」，必须真的返回 None
# （而不是返回空 plan），否则统计会被空动作污染。 
def _plan(states=None, actions=()):
    return {"states": states or {}, "actions": tuple(actions)}


def _inv_01_answer_content_changed(state, event):
    """① 作答内容变化（重导覆盖 / 单条补录 / 校正）→ 核验、错因、追问证据、成绩全部失效。"""
    old = (state or {}).get(DIM_ANSWER) or {}
    old_fp = str(old.get("fingerprint") or "")
    new_fp = str((event or {}).get("fingerprint") or "")
    if old_fp and new_fp and old_fp == new_fp:
        return None
    return _plan(
        {
            DIM_VERIFY: {"state": "stale", "version": old_fp},
            DIM_CAUSE: {"state": "undecided", "code": "", "label": "", "reason": "answer_updated"},
            DIM_SCORE: {"state": "invalidated", "answer_version": old_fp,
                        "reason": "answer_updated", "formal": False},
        },
        ("answer_checks.invalidate", "answer_followups.invalidate:answer_updated",
         "answer_scores.invalidate", "history.keep"),
    )


def _inv_02_check_retracted(state, event):
    """② 撤回核验结论 → 退出核验/错因统计，评分独立保留；同版本不堆重复行。"""
    verify = (state or {}).get(DIM_VERIFY) or {}
    if verify.get("state") not in ("verified_rule", "verified_manual", "verified_preset"):
        return None
    return _plan(
        {
            DIM_VERIFY: {"state": "unverified", "version": ""},
            DIM_CAUSE: {"state": "undecided", "code": "", "label": "", "reason": "conclusion_retracted"},
        },
        ("answer_checks.retract:answer_version=''", "answer_checks.invalidated_*",
         "answer_followups.invalidate:conclusion_retracted", "history.keep"),
    )


def _inv_03_cause_retracted(state, event):
    """③ 撤回错因判断 → 退出错因统计，核验与评分各自独立保留（R2 派单原文）。"""
    cause = (state or {}).get(DIM_CAUSE) or {}
    if cause.get("state") != "decided":
        return None
    return _plan(
        {DIM_CAUSE: {"state": "undecided", "code": "", "label": "", "reason": "cause_retracted"}},
        ("answer_checks.cause_history.append", "mistakes.unlink_confirmed",
         "stats.cause_freq.recount"),
    )


def _inv_04_score_saved(state, event):
    """④ 评分落库 → 只有 `confirmed` 计入正式统计；草稿一律排除。"""
    target = str((event or {}).get("new_state") or "")
    if not target:
        return None
    if target == "confirmed":
        return _plan({DIM_SCORE: {"formal": True}}, ("stats.formal_scores_only",))
    return _plan({DIM_SCORE: {"formal": False}}, ("stats.exclude_draft",))


def _inv_05_rubric_changed(state, event):
    """⑤ 改评分标准/满分 → 该题既有成绩失效（需重评），旧值留历史。"""
    new_version = str((event or {}).get("rubric_version") or "")
    if not new_version:
        return None
    score = (state or {}).get(DIM_SCORE) or {}
    if score.get("state") not in ("draft", "confirmed"):
        return None
    if str(score.get("rubric_version") or "") == new_version:
        return None
    return _plan(
        {DIM_SCORE: {"state": "invalidated", "reason": "rubric_changed",
                     "rubric_version": str(score.get("rubric_version") or ""), "formal": False}},
        ("answer_scores.invalidate:rubric_changed", "score_steps.keep_history", "stats.recount"),
    )


def _inv_06_score_retracted(state, event):
    """⑥ 撤回成绩 → 回未评分、退出正式统计、保留历史。"""
    score = (state or {}).get(DIM_SCORE) or {}
    if score.get("state") != "confirmed":
        return None
    return _plan(
        {DIM_SCORE: {"state": "unscored", "reason": "score_retracted", "formal": False}},
        ("answer_scores.retract", "score_steps.keep_history", "stats.recount"),
    )


def _inv_07_step_deductions_mismatch(state, event):
    """⑦ 步骤扣分与总分不一致 → 拒绝保存（本节不产生状态变更，闸门在 validate/step 校验）。"""
    return None


def _inv_08_resource_version_changed(state, event):
    """⑧ 教学资源出新版本/被停用 → 引用旧版本的推荐、提示、任务标「待复核」，不静默替换。"""
    ref = str((event or {}).get("resource_version") or "")
    if not ref:
        return None
    return _plan(
        {"resource": {"state": "stale", "version": ref, "reason": "resource_version_changed"}},
        ("teaching_resources.mark_review", "teaching_tasks.mark_review:resource_version_changed",
         "hint_usages.keep"),
    )


def _inv_09_task_basis_changed(state, event):
    """⑨ 教学任务的依据（错因/知识点/统计范围）变化 → 打待复核标记，不自动改派或删除。"""
    if not (event or {}).get("basis_changed"):
        return None
    return _plan(
        {"task": {"review": "needs_review", "reason": "basis_changed"}},
        ("teaching_tasks.mark_review:basis_changed", "teaching_tasks.history.append"),
    )


def _inv_10_batch_config_changed(state, event):
    """⑩ 批次配置变化（应评分题范围/应作答题号/满分）→ 统计与完整成绩重算。

    只改满分/评分标准的，由第 ⑤ 条负责让旧成绩失效；本条不做第二遍失效，
    免得同一份成绩被两条规则各撤一次（重复失效会让历史里出现两条相同记录）。
    """
    if not (event or {}).get("config_changed"):
        return None
    return _plan(
        {DIM_SCORE: {"completeness": "recount"}},
        ("stats.recount", "score_completeness.recompute"),
    )


def _inv_11_batch_scope_changed(state, event):
    """⑪ 同名批次/演示数据的归属隔离：统计按 (class_id, batch) 作用域重算，不改写历史归属。"""
    if not (event or {}).get("scope_changed"):
        return None
    return _plan({}, ("stats.recount:per_batch", "answers.keep_batch_scope",
                     "stats.exclude_demo"))


INVALIDATION_RULES = (
    {
        "id": "R2-INV-01",
        "name": "作答内容变化",
        "trigger": "answer_content_changed",
        "effect": _inv_01_answer_content_changed,
        "statement": "重导覆盖、单条补录、校正使 answers.fingerprint 变化后，旧版本上的核验结论、"
                     "错因判断、追问证据与成绩全部退出当前视图（行保留可追溯），该份回到待核验/未评分。",
        "executor": "answer_check.update_content() / answer_import_zip 覆盖提交",
        "counter_proof": "把指纹比较改成恒 False（即「内容必变」），同指纹重导也必须失效 → 断言必须变红。",
    },
    {
        "id": "R2-INV-02",
        "name": "撤回核验结论",
        "trigger": "check_retracted",
        "effect": _inv_02_check_retracted,
        "statement": "撤回 = answer_version 置空 + invalidated_*（不新增「失效」布尔）；该条退出核验与"
                     "错因统计，评分独立保留；同一作答同一版本只保留一条当前结论，不堆重复行。",
        "executor": "answer_check._retract()",
        "counter_proof": "把撤回行的 answer_version 改回原值，唯一索引与「当前结论」查询同时变红。",
    },
    {
        "id": "R2-INV-03",
        "name": "撤回错因判断",
        "trigger": "cause_retracted",
        "effect": _inv_03_cause_retracted,
        "statement": "撤回错因只让该份退出错因统计（cause_state=undecided），核验结论与已确认成绩"
                     "各自独立保留，不连带撤销。",
        "executor": "answer_check.save()（cause_state 改判/撤回）",
        "counter_proof": "把撤回连带撤成绩写进实现，R2-02「撤 1 条错因 → 核验与评分仍在」必红。",
    },
    {
        "id": "R2-INV-04",
        "name": "评分草稿与正式口径",
        "trigger": "score_saved",
        "effect": _inv_04_score_saved,
        "statement": "正式得分必须教师显式确认（confirmed）；草稿（draft）不计入平均分、得分率、"
                     "完整成绩与评分覆盖率，只用于回显。",
        "executor": "grading.save_score()",
        "counter_proof": "让统计把 draft 也算进去，R2-03「部分评分不计入平均」断言必红。",
    },
    {
        "id": "R2-INV-05",
        "name": "评分标准或满分变化",
        "trigger": "rubric_changed",
        "effect": _inv_05_rubric_changed,
        "statement": "同批次同题的评分标准/满分一变，该题既有成绩置 invalidated 并要求重评；旧分数、"
                     "旧扣分项、旧标准版本全部留历史，不覆盖。",
        "executor": "grading.update_rubric()（写 assignment_batches.rubric_version）",
        "counter_proof": "不改 rubric_version 只改标准正文，R2-02「改标准 → 旧成绩失效」必红。",
    },
    {
        "id": "R2-INV-06",
        "name": "撤回成绩",
        "trigger": "score_retracted",
        "effect": _inv_06_score_retracted,
        "statement": "撤回成绩 = 回未评分 + 退出正式统计（平均分/得分率/覆盖率/完整成绩）；同一版本的"
                     "撤回行留历史，重评写新行。",
        "executor": "grading.retract_score()",
        "counter_proof": "撤回后仍按 confirmed 计数，R2-03「撤回成绩 → 退出正式统计」必红。",
    },
    {
        "id": "R2-INV-07",
        "name": "步骤扣分与总分一致性",
        "trigger": None,
        "effect": _inv_07_step_deductions_mismatch,
        "statement": "步骤扣分项之和必须满足「满分 − Σ扣分 = 录入分数」，不一致一律拒绝保存（不改库），"
                     "并在界面提示差额。",
        "executor": "grading.save_score() 前置校验 step_deductions_consistent()",
        "counter_proof": "去掉一致性校验后同一请求返回 200，R2-02「步骤分一致性拦截」必红。",
    },
    {
        "id": "R2-INV-08",
        "name": "教学资源版本变化",
        "trigger": "resource_version_changed",
        "effect": _inv_08_resource_version_changed,
        "statement": "资源出新版本或被停用后，引用旧版本的推荐/提示/任务标「依据已变，待复核」，"
                     "不静默替换成新版本，也不删除旧引用。",
        "executor": "teaching_resources.publish_version() / grading 建议匹配",
        "counter_proof": "把引用改成「总读最新版本」，R2-05「历史版本与任务引用可追溯」必红。",
    },
    {
        "id": "R2-INV-09",
        "name": "教学任务依据变化",
        "trigger": "task_basis_changed",
        "effect": _inv_09_task_basis_changed,
        "statement": "任务的关联依据（错因/知识点/统计范围）变化后只打待复核标记，任务状态不动、"
                     "不自动改派、不自动完成。",
        "executor": "teaching_resources.mark_task_review()",
        "counter_proof": "依据变化后自动把任务置 cancelled，R2-05「任务历史/状态可追溯」必红。",
    },
    {
        "id": "R2-INV-10",
        "name": "批次配置变化",
        "trigger": "batch_config_changed",
        "effect": _inv_10_batch_config_changed,
        "statement": "应作答题号/应评分题范围变化只重算口径与完整成绩判定（原本完整的可能变不完整，"
                     "反之亦然）；已有成绩不因题号范围变化而失效，因满分/标准变化失效的走第 ⑤ 条。",
        "executor": "db.save_batch_config()",
        "counter_proof": "把「完整成绩」判成「有至少一题成绩」，R2-03/R2-04 的完整成绩断言必红。",
    },
    {
        "id": "R2-INV-11",
        "name": "批次与演示数据归属",
        "trigger": "batch_scope_changed",
        "effect": _inv_11_batch_scope_changed,
        "statement": "统计一律按 (class_id, batch) 作用域；同名历史批次不迁就、不改名、不合并，"
                     "迁移只登记归属；合成演示数据不进正式统计与成绩。",
        "executor": "db.ensure_r2_schema() 登记 / r2_stats 查询条件",
        "counter_proof": "去掉 source<>'合成演示' 或按批名合并同名批次，R2-03 的演示隔离断言必红。",
    },
)

INVALIDATION_RULE_IDS = tuple(rule["id"] for rule in INVALIDATION_RULES)
INVALIDATION_TRIGGERS = {
    "answer_content_changed": "作答内容变化（重导/补录/校正）",
    "check_retracted": "撤回核验结论",
    "cause_retracted": "撤回错因判断",
    "score_saved": "评分落库（草稿/确认）",
    "rubric_changed": "评分标准或满分变化",
    "score_retracted": "撤回成绩",
    "resource_version_changed": "教学资源版本变化",
    "task_basis_changed": "教学任务依据变化",
    "batch_config_changed": "批次配置变化",
    "batch_scope_changed": "批次归属作用域重算",
}


def rules_for(trigger):
    """取某个触发词对应的规则（按 INVALIDATION_RULES 顺序）。"""
    return tuple(rule for rule in INVALIDATION_RULES if rule["trigger"] == trigger)


def empty_state():
    """四维状态快照的空值（各模块加载数据时按这个形状填，不要另起字段名）。"""
    return {
        DIM_ANSWER: {"state": "unsubmitted", "fingerprint": ""},
        DIM_VERIFY: {"state": "unverified", "version": ""},
        DIM_CAUSE: {"state": "undecided", "code": "", "label": ""},
        DIM_SCORE: {"state": "unscored", "formal": False, "rubric_version": ""},
    }


def apply_event(trigger, state, event=None, rules=None):
    """执行一次失效事件：返回 `{"state", "actions", "rules"}`（纯函数，不写库）。

    - `state`：四维状态快照（形状见 `empty_state()`，多余键原样保留）；
    - `event`：事件载荷（如 `{"fingerprint": "..."}`）；
    - `rules`：可注入的规则集（默认 `INVALIDATION_RULES`）。**测试用它做反向自证**：
      把某条规则摘掉后，同一断言必须变红，证明断言不是恒真。
    落库由调用方按 `actions` 执行（`actions` 只是动作名，不含 SQL，避免契约层绑死实现）。
    """
    event = dict(event or {})
    rule_set = INVALIDATION_RULES if rules is None else tuple(rules)
    current = {key: dict(value or {}) for key, value in (state or {}).items()}
    for key in empty_state():
        current.setdefault(key, dict(empty_state()[key]))
    actions = []
    applied = []
    for rule in rule_set:
        if rule.get("trigger") != trigger:
            continue
        effect = rule.get("effect")
        if effect is None:
            continue
        plan = effect(current, event)
        if not plan:
            continue
        for dimension, change in (plan.get("states") or {}).items():
            bucket = current.setdefault(dimension, {})
            bucket.update(change or {})
        actions.extend(plan.get("actions") or ())
        applied.append(rule["id"])
    return {"state": current, "actions": tuple(dict.fromkeys(actions)), "rules": tuple(applied)}


# ---------------------------------------------------------------------------
# 八、数据隔离与 AI 外发边界（派单 §2.6）
# ---------------------------------------------------------------------------
# 口径（与 R1 契约 §8 一致，只在 R2 侧收紧，不放松）：
#   1. 生成本身一律本地规则：批量核验、错因候选、评分建议、统计、资源匹配都不调用生成式模型；
#   2. 真实学生作答**永不**外发——R2 新增的流程即使 `users.ai_individual_allowed=1` 也拒绝，
#      开关只对 R1 既有的错题照片分析生效（那条路径的行为一字不改）；
#   3. 外发前必须写审计（ai_egress_audit），审计只记字段形状，不记取值。
EGRESS_DECISION_DENY = "deny"
EGRESS_DECISION_LEGACY_SWITCH = "legacy_switch"

AI_FLOWS = {
    # R1 既有：错题照片分析（受账号级开关约束，行为不变）
    "mistake_photo": {"decision": EGRESS_DECISION_LEGACY_SWITCH,
                      "note": "R1 既有链路：provider 已授权 + ai_individual_allowed=1 才发；"
                              "mock 通道永不读图"},
    # R2 新增：一律本地规则，不读开关
    "answer_check_grading": {"decision": EGRESS_DECISION_DENY,
                             "note": "批量核验/评分建议：本地规则，真实作答不外发"},
    "answer_check_cause": {"decision": EGRESS_DECISION_DENY,
                           "note": "错因候选与确认：本地规则 + 已审资源库，不外发"},
    "class_stats_summary": {"decision": EGRESS_DECISION_DENY,
                            "note": "统计与报告文案：本地计算，不外发"},
    "teaching_resource_match": {"decision": EGRESS_DECISION_DENY,
                                "note": "建议匹配/三级提示：本地检索已发布资源，不外发"},
}
# 任何 R2 流程都不许出现的字段（即使将来有人误开了外发，也在组装前拦下）
EGRESS_FORBIDDEN_FIELDS = (
    "student_name", "student_code", "class_name", "record_key",
    "student_steps", "question_text", "observed_step", "teacher_error_text",
    "answer_text", "image_bytes", "answer_id", "batch", "account",
)


def egress_decision(flow, individual_allowed=False):
    """R2 外发闸门（纯函数）：返回 {"allowed","code","message","flow"}。

    R2 流程**恒**返回 allowed=False（哪怕 individual_allowed=True）——派单 §2.6 要求
    「真实学生作答相关的批改/错因更新一律本地规则」，所以开关对它无效；
    只有 R1 既有的 `mistake_photo` 沿用旧口径（开关开才允许）。
    """
    spec = AI_FLOWS.get(str(flow or ""))
    if spec is None:
        return {"allowed": False, "code": "unknown_flow", "flow": flow,
                "message": "未知的外发流程，一律拒绝"}
    if spec["decision"] == EGRESS_DECISION_DENY:
        return {"allowed": False, "code": "ai_isolation", "flow": flow,
                "message": "R2 流程一律本地规则，真实学生作答不外发（%s）" % spec["note"]}
    allowed = bool(individual_allowed)
    return {"allowed": allowed, "code": "" if allowed else "ai_isolation", "flow": flow,
            "message": "R1 既有链路：需账号级开关，当前%s" % ("已开启" if allowed else "未开启")}


def egress_field_problem(fields):
    """检查待外发字段里有没有 R2 禁止的字段；返回违规字段列表（空 = 可以继续）。"""
    names = [str(item or "").strip() for item in (fields or [])]
    return [name for name in names if name in EGRESS_FORBIDDEN_FIELDS]


def include_in_formal_stats(source, batch_is_synthetic=False):
    """这一行是否计入**正式**统计（演示数据一律排除；纯演示批次整体排除）。

    R1 的教训（R1-08）：只按批名判会把老师填成演示批名的真实作答整批排除，
    所以这里两个条件分开：来源是合成演示 → 排除；批次是纯演示批次 → 排除；
    真实作答即使批名撞了演示批名，也仍然计入（`batch_is_synthetic` 必须是
    「批名命中且该批次没有真实行」的结果，由 `answer_check._batch_is_synthetic` 给出）。
    """
    if str(source or "").strip() == DEMO_SOURCE:
        return False
    return not bool(batch_is_synthetic)


# ---------------------------------------------------------------------------
# 九、18 项统计口径（派单 §5.1，R2-03 按此实现，R2-04 复用同一份）
# ---------------------------------------------------------------------------
# 每条 = 一个指标 key + 中文名 + 定义 + 分子/分母 + 用了哪几个维度的哪些状态 + 缺数据时怎么显示。
# 硬口径（对应派单 §5.1 要求，R2-03/R2-04 不得自行改写）：
#   - 多知识点/多错因按类别分别计数，同类别内去重（同一条错题同一知识点只计一次）；
#   - 成绩只用**已确认**成绩；不合并不可比的满分/标准/批次；不完整成绩不进平均分；
#   - 「作答人数」= 至少有一条**非缺测**记录的去重学生数（5 题全缺测的学生不算作答）；
#   - 「作业总分」= **逐生**有效确认得分之和（不是满分之和；不完整不给值）；
#   - 「评分覆盖率」分母 = 应参与评分学生数（不是应作答人数）；
#   - 缺分母显示「暂无/无法计算」，绝不拿 0 冒充；历史错题不用于推算正确率或掌握率；
#   - 正式统计排除合成演示数据。
R2_STAT_METRICS = (
    {"key": "class_students", "label": "班级人数",
     "definition": "**当前统计范围内**的班级学生人数（在册 `students.active=1`）。"
                   "两条可断言约束：① 知识点 / 错因筛选**不改变**班级人数（这两类只作用于"
                   "错题分布、涉及学生数与证据明细）；② 学生筛选**改变**班级人数（取该学生群体"
                   "在册人数）。换班级 = 换整套分母",
     "numerator": "统计范围内在册学生数", "denominator": "—",
     "states": ("students.active=1",), "empty": "空班显示 0"},
    {"key": "expected_students", "label": "应作答人数",
     "definition": "批次配置 expected_students 去重后的人数；未配置名单显示暂无",
     "numerator": "应作答学生数", "denominator": "—",
     "states": ("batch.config_state=configured",), "empty": "未配置名单 → 暂无"},
    {"key": "submitted_students", "label": "作答人数",
     "definition": "该批次内**至少有一条非缺测作答**的去重学生数（排除合成演示）："
                   "只有缺测记录的学生不算作答（例如 5 题全标缺测，作答人数里不该出现他）；"
                   "内容不足（仅照片）算作答，不算有效核验内容",
     "numerator": "有非缺测记录的学生数", "denominator": "—",
     "states": ("answer in (valid, insufficient)",), "empty": "无记录 → 0"},
    {"key": "valid_answers", "label": "有效作答数",
     "definition": "**非缺测**且作答维为 valid 的记录数（有可核验文本/步骤；「缺测」行不计入，"
                   "仅照片的「内容不足」也不计入有效）",
     "numerator": "有效（非缺测）记录数", "denominator": "—",
     "states": ("answer=valid",), "empty": "0"},
    {"key": "insufficient_answers", "label": "内容不足数",
     "definition": "记录级：仅照片或无过程，无法核验（不实现手写 OCR）",
     "numerator": "insufficient 记录数", "denominator": "—",
     "states": ("answer=insufficient",), "empty": "0"},
    {"key": "missing_answers", "label": "缺测数",
     "definition": "记录级：answers.missing=1 的记录数（请假/未测），与「未提交」严格区分",
     "numerator": "missing 记录数", "denominator": "—",
     "states": ("answer=missing",), "empty": "0"},
    {"key": "unsubmitted_answers", "label": "未提交数",
     "definition": "应作答名单 × 应作答题号 中完全没有记录的格子数（应作答格子数 − 有记录格子数）",
     "numerator": "缺格数", "denominator": "应作答格子数",
     "states": ("answer=unsubmitted",), "empty": "未配置名单/题号 → 暂无"},
    {"key": "unverified_answers", "label": "未核验数",
     "definition": "有效作答中当前版本还没有核验结论的份数",
     "numerator": "verify=unverified 的份数", "denominator": "有效作答数",
     "states": ("verify=unverified",), "empty": "0"},
    {"key": "confirmed_mistakes", "label": "已确认错题数",
     "definition": "当前版本核验为 first_bad 且错因已由教师确认（cause=decided）的作答数；"
                   "与 R1 的 original_errors（原题错误数）/cause_decided 分列，不合并口径",
     "numerator": "已确认错题数", "denominator": "—",
     "states": ("verify=verified_rule/verified_manual", "cause=decided"), "empty": "0"},
    {"key": "knowledge_error_freq", "label": "知识点错误频次",
     "definition": "已确认错题按知识点分别计数；一条错题多个知识点分别计，同一错题同一知识点只计一次",
     "numerator": "按知识点分组的错题数", "denominator": "—",
     "states": ("cause=decided",), "empty": "无 → 空列表（不是 0 图表）"},
    {"key": "affected_students", "label": "涉及学生人数",
     "definition": "至少有一条已确认错题的学生数（同一学生多条只计一次）",
     "numerator": "有错题的学生数", "denominator": "班级人数",
     "states": ("cause=decided",), "empty": "0"},
    {"key": "cause_freq", "label": "错因频次",
     "definition": "已确认错题按 cause_code 分别计数；多错因分别计，同类去重",
     "numerator": "按错因分组的错题数", "denominator": "—",
     "states": ("cause=decided",), "empty": "无 → 空列表"},
    {"key": "undecided_causes", "label": "待判断错因",
     "definition": "当前版本核验为 first_bad、但错因仍待判断（cause=undecided）的份数",
     "numerator": "待判断份数", "denominator": "first_bad 份数",
     "states": ("cause=undecided",), "empty": "0"},
    {"key": "question_score_rate", "label": "单题得分率",
     "definition": "该题已确认成绩之和 ÷（已确认份数 × 该题满分）；同时返回 scored_count（已评分数）",
     "numerator": "已确认成绩之和", "denominator": "已确认份数 × 满分",
     "states": ("score=confirmed",), "empty": "无已评 → 暂无"},
    {"key": "assignment_total_score", "label": "作业总分",
     "definition": "**逐生得分**：某一学生全部应评分题目的**有效确认得分之和**（键为 student_id，"
                   "`assignment_total_score()` / `student_total_scores()`）；"
                   "缺任何一题就**不给值**（不得用 0 或已判题目的部分和顶替）；"
                   "各题满分与评分标准不可比时不合并。满分之和不是得分，另见 supplement.batch_full_score",
     "numerator": "逐生有效确认得分之和", "denominator": "—",
     "states": ("score=confirmed",), "empty": "该生不完整/未配置 → 暂无（不给 0）"},
    {"key": "average_score", "label": "平均分",
     "definition": "**完整成绩**学生的确认总分平均；不完整成绩一分都不计入；不合并不同满分/标准/批次",
     "numerator": "完整成绩总分之和", "denominator": "完整成绩学生数",
     "states": ("score=confirmed",), "empty": "无完整成绩 → 暂无"},
    {"key": "score_coverage", "label": "评分覆盖率",
     "definition": "已确认且完整成绩的学生数 ÷ **应参与评分学生数**（= 批次配置中至少有一道"
                   "应评分题目的学生数去重；无应评分题目时显示无法计算）。"
                   "分母不是「应作答人数」——某学生只有不参与评分的题目时，用应作答人数会把"
                   "覆盖率算低，与派单 §5.1 口径不符",
     "numerator": "完整成绩学生数", "denominator": "应参与评分学生数",
     "states": ("score=confirmed",), "empty": "未配置/无应评分题目 → 无法计算"},
    {"key": "repeated_errors", "label": "重复错误",
     "definition": "同一学生**不同作答中重复出现**的两类对数之和："
                   "① `knowledge_pairs`（同一学生同一知识点 ≥2 条已确认错题）；"
                   "② `cause_pairs`（同一学生同一确认错因 ≥2 次）。明细必须分开看"
                   "（`R2_STAT_REPEATED_ERROR_PARTS`，计数用 `repeated_pairs()`）；"
                   "同一错题多知识点/多错因按类别分别计、同类别内去重；"
                   "历史错题不用于推算正确率或掌握率",
     "numerator": "knowledge_pairs 对数 + cause_pairs 对数", "denominator": "—",
     "states": ("cause=decided",), "empty": "两类都无 → 0（明细给空列表）"},
)

# 第 15/18 项的配套口径：不是新的「18 项」，而是同一组指标里必须能分开看的部分。
# R2-03 的看板与 R2-04 的报告都按这里的 key 出明细，避免各自另起名字。
R2_STAT_SUPPLEMENTARY = (
    {"key": "batch_full_score", "label": "满分合计（非得分）",
     "definition": "应评分题满分之和（`batch_full_score()`）；只用于展示「本卷多少分」，"
                   "**不得**参与平均分、得分率、覆盖率或「作业总分」"},
    {"key": "scored_count", "label": "已评分数",
     "definition": "单题得分率的分母口径配套值：该题已确认成绩的份数"},
)
R2_STAT_REPEATED_ERROR_PARTS = (
    {"key": "knowledge_pairs", "label": "知识点重复",
     "definition": "同一学生同一知识点上 ≥2 条已确认错题的对数（`repeated_pairs()`，"
                   "输入项的 category 取知识点）"},
    {"key": "cause_pairs", "label": "错因重复",
     "definition": "同一学生同一确认错因出现 ≥2 次的对数（`repeated_pairs()`，"
                   "输入项的 category 取 cause_code）"},
)

# 两项「排序结论」不是数值口径，靠上面的指标排序得出；R2-03 的看板与 R2-04 的报告
# 都用这两个 key，避免各自另起名字。
R2_STAT_RANKINGS = (
    {"key": "class_common_issues", "label": "班级共性问题",
     "definition": "先按**涉及学生人数（去重）**降序；人数相同再按频次降序；仍相同按知识点名"
                   "稳定排序（任何调用顺序、任何进程重启结果一致）。"
                   "**频次只作辅助，不得作为主排序键**；附涉及学生数与示例错题"},
    {"key": "student_priority_issues", "label": "个人重点问题",
     "definition": "**复用 `app/tutor.py` 的近期加权排序**（`summary()` 的既有口径）："
                   "近 30 天每次 3 分、31–90 天每次 2 分、超 90 天每次 1 分"
                   "（`WEIGHT_RECENT=3` / `WEIGHT_MID=2` / `WEIGHT_OLD=1`，"
                   "`RECENT_DAYS=30` / `MID_DAYS=90`）；日期缺失不参与加权，未来日期按异常不计入；"
                   "未分类（unclassified）排最后。**不得另立一套排序、不得复制这些常量**："
                   "以 `app/tutor.py` 的同名常量为唯一来源，原版调值时 R2 必须跟着变"},
)

# 筛选口径（派单 §5.2）：哪些筛选会改变哪些指标的分母。
R2_STAT_FILTER_RULES = (
    {"filter": "class", "affects": "全部指标", "note": "换班级 = 换整套分母"},
    {"filter": "batch", "affects": "全部指标", "note": "只算该批次，不跨批次相加"},
    {"filter": "knowledge_point", "affects": "知识点/错因/错题明细类指标",
     "note": "默认**不**改变班级人数、作业总分、平均分、评分覆盖率"},
    {"filter": "cause", "affects": "错因/错题明细类指标",
     "note": "同上；若界面要展示「筛选群体成绩」，必须单独注明范围"},
)

# 缺分母时的统一文案（R2-03/R2-04 共用，避免一个说「暂无」一个说「0」）
STAT_EMPTY_TEXT = "暂无"
STAT_UNDEFINED_TEXT = "无法计算"


# ---------------------------------------------------------------------------
# 十、R1 遗留问题的 R2 归属（派单 §2.7）
# ---------------------------------------------------------------------------
R1_LEGACY_OWNERSHIP = (
    {"id": "C1", "problem": "统计块不能自选批次（只能看当前作业）", "owner": "R2-03",
     "task": "t5", "closed_by": "r2_stats 支持批次参数 + 看板批次选择器"},
    {"id": "C2", "problem": "records 不传批次时包含演示批次（记录页混演示）", "owner": "R2-03",
     "task": "t5", "closed_by": "正式统计显式排除演示数据，界面标注演示来源"},
    {"id": "C6", "problem": "统计读快照、详情读实时，两处口径可能不一致", "owner": "R2-03",
     "task": "t5", "closed_by": "统计与详情共用同一「当前版本」判定（answer_version=fingerprint）"},
    {"id": "U2", "problem": "历史 decided 行与新 applicability 可能冲突", "owner": "R2-02",
     "task": "t3", "closed_by": "改判/撤回统一走 cause_history，校验当前结论适用性"},
    {"id": "K1", "problem": "新同步到 mistakes 的错题缺知识点分类或分类无依据", "owner": "R2-02",
     "task": "t3", "closed_by": "写入时保存有效知识点分类与可追溯依据，缺依据标未分类"},
    {"id": "K2", "problem": "新旧撤回口径不一致（原版错题撤回 vs 作答核对撤回）", "owner": "R2-02",
     "task": "t3", "closed_by": "两处撤回沿用同一版本闸门与 invalidated_* 留痕"},
    {"id": "K3", "problem": "历史判断与当前作答适用性冲突（旧结论套新内容）", "owner": "R2-02",
     "task": "t3", "closed_by": "失效规则 R2-INV-01：指纹一变旧结论一律退出当前视图"},
    {"id": "L1", "problem": "已审错因库/追问库（library/curated）没有数据来源", "owner": "R2-05",
     "task": "t7", "closed_by": "教学资源库 + 双审核 + 版本管理"},
    {"id": "L2", "problem": "三级提示与「学生独立尝试」没有记录", "owner": "R2-05",
     "task": "t7", "closed_by": "hint_usages 记录级别/独立尝试/修正结果/完整解析时机"},
    {"id": "P1", "problem": "学情报告只能看网页，不能导出存档", "owner": "R2-04",
     "task": "t6", "closed_by": "预览与导出同快照 + A4 模板 + 导出历史"},
    {"id": "V1", "problem": "版本号仍是 1.8.1", "owner": "captain（R2-06）",
     "task": "t7", "closed_by": "统一改 src/app/version.py 与打包产物"},
)


# ---------------------------------------------------------------------------
# 十一、迁移方案（1.8.1 / R1 → 1.9.0）
# ---------------------------------------------------------------------------
# 三条硬约束（对应派单 §2.8 的迁移要求）：
#   1. **保留原有关联**：不重建表、不删列、不改既有行；新表全部以既有主键为外键；
#   2. **同名历史批次维持原归属**：迁移只按 (class_id, batch) 登记配置行，不改写任何
#      answers.batch；同名不同批、跨班同名都各算各的（不合并、不加前缀）；
#   3. **幂等**：`db.ensure_r2_schema()` 二次调用返回 None 且库内一行不动。
MIGRATION_STEPS = (
    "只读检查：r2_schema_ready() 判表/索引/关键列是否齐备；齐备直接返回 None",
    "一致性备份：sqlite3 online backup 到 app.db.bak-<时间戳>-<随机>（失败删残片并抛 MigrationError）",
    "BEGIN IMMEDIATE：建 9 张 R2 新表与索引（全部 CREATE ... IF NOT EXISTS）",
    "登记历史批次：SELECT DISTINCT class_id, batch FROM answers WHERE batch<>'' → 插 assignment_batches"
    "（origin=legacy, config_state=unconfigured；已存在的跳过，绝不改写 answers.batch）",
    "COMMIT；复核 r2_schema_ready()，不就绪抛 MigrationError（原库已被备份，可回退）",
    "不改 SCHEMA_VERSION（仍为 4）；不 DROP/ALTER 任何 R1 既有列；不建任何未启用字段的预留表",
)

# 迁移时的历史批次缺省配置：只登记归属，不猜名单/满分（猜出来的口径会污染统计）
def legacy_batch_config(class_id, batch, stamp=""):
    """迁移登记用：给历史批次生成一份「未配置」的缺省配置（纯数据，不写库）。"""
    name = str(batch or "").strip()
    return {
        "class_id": int(class_id),
        "batch": name,
        "name": name,
        "occurred_on": "",
        "expected_students": [],
        "expected_questions": [],
        "scored_questions": [],
        "full_scores": {},
        "rubric": {},
        "rubric_version": "",
        "config_state": BATCH_CONFIG_STATE_UNCONFIGURED,
        "origin": BATCH_ORIGIN_LEGACY,
        "created_at": stamp,
        "updated_at": stamp,
    }


# 历史批次「维持原归属」的判据（R2-01 导入与 R2-03 统计都得按这套判，不许自己发明）
def same_batch_scope(class_a, batch_a, class_b, batch_b):
    """两个「批次」是不是同一个作用域：同班级 + 批次名逐字相等（不做大小写/空白归一）。

    为什么要逐字比较：R1 的 answers.batch 是老师自由填写的字符串，历史上已经存在
    「同名不同班」「同班同名不同次」两种情形；R2 迁移不做任何改名或合并，统计与导入
    也一律按这个二元组判作用域，避免把两份作业悄悄并成一份。
    """
    return int(class_a) == int(class_b) and str(batch_a or "") == str(batch_b or "")


def is_current_version(answer_version, fingerprint):
    """R1 起就有的「当前结论」判据：answer_version 与 answers.fingerprint 相等。

    空版本（撤回行）永远不是当前版本；指纹为空（老库未算指纹）时只认两边都为空的情况。
    """
    version = str(answer_version or "")
    current = str(fingerprint or "")
    if not version:
        return False
    return version == current


__all__ = [
    "TARGET_APP_VERSION", "R2_CONTRACT_VERSION", "R2_SCHEMA_VERSION_UNCHANGED",
    "R2_TABLE_NAMES", "DEMO_SOURCE", "DEMO_RECORD_PREFIX",
    "BATCH_ORIGIN_LEGACY", "BATCH_ORIGIN_IMPORT", "BATCH_ORIGIN_MANUAL", "BATCH_ORIGINS",
    "BATCH_CONFIG_STATE_CONFIGURED", "BATCH_CONFIG_STATE_UNCONFIGURED", "BATCH_CONFIG_STATES",
    "DIM_ANSWER", "DIM_VERIFY", "DIM_CAUSE", "DIM_SCORE", "DIMENSIONS",
    "ANSWER_STATES", "VERIFY_STATES", "CAUSE_STATES", "SCORE_STATES", "STATES", "STATE_LABELS",
    "RESOURCE_STATES", "RESOURCE_KINDS", "RESOURCE_REVIEW_KINDS", "TASK_STATES",
    "TASK_REVIEW_STATES", "HINT_LEVELS", "HINT_LEVEL_ORDER",
    "STATE_TRANSITIONS", "STATE_GATES", "transition_allowed", "transition_state",
    "LEGACY_STATE_MAP", "R1_UNCHANGED_FIELDS",
    "BATCH_CONFIG_KEYS", "DEFAULT_BATCH_CONFIG", "BATCH_CONFIG_CODES",
    "normalize_batch_config", "batch_config_errors", "expected_question_nos",
    "scored_question_nos", "expected_student_keys", "full_score_of", "batch_full_score",
    "assignment_total_score", "student_total_scores", "repeated_pairs",
    "classify_record_state", "roster_status", "scoring_students", "score_completeness",
    "MAX_SCORE_DECIMALS", "SCORE_ERROR_CODES", "validate_score", "step_deductions_consistent",
    "INVALIDATION_RULES", "INVALIDATION_RULE_IDS", "INVALIDATION_TRIGGERS", "rules_for",
    "empty_state", "apply_event",
    "AI_FLOWS", "EGRESS_FORBIDDEN_FIELDS", "egress_decision", "egress_field_problem",
    "include_in_formal_stats",
    "R2_STAT_METRICS", "R2_STAT_RANKINGS", "R2_STAT_FILTER_RULES",
    "R2_STAT_SUPPLEMENTARY", "R2_STAT_REPEATED_ERROR_PARTS",
    "STAT_EMPTY_TEXT", "STAT_UNDEFINED_TEXT",
    "R1_LEGACY_OWNERSHIP", "MIGRATION_STEPS", "legacy_batch_config", "same_batch_scope",
    "is_current_version",
]
