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
    # 库级不变量：演示写入的作答只能落在演示批次里。用 AFTER 触发器「归位」而不是 BEFORE
    # 里 RAISE，是为了不把任何一次导入变成报错或丢行——数据一条不少，只是回到它该在的批次。
    """CREATE TRIGGER IF NOT EXISTS trg_answers_demo_batch_ins
AFTER INSERT ON answers
WHEN NEW.source = '{source}' AND substr(NEW.record_key, 1, {plen}) = '{prefix}'
     AND NEW.batch <> '{batch}'
BEGIN
    UPDATE answers SET batch = '{batch}' WHERE id = NEW.id;
END""".format(source=DEMO_SOURCE, prefix=DEMO_RECORD_PREFIX, batch=DEMO_BATCH,
              plen=len(DEMO_RECORD_PREFIX)),
    """CREATE TRIGGER IF NOT EXISTS trg_answers_demo_batch_upd
AFTER UPDATE OF source, record_key, batch ON answers
WHEN NEW.source = '{source}' AND substr(NEW.record_key, 1, {plen}) = '{prefix}'
     AND NEW.batch <> '{batch}'
BEGIN
    UPDATE answers SET batch = '{batch}' WHERE id = NEW.id;
END""".format(source=DEMO_SOURCE, prefix=DEMO_RECORD_PREFIX, batch=DEMO_BATCH,
              plen=len(DEMO_RECORD_PREFIX)),
)

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
    returned_reason       TEXT NOT NULL DEFAULT ''
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
""" + "\n".join(ddl.rstrip(";") + ";" for ddl in ANSWERS_DDL + REVIEWS_DDL + TUTOR_DDL + FOLLOWUP_DDL + ANSWER_CHECK_DDL) + """
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
)

# 库级不变量（演示作答只能待在演示批次）由这两个触发器保证；缺一个就当结构没升级到位。
ANSWER_TRIGGER_NAMES = (
    "trg_answers_demo_batch_ins",
    "trg_answers_demo_batch_upd",
)


def answers_schema_ready(db_file):
    """只读判断已有库是否已有作答记录表、作答审核表、学生编号列、索引与演示批次触发器。"""
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        objects = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')")
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
    finally:
        conn.close()
    return (
        "answers" in tables
        and "answer_reviews" in tables
        and "code" in columns
        and all(name in objects for name in ANSWER_INDEX_NAMES)
        and all(name in objects for name in ANSWER_TRIGGER_NAMES)
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
            conn.execute(
                "UPDATE answers SET batch=? WHERE source=? AND record_key LIKE ? AND batch<>?",
                (DEMO_BATCH, DEMO_SOURCE, DEMO_RECORD_PREFIX + "%", DEMO_BATCH),
            )
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
            for ddl in ANSWER_CHECK_DDL[:3]:
                conn.execute(ddl)                       # 两张表 + 两个普通索引
            _add_missing_columns(conn, "answer_checks", ANSWER_CHECK_COLUMNS)
            _add_missing_columns(conn, "answer_followups", ANSWER_FOLLOWUP_COLUMNS)
            # 历史行没有来源记录：有结论的按历史行标注，避免读成「未判断」。
            conn.execute(
                "UPDATE answer_checks SET cause_source='legacy'"
                " WHERE (cause_source IS NULL OR cause_source='') AND cause_code <> ''"
            )
            conn.execute(ANSWER_CHECK_DDL[4])           # 追问表
            _dedupe_answer_checks(conn)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_checks_answer_version"
                " ON answer_checks(answer_id, answer_version) WHERE answer_version <> ''"
            )
            conn.execute(ANSWER_CHECK_DDL[5])
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
