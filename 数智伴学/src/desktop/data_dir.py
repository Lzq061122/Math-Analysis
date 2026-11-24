"""数据目录的识别与检查。

桌面端不会在背后另建一份数据库：选中的目录要么是已有的 1.3.0 数据目录
（含 app.db，结构版本一致），要么由老师明确点「新建」才创建。
"""
import os
import sqlite3

from app.db import SCHEMA_VERSION, REQUIRED_INDEXES, REQUIRED_TABLES

DB_NAME = "app.db"

READY = "ready"
EMPTY = "empty"
MISSING = "missing"
BROKEN = "broken"
INCOMPATIBLE = "incompatible"
UNWRITABLE = "unwritable"


def db_path(data_dir):
    return os.path.join(os.path.abspath(data_dir), DB_NAME)


def _db_problem(db_file):
    """只读检查现有 app.db 是否结构完整可用；返回问题说明，完整则 None。

    不看版本号就通过会漏掉「只有 app_meta 表」的残缺库；这里逐表逐字段核对
    app.db.REQUIRED_TABLES，缺表、缺字段、版本不符、损坏都拒绝，绝不自动补。
    """
    uri = "file:%s?mode=ro" % db_file.replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = ""
        if "app_meta" in tables:
            row = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
            version = row[0] if row else ""
        if version != SCHEMA_VERSION:
            return "app.db 的结构版本是 %s，这个桌面测试版只支持 %s" % (version or "未知", SCHEMA_VERSION)
        missing_tables = [name for name in REQUIRED_TABLES if name not in tables]
        if missing_tables:
            return "app.db 缺少必要的数据表：%s（这可能是残缺或损坏的数据，不会被自动修补）" % "、".join(missing_tables)
        for table, columns in REQUIRED_TABLES.items():
            existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
            missing = [name for name in columns if name not in existing]
            if missing:
                return "app.db 的 %s 表缺少字段：%s（结构不兼容，不会被自动修补）" % (table, "、".join(missing))
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for table, names in REQUIRED_INDEXES.items():
            missing = [name for name in names if name not in indexes]
            if missing:
                return "app.db 缺少必要的索引：%s（结构不兼容，不会被自动修补）" % "、".join(missing)
    finally:
        conn.close()
    return None


def describe(data_dir):
    """返回 (状态, 给老师看的说明)。只读检查，不会创建或改动任何文件。"""
    path = os.path.abspath(data_dir)
    if not os.path.exists(path):
        return MISSING, "这个目录还不存在"
    if not os.path.isdir(path):
        return BROKEN, "这个路径不是文件夹"
    db_file = os.path.join(path, DB_NAME)
    if not os.path.exists(db_file):
        try:
            others = os.listdir(path)
        except OSError as exc:
            return UNWRITABLE, "这个目录打不开：%s" % exc
        if others:
            return EMPTY, "这个目录里没有 app.db，不是 1.3.0 的数据目录"
        return EMPTY, "这个目录是空的，可以新建一份数据"
    try:
        problem = _db_problem(db_file)
    except sqlite3.DatabaseError as exc:
        return BROKEN, "app.db 打不开（%s），可能已损坏" % exc
    if problem:
        return INCOMPATIBLE, problem
    if not os.access(path, os.W_OK):
        return UNWRITABLE, "这个目录不能写入，请换一个位置或检查权限"
    return READY, "已找到 1.3.0 数据目录"


def create_new(data_dir):
    """新建一份空数据。目录必须不存在或为空，避免覆盖已有文件。"""
    from app.db import Database

    path = os.path.abspath(data_dir)
    if os.path.exists(path):
        if not os.path.isdir(path):
            raise ValueError("这个路径不是文件夹")
        if os.listdir(path):
            raise ValueError("这个目录里已经有文件，不能当作新数据目录")
    else:
        os.makedirs(path)
    Database(os.path.join(path, DB_NAME))
    return path


__all__ = ["DB_NAME", "READY", "EMPTY", "MISSING", "BROKEN", "INCOMPATIBLE", "UNWRITABLE",
           "db_path", "describe", "create_new"]
