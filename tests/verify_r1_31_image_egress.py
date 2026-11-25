# -*- coding: utf-8 -*-
"""R1-T31：授权后错题照片分析必须真的能发出去（且未授权时一次都不读图）。

跑法：C:\\Python314\\python.exe tests\\verify_r1_31_image_egress.py

背景（t30 取证）：`_mock_job` 只给 image_file/宽高、没有 image_bytes，于是「授权后可外发」
只对文本链路成立，错题照片分析永远在 vision 的 payload 校验处被拦。修法：为已授权路径单独
组装 `_authorized_job`（读 uploads 取 blob），mock 路径保持「不读照片字节」。

本套件四组：
1. 开关关 → 403 `ai_isolation`，且**一次 uploads 读取都没有发生**；
2. 开关开 → 真发：桩收到 1 次请求，payload 含 image_bytes/image_mime，无身份字段；审计只记字段名与字节数；
3. 图片缺失 → 明确错误（文案取自 vision）且不发送；
4. 图片超过 12MB → 明确错误且不发送。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import analysis, api, vision  # noqa: E402
from app import db as db_mod  # noqa: E402
from app.api import ApiError  # noqa: E402
from app.db import Database, ensure_answer_check_schema  # noqa: E402
from app.uploads import UploadStore  # noqa: E402
from desktop.service import DesktopService  # noqa: E402
from verify_r1_07_isolation import png_bytes  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "r1-31-image-egress")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-31-image-egress.txt")
STUB = {"calls": [], "lock": threading.Lock()}
LINES = []
RESULTS = {"pass": 0, "fail": 0}
IDENTITY_TOKENS = ("学生甲", "XY001", "隔离验证班", "answer_id", "student_id", "account")


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s  %s" % (name, detail))
    return bool(ok)


def section(title):
    log("")
    log("== %s ==" % title)


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with STUB["lock"]:
            STUB["calls"].append({"path": self.path, "body": raw.decode("utf-8", "ignore")})
        suggestion = {
            "question_text": "（桩回的题目）", "solution_text": "（桩回的步骤）",
            "knowledge_points": ["一元一次方程"], "error_type": "符号错误",
            "observed_errors": ["第 2 行移项后没有变号"], "possible_causes": ["计算习惯"],
            "advice_text": "（桩回的建议）", "insufficient": False, "insufficient_reason": "",
            "likely_correct": False,
        }
        data = json.dumps({
            "choices": [{"message": {"content": json.dumps(suggestion, ensure_ascii=False)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def stub_calls():
    with STUB["lock"]:
        return list(STUB["calls"])


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-T31 授权后的图片外发（mock 永不读图 · 未授权零读取）")
    for line in environment_lines():
        log(line)

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_root = "http://127.0.0.1:%d" % stub.server_address[1]
    try:
        data_dir = os.path.join(WORK, "data")
        os.makedirs(data_dir)
        db_file = os.path.join(data_dir, "app.db")
        Database(db_file)
        ensure_answer_check_schema(db_file)
        svc = DesktopService(data_dir)
        svc.register("oc-31", "pw-r1-31")
        cid = svc.create_class("隔离验证班")["id"]
        sid = svc.create_student(cid, "学生甲", "XY001")["id"]
        db = svc.db
        uploads = UploadStore(os.path.join(data_dir, "uploads"))

        # 建一条带照片的错题（照片真的入库）
        photo_dir = os.path.join(WORK, "题图.png")
        with open(photo_dir, "wb") as fh:
            fh.write(png_bytes(900, 600))
        import base64
        with open(photo_dir, "rb") as fh:
            blob = fh.read()
        created = api.api_create_mistake(api.FakeCtx if False else __import__("verify_r1_07_isolation", fromlist=["FakeCtx"]).FakeCtx(
            db, svc.user, uploads, body={
                "class_id": cid, "student_id": sid, "source_type": "作业",
                "source_detail": "课本 P42", "note": "", "occurred_on": "2026-11-20",
                "question_text": "解方程 3(x-2)=2x+5", "solution_text": "3x-6=2x+5；x=11",
                "image": {"data_url": "data:image/png;base64,"
                                    + base64.b64encode(blob).decode("ascii"),
                          "width": 900, "height": 600},
                "local_only": True,
            }))
        mid = created["id"]
        row = db.query_one("SELECT * FROM mistakes WHERE id=?", (mid,))
        photo_path = uploads.path(row["image_file"])
        check("0.1 夹具就绪：错题有照片且文件真的存在",
              bool(row["image_file"]) and os.path.exists(photo_path),
              (row["image_file"], os.path.exists(photo_path)))

        svc.save_ai_settings("custom", stub_root + "/v1", "stub-model", "sk-r1-31",
                            service_name="本机桩")

        # ---------------------------------------------------------------
        section("一、开关关：403 ai_isolation，且一次 uploads 读取都没有发生")
        # ---------------------------------------------------------------
        reads = {"count": 0}
        original_path = UploadStore.path

        def counting_path(self, name):
            reads["count"] += 1
            return original_path(self, name)

        UploadStore.path = counting_path
        try:
            mark = len(stub_calls())
            error = ""
            try:
                svc.preview_analysis(mid, consent=True)
            except ApiError as exc:
                error = "%s %s" % (getattr(exc, "status", ""), getattr(exc, "code", ""))
            check("1.1 未开启开关 → 403 ai_isolation（原口径不变）",
                  error.strip() == "403 %s" % analysis.INDIVIDUAL_FLOW_CODE, error)
            check("1.2 未授权时一次 uploads 读取都没有发生（不是靠开关布尔假放行）",
                  reads["count"] == 0, reads["count"])
            check("1.3 未授权时桩一次请求都没收到", len(stub_calls()) == mark,
                  len(stub_calls()) - mark)
        finally:
            UploadStore.path = original_path

        # ---------------------------------------------------------------
        section("二、开关开：真发一次（payload 带图片、无身份字段、审计只记字段名）")
        # ---------------------------------------------------------------
        svc.set_ai_individual(True)
        mark = len(stub_calls())
        reads["count"] = 0
        UploadStore.path = counting_path
        try:
            result = svc.preview_analysis(mid, consent=True)
        finally:
            UploadStore.path = original_path
        sent = len(stub_calls()) - mark
        check("2.1 授权后错题照片分析真的发出去了（桩收到 1 次）", sent == 1, sent)
        check("2.2 不再返回 error_code=payload（缺口已修）",
              not (result or {}).get("error_code") == "payload", (result or {}).get("error_code"))
        check("2.3 授权路径确实读了照片（uploads 至少被读一次）", reads["count"] >= 1, reads["count"])
        body = stub_calls()[-1]["body"] if sent else ""
        check("2.4 payload 里含图片数据（image_url/data 或 base64 片段）",
              ("image_url" in body) and (len(body) > 5000), len(body))
        leaked = [token for token in IDENTITY_TOKENS if token in body]
        check("2.5 payload 不含身份字段（姓名/学号/班级名/answer_id/account）",
              leaked == [], leaked)
        audit = db_mod.ai_egress_audit_rows(db)
        check("2.6 审计写了 1 条，且只记字段名与字节数（不含图片内容）",
              len(audit) == 1 and audit[0]["flow"] == "错题照片分析建议"
              and any(name.startswith(("messages", "content", "image")) for name in audit[0]["payload_fields"])
              and int(audit[0]["payload_bytes"] or 0) > 5000,
              {k: audit[0][k] for k in ("flow", "payload_fields", "payload_bytes")} if audit else None)
        check("2.7 审计里没有任何身份取值（姓名/学号/answer_id）",
              all(token not in json.dumps(audit, ensure_ascii=False) for token in IDENTITY_TOKENS),
              json.dumps(audit, ensure_ascii=False)[:120])

        # ---------------------------------------------------------------
        section("三、图片缺失：明确错误、不发送（沿用 vision 文案）")
        # ---------------------------------------------------------------
        with db.tx() as conn:
            conn.execute("UPDATE mistakes SET image_file='' WHERE id=?", (mid,))
        mark = len(stub_calls())
        error = ""
        try:
            svc.preview_analysis(mid, consent=True)
        except ApiError as exc:
            error = "%s %s %s" % (getattr(exc, "status", ""), getattr(exc, "code", ""),
                                  getattr(exc, "message", ""))
        check("3.1 图片缺失 → 400 payload + 「没有可发送的图片数据」",
              "400 payload" in error and "没有可发送的图片数据" in error, error[:120])
        check("3.2 图片缺失时一次都没发送", len(stub_calls()) == mark, len(stub_calls()) - mark)

        with db.tx() as conn:
            conn.execute("UPDATE mistakes SET image_file=? WHERE id=?", (row["image_file"], mid))

        # ---------------------------------------------------------------
        section("四、图片超过 12MB：明确错误、不发送")
        # ---------------------------------------------------------------
        big = os.path.join(data_dir, "uploads", "big.png")
        with open(big, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * (vision.MAX_PAYLOAD_IMAGE_BYTES + 1024))
        with db.tx() as conn:
            conn.execute("UPDATE mistakes SET image_file='big.png' WHERE id=?", (mid,))
        mark = len(stub_calls())
        error = ""
        try:
            svc.preview_analysis(mid, consent=True)
        except ApiError as exc:
            error = "%s %s %s" % (getattr(exc, "status", ""), getattr(exc, "code", ""),
                                  getattr(exc, "message", ""))
        check("4.1 超过 12MB → 400 payload + 「字节上限」文案",
              "400 payload" in error and "字节上限" in error, error[:120])
        check("4.2 超限时一次都没发送", len(stub_calls()) == mark, len(stub_calls()) - mark)

        # ---------------------------------------------------------------
        section("五、mock 通道语义不变：不读照片字节、仍 409 mock")
        # ---------------------------------------------------------------
        svc.save_ai_settings("mock", "", "", None, service_name="")
        reads["count"] = 0
        UploadStore.path = counting_path
        try:
            result = svc.preview_analysis(mid, consent=True)
        finally:
            UploadStore.path = original_path
        check("5.1 mock 通道仍是「当前是模拟分析」口径（409 mock 或被包成 ok=false）",
              (result or {}).get("error_code") in ("mock", None)
              or "模拟" in str((result or {}).get("error") or ""), result)
        check("5.2 mock 通道下一次 uploads 读取都没有发生（_mock_job 不读图）",
              reads["count"] == 0, reads["count"])
    finally:
        stub.shutdown()
        stub.server_close()

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                    RESULTS["pass"], RESULTS["fail"]))
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LINES) + "\n")
    shutil.rmtree(WORK, ignore_errors=True)
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
