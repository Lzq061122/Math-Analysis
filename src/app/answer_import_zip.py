"""R2-01 压缩包批量导入：`assignment.zip{answers.csv, images/}` 的校验、预览与入库。

与 R1 的 CSV 导入（`src/app/answers.py`）**并存**，不替换它：
- 表头、逐行校验、学生匹配、内容指纹、重复/冲突判定，全部沿用 `answers.py` 的既有实现；
- 本模块只增加三件事：**ZIP 容器安全**（解压限制、路径校验、加密/嵌套拒绝）、
  **多图**（包内 `images/` 落盘到 uploads + `answer_images` 表）、**压缩包预览分类**
  （有效/重复/冲突/缺测/未匹配学生/文件异常）。

几条硬口径（派单 §三 + R2 接口契约）：
1. **不发网络请求**：整包只在本机内存与数据库里流转，不调用任何 AI 服务，不实现手写 OCR。
2. **预览严格只读**：`preview()` 不写数据库、不落盘；只有 `commit()` 才写。
3. **限制在解压过程中实际执行**：不是在界面上提示一句，而是遍历 zip 条目时就判：
   ZIP 100 MB、解压后合计 300 MB、文件数 2000、单图 10 MB、图片只收 JPG/JPEG/PNG。
4. **拒绝非法包**：绝对路径、`..` 越界、加密条目、嵌套压缩包、不支持的文件类型、
   根目录下不是恰好一个 CSV——一律整包拒绝，且**一个文件都不落盘**。
5. **仅照片作答 = 内容不足**（`r2_contract.classify_record_state` → `insufficient`）：
   不能沿用 R1「没有学生步骤就算缺测」的推断，否则「未提交/缺测」统计会被照片作答污染。
6. **覆盖 = R2-INV-01**：同编号异内容覆盖后，旧版本上的追问证据失效、既有评分失效，
   并把 `r2_contract.apply_event` 的动作清单原样回给调用方，便于 R2-02 复核。
7. **提交异常整批回滚**：事务内失败 → 回滚 + 删除本次新落盘的图片（不留孤立文件）。
8. **批次登记**：导入后按 `db.ensure_batch_config()` 登记批次（不覆盖老师已有配置）。
"""

import csv
import hashlib
import io
import json
import os
import re
import struct
import uuid
import zipfile
from datetime import datetime

from . import answers as answers_mod
from . import db as db_mod
from . import r2_contract

# ---------------------------------------------------------------------------
# 限制（派单 §三 的数值口径，测试逐条断言）
# ---------------------------------------------------------------------------
MAX_ZIP_BYTES = 100 * 1024 * 1024          # 整个 ZIP 文件
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024  # 解压后合计
MAX_FILES = 2000                            # 条目总数（含目录）
MAX_IMAGE_BYTES = 10 * 1024 * 1024          # 单张图片
ALLOWED_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
NESTED_ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz")

# 图片路径列：与 R1 的 12 列并列，属于 R2 新增的可选列（不填 = 纯文字导入，行为与 R1 一致）
IMAGE_HEADERS = ("图片路径", "图片", "图片路径（多图用分号分隔）", "image_paths", "images")
IMAGE_SEPARATORS = (";", "；", "|")

# 目录条目之外的「系统垃圾」：静默忽略，不算不支持文件（它们不是老师放的作业内容）
IGNORED_PREFIXES = ("__MACOSX/",)
IGNORED_NAMES = (".ds_store", "thumbs.db", "desktop.ini")

CSV_NAME = "answers.csv"
IMAGES_DIR = "images/"
IMAGE_DIR_LABEL = "images/"

MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

# 预览分类（派单 §三的分类口径，界面与统计都按这六个值出数）
CATEGORY_VALID = "valid"            # 有效（可导入）
CATEGORY_MISSING = "missing"        # 缺测（可导入，带 missing=1）
CATEGORY_DUPLICATE = "duplicate"    # 重复（同编号同内容，跳过）
CATEGORY_CONFLICT = "conflict"      # 冲突（同编号异内容，需老师选「跳过/覆盖」）
CATEGORY_UNMATCHED = "unmatched"    # 未匹配学生（需老师选，或补学生名单）
CATEGORY_FILE_ERROR = "file_error"  # 文件异常（图片缺失/类型不支持/行内容不合规）
CATEGORIES = (CATEGORY_VALID, CATEGORY_MISSING, CATEGORY_DUPLICATE, CATEGORY_CONFLICT,
              CATEGORY_UNMATCHED, CATEGORY_FILE_ERROR)

CATEGORY_LABELS = {
    CATEGORY_VALID: "有效",
    CATEGORY_MISSING: "缺测",
    CATEGORY_DUPLICATE: "重复",
    CATEGORY_CONFLICT: "冲突",
    CATEGORY_UNMATCHED: "未匹配学生",
    CATEGORY_FILE_ERROR: "文件异常",
}

# 整包拒绝的原因码（测试按 code 断言，界面按 message 显示中文）
ZIP_ERROR_CODES = {
    "empty_file": "没有读到文件内容",
    "zip_too_large": "压缩包超过 100 MB，请拆分后重试",
    "invalid_zip": "这个文件不是有效的 ZIP 压缩包（可能已损坏）",
    "encrypted": "压缩包里的文件被加密了，请去掉密码后重新打包",
    "too_many_files": "压缩包里的文件超过 2000 个上限，请拆分后重试",
    "too_large_uncompressed": "解压后合计超过 300 MB 上限，请拆分后重试",
    "unsafe_path": "压缩包里有非法路径（绝对路径或越界目录），已拒绝整包",
    "nested_archive": "压缩包里还套着压缩包，请解开只放本次作业",
    "unsupported_file": "压缩包里有不支持的文件类型，请只放 answers.csv 与 images/ 下的图片",
    "image_too_large": "单张图片超过 10 MB 上限，请压缩后重试",
    "missing_csv": "压缩包根目录必须有且只有一个 answers.csv",
    "multiple_csv": "压缩包根目录有多个 CSV，无法确定用哪一个",
    "csv_not_root": "answers.csv 必须放在压缩包根目录",
    "image_not_in_images": "图片必须放在 images/ 目录下",
    "csv_too_large": "answers.csv 超过 2 MB，请只放一个批次",
}


class ZipImportError(Exception):
    """整包不能导入（体积/类型/路径/加密/CSV 结构等）。`code` 供界面与测试断言。"""

    def __init__(self, message, code="invalid_zip", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _zip_error(code, detail=""):
    message = ZIP_ERROR_CODES.get(code, "压缩包无法导入")
    if detail:
        message = "%s（%s）" % (message, detail)
    return ZipImportError(message, code)


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 一、容器安全：在不落盘的前提下把限制真的执行一遍
# ---------------------------------------------------------------------------
class Package:
    """一个已通过校验的压缩包：只保留原始字节与条目清单，图片按需读取。

    为什么不先解压到临时目录：解压即落盘，会绕过「文件数/总体积」的预算判定，
    也会把非法路径真正创建出来。这里全程在内存里完成校验，只有 `commit()` 才把
    **图片**写进 uploads（文件名由程序生成，不采用包内路径）。
    """

    def __init__(self, data, csv_name, csv_bytes, images, files, uncompressed, total_bytes):
        self.data = data
        self.csv_name = csv_name
        self.csv_bytes = csv_bytes
        self.images = images                  # {包内相对路径: {"bytes": n, "ext": ".png"}}
        self.files = files                    # 条目数（含目录）
        self.uncompressed = uncompressed      # 解压后合计字节
        self.total_bytes = total_bytes        # zip 文件本身字节

    def has_image(self, path):
        return normalize_rel_path(path) in self.images

    def read_image(self, path):
        """读出一张图片的字节；读取时再次核对单图上限（防止声明与实际不符）。"""
        name = normalize_rel_path(path)
        info = self.images.get(name)
        if info is None:
            raise ZipImportError("压缩包里没有这张图片：%s" % path, "missing_image")
        if info["bytes"] > MAX_IMAGE_BYTES:
            raise _zip_error("image_too_large", "%s 有 %d 字节" % (name, info["bytes"]))
        with zipfile.ZipFile(io.BytesIO(self.data)) as archive:
            blob = archive.read(name)
        if len(blob) > MAX_IMAGE_BYTES:
            raise _zip_error("image_too_large", "%s 实际 %d 字节" % (name, len(blob)))
        return blob

    def summary(self):
        return {
            "bytes": self.total_bytes,
            "files": self.files,
            "uncompressed_bytes": self.uncompressed,
            "csv_name": self.csv_name,
            "image_count": len(self.images),
        }


def normalize_rel_path(path):
    """把包内相对路径归一：统一用 `/`、去掉前导 `./`、合并重复分隔符。"""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = re.sub(r"/{2,}", "/", text)
    return text


def _is_unsafe_name(name):
    raw = str(name or "")
    if not raw:
        return True
    if raw.startswith("/") or raw.startswith("\\"):
        return True
    if re.match(r"^[A-Za-z]:", raw):
        return True
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    return any(part in ("..", "") for part in parts[:-1]) or parts[-1] == ".."


def _is_ignored(name):
    lowered = name.lower()
    if lowered.startswith(IGNORED_PREFIXES):
        return True
    return os.path.basename(lowered) in IGNORED_NAMES


def read_package(data):
    """校验并读取压缩包，返回 `Package`；不合格一律抛 `ZipImportError`（不落盘）。

    判定顺序（每条都是「先拒绝，再谈内容」）：
      体积 → 是否合法 ZIP → 条目数 → 加密 → 路径安全 → 嵌套压缩包 → 类型/单图体积 → 总体积 → CSV
    """
    if not isinstance(data, (bytes, bytearray)):
        raise _zip_error("empty_file")
    raw = bytes(data)
    if not raw:
        raise _zip_error("empty_file")
    if len(raw) > MAX_ZIP_BYTES:
        raise _zip_error("zip_too_large", "%d 字节" % len(raw))
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise _zip_error("invalid_zip")

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise _zip_error("too_many_files", "%d 个" % len(infos))
            images = {}
            total = 0
            csv_names = []
            for info in infos:
                name = info.filename or ""
                if info.is_dir():
                    if _is_unsafe_name(name.rstrip("/")):
                        raise _zip_error("unsafe_path", name)
                    continue
                if info.flag_bits & 0x1:
                    raise _zip_error("encrypted", name)
                if _is_unsafe_name(name):
                    raise _zip_error("unsafe_path", name)
                if _is_ignored(name):
                    continue
                total += int(info.file_size or 0)
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise _zip_error("too_large_uncompressed", "%d 字节" % total)
                lowered = name.lower()
                ext = os.path.splitext(lowered)[1]
                if ext in NESTED_ARCHIVE_EXTS:
                    raise _zip_error("nested_archive", name)
                if ext == ".csv":
                    if "/" in name.strip("/"):
                        raise _zip_error("csv_not_root", name)
                    csv_names.append(name)
                    continue
                if ext in ALLOWED_IMAGE_EXTS:
                    if not name.startswith(IMAGES_DIR):
                        raise _zip_error("image_not_in_images", name)
                    if int(info.file_size or 0) > MAX_IMAGE_BYTES:
                        raise _zip_error("image_too_large", name)
                    images[normalize_rel_path(name)] = {
                        "bytes": int(info.file_size or 0),
                        "ext": ext,
                    }
                    continue
                raise _zip_error("unsupported_file", name)

            if not csv_names:
                raise _zip_error("missing_csv")
            if len(csv_names) > 1:
                raise _zip_error("multiple_csv", "、".join(csv_names))
            csv_name = csv_names[0]
            try:
                csv_bytes = archive.read(csv_name)
            except RuntimeError as exc:  # 加密条目在 read 时才报
                raise _zip_error("encrypted", str(exc))
    except zipfile.BadZipFile:
        raise _zip_error("invalid_zip")

    if len(csv_bytes) > answers_mod.MAX_TEXT_BYTES:
        raise _zip_error("csv_too_large", "%d 字节" % len(csv_bytes))
    return Package(raw, csv_name, csv_bytes, images, len(infos), total, len(raw))


# ---------------------------------------------------------------------------
# 二、CSV 解析：沿用 answers.py 的表头/校验/指纹，只多认一列「图片路径」
# ---------------------------------------------------------------------------
def _split_image_cells(text):
    """把「图片路径」列摘出来，返回 `(去该列后的 CSV 文本, {行号: 原始图片单元格}, {行号: 原始缺测单元格})`。

    为什么要摘列：R1 的 `answers.parse()` 是表头与逐行校验的**唯一实现**，
    R2 不该再写一份。摘掉 R2 新增的可选列之后，剩下的 12 列与 R1 完全一致，
    校验、学生匹配、内容指纹因此天然同源（同一份内容，CSV 导入与 ZIP 导入的
    fingerprint 逐字相同，重复/冲突判定才可信）。
    行号保持原样（空行照抄），`answers.parse()` 的 line 编号才对得上界面。

    顺带把「缺测」列的**原文**记下来：R1 的 `answers.parse()` 会把「没写步骤」自动判成
    缺测并把 missing 归一成 0/1，丢掉「老师到底有没有明确标缺测」这个事实。仅照片作答
    要改判成「内容不足」，就必须能区分这两种来源，所以这里留一份原文。
    """
    reader = csv.reader(io.StringIO(text))
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise answers_mod.AnswerImportError("CSV 格式无法解析（%s）" % exc)
    if not rows:
        raise answers_mod.AnswerImportError("文件是空的")
    header = rows[0]
    image_indexes = []
    missing_index = None
    for index, name in enumerate(header):
        cleaned = (name or "").strip().lstrip("\ufeff")
        if cleaned in IMAGE_HEADERS or cleaned.lower() in IMAGE_HEADERS:
            image_indexes.append(index)
        if cleaned == answers_mod.LABELS["missing"] or cleaned.lower() == "missing":
            missing_index = index
    if not image_indexes and missing_index is None:
        return text, {}, {}

    image_by_line = {}
    missing_by_line = {}
    kept_rows = []
    for offset, cells in enumerate(rows):
        line_no = offset + 1
        image_cells = []
        kept = []
        for index, cell in enumerate(cells):
            if index in image_indexes:
                if (cell or "").strip():
                    image_cells.append((cell or "").strip())
            else:
                kept.append(cell)
        kept_rows.append(kept)
        if image_cells:
            image_by_line[line_no] = image_cells[0]
        if missing_index is not None:
            value = cells[missing_index] if missing_index < len(cells) else ""
            missing_by_line[line_no] = (value or "").strip()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for cells in kept_rows:
        if not any((cell or "").strip() for cell in cells):
            buffer.write("\r\n")
            continue
        writer.writerow(cells)
    return buffer.getvalue(), image_by_line, missing_by_line


def split_image_paths(value):
    """把「图片路径」单元格拆成多条（`;` / `；` / `|` 分隔），保持顺序、去重。"""
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"[;；|]", text)
    paths = []
    for part in parts:
        item = normalize_rel_path(part)
        if item and item not in paths:
            paths.append(item)
    return paths


def parse(text, batch="", package=None):
    """解析 CSV 并补上图片信息，返回 `(rows, header_error)`。

    每行在 R1 的字段之外多三个键：
      `image_paths`  声明了哪些图片（包内相对路径）
      `images`       每张图片的解析结果 `[{"path","present","bytes","mime"}]`
      `content_state` 作答维状态（`valid` / `insufficient` / `missing`）
    以及 `raw_missing`（原始「缺测」单元格），用于区分「老师明确标了缺测」与
    「R1 因为没写步骤而自动判缺测」——后者在有图片时要改判成「内容不足」。
    """
    cleaned, image_cells, missing_cells = _split_image_cells(text)
    rows, header_error = answers_mod.parse(cleaned, batch)
    if header_error:
        return [], header_error
    for row in rows:
        declared = split_image_paths(image_cells.get(row["line"], ""))
        row["image_paths"] = declared
        row["images"] = []
        row["image_problems"] = []
        for path in declared:
            ext = os.path.splitext(path.lower())[1]
            present = bool(package is not None and package.has_image(path))
            if ext not in ALLOWED_IMAGE_EXTS:
                row["image_problems"].append("图片类型不支持：%s（只收 JPG/JPEG/PNG）" % path)
                continue
            if not present:
                row["image_problems"].append("压缩包里找不到这张图片：%s" % path)
            row["images"].append({
                "path": path,
                "present": present,
                "bytes": (package.images.get(path) or {}).get("bytes", 0) if present else 0,
                "mime": MIME_BY_EXT.get(ext, ""),
            })
        row["raw_missing"] = missing_cells.get(row["line"], "")
        row["content_state"] = _content_state(row)
    return rows, ""


def _content_state(row):
    """按四维契约的作答维判定这一行的状态（并修正 R1 的「没步骤就算缺测」推断）。"""
    steps = (row.get("student_steps") or "").strip()
    declared = [item for item in row.get("image_paths") or []]
    state, reason = r2_contract.classify_record_state(
        missing=row.get("missing"),
        missing_reason=row.get("missing_reason") or "",
        has_text=False,
        has_steps=bool(steps),
        image_count=len(declared),
    )
    return {"state": state, "reason": reason}


def _adjust_missing_for_images(row):
    """仅照片作答：R1 会把「没写步骤」自动判成缺测，R2 必须改判成「内容不足」。

    判据只有一条：**老师没有在「缺测」列里明确写「是」，且这一行有图片声明**。
    明确标了缺测的行一律不动（缺测优先，与 `classify_record_state` 的优先级一致）。
    """
    if (row.get("raw_missing") or "").strip().lower() in answers_mod.TRUE_WORDS:
        return False
    if (row.get("student_steps") or "").strip():
        return False
    if not (row.get("image_paths") or []):
        return False
    row["missing"] = 0
    row["missing_reason"] = ""
    row["content_state"] = {"state": "insufficient", "reason": "image_only"}
    return True


# ---------------------------------------------------------------------------
# 三、预览（严格只读）
# ---------------------------------------------------------------------------
def preview(db, class_id, data, batch=""):
    """预览整包：不写数据库、不落盘，只给分类与逐行明细。

    `data` 是 ZIP 的原始字节；`batch` 为空时用压缩包文件名（去扩展名）作批次名。
    """
    package = read_package(data)
    text = answers_mod.decode(package.csv_bytes)
    batch = (batch or "").strip()
    rows, header_error = parse(text, batch, package)
    if header_error:
        raise answers_mod.AnswerImportError(header_error)

    students = answers_mod._class_students(db, class_id)
    existing = {
        row["record_key"]: row
        for row in db.query(
            "SELECT record_key, student_id, fingerprint FROM answers WHERE class_id=?", (class_id,)
        )
    }
    for index, row in enumerate(rows):
        _adjust_missing_for_images(row)
        item = dict(row)
        item["status"] = "error"
        item["reason"] = ""
        item["warnings"] = list(row.get("image_problems") or [])
        item["student_id"] = None
        item["student_name_matched"] = ""
        item["candidates"] = []
        item["existing"] = None

        student, candidates, problem = answers_mod._match(row, students)
        if candidates and not student:
            item["candidates"] = [
                {"id": s["id"], "name": s["name"], "code": s.get("code") or "",
                 "active": int(s.get("active", 1) or 0)}
                for s in candidates
            ]
        if problem:
            item["reason"] = problem
            item["category"] = CATEGORY_UNMATCHED
        elif row["errors"]:
            item["reason"] = "；".join(row["errors"])
            item["category"] = CATEGORY_FILE_ERROR
        else:
            item["student_id"] = student["id"]
            item["student_name_matched"] = student["name"]
            old = existing.get(row["record_key"])
            if not old:
                item["status"] = "ok"
                item["category"] = (CATEGORY_MISSING if int(row["missing"])
                                    else CATEGORY_VALID)
            elif old["fingerprint"] == answers_mod.fingerprint(row, student["id"]):
                item["status"] = "duplicate"
                item["category"] = CATEGORY_DUPLICATE
                item["reason"] = "这条记录已经导入过，内容相同，不会重复计数"
            else:
                item["status"] = "conflict"
                item["category"] = CATEGORY_CONFLICT
                item["reason"] = "记录编号已存在但内容不同，需要老师确认是否覆盖"
                item["existing"] = {
                    "student_id": old["student_id"],
                    "fingerprint": old["fingerprint"],
                }
        # 声明的图片类型不支持 → 这一行算「文件异常」；只是缺图 → 只给提示，不阻断导入
        blocked = [message for message in (row.get("image_problems") or [])
                   if "找不到" not in message]
        if blocked:
            item["status"] = "error"
            item["category"] = CATEGORY_FILE_ERROR
            item["reason"] = "；".join(blocked)
        rows[index] = item

    return {
        "kind": "zip",
        "batch": batch,
        "package": package.summary(),
        "total": len(rows),
        "rows": rows,
        "counts": _preview_counts(rows),
        "importable": sum(1 for row in rows
                          if row["status"] in ("ok", "conflict") and row["category"] != CATEGORY_FILE_ERROR),
        "images_declared": sum(len(row.get("image_paths") or []) for row in rows),
        "images_present": sum(1 for row in rows for image in row.get("images") or []
                              if image.get("present")),
    }


def _preview_counts(rows):
    counts = {category: 0 for category in CATEGORIES}
    for row in rows:
        category = row.get("category") or CATEGORY_FILE_ERROR
        counts[category] = counts.get(category, 0) + 1
    counts["insufficient"] = sum(1 for row in rows
                                 if (row.get("content_state") or {}).get("state") == "insufficient")
    return counts


# ---------------------------------------------------------------------------
# 四、提交（事务 + 多图落盘 + 覆盖失效）
# ---------------------------------------------------------------------------
def commit(db, class_id, data, batch="", selections=None, overwrite=False, uploads=None):
    """把预览里可导入的行写进库；任何一步失败整批回滚并清掉本次新落盘的图片。

    - 只写 `status == "ok"` 的行；`duplicate` 跳过；`conflict` 仅在 `overwrite=True` 时替换；
    - 图片落盘到 `uploads`（文件名程序生成，绝不采用包内路径），并写 `answer_images`；
    - 覆盖已有记录时按 `R2-INV-01` 让旧结论失效（追问证据 / 既有评分），并把动作清单回给调用方；
    - 导入成功后调用 `db.ensure_batch_config()` 登记批次（不覆盖老师已有配置）。
    """
    data_preview = preview(db, class_id, data, batch)
    rows = data_preview["rows"]
    batch = data_preview["batch"]
    students = answers_mod._class_students(db, class_id)
    if selections:
        answers_mod.apply_selections(rows, students, selections)
        existing = {
            row["record_key"]: row
            for row in db.query(
                "SELECT record_key, student_id, fingerprint FROM answers WHERE class_id=?",
                (class_id,))
        }
        for row in rows:
            if row.get("status") not in ("ok", "error") or not row.get("student_id"):
                continue
            old = existing.get(row["record_key"])
            if not old:
                continue
            row["status"] = ("duplicate"
                             if old["fingerprint"] == answers_mod.fingerprint(row, row["student_id"])
                             else "conflict")
            row["category"] = (CATEGORY_DUPLICATE if row["status"] == "duplicate"
                               else CATEGORY_CONFLICT)
    package = read_package(data)
    now = _now()
    saved_files = []
    stale_files = []
    written_images = 0
    inserted = 0
    updated = 0
    invalidated = []
    try:
        with db.tx() as conn:
            for row in rows:
                if row["status"] not in ("ok", "conflict"):
                    continue
                if row["status"] == "conflict" and not overwrite:
                    continue
                answer_id, created = _write_answer(
                    conn, class_id, row, batch, now,
                    overwrite=bool(overwrite and row["status"] == "conflict"))
                if answer_id is None:
                    continue
                if created:
                    inserted += 1
                else:
                    updated += 1
                    stale_files.extend(_drop_images(conn, answer_id))
                    invalidated.append(_invalidate_after_overwrite(conn, answer_id, now))
                count, files = _write_images(conn, class_id, row, answer_id, package, uploads,
                                             now)
                written_images += count
                saved_files.extend(files)
                row["answer_id"] = answer_id
                row["status"] = "ok" if created else "overwritten"
            _fault("zip_commit")
            db_mod.ensure_batch_config(db, class_id, batch,
                                       origin=r2_contract.BATCH_ORIGIN_IMPORT)
    except BaseException:
        # 事务已回滚；把本次新落盘的图片删掉，绝不留孤立文件
        if uploads is not None and saved_files:
            try:
                uploads.remove(saved_files)
            except OSError:
                pass
        raise
    # 覆盖成功后清理被替换掉的旧图片文件（事务已提交，删文件不会破坏一致性）
    if uploads is not None and stale_files:
        try:
            uploads.remove(stale_files)
        except OSError:
            pass

    counts = {"new": inserted, "overwritten": updated, "duplicate": 0, "conflict": 0, "failed": 0}
    for row in rows:
        if row["status"] == "duplicate":
            counts["duplicate"] += 1
        elif row["status"] == "conflict":
            counts["conflict"] += 1
        elif row["status"] == "error":
            counts["failed"] += 1
    return {
        "kind": "zip",
        "batch": batch,
        "total": len(rows),
        "counts": counts,
        "images": written_images,
        "invalidated": [item for item in invalidated if item],
        "rows": rows,
        "package": data_preview["package"],
        "errors": [row for row in rows if row["status"] in ("error", "conflict")],
    }


def _fault(name):
    """测试注入点（与仓库既有做法一致）：MISTAKE_FAULT 命中时抛 OSError。"""
    if name in (os.environ.get("MISTAKE_FAULT") or "").split(","):
        raise OSError("模拟故障：%s" % name)


def _write_answer(conn, class_id, row, batch, now, overwrite=False):
    """写一行作答；返回 `(answer_id, created)`。同编号且要求覆盖时走 UPDATE。"""
    print_fp = answers_mod.fingerprint(row, row["student_id"])
    existing = conn.execute(
        "SELECT id, fingerprint FROM answers WHERE class_id=? AND record_key=?",
        (class_id, row["record_key"]),
    ).fetchone()
    if existing is not None and not overwrite:
        return None, False
    if existing is not None:
        conn.execute(
            "UPDATE answers SET student_id=?, batch=?, student_name=?, student_code=?,"
            " question_no=?, question_text=?, unit=?, standard_solution=?, student_steps=?,"
            " source=?, occurred_on=?, missing=?, missing_reason=?, fingerprint=?,"
            " imported_at=?, updated_at=? WHERE id=?",
            (
                row["student_id"], batch, row["student_name"], row["student_code"],
                row["question_no"], row["question_text"], row["unit"],
                row["standard_solution"], row["student_steps"], row["source"],
                row["occurred_on"], int(row["missing"]), row["missing_reason"],
                print_fp, now, now, int(existing[0]),
            ),
        )
        return int(existing[0]), False
    cursor = conn.execute(
        "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
        " student_code, question_no, question_text, unit, standard_solution,"
        " student_steps, source, occurred_on, missing, missing_reason, fingerprint,"
        " imported_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            class_id, row["student_id"], row["record_key"], batch,
            row["student_name"], row["student_code"], row["question_no"],
            row["question_text"], row["unit"], row["standard_solution"],
            row["student_steps"], row["source"], row["occurred_on"],
            int(row["missing"]), row["missing_reason"], print_fp, now, now, now,
        ),
    )
    return int(cursor.lastrowid), True


def _invalidate_after_overwrite(conn, answer_id, now):
    """覆盖已有作答后按 R2-INV-01 执行失效；返回执行了什么（供界面/R2-02 复核）。

    这里只做**本模块拥有的数据**：
      - 追问证据按 `answer_updated` 失效（与 R1-06 的证据快照口径一致）；
      - 既有评分置 `invalidated`（R2-INV-01 的 `answer_scores.invalidate`），
        否则旧成绩会继续挂在旧内容上、把平均分算错；
      - `answer_checks` 不需要改：它的「当前结论」判据是
        `answer_version = answers.fingerprint`，指纹一变旧结论自动退出当前视图。
    规则本身不在这里判断：用 `r2_contract.apply_event` 取动作清单，避免两处各写一套失效。
    """
    row = conn.execute(
        "SELECT fingerprint FROM answers WHERE id=?", (answer_id,)).fetchone()
    fingerprint = row[0] if row else ""
    plan = r2_contract.apply_event(
        "answer_content_changed",
        {
            "answer": {"state": "valid", "fingerprint": fingerprint},
            "verify": {"state": "verified_rule", "version": ""},
            "cause": {"state": "decided"},
            "score": {"state": "confirmed", "rubric_version": ""},
        },
        {"fingerprint": fingerprint + "#changed"},
    )
    actions = list(plan["actions"])
    invalidated_followups = 0
    invalidated_scores = 0
    if "answer_followups.invalidate:answer_updated" in actions:
        cursor = conn.execute(
            "UPDATE answer_followups SET invalidated_at=?, invalidated_reason=?"
            " WHERE answer_id=? AND invalidated_at='' AND answer_version<>?",
            (now, "answer_updated", answer_id, fingerprint),
        )
        invalidated_followups = cursor.rowcount or 0
    if "answer_scores.invalidate" in actions:
        cursor = conn.execute(
            "UPDATE answer_scores SET state='invalidated', invalidated_at=?,"
            " invalidated_reason='answer_updated', updated_at=?"
            " WHERE answer_id=? AND state IN ('draft','confirmed')",
            (now, now, answer_id),
        )
        invalidated_scores = cursor.rowcount or 0
    return {
        "answer_id": answer_id,
        "rules": list(plan["rules"]),
        "actions": actions,
        "followups": invalidated_followups,
        "scores": invalidated_scores,
    }


def _drop_images(conn, answer_id):
    """覆盖作答时先摘掉旧的图片行，返回旧文件名列表（提交成功后由调用方删文件）。

    为什么先删行、提交后再删文件：事务回滚时旧行会一起回来，但文件删了不会回来，
    所以文件必须等 COMMIT 之后再删；先把文件名收集起来，避免回滚后丢了线索。
    """
    names = [row[0] for row in conn.execute(
        "SELECT file_path FROM answer_images WHERE answer_id=?", (answer_id,))]
    conn.execute("DELETE FROM answer_images WHERE answer_id=?", (answer_id,))
    return names


def _write_images(conn, class_id, row, answer_id, package, uploads, now):
    """把这一行声明的图片逐张落盘并写 `answer_images`；返回 `(张数, 新文件名列表)`。

    只写包里**确实存在**的图片：缺图不阻断导入（派单要求「图片缺失可跳过图片后导入」），
    该行的文字内容照常入库，界面按 `warnings` 显示「缺图」。
    """
    if uploads is None:
        return 0, []
    files = []
    count = 0
    seq = 0
    for image in row.get("images") or []:
        if not image.get("present"):
            continue
        path = normalize_rel_path(image.get("path") or "")
        ext = os.path.splitext(path.lower())[1]
        if ext not in ALLOWED_IMAGE_EXTS:
            continue
        blob = package.read_image(path)
        digest = hashlib.sha256(blob).hexdigest()
        seq += 1
        name = "answer-%s.%s" % (uuid.uuid4().hex, ext.lstrip("."))
        uploads.save(name, blob)
        files.append(name)
        width, height = image_size(blob)
        conn.execute(
            "INSERT INTO answer_images(class_id, student_id, answer_id, seq, file_path, mime,"
            " width, height, bytes, sha256, source, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, row["student_id"], answer_id, seq, name,
             MIME_BY_EXT.get(ext, ""), width, height, len(blob), digest, "zip", "", now),
        )
        count += 1
    return count, files


def image_size(blob):
    """从 PNG/JPEG 头里读尺寸，读不出返回 `(0, 0)`。

    为什么不 import PIL：app 层要能在只有标准库的解释器（C:\\Python314）里跑，
    而且「宽高」只是给界面排版用的参考值，读不出就记 0，不猜测、不报错。
    """
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 24:
        return 0, 0
    data = bytes(blob)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:2] == b"\xff\xd8":
        index = 2
        length = len(data)
        while index + 9 < length:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            segment = struct.unpack(">H", data[index + 2:index + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                          0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return int(width), int(height)
            index += 2 + segment
    return 0, 0


# ---------------------------------------------------------------------------
# 五、标准模板（老师下载后填内容再打包）
# ---------------------------------------------------------------------------
_ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def template_csv_text():
    """模板 CSV：R1 的 12 列 + R2 的「图片路径」列（示例全部是虚构数据）。"""
    header = ",".join(label for label, _ in answers_mod.HEADERS) + ",图片路径"
    rows = [
        "A-001,示例学生甲,XY001,1,解方程 3(x-2)=2x+5,一元一次方程,"
        "3x-6=2x+5；3x-2x=5+6；x=11,3x-6=2x+5；x=11,9 月第 1 次作业,2026-09-02,否,,"
        "images/示例-学生甲-第1题-1.png;images/示例-学生甲-第1题-2.png",
        "A-002,示例学生乙,,1,解方程 3(x-2)=2x+5,一元一次方程,3x-6=2x+5；3x-2x=5+6；x=11,,"
        "9 月第 1 次作业,2026-09-02,是,没交作业,",
        "A-003,示例学生丙,XY003,2,解方程 2(x+1)=6,一元一次方程,2x+2=6；x=2,,"
        "9 月第 1 次作业,2026-09-02,否,,images/示例-学生丙-第2题-1.png",
    ]
    return header + "\r\n" + "\r\n".join(rows) + "\r\n"


TEMPLATE_DATE = (2026, 9, 1, 0, 0, 0)


def _write_entry(archive, name, blob):
    """以**固定时间戳 + 不压缩**写条目：任何解释器重建都得到逐字节相同的包。

    为什么要较真：模板与验收包都是进 git 的二进制夹具。
    1. `zipfile.writestr(name, ...)` 会把「当前时间」写进条目头 → 每次重建字节都不同；
    2. 用 DEFLATE 时结果取决于解释器内置 zlib 版本（本机 3.14 与 3.12 实测差几个字节）
       → 换解释器重建就 diff，「夹具与代码逐字节相等」这条断言会在 CI 上莫名变红。
    所以模板定位为结构示例：固定时间戳 + 原样存储（几 KB 的小文件，不压缩也几乎不涨），
    换来可逐字节复现。老师自己打的包仍可任意压缩，读取侧两种都支持（`ZIP_DEFLATED` 的
    验收包由测试现场构造，正好覆盖另一条分支）。
    """
    info = zipfile.ZipInfo(name, date_time=TEMPLATE_DATE)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    archive.writestr(info, blob)


def template_zip():
    """返回标准模板 ZIP 的字节：`answers.csv` + `images/` 下三张 1×1 示例图。

    模板只放虚构内容，且刻意包含「两张图的题」「缺测的题」「仅照片的题」三种样例，
    老师照着替换即可；文件名固定、条目时间戳固定、不压缩，因此可逐字节复现。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        _write_entry(archive, CSV_NAME, template_csv_text().encode("utf-8-sig"))
        for name in ("images/示例-学生甲-第1题-1.png", "images/示例-学生甲-第1题-2.png",
                     "images/示例-学生丙-第2题-1.png"):
            _write_entry(archive, name, _ONE_PIXEL_PNG)
    return buffer.getvalue()


def template_name():
    return "作答导入模板.zip"


# ---------------------------------------------------------------------------
# 六、给界面用的中文文案（统一在这里，UI 不自己写死）
# ---------------------------------------------------------------------------
def category_text(category):
    return CATEGORY_LABELS.get(category, "未知")


def status_text(status):
    return {"ok": "可导入", "duplicate": "重复", "conflict": "冲突",
            "overwritten": "已覆盖", "error": "需处理"}.get(status, status)


__all__ = [
    "ZipImportError", "Package", "read_package", "preview", "commit", "parse",
    "template_zip", "template_csv_text", "template_name", "split_image_paths",
    "normalize_rel_path", "image_size", "category_text", "status_text",
    "MAX_ZIP_BYTES", "MAX_UNCOMPRESSED_BYTES", "MAX_FILES", "MAX_IMAGE_BYTES",
    "ALLOWED_IMAGE_EXTS", "IMAGE_HEADERS", "CATEGORIES", "CATEGORY_LABELS",
    "ZIP_ERROR_CODES", "CSV_NAME", "IMAGES_DIR",
    "CATEGORY_VALID", "CATEGORY_MISSING", "CATEGORY_DUPLICATE", "CATEGORY_CONFLICT",
    "CATEGORY_UNMATCHED", "CATEGORY_FILE_ERROR",
]
