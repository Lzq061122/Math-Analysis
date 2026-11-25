"""1.4.6 原有功能的回归测试（1.5.0）：错题、照片、模拟 AI 核对、统计、备份恢复。

与 CSV 导入无关的旧功能都要还能用；全部使用临时目录与自制图片。
运行：.venv\\Scripts\\python.exe tests\\verify_core_150.py
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image

from app import backup as B
from app.db import Database
from desktop.service import DesktopService

LOG_PATH = os.path.join(ROOT, "docs", "verify-core-150.txt")
TOTAL = [0]
FAILED = [0]
LINES = []


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if not ok:
        FAILED[0] += 1
    log(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  ← %r" % (detail,)))


def section(title):
    log("")
    log("== %s ==" % title)


def make_photo(path):
    image = Image.new("RGB", (900, 600), "white")
    for x in range(200, 700):
        image.putpixel((x, 300), (20, 20, 20))
    image.save(path)


def main():
    from logmeta import environment_lines

    for line in environment_lines():
        log(line)
    log("")
    work = tempfile.mkdtemp(prefix="core-150-")
    data = os.path.join(work, "data")
    os.makedirs(data)
    Database(os.path.join(data, "app.db"))
    svc = DesktopService(data)
    svc.register("teacher", "pw123456")
    svc.login("teacher", "pw123456")

    section("一、班级、学生与错题")
    cid = svc.create_class("初三 3 班")["id"]
    sid = svc.create_student(cid, "李四", "SCH9")["id"]
    photo = os.path.join(work, "题图.png")
    make_photo(photo)
    fields = {
        "source_type": "作业",
        "occurred_on": "2026-11-10",
        "question_text": "解方程 2x+1=5",
        "solution_text": "2x=4, x=2",
    }
    created = svc.create_mistake(cid, sid, fields, photo, 900, 600)
    check("能建错题", created and created.get("id"), created)
    mid = created["id"]
    photo_payload = svc.mistake_photo(mid)
    check("能取回错题照片", bool(photo_payload[0]) and photo_payload[1].startswith("image/"), photo_payload[1])
    stored = svc.get_mistake(mid)
    check("错题内容保存正确", stored["question_text"] == "解方程 2x+1=5", stored.get("question_text"))
    check("照片尺寸已记录", stored.get("image_width") == 900 and stored.get("image_height") == 600, stored)

    section("二、模拟 AI 分析与老师核对")
    preview = svc.preview_analysis(mid, consent=True)
    check("模拟分析返回建议", bool(preview.get("suggestion", {}).get("knowledge_points")), preview.get("suggestion"))
    check("模拟结果标明不是真实 AI", "模拟" in str(preview.get("suggestion", {}).get("basis") or ""), preview.get("suggestion"))
    applied = svc.apply_analysis(mid, preview["preview_id"])
    check("采用建议后待核对", applied.get("status") == "pending", applied.get("status"))
    confirmed = svc.confirm_mistake(mid, applied["base_fingerprint"])
    check("老师确认后状态为已确认", confirmed.get("status") == "confirmed", confirmed.get("status"))

    section("三、统计")
    stats = svc.class_stats(cid)
    check("班级统计含 1 条错题", stats["summary"]["confirmed"] == 1, stats.get("summary"))
    student_stats = svc.student_stats(sid)
    check("学生统计含 1 条错题", student_stats["summary"]["confirmed"] == 1, student_stats.get("summary"))

    section("四、备份与恢复")
    user_id = sqlite3.connect(os.path.join(data, "app.db")).execute("SELECT id FROM users WHERE username='teacher'").fetchone()[0]
    zip_path = os.path.join(work, "backup.zip")
    B.create_backup(data, zip_path, user_id)
    staging = os.path.join(work, "staged")
    manifest, _relation = B.inspect_backup(zip_path, staging)
    check("备份包含错题", manifest["counts"].get("mistakes") == 1, manifest["counts"])
    photos = [item for item in manifest.get("uploads") or []]
    check("备份包含照片", len(photos) == 1, photos)
    result = B.apply_staged(staging, data, user_id, "1.5.0", mode="restore")
    check("恢复成功", result["counts"].get("mistakes") == 1, result["counts"])
    restored = svc.list_mistakes(cid, sid)
    check("恢复后错题还在且带照片", len(restored) == 1 and restored[0]["has_image"], restored)
    payload = svc.mistake_photo(restored[0]["id"])
    check("恢复后照片仍可读", bool(payload[0]) and payload[1].startswith("image/"), payload[1])
    uploads = os.listdir(os.path.join(data, "uploads"))
    check("恢复后照片文件只有一份", len(uploads) == 1, uploads)

    section("五、旧格式备份")
    old_zip = os.path.join(work, "format3.zip")
    shutil.copyfile(zip_path, old_zip)
    rewrite_format(old_zip, 3)
    staging3 = os.path.join(work, "staged3")
    manifest3, _rel3 = B.inspect_backup(old_zip, staging3)
    check("能读 3 号格式备份", manifest3.get("format") == 3, manifest3.get("format"))
    result3 = B.apply_staged(staging3, data, user_id, "1.5.0", mode="restore")
    check("3 号格式可以恢复", result3["counts"].get("mistakes") == 1, result3["counts"])

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    code = 1 if FAILED[0] else 0
    log("退出码：%d" % code)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    return code


def rewrite_format(zip_path, fmt):
    tmp = zip_path + ".tmp"
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.json":
                manifest = json.loads(data.decode("utf-8"))
                manifest["format"] = fmt
                data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            dst.writestr(info, data)
    os.replace(tmp, zip_path)


if __name__ == "__main__":
    sys.exit(main())
