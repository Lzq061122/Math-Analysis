"""作答 CSV 导入的源码级测试（1.5.0）。

覆盖：CSV 解析与逐行校验、学生匹配（学校编号/同名）、重复与冲突、覆盖、
账号与班级隔离、事务回滚、旧库迁移、备份与恢复、导入过程零网络请求。

全部使用虚构数据与临时目录，不触碰真实数据；运行：.venv\\Scripts\\python.exe tests\\verify_answers_150.py
"""
import csv
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from app import answers as A
from app import backup as B
from app import db as dbmod
from app.db import Database
from desktop.service import DesktopService

WORK = os.path.join(ROOT, "tests", ".tmp-answers-150")
LOG_PATH = os.path.join(ROOT, "docs", "verify-answers-150.txt")
LEGACY_DB = os.path.join(ROOT, "tests", "fixtures", "legacy146", "db.py")

TOTAL = [0]
FAILED = [0]
LINES = []


def log(text=""):
    LINES.append(text)
    print(text)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if ok:
        log("PASS  %s" % name)
    else:
        FAILED[0] += 1
        log("FAIL  %s%s" % (name, ("  -> " + str(detail)) if detail else ""))


def section(title):
    log("")
    log("== %s ==" % title)


HEAD = [label for label, _ in A.HEADERS]


def row(**kw):
    base = {
        "记录编号": "A1", "学生姓名": "张三", "学校编号": "", "题号": "1",
        "题目内容": "3(x-2)=2x+5", "单元": "一元一次方程", "标准解法": "3x-6=2x+5；x=11",
        "学生步骤": "3x-6=2x+5", "来源批次": "9月作业", "作答日期": "2026-09-02",
        "缺测": "否", "缺测原因": "",
    }
    base.update(kw)
    return base


def csv_text(rows, header=HEAD):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header)
    writer.writeheader()
    for item in rows:
        writer.writerow({key: item.get(key, "") for key in header})
    return out.getvalue()


def make_data_dir(name):
    path = os.path.join(WORK, name)
    os.makedirs(path)
    Database(os.path.join(path, "app.db"))
    return path


def make_service(name):
    data_dir = make_data_dir(name)
    service = DesktopService(data_dir)
    service.register("teacher", "pw123456")
    service.login("teacher", "pw123456")
    cls = service.create_class("初三 1 班")
    return data_dir, service, cls


def counts_rows(data):
    return data["counts"]


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    from logmeta import environment_lines

    for line in environment_lines():
        log(line)
    log("")
    data_dir, svc, cls = make_service("main")
    cid = cls["id"]
    zhangsan = svc.create_student(cid, "张三", "XY001")
    svc.create_student(cid, "李四")

    section("一、CSV 解析与逐行校验")
    rows, err = A.parse(A.template_text())
    check("模板自身可以解析", not err and len(rows) == 2, (len(rows), err))

    header = HEAD[:5]
    _, header_err = A.parse(csv_text([row()], header=header))
    check("缺少必需列时给出明确说明", header_err and "缺少必需的列" in header_err, header_err)

    rows, _ = A.parse(csv_text([row(作答日期="2026-02-30")]))
    check("非法日期被标错", rows[0]["errors"] and "日期" in rows[0]["errors"][0], rows[0]["errors"])

    rows, _ = A.parse(csv_text([row(题目内容="   ")]))
    check("空题目被标错", rows[0]["errors"] and "题目" in rows[0]["errors"][0], rows[0]["errors"])

    rows, _ = A.parse(csv_text([row(学生步骤="")]))
    check("缺步骤按缺测处理", rows[0]["missing"] == 1 and "未提供" in rows[0]["missing_reason"], rows[0])

    rows, _ = A.parse(csv_text([row(缺测="是", 缺测原因="请假", 学生步骤="x=11")]))
    check("标了缺测却给了步骤时标错", rows[0]["errors"], rows[0]["errors"])

    rows, _ = A.parse(csv_text([row(缺测="是", 缺测原因="")]))
    check("缺测未写原因时标错", rows[0]["errors"], rows[0]["errors"])

    gb = csv_text([row(学生姓名="王五")]).encode("gb18030")
    text = A.decode(gb)
    check("GB18030 中文可以解码", "王五" in text, text[:30])

    bom = b"\xef\xbb\xbf" + csv_text([row()]).encode("utf-8")
    decoded = A.decode(bom)
    check("UTF-8 BOM 不会污染第一列表头", decoded.startswith("记录编号"), repr(decoded[:12]))

    dup = csv_text([row(记录编号="D1"), row(记录编号="D1", 题号="2")])
    rows, _ = A.parse(dup)
    check("文件内记录编号重复被标错", rows[1]["errors"] and "重复" in rows[1]["errors"][0], rows[1]["errors"])

    rows, _ = A.parse(csv_text([row(记录编号="X" * 61)]))
    check("过长的记录编号被标错", rows[0]["errors"], rows[0]["errors"])

    section("二、学生匹配与预览")
    svc.create_student(cid, "王五", "WW9")
    data = svc.preview_answers(cid, csv_text([row(学生姓名="王五", 学校编号="WW9", 记录编号="B1")]), "9月作业")
    check("学校编号匹配学生", data["counts"]["ok"] == 1 and data["rows"][0]["student_id"], data["rows"][0])

    data = svc.preview_answers(cid, csv_text([row(学生姓名="张三", 学校编号="WW9", 记录编号="B2")]), "9月作业")
    check("编号与姓名指向不同学生时标错", data["counts"]["error"] == 1 and "不一致" in data["rows"][0]["reason"],
          data["rows"][0]["reason"])

    svc.create_student(cid, "同名", "N1")
    svc.create_student(cid, "同名", "N2")
    data = svc.preview_answers(cid, csv_text([row(学生姓名="同名", 记录编号="B3")]), "9月作业")
    check("同班同名不自动猜测", data["counts"]["error"] == 1 and len(data["rows"][0]["candidates"]) == 2,
          data["rows"][0])

    cand = data["rows"][0]["candidates"][0]
    sel = {str(data["rows"][0]["line"]): cand["id"]}
    data2 = svc.preview_answers(cid, csv_text([row(学生姓名="同名", 记录编号="B3")]), "9月作业", selections=sel)
    check("老师选定同名学生后变可导入", data2["counts"]["ok"] == 1 and data2["rows"][0]["student_id"] == cand["id"],
          data2["counts"])

    data = svc.preview_answers(cid, csv_text([row(学生姓名="查无此人", 记录编号="B4")]), "9月作业")
    check("名单外的学生被拦下", data["counts"]["error"] == 1 and "没有叫" in data["rows"][0]["reason"],
          data["rows"][0]["reason"])

    before_students = len(svc.list_students(cid))
    svc.import_answers(cid, csv_text([row(学生姓名="查无此人", 记录编号="B4")]), "9月作业")
    check("未匹配的行不会自动建学生", len(svc.list_students(cid)) == before_students,
          (before_students, len(svc.list_students(cid))))

    section("三、导入、重复与冲突")
    text = csv_text([row(记录编号="C1", 学生姓名="张三", 学校编号="XY001"),
                     row(记录编号="C2", 学生姓名="李四", 学生步骤="", 缺测="是", 缺测原因="没交")])
    imported = svc.import_answers(cid, text, "9月作业")
    check("首次导入写入 2 条", imported["counts"]["new"] == 2, imported["counts"])
    check("缺测记录按缺测保存", [r["missing"] for r in imported["rows"] if r["record_key"] == "C2"] == [1],
          imported["rows"])

    total_before = svc.answer_batches(cid)["total"]
    again = svc.import_answers(cid, text, "9月作业")
    check("重复导入不重复计数", again["counts"]["duplicate"] == 2 and again["counts"]["new"] == 0, again["counts"])
    check("重复导入后总条数不变", svc.answer_batches(cid)["total"] == total_before,
          (total_before, svc.answer_batches(cid)["total"]))

    changed = csv_text([row(记录编号="C1", 学生姓名="张三", 学校编号="XY001", 学生步骤="3x-2x=5+6")])
    conflict = svc.preview_answers(cid, changed, "9月作业")
    check("同编号改了内容标冲突", conflict["counts"]["conflict"] == 1, conflict["counts"])

    blocked = svc.import_answers(cid, changed, "9月作业")
    check("未确认覆盖时冲突不写入", blocked["counts"]["overwritten"] == 0 and blocked["counts"]["conflict"] == 1,
          blocked["counts"])
    stored = svc.answer_batches(cid, "9月作业")["items"]
    check("冲突未覆盖时原内容保留", [r["student_steps"] for r in stored if r["record_key"] == "C1"] == ["3x-6=2x+5"],
          [r["student_steps"] for r in stored])

    replaced = svc.import_answers(cid, changed, "9月作业", overwrite=True)
    check("勾选覆盖后替换内容", replaced["counts"]["overwritten"] == 1, replaced["counts"])
    stored = svc.answer_batches(cid, "9月作业")["items"]
    check("覆盖后数据库是新内容", [r["student_steps"] for r in stored if r["record_key"] == "C1"] == ["3x-2x=5+6"],
          [r["student_steps"] for r in stored])
    untouched = [r for r in stored if r["record_key"] == "C2"]
    check("覆盖只影响冲突行", len(untouched) == 1 and untouched[0]["student_steps"] == "", untouched)
    check("覆盖后总条数不变", len(stored) == 2, len(stored))

    stable = csv_text([row(记录编号="K1", 学生姓名="李四", 学生步骤="4x=8", 来源批次="批次甲")])
    svc.import_answers(cid, stable, "批次甲")
    same_other_batch = svc.import_answers(cid, stable, "批次乙")
    check("换批次同内容仍按重复跳过", same_other_batch["counts"]["duplicate"] == 1, same_other_batch["counts"])

    section("四、隔离")
    svc.register("teacher2", "pw123456")
    svc.login("teacher2", "pw123456")
    denied = None
    try:
        svc.preview_answers(cid, text, "9月作业")
    except Exception as exc:  # noqa: BLE001 - 这里就是要看对外错误
        denied = str(exc)
    check("其他账号不能预览本班作答", denied and ("不存在" in denied or "404" in denied or "找不到" in denied), denied)

    svc.login("teacher", "pw123456")
    other = svc.create_class("初三 2 班")
    outsider = svc.create_student(other["id"], "外班学生", "OUT1")
    sel = {"2": outsider["id"]}
    try:
        data = svc.preview_answers(cid, csv_text([row(记录编号="I1"), row(记录编号="I2", 学生姓名="张三")]),
                                   "9月作业", selections=sel)
        check("选择其他班级学生无效", data["rows"][1]["student_id"] != outsider["id"], data["rows"][1])
    except Exception as exc:  # noqa: BLE001
        check("选择其他班级学生被拒绝", True, exc)

    section("五、事务与副作用")
    before = svc.answer_batches(cid)["total"]
    original = A.fingerprint
    calls = {"n": 0}

    def broken(row_, student_id):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("模拟写入中途失败")
        return original(row_, student_id)

    A.fingerprint = broken
    try:
        svc.import_answers(cid, csv_text([row(记录编号="T1"), row(记录编号="T2", 学生姓名="李四")]), "事务")
        check("中途失败会抛错", False, "没有抛错")
    except Exception:  # noqa: BLE001
        check("中途失败会抛错", True)
    finally:
        A.fingerprint = original
    check("中途失败不留下半批次", svc.answer_batches(cid)["total"] == before,
          (before, svc.answer_batches(cid)["total"]))

    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    mistakes_count = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    conn.close()
    check("导入不会写错题表", mistakes_count == 0, mistakes_count)

    section("六、零网络")
    import socket
    real_socket = socket.socket

    class NoNet(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("导入过程不允许联网")

    socket.socket = NoNet
    try:
        data = svc.preview_answers(cid, csv_text([row(记录编号="N1")]), "离线")
        ok = data["counts"]["ok"] == 1
    except AssertionError as exc:
        ok = False
    finally:
        socket.socket = real_socket
    check("预览与导入全程不联网", ok)

    section("七、旧库迁移")
    legacy_dir = os.path.join(WORK, "legacy")
    os.makedirs(legacy_dir)
    legacy_db = os.path.join(legacy_dir, "app.db")
    build_legacy_db(legacy_db)
    backup_files_before = {name for name in os.listdir(legacy_dir)}
    report = dbmod.ensure_answers_schema(legacy_db)
    check("迁移返回备份文件路径", isinstance(report, str) and os.path.isfile(report), report)
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in conn.execute("PRAGMA table_info(students)")}
    students = conn.execute("SELECT name, code FROM students ORDER BY id").fetchall()
    mistakes = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    conn.close()
    check("迁移后 answers 表存在", "answers" in tables, tables)
    check("迁移后 students 有 code 列", "code" in columns, columns)
    check("旧学生数据保留", [tuple(r) for r in students] == [("老学生", "")], students)
    check("旧错题数据保留", mistakes == 1, mistakes)
    new_files = {name for name in os.listdir(legacy_dir)} - backup_files_before
    check("迁移前自动备份", any(name.startswith("app.db.bak-") for name in new_files), new_files)
    check("迁移后通过桌面端结构检查", dbmod.answers_schema_ready(legacy_db))

    again_report = dbmod.ensure_answers_schema(legacy_db)
    check("重复迁移安全（幂等）", again_report is None, again_report)

    fault_dir = os.path.join(WORK, "fault")
    os.makedirs(fault_dir)
    fault_db = os.path.join(fault_dir, "app.db")
    build_legacy_db(fault_db)
    fault_error = None
    os.environ["MISTAKE_FAULT"] = "answers_commit"
    try:
        dbmod.ensure_answers_schema(fault_db)
    except Exception as exc:  # noqa: BLE001 - 这里就是要看迁移失败的表现
        fault_error = exc
    finally:
        os.environ.pop("MISTAKE_FAULT", None)
    check("迁移中途失败会报错", fault_error is not None, fault_error)
    conn = sqlite3.connect(fault_db)
    fault_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fault_columns = {r[1] for r in conn.execute("PRAGMA table_info(students)")}
    fault_mistakes = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    conn.close()
    check("迁移失败后没有留下 answers 表", "answers" not in fault_tables, fault_tables)
    check("迁移失败后没有补 code 列", "code" not in fault_columns, fault_columns)
    check("迁移失败后旧数据可读", fault_mistakes == 1, fault_mistakes)
    check("迁移失败也保留备份文件", any(name.startswith("app.db.bak-") for name in os.listdir(fault_dir)),
          os.listdir(fault_dir))

    upgrade_dir = os.path.join(WORK, "upgrade")
    os.makedirs(upgrade_dir)
    build_legacy_db(os.path.join(upgrade_dir, "app.db"))
    upgraded = DesktopService(upgrade_dir)
    check("桌面端打开旧库时自动迁移", bool(upgraded.answers_migration), upgraded.answers_migration)
    check("迁移后旧库通过结构检查", dbmod.answers_schema_ready(os.path.join(upgrade_dir, "app.db")))

    section("八、备份与恢复")
    backup_zip = os.path.join(WORK, "backup.zip")
    user_id = svc.current_user()["id"] if hasattr(svc, "current_user") else None
    if user_id is None:
        conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
        user_id = conn.execute("SELECT id FROM users WHERE username='teacher'").fetchone()[0]
        conn.close()
    B.create_backup(data_dir, backup_zip, user_id, "1.5.0")
    with zipfile.ZipFile(backup_zip) as bundle:
        names = bundle.namelist()
        check("备份包含 manifest", "manifest.json" in names, names)
        bundle.extract("app.db", os.path.join(WORK, "peek"))
    peek = sqlite3.connect(os.path.join(WORK, "peek", "app.db"))
    peek_answers = peek.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    peek_code = peek.execute("SELECT code FROM students WHERE name='张三'").fetchone()[0]
    peek.close()
    check("备份里含作答记录", peek_answers == svc.answer_batches(cid)["total"], peek_answers)
    check("备份里含学校编号", peek_code == "XY001", peek_code)

    staging = os.path.join(WORK, "staging")
    os.makedirs(staging)
    with zipfile.ZipFile(backup_zip) as bundle:
        for name in bundle.namelist():
            if name.endswith("/"):
                continue
            bundle.extract(name, staging)
    total_now = svc.answer_batches(cid)["total"]
    result = B.apply_staged(staging, data_dir, user_id, "1.5.0", mode="restore")
    check("恢复成功并返回条数", result["counts"].get("answers") == total_now, result["counts"])
    stored = svc.answer_batches(cid, "9月作业")["items"]
    check("恢复后作答明细还在", any(r["record_key"] == "C1" for r in stored), [r["record_key"] for r in stored])
    students = svc.list_students(cid)
    check("恢复后学校编号保留", any(s.get("code") == "XY001" for s in students), students)

    old_db = os.path.join(WORK, "old-backup", "app.db")
    os.makedirs(os.path.dirname(old_db))
    build_legacy_db(old_db)
    old_conn = sqlite3.connect(old_db)
    old_counts = B._counts(old_conn)
    old_conn.close()
    check("旧备份没有 answers 时计数不报错", "answers" not in old_counts, old_counts)

    section("九、备份格式与旧版本")
    with zipfile.ZipFile(backup_zip) as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    check("新备份标记为 format 6", manifest.get("format") == 6, manifest.get("format"))
    legacy = load_legacy_backup()
    try:
        legacy.inspect_backup(backup_zip, os.path.join(WORK, "legacy-read"))
        legacy_error = ""
    except Exception as exc:  # noqa: BLE001 - 这里要看旧版本给出的提示
        legacy_error = str(exc)
    check("1.4.6 明确拒绝新备份", "格式不受支持" in legacy_error, legacy_error)

    old_zip = os.path.join(WORK, "format3.zip")
    shutil.copyfile(backup_zip, old_zip)
    rewrite_manifest_format(old_zip, 3)
    try:
        old_manifest, _relation = B.inspect_backup(old_zip, os.path.join(WORK, "format3-read"))
        old_error = ""
    except Exception as exc:  # noqa: BLE001
        old_manifest, old_error = {}, str(exc)
    check("仍能读取 1.4.6 时代的 format 3 备份", old_error == "" and old_manifest.get("format") == 3,
          old_error or old_manifest.get("format"))

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    code = 1 if FAILED[0] else 0
    log("退出码：%d" % code)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code


def build_legacy_db(path):
    """用 1.4.6 的 db.py（tests/fixtures/legacy146）建一个旧结构库，模拟升级前的数据。"""
    if not os.path.isfile(LEGACY_DB):
        raise SystemExit("缺少测试样例 %s；请确认源码包完整（tests/fixtures/legacy146/db.py）" % LEGACY_DB)
    with open(LEGACY_DB, "r", encoding="utf-8-sig") as fh:
        source = fh.read()
    namespace = {"__name__": "legacy_db"}
    exec(compile(source, "legacy_db.py", "exec"), namespace)
    conn = sqlite3.connect(path)
    conn.executescript(namespace["SCHEMA"])
    conn.execute(
        "INSERT INTO users(id, username, display_name, pwd_salt, pwd_hash, iterations, created_at)"
        " VALUES (1,'old','旧老师','s','h',1,'2026-01-01')"
    )
    conn.execute("INSERT INTO classes(id, user_id, name, created_at, updated_at) VALUES (1,1,'旧班','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO students(id, class_id, name, created_at, updated_at) VALUES (1,1,'老学生','2026-01-01','2026-01-01')")
    conn.execute(
        "INSERT INTO mistakes(id, class_id, student_id, status, created_at, updated_at)"
        " VALUES (1,1,1,'confirmed','2026-01-01','2026-01-01')"
    )
    conn.execute("INSERT INTO app_meta(key, value) VALUES ('schema_version','2')")
    conn.commit()
    conn.close()


def load_legacy_backup():
    """把 1.4.6 的 backup.py 当作独立模块载入，用来验证旧版本读到新备份时的表现。"""
    package = types.ModuleType("legacy146")
    package.__path__ = [os.path.dirname(LEGACY_DB)]
    sys.modules["legacy146"] = package
    version = types.ModuleType("legacy146.version")
    version.VERSION = "1.4.6"
    sys.modules["legacy146.version"] = version
    spec = importlib.util.spec_from_file_location(
        "legacy146.backup", os.path.join(os.path.dirname(LEGACY_DB), "backup.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rewrite_manifest_format(zip_path, fmt):
    """把备份清单里的 format 改成指定值，用来模拟 1.4.6 时代导出的备份。"""
    tmp_path = zip_path + ".tmp"
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.json":
                manifest = json.loads(data.decode("utf-8"))
                manifest["format"] = fmt
                data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            dst.writestr(info, data)
    os.replace(tmp_path, zip_path)


if __name__ == "__main__":
    sys.exit(main())
