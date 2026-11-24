"""按老师账号备份、恢复与跨电脑导入。

备份结构（format 3，scope=teacher）：
    manifest.json   清单（账号、backup_key、条数、每个文件的 sha256）
    app.db          只含该账号数据的数据库快照（不含密码、会话）
    uploads/...     该账号错题照片

账号归属用 users.backup_key 判断（随机 32 位十六进制，注册时生成），不依赖
数字 id 和用户名：换电脑重新注册后 id 会变，用户名也可能被其他老师使用。

format 2 是 1.2.1 及更早版本的备份（清单里没有 backup_key）。它无法可靠确认
归属：只有经过明确确认的「恢复旧备份」或「从其他电脑导入」流程才会被接受，
绝不静默当作当前账号备份。

恢复与导入分两步：服务端校验并把内容解压到独立任务暂存目录；服务停止后由
启动器把数据合并进现有数据库。合并只动当前账号的数据，其他账号不受影响；
失败时事务回滚，原数据保持可用。结果按阶段如实记录在 .restore-result.json：
    prep_failed      尚未修改数据
    rolled_back      修改失败且已回滚
    restored         数据已恢复，收尾完成
    finalize_failed  数据已恢复，但结果记录写入失败
    unknown          数据库状态不确定（回滚失败）
状态为 unknown 时服务会拒绝所有修改请求，重开软件也不会解除，需按提示联系
技术人员处理。
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime

from .version import VERSION

FORMAT = 3
LEGACY_FORMAT = 2
SCOPE_TEACHER = "teacher"
APP_TAG = "math-mistake-studio"
MAX_RESTORE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 20000
REQUIRED_TABLES = ("classes", "students", "mistakes", "advice")
RESULT_FILE = ".restore-result.json"
MARKER_FILE = ".restore-pending.json"
CLAIM_FILE = "upload.lock"
TASKS_DIR = "tasks"

DRIVE_RE = re.compile(r"^[A-Za-z]:")


class BackupError(Exception):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(conn):
    return {
        "classes": conn.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
        "students": conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
        "mistakes": conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0],
    }


def _photo_names(conn):
    return sorted({
        os.path.basename(row[0])
        for row in conn.execute("SELECT image_file FROM mistakes WHERE COALESCE(image_file, '') <> ''")
    })


def ensure_user_backup_key(db_path, user_id):
    """旧账号（1.2.1 之前注册）没有 backup_key：第一次备份时生成并保存。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT backup_key FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise BackupError("账号不存在，无法备份")
        key = row[0] or ""
        if not key:
            key = uuid.uuid4().hex
            conn.execute("UPDATE users SET backup_key=? WHERE id=?", (key, user_id))
            conn.commit()
        return key
    finally:
        conn.close()


def create_backup(data_dir, zip_path, user_id, app_version=VERSION):
    """把 user_id 的班级、学生、错题、建议和照片打成 zip，返回清单。"""
    db_path = os.path.join(data_dir, "app.db")
    if not os.path.isfile(db_path):
        raise BackupError("还没有可备份的数据：数据库不存在")

    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    backup_key = ensure_user_backup_key(db_path, user_id)
    fd, tmp_db = tempfile.mkstemp(prefix=".backup-", suffix=".db", dir=data_dir)
    os.close(fd)
    try:
        source = sqlite3.connect(db_path)
        try:
            target = sqlite3.connect(tmp_db)
            try:
                source.backup(target)
                target.row_factory = sqlite3.Row
                target.execute("PRAGMA foreign_keys = ON")
                owner = target.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
                if not owner:
                    raise BackupError("账号不存在，无法备份")
                target.execute("DELETE FROM classes WHERE user_id<>?", (user_id,))
                target.execute("DELETE FROM sessions")
                target.execute("DELETE FROM users WHERE id<>?", (user_id,))
                target.execute("UPDATE users SET pwd_hash='' WHERE id=?", (user_id,))
                target.commit()
                counts = _counts(target)
                photos = _photo_names(target)
                counts["photos"] = len(photos)
                uploads_dir = os.path.join(data_dir, "uploads")
                photo_items = []
                manifest_uploads = []
                for name in photos:
                    path = os.path.join(uploads_dir, name)
                    if not os.path.isfile(path):
                        raise BackupError("错题照片缺失，无法备份：%s" % name)
                    item = {"name": "uploads/" + name, "size": os.path.getsize(path), "sha256": _sha256(path)}
                    photo_items.append(item)
                    manifest_uploads.append(path)
            finally:
                target.close()
        finally:
            source.close()

        manifest = {
            "app": APP_TAG,
            "format": FORMAT,
            "scope": SCOPE_TEACHER,
            "app_version": app_version,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "owner": {"id": owner["id"], "username": owner["username"], "backup_key": backup_key},
            "counts": counts,
            "db": {"name": "app.db", "size": os.path.getsize(tmp_db), "sha256": _sha256(tmp_db)},
            "uploads": photo_items,
            "notice": "这份备份只含本账号的数据，内含学生姓名与错题照片，请妥善保管。",
        }
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            bundle.write(tmp_db, "app.db")
            for item, path in zip(photo_items, manifest_uploads):
                bundle.write(path, item["name"])
        return manifest
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass


def _safe_member(name):
    """拒绝绝对路径、盘符、反斜杠、目录穿越、控制字符和空文件名。"""
    if not name or name.strip() != name:
        raise BackupError("备份里有不合法的文件名：%r" % name)
    if name.startswith("/") or name.startswith("\\") or DRIVE_RE.match(name) or "\\" in name:
        raise BackupError("备份里有不安全的文件路径：%s" % name)
    if any(ord(ch) < 32 for ch in name):
        raise BackupError("备份里有不合法的文件名：%r" % name)
    parts = name.split("/")
    if ".." in parts or "." in parts:
        raise BackupError("备份里有不安全的文件路径：%s" % name)
    if parts[0] not in ("manifest.json", "app.db", "uploads"):
        raise BackupError("备份里有不符合结构的文件：%s" % name)
    if parts[0] == "uploads" and len(parts) != 2:
        raise BackupError("备份里有不符合结构的照片路径：%s" % name)
    return name


def _extract(bundle, infos, workdir):
    root = os.path.realpath(workdir)
    for name in sorted(infos):
        target = os.path.realpath(os.path.join(root, name))
        if target != root and not target.startswith(root + os.sep):
            raise BackupError("备份里有不安全的文件路径：%s" % name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with bundle.open(name) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def inspect_backup(zip_path, workdir, user=None):
    """校验备份完整性并解压到 workdir，返回 (manifest, relation)。

    user 为当前账号行 {"id", "username", "backup_key"}（可为 None，仅预览时）。
    relation 描述备份与当前账号的关系：
        same_key        backup_key 相同（可靠确认是本人账号）
        legacy          format 2 或清单没有 backup_key（旧版备份）
        id_match        数字 id 相同
        username_match  用户名相同
    不做归属裁决（是否允许恢复/导入由调用方按 relation 决定）。
    """
    if not zipfile.is_zipfile(zip_path):
        raise BackupError("这不是有效的 zip 备份文件")
    if os.path.isdir(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir)

    with zipfile.ZipFile(zip_path) as bundle:
        infolist = [info for info in bundle.infolist() if not info.is_dir()]
        if len(infolist) > MAX_MEMBERS:
            raise BackupError("备份里文件太多，无法恢复")
        infos = {}
        for info in infolist:
            name = _safe_member(info.filename)
            if name in infos:
                raise BackupError("备份里有重复的文件名：%s" % name)
            infos[name] = info
        total = sum(info.file_size for info in infos.values())
        if total > MAX_RESTORE_BYTES:
            raise BackupError("备份内容过大，无法恢复")
        if "manifest.json" not in infos or "app.db" not in infos:
            raise BackupError("备份不完整：缺少 manifest.json 或 app.db")
        try:
            manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BackupError("备份清单损坏，无法读取")
        if manifest.get("app") != APP_TAG:
            raise BackupError("这不是本软件的备份文件")
        if manifest.get("format") not in (FORMAT, LEGACY_FORMAT):
            raise BackupError("备份格式不受支持，请用同一版本软件导出的备份")
        if manifest.get("scope") != SCOPE_TEACHER:
            raise BackupError("这不是老师个人备份，无法恢复到账号里")
        owner = manifest.get("owner") or {}
        if not isinstance(owner.get("id"), int):
            raise BackupError("备份清单缺少账号信息，无法确认归属")

        listed = {"manifest.json", "app.db"}
        for item in manifest.get("uploads") or []:
            listed.add(_safe_member(item.get("name") or ""))
        extra = set(infos) - listed
        if extra:
            raise BackupError("备份里有未登记的文件：%s" % sorted(extra)[0])
        missing = listed - set(infos)
        if missing:
            raise BackupError("备份不完整：缺少 %s" % sorted(missing)[0])

        _extract(bundle, infos, workdir)

    db_file = os.path.join(workdir, "app.db")
    if _sha256(db_file) != (manifest.get("db") or {}).get("sha256"):
        raise BackupError("app.db 校验失败，备份可能损坏")
    for item in manifest.get("uploads") or []:
        path = os.path.join(workdir, item["name"])
        if not os.path.isfile(path) or _sha256(path) != item.get("sha256"):
            raise BackupError("照片 %s 校验失败，备份可能损坏" % item["name"])

    try:
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()
            if not ok or ok[0] != "ok":
                raise BackupError("数据库完整性检查未通过")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing_tables = [name for name in REQUIRED_TABLES if name not in tables]
            if missing_tables:
                raise BackupError("数据库缺少数据表：%s" % missing_tables[0])
            foreign = conn.execute("SELECT COUNT(*) FROM classes WHERE user_id<>?", (owner["id"],)).fetchone()[0]
            if foreign:
                raise BackupError("备份里混有其他账号的班级，已拒绝恢复")
            counts = _counts(conn)
            for key in ("classes", "students", "mistakes"):
                if counts[key] != (manifest.get("counts") or {}).get(key):
                    raise BackupError("备份清单与数据库条数不一致：%s" % key)
            photos = _photo_names(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError("数据库无法读取：%s" % exc)

    if len(photos) != (manifest.get("counts") or {}).get("photos"):
        raise BackupError("备份清单与照片数量不一致")
    bundled = {os.path.basename(item["name"]) for item in manifest.get("uploads") or []}
    for name in photos:
        if name not in bundled:
            raise BackupError("备份缺少错题照片：%s" % name)

    owner_key = str(owner.get("backup_key") or "")
    relation = {
        "same_key": bool(owner_key and user and owner_key == str(user.get("backup_key") or "")),
        "legacy": manifest.get("format") == LEGACY_FORMAT or not owner_key,
        "id_match": bool(user and owner.get("id") == user.get("id")),
        "username_match": bool(user and (owner.get("username") or "") == (user.get("username") or "")),
    }
    manifest["_workdir"] = workdir
    return manifest, relation


# -- 任务占用与暂存目录：同一份数据只允许一个恢复/导入任务 ----------------

def _restore_dir(data_dir):
    return os.path.join(data_dir, ".restore")


def pending_path(data_dir):
    return os.path.join(data_dir, MARKER_FILE)


STATE_NONE = "none"
STATE_OK = "ok"
STATE_BAD = "bad"


def read_state_file(path):
    """读恢复状态文件，返回 (状态, 内容)。

    none=文件不存在；bad=文件存在但读不出来、不是 UTF-8、不是合法 JSON 或内容为空；
    ok=解析成功，结构是否合用由调用方继续核对。
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return STATE_NONE, None
    except OSError:
        return STATE_BAD, None
    if not raw.strip():
        return STATE_BAD, None
    try:
        return STATE_OK, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return STATE_BAD, None


def pending_state(data_dir):
    return read_state_file(pending_path(data_dir))


def read_pending(data_dir):
    state, data = pending_state(data_dir)
    if state != STATE_OK or not isinstance(data, dict) or not isinstance(data.get("task_dir"), str) or not data["task_dir"]:
        return None
    return data


def pending_problem(data_dir):
    """返回待处理任务的说明；没有任务时返回 None。文件损坏同样算作「有待处理」。"""
    state, data = pending_state(data_dir)
    if state == STATE_NONE:
        return None
    if state == STATE_BAD:
        return "待处理任务标记 %s 存在但无法读取或内容不完整" % MARKER_FILE
    if not isinstance(data, dict) or not isinstance(data.get("task_dir"), str) or not data["task_dir"]:
        return "待处理任务标记 %s 存在但内容不符合要求" % MARKER_FILE
    return "检测到一次尚未处理完的恢复/导入任务"


def write_pending(data_dir, payload):
    with open(pending_path(data_dir), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def claim_upload(data_dir):
    """原子占用上传入口并创建本任务独立目录，返回 task_dir；冲突抛 BackupError。"""
    base = _restore_dir(data_dir)
    os.makedirs(base, exist_ok=True)
    lock_path = os.path.join(base, CLAIM_FILE)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise BackupError("已有一个备份恢复或导入任务正在进行，请等它完成后再试")
    try:
        os.write(fd, b"1")
    finally:
        os.close(fd)
    task_dir = os.path.join(base, TASKS_DIR, uuid.uuid4().hex)
    os.makedirs(task_dir)
    return task_dir


def clear_upload_claim(data_dir):
    """任务成功进入等待队列后释放上传占用（任务目录保留给启动器）。"""
    try:
        os.remove(os.path.join(_restore_dir(data_dir), CLAIM_FILE))
    except OSError:
        pass


def release_upload(data_dir, task_dir):
    """校验失败或上传中断：只清理本任务的文件，并释放上传占用。"""
    if task_dir:
        shutil.rmtree(task_dir, ignore_errors=True)
    clear_upload_claim(data_dir)


def task_meta_path(task_dir):
    return os.path.join(task_dir, "meta.json")


def write_task_meta(task_dir, payload):
    with open(task_meta_path(task_dir), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def read_task_meta(task_dir):
    try:
        with open(task_meta_path(task_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def task_dir_for(data_dir, task_id):
    base = os.path.join(_restore_dir(data_dir), TASKS_DIR)
    path = os.path.join(base, task_id)
    real = os.path.realpath(base)
    candidate = os.path.realpath(path)
    if candidate != real and not candidate.startswith(real + os.sep):
        raise BackupError("任务编号不合法")
    return path if os.path.isdir(path) else None


def cleanup_stale_tasks(data_dir):
    """服务启动时清理上次残留：释放上传占用、删除未被待办任务引用的任务目录。

    数据状态不确定（phase=unknown / rollback_failed）或状态文件读不出来时保留全部现场，不做清理。
    """
    result_stat, result = result_state(data_dir)
    if result_stat == STATE_BAD:
        return
    if result and (result.get("rollback_failed") or result.get("phase") == "unknown"):
        return
    if pending_state(data_dir)[0] == STATE_BAD:
        return
    pending = read_pending(data_dir)
    referenced = os.path.abspath(pending.get("task_dir")) if pending and pending.get("task_dir") else None
    base = os.path.join(_restore_dir(data_dir), TASKS_DIR)
    if os.path.isdir(base):
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if os.path.isdir(path) and os.path.abspath(path) != referenced:
                shutil.rmtree(path, ignore_errors=True)
    clear_upload_claim(data_dir)


# -- 恢复：把暂存内容合并进当前数据库 -------------------------------------

CLASS_COLUMNS = ("name", "grade", "note", "created_at", "updated_at")
STUDENT_COLUMNS = ("name", "note", "active", "created_at", "updated_at")
MISTAKE_COLUMNS = (
    "status", "source_type", "source_detail", "note", "occurred_on",
    "image_file", "image_mime", "image_width", "image_height",
    "question_text", "solution_text", "knowledge_points", "error_type",
    "observed_errors", "possible_causes", "advice_text",
    "insufficient", "insufficient_reason", "ai_snapshot", "ai_mode",
    "ai_error", "ai_error_code", "ai_likely_correct", "teacher_edited_fields",
    "teacher_note", "created_at", "updated_at", "confirmed_at", "confirmed_by",
    "returned_at", "returned_reason",
)
ADVICE_COLUMNS = ("scope", "content", "evidence", "teacher_edited", "generated_at", "updated_at")


def _fault(name):
    if name in (os.environ.get("MISTAKE_FAULT") or "").split(","):
        raise BackupError("模拟故障：%s" % name)


def _insert(conn, table, columns, values):
    sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(columns), ", ".join("?" for _ in columns))
    cur = conn.execute(sql, values)
    return cur.lastrowid


def _write_result(data_dir, payload):
    path = os.path.join(data_dir, RESULT_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def result_state(data_dir):
    return read_state_file(os.path.join(data_dir, RESULT_FILE))


def read_result(data_dir):
    state, data = result_state(data_dir)
    if state != STATE_OK or not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return None
    return data


def clear_result(data_dir):
    try:
        os.remove(os.path.join(data_dir, RESULT_FILE))
    except OSError:
        pass


def write_failure(data_dir, message, phase="prep_failed"):
    _write_result(data_dir, {
        "ok": False,
        "phase": phase,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "error": message,
    })


def is_restore_blocked(data_dir):
    """状态不确定时阻止所有修改请求；重开软件后依然生效（结果文件持久）。

    状态文件存在但读不出来，同样按「状态不确定」处理，不能当成没有发生过恢复。
    """
    state, result = result_state(data_dir)
    if state == STATE_BAD:
        return True
    if state == STATE_NONE:
        return False
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return True
    return bool(result.get("rollback_failed") or result.get("phase") == "unknown")


def restore_problem(data_dir):
    """数据目录存在恢复异常或待处理恢复任务时，返回要提示老师的原因；否则返回 None。

    桌面版与网页版共用同一套状态文件，任何入口在写入数据前都必须先经过这里，
    不能绕过保护直接打开数据库。「文件损坏」与「文件不存在」必须区别对待：
    前者一律拒绝进入，且不删除标记、不解除保护、不自动换库。
    """
    state, result = result_state(data_dir)
    if state == STATE_BAD:
        return ("恢复状态文件 %s 存在但无法读取或内容不完整，无法确认上次恢复是否结束。"
                "为保护已有数据，已停止打开这个数据目录。请不要删除这个文件，"
                "保留整个软件文件夹并联系技术人员处理。" % RESULT_FILE)
    if state == STATE_OK:
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            return ("恢复状态文件 %s 存在但内容不符合要求（缺少 ok 字段），无法确认上次恢复是否结束。"
                    "为保护已有数据，已停止打开这个数据目录。请不要删除这个文件，"
                    "保留整个软件文件夹并联系技术人员处理。" % RESULT_FILE)
        if result.get("rollback_failed") or result.get("phase") == "unknown":
            return "上次恢复尚未完整结束，数据状态不确定。为保护已有数据，请保留这个数据目录并联系技术人员处理。"

    problem = pending_problem(data_dir)
    if problem == "检测到一次尚未处理完的恢复/导入任务":
        return "检测到一次尚未处理完的恢复/导入任务。为保护已有数据，请保留这个数据目录并联系技术人员处理。"
    if problem:
        return ("%s，无法确认是否有恢复/导入任务还没做完。为保护已有数据，已停止打开这个数据目录。"
                "请不要删除这个文件，保留整个软件文件夹并联系技术人员处理。" % problem)
    return None


def _remove_pending(data_dir, pending=None):
    if pending is None:
        pending = read_pending(data_dir)
    try:
        os.remove(pending_path(data_dir))
    except OSError:
        pass
    task_dir = (pending or {}).get("task_dir")
    if task_dir:
        shutil.rmtree(task_dir, ignore_errors=True)


def _live_user(db_path, user_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, username, backup_key FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()


def _user_class_count(db_path, user_id):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM classes WHERE user_id=?", (user_id,)).fetchone()[0]
    finally:
        conn.close()


def apply_staged(staging_dir, data_dir, user_id, app_version=VERSION, mode="restore", legacy_confirmed=False):
    """把暂存备份合并进 data_dir 里的数据库，只替换 user_id 的数据。

    mode="restore"：备份必须可靠地属于 user_id（backup_key 相同；旧版备份需
    legacy_confirmed=True 且 id 与用户名同时匹配）。
    mode="import"：把备份里的班级等重映射给 user_id，要求该账号还没有任何班级。

    返回 {"safety": 恢复前自动备份路径, "counts": 恢复后的条数}。
    任何一步失败都回滚事务、删除本次复制进来的照片，原数据保持可用；
    回滚失败或结果写入失败时在 .restore-result.json 里如实记录阶段。
    """
    manifest_path = os.path.join(staging_dir, "manifest.json")
    staged_db = os.path.join(staging_dir, "app.db")
    if not os.path.isfile(manifest_path) or not os.path.isfile(staged_db):
        raise BackupError("暂存目录不完整，无法恢复")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    db_path = os.path.join(data_dir, "app.db")
    if not os.path.isfile(db_path):
        raise BackupError("当前数据库不存在，无法合并恢复")

    owner = manifest.get("owner") or {}
    owner_key = str(owner.get("backup_key") or "")
    if mode == "import":
        if _user_class_count(db_path, user_id):
            raise BackupError("当前账号已有班级数据，导入只支持还没有数据的新账号")
    else:
        if owner_key:
            live = _live_user(db_path, user_id)
            if not live or owner_key != str(live["backup_key"] or ""):
                raise BackupError("暂存备份与当前账号不一致")
        else:
            if not legacy_confirmed:
                raise BackupError("旧版本备份必须经过明确确认后才能恢复")
            live = _live_user(db_path, user_id)
            if not live or owner.get("id") != live["id"] or (owner.get("username") or "") != live["username"]:
                raise BackupError("暂存备份与当前账号不一致")

    uploads_dir = os.path.join(data_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    backups = os.path.join(data_dir, "backups")
    os.makedirs(backups, exist_ok=True)
    safety = os.path.join(backups, "auto-before-restore-%s.zip" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    create_backup(data_dir, safety, user_id, app_version)

    copied = []
    mapping = {}
    try:
        for item in manifest.get("uploads") or []:
            name = os.path.basename(item["name"])
            target = os.path.join(uploads_dir, name)
            if os.path.exists(target):
                stem, ext = os.path.splitext(name)
                name = "%s-r%s%s" % (stem, uuid.uuid4().hex[:8], ext)
                target = os.path.join(uploads_dir, name)
            shutil.copyfile(os.path.join(staging_dir, item["name"]), target)
            copied.append(target)
            mapping[os.path.basename(item["name"])] = name
            _fault("photo")
    except Exception as exc:
        for path in copied:
            try:
                os.remove(path)
            except OSError:
                pass
        write_failure(data_dir, "恢复失败，原数据未改动：%s" % exc, phase="prep_failed")
        raise BackupError("恢复失败，原数据未改动：%s" % exc)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    old_photos = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        old_photos = [
            row[0] for row in conn.execute(
                "SELECT m.image_file FROM mistakes m JOIN classes c ON c.id=m.class_id WHERE c.user_id=?",
                (user_id,),
            ) if row[0]
        ]
        conn.execute("DELETE FROM classes WHERE user_id=?", (user_id,))
        _fault("db")

        src = sqlite3.connect(staged_db)
        src.row_factory = sqlite3.Row
        try:
            for row in src.execute("SELECT * FROM classes"):
                new_class = _insert(conn, "classes", ("user_id",) + CLASS_COLUMNS,
                                    (user_id,) + tuple(row[col] for col in CLASS_COLUMNS))
                student_map = {}
                for stu in src.execute("SELECT * FROM students WHERE class_id=? ORDER BY id", (row["id"],)):
                    # active 是 1.3.1 起新增的「停用状态」字段；旧版备份的 students 表没有这一列，
                    # 按默认 1（在读）处理，不报错也不丢数据。
                    values = tuple(
                        1 if col == "active" and "active" not in stu.keys() else stu[col]
                        for col in STUDENT_COLUMNS
                    )
                    student_map[stu["id"]] = _insert(
                        conn, "students", ("class_id",) + STUDENT_COLUMNS,
                        (new_class,) + values,
                    )
                for mis in src.execute("SELECT * FROM mistakes WHERE class_id=? ORDER BY id", (row["id"],)):
                    values = []
                    for col in MISTAKE_COLUMNS:
                        value = mis[col]
                        if col == "image_file" and value:
                            value = mapping.get(os.path.basename(value)) or value
                        values.append(value)
                    new_student = student_map.get(mis["student_id"])
                    if new_student is None:
                        raise BackupError("备份里的错题找不到所属学生")
                    _insert(conn, "mistakes", ("class_id", "student_id") + MISTAKE_COLUMNS,
                            (new_class, new_student) + tuple(values))
                for adv in src.execute("SELECT * FROM advice WHERE class_id=?", (row["id"],)):
                    new_student = adv["student_id"]
                    if new_student is not None:
                        new_student = student_map.get(adv["student_id"])
                        if new_student is None:
                            raise BackupError("备份里的建议找不到所属学生")
                    _insert(conn, "advice", ("class_id", "student_id") + ADVICE_COLUMNS,
                            (new_class, new_student) + tuple(adv[col] for col in ADVICE_COLUMNS))
        finally:
            src.close()

        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception as exc:
        rollback_failed = False
        try:
            _fault("rollback")
            conn.rollback()
        except Exception:
            rollback_failed = True
        conn.close()
        if rollback_failed:
            # 回滚没成功时数据库状态不确定：保留已复制照片、暂存目录和自动备份，
            # 结果文件如实记录，不得写「原数据未改动」。
            _write_result(data_dir, {
                "ok": False,
                "phase": "unknown",
                "rollback_failed": True,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "error": "恢复中断：数据库回滚没有成功，当前数据状态不确定。恢复前的数据已自动备份到 data/backups/%s，"
                         "暂存文件也已保留，请保留整个软件文件夹并联系技术人员处理。" % os.path.basename(safety),
                "safety": os.path.basename(safety),
            })
            error = BackupError(
                "恢复中断：数据库回滚没有成功，当前数据状态不确定。恢复前的数据已自动备份到 data/backups/%s，"
                "暂存文件也已保留，请保留整个软件文件夹并联系技术人员处理。" % os.path.basename(safety)
            )
            error.rollback_failed = True
            raise error
        for path in copied:
            try:
                os.remove(path)
            except OSError:
                pass
        write_failure(data_dir, "恢复失败，原数据未改动：%s" % exc, phase="rolled_back")
        raise BackupError("恢复失败，原数据未改动：%s" % exc)
    conn.close()

    kept = set(name for name in mapping.values())
    warnings = []
    for name in old_photos:
        if name in kept:
            continue
        try:
            os.remove(os.path.join(uploads_dir, os.path.basename(name)))
        except OSError as exc:
            warnings.append("旧照片未能删除：%s（%s）" % (name, exc))

    try:
        conn = sqlite3.connect(db_path)
        try:
            counts = _counts(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        counts = {}

    shutil.rmtree(staging_dir, ignore_errors=True)
    _remove_pending(data_dir)
    final = {
        "ok": True,
        "phase": "restored",
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "counts": counts,
        "safety": os.path.basename(safety),
    }
    if warnings:
        final["warnings"] = warnings
    try:
        _fault("result")
        _write_result(data_dir, final)
    except Exception as exc:
        # 数据已经提交成功，只是结果记录写不进去：兜底写一份诚实的结果，
        # 绝不写「原数据未改动」。
        fallback = {
            "ok": True,
            "phase": "finalize_failed",
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "counts": counts,
            "safety": os.path.basename(safety),
            "error": "数据已经恢复到数据库，但恢复结果记录写入失败：%s。"
                     "恢复前的数据仍自动备份在 data/backups/%s，请核对班级和错题是否与预期一致。" % (exc, os.path.basename(safety)),
        }
        try:
            _write_result(data_dir, fallback)
        except OSError:
            pass
        error = BackupError(fallback["error"])
        error.finalize_failed = True
        raise error
    return {"safety": safety, "counts": counts}
