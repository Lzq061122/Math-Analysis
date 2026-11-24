"""SQLite 存储层：建表、连接管理、通用查询辅助。

所有时间统一存为带时区偏移的 ISO-8601 字符串（例如 2026-11-06T11:54:48+08:00），
字典序即时间序，便于按时间筛选。
"""

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

SCHEMA_VERSION = "2"

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
