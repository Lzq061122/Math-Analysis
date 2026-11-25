"""本地 HTTP 服务：静态页面 + JSON API + 图片访问。

启动：python src\server.py [--port 8765] [--host 0.0.0.0] [--no-demo]
- 默认监听 0.0.0.0，方便手机在同一 WiFi 下打开拍题页；只在本机使用时可用 --host 127.0.0.1。
- 数据保存在 data/ 目录：app.db 与 uploads/。
"""

import argparse
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(SRC_DIR)
sys.path.insert(0, SRC_DIR)

from app import api, auth, backup, vision  # noqa: E402
from app.db import (  # noqa: E402
    Database, MigrationError, add_student_active, ensure_answer_check_schema, ensure_answers_schema,
    ensure_followup_schema, ensure_tutor_schema, now_iso,
)
from app.uploads import UploadStore  # noqa: E402
from app.version import VERSION  # noqa: E402

# 与 launcher.py 的退出码约定保持一致：75 恢复、76 锁占用、77 数据目录结构升级失败
MIGRATION_EXIT = 77

# 打包后源码与网页资源解压在 PyInstaller 的临时目录里
RESOURCE_DIR = getattr(sys, "_MEIPASS", SRC_DIR)
WEB_DIR = os.path.join(RESOURCE_DIR, "web")
COOKIE_NAME = "teacher_session"
MAX_BODY = 24 * 1024 * 1024
MAX_RESTORE_BODY = backup.MAX_RESTORE_BYTES

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Ctx:
    def __init__(self, server, db, uploads, user, token, body, query, params, client_ip):
        self.server = server
        self.db = db
        self.uploads = uploads
        self.user = user
        self.token = token
        self.body = body
        self.query = query
        self.params = params
        self.client_ip = client_ip


class Handler(BaseHTTPRequestHandler):
    server_version = "MistakeStudio/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础设施 ---------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload, extra_headers=None):
        self._send(status, payload, extra_headers=extra_headers)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise api.ApiError("请求体过大", 413)
        raw = self.rfile.read(length)
        self._body_read = length
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise api.ApiError("请求必须使用 application/json", 415)
        try:
            return json.loads(raw.decode("utf-8")) or {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise api.ApiError("请求体不是合法 JSON", 400)

    def _read_raw_body(self, dest_path):
        """把原始请求体（zip 备份）流式写入 dest_path（本任务独立的暂存目录）。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise api.ApiError("请求体为空", 400)
        if length > MAX_RESTORE_BODY:
            raise api.ApiError("备份文件过大，无法上传", 413)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/zip", "application/octet-stream", "application/x-zip-compressed"):
            raise api.ApiError("请上传 zip 格式的备份文件", 415)
        remaining = length
        with open(dest_path, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise api.ApiError("上传中断，请重试", 400)
                fh.write(chunk)
                remaining -= len(chunk)
        self._body_read = length
        return dest_path

    def _drain_body(self):
        """出错时把没读完的请求体读掉，避免同一连接上的下一个请求错位。"""
        length = int(self.headers.get("Content-Length") or 0)
        remaining = length - getattr(self, "_body_read", 0)
        if remaining <= 0:
            return
        if remaining > MAX_BODY:
            self.close_connection = True
            return
        try:
            self.rfile.read(remaining)
        except OSError:
            self.close_connection = True
        self._body_read = length

    def _cookies(self):
        raw = self.headers.get("Cookie") or ""
        jar = {}
        for part in raw.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                jar[key.strip()] = value.strip()
        return jar

    def _client_ip(self):
        return self.client_address[0]

    # -- 请求入口 ---------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        self._body_read = 0
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                self._handle_api(method, path, query)
            elif method == "GET":
                self._serve_static(path)
            else:
                raise api.ApiError("接口不存在", 404)
        except api.ApiError as exc:
            self._drain_body()
            payload = {"error": exc.message}
            if getattr(exc, "code", None):
                payload["code"] = exc.code
            if getattr(exc, "payload", None):
                payload.update(exc.payload)
            self._json(exc.status, payload)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "服务器内部错误"})

    # -- API --------------------------------------------------------------
    def _handle_api(self, method, path, query):
        handler, params, need_auth = api.resolve(method, path)
        db = self.server.db
        token = self._cookies().get(COOKIE_NAME)
        user = auth.lookup_session(db, token) if token else None

        if need_auth and not user:
            raise api.ApiError("请先登录", 401)

        # 数据状态不确定时阻止一切修改（关闭软件、登录与退出登录除外），重开软件也不会解除。
        # 登录只写临时会话，放开登录让老师仍能进入软件查看数据与红色提示。
        if (
            method in ("POST", "PATCH", "DELETE")
            and path not in ("/api/shutdown", "/api/auth/logout", "/api/auth/login")
            and backup.is_restore_blocked(self.server.data_dir)
        ):
            raise api.ApiError(
                "数据状态待人工处理：上次恢复没有完整结束，已暂停所有修改操作。"
                "请保留整个软件文件夹（包括 data/backups/ 里的自动备份），联系技术人员处理。",
                503,
                code="restore_blocked",
            )

        # 恢复/导入上传：在接收文件前完成占用检查，每个任务使用独立目录。
        task_dir = None
        if method in ("POST", "PATCH", "DELETE"):
            if path in ("/api/restore", "/api/import"):
                if backup.pending_problem(self.server.data_dir):
                    raise api.ApiError("已有一个恢复任务正在等待处理，请等它完成后再试", 409, code="restore_busy")
                try:
                    task_dir = backup.claim_upload(self.server.data_dir)
                except backup.BackupError as exc:
                    raise api.ApiError(str(exc), 409, code="upload_busy")
                try:
                    body = {
                        "upload_path": self._read_raw_body(os.path.join(task_dir, "upload.zip")),
                        "task_dir": task_dir,
                    }
                except BaseException:
                    backup.release_upload(self.server.data_dir, task_dir)
                    raise
            else:
                body = self._read_body()
        else:
            body = {}

        ctx = Ctx(
            server=self.server,
            db=db,
            uploads=self.server.uploads,
            user=user,
            token=token,
            body=body,
            query=query,
            params=params,
            client_ip=self._client_ip(),
        )
        result = handler(ctx)
        headers = {"Cache-Control": "no-store"}

        if path in ("/api/auth/login", "/api/auth/register") and isinstance(result, dict) and result.get("token"):
            token_value = result.pop("token")
            headers["Set-Cookie"] = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
                COOKIE_NAME,
                token_value,
                auth.SESSION_SECONDS,
            )
        if path == "/api/auth/password" and isinstance(result, dict) and result.get("token"):
            token_value = result.pop("token")
            headers["Set-Cookie"] = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
                COOKIE_NAME,
                token_value,
                auth.SESSION_SECONDS,
            )
        if path == "/api/auth/logout":
            headers["Set-Cookie"] = "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE_NAME

        if isinstance(result, dict) and result.get("__raw__"):
            raw_headers = {"Cache-Control": "no-store"}
            download_name = result.get("download_name")
            if download_name:
                raw_headers["Content-Disposition"] = (
                    "attachment; filename=\"backup.zip\"; filename*=UTF-8''%s" % quote(download_name)
                )
            self._send(200, result["body"], content_type=result["content_type"], extra_headers=raw_headers)
            return
        self._json(200, result, headers)

    # -- 静态文件 ---------------------------------------------------------
    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        target = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not os.path.abspath(target).startswith(os.path.abspath(WEB_DIR)):
            raise api.ApiError("路径非法", 400)
        if not os.path.isfile(target):
            # 单页应用：未知路径回落到首页
            target = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(target):
                raise api.ApiError("页面不存在", 404)
        ext = os.path.splitext(target)[1].lower()
        ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            body = fh.read()
        self._send(
            200,
            body,
            content_type=ctype,
            extra_headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": (
                    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline';"
                    " script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                ),
            },
        )


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db, uploads, data_dir=None, portable=False):
        self.db = db
        self.uploads = uploads
        self.data_dir = data_dir or os.path.dirname(db.path)
        self.portable = portable
        self.exit_code = 0
        super().__init__(addr, Handler)

    def handle_error(self, request, client_address):
        """浏览器提前断开连接时不打印整段异常栈。"""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def lan_addresses(port):
    urls = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip in [u for _, u in urls]:
                continue
            urls.append((info[4][0], "http://%s:%d" % (ip, port)))
    except OSError:
        pass
    return urls


def make_server(host, port, data_dir, use_demo=False, portable=False):
    """创建服务实例；调用方负责 serve_forever。"""
    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    backup.cleanup_stale_tasks(data_dir)
    db_file = os.path.join(data_dir, "app.db")
    if os.path.exists(db_file):
        # 1.3.1 起学生有了「停用状态」active 列；打开 1.3.0 的库时先备份再补这一列，
        # 补失败（MigrationError）就冒泡到 run_serve，由它给出符合真实数据状态的提示。
        add_student_active(db_file)
        # 1.5.0 起有作答记录表与学生 school 编号列；同样先备份再补，失败时原库不动。
        ensure_answers_schema(db_file)
        # 1.7.0 起有个人辅导建议表；同样先备份再补，失败时原库不动。
        ensure_tutor_schema(db_file)
        # 1.8.0 起有追问记录与候选更新表。
        ensure_followup_schema(db_file)
        # 本轮起有作答核对与追问原话表：同样先备份再补，失败时原库不动。
        ensure_answer_check_schema(db_file)
    db = Database(db_file)
    uploads = UploadStore(os.path.join(data_dir, "uploads"))
    if use_demo:
        from app import demo

        demo.ensure_seed(db, uploads)
    server = AppServer((host, port), db, uploads, data_dir=data_dir, portable=portable)
    server.use_demo = use_demo
    return server


def banner(server, use_demo):
    port = server.server_address[1]
    cfg = vision.describe_config()
    if cfg["mode"] == vision.MODE_REAL:
        mode_line = "AI 模式：真实 AI 分析（OpenCode Console / %s）" % cfg["model"]
    else:
        mode_line = "AI 模式：模拟分析（%s）" % cfg["reason"]

    print("=" * 62)
    print(" 数学错题学情分析 %s  %s" % (VERSION, mode_line))
    print("=" * 62)
    print(" 本机访问：http://127.0.0.1:%d" % port)
    if server.server_address[0] in ("0.0.0.0", ""):
        for _, url in lan_addresses(port):
            print(" 手机访问：%s   （需同一局域网）" % url)
    print(" 数据目录：%s" % server.data_dir)
    if cfg["mode"] == vision.MODE_REAL:
        print(" 真实 AI：每张照片需老师勾选同意后才发送；重试会再次调用服务，是否计费以所配置服务为准")
    else:
        print(" 启用真实 AI：本版本默认关闭；需显式设置 MISTAKE_AI_MODE=real 并配置 MISTAKE_AI_API_KEY（默认地址/模型为 OpenCode Console）")
    if use_demo:
        print(" 演示账号：demo（密码见 README 的「演示账号」一节，可在“设置”里改密码）")
    print(" Ctrl+C 停止服务")
    print("=" * 62, flush=True)


def run_serve(argv=None):
    parser = argparse.ArgumentParser(description="数学错题学情分析（教师端）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0 便于手机访问")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=os.path.join(BASE_DIR, "data"))
    parser.add_argument("--no-demo", action="store_true", help="不创建演示账号与演示数据")
    parser.add_argument("--portable", action="store_true", help="便携版模式：允许一键恢复与关闭软件")
    args = parser.parse_args(argv)

    try:
        server = make_server(args.host, args.port, args.data_dir, use_demo=not args.no_demo, portable=args.portable)
    except MigrationError as exc:
        print("=" * 62)
        print(" 无法使用这个数据目录")
        print("=" * 62)
        print(" %s" % exc)
        print(" 软件没有改动里面的数据，请按上面的说明处理后重试。")
        print("=" * 62, flush=True)
        return MIGRATION_EXIT
    banner(server, use_demo=not args.no_demo)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return server.exit_code


def main(argv=None):
    raise SystemExit(run_serve(argv))


if __name__ == "__main__":
    main()
