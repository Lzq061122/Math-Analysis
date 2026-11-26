"""SQLite 存储层：建表、连接管理、通用查询辅助。

所有时间统一存为带时区偏移的 ISO-8601 字符串（例如 2026-11-06T11:54:48+08:00），
字典序即时间序，便于按时间筛选。
"""

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

SCHEMA_VERSION = "4"

# ---- 合成演示作答的存储级标识（与 demo_answers 共用同一份常量，避免两处各写一个字符串）----
# 三个一起构成「这条作答确实是演示写入的」的结构标记：
#   source  = 固定来源标记（CSV 的「来源批次」列也能写这五个字，所以它只是第一道条件）
#   record_key 以 DEMO- 开头（老师给真实作答编这种编号的可能性可以忽略）
#   batch   = 演示专用批次名，真实导入不可能自然取到
# 下面的触发器把「合成作答只能待在演示批次」变成库级不变量：
# 任何绕过 seed 直接写进真实批次的演示行，会在写入后立即被归位到演示批次，
# 于是「真实作业的应交 / 待核对 / 原题错误数」无论由哪段代码统计都不会被演示数据污染。
# 代价（已知且可接受）：真实导入若**同时**把来源列写成「合成演示」、编号又以 DEMO- 开头，
# 会被当成演示数据归位并被 reset 清掉；这两个条件要人为同时凑出来才会发生，
# 而只撞其中一个（只有来源、或只有前缀、或只撞批名）都不会被删。
DEMO_SOURCE = "合成演示"
DEMO_BATCH = "【合成演示】不计入真实作业"
DEMO_RECORD_PREFIX = "DEMO-"

# 作答记录：老师用 CSV 批量导入的「一份作答」，与错题分开存。
# 错题表带审核状态（pending/confirmed/returned），导入的作答还没有经过审核，
# 混进去会把班级统计的待核对/已确认数量算错，也会把「缺测」和「AI 判定信息不足」混为一谈。
# record_key 是老师给的稳定编号，同一个班级里唯一，用来识别重复导入。
# source 是「这条作答从哪来」：CSV 的「来源批次」列写什么就是什么，**不可以用它单独判定真伪**
# （真实数据也能写成「合成演示」）。合成演示作答另有两条结构标记：record_key 带 `DEMO-` 前缀、
# batch 固定为 DEMO_BATCH；核对查询一律按 (batch, source) 双重收口，
# 所以 idx_answers_source 存在的意义就是让这类「按来源收口」的查询不必全表扫描。
ANSWERS_DDL = (
    """CREATE TABLE IF NOT EXISTS answers (
    id                INTEGER PRIMARY KEY,
    class_id          INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id        INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    record_key        TEXT NOT NULL,
    batch             TEXT NOT NULL DEFAULT '',
    student_name      TEXT NOT NULL DEFAULT '',
    student_code      TEXT NOT NULL DEFAULT '',
    question_no       TEXT NOT NULL DEFAULT '',
    question_text     TEXT NOT NULL DEFAULT '',
    unit              TEXT NOT NULL DEFAULT '',
    standard_solution TEXT NOT NULL DEFAULT '',
    student_steps     TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT '',
    occurred_on       TEXT NOT NULL DEFAULT '',
    missing           INTEGER NOT NULL DEFAULT 0,
    missing_reason    TEXT NOT NULL DEFAULT '',
    fingerprint       TEXT NOT NULL DEFAULT '',
    imported_at       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_answers_class_record ON answers(class_id, record_key)",
    "CREATE INDEX IF NOT EXISTS idx_answers_student ON answers(student_id)",
    "CREATE INDEX IF NOT EXISTS idx_answers_batch ON answers(class_id, batch)",
    "CREATE INDEX IF NOT EXISTS idx_answers_source ON answers(class_id, source)",
)

# T6：两个「演示批次归位」触发器（trg_answers_demo_batch_ins / _upd）已删除。
# 理由（船长裁定）：它们是隐形行为——写入时悄悄改 batch，谁也说不清某一行为什么换了作业；
# 而且它们是**唯一**防线时，绕过触发器（或旧库还没建触发器）统计就会被演示数据污染。
# 现在改为显式口径：answer_check 的每条查询按 (batch, source) 收口，stats.answer_check_stats
# 也显式 `AND a.source<>'合成演示'`；迁移期的一次性归位（ensure_answers_schema）保留，
# 旧库里演示写入的行仍然会被挪回演示批次，但那是**一次可见的迁移**，不是每次写入都发生的魔法。
# 旧库若已建过触发器，打开时由 Database._ensure_columns() 显式 DROP（见那里的注释）。

# 作答审核：老师针对一条作答发起一次 AI 分析后留下的候选错因。
# 建议本身不是错题；只有老师确认时才生成正式错题，并把 mistake_id 记回来。
# answer_fingerprint 记住分析时的作答内容，确认前再核对一次，防止拿旧建议套新内容。
REVIEWS_DDL = (
    """CREATE TABLE IF NOT EXISTS answer_reviews (
    id                 INTEGER PRIMARY KEY,
    class_id           INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id         INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    answer_id          INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
    status             TEXT NOT NULL DEFAULT 'pending',
    provider           TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT '',
    causes             TEXT NOT NULL DEFAULT '[]',
    note               TEXT NOT NULL DEFAULT '',
    uncertain          INTEGER NOT NULL DEFAULT 0,
    raw                TEXT NOT NULL DEFAULT '',
    answer_fingerprint TEXT NOT NULL DEFAULT '',
    mistake_id         INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    decided_at         TEXT NOT NULL DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_reviews_answer ON answer_reviews(answer_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_answer_confirmed"
    " ON answer_reviews(answer_id) WHERE status='confirmed'",
)

# 个人辅导建议：老师针对一名学生的已确认错题，让 AI 给出候选建议后由老师改定。
# 一名学生只保留当前一份；evidence 存生成时的问题汇总快照与错题编号，
# fingerprint 用来发现「生成之后又新增或改动了已确认错题」，据此提示可能需要更新。
TUTOR_DDL = (
    """CREATE TABLE IF NOT EXISTS tutor_advice (
    id             INTEGER PRIMARY KEY,
    class_id       INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id     INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    content        TEXT NOT NULL DEFAULT '',
    ai_content     TEXT NOT NULL DEFAULT '',
    evidence       TEXT NOT NULL DEFAULT '{}',
    fingerprint    TEXT NOT NULL DEFAULT '',
    teacher_edited INTEGER NOT NULL DEFAULT 0,
    mode           TEXT NOT NULL DEFAULT '',
    generated_at   TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tutor_student ON tutor_advice(student_id)",
    "CREATE INDEX IF NOT EXISTS idx_tutor_class ON tutor_advice(class_id)",
)

# 教师短追问：一次实际提问一条记录，关联学生、问题与来源错题/作答；
# 问题证据快照与 AI 候选追问都留在 suggest 里，便于回看当时依据。
FOLLOWUP_DDL = (
    """CREATE TABLE IF NOT EXISTS followups (
    id             INTEGER PRIMARY KEY,
    class_id       INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id     INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    problem        TEXT NOT NULL DEFAULT '',
    mistake_id     INTEGER REFERENCES mistakes(id) ON DELETE SET NULL,
    review_id      INTEGER REFERENCES answer_reviews(id) ON DELETE SET NULL,
    question       TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'teacher',
    suggest        TEXT NOT NULL DEFAULT '{}',
    evidence       TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'asked',
    asked_at       TEXT NOT NULL,
    answer         TEXT NOT NULL DEFAULT '',
    answered_at    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_followup_student ON followups(student_id)",
    "CREATE INDEX IF NOT EXISTS idx_followup_class ON followups(class_id)",
    "CREATE INDEX IF NOT EXISTS idx_followup_mistake ON followups(mistake_id)",
    """CREATE TABLE IF NOT EXISTS followup_updates (
    id             INTEGER PRIMARY KEY,
    followup_id    INTEGER NOT NULL REFERENCES followups(id) ON DELETE CASCADE,
    causes         TEXT NOT NULL DEFAULT '[]',
    note           TEXT NOT NULL DEFAULT '',
    uncertain      INTEGER NOT NULL DEFAULT 0,
    raw            TEXT NOT NULL DEFAULT '{}',
    before_json    TEXT NOT NULL DEFAULT '[]',
    after_json     TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    decided_at     TEXT NOT NULL DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_followup_update_parent ON followup_updates(followup_id)",
)

# 教师对一份作答的处理记录：progress 记「待核对/已核对」，cause_state 记「原因待判断/教师已判断」，
# 两条轴分开存。answer_version 记住保存时的作答指纹，只有与 answers.fingerprint 相同才算当前结论；
# 重导后旧记录自动退出队列与统计，不再写任何清理逻辑。
# 与 answer_reviews 分开：后者是模型候选错因，这里是教师处理记录，两者不能混成一个状态。
#
# R1-02 / R1-03 增列（向后兼容：老库由 ensure_answer_check_schema 逐列 ALTER 补齐）：
#   cause_source        错因来源：'' / none（未判断）/ preset（采纳候选）/ teacher（教师自填）/ legacy（历史行）
#   basis_note          判断依据（与教师备注 teacher_note 分列；PRD §4.1「候选错因分别列出支持证据…
#                       不编造步骤或强行选定原因」，教师要知道自己凭什么这么判）
#   change_reason       最近一次改判/撤回的理由（改判理由与操作人、时间同列记录）
#   completion_state    ④ 作答完成情况快照（unknown/unfinished/solved/wrong_result）
#   failed_line_index   核验范围里的失败行（out_of_scope 时哪一行没法解析）
#   verification_scope  核验范围（none/partial/full）
#   manual_state        ⑤ 人工处理状态（none/referred/need_steps）
#   manual_reason       ⑤ 的状态原因码（question_unparsable/step_unparsable/empty_steps/answer_only）
#   locate_detail       ③④⑤+核验范围四组结构的 JSON 快照（历史可追溯，不受规则引擎改版影响）
#   decided_by          做出「教师已判断」的操作人
#   invalidated_at/_reason/_by
#                       撤回（回到待核对）时记谁在什么时候因为什么撤回；撤回的行 answer_version 置空，
#                       因此它自动退出队列/详情/记录/统计，但行本身与 cause_history 都留着。
#
# R1-05 增列（人工核验轴，与规则定位结果**分开存**；老库同样由 ensure_answer_check_schema 补列）：
#   verify_source       这条结论的核验来源：'' / rule（规则定位）/ preset（合成演示预置定位）/
#                       manual（规则判不了或规则给不出这个错步，由教师人工记录并给出依据）。
#                       统计与记录按它就能区分「规则结果」与「人工核验」，不必去猜 observed_step 的来历。
#   teacher_error_text  教师人工记录的数学错误（错误步骤原文/说明），与规则填的 observed_step 分列，
#                       规则结果被覆盖时旧的规则观察仍可从 cause_history 里查到。
#   teacher_verified_at / teacher_verified_by
#                       谁在什么时候做了这次人工核验（留痕；没有人工核验时为空/NULL）。
# 追问来源从两值扩到三值：preset（系统问句）/ teacher（教师自拟）/ library（已审资源库，R1-06 用）；
# 细分来源写 answer_followups.source_detail（demo_record / rule:term_move / teacher / library:<id>）。
ANSWER_CHECK_DDL = (
    """CREATE TABLE IF NOT EXISTS answer_checks (
    id             INTEGER PRIMARY KEY,
    class_id       INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id     INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    answer_id      INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
    batch          TEXT NOT NULL DEFAULT '',
    answer_version TEXT NOT NULL DEFAULT '',
    progress       TEXT NOT NULL DEFAULT 'checked',
    cause_state    TEXT NOT NULL DEFAULT 'undecided',
    cause_source   TEXT NOT NULL DEFAULT '',
    observed_step  TEXT NOT NULL DEFAULT '',
    first_bad_index INTEGER,
    locate_status  TEXT NOT NULL DEFAULT '',
    locate_source  TEXT NOT NULL DEFAULT '',
    contrast_note  TEXT NOT NULL DEFAULT '',
    completion_state TEXT NOT NULL DEFAULT '',
    failed_line_index INTEGER,
    verification_scope TEXT NOT NULL DEFAULT '',
    manual_state   TEXT NOT NULL DEFAULT '',
    manual_reason  TEXT NOT NULL DEFAULT '',
    locate_detail  TEXT NOT NULL DEFAULT '{}',
    cause_code     TEXT NOT NULL DEFAULT '',
    cause_label    TEXT NOT NULL DEFAULT '',
    cause_history  TEXT NOT NULL DEFAULT '[]',
    teacher_note   TEXT NOT NULL DEFAULT '',
    basis_note     TEXT NOT NULL DEFAULT '',
    verify_source  TEXT NOT NULL DEFAULT '',
    teacher_error_text TEXT NOT NULL DEFAULT '',
    teacher_verified_at TEXT NOT NULL DEFAULT '',
    teacher_verified_by INTEGER,
    change_reason  TEXT NOT NULL DEFAULT '',
    created_by     INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    decided_at     TEXT NOT NULL DEFAULT '',
    decided_by     INTEGER,
    invalidated_at TEXT NOT NULL DEFAULT '',
    invalidated_reason TEXT NOT NULL DEFAULT '',
    invalidated_by INTEGER
)""",
    "CREATE INDEX IF NOT EXISTS idx_checks_answer ON answer_checks(answer_id)",
    "CREATE INDEX IF NOT EXISTS idx_checks_class  ON answer_checks(class_id, batch)",
    # 同一份作答的同一个版本只能有一条当前结论：A→B→A 反复提交不许堆行（R1-03）。
    # 用部分索引：撤回后的行 answer_version 置空（历史留痕），因此不受唯一约束限制。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_checks_answer_version"
    " ON answer_checks(answer_id, answer_version) WHERE answer_version <> ''",
    # 当面追问的原话记录挂在「作答 + 版本」上，不以已经生成错题为前提。
    """CREATE TABLE IF NOT EXISTS answer_followups (
    id          INTEGER PRIMARY KEY,
    class_id    INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    answer_id   INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
    check_id    INTEGER REFERENCES answer_checks(id) ON DELETE SET NULL,
    answer_version TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'preset',
    source_detail TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL DEFAULT '',
    asked_at    TEXT NOT NULL DEFAULT '',
    answered_at TEXT NOT NULL DEFAULT '',
    evidence_snapshot TEXT NOT NULL DEFAULT '{}',
    invalidated_at TEXT NOT NULL DEFAULT '',
    invalidated_reason TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_answer_followups_answer ON answer_followups(answer_id)",
)

# 老库补列清单（顺序即执行顺序）：ensure_answer_check_schema 与 Database._ensure_columns 共用，
# 两处若各写一份必然漂移，所以只留这一份。
ANSWER_CHECK_COLUMNS = (
    ("cause_source", "TEXT NOT NULL DEFAULT ''"),
    ("basis_note", "TEXT NOT NULL DEFAULT ''"),
    ("change_reason", "TEXT NOT NULL DEFAULT ''"),
    ("completion_state", "TEXT NOT NULL DEFAULT ''"),
    ("failed_line_index", "INTEGER"),
    ("verification_scope", "TEXT NOT NULL DEFAULT ''"),
    ("manual_state", "TEXT NOT NULL DEFAULT ''"),
    ("manual_reason", "TEXT NOT NULL DEFAULT ''"),
    ("locate_detail", "TEXT NOT NULL DEFAULT '{}'"),
    ("decided_by", "INTEGER"),
    ("invalidated_at", "TEXT NOT NULL DEFAULT ''"),
    ("invalidated_reason", "TEXT NOT NULL DEFAULT ''"),
    ("invalidated_by", "INTEGER"),
    # R1-05 人工核验轴（与规则结果分列；老库补空列，历史行读作「未记录来源」）
    ("verify_source", "TEXT NOT NULL DEFAULT ''"),
    ("teacher_error_text", "TEXT NOT NULL DEFAULT ''"),
    ("teacher_verified_at", "TEXT NOT NULL DEFAULT ''"),
    ("teacher_verified_by", "INTEGER"),
)

ANSWER_FOLLOWUP_COLUMNS = (
    ("source_detail", "TEXT NOT NULL DEFAULT ''"),
    ("invalidated_at", "TEXT NOT NULL DEFAULT ''"),
    ("invalidated_reason", "TEXT NOT NULL DEFAULT ''"),
    # R1-06：记下这条追问是「拿哪份证据」问的；关键证据一改，旧确认据此失效。
    ("evidence_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
)

# ---------------------------------------------------------------------------
# R1-14：个体 AI 外发的账号级开关 + 审计
# ---------------------------------------------------------------------------
# 开关存在 users 表上（教师账号级，默认关）。桌面端以 create=False 打开旧库，不会跑
# Database._ensure_columns()，所以写入开关时由 ensure_ai_switch_column() 补列；
# **读取时缺列按「关」处理**，绝不让旧库因为少一列而报错。
USER_AI_SWITCH_COLUMN = ("ai_individual_allowed", "INTEGER NOT NULL DEFAULT 0")

# 审计表：每次「真的把个体内容发出去」之前写一行。字段清单只记**形状**（字段名/角色/类型），
# 不记任何取值——审计表不能变成第二个身份数据库。
AI_EGRESS_AUDIT_TABLE = "ai_egress_audit"
AI_EGRESS_AUDIT_DDL = (
    """CREATE TABLE IF NOT EXISTS ai_egress_audit (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER,
    username       TEXT NOT NULL DEFAULT '',
    flow           TEXT NOT NULL DEFAULT '',
    model_channel  TEXT NOT NULL DEFAULT '',
    payload_fields TEXT NOT NULL DEFAULT '[]',
    payload_bytes  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_ai_egress_user ON ai_egress_audit(user_id, created_at)",
)

# ---------------------------------------------------------------------------
# R2（1.9.0）：公共契约新增表（配套 src/app/r2_contract.py，正文见 docs/R2_接口契约.md）
# ---------------------------------------------------------------------------
# 五条总规矩（R2-00 硬约束，评审逐条核对）：
#   1. **只加不删**：R1/1.8.1 的既有表一个字段都不删、不改默认值、不改写入规则
#      （answer_checks.manual_verified 这类列根本没被碰：R1 里它只出现在契约文本里，
#      归档字段名仍是 verify_source='manual' 的那一套，本轮一律不动）。
#   2. **不做预留表**：下面 9 张表的每个字段都在 R2-01/02/03/04/05 的派单里有明确用途，
#      没有「将来可能要用」的空表或空列。
#   3. **不改 SCHEMA_VERSION**：仍为 "4"。理由见 r2_contract.R2_SCHEMA_VERSION_UNCHANGED ——
#      desktop/data_dir.py 用版本号决定「结构不兼容」，升号会把 1.8.1 老师的库挡在门外。
#   4. **幂等**：全部 CREATE ... IF NOT EXISTS；ensure_r2_schema() 二次调用返回 None。
#   5. **演示数据仍靠查询侧收口**：新表不建任何「写入即改归属」的触发器（R1-08 的教训）。
#
# 批次配置：一份作业一行。`batch` 与 answers.batch 是同一字符串（R1 的归属口径），
# 迁移只按 (class_id, batch) 登记，绝不改写历史 answers 行的归属——「同名历史批次维持
# 原归属」就落在这条唯一索引上：同名 = 同一作用域，不同班 = 各算各的，不合并、不加前缀。
ASSIGNMENT_BATCH_DDL = (
    """CREATE TABLE IF NOT EXISTS assignment_batches (
    id                 INTEGER PRIMARY KEY,
    class_id           INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    batch              TEXT NOT NULL DEFAULT '',
    name               TEXT NOT NULL DEFAULT '',
    occurred_on        TEXT NOT NULL DEFAULT '',
    expected_students  TEXT NOT NULL DEFAULT '[]',
    expected_questions TEXT NOT NULL DEFAULT '[]',
    scored_questions   TEXT NOT NULL DEFAULT '[]',
    full_scores        TEXT NOT NULL DEFAULT '{}',
    rubrics            TEXT NOT NULL DEFAULT '{}',
    rubric             TEXT NOT NULL DEFAULT '{}',
    rubric_version     TEXT NOT NULL DEFAULT '',
    config_state       TEXT NOT NULL DEFAULT 'unconfigured',
    origin             TEXT NOT NULL DEFAULT 'legacy',
    created_by         INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_class_batch"
    " ON assignment_batches(class_id, batch)",
    "CREATE INDEX IF NOT EXISTS idx_batches_occurred ON assignment_batches(class_id, occurred_on)",
)

# 作答图片多图：一张作答 1..n 张（ZIP 的 images/ 目录，或界面补录）。
# 为什么不复用 mistakes.image_file：那张表是「错题」的照片，作答本身没有照片字段，
# 且 R1 的作答是以文本步骤为核验依据的；R2 要「多图可查看」，就必须有独立表。
# 只有图片、没有文本步骤的作答由 r2_contract.classify_record_state 判成「内容不足」，
# 这里不记录任何「AI 识别出来的内容」（不实现手写 OCR）。
ANSWER_IMAGES_DDL = (
    """CREATE TABLE IF NOT EXISTS answer_images (
    id          INTEGER PRIMARY KEY,
    class_id    INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    answer_id   INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL DEFAULT 1,
    file_path   TEXT NOT NULL DEFAULT '',
    mime        TEXT NOT NULL DEFAULT '',
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'zip',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_answer_images_seq ON answer_images(answer_id, seq)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_answer_images_path ON answer_images(answer_id, file_path)",
    "CREATE INDEX IF NOT EXISTS idx_answer_images_student ON answer_images(student_id)",
    "CREATE INDEX IF NOT EXISTS idx_answer_images_class ON answer_images(class_id)",
)

# 教师评分：同批次同题统一满分（满分抄 assignment_batches.full_scores，不在这里重新定义）。
# 状态：unscored / draft / confirmed / invalidated（见 r2_contract.SCORE_STATES）。
# 唯一性口径与 R1 的 answer_checks 保持一致：同一作答同一题同一作答版本只保留一条
# 「还活着」的成绩（draft/confirmed），失效行（invalidated）与撤回行留历史、不受限制。
# 正式统计只认 confirmed —— 这条闸门写在查询侧，不靠任何触发器。
ANSWER_SCORES_DDL = (
    """CREATE TABLE IF NOT EXISTS answer_scores (
    id                 INTEGER PRIMARY KEY,
    class_id           INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id         INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    answer_id          INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
    batch              TEXT NOT NULL DEFAULT '',
    question_no        TEXT NOT NULL DEFAULT '',
    full_score         REAL NOT NULL DEFAULT 0,
    score              REAL,
    state              TEXT NOT NULL DEFAULT 'unscored',
    answer_version     TEXT NOT NULL DEFAULT '',
    rubric_version     TEXT NOT NULL DEFAULT '',
    steps_json         TEXT NOT NULL DEFAULT '[]',
    scored_by          INTEGER,
    scored_at          TEXT NOT NULL DEFAULT '',
    confirmed_by       INTEGER,
    confirmed_at       TEXT NOT NULL DEFAULT '',
    revision_reason    TEXT NOT NULL DEFAULT '',
    invalidated_at     TEXT NOT NULL DEFAULT '',
    invalidated_reason TEXT NOT NULL DEFAULT '',
    invalidated_by     INTEGER,
    history            TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_scores_answer_version"
    " ON answer_scores(answer_id, question_no, answer_version)"
    " WHERE state IN ('draft','confirmed')",
    "CREATE INDEX IF NOT EXISTS idx_scores_batch ON answer_scores(class_id, batch, question_no)",
    "CREATE INDEX IF NOT EXISTS idx_scores_answer ON answer_scores(answer_id)",
)

# 步骤扣分项：「满分 − Σ扣分 = 录入分数」，不一致一律拒绝保存（r2_contract §七 第 7 条）。
# 扣分项与成绩行同生共死（ON DELETE CASCADE）；撤回/失效时不删行，只由成绩行的 history
# 与 state 表达「这批扣分已不是当前结论」。
SCORE_STEPS_DDL = (
    """CREATE TABLE IF NOT EXISTS score_step_deductions (
    id         INTEGER PRIMARY KEY,
    score_id   INTEGER NOT NULL REFERENCES answer_scores(id) ON DELETE CASCADE,
    step_index INTEGER,
    label      TEXT NOT NULL DEFAULT '',
    deduction  REAL NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_score_steps_score ON score_step_deductions(score_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_score_steps_index"
    " ON score_step_deductions(score_id, step_index) WHERE step_index IS NOT NULL",
)

# 教学资源库（R2-05）：五类资源统一结构，`resource_key` 是跨版本的独立 ID。
# 正文/条件/练习字段放 JSON，是因为五类资源的字段差异大（练习有题干/难度/答案/解析，
# 追问只有问句），给每类各建一张表会让「双审核 + 版本」重复三遍。
# 状态：draft / pending_review / published / disabled（r2_contract.RESOURCE_STATES）。
TEACHING_RESOURCES_DDL = (
    """CREATE TABLE IF NOT EXISTS teaching_resources (
    id              INTEGER PRIMARY KEY,
    resource_key    TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'cause',
    unit            TEXT NOT NULL DEFAULT '',
    knowledge_point TEXT NOT NULL DEFAULT '',
    cause_code      TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '{}',
    conditions      TEXT NOT NULL DEFAULT '{}',
    source          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'draft',
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_key ON teaching_resources(resource_key)",
    "CREATE INDEX IF NOT EXISTS idx_resources_kind ON teaching_resources(kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_resources_kp ON teaching_resources(knowledge_point)",
    "CREATE INDEX IF NOT EXISTS idx_resources_cause ON teaching_resources(cause_code)",
    """CREATE TABLE IF NOT EXISTS teaching_resource_versions (
    id           INTEGER PRIMARY KEY,
    resource_key TEXT NOT NULL REFERENCES teaching_resources(resource_key) ON DELETE CASCADE,
    version      INTEGER NOT NULL DEFAULT 1,
    body         TEXT NOT NULL DEFAULT '{}',
    conditions   TEXT NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL DEFAULT 'pending_review',
    note         TEXT NOT NULL DEFAULT '',
    created_by   INTEGER,
    created_at   TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    disabled_at  TEXT NOT NULL DEFAULT ''
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_versions_key"
    " ON teaching_resource_versions(resource_key, version)",
    """CREATE TABLE IF NOT EXISTS teaching_resource_reviews (
    id             INTEGER PRIMARY KEY,
    resource_key   TEXT NOT NULL REFERENCES teaching_resources(resource_key) ON DELETE CASCADE,
    version        INTEGER NOT NULL DEFAULT 1,
    review_kind    TEXT NOT NULL DEFAULT 'math',
    reviewer       INTEGER,
    reviewer_name  TEXT NOT NULL DEFAULT '',
    decision       TEXT NOT NULL DEFAULT 'pending',
    opinion        TEXT NOT NULL DEFAULT '',
    reviewed_at    TEXT NOT NULL DEFAULT '',
    publish_version INTEGER
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_reviews_once"
    " ON teaching_resource_reviews(resource_key, version, review_kind, reviewer)",
    "CREATE INDEX IF NOT EXISTS idx_resource_reviews_key"
    " ON teaching_resource_reviews(resource_key, version)",
)

# 教学任务（R2-05 E）：个人或班级的辅导任务。
# `review_state` 与 `status` 正交：依据变了只打 needs_review，状态不自动改（§2.5 第 9 条）。
TEACHING_TASKS_DDL = (
    """CREATE TABLE IF NOT EXISTS teaching_tasks (
    id                INTEGER PRIMARY KEY,
    class_id          INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id        INTEGER,
    kind              TEXT NOT NULL DEFAULT 'individual',
    title             TEXT NOT NULL DEFAULT '',
    basis             TEXT NOT NULL DEFAULT '{}',
    hint_plan         TEXT NOT NULL DEFAULT '{}',
    exercise_refs     TEXT NOT NULL DEFAULT '[]',
    resource_versions TEXT NOT NULL DEFAULT '{}',
    teacher_note      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'draft',
    review_state      TEXT NOT NULL DEFAULT 'ok',
    created_by        INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT NOT NULL DEFAULT '',
    history           TEXT NOT NULL DEFAULT '[]'
)""",
    "CREATE INDEX IF NOT EXISTS idx_tasks_class ON teaching_tasks(class_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_student ON teaching_tasks(student_id)",
)

# 三级提示使用记录（R2-05 D）：记录「学生独立尝试 → 实际给的级别 → 是否自行修正」。
# `independent_before_solution` 专门记「完整解析是在独立尝试之后给的」，因为
# 「教师提前看答案不算学生完成」这条验收靠的就是它，不能从别的字段推断。
HINT_USAGES_DDL = (
    """CREATE TABLE IF NOT EXISTS hint_usages (
    id                         INTEGER PRIMARY KEY,
    class_id                   INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id                 INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    answer_id                  INTEGER REFERENCES answers(id) ON DELETE SET NULL,
    task_id                    INTEGER REFERENCES teaching_tasks(id) ON DELETE SET NULL,
    knowledge_point            TEXT NOT NULL DEFAULT '',
    cause_code                 TEXT NOT NULL DEFAULT '',
    level                      TEXT NOT NULL DEFAULT 'P1',
    planned_level              TEXT NOT NULL DEFAULT 'P1',
    provided                   INTEGER NOT NULL DEFAULT 0,
    independent_attempt        INTEGER NOT NULL DEFAULT 0,
    student_fixed              INTEGER NOT NULL DEFAULT 0,
    full_solution_provided     INTEGER NOT NULL DEFAULT 0,
    independent_before_solution INTEGER NOT NULL DEFAULT 0,
    resource_key               TEXT NOT NULL DEFAULT '',
    resource_version           INTEGER,
    note                       TEXT NOT NULL DEFAULT '',
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_hints_class ON hint_usages(class_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_hints_answer ON hint_usages(answer_id)",
    "CREATE INDEX IF NOT EXISTS idx_hints_task ON hint_usages(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_hints_student ON hint_usages(student_id)",
)

# 学情报告导出历史（R2-00.5）：随 app.db 一起备份/恢复。
# 为什么必须入库：R2-04 起初把历史写在数据目录的侧车文件 r2_report_exports.json，
# 但 `backup.py` 的备份包只收 manifest.json + app.db + uploads/，侧车文件不在范围内 →
# 老师「备份 → 换机器恢复」后导出历史整批静默消失（派单 §9.2 要求新增业务数据可备份恢复，
# §6.2 要求记录导出历史）。放进库里之后，备份/恢复天然覆盖，不需要第二套机制。
# 本表只存「谁在什么时候导出了什么」，不存报告内容；报告内容正确性由 report_pdf 负责，
# 归档上限也由调用方决定（db 层不截断，避免「看起来 50 条、其实被悄悄删了」）。
REPORT_EXPORTS_DDL = (
    """CREATE TABLE IF NOT EXISTS r2_report_exports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    class_id     INTEGER NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'class',
    student_id   INTEGER NOT NULL DEFAULT 0,
    title        TEXT NOT NULL DEFAULT '',
    purpose      TEXT NOT NULL DEFAULT '',
    data_version TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL DEFAULT '',
    batch        TEXT NOT NULL DEFAULT '',
    path         TEXT NOT NULL DEFAULT '',
    sha256       TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_report_exports_user"
    " ON r2_report_exports(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_report_exports_class"
    " ON r2_report_exports(class_id, kind)",
)

R2_DDL = (
    ASSIGNMENT_BATCH_DDL
    + ANSWER_IMAGES_DDL
    + ANSWER_SCORES_DDL
    + SCORE_STEPS_DDL
    + TEACHING_RESOURCES_DDL
    + TEACHING_TASKS_DDL
    + HINT_USAGES_DDL
    + REPORT_EXPORTS_DDL
)

# 「R2 结构就绪」的判据（与 r2_contract.R2_TABLE_NAMES 一一对应）：
# 表在、索引在、关键列在，缺任何一项都按未升级处理，由 ensure_r2_schema() 补齐。
R2_REQUIRED_COLUMNS = {
    "assignment_batches": [
        "id", "class_id", "batch", "name", "occurred_on", "expected_students",
        "expected_questions", "scored_questions", "full_scores", "rubrics", "rubric",
        "rubric_version", "config_state", "origin", "created_by", "created_at", "updated_at",
    ],
    "answer_images": [
        "id", "class_id", "student_id", "answer_id", "seq", "file_path", "mime",
        "width", "height", "bytes", "sha256", "source", "note", "created_at",
    ],
    "answer_scores": [
        "id", "class_id", "student_id", "answer_id", "batch", "question_no", "full_score",
        "score", "state", "answer_version", "rubric_version", "steps_json", "scored_by",
        "scored_at", "confirmed_by", "confirmed_at", "revision_reason", "invalidated_at",
        "invalidated_reason", "invalidated_by", "history", "created_at", "updated_at",
    ],
    "score_step_deductions": ["id", "score_id", "step_index", "label", "deduction", "note", "created_at"],
    "teaching_resources": [
        "id", "resource_key", "kind", "unit", "knowledge_point", "cause_code", "title",
        "body", "conditions", "source", "status", "current_version", "created_by",
        "created_at", "updated_at",
    ],
    "teaching_resource_versions": [
        "id", "resource_key", "version", "body", "conditions", "state", "note",
        "created_by", "created_at", "published_at", "disabled_at",
    ],
    "teaching_resource_reviews": [
        "id", "resource_key", "version", "review_kind", "reviewer", "reviewer_name",
        "decision", "opinion", "reviewed_at", "publish_version",
    ],
    "teaching_tasks": [
        "id", "class_id", "student_id", "kind", "title", "basis", "hint_plan",
        "exercise_refs", "resource_versions", "teacher_note", "status", "review_state",
        "created_by", "created_at", "updated_at", "completed_at", "history",
    ],
    "hint_usages": [
        "id", "class_id", "student_id", "answer_id", "task_id", "knowledge_point",
        "cause_code", "level", "planned_level", "provided", "independent_attempt",
        "student_fixed", "full_solution_provided", "independent_before_solution",
        "resource_key", "resource_version", "note", "created_at", "updated_at",
    ],
    "r2_report_exports": [
        "id", "user_id", "class_id", "kind", "student_id", "title", "purpose",
        "data_version", "scope", "batch", "path", "sha256", "bytes", "generated_at",
        "created_at",
    ],
}

# 后续契约版本给 R2 表补的列（R2-00.4 起）：老 R2 库缺列时由 ensure_r2_schema /
# Database._ensure_columns 逐列 ALTER 补上，只补空值、不动既有行。
R2_BATCH_EXTRA_COLUMNS = (
    ("rubrics", "TEXT NOT NULL DEFAULT '{}'"),
)

R2_REQUIRED_INDEXES = (
    "idx_batches_class_batch",
    "idx_batches_occurred",
    "idx_answer_images_seq",
    "idx_answer_images_path",
    "idx_answer_images_student",
    "idx_answer_images_class",
    "idx_scores_answer_version",
    "idx_scores_batch",
    "idx_scores_answer",
    "idx_score_steps_score",
    "idx_score_steps_index",
    "idx_resources_key",
    "idx_resources_kind",
    "idx_resources_kp",
    "idx_resources_cause",
    "idx_resource_versions_key",
    "idx_resource_reviews_once",
    "idx_resource_reviews_key",
    "idx_tasks_class",
    "idx_tasks_student",
    "idx_hints_class",
    "idx_hints_answer",
    "idx_hints_task",
    "idx_hints_student",
    "idx_report_exports_user",
    "idx_report_exports_class",
)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    pwd_salt      TEXT NOT NULL,
    pwd_hash      TEXT NOT NULL,
    iterations    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    backup_key    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classes (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    grade       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY,
    class_id    INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mistakes (
    id                    INTEGER PRIMARY KEY,
    class_id              INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id            INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    status                TEXT NOT NULL CHECK (status IN ('pending','confirmed','returned')),
    source_type           TEXT NOT NULL DEFAULT '作业',
    source_detail         TEXT NOT NULL DEFAULT '',
    note                  TEXT NOT NULL DEFAULT '',
    occurred_on           TEXT NOT NULL DEFAULT '',
    image_file            TEXT NOT NULL DEFAULT '',
    image_mime            TEXT NOT NULL DEFAULT '',
    image_width           INTEGER NOT NULL DEFAULT 0,
    image_height          INTEGER NOT NULL DEFAULT 0,
    question_text         TEXT NOT NULL DEFAULT '',
    solution_text         TEXT NOT NULL DEFAULT '',
    knowledge_points      TEXT NOT NULL DEFAULT '[]',
    error_type            TEXT NOT NULL DEFAULT '',
    observed_errors       TEXT NOT NULL DEFAULT '[]',
    possible_causes       TEXT NOT NULL DEFAULT '[]',
    advice_text           TEXT NOT NULL DEFAULT '',
    insufficient          INTEGER NOT NULL DEFAULT 0,
    insufficient_reason   TEXT NOT NULL DEFAULT '',
    ai_snapshot           TEXT NOT NULL DEFAULT '{}',
    ai_mode               TEXT NOT NULL DEFAULT 'mock',
    ai_error              TEXT NOT NULL DEFAULT '',
    ai_error_code         TEXT NOT NULL DEFAULT '',
    ai_likely_correct     INTEGER NOT NULL DEFAULT 0,
    teacher_edited_fields TEXT NOT NULL DEFAULT '[]',
    teacher_note          TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    confirmed_at          TEXT NOT NULL DEFAULT '',
    confirmed_by          INTEGER,
    returned_at           TEXT NOT NULL DEFAULT '',
    returned_reason       TEXT NOT NULL DEFAULT '',
    answer_id             INTEGER
);

CREATE INDEX IF NOT EXISTS idx_mistakes_class   ON mistakes(class_id, status);
CREATE INDEX IF NOT EXISTS idx_mistakes_student ON mistakes(student_id, status);
CREATE INDEX IF NOT EXISTS idx_students_class   ON students(class_id);

CREATE TABLE IF NOT EXISTS advice (
    id                INTEGER PRIMARY KEY,
    class_id          INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    student_id        INTEGER REFERENCES students(id) ON DELETE CASCADE,
    scope             TEXT NOT NULL CHECK (scope IN ('class','student')),
    content           TEXT NOT NULL DEFAULT '',
    evidence          TEXT NOT NULL DEFAULT '{}',
    teacher_edited    INTEGER NOT NULL DEFAULT 0,
    generated_at      TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_advice_class   ON advice(class_id) WHERE student_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_advice_student ON advice(student_id) WHERE student_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS app_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
""" + "\n".join(ddl.rstrip(";") + ";" for ddl in ANSWERS_DDL + REVIEWS_DDL + TUTOR_DDL + FOLLOWUP_DDL + ANSWER_CHECK_DDL + AI_EGRESS_AUDIT_DDL + R2_DDL) + """
"""

# 桌面版对「已有 1.3.0 数据目录」做只读完整性检查时用：必需表及其必需列。
# 与上面的 SCHEMA 保持同步；缺表、缺列一律按「结构不兼容」拒绝，绝不自动补。
# students.active 不在此列：它是 1.3.0 之后新增的列，由 add_student_active() 显式迁移补上，
# 缺这一列说明是 1.3.0 的库，不是残缺库。
REQUIRED_TABLES = {
    "users": ["id", "username", "display_name", "pwd_salt", "pwd_hash",
              "iterations", "created_at", "backup_key"],
    "sessions": ["token", "user_id", "created_at", "expires_at"],
    "classes": ["id", "user_id", "name", "grade", "note", "created_at", "updated_at"],
    "students": ["id", "class_id", "name", "note", "created_at", "updated_at"],
    "mistakes": [
        "id", "class_id", "student_id", "status", "source_type", "source_detail",
        "note", "occurred_on", "image_file", "image_mime", "image_width", "image_height",
        "question_text", "solution_text", "knowledge_points", "error_type",
        "observed_errors", "possible_causes", "advice_text",
        "insufficient", "insufficient_reason", "ai_snapshot", "ai_mode",
        "ai_error", "ai_error_code", "ai_likely_correct", "teacher_edited_fields",
        "teacher_note", "created_at", "updated_at", "confirmed_at", "confirmed_by",
        "returned_at", "returned_reason",
    ],
    "advice": ["id", "class_id", "student_id", "scope", "content", "evidence",
               "teacher_edited", "generated_at", "updated_at"],
    "app_meta": ["key", "value"],
}

# 与 SCHEMA 里的 CREATE INDEX 保持同步；桌面版打开已有库时核对，缺索引一律拒绝，绝不自动补。
REQUIRED_INDEXES = {
    "mistakes": ["idx_mistakes_class", "idx_mistakes_student"],
    "students": ["idx_students_class"],
    "advice": ["idx_advice_class", "idx_advice_student"],
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MigrationError(Exception):
    """结构升级失败：原库没有被改动，备份文件仍在。"""


def _migration_fault(name):
    """测试注入点：MISTAKE_FAULT 环境变量逗号分隔，命中时抛 MigrationError 模拟真实故障。

    老师正常使用时不会设置这个变量，不影响实际运行。
    """
    if name in (os.environ.get("MISTAKE_FAULT") or "").split(","):
        raise MigrationError("模拟故障：%s" % name)


def student_active_column(db_file):
    """只读判断已有库的 students 表是否已有 active 列。"""
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        return any(row[1] == "active" for row in conn.execute("PRAGMA table_info(students)"))
    finally:
        conn.close()


def add_student_active(db_file):
    """给 1.3.0 的库补上 students.active 列；返回备份文件路径，本来就有这一列时返回 None。

    先做一份一致性备份（SQLite 在线备份，未合并的 WAL 也会算进去），再单独用一条连接
    在写事务里加列。备份、开始事务、加列、提交任一环节失败，都按真实数据状态抛出
    MigrationError；备份文件名带时间戳和随机后缀，绝不覆盖已有备份。
    """
    if student_active_column(db_file):
        return None
    backup_file = "%s.bak-%s-%s" % (
        db_file, datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]
    )

    # 阶段一：一致性备份（只读，不碰原库）。失败时清掉可能留下的残缺备份文件。
    try:
        source = sqlite3.connect(db_file, timeout=15)
        try:
            target = sqlite3.connect(backup_file)
            try:
                _migration_fault("migrate_backup")
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception as exc:
        try:
            if os.path.exists(backup_file):
                os.remove(backup_file)
        except OSError:
            pass
        raise MigrationError(
            "给学生的「停用状态」加字段前先做备份时失败（%s），原有数据没有改动，也没有改写任何文件。" % exc
        )

    # 阶段二：写事务加列。写连接打不开（文件被占用、没有权限等）时明确说明，不碰原库。
    try:
        _migration_fault("migrate_connect")
        conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
    except Exception as exc:
        raise MigrationError(
            "给学生的「停用状态」加字段时无法打开数据库写入连接（%s），原有数据没有改动。"
            "请先关闭其他正在使用这个数据目录的软件后重试；备份文件：%s" % (exc, backup_file)
        )
    try:
        try:
            _migration_fault("migrate_begin")
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise MigrationError(
                "给学生的「停用状态」加字段时无法开始写入（%s），可能是另一份软件正在使用这个数据目录。"
                "原有数据没有改动。备份文件：%s" % (exc, backup_file)
            )
        try:
            conn.execute("ALTER TABLE students ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            _migration_fault("migrate_commit")
            conn.execute("COMMIT")
        except Exception as exc:
            rollback_failed = False
            try:
                conn.execute("ROLLBACK")
            except Exception:
                rollback_failed = True
            if rollback_failed:
                raise MigrationError(
                    "给学生的「停用状态」加字段时失败（%s），而且撤销改动也没有成功，当前数据状态不确定。"
                    "请保留备份文件 %s 和整个软件文件夹，联系技术人员处理。" % (exc, backup_file)
                )
            raise MigrationError(
                "给学生的「停用状态」加字段时失败（%s），改动已撤销，原有数据没有改动。备份文件：%s"
                % (exc, backup_file)
            )
    finally:
        conn.close()

    if not student_active_column(db_file):
        raise MigrationError("学生的「停用状态」字段没有生效，原有数据没有改动。备份文件：%s" % backup_file)
    return backup_file


ANSWER_INDEX_NAMES = (
    "idx_answers_class_record",
    "idx_answers_student",
    "idx_answers_batch",
    "idx_answers_source",
    "idx_reviews_answer",
    "idx_reviews_answer_confirmed",
    "idx_mistakes_answer",
)

# 已废弃的库级触发器名：T6 起不再创建，旧库打开时由 Database._ensure_columns() 显式 DROP。
RETIRED_TRIGGER_NAMES = (
    "trg_answers_demo_batch_ins",
    "trg_answers_demo_batch_upd",
)

# ---------------------------------------------------------------------------
# R1-16：「作答核对 → 原版错题」的幂等键
# ---------------------------------------------------------------------------
# mistakes 原先没有任何指向 answers 的列，于是「作答核对」页的教师判断没法进原版四个页面
# （个人学情 / 班级统计 / 错题管理 / 个性化辅导 都只读 mistakes）。这里补一个可空外键列：
# - 可空：原有手工录入的错题没有来源作答，必须保持合法（新列默认 NULL）；
# - 唯一：同一份作答最多一条错题（局部唯一索引，NULL 不参与），重复保存只会更新同一条；
# - 老库升级走 ensure_answers_schema 的加列套路（先备份再 BEGIN IMMEDIATE），
#   桌面端启动时调的就是它，因此旧库打开即可用。
MISTAKE_ANSWER_COLUMNS = (
    ("answer_id", "INTEGER"),
)
MISTAKE_ANSWER_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mistakes_answer"
    " ON mistakes(answer_id) WHERE answer_id IS NOT NULL",
)


def answers_schema_ready(db_file):
    """只读判断已有库是否已有作答记录表、作答审核表、学生编号列与索引。

    注意：**不再**要求那两个「演示批次归位」触发器（T6 已删除这套隐形行为）。
    旧库里若还留着它们，结构仍算就绪；它们会被 Database 打开时清掉，不影响判定。
    """
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
        mistake_columns = {row[1] for row in conn.execute("PRAGMA table_info(mistakes)")}
    finally:
        conn.close()
    return (
        "answers" in tables
        and "answer_reviews" in tables
        and "code" in columns
        and "answer_id" in mistake_columns
        and all(name in indexes for name in ANSWER_INDEX_NAMES)
    )


def ensure_answers_schema(db_file):
    """给旧库补上作答记录表、作答审核表和学生「学校编号」列；返回备份文件路径，本来就齐全时返回 None。

    与 add_student_active 同一套做法：先做一致性备份，再在写事务里建表、加列、建索引。
    备份、开始事务、建表、提交任一环节失败都按真实数据状态抛出 MigrationError，原库不动。
    """
    if answers_schema_ready(db_file):
        return None
    backup_file = "%s.bak-%s-%s" % (
        db_file, datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]
    )

    try:
        source = sqlite3.connect(db_file, timeout=15)
        try:
            target = sqlite3.connect(backup_file)
            try:
                _migration_fault("answers_backup")
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception as exc:
        try:
            if os.path.exists(backup_file):
                os.remove(backup_file)
        except OSError:
            pass
        raise MigrationError(
            "给作答导入加数据表前先做备份时失败（%s），原有数据没有改动，也没有改写任何文件。" % exc
        )

    try:
        conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
    except Exception as exc:
        raise MigrationError(
            "给作答导入加数据表时无法打开数据库写入连接（%s），原有数据没有改动。"
            "请先关闭其他正在使用这个数据目录的软件后重试；备份文件：%s" % (exc, backup_file)
        )
    try:
        try:
            _migration_fault("answers_begin")
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise MigrationError(
                "给作答导入加数据表时无法开始写入（%s），可能是另一份软件正在使用这个数据目录。"
                "原有数据没有改动。备份文件：%s" % (exc, backup_file)
            )
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
            if "code" not in columns:
                conn.execute("ALTER TABLE students ADD COLUMN code TEXT NOT NULL DEFAULT ''")
            for ddl in ANSWERS_DDL + REVIEWS_DDL:
                conn.execute(ddl)
            # 一次性归位：旧版本把演示作答写进了真实批次（DEMO_BATCH 曾与真实作业撞名），
            # 这里把「确实由演示写入」的行挪回演示批次。判据用 record_key 前缀 + 来源标记，
            # 老师的真实作答一条都不动（它没有 DEMO- 前缀）。
            # T6：这套归位是**唯一**保留的「自动纠正」——它只在升级迁移时跑一次、有备份、
            # 可审计；原先两个每次写入都会发作的触发器已删除（见 ANSWERS_DDL 上的说明）。
            conn.execute(
                "UPDATE answers SET batch=? WHERE source=? AND record_key LIKE ? AND batch<>?",
                (DEMO_BATCH, DEMO_SOURCE, DEMO_RECORD_PREFIX + "%", DEMO_BATCH),
            )
            for name in RETIRED_TRIGGER_NAMES:
                conn.execute("DROP TRIGGER IF EXISTS %s" % name)
            # R1-16：让「作答核对」的教师判断以幂等方式落到原版 mistakes。
            # 幂等键 = answers.id（mistakes 原先没有任何指向作答的列），同一份作答只允许一条。
            _add_missing_columns(conn, "mistakes", MISTAKE_ANSWER_COLUMNS)
            for ddl in MISTAKE_ANSWER_DDL:
                conn.execute(ddl)
            _migration_fault("answers_commit")
            conn.execute("COMMIT")
        except Exception as exc:
            rollback_failed = False
            try:
                conn.execute("ROLLBACK")
            except Exception:
                rollback_failed = True
            if rollback_failed:
                raise MigrationError(
                    "给作答导入加数据表时失败（%s），而且撤销改动也没有成功，当前数据状态不确定。"
                    "请保留备份文件 %s 和整个软件文件夹，联系技术人员处理。" % (exc, backup_file)
                )
            raise MigrationError(
                "给作答导入加数据表时失败（%s），改动已撤销，原有数据没有改动。备份文件：%s"
                % (exc, backup_file)
            )
    finally:
        conn.close()

    if not answers_schema_ready(db_file):
        raise MigrationError("作答记录表没有生效，原有数据没有改动。备份文件：%s" % backup_file)
    return backup_file


TUTOR_INDEX_NAMES = ("idx_tutor_student", "idx_tutor_class")


def tutor_schema_ready(db_file):
    """只读判断已有库是否已有个人辅导建议表与对应索引。"""
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    return "tutor_advice" in tables and all(name in indexes for name in TUTOR_INDEX_NAMES)


def ensure_tutor_schema(db_file):
    """给旧库补上个人辅导建议表；返回备份文件路径，本来就齐全时返回 None。

    与 ensure_answers_schema 同一套做法：先做一致性备份，再在写事务里建表建索引。
    备份、开始事务、建表、提交任一环节失败都按真实数据状态抛出 MigrationError，原库不动。
    """
    if tutor_schema_ready(db_file):
        return None
    backup_file = "%s.bak-%s-%s" % (
        db_file, datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]
    )

    try:
        source = sqlite3.connect(db_file, timeout=15)
        try:
            target = sqlite3.connect(backup_file)
            try:
                _migration_fault("tutor_backup")
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception as exc:
        try:
            if os.path.exists(backup_file):
                os.remove(backup_file)
        except OSError:
            pass
        raise MigrationError(
            "给个人辅导建议加数据表前先做备份时失败（%s），原有数据没有改动，也没有改写任何文件。" % exc
        )

    try:
        conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
    except Exception as exc:
        raise MigrationError(
            "给个人辅导建议加数据表时无法打开数据库写入连接（%s），原有数据没有改动。"
            "请先关闭其他正在使用这个数据目录的软件后重试；备份文件：%s" % (exc, backup_file)
        )
    try:
        try:
            _migration_fault("tutor_begin")
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise MigrationError(
                "给个人辅导建议加数据表时无法开始写入（%s），可能是另一份软件正在使用这个数据目录。"
                "原有数据没有改动。备份文件：%s" % (exc, backup_file)
            )
        try:
            for ddl in TUTOR_DDL:
                conn.execute(ddl)
            _migration_fault("tutor_commit")
            conn.execute("COMMIT")
        except Exception as exc:
            rollback_failed = False
            try:
                conn.execute("ROLLBACK")
            except Exception:
                rollback_failed = True
            if rollback_failed:
                raise MigrationError(
                    "给个人辅导建议加数据表时失败（%s），而且撤销改动也没有成功，当前数据状态不确定。"
                    "请保留备份文件 %s 和整个软件文件夹，联系技术人员处理。" % (exc, backup_file)
                )
            raise MigrationError(
                "给个人辅导建议加数据表时失败（%s），改动已撤销，原有数据没有改动。备份文件：%s"
                % (exc, backup_file)
            )
    finally:
        conn.close()

    if not tutor_schema_ready(db_file):
        raise MigrationError("个人辅导建议表没有生效，原有数据没有改动。备份文件：%s" % backup_file)
    return backup_file


FOLLOWUP_INDEX_NAMES = (
    "idx_followup_student",
    "idx_followup_class",
    "idx_followup_mistake",
    "idx_followup_update_parent",
)
FOLLOWUP_TABLES = ("followups", "followup_updates")


def followup_schema_ready(db_file):
    """只读判断已有库是否已有追问表、候选更新表与对应索引。"""
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    return (
        all(name in tables for name in FOLLOWUP_TABLES)
        and all(name in indexes for name in FOLLOWUP_INDEX_NAMES)
    )


def ensure_followup_schema(db_file):
    """给旧库补上追问与候选更新表；返回备份文件路径，本来就齐全时返回 None。

    与 ensure_tutor_schema 同一套做法：先做一致性备份，再在写事务里建表建索引。
    """
    if followup_schema_ready(db_file):
        return None
    backup_file = "%s.bak-%s-%s" % (
        db_file, datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]
    )

    try:
        source = sqlite3.connect(db_file, timeout=15)
        try:
            target = sqlite3.connect(backup_file)
            try:
                _migration_fault("followup_backup")
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception as exc:
        try:
            if os.path.exists(backup_file):
                os.remove(backup_file)
        except OSError:
            pass
        raise MigrationError(
            "给追问记录加数据表前先做备份时失败（%s），原有数据没有改动，也没有改写任何文件。" % exc
        )

    try:
        conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
    except Exception as exc:
        raise MigrationError(
            "给追问记录加数据表时无法打开数据库写入连接（%s），原有数据没有改动。"
            "请先关闭其他正在使用这个数据目录的软件后重试；备份文件：%s" % (exc, backup_file)
        )
    try:
        try:
            _migration_fault("followup_begin")
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise MigrationError(
                "给追问记录加数据表时无法开始写入（%s），可能是另一份软件正在使用这个数据目录。"
                "原有数据没有改动。备份文件：%s" % (exc, backup_file)
            )
        try:
            for ddl in FOLLOWUP_DDL:
                conn.execute(ddl)
            _migration_fault("followup_commit")
            conn.execute("COMMIT")
        except Exception as exc:
            rollback_failed = False
            try:
                conn.execute("ROLLBACK")
            except Exception:
                rollback_failed = True
            if rollback_failed:
                raise MigrationError(
                    "给追问记录加数据表时失败（%s），而且撤销改动也没有成功，当前数据状态不确定。"
                    "请保留备份文件 %s 和整个软件文件夹，联系技术人员处理。" % (exc, backup_file)
                )
            raise MigrationError(
                "给追问记录加数据表时失败（%s），改动已撤销，原有数据没有改动。备份文件：%s"
                % (exc, backup_file)
            )
    finally:
        conn.close()

    if not followup_schema_ready(db_file):
        raise MigrationError("追问记录表没有生效，原有数据没有改动。备份文件：%s" % backup_file)
    return backup_file


ANSWER_CHECK_TABLES = ("answer_checks", "answer_followups")
ANSWER_CHECK_INDEX_NAMES = (
    "idx_checks_answer",
    "idx_checks_class",
    "idx_checks_answer_version",
    "idx_answer_followups_answer",
)
# 「结构就绪」= 表在、索引在、R1-02/03 的新列都在。少任何一列都按未升级处理，
# 由 ensure_answer_check_schema 逐列 ALTER 补齐（老库行内容一条不改，只补空列）。
ANSWER_CHECK_REQUIRED_COLUMNS = {
    "answer_checks": [name for name, _ in ANSWER_CHECK_COLUMNS] + [
        "progress", "cause_state", "answer_version", "cause_code", "cause_label",
        "cause_history", "teacher_note", "locate_status", "locate_source",
    ],
    "answer_followups": [name for name, _ in ANSWER_FOLLOWUP_COLUMNS] + [
        "source", "question", "answer_text", "answer_version",
    ],
}


def _table_columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def answer_check_schema_ready(db_file):
    """只读判断已有库是否已有作答核对表、追问原话表、索引与新列。"""
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        if not all(name in tables for name in ANSWER_CHECK_TABLES):
            return False
        if not all(name in indexes for name in ANSWER_CHECK_INDEX_NAMES):
            return False
        for table, columns in ANSWER_CHECK_REQUIRED_COLUMNS.items():
            existing = _table_columns(conn, table)
            if any(name not in existing for name in columns):
                return False
        return True
    finally:
        conn.close()


def _add_missing_columns(conn, table, columns):
    """按清单补齐缺失列；已有的列跳过（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
    existing = _table_columns(conn, table)
    added = []
    for name, ddl in columns:
        if name in existing:
            continue
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
        added.append(name)
    return added


# ---------------------------------------------------------------------------
# R1-14：账号级个体 AI 开关 + 外发审计（旧库兼容：缺列/缺表一律按「关」/「还没发生过」处理）
# ---------------------------------------------------------------------------
def _users_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(users)")}


def ai_switch_ready(db):
    """只读判断 users 表有没有开关列（没有 = 旧库，读作「关」）。"""
    return USER_AI_SWITCH_COLUMN[0] in _users_columns(db.conn)


def ensure_ai_switch_column(db):
    """给旧库补 users.ai_individual_allowed 列（幂等）；返回是否新加了列。

    桌面端用 create=False 打开旧库，不会跑 Database._ensure_columns()，所以补列放在这里，
    由「写开关」这一动作触发；读取侧不补列、缺列直接按「关」，避免 GET 请求写库。
    """
    if ai_switch_ready(db):
        return False
    with db.tx() as conn:
        conn.execute("ALTER TABLE users ADD COLUMN %s %s" % USER_AI_SWITCH_COLUMN)
    return True


def ai_individual_allowed(db, user_id):
    """该教师账号是否显式开启「个体 AI 外发」。默认关；缺列（旧库）也是关。"""
    if not ai_switch_ready(db):
        return False
    try:
        owner = int(user_id)
    except (TypeError, ValueError):
        return False
    value = db.scalar(
        "SELECT ai_individual_allowed FROM users WHERE id=?", (owner,))
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return False


def set_ai_individual_allowed(db, user_id, allowed):
    """写开关（先补列）。返回写入后的值。"""
    ensure_ai_switch_column(db)
    owner = int(user_id)
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET ai_individual_allowed=? WHERE id=?",
            (1 if allowed else 0, owner),
        )
    return ai_individual_allowed(db, owner)


def ensure_ai_egress_audit(conn, db_file=None):
    """建外发审计表（幂等）。新库由 SCHEMA 直接建；旧库在第一次真实外发时自建。"""
    if db_file is not None:
        conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for ddl in AI_EGRESS_AUDIT_DDL:
                conn.execute(ddl)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return True
    for ddl in AI_EGRESS_AUDIT_DDL:
        conn.execute(ddl)
    return True


def record_ai_egress(db, user_id=None, username="", flow="", model_channel="",
                     fields=(), payload_bytes=0):
    """写一条「真的要发出去」的审计，返回行 id。

    只记字段**形状**（`fields` 是字段名列表），不记取值——审计表不是第二个身份数据库。
    写不进去时抛异常：调用方（vision._note_egress）据此在 urlopen 之前中止发送。
    """
    blob = json.dumps(list(fields or ()), ensure_ascii=False)
    stamp = now_iso()
    with db.tx() as conn:
        ensure_ai_egress_audit(conn)
        cur = conn.execute(
            "INSERT INTO ai_egress_audit(user_id, username, flow, model_channel,"
            " payload_fields, payload_bytes, created_at) VALUES (?,?,?,?,?,?,?)",
            (int(user_id) if user_id is not None else None, username or "", flow or "",
             model_channel or "", blob, int(payload_bytes or 0), stamp),
        )
        return cur.lastrowid


def ai_egress_audit_rows(db, user_id=None, limit=50):
    """读最近的外发审计（新→旧）。表不存在时返回 []（旧库还没发生过外发）。"""
    names = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    if AI_EGRESS_AUDIT_TABLE not in names:
        return []
    limit = max(1, int(limit or 1))
    if user_id is None:
        rows = db.query(
            "SELECT * FROM ai_egress_audit ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = db.query(
            "SELECT * FROM ai_egress_audit WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), limit))
    for row in rows:
        try:
            row["payload_fields"] = json.loads(row.get("payload_fields") or "[]")
        except (TypeError, ValueError):
            row["payload_fields"] = []
    return rows


def _dedupe_answer_checks(conn):
    """建唯一索引前先合并重复的 (answer_id, answer_version)：保留 id 最大的一条。

    为什么要先合并：R1-00 §15.2 实测 A→B→A 会堆出多条同版本行（旧实现靠先查后写，
    并发下不防重）。直接建唯一索引会因历史重复数据失败，所以迁移时保留最后一行，
    把被合并行的结论并进 cause_history，保证「历史可追溯」而不是悄悄丢掉。
    """
    rows = conn.execute(
        "SELECT answer_id, answer_version FROM answer_checks"
        " WHERE answer_version <> '' GROUP BY answer_id, answer_version HAVING COUNT(*) > 1"
    ).fetchall()
    merged = 0
    for answer_id, version in rows:
        ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM answer_checks WHERE answer_id=? AND answer_version=?"
                " ORDER BY id", (answer_id, version))
        ]
        keep, drop = ids[-1], ids[:-1]
        keep_row = conn.execute("SELECT * FROM answer_checks WHERE id=?", (keep,)).fetchone()
        columns = [item[0] for item in conn.execute("SELECT * FROM answer_checks LIMIT 0").description]
        record = dict(zip(columns, keep_row)) if keep_row else {}
        history = []
        try:
            history = json.loads(record.get("cause_history") or "[]")
        except (TypeError, ValueError):
            history = []
        for old_id in drop:
            old_row = conn.execute("SELECT * FROM answer_checks WHERE id=?", (old_id,)).fetchone()
            old = dict(zip(columns, old_row)) if old_row else {}
            history.append({
                "action": "merge_duplicate",
                "merged_id": old_id,
                "code": old.get("cause_code") or "",
                "label": old.get("cause_label") or "",
                "cause_state": old.get("cause_state") or "",
                "note": old.get("teacher_note") or "",
                "decided_at": old.get("decided_at") or old.get("updated_at") or "",
            })
            conn.execute("DELETE FROM answer_checks WHERE id=?", (old_id,))
            merged += 1
        conn.execute(
            "UPDATE answer_checks SET cause_history=? WHERE id=?",
            (json.dumps(history, ensure_ascii=False), keep),
        )
    return merged


def _answer_check_ddl_by_kind(kind):
    """按语句种类取 `ANSWER_CHECK_DDL` 里的语句：`table` 只取建表，`index` 取全部建索引。

    为什么必须分两步执行（T5 发现的 1.7.x 迁移 bug）：
    - `answer_checks` 与 `answer_followups` 在 1.7.x 旧库里**都不存在**；
    - 补列语句 `ALTER TABLE answer_followups ADD COLUMN ...` 若在「建表」之前跑，
      会直接抛 `no such table: answer_followups`（实测见 t6 探针），整次迁移回滚；
    - 唯一索引 `idx_checks_answer_version` 又必须在合并重复行之后建，否则历史重复数据会让它失败。
    所以顺序固定为：**先建表 → 再补列 → 再合并重复行 → 最后建索引**。
    """
    statements = [ddl for ddl in ANSWER_CHECK_DDL if isinstance(ddl, str)]
    if kind == "table":
        return [ddl for ddl in statements if ddl.lstrip().upper().startswith("CREATE TABLE")]
    return [ddl for ddl in statements if "INDEX" in ddl.lstrip().upper()[:24]]


def ensure_answer_check_schema(db_file):
    """给旧库补上作答核对与追问原话表、R1-02/03 新列与唯一索引；本来就齐全时返回 None。

    与 ensure_followup_schema 同一套三段式：只读检查 → 一致性备份 → BEGIN IMMEDIATE 建表/补列
    /合并重复行/建唯一索引 → COMMIT → 复核。备份、开始事务、任何一步失败都按真实数据状态抛
    MigrationError，原库不动。测试注入点：MISTAKE_FAULT 含 answer_check_backup /
    answer_check_begin / answer_check_commit 时模拟对应环节故障。

    如何禁用/回滚（T9 评审用）：本函数只做「建表 + ADD COLUMN + 合并重复行 + 建索引」，
    不删任何表、不改任何列名与既有列的值；误伤回滚 = 用同目录下 `app.db.bak-<时间戳>-<随机>` 覆盖回去
    （每次迁移前都会先做一致性备份），再按需删除 `idx_checks_answer_version` 索引即可恢复旧结构。
    """
    if answer_check_schema_ready(db_file):
        return None
    backup_file = "%s.bak-%s-%s" % (
        db_file, datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]
    )

    try:
        source = sqlite3.connect(db_file, timeout=15)
        try:
            target = sqlite3.connect(backup_file)
            try:
                _migration_fault("answer_check_backup")
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception as exc:
        try:
            if os.path.exists(backup_file):
                os.remove(backup_file)
        except OSError:
            pass
        raise MigrationError(
            "给作答核对加数据表前先做备份时失败（%s），原有数据没有改动，也没有改写任何文件。" % exc
        )

    try:
        conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
    except Exception as exc:
        raise MigrationError(
            "给作答核对加数据表时无法打开数据库写入连接（%s），原有数据没有改动。"
            "请先关闭其他正在使用这个数据目录的软件后重试；备份文件：%s" % (exc, backup_file)
        )
    try:
        try:
            _migration_fault("answer_check_begin")
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise MigrationError(
                "给作答核对加数据表时无法开始写入（%s），可能是另一份软件正在使用这个数据目录。"
                "原有数据没有改动。备份文件：%s" % (exc, backup_file)
            )
        try:
            # T6 修复：旧库（1.7.x）两张表都不存在，必须**先建表**再补列，
            # 否则 ALTER TABLE answer_followups 会抛 no such table（T5 实测、本轮探针复现）。
            for ddl in _answer_check_ddl_by_kind("table"):
                conn.execute(ddl)
            _add_missing_columns(conn, "answer_checks", ANSWER_CHECK_COLUMNS)
            _add_missing_columns(conn, "answer_followups", ANSWER_FOLLOWUP_COLUMNS)
            # 历史行没有来源记录：有结论的按历史行标注，避免读成「未判断」。
            conn.execute(
                "UPDATE answer_checks SET cause_source='legacy'"
                " WHERE (cause_source IS NULL OR cause_source='') AND cause_code <> ''"
            )
            _dedupe_answer_checks(conn)
            # 索引放到最后：唯一索引必须在合并重复行之后建（否则历史重复数据会让建索引失败）。
            for ddl in _answer_check_ddl_by_kind("index"):
                conn.execute(ddl)
            _migration_fault("answer_check_commit")
            conn.execute("COMMIT")
        except Exception as exc:
            rollback_failed = False
            try:
                conn.execute("ROLLBACK")
            except Exception:
                rollback_failed = True
            if rollback_failed:
                raise MigrationError(
                    "给作答核对加数据表时失败（%s），而且撤销改动也没有成功，当前数据状态不确定。"
                    "请保留备份文件 %s 和整个软件文件夹，联系技术人员处理。" % (exc, backup_file)
                )
            raise MigrationError(
                "给作答核对加数据表时失败（%s），改动已撤销，原有数据没有改动。备份文件：%s"
                % (exc, backup_file)
            )
    finally:
        conn.close()

    if not answer_check_schema_ready(db_file):
        raise MigrationError("作答核对表没有生效，原有数据没有改动。备份文件：%s" % backup_file)
    return backup_file


# ---------------------------------------------------------------------------
# R2（1.9.0）：结构就绪判定、增量迁移与批次配置读写
# ---------------------------------------------------------------------------
def _r2_contract():
    """延迟导入 R2 契约模块。

    为什么延迟：`r2_contract` 是纯函数层（只 import re），但 db.py 是它「不许反向依赖」
    的对象；放成模块级 import 会让「谁依赖谁」变得看不出来，也挡住将来单独复用 db.py 的
    场景（tests/fixtures 里就有独立的 db.py 副本）。这里照 `answer_check._demo_module()`
    的既有做法，import 失败也能给出明确异常。
    """
    try:
        from . import r2_contract
    except ImportError:  # 直接以脚本方式 import db.py 时
        import r2_contract  # type: ignore[no-redef]
    return r2_contract


def r2_schema_ready(db_file):
    """只读判断已有库是否已具备 R2 结构（9 张表 + 索引 + 关键列都在）。

    与 R1 的 `*_schema_ready` 同一套判据风格：**不看 SCHEMA_VERSION**（它仍是 "4"），
    只看结构本身，因此 1.8.1 的库升级成功后版本号不变、结构就绪为真。
    """
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for table, columns in R2_REQUIRED_COLUMNS.items():
            if table not in tables:
                return False
            existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
            if any(name not in existing for name in columns):
                return False
        return all(name in indexes for name in R2_REQUIRED_INDEXES)
    finally:
        conn.close()


def _seed_assignment_batches(conn, stamp=None):
    """把历史作答里出现过的批次登记成「未配置」的批次配置行，返回新增行数。

    三条硬口径（对应派单「同名历史批次维持原归属」）：
      1. **不改写归属**：只 INSERT 配置行，绝不 UPDATE answers.batch；
      2. **按 (class_id, batch) 逐字登记**：同名不同班各一行，同班同名视为同一份作业
         （R1 的数据模型里它们本来就是同一个字符串，无法也无权区分）；
      3. **不猜名单/满分**：名单、题号、满分一律留空 → `config_state='unconfigured'`，
         统计显示「暂无」。若把该批次自己的作答行反推成「应作答名单」，未提交数会永远是 0，
         那是**用数据污染口径**，明确不做。
    幂等：已存在的 (class_id, batch) 跳过，所以二次调用返回 0。
    """
    contract = _r2_contract()
    stamp = stamp or now_iso()
    rows = conn.execute(
        "SELECT DISTINCT class_id, batch FROM answers"
        " WHERE batch IS NOT NULL AND TRIM(batch) <> ''"
    ).fetchall()
    added = 0
    for class_id, batch in rows:
        existing = conn.execute(
            "SELECT 1 FROM assignment_batches WHERE class_id=? AND batch=?",
            (class_id, batch),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO assignment_batches(class_id, batch, name, occurred_on,"
            " expected_students, expected_questions, scored_questions, full_scores,"
            " rubric, rubric_version, config_state, origin, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                class_id, batch, batch, "",
                "[]", "[]", "[]", "{}", "{}", "",
                contract.BATCH_CONFIG_STATE_UNCONFIGURED, contract.BATCH_ORIGIN_LEGACY,
                None, stamp, stamp,
            ),
        )
        added += 1
    return added


def ensure_r2_schema(db_file):
    """给旧库补上 R2 的 9 张表/索引并登记历史批次；本来就齐全时返回 None。

    与 `ensure_answer_check_schema` 同一套三段式：只读检查 → 一致性备份 → `BEGIN IMMEDIATE`
    建表/建索引/登记历史批次 → COMMIT → 复核。失败抛 `MigrationError`，原库不动
    （迁移前的一致性备份仍在，覆盖回去即回退）。测试注入点：`MISTAKE_FAULT` 含
    `r2_backup` / `r2_begin` / `r2_commit` 时模拟对应环节故障。

    幂等性：结构就绪时**直接返回 None，一个字节都不写**；未就绪时才走完整流程。
    因此「老师每次启动桌面端都调一次」是安全的。
    """
    if not os.path.exists(db_file):
        raise FileNotFoundError("数据库文件不存在：%s" % db_file)
    if r2_schema_ready(db_file):
        return None
    backup_file = "%s.bak-%s-%s" % (
        db_file, datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]
    )

    try:
        source = sqlite3.connect(db_file, timeout=15)
        try:
            target = sqlite3.connect(backup_file)
            try:
                _migration_fault("r2_backup")
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception as exc:
        try:
            if os.path.exists(backup_file):
                os.remove(backup_file)
        except OSError:
            pass
        raise MigrationError(
            "给 R2 新功能加数据表前先做备份时失败（%s），原有数据没有改动，也没有改写任何文件。" % exc
        )

    try:
        conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
    except Exception as exc:
        raise MigrationError(
            "给 R2 新功能加数据表时无法打开数据库写入连接（%s），原有数据没有改动。"
            "请先关闭其他正在使用这个数据目录的软件后重试；备份文件：%s" % (exc, backup_file)
        )
    try:
        try:
            _migration_fault("r2_begin")
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise MigrationError(
                "给 R2 新功能加数据表时无法开始写入（%s），可能是另一份软件正在使用这个数据目录。"
                "原有数据没有改动。备份文件：%s" % (exc, backup_file)
            )
        try:
            for ddl in R2_DDL:
                conn.execute(ddl)
            # R2-00.4：题级评分标准版本（rubrics）。已建过 R2 表但还没有这一列的库在这里补，
            # 补的是空 JSON（读作「题级未配置」，回退批次级旧版本），旧行一条不动。
            _add_missing_columns(conn, "assignment_batches", R2_BATCH_EXTRA_COLUMNS)
            _seed_assignment_batches(conn)
            _migration_fault("r2_commit")
            conn.execute("COMMIT")
        except Exception as exc:
            rollback_failed = False
            try:
                conn.execute("ROLLBACK")
            except Exception:
                rollback_failed = True
            if rollback_failed:
                raise MigrationError(
                    "给 R2 新功能加数据表时失败（%s），而且撤销改动也没有成功，当前数据状态不确定。"
                    "请保留备份文件 %s 和整个软件文件夹，联系技术人员处理。" % (exc, backup_file)
                )
            raise MigrationError(
                "给 R2 新功能加数据表时失败（%s），改动已撤销，原有数据没有改动。备份文件：%s"
                % (exc, backup_file)
            )
    finally:
        conn.close()

    if not r2_schema_ready(db_file):
        raise MigrationError("R2 数据表没有生效，原有数据没有改动。备份文件：%s" % backup_file)
    return backup_file


def _batch_config_from_row(row, batch=""):
    """把 assignment_batches 的一行还原成规范化配置（JSON 列解析失败按空值处理，不抛）。"""
    contract = _r2_contract()
    row = dict(row or {})
    raw = {}
    for key, default in (("expected_students", []), ("expected_questions", []),
                         ("scored_questions", []), ("full_scores", {}), ("rubrics", {}),
                         ("rubric", {})):
        value = row.get(key)
        if isinstance(value, str):
            try:
                raw[key] = json.loads(value)
            except (TypeError, ValueError):
                raw[key] = default
        elif value is None:
            raw[key] = default
        else:
            raw[key] = value
    raw["batch"] = row.get("batch") or batch or ""
    raw["name"] = row.get("name") or raw["batch"]
    raw["occurred_on"] = row.get("occurred_on") or ""
    raw["rubric_version"] = row.get("rubric_version") or ""
    raw["origin"] = row.get("origin") or contract.BATCH_ORIGIN_LEGACY
    raw["config_state"] = row.get("config_state") or contract.BATCH_CONFIG_STATE_UNCONFIGURED
    config, _errors = contract.normalize_batch_config(raw, batch=raw["batch"])
    # 库里记的是权威状态：不能因为「名单本来就有值」就把未配置的旧行读成已配置
    config["config_state"] = raw["config_state"]
    config["origin"] = raw["origin"]
    config["class_id"] = row.get("class_id")
    config["id"] = row.get("id")
    config["created_at"] = row.get("created_at") or ""
    config["updated_at"] = row.get("updated_at") or ""
    return config


def batch_config_row(db, class_id, batch):
    """读一行原始批次配置（没有登记返回 None）。表不存在时也返回 None（未升级的旧库）。"""
    names = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assignment_batches" not in names:
        return None
    return db.query_one(
        "SELECT * FROM assignment_batches WHERE class_id=? AND batch=?",
        (int(class_id), str(batch or "")),
    )


def load_batch_config(db, class_id, batch):
    """读规范化后的批次配置；没有登记时返回「未配置」缺省值（不写库、不抛异常）。

    统计与评分都从这里取口径，因此「未配置」必须是一等公民：`config_state =
    'unconfigured'`，应作答人数/未提交数/作业总分等指标显示「暂无」，而不是拿 0 冒充。
    """
    contract = _r2_contract()
    row = batch_config_row(db, class_id, batch)
    if row is None:
        config, _errors = contract.normalize_batch_config({}, batch=batch)
        config["config_state"] = contract.BATCH_CONFIG_STATE_UNCONFIGURED
        return config
    return _batch_config_from_row(row, batch=batch)


def list_batch_configs(db, class_id):
    """列出该班已登记的批次配置（按日期→批次名排序，便于界面下拉）。"""
    names = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assignment_batches" not in names:
        return []
    rows = db.query(
        "SELECT * FROM assignment_batches WHERE class_id=?"
        " ORDER BY occurred_on DESC, batch ASC",
        (int(class_id),),
    )
    return [_batch_config_from_row(row) for row in rows]


def save_batch_config(db, class_id, batch, config, user_id=None):
    """写入/更新批次配置并返回规范化后的配置；不合法抛 ValueError（中文消息，含错误码）。

    校验口径在 `r2_contract.batch_config_errors()`：应评分题必须在应作答题号内、每题满分
    > 0、给了评分标准就要有 rubric_version。名单/题号可以为空（= 未配置），那是合法的。
    """
    contract = _r2_contract()
    normalized, errors = contract.normalize_batch_config(config, batch=batch)
    if errors:
        messages = []
        for code, detail in errors:
            label = contract.BATCH_CONFIG_CODES.get(code, code)
            messages.append("%s（%s%s）" % (label, code, "：" + detail if detail else ""))
        raise ValueError("批次配置不合法：" + "；".join(messages))
    stamp = now_iso()
    payload = (
        normalized["name"],
        normalized["occurred_on"],
        json.dumps(normalized["expected_students"], ensure_ascii=False),
        json.dumps(list(normalized["expected_questions"]), ensure_ascii=False),
        json.dumps(list(normalized["scored_questions"]), ensure_ascii=False),
        json.dumps(normalized["full_scores"], ensure_ascii=False),
        json.dumps(normalized["rubrics"], ensure_ascii=False),
        json.dumps(normalized["rubric"], ensure_ascii=False),
        normalized["rubric_version"],
        normalized["config_state"],
        normalized["origin"],
        int(user_id) if user_id is not None else None,
        stamp,
    )
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id FROM assignment_batches WHERE class_id=? AND batch=?",
            (int(class_id), str(batch or "")),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO assignment_batches(class_id, batch, name, occurred_on,"
                " expected_students, expected_questions, scored_questions, full_scores,"
                " rubrics, rubric, rubric_version, config_state, origin, created_by,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(class_id), str(batch or "")) + payload + (stamp,),
            )
        else:
            conn.execute(
                "UPDATE assignment_batches SET name=?, occurred_on=?, expected_students=?,"
                " expected_questions=?, scored_questions=?, full_scores=?, rubrics=?, rubric=?,"
                " rubric_version=?, config_state=?, origin=?, created_by=?, updated_at=?"
                " WHERE id=?",
                payload + (int(row[0] if not isinstance(row, dict) else row["id"]),),
            )
    return load_batch_config(db, class_id, batch)


def ensure_batch_config(db, class_id, batch, origin=None, user_id=None):
    """按需登记一个批次（已存在则原样返回，不覆盖老师的现有配置）。

    R2-01 导入时用它「先登记再导数据」：新批次拿到的是一份未配置行（名单/满分待填），
    历史批次则不会因为再次导入就被清空配置。
    """
    contract = _r2_contract()
    row = batch_config_row(db, class_id, batch)
    if row is not None:
        return _batch_config_from_row(row, batch=batch)
    return save_batch_config(
        db, class_id, batch,
        {"origin": origin or contract.BATCH_ORIGIN_IMPORT,
         "config_state": contract.BATCH_CONFIG_STATE_UNCONFIGURED},
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# R2-00.5：学情报告导出历史的读写（只做插入与按账号倒序读，不做业务判断）
# ---------------------------------------------------------------------------
REPORT_EXPORT_COLUMNS = (
    "id", "user_id", "class_id", "kind", "student_id", "title", "purpose",
    "data_version", "scope", "batch", "path", "sha256", "bytes", "generated_at", "created_at",
)


def report_exports_ready(db):
    """只读判断当前库有没有导出历史表（旧库还没升级时读侧要能安全退化）。"""
    names = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    return "r2_report_exports" in names


def add_report_export(db, user_id, class_id, kind, student_id=0, purpose="", scope="",
                      batch="", path="", sha256="", bytes=0, generated_at="", title="",
                      data_version="", created_at=None):
    """记一条「谁在什么时候导出了哪份报告」，返回新行 id。

    参数口径（与 R2-04 的报告元数据一一对应）：
      user_id      导出账号（`users.id`）
      class_id     所属班级
      kind         `class`（班级报告）/ `student`（个人报告）
      student_id   个人报告的学生；班级报告传 0（不用 NULL，省掉一处三值判断）
      title        导出时的**报告标题原样快照**：班级/学生后来改名，历史标题不能跟着变
      purpose      老师填的导出用途
      data_version 导出时的数据版本串（如「统计 R2-03.1 / 契约 R2-00.4」）——这是历史事实，
                   不能用当前常量反推，否则 R2-06 升版后旧记录会被改写成新版本
      scope        筛选范围（dict 会转成 JSON 串；字符串原样存）
      batch        批次（空 = 未指定）
      path         保存路径
      sha256       导出文件校验值
      bytes        文件字节数
      generated_at 报告生成时间（快照时间，不是写入时间）
      created_at   入库时间，缺省取当前时间

    **不做任何业务判断**（不校验 kind 取值、不检查班级归属、不截断历史）：那些属于
    `report_pdf`/`api` 的职责；db 层只保证「写进去的东西一字不差地读得出来」。
    表不存在时先建表（幂等，照 `ensure_ai_egress_audit` 的既有做法），这样老库在
    captain 接线完成前也不会因为少一张表而丢历史。
    """
    if scope and not isinstance(scope, str):
        scope = json.dumps(scope, ensure_ascii=False, sort_keys=True)
    stamp = created_at or now_iso()
    payload = (
        int(user_id), int(class_id), str(kind or "class"), int(student_id or 0),
        str(title or ""), str(purpose or ""), str(data_version or ""),
        str(scope or ""), str(batch or ""), str(path or ""),
        str(sha256 or ""), int(bytes or 0), str(generated_at or ""), stamp,
    )
    with db.tx() as conn:
        for ddl in REPORT_EXPORTS_DDL:
            conn.execute(ddl)
        cursor = conn.execute(
            "INSERT INTO r2_report_exports(user_id, class_id, kind, student_id, title,"
            " purpose, data_version, scope, batch, path, sha256, bytes, generated_at,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
        return int(cursor.lastrowid)


def list_report_exports(db, user_id, limit=50, class_id=None, kind=None):
    """按账号倒序列出最近 `limit` 条导出历史（表不存在时返回 []，不抛异常）。

    `user_id` **必填**：导出历史是账号级数据，不提供「不传账号就读全部」的入口
    （避免调用方一不留神跨账号读出别人的导出记录）。同账号内可用 `class_id` / `kind`
    再过滤，界面「本班历史」就用这两个参数。
    `limit` 只影响**读取条数**，不删除任何行：归档上限由调用方决定（派单要求留痕 ≥ 六个月，
    所以 db 层绝不静默截断）。排序用 `(created_at DESC, id DESC)`，同一秒内多次导出也有稳定顺序。
    """
    if not report_exports_ready(db):
        return []
    limit = max(1, int(limit or 1))
    sql = ("SELECT * FROM r2_report_exports WHERE user_id=?")
    params = [int(user_id)]
    if class_id is not None:
        sql += " AND class_id=?"
        params.append(int(class_id))
    if kind:
        sql += " AND kind=?"
        params.append(str(kind))
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, tuple(params))


class Database:
    """线程安全的 SQLite 封装：每个线程一条连接，写操作显式事务。"""

    def __init__(self, path, create=True):
        """create=True：新建库（执行建表、补列、写结构版本）；create=False：只打开已有库，
        绝不执行任何建表/补列/补索引/改结构版本。"""
        self.path = path
        self._create = create
        self._local = threading.local()
        if create:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            conn = self.conn
            conn.executescript(SCHEMA)
            self._ensure_columns()
            with self.tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta(key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
        elif not os.path.exists(path):
            raise FileNotFoundError("数据库文件不存在：%s" % path)

    # -- 结构升级 ---------------------------------------------------------
    def _ensure_columns(self):
        """给旧库补上新增列；先查 PRAGMA，避免重复 ALTER 报错。"""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(mistakes)")}
        additions = (
            ("ai_error", "TEXT NOT NULL DEFAULT ''"),
            ("ai_error_code", "TEXT NOT NULL DEFAULT ''"),
            ("ai_likely_correct", "INTEGER NOT NULL DEFAULT 0"),
        )
        for name, ddl in additions:
            if name not in existing:
                self.conn.execute("ALTER TABLE mistakes ADD COLUMN %s %s" % (name, ddl))
        existing_users = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
        if "backup_key" not in existing_users:
            self.conn.execute("ALTER TABLE users ADD COLUMN backup_key TEXT NOT NULL DEFAULT ''")
        # R1-14：教师账号级「个体 AI 外发」开关，默认关（0）。桌面端 create=False 打开旧库时
        # 不会走到这里，由 ensure_ai_switch_column() 在写入开关时补列（读侧缺列按「关」处理）。
        # R1-16：「作答核对 → 错题」的幂等键（create=True 直接打开旧库时也补上）
        _add_missing_columns(self.conn, "mistakes", MISTAKE_ANSWER_COLUMNS)
        for ddl in MISTAKE_ANSWER_DDL:
            self.conn.execute(ddl)
        if USER_AI_SWITCH_COLUMN[0] not in existing_users:
            self.conn.execute(
                "ALTER TABLE users ADD COLUMN %s %s" % USER_AI_SWITCH_COLUMN)
        existing_students = {row[1] for row in self.conn.execute("PRAGMA table_info(students)")}
        if "code" not in existing_students:
            self.conn.execute("ALTER TABLE students ADD COLUMN code TEXT NOT NULL DEFAULT ''")
        # 作答核对表的新列：server.py 启动时会走 ensure_answer_check_schema；
        # 这里再兜一次，保证「直接 Database(create=True) 打开旧库」也能读到新列
        # （桌面端、测试夹具、备份恢复都走这条路）。
        for table, columns in (("answer_checks", ANSWER_CHECK_COLUMNS),
                               ("answer_followups", ANSWER_FOLLOWUP_COLUMNS)):
            tables = {row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if table in tables:
                _add_missing_columns(self.conn, table, columns)
        # T6：撤掉两个「演示批次归位」触发器（隐形行为）。旧库里若已建过，打开时显式删除；
        # 新库根本不会再建。删掉之后「演示数据不进真实作业/统计」由查询侧的
        # (batch, source) 收口与 stats 的 a.source 过滤保证，不依赖任何隐式写入改写。
        for name in RETIRED_TRIGGER_NAMES:
            self.conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        # R2（1.9.0）：新库由 SCHEMA 直接建好 9 张新表；用 create=True 打开一份旧库时，
        # 这里把历史批次登记成「未配置」的批次配置行（幂等，只 INSERT、不改 answers.batch）。
        # 桌面端走的是 create=False + service 里的 ensure_r2_schema（见 docs/R2_接口契约.md
        # §迁移方案第 3 步的接线片段），两条路径最终得到同一份结构。
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "assignment_batches" in names:
            # R2-00.4：题级评分标准版本列（老 R2 库用 create=True 打开时补空列，不动旧行）
            _add_missing_columns(self.conn, "assignment_batches", R2_BATCH_EXTRA_COLUMNS)
            _seed_assignment_batches(self.conn)

    # -- 连接 -------------------------------------------------------------
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self._create:
            # 只有新建库才设置 WAL；打开已有库不改变其日志模式（那也是一次持久写入）。
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self):
        """写事务：BEGIN IMMEDIATE，异常回滚。"""
        conn = self.conn
        if conn.in_transaction:
            # 已在事务中（例如批量调用），直接复用，由最外层提交
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")

    # -- 查询辅助 ---------------------------------------------------------
    def query(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def scalar(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None
