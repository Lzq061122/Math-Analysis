"""按老师账号备份、恢复与跨电脑导入。

备份结构（format 4，scope=teacher）：
    manifest.json   清单（账号、backup_key、条数、每个文件的 sha256）
    app.db          只含该账号数据的数据库快照（不含密码、会话）
    uploads/...     该账号错题照片

format 3 是 1.5.0 之前的备份，没有作答记录与学生学校编号；读取时会照常恢复
这四张表的内容。

format 2 是 1.2.1 及更早版本的备份（清单里没有 backup_key）。它无法可靠确认
归属：只有经过明确确认的「恢复旧备份」或「从其他电脑导入」流程才会被接受，
绝不静默当作当前账号备份。旧版本软件不认识 format 4，会明确提示格式不受支持。

账号归属用 users.backup_key 判断（随机 32 位十六进制，注册时生成），不依赖
数字 id 和用户名：换电脑重新注册后 id 会变，用户名也可能被其他老师使用。

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

FORMAT = 6
LEGACY_FORMAT = 2
# 1.8.0 的备份多了追问记录与候选更新表；更早版本的备份没有它们，照常可恢复。
READABLE_FORMATS = (FORMAT, 5, 4, 3, LEGACY_FORMAT)
SCOPE_TEACHER = "teacher"
APP_TAG = "math-mistake-studio"
MAX_RESTORE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 20000
REQUIRED_TABLES = ("classes", "students", "mistakes", "advice")
# R2（1.9.0）的十张表随 app.db 整份备份/恢复；这里只用于清单里报条数，便于恢复前后核对。
# 不写死在 backup.py 里：口径以 r2_contract.R2_TABLE_NAMES 为唯一来源，避免两边漂移。
from .r2_contract import R2_TABLE_NAMES as R2_BACKUP_TABLES  # noqa: E402  （常量别名，无循环依赖）
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
    counts = {
        "classes": conn.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
        "students": conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
        "mistakes": conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0],
    }
    # 1.5.0 之前的备份没有 answers 表；有表时才统计，旧备份照常可恢复。
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "answers" in tables:
        counts["answers"] = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    if "answer_reviews" in tables:
        counts["answer_reviews"] = conn.execute("SELECT COUNT(*) FROM answer_reviews").fetchone()[0]
    if "tutor_advice" in tables:
        counts["tutor_advice"] = conn.execute("SELECT COUNT(*) FROM tutor_advice").fetchone()[0]
    if "followups" in tables:
        counts["followups"] = conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0]
    if "followup_updates" in tables:
        counts["followup_updates"] = conn.execute(
            "SELECT COUNT(*) FROM followup_updates"
        ).fetchone()[0]
    # 作答核对与追问原话是本轮新增的表；旧备份里没有这两张表，照样可以恢复。
    if "answer_checks" in tables:
        counts["answer_checks"] = conn.execute(
            "SELECT COUNT(*) FROM answer_checks"
        ).fetchone()[0]
    if "answer_followups" in tables:
        counts["answer_followups"] = conn.execute(
            "SELECT COUNT(*) FROM answer_followups"
        ).fetchone()[0]
    # t14 的外发审计（可选表：旧备份/旧库没有它，有表才统计）
    if "ai_egress_audit" in tables:
        counts["ai_egress_audit"] = conn.execute(
            "SELECT COUNT(*) FROM ai_egress_audit"
        ).fetchone()[0]
    # R2（1.9.0）新增的十张表：批次配置、作答图片、评分与步骤扣分、教学资源三表、
    # 教学任务、提示使用记录、报告导出历史。同样是「有表才统计」——1.8.1 与 R1 的备份
    # 里没有这些表，不能因为算不出来就判备份损坏。
    # 注意：这些表不进 REQUIRED_TABLES（那是「打开库时结构是否兼容」的判据，加进去会把
    # 1.8.1 老师的库挡在门外，迁移根本没机会跑）；恢复时 app.db 整份覆盖，表跟着走。
    for table in R2_BACKUP_TABLES:
        if table in tables:
            counts[table] = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    return counts


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
                # R2：报告导出历史是按账号存的，不跟班级走，所以删别人的班级删不掉它。
                # 不清这一行，另一位老师的 title/purpose/path/sha256 会被塞进这份备份包，
                # 而 _counts() 报的也会是全表条数。恢复侧没有这个问题（按班级遍历 + 重映射）。
                tables_in_target = {r[0] for r in target.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "r2_report_exports" in tables_in_target:
                    target.execute("DELETE FROM r2_report_exports WHERE user_id<>?", (user_id,))
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
        if manifest.get("format") not in READABLE_FORMATS:
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
STUDENT_COLUMNS = ("name", "code", "note", "active", "created_at", "updated_at")
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
ANSWER_COLUMNS = (
    "record_key", "batch", "student_name", "student_code", "question_no", "question_text",
    "unit", "standard_solution", "student_steps", "source", "occurred_on", "missing",
    "missing_reason", "fingerprint", "imported_at", "created_at", "updated_at",
)
REVIEW_COLUMNS = (
    "status", "provider", "model", "causes", "note", "uncertain", "raw",
    "answer_fingerprint", "created_at", "updated_at", "decided_at",
)
TUTOR_COLUMNS = (
    "content", "ai_content", "evidence", "fingerprint", "teacher_edited",
    "mode", "generated_at", "updated_at",
)
FOLLOWUP_COLUMNS = (
    "problem", "question", "source", "suggest", "evidence", "status",
    "asked_at", "answer", "answered_at", "created_at", "updated_at",
)
FOLLOWUP_UPDATE_COLUMNS = (
    "causes", "note", "uncertain", "raw", "before_json", "after_json",
    "status", "created_at", "decided_at",
)

# R1-T15（B1）：作答核对与追问原话。外键（class_id / student_id / answer_id / check_id）
# **不在**这个清单里——它们在恢复时按 student_map / answer_map / check_map 换成新 id，
# 绝不沿用暂存库里的旧 id。其余列原样搬：包括撤回行的 `answer_version=''` 与
# `invalidated_at/_reason/_by`（撤回痕迹与「这条追问已失效」都必须跟着搬，不能只恢复有效行）。
ANSWER_CHECK_COLUMNS = (
    "answer_version", "progress", "cause_state", "cause_source", "observed_step",
    "first_bad_index", "locate_status", "locate_source", "contrast_note", "completion_state",
    "failed_line_index", "verification_scope", "manual_state", "manual_reason", "locate_detail",
    "cause_code", "cause_label", "cause_history", "teacher_note", "basis_note", "change_reason",
    "created_by", "created_at", "updated_at", "decided_at", "decided_by",
    "invalidated_at", "invalidated_reason", "invalidated_by",
    "verify_source", "teacher_error_text", "teacher_verified_at", "teacher_verified_by",
)

ANSWER_FOLLOWUP_COLUMNS = (
    "answer_version", "question", "source", "source_detail", "answer_text", "asked_at",
    "answered_at", "evidence_snapshot", "invalidated_at", "invalidated_reason",
    "created_at", "updated_at",
)

# t14 的「真的外发过一次」审计：按账号（不是按班级）落库，所以不放进 per-class 循环，
# 在班级全部搬完之后按 user_id 整体重写；字段只存形状，不含任何取值。
AI_EGRESS_AUDIT_COLUMNS = ("username", "flow", "model_channel", "payload_fields",
                           "payload_bytes", "created_at")

# 旧格式备份缺列时的默认值：文本类补空串，JSON 类补各自的空壳，整数类补 NULL。
# 1.8.1 的备份没有 verify_source/teacher_error_text/teacher_verified_*（R1-T5 增列）
# 与 evidence_snapshot（R1-T6 增列），按这里的默认值补，恢复照常成功。
_COLUMN_DEFAULTS = {
    "first_bad_index": None, "failed_line_index": None, "created_by": None,
    "decided_by": None, "invalidated_by": None, "teacher_verified_by": None,
    "payload_bytes": 0, "payload_fields": "[]",
    "locate_detail": "{}", "cause_history": "[]", "evidence_snapshot": "{}",
}


def _row_values(row, columns):
    """按列清单取值：暂存库里没有这一列（旧格式备份）就用默认值，不报错也不塞 NULL。"""
    keys = row.keys() if hasattr(row, "keys") else ()
    out = []
    for col in columns:
        if col in keys:
            out.append(row[col])
        else:
            out.append(_COLUMN_DEFAULTS.get(col, ""))
    return tuple(out)


def _table_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


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

    R1-T15（B1）：恢复范围包含 answer_checks（教师判断 / 人工核验 / 撤回）与
    answer_followups（追问原话 + 证据快照），外键按 student_map / answer_map / check_map
    重映射；撤回行（answer_version=''）与 invalidated_* 原样保留。旧格式备份没有这两张表时
    整段跳过、恢复照常成功。ai_egress_audit（t14）按账号整体重写。

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
        src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        try:
            for row in src.execute("SELECT * FROM classes"):
                new_class = _insert(conn, "classes", ("user_id",) + CLASS_COLUMNS,
                                    (user_id,) + tuple(row[col] for col in CLASS_COLUMNS))
                student_map = {}
                for stu in src.execute("SELECT * FROM students WHERE class_id=? ORDER BY id", (row["id"],)):
                    # active 是 1.3.1 起新增的「停用状态」字段，code 是 1.5.0 起新增的学校编号；
                    # 旧版备份的 students 表没有这些列，按默认值处理，不报错也不丢数据。
                    keys = stu.keys()
                    values = tuple(
                        stu[col] if col in keys else (1 if col == "active" else "")
                        for col in STUDENT_COLUMNS
                    )
                    student_map[stu["id"]] = _insert(
                        conn, "students", ("class_id",) + STUDENT_COLUMNS,
                        (new_class,) + values,
                    )
                mistake_map = {}
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
                    mistake_map[mis["id"]] = _insert(
                        conn, "mistakes", ("class_id", "student_id") + MISTAKE_COLUMNS,
                        (new_class, new_student) + tuple(values),
                    )
                for adv in src.execute("SELECT * FROM advice WHERE class_id=?", (row["id"],)):
                    new_student = adv["student_id"]
                    if new_student is not None:
                        new_student = student_map.get(adv["student_id"])
                        if new_student is None:
                            raise BackupError("备份里的建议找不到所属学生")
                    _insert(conn, "advice", ("class_id", "student_id") + ADVICE_COLUMNS,
                            (new_class, new_student) + tuple(adv[col] for col in ADVICE_COLUMNS))
                answer_map = {}
                if "answers" in src_tables:
                    for ans in src.execute("SELECT * FROM answers WHERE class_id=? ORDER BY id", (row["id"],)):
                        new_student = student_map.get(ans["student_id"])
                        if new_student is None:
                            raise BackupError("备份里的作答找不到所属学生")
                        answer_map[ans["id"]] = _insert(
                            conn, "answers", ("class_id", "student_id") + ANSWER_COLUMNS,
                            (new_class, new_student) + tuple(ans[col] for col in ANSWER_COLUMNS),
                        )
                # R1-T15：作答核对与追问原话（B1）。必须排在 answers 之后（answer_map 已就绪），
                # 且都要过 student_map/answer_map 重映射；旧格式备份没有这两张表就整段跳过。
                check_map = {}
                if "answer_checks" in src_tables:
                    _fault("check_restore")
                    for chk in src.execute(
                        "SELECT * FROM answer_checks WHERE class_id=? ORDER BY id", (row["id"],)
                    ):
                        new_student = student_map.get(chk["student_id"])
                        new_answer = answer_map.get(chk["answer_id"])
                        if new_student is None or new_answer is None:
                            # 找不到所属学生/作答的核对记录不能硬塞：宁可整体回滚，也不留半条。
                            raise BackupError("备份里的核对记录找不到所属学生或作答")
                        values = list(_row_values(chk, ANSWER_CHECK_COLUMNS))
                        # 操作人列都是「本账号的 user id」：导入到别的账号时换成新账号，避免指向不存在的人。
                        for pos, col in enumerate(ANSWER_CHECK_COLUMNS):
                            if col in ("created_by", "decided_by", "invalidated_by",
                                       "teacher_verified_by"):
                                values[pos] = user_id if values[pos] is not None else None
                        check_map[chk["id"]] = _insert(
                            conn, "answer_checks",
                            ("class_id", "student_id", "answer_id") + ANSWER_CHECK_COLUMNS,
                            (new_class, new_student, new_answer) + tuple(values),
                        )
                if "answer_followups" in src_tables:
                    for fol2 in src.execute(
                        "SELECT * FROM answer_followups WHERE class_id=? ORDER BY id", (row["id"],)
                    ):
                        new_student = student_map.get(fol2["student_id"])
                        new_answer = answer_map.get(fol2["answer_id"])
                        if new_student is None or new_answer is None:
                            raise BackupError("备份里的追问原话找不到所属学生或作答")
                        new_check = None
                        if fol2["check_id"] is not None:
                            new_check = check_map.get(fol2["check_id"])
                        _insert(
                            conn, "answer_followups",
                            ("class_id", "student_id", "answer_id", "check_id")
                            + ANSWER_FOLLOWUP_COLUMNS,
                            (new_class, new_student, new_answer, new_check)
                            + _row_values(fol2, ANSWER_FOLLOWUP_COLUMNS),
                        )
                if "r2_report_exports" in src_tables:
                    # R2 报告导出历史：class_id 换成本库新 id，student_id 过 student_map
                    # （班级报告的 student_id 固定存 0，用 .get(..., 0) 兜住，不要求该生
                    # 在本次备份的学生名单里）。path 记的是「导出当时本机的保存路径」，
                    # 换机器后它自然不存在——那是事实，不跟着改，也不因路径失效就丢这条历史。
                    # 目标库没有这张表（更老的库尚未迁移）时整段跳过，不让恢复失败。
                    dst_tables = {r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    if "r2_report_exports" in dst_tables:
                        for exp in src.execute(
                            "SELECT * FROM r2_report_exports WHERE class_id=? ORDER BY id",
                            (row["id"],)
                        ):
                            _insert(
                                conn, "r2_report_exports",
                                ("user_id", "class_id", "kind", "student_id", "title", "purpose",
                                 "data_version", "scope", "batch", "path", "sha256", "bytes",
                                 "generated_at", "created_at"),
                                (user_id, new_class, exp["kind"],
                                 student_map.get(exp["student_id"], 0),
                                 exp["title"], exp["purpose"], exp["data_version"],
                                 exp["scope"], exp["batch"], exp["path"], exp["sha256"],
                                 exp["bytes"], exp["generated_at"], exp["created_at"]),
                            )
                review_map = {}
                if "answer_reviews" in src_tables:
                    # 审核记录跟着作答和错题一起搬：两边的 id 都换成本库的新 id。
                    for rev in src.execute(
                        "SELECT * FROM answer_reviews WHERE class_id=? ORDER BY id", (row["id"],)
                    ):
                        new_student = student_map.get(rev["student_id"])
                        new_answer = answer_map.get(rev["answer_id"])
                        if new_student is None or new_answer is None:
                            raise BackupError("备份里的审核记录找不到所属作答")
                        new_mistake = None
                        if rev["mistake_id"] is not None:
                            new_mistake = mistake_map.get(rev["mistake_id"])
                        review_map[rev["id"]] = _insert(
                            conn, "answer_reviews",
                            ("class_id", "student_id", "answer_id", "mistake_id") + REVIEW_COLUMNS,
                            (new_class, new_student, new_answer, new_mistake)
                            + tuple(rev[col] for col in REVIEW_COLUMNS),
                        )
                if "tutor_advice" in src_tables:
                    # 个人辅导建议跟着学生一起搬，student_id 换成本库的新 id。
                    for tut in src.execute(
                        "SELECT * FROM tutor_advice WHERE class_id=? ORDER BY id", (row["id"],)
                    ):
                        new_student = student_map.get(tut["student_id"])
                        if new_student is None:
                            raise BackupError("备份里的辅导建议找不到所属学生")
                        _insert(
                            conn, "tutor_advice",
                            ("class_id", "student_id") + TUTOR_COLUMNS,
                            (new_class, new_student) + tuple(tut[col] for col in TUTOR_COLUMNS),
                        )
                if "followups" in src_tables:
                    # 追问记录跟着学生、错题与来源作答一起搬；候选更新再按新的追问编号挂回去。
                    followup_map = {}
                    for fol in src.execute(
                        "SELECT * FROM followups WHERE class_id=? ORDER BY id", (row["id"],)
                    ):
                        new_student = student_map.get(fol["student_id"])
                        if new_student is None:
                            raise BackupError("备份里的追问记录找不到所属学生")
                        new_mistake = None
                        if fol["mistake_id"] is not None:
                            new_mistake = mistake_map.get(fol["mistake_id"])
                        new_review = None
                        if fol["review_id"] is not None:
                            new_review = review_map.get(fol["review_id"])
                        followup_map[fol["id"]] = _insert(
                            conn, "followups",
                            ("class_id", "student_id", "mistake_id", "review_id") + FOLLOWUP_COLUMNS,
                            (new_class, new_student, new_mistake, new_review)
                            + tuple(fol[col] for col in FOLLOWUP_COLUMNS),
                        )
                    if "followup_updates" in src_tables:
                        for upd in src.execute(
                            "SELECT * FROM followup_updates ORDER BY id"
                        ):
                            new_parent = followup_map.get(upd["followup_id"])
                            if new_parent is None:
                                continue
                            _insert(
                                conn, "followup_updates", ("followup_id",) + FOLLOWUP_UPDATE_COLUMNS,
                                (new_parent,) + tuple(upd[col] for col in FOLLOWUP_UPDATE_COLUMNS),
                            )
            # R1-T15：外发审计按账号落库（没有 class_id），所以放在 per-class 循环之后整体重写。
            # 语义与班级一致：恢复 = 用备份里的状态替换这个账号的这份数据，重复恢复不叠加。
            # 注意：这段必须在 `finally: src.close()` **之前**执行 —— 它要读备份库。
            if "ai_egress_audit" in src_tables and "ai_egress_audit" in _table_names(conn):
                # 故障注入点：证明审计步骤也在同一个事务里（失败时 DELETE 与插入一起回滚）。
                _fault("audit_restore")
                conn.execute("DELETE FROM ai_egress_audit WHERE user_id=?", (user_id,))
                for audit in src.execute("SELECT * FROM ai_egress_audit ORDER BY id"):
                    _insert(conn, "ai_egress_audit",
                            ("user_id",) + AI_EGRESS_AUDIT_COLUMNS,
                            (user_id,) + _row_values(audit, AI_EGRESS_AUDIT_COLUMNS))
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
