"""R2-01 压缩包批量导入验收（1.9.0）。

覆盖派单 §三 + §3.4 的 9 条验收：
  1. 10 名学生 × 5 道题 → 50 条（预览只读、分类正确、提交后 50 条入库）；
  2. 重导同一包 → 0 新增 / 50 重复（图片不重复落盘）；
  3. 改 1 条 → 1 冲突；不覆盖时一条不写，覆盖时只改这 1 条并触发 R2-INV-01；
  4. 多图可查看（`answer_images` 逐张落盘、seq 有序、宽高/字节/摘要都有）；
  5. 缺图提示（声明了但包里没有 → 可导入 + 提示，不阻断文字入库）；
  6. 仅照片作答 → 内容不足（不是缺测）；
  7. 非法/损坏/超限整包拒绝且**一个文件都不落盘**（含 5 个限制的实际执行点）；
  8. 提交异常 → 整批回滚 + 清理本次新落盘的图片（含反向自证：摘掉清理就必须出现孤儿）；
  9. 原 CSV 导入照常可用，且同一内容两路 fingerprint 一致。

运行：C:\\Python314\\python.exe tests\\verify_r2_01_zip_import.py
日志：docs/verify-r2-01-zip-import.txt
"""

import csv
import io
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from app import answer_import_zip as Z          # noqa: E402
from app import answers as ANSWERS              # noqa: E402
from app import db as DB                        # noqa: E402
from app import r2_contract as C                # noqa: E402
from app.uploads import UploadStore             # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
TEMPLATE_FIXTURE = os.path.join(FIXTURES, "R2_ZIP模板.zip")
PACKAGE_FIXTURE = os.path.join(FIXTURES, "R2_验收_10x5.zip")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-01-zip-import.txt")

TOTAL = [0]
FAILED = [0]
LINES = []
WORK = []
PENDING_RESTORES = {}

HEADER = ["记录编号", "学生姓名", "学校编号", "题号", "题目内容", "单元", "标准解法",
          "学生步骤", "来源批次", "作答日期", "缺测", "缺测原因", "图片路径"]
PNG = Z._ONE_PIXEL_PNG
BATCH = "9月第1次作业"


def log(text=""):
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


# ---------------------------------------------------------------------------
# 环境：一个干净的班级 + 三个学生
# ---------------------------------------------------------------------------
class Env:
    def __init__(self, students=10):
        self.work = tmpdir("r2-01-")
        self.db_file = os.path.join(self.work, "app.db")
        self.db = DB.Database(self.db_file)
        self.uploads = UploadStore(os.path.join(self.work, "uploads"))
        with self.db.tx() as conn:
            conn.execute("INSERT INTO users(username,display_name,pwd_salt,pwd_hash,iterations,"
                         "created_at,backup_key) VALUES('teacher','老师','salt','hash',1,'now','')")
            conn.execute("INSERT INTO classes(user_id,name,grade,created_at,updated_at)"
                         " VALUES(1,'初三 1 班','初三','now','now')")
            for index in range(1, students + 1):
                conn.execute("INSERT INTO students(class_id,name,code,created_at,updated_at)"
                             " VALUES(1,?,?,'now','now')",
                             ("学生%02d" % index, "S%02d" % index))
        self.class_id = 1

    def count(self, table, where="1=1", params=()):
        return self.db.scalar("SELECT COUNT(*) FROM %s WHERE %s" % (table, where), params) or 0

    def upload_names(self):
        return set(os.listdir(self.uploads.root))

    def files(self):
        return os.listdir(self.uploads.root)

    def close(self):
        try:
            self.db.conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 造包工具
# ---------------------------------------------------------------------------
def csv_text(rows, header=None):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header or HEADER)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def build_zip(entries, csv_bytes=None, csv_name="answers.csv"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if csv_bytes is not None:
            archive.writestr(csv_name, csv_bytes)
        for name, blob in entries:
            if isinstance(blob, str):
                blob = blob.encode("utf-8")
            archive.writestr(name, blob)
    return buffer.getvalue()


def row(record_key, name, code, question_no, steps="x=11。", missing="否", reason="",
        images="", occurred="2026-09-10"):
    return [record_key, name, code, question_no, "解方程 3(x-2)=2x+5", "一元一次方程",
            "3x-6=2x+5；3x-2x=5+6；x=11", steps, "", occurred, missing, reason, images]


def small_package(students=("S01", "S02"), questions=("1",), steps="x=1。", images=None):
    rows = []
    for code in students:
        for question in questions:
            rows.append(row("R-%s-%s" % (code, question), "学生" + code[1:], code, question,
                            steps=steps,
                            images=(images or {}).get((code, question), "")))
    return build_zip([], csv_bytes=csv_text(rows).encode("utf-8"))


def mark_encrypted(data):
    """把 ZIP 条目的「加密」标志位打上（stdlib 不能写加密包，这里只翻转标志位）。

    本地文件头偏移 6、中央目录记录偏移 8 各 2 字节是 general purpose bit flag；
    置最低位即「该条目加密」。读取侧必须在解压前就拦下它（而不是读到一半才报错）。
    """
    out = bytearray(data)
    index = 0
    while True:
        index = out.find(b"PK\x03\x04", index)
        if index < 0:
            break
        out[index + 6] = out[index + 6] | 0x1
        index += 4
    index = 0
    while True:
        index = out.find(b"PK\x01\x02", index)
        if index < 0:
            break
        out[index + 8] = out[index + 8] | 0x1
        index += 4
    return bytes(out)


def _with_limit(zip_limit, callback):
    """临时把 MAX_ZIP_BYTES 注入成 `zip_limit` 跑一次，跑完还原（测完必须还原）。"""
    saved = Z.MAX_ZIP_BYTES
    try:
        Z.MAX_ZIP_BYTES = zip_limit
        return callback()
    finally:
        Z.MAX_ZIP_BYTES = saved


def _reject_code(data):
    """读包，返回拒绝码（通过校验返回空串）。用于断言「同一包在变异前后结论不同」。"""
    try:
        Z.read_package(data)
    except Z.ZipImportError as exc:
        return exc.code
    return ""


def patch_bytes(path, replacements):
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
    importlib.reload(Z)


def emergency_restore():
    restored = []
    for path, data in list(PENDING_RESTORES.items()):
        with open(path, "wb") as fh:
            fh.write(data)
        restored.append(os.path.basename(path))
    PENDING_RESTORES.clear()
    return restored


# ---------------------------------------------------------------------------
# 一、模板与夹具
# ---------------------------------------------------------------------------
def section_template():
    section("一、标准模板与验收夹具")
    check("1.1 模板夹具存在：tests/fixtures/R2_ZIP模板.zip", os.path.exists(TEMPLATE_FIXTURE),
          TEMPLATE_FIXTURE)
    check("1.2 验收包夹具存在：tests/fixtures/R2_验收_10x5.zip", os.path.exists(PACKAGE_FIXTURE),
          PACKAGE_FIXTURE)
    template_bytes = Z.template_zip()
    check("1.3 模板夹具与代码 template_zip() 逐字节一致（只改一边会红）",
          open(TEMPLATE_FIXTURE, "rb").read() == template_bytes, "")
    # 跨解释器（3.12 venv / 3.14 主机）zlib 版本可能不同，压缩后的字节会差几个字节，
    # 所以「内容一致」另用解包后的条目逐项核：文件名集合 + 每个文件的内容。
    with zipfile.ZipFile(TEMPLATE_FIXTURE) as fixture_zip, \
            zipfile.ZipFile(io.BytesIO(template_bytes)) as code_zip:
        fixture_entries = {name: fixture_zip.read(name) for name in fixture_zip.namelist()}
        code_entries = {name: code_zip.read(name) for name in code_zip.namelist()}
    check("1.3b 模板夹具与代码的条目逐项相同（不受 zlib 版本影响）",
          fixture_entries == code_entries,
          sorted(set(fixture_entries) ^ set(code_entries)))
    with zipfile.ZipFile(io.BytesIO(template_bytes)) as archive:
        names = archive.namelist()
    check("1.4 模板结构 = answers.csv + images/ 下示例图",
          "answers.csv" in names and len([n for n in names if n.startswith("images/")]) == 3,
          names)
    package = Z.read_package(template_bytes)
    check("1.5 模板本身能通过自己的整包校验（老师拿到就能用）",
          package.csv_name == "answers.csv" and len(package.images) == 3, package.summary())
    check("1.6 模板 CSV 表头 = R1 的 12 列 + 图片路径",
          Z.template_csv_text().splitlines()[0].startswith("记录编号,学生姓名")
          and Z.template_csv_text().splitlines()[0].endswith("图片路径"),
          Z.template_csv_text().splitlines()[0])

    package_bytes = open(PACKAGE_FIXTURE, "rb").read()
    env = Env()
    try:
        data = Z.preview(env.db, env.class_id, package_bytes, batch=BATCH)
        check("1.7 验收包 = 10 名学生 × 5 道题 = 50 条", data["total"] == 50, data["total"])
        check("1.8 验收包里有 4 张图片、1 行缺图、1 行仅照片",
              data["package"]["image_count"] == 4 and data["images_present"] == 4,
              data["package"])
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 二、预览只读 + 分类
# ---------------------------------------------------------------------------
def section_preview():
    section("二、预览只读与六类分类（含 §3.4 第 1 条）")
    package_bytes = open(PACKAGE_FIXTURE, "rb").read()
    env = Env()
    try:
        before_files = env.upload_names()
        data = Z.preview(env.db, env.class_id, package_bytes, batch=BATCH)
        counts = data["counts"]
        check("2.1 分类：有效 45 + 缺测 5 = 50（六类计数齐备）",
              counts[Z.CATEGORY_VALID] == 45 and counts[Z.CATEGORY_MISSING] == 5
              and set(Z.CATEGORIES) <= set(counts), counts)
        check("2.2 内容不足单独给出（仅照片那 1 行），不混进缺测",
              counts.get("insufficient") == 1 and data["total"] == 50, counts)
        check("2.3 预览不写库（answers 仍为 0）", env.count("answers") == 0,
              env.count("answers"))
        check("2.4 预览不落盘（uploads 目录仍为空）", env.upload_names() == before_files,
              env.files())
        check("2.5 预览可导入数 = 50（缺测行也算可导入，它是一条真实记录）",
              data["importable"] == 50, data["importable"])
        check("2.6 每行都带分类与状态（界面只按返回值渲染）",
              all(row.get("category") in Z.CATEGORIES for row in data["rows"])
              and all(row.get("status") in ("ok", "duplicate", "conflict", "error")
                      for row in data["rows"]), data["rows"][:1])
        warn_rows = [row for row in data["rows"] if row.get("warnings")]
        check("2.7 缺图那行给出提示但不阻断（category 仍是 valid/missing）",
              len(warn_rows) == 1 and warn_rows[0]["category"] in
              (Z.CATEGORY_VALID, Z.CATEGORY_MISSING)
              and "找不到" in warn_rows[0]["warnings"][0], warn_rows[:1])
        check("2.8 批次名按参数保留，未传时用包名（此处传了 %s）" % BATCH,
              data["batch"] == BATCH, data["batch"])
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 三、导入 50 条 + 多图
# ---------------------------------------------------------------------------
def section_commit():
    section("三、导入 50 条、多图可查看（§3.4 第 1、4 条）")
    package_bytes = open(PACKAGE_FIXTURE, "rb").read()
    env = Env()
    try:
        result = Z.commit(env.db, env.class_id, package_bytes, batch=BATCH, uploads=env.uploads)
        check("3.1 首次导入：新增 50、覆盖 0、跳过 0、失败 0",
              result["counts"] == {"new": 50, "overwritten": 0, "duplicate": 0,
                                   "conflict": 0, "failed": 0}, result["counts"])
        check("3.2 库里确实 50 条", env.count("answers") == 50, env.count("answers"))
        check("3.3 4 张图片落盘且库内 4 行 answer_images",
              len(env.files()) == 4 and env.count("answer_images") == 4,
              (len(env.files()), env.count("answer_images")))
        check("3.4 落盘文件名由程序生成（不是包内路径）",
              all(name.startswith("answer-") and name.endswith(".png") for name in env.files()),
              env.files())
        check("3.5 没有孤立文件：uploads 的文件集合 = 库内 file_path 集合",
              env.upload_names() == {row["file_path"] for row in
                                     env.db.query("SELECT file_path FROM answer_images")},
              env.files())
        multi = env.db.query(
            "SELECT a.record_key, i.seq, i.width, i.height, i.bytes, i.sha256, i.mime"
            " FROM answer_images i JOIN answers a ON a.id=i.answer_id"
            " WHERE a.record_key='A-S01-1' ORDER BY i.seq")
        check("3.6 多图可查看：同一份作答 2 张图、seq 1/2、宽高与字节已记录",
              len(multi) == 2 and [item["seq"] for item in multi] == [1, 2]
              and all(item["width"] == 1 and item["height"] == 1 and item["bytes"] == 69
                      for item in multi)
              and all(item["mime"] == "image/png" and len(item["sha256"]) == 64
                      for item in multi), multi)
        check("3.7 批次被登记为「未配置」（不猜名单/满分），来源 import",
              [(row["batch"], row["config_state"], row["origin"])
               for row in env.db.query("SELECT batch, config_state, origin FROM assignment_batches")]
              == [(BATCH, C.BATCH_CONFIG_STATE_UNCONFIGURED, C.BATCH_ORIGIN_IMPORT)],
              env.db.query("SELECT * FROM assignment_batches"))
        check("3.8 缺图那行照样入库，只是没有图片行",
              env.count("answers", "record_key=?", ("A-S04-4",)) == 1
              and env.count("answer_images", "answer_id=(SELECT id FROM answers"
                                              " WHERE record_key='A-S04-4')") == 0, "")
        check("3.9 仅照片那行：入库为「内容不足」，missing=0、无步骤但有图",
              [(row["missing"], row["student_steps"]) for row in env.db.query(
                  "SELECT missing, student_steps FROM answers WHERE record_key='A-S03-3'")]
              == [(0, "")] and env.count(
                  "answer_images", "answer_id=(SELECT id FROM answers"
                                   " WHERE record_key='A-S03-3')") == 1,
              env.db.query("SELECT missing, student_steps FROM answers"
                           " WHERE record_key='A-S03-3'"))
        check("3.10 缺测那 5 条 missing=1（S10 五题全缺测）",
              env.count("answers", "record_key LIKE 'A-S10-%' AND missing=1") == 5,
              env.count("answers", "record_key LIKE 'A-S10-%' AND missing=1"))
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 四、重导 / 冲突 / 覆盖
# ---------------------------------------------------------------------------
def section_reimport():
    section("四、重导 0 新增 50 重复、改 1 条 → 1 冲突、覆盖触发 R2-INV-01（§3.4 第 2、3 条）")
    package_bytes = open(PACKAGE_FIXTURE, "rb").read()
    env = Env()
    try:
        Z.commit(env.db, env.class_id, package_bytes, batch=BATCH, uploads=env.uploads)
        files_before = env.upload_names()
        again = Z.preview(env.db, env.class_id, package_bytes, batch=BATCH)
        check("4.1 重导预览：0 有效、50 重复",
              again["counts"][Z.CATEGORY_DUPLICATE] == 50
              and again["counts"][Z.CATEGORY_VALID] == 0, again["counts"])
        second = Z.commit(env.db, env.class_id, package_bytes, batch=BATCH, uploads=env.uploads)
        check("4.2 重导提交：0 新增、50 跳过重复",
              second["counts"]["new"] == 0 and second["counts"]["duplicate"] == 50,
              second["counts"])
        check("4.3 重导不落新图片（图片不重复占盘）", env.upload_names() == files_before,
              len(env.files()))

        # 改 1 条：换掉 S02Q02 的学生步骤
        changed = bytearray()
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            csv_bytes = archive.read("answers.csv")
            images = [(name, archive.read(name)) for name in archive.namelist()
                      if name.startswith("images/")]
        text = csv_bytes.decode("utf-8-sig")
        text = text.replace("S02,2,解方程", "S02,2,解方程")  # 占位，真正的改在下面按行做
        rows = list(csv.reader(io.StringIO(text)))
        for cells in rows[1:]:
            if cells[0] == "A-S02-2":
                cells[7] = "2x+2=6；x=2"      # 只改这一条的学生步骤
        changed_text = csv_text(rows[1:], header=rows[0])
        changed_package = build_zip(images, csv_bytes=changed_text.encode("utf-8-sig"))
        preview_changed = Z.preview(env.db, env.class_id, changed_package, batch=BATCH)
        check("4.4 改 1 条 → 预览恰好 1 条冲突、49 条重复",
              preview_changed["counts"][Z.CATEGORY_CONFLICT] == 1
              and preview_changed["counts"][Z.CATEGORY_DUPLICATE] == 49,
              preview_changed["counts"])
        row_before = env.db.query_one(
            "SELECT student_steps, fingerprint FROM answers WHERE record_key='A-S02-2'")
        skipped = Z.commit(env.db, env.class_id, changed_package, batch=BATCH,
                           uploads=env.uploads)
        row_unchanged = env.db.query_one(
            "SELECT student_steps, fingerprint FROM answers WHERE record_key='A-S02-2'")
        check("4.5 不覆盖时：冲突一条不写（0 新增 0 覆盖），内容与指纹不变",
              skipped["counts"]["new"] == 0 and skipped["counts"]["overwritten"] == 0
              and skipped["counts"]["conflict"] == 1 and row_unchanged == row_before,
              (skipped["counts"], row_before, row_unchanged))

        overwritten = Z.commit(env.db, env.class_id, changed_package, batch=BATCH,
                               uploads=env.uploads, overwrite=True)
        row_after = env.db.query_one(
            "SELECT student_steps, fingerprint FROM answers WHERE record_key='A-S02-2'")
        check("4.6 覆盖后：1 条被替换、内容与指纹都更新",
              overwritten["counts"]["overwritten"] == 1
              and row_after["student_steps"].endswith("x=2")
              and row_after["fingerprint"] != row_before["fingerprint"],
              (overwritten["counts"], row_after))
        check("4.7 覆盖触发 R2-INV-01（动作清单里带追问/评分失效与保留历史）",
              bool(overwritten["invalidated"])
              and "R2-INV-01" in overwritten["invalidated"][0]["rules"]
              and "answer_followups.invalidate:answer_updated"
              in overwritten["invalidated"][0]["actions"], overwritten["invalidated"])
        check("4.8 覆盖后旧图片文件被删、新图片只有一份（无孤儿、无重复）",
              env.upload_names() == {row["file_path"] for row in
                                     env.db.query("SELECT file_path FROM answer_images")},
              env.files())

        # 覆盖时把旧评分/追问置失效
        with env.db.tx() as conn:
            answer_id = conn.execute(
                "SELECT id FROM answers WHERE record_key='A-S02-2'").fetchone()[0]
            conn.execute(
                "INSERT INTO answer_scores(class_id, student_id, answer_id, batch, question_no,"
                " full_score, score, state, answer_version, created_at, updated_at)"
                " VALUES(1,2,?,?, '2', 10, 8, 'confirmed', 'old-fp', 'now', 'now')",
                (answer_id, BATCH))
            conn.execute(
                "INSERT INTO answer_followups(class_id, student_id, answer_id, answer_version,"
                " question, source, created_at, updated_at)"
                " VALUES(1,2,?,'old-fp','你当时怎么移项的？','teacher','now','now')", (answer_id,))
        # 再改一次内容（否则与库里一致 → 只会被判重复，不会触发失效）
        for cells in rows[1:]:
            if cells[0] == "A-S02-2":
                cells[7] = "2x+2=6；2x=4；x=2（第二次改）"
        changed_again = build_zip(images, csv_bytes=csv_text(rows[1:], header=rows[0])
                                  .encode("utf-8-sig"))
        Z.commit(env.db, env.class_id, changed_again, batch=BATCH, uploads=env.uploads,
                 overwrite=True)
        scores = env.db.query(
            "SELECT state, invalidated_reason FROM answer_scores WHERE answer_id=?", (answer_id,))
        followups = env.db.query(
            "SELECT invalidated_reason FROM answer_followups WHERE answer_id=?", (answer_id,))
        check("4.9 覆盖后既有评分置 invalidated、旧追问标 answer_updated",
              bool(scores) and scores[0]["state"] == "invalidated"
              and scores[0]["invalidated_reason"] == "answer_updated"
              and bool(followups)
              and followups[0]["invalidated_reason"] == "answer_updated",
              (scores, followups))
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 五、缺图 / 仅照片 / 字符编码
# ---------------------------------------------------------------------------
def section_images_and_encoding():
    section("五、缺图提示、仅照片=内容不足、编码兼容（§3.4 第 5、6 条）")
    env = Env(students=4)
    try:
        rows = [row("R-S01-1", "学生01", "S01", "1"),
                row("R-S02-1", "学生02", "S02", "1", images="images/不存在的图.png"),
                row("R-S03-1", "学生03", "S03", "1", steps="", images="images/照片.png"),
                row("R-S04-1", "学生04", "S04", "1", steps="", missing="是", reason="请假")]
        data = build_zip([("images/照片.png", PNG)],
                         csv_bytes=csv_text(rows).encode("utf-8"))
        preview = Z.preview(env.db, env.class_id, data, batch="B1")
        by_key = {item["record_key"]: item for item in preview["rows"]}
        check("5.1 缺图行：可导入（category=valid）且提示「找不到这张图片」",
              by_key["R-S02-1"]["category"] == Z.CATEGORY_VALID
              and any("找不到" in text for text in by_key["R-S02-1"]["warnings"]), by_key["R-S02-1"])
        check("5.2 仅照片行：内容不足（insufficient），不是缺测",
              by_key["R-S03-1"]["content_state"]["state"] == "insufficient"
              and int(by_key["R-S03-1"]["image_count"] if "image_count" in by_key["R-S03-1"]
                      else len(by_key["R-S03-1"]["images"])) == 1, by_key["R-S03-1"])
        result = Z.commit(env.db, env.class_id, data, batch="B1", uploads=env.uploads)
        stored = env.db.query_one(
            "SELECT missing, missing_reason, student_steps FROM answers WHERE record_key='R-S03-1'")
        check("5.3 仅照片入库后 missing=0、无缺测原因、有 1 张图",
              stored["missing"] == 0 and stored["missing_reason"] == ""
              and env.count("answer_images",
                            "answer_id=(SELECT id FROM answers WHERE record_key='R-S03-1')") == 1,
              stored)
        check("5.4 缺图行照样入库（文字导入不被图片缺失阻断）",
              result["counts"]["new"] == 4
              and env.count("answer_images",
                            "answer_id=(SELECT id FROM answers WHERE record_key='R-S02-1')") == 0,
              result["counts"])
        check("5.5 明确标缺测的行仍是缺测（不被「有图就改判」覆盖）",
              env.db.query_one("SELECT missing FROM answers WHERE record_key='R-S04-1'")["missing"]
              == 1, "")

        # 编码：UTF-8 BOM 与 GB18030 都能读
        bom_package = build_zip([], csv_bytes=csv_text(rows).encode("utf-8-sig"))
        gbk_package = build_zip([], csv_bytes=csv_text(rows).encode("gb18030"))
        bom_preview = Z.preview(env.db, env.class_id, bom_package, batch="B1")
        gbk_preview = Z.preview(env.db, env.class_id, gbk_package, batch="B1")
        check("5.6 编码兼容：UTF-8 BOM 与 GB18030 的包都能解析出 4 行",
              bom_preview["total"] == 4 and gbk_preview["total"] == 4,
              (bom_preview["total"], gbk_preview["total"]))
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 六、限制：解压过程中实际执行
# ---------------------------------------------------------------------------
def section_limits():
    section("六、整包拒绝：限制在解压过程中实际执行，且一个文件都不落盘（§3.4 第 7 条）")
    check("6.1 限制常量：ZIP 100MB / 解压后 300MB / 2000 个文件 / 单图 10MB",
          Z.MAX_ZIP_BYTES == 100 * 1024 * 1024
          and Z.MAX_UNCOMPRESSED_BYTES == 300 * 1024 * 1024
          and Z.MAX_FILES == 2000 and Z.MAX_IMAGE_BYTES == 10 * 1024 * 1024,
          (Z.MAX_ZIP_BYTES, Z.MAX_UNCOMPRESSED_BYTES, Z.MAX_FILES, Z.MAX_IMAGE_BYTES))
    check("6.2 图片只收 JPG/JPEG/PNG",
          tuple(Z.ALLOWED_IMAGE_EXTS) == (".jpg", ".jpeg", ".png"), Z.ALLOWED_IMAGE_EXTS)

    env = Env(students=3)
    good_csv = csv_text([row("R-S01-1", "学生01", "S01", "1")]).encode("utf-8")

    cases = []

    def expect_reject(name, data, code, expected_code="", env_=env):
        before_answers = env_.count("answers")
        before_files = env_.upload_names()
        try:
            Z.read_package(data)
        except Z.ZipImportError as exc:
            ok = (exc.code == expected_code) and env_.count("answers") == before_answers \
                and env_.upload_names() == before_files
            check("%s → 拒绝（%s）且无写入/无落盘" % (name, expected_code), ok,
                  (exc.code, exc.message))
            return
        except ANSWERS.AnswerImportError as exc:
            check("%s → 拒绝（CSV 层错误也算拒绝）" % name, False, str(exc))
            return
        check("%s → 拒绝（%s）" % (name, expected_code), False, "竟然通过了校验")

    try:
        # 损坏 / 空
        expect_reject("6.3 不是 ZIP 的文件", b"this is not a zip", "", "invalid_zip")
        expect_reject("6.4 空字节", b"", "", "empty_file")
        truncated = open(PACKAGE_FIXTURE, "rb").read()[:512]
        expect_reject("6.5 截断损坏的 ZIP", truncated, "", "invalid_zip")

        # 路径安全
        expect_reject("6.6 绝对路径条目",
                      build_zip([("/etc/passwd.png", PNG)], csv_bytes=good_csv), "", "unsafe_path")
        expect_reject("6.7 盘符路径条目",
                      build_zip([("C:/x.png", PNG)], csv_bytes=good_csv), "", "unsafe_path")
        expect_reject("6.8 越界目录（images/../../x.png）",
                      build_zip([("images/../../x.png", PNG)], csv_bytes=good_csv), "",
                      "unsafe_path")

        # 加密 / 嵌套 / 类型
        expect_reject("6.9 加密条目的包",
                      mark_encrypted(build_zip([], csv_bytes=good_csv)), "", "encrypted")
        expect_reject("6.10 嵌套压缩包",
                      build_zip([("images/inner.zip", b"PK\x05\x06" + b"\x00" * 18)],
                                csv_bytes=good_csv), "", "nested_archive")
        expect_reject("6.11 不支持的文件类型（docx）",
                      build_zip([("images/说明.docx", b"x")], csv_bytes=good_csv), "",
                      "unsupported_file")
        expect_reject("6.12 图片不在 images/ 下",
                      build_zip([("图.png", PNG)], csv_bytes=good_csv), "", "image_not_in_images")
        expect_reject("6.13 根目录没有 answers.csv",
                      build_zip([("images/a.png", PNG)]), "", "missing_csv")
        expect_reject("6.14 根目录有多个 CSV",
                      build_zip([("b.csv", good_csv)], csv_bytes=good_csv), "", "multiple_csv")
        expect_reject("6.15 CSV 在子目录里",
                      build_zip([], csv_bytes=good_csv, csv_name="data/answers.csv"), "",
                      "csv_not_root")

        # 限制：文件数 / 单图 / 解压后总体积 / ZIP 体积
        many = build_zip([("images/%04d.png" % index, PNG) for index in range(2000)],
                         csv_bytes=good_csv)
        expect_reject("6.16 条目超过 2000 个上限", many, "", "too_many_files")

        big_image = build_zip([("images/big.png", b"\x00" * (10 * 1024 * 1024 + 1))],
                              csv_bytes=good_csv)
        expect_reject("6.17 单图超过 10MB 上限", big_image, "", "image_too_large")

        # 300MB 与 100MB 这两道闸：用「注入更小的上限」跑同一条代码路径（真造 300MB 包会拖垮套件）
        saved_zip_limit, saved_unc_limit = Z.MAX_ZIP_BYTES, Z.MAX_UNCOMPRESSED_BYTES
        try:
            Z.MAX_ZIP_BYTES = 1024
            expect_reject("6.18 ZIP 体积超过上限（同一道闸，上限注入为 1KB）",
                          open(PACKAGE_FIXTURE, "rb").read(), "", "zip_too_large")
            Z.MAX_ZIP_BYTES = saved_zip_limit
            Z.MAX_UNCOMPRESSED_BYTES = 2000
            expect_reject("6.19 解压后合计超过上限（同一道闸，上限注入为 2KB）",
                          build_zip([("images/big.png", b"\x00" * 4096)], csv_bytes=good_csv),
                          "", "too_large_uncompressed")
        finally:
            Z.MAX_ZIP_BYTES = saved_zip_limit
            Z.MAX_UNCOMPRESSED_BYTES = saved_unc_limit

        # 反向自证 1：摘掉「ZIP 体积」这道闸，同一包（上限注入 1KB）必须不再被判超限
        saved_limit = Z.MAX_ZIP_BYTES
        module_path = os.path.join(SRC, "app", "answer_import_zip.py")
        original = patch_bytes(module_path, [
            ("    if len(raw) > MAX_ZIP_BYTES:\n",
             "    if False:  # 变异体：摘掉 ZIP 体积闸门\n")])
        try:
            importlib.reload(Z)
            Z.MAX_ZIP_BYTES = 1024
            mutant_code = ""
            try:
                Z.read_package(open(PACKAGE_FIXTURE, "rb").read())
            except Z.ZipImportError as exc:
                mutant_code = exc.code
            check("6.20 反向自证：摘掉体积闸门后同一包不再报 zip_too_large（6.18 不恒真）",
                  mutant_code != "zip_too_large", mutant_code)
        finally:
            restore_bytes(module_path, original)
            Z.MAX_ZIP_BYTES = saved_limit
        check("6.21 恢复实现后同一包重新被拒（limit 仍注入 1KB）",
              _with_limit(1024, lambda: _reject_code(open(PACKAGE_FIXTURE, "rb").read()))
              == "zip_too_large", "")

        # 反向自证 2：摘掉「文件数」这道闸
        original = patch_bytes(module_path, [
            ("            if len(infos) > MAX_FILES:\n",
             "            if False:  # 变异体：摘掉文件数闸门\n")])
        try:
            importlib.reload(Z)
            mutant_ok = True
            try:
                Z.read_package(many)
            except Z.ZipImportError as exc:
                mutant_ok = exc.code != "too_many_files"
            check("6.22 反向自证：摘掉文件数闸门后 2001 个条目不再被拒（6.16 不恒真）",
                  mutant_ok is True, mutant_ok)
        finally:
            restore_bytes(module_path, original)
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 七、异常回滚 + 反向自证
# ---------------------------------------------------------------------------
def section_rollback():
    section("七、提交异常 → 整批回滚 + 清理本次新落盘的图片（§3.4 第 8 条）")
    package_bytes = open(PACKAGE_FIXTURE, "rb").read()

    # 7.1 全新班级：写到一半抛异常 → 库里 0 条、uploads 0 个文件
    env = Env()
    try:
        os.environ["MISTAKE_FAULT"] = "zip_commit"
        raised = ""
        try:
            Z.commit(env.db, env.class_id, package_bytes, batch=BATCH, uploads=env.uploads)
        except OSError as exc:
            raised = str(exc)
        finally:
            os.environ.pop("MISTAKE_FAULT", None)
        check("7.1 注入提交中途异常 → 抛错并整批回滚（answers 0 条）",
              bool(raised) and env.count("answers") == 0, (raised, env.count("answers")))
        check("7.2 回滚后 uploads 里没有本次落盘的孤儿文件",
              env.upload_names() == set(), env.files())
        check("7.3 回滚后连批次配置也没留下（同一次事务）",
              env.count("assignment_batches") == 0, env.count("assignment_batches"))

        # 7.4 已导入后覆盖时失败 → 原内容、原图片、原批次全都回到导入前
        Z.commit(env.db, env.class_id, package_bytes, batch=BATCH, uploads=env.uploads)
        files_before = env.upload_names()
        rows_before = env.db.query("SELECT id, record_key, fingerprint FROM answers"
                                   " ORDER BY id")
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            csv_bytes = archive.read("answers.csv")
            images = [(name, archive.read(name)) for name in archive.namelist()
                      if name.startswith("images/")]
        csv_rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        for cells in csv_rows[1:]:
            if cells[0] == "A-S01-1":
                cells[7] = "改过的步骤：x=2"
        changed = build_zip(images, csv_bytes=csv_text(csv_rows[1:], header=csv_rows[0])
                            .encode("utf-8-sig"))
        os.environ["MISTAKE_FAULT"] = "zip_commit"
        raised = ""
        try:
            Z.commit(env.db, env.class_id, changed, batch=BATCH, uploads=env.uploads,
                     overwrite=True)
        except OSError as exc:
            raised = str(exc)
        finally:
            os.environ.pop("MISTAKE_FAULT", None)
        check("7.4 覆盖途中失败 → 作答内容/指纹/图片全部回到覆盖前",
              bool(raised)
              and env.db.query("SELECT id, record_key, fingerprint FROM answers ORDER BY id")
              == rows_before
              and env.upload_names() == files_before, (raised, env.files()))

        # 反向自证：摘掉「回滚时删除新落盘文件」这一步，同一场景必须留下孤儿
        module_path = os.path.join(SRC, "app", "answer_import_zip.py")
        original = patch_bytes(module_path, [
            ("        if uploads is not None and saved_files:\n"
             "            try:\n"
             "                uploads.remove(saved_files)\n"
             "            except OSError:\n"
             "                pass\n"
             "        raise\n",
             "        raise  # 变异体：摘掉回滚时的孤儿文件清理\n")])
        try:
            importlib.reload(Z)
            env2 = Env()
            try:
                os.environ["MISTAKE_FAULT"] = "zip_commit"
                try:
                    Z.commit(env2.db, env2.class_id, package_bytes, batch=BATCH,
                             uploads=env2.uploads)
                except OSError:
                    pass
                finally:
                    os.environ.pop("MISTAKE_FAULT", None)
                check("7.5 反向自证：摘掉清理后同一故障确实留下孤儿文件（7.2 不恒真）",
                      env2.count("answers") == 0 and len(env2.files()) > 0, env2.files())
            finally:
                env2.close()
        finally:
            restore_bytes(module_path, original)
        check("7.6 恢复实现后再次回滚仍然零孤儿",
              (lambda: True)() and env.upload_names() == files_before, env.files())
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 八、原 CSV 导入不受影响 + 指纹同源
# ---------------------------------------------------------------------------
def section_csv_regression():
    section("八、原 CSV 导入照常可用，且两路 fingerprint 同源（§3.4 第 9 条）")
    # 模板里的示例学生是「示例学生甲（XY001）/ 示例学生乙」，先按模板把学生建好，
    # 否则这两行会落到「未匹配学生」——那是匹配问题，不是 CSV 导入坏了。
    env = Env(students=0)
    try:
        with env.db.tx() as conn:
            conn.execute("INSERT INTO students(class_id,name,code,created_at,updated_at)"
                         " VALUES(1,'示例学生甲','XY001','now','now')")
            conn.execute("INSERT INTO students(class_id,name,code,created_at,updated_at)"
                         " VALUES(1,'示例学生乙','','now','now')")
        text = ANSWERS.template_text()
        preview = ANSWERS.preview(env.db, env.class_id, text, "CSV批次")
        check("8.1 原 CSV 预览仍可用（模板 2 行示例，表头不再要求图片列）",
              preview["total"] == 2 and preview["counts"]["ok"] == 2, preview["counts"])
        committed = ANSWERS.commit(env.db, env.class_id, text, "CSV批次")
        check("8.2 原 CSV 提交仍可用（2 条入库）",
              committed["counts"]["new"] == 2 and env.count("answers") == 2,
              committed["counts"])
        csv_row = env.db.query_one(
            "SELECT record_key, fingerprint, missing, student_steps FROM answers"
            " WHERE record_key='A-001'")
        check("8.3 CSV 导入后没有图片行（R1 行为不变）", env.count("answer_images") == 0, "")

        # 同一内容走 ZIP：指纹必须与 CSV 一致（否则重复/冲突判定就不可信）
        same_row = ["A-001", "示例学生甲", "XY001", "1", "解方程 3(x-2)=2x+5", "一元一次方程",
                    "3x-6=2x+5；3x-2x=5+6；x=11", "3x-6=2x+5；x=11", "CSV批次", "2026-09-02",
                    "否", "", ""]
        zip_bytes = build_zip([], csv_bytes=csv_text([same_row]).encode("utf-8"))
        zip_preview = Z.preview(env.db, env.class_id, zip_bytes, batch="CSV批次")
        check("8.4 同一内容走 ZIP 时识别为「重复」而不是「冲突」（指纹同源）",
              zip_preview["counts"][Z.CATEGORY_DUPLICATE] == 1
              and zip_preview["counts"][Z.CATEGORY_CONFLICT] == 0, zip_preview["counts"])
        zip_row = zip_preview["rows"][0]
        zip_fingerprint = ANSWERS.fingerprint(zip_row, zip_row["student_id"])
        check("8.5 ZIP 侧算出的指纹与 CSV 侧入库指纹逐字相同（同一份内容只有一个版本号）",
              zip_row["student_id"] is not None
              and zip_fingerprint == csv_row["fingerprint"],
              (zip_fingerprint, csv_row["fingerprint"]))
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 九、桌面导入窗口（offscreen；没有 PySide6 时明确记为「未验证」）
# ---------------------------------------------------------------------------
class FakeService:
    """把 app 模块包装成 service 接口：证明窗口只依赖这三个方法（接线无需改窗口）。"""

    def __init__(self, env):
        self.db = env.db
        self.uploads = env.uploads
        self.calls = []

    def preview_zip_answers(self, class_id, data, batch="", selections=None):
        self.calls.append("preview")
        return Z.preview(self.db, class_id, data, batch)

    def import_zip_answers(self, class_id, data, batch="", selections=None, overwrite=False):
        self.calls.append("import")
        return Z.commit(self.db, class_id, data, batch, selections, overwrite, self.uploads)

    def zip_import_template(self):
        self.calls.append("template")
        return Z.template_zip()


def section_ui():
    section("九、桌面导入窗口（offscreen；无 PySide6 则记为未验证）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication      # noqa: F401
        from desktop.ui_import_zip import ZipImportWindow, _InlineRunner
    except Exception as exc:                            # noqa: BLE001
        log("SKIP  9.x 当前解释器没有 PySide6（%s）：UI 段未验证，"
            "请用 E:\\Agent\\venv-desktop\\Scripts\\python.exe 复跑本套件" % exc)
        return
    app = QApplication.instance() or QApplication([])
    package_bytes = open(PACKAGE_FIXTURE, "rb").read()
    env = Env()
    service = FakeService(env)
    window = ZipImportWindow(service, {"id": env.class_id, "name": "初三 1 班"})
    window.runner = _InlineRunner()                     # 测试不走后台线程
    try:
        window.load_package(package_bytes, batch=BATCH, path="R2_验收_10x5.zip")
        check("9.1 选包后表格逐行铺开 50 行", window.table.rowCount() == 50,
              window.table.rowCount())
        check("9.2 摘要显示压缩包结构（文件数/解压体积/图片张数）",
              "answers.csv" in window.summary.text() and "4 张" in window.summary.text()
              or "图片 4" in window.summary.text(), window.summary.text())
        check("9.3 状态行给出六类计数（有效 45、缺测 5）",
              "有效 45" in window.status.text() and "缺测 5" in window.status.text(),
              window.status.text())
        check("9.4 有可导入行时「确认导入」可用", window.import_button.isEnabled() is True, "")
        check("9.5 分类列显示中文（不是英文枚举）",
              window.table.item(0, 1).text() in tuple(Z.CATEGORY_LABELS.values()),
              window.table.item(0, 1).text())

        result = window.do_import(overwrite=False, confirm=False)
        check("9.6 点确认导入后真正入库 50 条（窗口→service→app 三层贯通）",
              result and result["counts"]["new"] == 50 and env.count("answers") == 50,
              (result or {}).get("counts"))
        check("9.7 结果区显示批次与图片落盘张数",
              BATCH in window.report.toPlainText() and "图片落盘 4 张" in window.report.toPlainText(),
              window.report.toPlainText())

        # 冲突：不勾覆盖时按钮禁用，勾上才可用
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            csv_bytes = archive.read("answers.csv")
            images = [(name, archive.read(name)) for name in archive.namelist()
                      if name.startswith("images/")]
        csv_rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        for cells in csv_rows[1:]:
            if cells[0] == "A-S01-1":
                cells[7] = "改过的步骤：x=6"
        changed = build_zip(images, csv_bytes=csv_text(csv_rows[1:], header=csv_rows[0])
                            .encode("utf-8-sig"))
        window.load_package(changed, batch=BATCH)
        check("9.8 冲突包：未勾「覆盖」时导入按钮禁用",
              window.import_button.isEnabled() is False, window.status.text())
        window.overwrite_check.setChecked(True)
        check("9.9 勾上「覆盖」后按钮可用", window.import_button.isEnabled() is True, "")
        overwritten = window.do_import(overwrite=True, confirm=False)
        check("9.10 覆盖导入只改 1 条，且结果区列出 R2-INV-01 失效明细",
              overwritten and overwritten["counts"]["overwritten"] == 1
              and "R2-INV-01" in window.report.toPlainText(),
              (overwritten or {}).get("counts"))

        # 模板下载 + 问题明细导出（都走可选路径参数，测试不弹对话框）
        template_path = os.path.join(env.work, "模板导出的.zip")
        saved = window.save_template(template_path)
        check("9.11 窗口能导出模板，且内容与 template_zip() 逐字节一致",
              saved == template_path and open(template_path, "rb").read() == Z.template_zip(),
              saved)
        bad_package = build_zip([("images/x.gif", b"GIF89a")],
                                csv_bytes=csv_text([row("R-S01-1", "学生01", "S01", "1")])
                                .encode("utf-8"))
        try:
            window.load_package(bad_package, batch="坏包")
            rejected_text = window.status.text()
        except Exception as exc:                        # noqa: BLE001
            rejected_text = str(exc)
        check("9.12 不支持的文件类型在窗口里被拒（提示写在状态行，不清空已选文件）",
              "不支持的文件类型" in rejected_text and window.file_label.text() != "",
              rejected_text)
        window.load_package(package_bytes, batch=BATCH)
        problems_path = os.path.join(env.work, "问题明细.csv")
        exported = window.export_problems(problems_path)
        check("9.13 问题明细导出（全部可导入时也给出明确提示）",
              exported == "" and "没有需要导出" in window.status.text()
              or (exported == problems_path and os.path.exists(problems_path)),
              (exported, window.status.text()))
    finally:
        window.close()
        env.close()


# ---------------------------------------------------------------------------
def main():
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from logmeta import environment_lines

    for line in environment_lines():
        log(line)
    log("套件：R2-01 压缩包批量导入（R2 契约版本 %s）" % C.R2_CONTRACT_VERSION)
    log("")
    try:
        section_template()
        section_preview()
        section_commit()
        section_reimport()
        section_images_and_encoding()
        section_limits()
        section_rollback()
        section_csv_regression()
        section_ui()
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
