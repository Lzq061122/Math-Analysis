"""1.5.0 作答核对网页：浏览器端到端（真实 Edge headless + DevTools 协议，1366×768）。

为什么不用 iframe 夹具：A1–A6 的验收标准里写着「单击直达」「暂时跳过不发请求」「保存失败留页」，
这些只有在真实浏览器里点真实按钮、看真实网络面板才算证据。本脚本自己拉起
`src/server.py` 的服务（127.0.0.1:8899、数据目录 tests/.tmp-e2e-web），再用 Edge 的
headless 模式通过 DevTools 协议（CDP）驱动页面：鼠标事件按元素中心点派发，截图存
docs/screenshots/，DOM 断言值原样写进 docs/verify-answer-check-ui.txt。

端口约定（契约 §6）：8899 归 verify 专用；CDP 用系统分配的空闲端口。
"""

import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

import server as server_mod  # noqa: E402

from app import demo_answers as _demo_answers  # noqa: E402  （sys.path 已含 src）

WORK = os.path.join(ROOT, "tests", ".tmp-e2e-web")
SHOTS = os.path.join(ROOT, "docs", "screenshots")
LOG_PATH = os.path.join(ROOT, "docs", "verify-answer-check-ui.txt")
PORT = 8899
DB_PATH = os.path.join(WORK, "app.db")
VIEWPORT = (1366, 768)
CLASS_NAME = "初二(3)班"          # demo.ensure_seed 建好的演示班级
# R1-01：合成演示批次名改由 db/demo_answers 决定（旧值「9月第2次作业」会与真实作业撞名，
# 撞名后演示数据会被算进真实作业的应交数）。这里直接取模块常量，避免测试与实现各写一份。
ASSIGNMENT_NAME = _demo_answers.DEMO_BATCH

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

RESULTS = {"pass": 0, "fail": 0, "hold": 0, "gap": 0}
LOG_LINES = []


# --------------------------------------------------------------------------
# 日志与断言
# --------------------------------------------------------------------------
def log(text=""):
    print(text)
    LOG_LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s %s" % (name, detail))
    return bool(ok)


def hold(name, reason):
    RESULTS["hold"] += 1
    log("未验证 %s —— %s" % (name, reason))


def gap(name, detail):
    """已知缺口：如实记「未通过」，但不算本次自动化断言的失败（修复任务在 T10）。"""
    RESULTS["gap"] += 1
    log("未通过（缺口）%s —— %s" % (name, detail))


def section(title):
    log("")
    log("== %s ==" % title)


def png_size(raw):
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def kill_stale_browsers():
    """上一轮异常退出时 Edge 会占住 profile 目录，先把用本工作目录启动的实例清掉。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\""
        " | Where-Object { $_.CommandLine -like '*tmp-e2e-web*' }"
        " | Select-Object -ExpandProperty ProcessId"
    )
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, timeout=120)
    except Exception:
        return
    for token in proc.stdout.decode("utf-8", "replace").split():
        if token.strip().isdigit():
            subprocess.run(["taskkill", "/F", "/T", "/PID", token.strip()],
                           capture_output=True, timeout=60)


def reset_work():
    kill_stale_browsers()
    for _attempt in range(12):
        shutil.rmtree(WORK, ignore_errors=True)
        if not os.path.exists(WORK):
            os.makedirs(WORK)
            return
        time.sleep(0.7)
    raise RuntimeError("临时目录删不掉（可能有残留浏览器进程占用）：%s" % WORK)


def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 环回 HTTP（造数与核对数据库结果用）
# --------------------------------------------------------------------------
class HTTP:
    def __init__(self, base):
        self.base = base
        self.token = None
        self.opener = urllib.request.build_opener()

    def call(self, method, path, body=None):
        data = None
        headers = {}
        if self.token:
            headers["Cookie"] = "teacher_session=" + self.token
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read()
                # 登录接口把令牌只放进 Set-Cookie（响应体里已 pop），这里取出来给浏览器注入。
                cookie = resp.headers.get("Set-Cookie") or ""
                if "teacher_session=" in cookie:
                    self.token = cookie.split("teacher_session=", 1)[1].split(";", 1)[0]
                return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                payload = {"raw": raw.decode("utf-8", "replace")}
            return exc.code, payload

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, body if body is not None else {})


# --------------------------------------------------------------------------
# 极简 WebSocket 客户端（CDP 只需要文本帧）
# --------------------------------------------------------------------------
class WS:
    def __init__(self, host, port, path, timeout=30):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手没有响应")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        self.buf = bytearray(rest)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手被拒绝：%r" % head[:160])

    def _need(self, size):
        while len(self.buf) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接被关闭")
            self.buf += chunk

    def _frame(self):
        self._need(2)
        b0, b1 = self.buf[0], self.buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            self._need(4)
            length = struct.unpack(">H", bytes(self.buf[2:4]))[0]
            offset = 4
        elif length == 127:
            self._need(10)
            length = struct.unpack(">Q", bytes(self.buf[2:10]))[0]
            offset = 10
        mask = None
        if masked:
            self._need(offset + 4)
            mask = bytes(self.buf[offset:offset + 4])
            offset += 4
        self._need(offset + length)
        payload = bytes(self.buf[offset:offset + length])
        del self.buf[:offset + length]
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _pong(self, payload):
        header = bytearray([0x8A])
        header.append(0x80 | len(payload))
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        data = text.encode("utf-8")
        header = bytearray([0x81])
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout):
        self.sock.settimeout(max(0.05, timeout))
        fin, opcode, payload = self._frame()
        if opcode == 9:
            self._pong(payload)
            return self.recv(timeout)
        if opcode == 8:
            raise RuntimeError("WebSocket 被对端关闭")
        if opcode in (1, 2):
            if fin:
                return payload.decode("utf-8")
            data = bytearray(payload)
            while True:
                fin2, op2, part = self._frame()
                if op2 == 9:
                    self._pong(part)
                    continue
                data += part
                if fin2:
                    return bytes(data).decode("utf-8")
        return self.recv(timeout)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# CDP 会话
# --------------------------------------------------------------------------
class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.counter = 0
        self.replies = {}
        self.requests = []
        self.console_errors = []
        self.exceptions = []
        self.fetch_mode = None
        self.fetch_urls = []

    # -- 基础 ------------------------------------------------------------
    def _send(self, method, params=None):
        self.counter += 1
        self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params or {}}))
        return self.counter

    def call(self, method, params=None, timeout=60):
        message_id = self._send(method, params)
        deadline = time.time() + timeout
        while message_id not in self.replies:
            self.pump(deadline)
        msg = self.replies.pop(message_id)
        if "error" in msg:
            raise RuntimeError("%s 调用失败：%s" % (method, json.dumps(msg["error"], ensure_ascii=False)))
        return msg.get("result") or {}

    def pump(self, deadline):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("等待 CDP 响应超时")
        try:
            text = self.ws.recv(remaining)
        except socket.timeout:
            raise TimeoutError("等待 CDP 消息超时")
        self.dispatch(json.loads(text))

    def dispatch(self, msg):
        if "id" in msg:
            self.replies[msg["id"]] = msg
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            self.requests.append({"url": request.get("url", ""),
                                  "method": request.get("method", ""),
                                  "t": time.time()})
        elif method == "Runtime.consoleAPICalled":
            if params.get("type") == "error":
                args = params.get("args") or []
                self.console_errors.append(" ".join(
                    str(arg.get("value", arg.get("description", ""))) for arg in args))
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            self.exceptions.append(details.get("text") or details.get("exception", {}).get("description", ""))
        elif method == "Fetch.requestPaused":
            self.on_fetch(params)

    def on_fetch(self, params):
        request = params.get("request") or {}
        self.fetch_urls.append(request.get("url", ""))
        request_id = params["requestId"]
        if self.fetch_mode is None:
            self.call("Fetch.continueRequest", {"requestId": request_id})
            return
        if self.fetch_mode == "block":
            self.call("Fetch.failRequest", {"requestId": request_id, "errorReason": "Failed"})
            return
        status, body = self.fetch_mode
        self.call("Fetch.fulfillRequest", {
            "requestId": request_id,
            "responseCode": status,
            "responseHeaders": [{"name": "Content-Type",
                                 "value": "application/json; charset=utf-8"}],
            "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        })

    def sleep(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self.pump(min(deadline, time.time() + 0.2))
            except TimeoutError:
                pass

    # -- 页面 ------------------------------------------------------------
    def evaluate(self, expression, timeout=30):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
            "userGesture": True,
        }, timeout)
        if "exceptionDetails" in result:
            raise RuntimeError("页面脚本异常：%s" % json.dumps(
                result["exceptionDetails"], ensure_ascii=False)[:400])
        return (result.get("result") or {}).get("value")

    def wait_js(self, expression, timeout=25, interval=0.15):
        deadline = time.time() + timeout
        value = None
        while time.time() < deadline:
            try:
                value = self.evaluate(expression)
            except Exception:
                value = None
            if value:
                return value
            self.sleep(interval)
        return value

    def wait_selector(self, selector, timeout=25):
        return self.wait_js("!!document.querySelector(%s)" % json.dumps(selector), timeout)

    def count(self, selector):
        return self.evaluate("document.querySelectorAll(%s).length" % json.dumps(selector)) or 0

    def text(self, selector):
        return self.evaluate(
            "(() => { const el = document.querySelector(%s); return el ? el.textContent.trim() : null; })()"
            % json.dumps(selector))

    def texts(self, selector):
        return self.evaluate(
            "(() => Array.from(document.querySelectorAll(%s)).map((el) => el.textContent.trim()))()"
            % json.dumps(selector)) or []

    def attr(self, selector, name):
        return self.evaluate(
            "(() => { const el = document.querySelector(%s); return el ? el.getAttribute(%s) : null; })()"
            % (json.dumps(selector), json.dumps(name)))

    def hash(self):
        return self.evaluate("location.hash") or ""

    def navigate(self, url, wait_load=True):
        # 同一个 URL（含只改 hash 相同）在浏览器里不会重新加载，这时改成真刷新，
        # 否则「再打开这一份」拿到的是上一屏的回执，断言会读到不存在的元素。
        try:
            current = self.evaluate("location.href")
        except Exception:
            current = None
        if current == url:
            self.reload()
            return
        self.console_errors = []
        self.exceptions = []
        self.call("Page.navigate", {"url": url}, 60)
        self.sleep(0.25)
        if wait_load:
            self.wait_js("document.readyState === 'complete'", 30)

    def reload(self):
        self.console_errors = []
        self.exceptions = []
        self.call("Page.reload", {"ignoreCache": False}, 60)
        self.sleep(0.3)
        self.wait_js("document.readyState === 'complete'", 30)

    def mark(self):
        """给要找的元素打上唯一标记，再用真实鼠标事件点它（不在 JS 里直接 click）。"""
        return "data-t4-mark"

    def box(self, selector, text=None, scroll="center"):
        expression = (
            "(() => {"
            "  const sel = %s, want = %s, how = %s;"
            "  const list = Array.from(document.querySelectorAll(sel));"
            "  const el = want === null ? list[0] : list.find((e) => e.textContent.trim() === want);"
            "  if (!el) return null;"
            "  if (how === 'bottom') window.scrollTo(0, document.documentElement.scrollHeight);"
            "  else el.scrollIntoView({block: 'center', inline: 'nearest'});"
            "  const r = el.getBoundingClientRect();"
            "  const x = r.left + r.width / 2, y = r.top + r.height / 2;"
            "  const hit = document.elementFromPoint(x, y);"
            "  el.setAttribute('data-t4-mark', '1');"
            "  return {x: x, y: y, w: r.width, h: r.height, top: r.top, bottom: r.bottom,"
            "          hitOk: !!(hit && (hit === el || el.contains(hit))),"
            "          hit: hit ? hit.tagName + '.' + String(hit.className || '') : 'none',"
            "          vh: window.innerHeight};"
            "})()"
        ) % (json.dumps(selector), json.dumps(text) if text is not None else "null",
             json.dumps(scroll))
        return self.evaluate(expression)

    def click(self, selector, text=None, settle=0.35):
        """按元素中心派发真实鼠标事件；被 sticky 操作栏挡住时滚到底部重试，并留下证据。"""
        target = self.box(selector, text, "center")
        if not target or target["w"] <= 0 or target["h"] <= 0:
            raise RuntimeError("点不到元素：%s %s" % (selector, text or ""))
        if not target["hitOk"]:
            log("点击目标首次未命中：%s %s → elementFromPoint=%s（y=%.0f / vh=%.0f），滚到底部重试"
                % (selector, text or "", target["hit"], target["y"], target["vh"]))
            target = self.box(selector, text, "bottom")
            if not target or not target["hitOk"]:
                raise RuntimeError("滚到底部后仍被遮挡，点不到：%s %s → %s"
                                   % (selector, text or "", (target or {}).get("hit")))
        x, y = target["x"], target["y"]
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                               "button": "left", "clickCount": 1})
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                               "button": "left", "clickCount": 1})
        self.sleep(settle)
        return target

    def click_radio(self, index, name_prefix="check-cause-"):
        return self.evaluate(
            "(() => { const list = document.querySelectorAll('input[type=radio][name^=%s]');"
            "  const el = list[%d]; if (!el) return false; el.click(); return true; })()"
            % (json.dumps(name_prefix), index))

    def fill(self, selector, text):
        """像老师那样输入：焦点拿得到就真敲（insertText），拿不到就写值 + 派发 input。

        headless 下有些输入框 focus 不生效，硬用 insertText 会把字打到上一个获得焦点的框里，
        所以先确认 activeElement 再决定走哪条路，并把走法写进日志。
        """
        focused = self.evaluate(
            "(() => { const el = document.querySelector(%s); if (!el) return false;"
            "  el.focus(); el.value = ''; return document.activeElement === el; })()"
            % json.dumps(selector))
        if focused:
            self.call("Input.insertText", {"text": text})
            self.sleep(0.1)
        value = self.evaluate("(() => { const el = document.querySelector(%s);"
                              " return el ? el.value : null; })()" % json.dumps(selector))
        if value != text:
            log("输入走「写值 + 派发 input」：%s（focus=%s，insertText 后值=%r）"
                % (selector, focused, value))
            self.evaluate(
                "(() => { const el = document.querySelector(%s); if (!el) return false;"
                "  el.focus(); el.value = %s;"
                "  el.dispatchEvent(new Event('input', {bubbles: true})); return true; })()"
                % (json.dumps(selector), json.dumps(text)))
            self.sleep(0.1)
            value = self.evaluate("(() => { const el = document.querySelector(%s);"
                                  " return el ? el.value : null; })()" % json.dumps(selector))
        return value == text

    def screenshot(self, name):
        os.makedirs(SHOTS, exist_ok=True)
        data = self.call("Page.captureScreenshot", {"format": "png",
                                                    "captureBeyondViewport": False}, 60)
        raw = base64.b64decode(data["data"])
        path = os.path.join(SHOTS, name)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path, png_size(raw)

    def set_viewport(self, width, height):
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
        })


# --------------------------------------------------------------------------
# 浏览器进程
# --------------------------------------------------------------------------
class Browser:
    def __init__(self):
        self.path = next((item for item in EDGE_CANDIDATES if os.path.isfile(item)), None)
        if not self.path:
            raise RuntimeError("没有找到 Edge/Chrome 可执行文件")
        self.port = free_port()
        self.profile = os.path.join(WORK, "edge-profile")
        shutil.rmtree(self.profile, ignore_errors=True)
        os.makedirs(self.profile, exist_ok=True)
        self.proc = subprocess.Popen([
            self.path,
            "--headless=new",
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % self.profile,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=msEdgeSyncPromo,msEdgeSigninPromo,EdgeCollections,msEdgeIdentity",
            "--window-size=%d,%d" % VIEWPORT,
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.target = self._wait_target()
        self.ws = WS("127.0.0.1", self.port, self.target["path"])
        self.cdp = CDP(self.ws)

    def _json(self, path, method="GET"):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _wait_target(self):
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                items = self._json("/json/list")
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
                continue
            pages = [item for item in items
                     if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]
            if pages:
                pages.sort(key=lambda item: 0 if item.get("url") in ("about:blank", "") else 1)
                url = pages[0]["webSocketDebuggerUrl"]
                ws_path = url.split("127.0.0.1:%d" % self.port, 1)[-1]
                return {"path": ws_path, "url": pages[0].get("url", "")}
            time.sleep(0.3)
        raise RuntimeError("Edge 没有提供可调试页面")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True, timeout=30)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------
def login(cdp, token):
    cdp.call("Network.setCookie", {
        "name": "teacher_session", "value": token,
        "domain": "127.0.0.1", "path": "/", "httpOnly": True,
    })


def save_via_api(http, class_id, answer_id, body):
    return http.post("/api/classes/%d/check/answers/%d/save" % (class_id, answer_id), body)


def stage_a1_classes(cdp, ctx):
    section("13.1 A1：班级列表 → 进入班级即见「查看待核对作答」（一次点击直达作答）")

    cdp.navigate("http://127.0.0.1:%d/#/classes" % PORT)
    ok = cdp.wait_selector(".class-card", 25)
    check("13.1.1 班级列表已渲染", bool(ok))
    cards = cdp.texts(".class-card h3 a")
    check("13.1.2 班级卡标题直接指向作答核对", bool(ok) and ctx["class_name"] in cards, cards)
    card_link = 'a[href="#/class/%d/check"]' % ctx["class_id"]
    href = cdp.attr(card_link, "href")
    check("13.1.3 进入班级的链接是 #/class/{id}/check（不绕学生名单）",
          href == "#/class/%d/check" % ctx["class_id"], href)

    entered = cdp.box(card_link, "进入班级")
    check("13.1.4 「进入班级」按钮存在且可点", bool(entered), entered)
    cdp.click(card_link, "进入班级")
    ok = cdp.wait_selector(".check-hero", 25)
    check("13.1.5 单击进入班级后落在作答核对总览",
          bool(ok) and cdp.hash() == "#/class/%d/check" % ctx["class_id"], cdp.hash())

    title = cdp.text(".check-hero .state-title")
    check("13.1.6 首屏主标题给出本批作答该看什么", bool(title) and len(title) > 4, title)
    primary = cdp.texts(".check-hero .btn.primary")
    check("13.1.7 主按钮逐字「查看待核对作答」", primary == ["查看待核对作答"], primary)
    check("13.1.8 每屏只有一个 .btn.primary（A6）", cdp.count(".btn.primary") == 1,
          cdp.count(".btn.primary"))
    kv = " ".join(cdp.texts(".check-hero .kv"))
    # T11 更新（T10 改口径）：总览第四个标签由「待核对」改为「待核对作答」，避免与班级卡的错题口径同名。
    check("13.1.9 总览显示 应交/已核对/待核对作答/缺测 四个数",
          all(word in kv for word in ("应交", "已核对", "待核对作答", "缺测")), kv)
    # R-03：比数字（assignment.pending 与 queue.total），不再硬匹配标签字面。
    overview_api = ctx["http"].get("/api/classes/%d/check/overview" % ctx["class_id"])[1]
    queue_api = ctx["http"].get(
        "/api/classes/%d/check/answers/%d" % (ctx["class_id"], ctx["first_answer"]))[1].get("queue") or {}
    pending_num = (overview_api.get("assignment") or {}).get("pending")
    check("13.1.10 待核对数与队列 total 是同一个数（比数字，不比标签字面）",
          pending_num == queue_api.get("total") == 4,
          {"assignment.pending": pending_num, "queue.total": queue_api.get("total")})
    # N-02 / O-2：不能只查「整块 hero 文本里出现过这个数字」——state-flag 自己就写着
    # 「待核对作答 4 份」，那样 .kv 四数格里的数字被改坏也不会红（T17 复审实测如此）。
    # 这里按 .kv 的 k/v 逐格取值，再要求它与 state-flag 说的是同一个数。
    kv_pairs = cdp.evaluate(
        "(() => Array.from(document.querySelectorAll('.check-hero .kv')).map((el) => {"
        " const k = el.querySelector('.k'), v = el.querySelector('.v');"
        " return [k ? k.textContent.trim() : '', v ? v.textContent.trim() : '']; }))()") or []
    kv_pending = next((value for key, value in kv_pairs if key == '待核对作答'), None)
    flag_text = cdp.text(".check-hero .state-flag") or ""
    flag_squeezed = "".join(flag_text.split())
    check("13.1.10b O-2：.kv 网格里「待核对作答」= %s（接口值），且 state-flag 说同一个数"
          % (pending_num if pending_num is not None else -1),
          pending_num is not None and kv_pending == str(pending_num)
          and ("待核对作答%s份" % pending_num) in flag_squeezed,
          {"pending": pending_num, "kv": kv_pairs, "flag": flag_text})

    # 反向自证：把 .kv 里那个数字人为 +1（模拟「接口对、页面渲染错」），同一条谓词必须为假；
    # 随即复原。这是「这条断言有牙齿」的实测证据，不是口头保证。
    mutation = cdp.evaluate(
        "(() => { const cells = Array.from(document.querySelectorAll('.check-hero .kv'));"
        " const cell = cells.find((el) => { const k = el.querySelector('.k');"
        "   return k && k.textContent.trim() === '待核对作答'; });"
        " if (!cell) return null; const v = cell.querySelector('.v');"
        " const original = v.textContent.trim(); v.textContent = String(Number(original) + 1);"
        " const mutated = v.textContent.trim(); v.textContent = original;"
        " return {original: original, mutated: mutated, restored: v.textContent.trim()}; })()")
    mutated_value = (mutation or {}).get("mutated")
    check("13.1.10b-mut O-2 反向自证：.kv 数字 +1 后该谓词为假、复原后为真",
          mutation is not None and pending_num is not None
          and (mutation.get("original") == str(pending_num))
          and (mutated_value != str(pending_num))
          and (mutation.get("restored") == str(pending_num)), mutation)
    log("O-2 变异实测：原值=%s → 人为+1=%s → 复原=%s；变异后该谓词=%s"
        % ((mutation or {}).get("original"), mutated_value, (mutation or {}).get("restored"),
           "假（断言会红）" if mutated_value != str(pending_num) else "真"))

    target = cdp.box(".check-hero .btn.primary", "查看待核对作答")
    check("13.1.11 主按钮可见（宽高都大于 0）", bool(target) and target["w"] > 0 and target["h"] > 0,
          target)
    cdp.click(".check-hero .btn.primary", "查看待核对作答")
    ok = cdp.wait_selector(".step-line", 25)
    check("13.1.12 单击主按钮直达作答页（hash 落到 check/{answer_id}）",
          bool(ok) and cdp.hash().startswith("#/class/%d/check/" % ctx["class_id"])
          and cdp.hash().endswith(str(ctx["first_answer"])), cdp.hash())


def stage_a2(cdp, ctx):
    section("13.2 A2：首处错步——逐行原文 + 下划线 + 「首处不等价」+ 数学对照")

    cdp.wait_selector(".step-line.bad", 25)
    lines = cdp.texts(".step-line .step-math")
    check("13.2.1 学生原文逐行列出（原题 + 三行原文里的两行）", len(lines) >= 3, lines)
    check("13.2.2 原文保留学生写的 3x-2=12", any("3x-2=12" in item for item in lines), lines)

    underline = cdp.text(".step-line.bad u.bad-underline")
    check("13.2.3 错步行有下划线，且只划到 -2", underline == "-2", underline)
    marks = cdp.texts(".step-mark")
    check("13.2.4 错步有文字标记「首处不等价」", "首处不等价" in marks, marks)
    check("13.2.4b 「首处不等价」全页只出现一次（T10 去掉标题旁的重复标记）",
          cdp.count(".step-mark") == 1, marks)
    bag = cdp.evaluate(
        "(() => { const el = document.querySelector('.step-line.bad');"
        " return el ? el.className + '|' + (el.querySelector('u') ? 'u' : 'no-u') : null; })()")
    check("13.2.5 标记与下划线出现在同一行（bad 行）", bool(bag) and bag.startswith("step-line bad|u"),
          bag)

    contrast = cdp.texts(".contrast .v")
    check("13.2.6 数学对照给出「按规则应为 3x-6」",
          any("3x-6" in item for item in contrast), contrast)
    note = " ".join(cdp.texts(".contrast ~ p, .step-list ~ p"))
    body = cdp.evaluate("document.querySelector('.check-body').innerText") or ""
    check("13.2.7 对照句逐字说明括号外的 3 也要乘以 -2",
          "展开后应为 3x-6；括号外的 3 也要乘以 -2。" in body, body[:0])
    check("13.2.8 预置来源标注「预置演示 · 不代表已实现通用定位」在页面上",
          "预置演示 · 不代表已实现通用定位" in body)
    check("13.2.9 页面写明不推断原因（只记录观察到的不等价）",
          "这里只记录观察到的不等价，不推断学生为什么这样写。" in body)
    open_state = cdp.evaluate(
        "(() => { const d = document.querySelector('details.std-sol'); return d ? d.open : null; })()")
    check("13.2.10 标准解默认折叠（details 未展开）", open_state is False, open_state)
    check("13.2.11 合成演示作答标「合成演示数据」", "合成演示数据" in body)


def stage_a3(cdp, ctx):
    section("13.3 A3：三个区块 + 主按钮逐字 + 两条轴分开显示")

    body = cdp.evaluate("document.querySelector('.check-body').innerText") or ""
    check("13.3.1 三个区块标题逐字出现",
          all(word in body for word in ("观察到的错步", "可能原因", "教师判断")),
          [word for word in ("观察到的错步", "可能原因", "教师判断") if word not in body])
    button = cdp.texts(".check-bar .btn.primary")
    check("13.3.2 主按钮逐字「保存错步，原因待判断」",
          button == ["保存错步，原因待判断"], button)
    chips = cdp.texts(".check-bar .axis-chip")
    check("13.3.3 两个轴各一个标签：待核对 / 原因待判断",
          chips == ["待核对", "原因待判断"], chips)
    check("13.3.4 两条轴是分开的两个元素（不是一句话）",
          cdp.count(".check-bar .axis-row .axis-chip") == 2,
          cdp.count(".check-bar .axis-row .axis-chip"))


def stage_a4_skip(cdp, ctx):
    section("13.4 A4：当面追问——问句按本条作答给出（R1-06 改写）；「暂时跳过」不发请求、不跳页、不保存")

    body = cdp.evaluate("document.querySelector('.check-ask').innerText") or ""
    # R1-06 改写（依据：PRD §4.3「追问要针对具体的错步」、规格 §3.2「追问来源分预置/教师自拟/
    # 已审资源库，没有可用资源时明确写暂无」、任务书 R1-06「移除对所有真实作答统一使用去括号问句；
    # 无资源时明确暂无」，以及基线审查 §10/§12 —— 原文断言等于把「写死常量」写成了预期）。
    # 原文只要求页面逐字出现「你当时是怎么展开这个括号的？」，它无法区分「按本条错步给的问句」
    # 与「任何作答都一样的常量」。现在改成与接口对账：页面上那句必须逐字等于服务端为本条作答
    # 给出的 followup.suggestion.question，且来源是这条演示记录的预置问句。
    detail_api = ctx["http"].get(
        "/api/classes/%d/check/answers/%d" % (ctx["class_id"], ctx["first_answer"]))[1]
    follow = detail_api.get("followup") or {}
    suggestion = follow.get("suggestion") or {}
    question = suggestion.get("question") or ""
    check("13.4.1 卡片问句逐字等于服务端按本条作答给出的建议问句（来源：本条预置记录）",
          bool(question) and ("可以这样问学生：" + question) in body
          and suggestion.get("source") == "preset"
          and suggestion.get("source_detail") == "demo_record", body[:160])
    other = (ctx["http"].get("/api/classes/%d/check/answers/%d"
                             % (ctx["class_id"], ctx["answers"]["DEMO-B-01"]))[1].get("followup") or {})
    ok_one = (ctx["http"].get("/api/classes/%d/check/answers/%d"
                              % (ctx["class_id"], ctx["answers"]["DEMO-OK-01"]))[1].get("followup") or {})
    check("13.4.1b 问句随本题错步变化（移项题另一句、不含「括号」）；正确作答不自动给追问",
          bool(other.get("question")) and other.get("question") != question
          and "括号" not in (other.get("question") or "")
          and ok_one.get("available") is False and (ok_one.get("question") or "") == ""
          and "暂无适用追问" in (ok_one.get("note") or ""),
          {"other": other.get("question"), "ok": ok_one.get("question")})
    actions = cdp.texts(".check-ask .check-actions .btn")
    check("13.4.2 追问卡只有「记录学生回答」「暂时跳过」两个动作",
          actions == ["记录学生回答", "暂时跳过"], actions)

    hash_before = cdp.hash()
    requests_before = len(cdp.requests)
    saves_before = len([item for item in cdp.requests if "/save" in item["url"]])
    cdp.click(".check-ask .check-actions .btn", "暂时跳过", settle=1.2)
    new_requests = cdp.requests[requests_before:]
    new_saves = len([item for item in cdp.requests if "/save" in item["url"]]) - saves_before
    log("跳过前后请求：总请求 %d → %d；新增请求 %s"
        % (requests_before, len(cdp.requests),
           [(item["method"], item["url"].split("127.0.0.1:%d" % PORT)[-1]) for item in new_requests]))
    check("13.4.3 点「暂时跳过」没有发出任何请求（网络面板为空）", not new_requests,
          [(item["method"], item["url"]) for item in new_requests])
    check("13.4.4 点「暂时跳过」没有发出保存请求", new_saves == 0, new_saves)
    check("13.4.5 点「暂时跳过」没有跳页", cdp.hash() == hash_before, cdp.hash())
    skip_line = cdp.evaluate(
        "(() => { const el = document.querySelector('.check-ask > p.tiny.muted');"
        " const hit = Array.from(document.querySelectorAll('.check-ask p')).map((p) => p.textContent);"
        " return hit.join('|'); })()")
    check("13.4.6 跳过之后明确写「已跳过提问，未记录回答」",
          "已跳过提问，未记录回答" in (skip_line or ""), skip_line)
    rows = db_query("SELECT COUNT(*) AS n FROM answer_checks")
    check("13.4.7 跳过之后库里没有任何 check 记录", rows[0]["n"] == 0, rows)
    followups = db_query("SELECT COUNT(*) AS n FROM answer_followups")
    check("13.4.8 跳过之后没有追问记录", followups[0]["n"] == 0, followups)


def stage_a5_save(cdp, ctx):
    section("13.5 A5（a）：主按钮保存 → cause_state=undecided 且 progress=checked；回执与下一份")

    hash_before = cdp.hash()
    cdp.click(".check-bar .btn.primary", "保存错步，原因待判断", settle=0.5)
    ok = cdp.wait_selector(".receipt", 25)
    check("13.5.1 保存后同屏出现回执（没有跳页）",
          bool(ok) and cdp.hash() == hash_before, cdp.hash())
    line = cdp.text(".receipt-line")
    expected = "已保存到：%s · %s" % (ctx["class_name"], ctx["assignment"])
    check("13.5.2 回执逐字「%s」" % expected, line == expected, line)
    chips = cdp.texts(".receipt .axis-chip")
    check("13.5.3 回执里两条轴分开显示：已核对 / 原因待判断",
          chips == ["已核对", "原因待判断"], chips)
    receipt_text = cdp.evaluate("document.querySelector('.receipt').innerText") or ""
    check("13.5.4 回执写明「两轴状态：已核对 · 原因待判断」",
          "两轴状态：已核对 · 原因待判断" in receipt_text, receipt_text[:160])
    check("13.5.5 跳过提问时回执说明未记录学生回答",
          "本次未记录学生回答" in receipt_text)
    check("13.5.6 回执给出未采纳候选的支持/削弱/待补依据",
          all(word in receipt_text for word in ("支持", "削弱", "待补")),
          receipt_text[:200])

    row = db_query("SELECT progress, cause_state, cause_code FROM answer_checks"
                   " WHERE answer_id=?", (ctx["first_answer"],))
    check("13.5.7 库里 progress=checked、cause_state=undecided、cause_code 为空",
          len(row) == 1 and row[0]["progress"] == "checked"
          and row[0]["cause_state"] == "undecided" and row[0]["cause_code"] == "",
          row)

    next_id = cdp.evaluate(
        "(() => { const a = document.querySelector('.receipt .btn.primary');"
        " if (!a) return null; return a.getAttribute('href'); })()")
    check("13.5.8 回执给出「查看下一份」，指向同一个队列的下一份",
          next_id == "#/class/%d/check/%d" % (ctx["class_id"], ctx["answers"]["DEMO-B-01"]), next_id)
    path, size = cdp.screenshot("ui-a5-receipt-1366x768.png")
    check("13.5.9 回执截图已保存且尺寸 1366×768", size == VIEWPORT,
          "%s %s" % (path, size))

    cdp.click(".receipt .btn.primary", "查看下一份", settle=0.4)
    ok = cdp.wait_selector(".step-line.bad", 25)
    check("13.5.10 点「查看下一份」进入下一份（B 的错步）",
          bool(ok) and cdp.hash().endswith(str(ctx["answers"]["DEMO-B-01"])), cdp.hash())


def stage_a5_adopt(cdp, ctx):
    section("13.6 A5（b）：采纳选中原因 → cause_state=decided 且 cause_code 有值；同屏显示学生原话")

    answer_id = ctx["answers"]["DEMO-B-01"]
    ok = cdp.wait_selector(".check-ask", 25)
    check("13.6.1 下一份（案例B）作答页已就绪", bool(ok))

    cdp.click(".check-ask .check-actions .btn", "记录学生回答", settle=0.3)
    visible = cdp.evaluate(
        "(() => { const w = document.querySelector('.follow-wrap');"
        " return w ? !w.hidden : false; })()")
    check("13.6.2 点「记录学生回答」后出现原话输入框", visible is True, visible)
    quote = "先移项，再合并同类项。"
    cdp.fill(".follow-wrap textarea", quote)
    filled = cdp.evaluate("document.querySelector('.follow-wrap textarea').value")
    check("13.6.3 输入框里逐字记下学生原话", filled == quote, filled)

    picked = cdp.click_radio(0)
    check("13.6.4 可以选中一条候选原因", picked is True)
    bar_texts = cdp.texts(".check-bar .check-actions .btn")
    adopt_texts = cdp.texts(".adopt-row .btn")
    # T11 更新（T10 提交唯一化）：底部栏只剩一个提交按钮，采纳挪到候选区 .adopt-row。
    check("13.6.5 底部操作栏只有一个提交按钮「保存错步，原因待判断」",
          bar_texts == ["保存错步，原因待判断"], bar_texts)
    check("13.6.6 采纳按钮在候选区且不是 primary（每屏仍只有一个 primary）",
          adopt_texts == ["采纳选中原因并保存"] and cdp.count(".btn.primary") == 1,
          {"adopt": adopt_texts, "primary": cdp.count(".btn.primary")})

    cdp.click(".adopt-row .btn", "采纳选中原因并保存", settle=0.5)
    ok = cdp.wait_selector(".receipt", 25)
    receipt_text = cdp.evaluate("document.querySelector('.receipt').innerText") or ""
    check("13.6.7 采纳保存后出现回执", bool(ok))
    check("13.6.8 回执显示学生原话", ("学生原话：「%s」" % quote) in receipt_text, receipt_text[:200])
    check("13.6.9 回执显示「教师已判断：移项没有变号」",
          "教师已判断：移项没有变号" in receipt_text, receipt_text[:400])
    check("13.6.10 回执显示已采纳的候选依据",
          "已采纳的候选" in receipt_text and "支持" in receipt_text)

    row = db_query("SELECT progress, cause_state, cause_code, cause_label FROM answer_checks"
                   " WHERE answer_id=?", (answer_id,))
    check("13.6.11 库里 cause_state=decided 且 cause_code=E-TRANS-01",
          len(row) == 1 and row[0]["cause_state"] == "decided"
          and row[0]["cause_code"] == "E-TRANS-01", row)
    follow = db_query("SELECT answer_id, answer_version, answer_text FROM answer_followups")
    check("13.6.12 追问原话写进库并关联这条作答",
          len(follow) == 1 and follow[0]["answer_id"] == answer_id
          and follow[0]["answer_text"] == quote, follow)
    path, size = cdp.screenshot("ui-a5-adopt-receipt-1366x768.png")
    check("13.6.13 采纳回执截图已保存且尺寸 1366×768", size == VIEWPORT, "%s %s" % (path, size))


def stage_a5_rejudge(cdp, ctx):
    section("13.7 A5（c）：改判 → cause_history 长度 +1，当前结论只显示新原因")

    answer_id = ctx["answers"]["DEMO-A-01"]
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class_id"], answer_id))
    ok = cdp.wait_selector(".cause-item", 25)
    check("13.7.1 案例A 有 3 条候选原因", bool(ok) and cdp.count(".cause-item") == 3,
          cdp.count(".cause-item"))

    cdp.click_radio(0)
    cdp.click(".adopt-row .btn", "采纳选中原因并保存", settle=0.5)
    cdp.wait_selector(".receipt", 25)
    first = db_query("SELECT cause_code, cause_label, cause_history FROM answer_checks"
                     " WHERE answer_id=?", (answer_id,))
    check("13.7.2 第一次采纳 E-DIST-01 成功",
          len(first) == 1 and first[0]["cause_code"] == "E-DIST-01", first)
    history_first = json.loads(first[0]["cause_history"] or "[]")
    check("13.7.3 第一次采纳时 cause_history 为空", history_first == [], history_first)

    # 老师刷新这一份再改判：换一条候选重新采纳（同一 hash 不能靠 navigate，必须真刷新）
    cdp.reload()
    cdp.wait_selector(".cause-item", 25)
    before_text = cdp.evaluate("document.querySelector('.check-bar').innerText") or ""
    check("13.7.4 重开这一份时当前结论显示已判断的 E-DIST-01",
          "已核对" in before_text and "教师已判断" in before_text, before_text[:200])
    cdp.click_radio(1)
    cdp.click(".adopt-row .btn", "采纳选中原因并保存", settle=0.5)
    cdp.wait_selector(".receipt", 25)
    after = db_query("SELECT cause_code, cause_label, cause_history FROM answer_checks"
                     " WHERE answer_id=?", (answer_id,))
    history = json.loads(after[0]["cause_history"] or "[]")
    check("13.7.5 改判后 cause_history 长度 = 1（旧原因进历史）",
          len(history) == 1 and history[0]["code"] == "E-DIST-01", history)
    check("13.7.6 当前结论只显示新原因 E-DIST-02",
          after[0]["cause_code"] == "E-DIST-02"
          and "只把系数乘以括号里的第一项" not in (after[0]["cause_label"] or ""), after)
    receipt_text = cdp.evaluate("document.querySelector('.receipt').innerText") or ""
    check("13.7.7 回执里显示新原因、不显示旧原因",
          "括号外系数与括号内符号的处理偏差" in receipt_text
          and "只把系数乘以括号里的第一项" not in receipt_text, receipt_text[:300])


def stage_records(cdp, ctx):
    section("13.8 A5 记录找回：列表与详情一致；改判只显示新原因")

    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/records?cause_state=decided"
                 % (PORT, ctx["class_id"]))
    ok = cdp.wait_selector(".check-records table", 25)
    heading = " ".join(cdp.texts(".check-records h3"))
    rows = cdp.texts(".check-records tbody tr")
    check("13.8.1 「教师已判断」筛选下有 2 条记录", bool(ok) and "核对记录（2 条）" in heading,
          heading)
    check("13.8.2 记录列表 2 行", len(rows) == 2, rows)
    joined = " | ".join(rows)
    check("13.8.3 列表只显示改判后的新原因", "括号外系数与括号内符号的处理偏差" in joined
          and "只把系数乘以括号里的第一项" not in joined, joined)

    detail = ctx["http"].get("/api/classes/%d/check/answers/%d"
                             % (ctx["class_id"], ctx["answers"]["DEMO-A-01"]))[1]
    observed = detail["judgement"]["observed_step"]
    check("13.8.4 列表里的「观察到的错步」与详情一致", observed and observed in joined,
          "%s vs %s" % (observed, joined))

    # R1-02 契约变更改写（依据：任务书 T4 第 1 条「cause_state 增加能收口的第三态」、
    # 基线审查 §15.3、契约 §3.3 规则 3）。旧断言把「只有答案」那份存成「原因待判断」，
    # 三态收口后它只能记「无需判断」（正确/缺测/缺少步骤没有可归因的错步）。
    # 因此这里改用「首处错步」那份准备待判断记录，并顺带覆盖「无需判断」那一档。
    pending_id = ctx["answers"]["DEMO-B-01"]
    pending_detail = ctx["http"].get(
        "/api/classes/%d/check/answers/%d" % (ctx["class_id"], pending_id))[1]
    status, _ = save_via_api(ctx["http"], ctx["class_id"], pending_id, {
        "expected_version": pending_detail["answer"]["version"],
        "observed_step": pending_detail["locate"].get("first_bad_text") or "",
        "first_bad_index": pending_detail["locate"].get("first_bad_index"),
        "cause_state": "undecided",
    })
    check("13.8.0 先把「首处错步」那份存成待判断（准备待判断记录）", status == 200, status)
    no_apply_id = ctx["answers"]["DEMO-C-01"]
    no_apply_detail = ctx["http"].get(
        "/api/classes/%d/check/answers/%d" % (ctx["class_id"], no_apply_id))[1]
    status_na, saved_na = save_via_api(ctx["http"], ctx["class_id"], no_apply_id, {
        "expected_version": no_apply_detail["answer"]["version"],
        "observed_step": "",
        "first_bad_index": None,
        "cause_state": "undecided",
    })
    check("13.8.0b 「只有答案」那份保存后记「无需判断」(not_applicable)",
          status_na == 200
          and (saved_na.get("judgement") or {}).get("cause_state") == "not_applicable",
          (status_na, (saved_na.get("judgement") or {}).get("cause_state")))

    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/records?cause_state=undecided"
                 % (PORT, ctx["class_id"]))
    cdp.wait_selector(".check-records", 25)
    undecided_heading = " ".join(cdp.texts(".check-records h3"))
    check("13.8.5 「原因待判断」筛选能找回待判断的那一份",
          "核对记录（1 条）" in undecided_heading, undecided_heading)
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/records?cause_state=not_applicable"
                 % (PORT, ctx["class_id"]))
    cdp.wait_selector(".check-records", 25)
    na_heading = " ".join(cdp.texts(".check-records h3"))
    na_cells = cdp.texts(".check-records tbody td:nth-child(6)")
    check("13.8.6 「无需判断」筛选能找回拒答/无需归因的那几份（不再混进待判断）",
          "核对记录（1 条）" in na_heading and any("无需判断" in (cell or "") for cell in na_cells),
          {"heading": na_heading, "cause_cells": na_cells})

def stage_a6(cdp, ctx):
    section("13.9 A6：1366×768 首屏可见错步与主动作、无横向滚动、只有一个主按钮")

    cdp.set_viewport(*VIEWPORT)
    answer_id = ctx["answers"]["DEMO-B-01"]
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class_id"], answer_id))
    cdp.wait_selector(".step-line.bad", 25)

    metrics = cdp.evaluate(
        "(() => { const de = document.documentElement;"
        " const bad = document.querySelector('.step-line.bad');"
        " const bar = document.querySelector('.check-bar');"
        " const primary = document.querySelector('.check-bar .btn.primary');"
        " const rb = bad ? bad.getBoundingClientRect() : null;"
        " const rp = primary ? primary.getBoundingClientRect() : null;"
        " const rbar = bar ? bar.getBoundingClientRect() : null;"
        " return {scrollWidth: de.scrollWidth, bodyScrollWidth: document.body.scrollWidth,"
        "  innerWidth: window.innerWidth, innerHeight: window.innerHeight,"
        "  bad: rb ? {top: rb.top, bottom: rb.bottom, width: rb.width} : null,"
        "  primary: rp ? {top: rp.top, bottom: rp.bottom, width: rp.width, height: rp.height} : null,"
        "  bar: rbar ? {top: rbar.top, bottom: rbar.bottom, position: getComputedStyle(bar).position} : null,"
        "  primaryCount: document.querySelectorAll('.btn.primary').length}; })()")
    log("A6 视口测量：%s" % json.dumps(metrics, ensure_ascii=False))
    check("13.9.1 视口就是 1366×768", metrics["innerWidth"] == 1366
          and metrics["innerHeight"] == 768, (metrics["innerWidth"], metrics["innerHeight"]))
    check("13.9.2 document.documentElement.scrollWidth <= 1366（无横向滚动）",
          metrics["scrollWidth"] <= 1366, metrics["scrollWidth"])
    check("13.9.3 错步在首屏可见（top>=0 且 bottom<=768）",
          metrics["bad"] and metrics["bad"]["top"] >= 0 and metrics["bad"]["bottom"] <= 768,
          metrics["bad"])
    check("13.9.4 主按钮在首屏可见（bottom<=768 且宽高>0）",
          metrics["primary"] and metrics["primary"]["bottom"] <= 768
          and metrics["primary"]["width"] > 0 and metrics["primary"]["height"] > 0,
          metrics["primary"])
    check("13.9.5 操作栏是文档流内的 sticky（不是 fixed 浮层）",
          metrics["bar"] and metrics["bar"]["position"] == "sticky", metrics["bar"])
    check("13.9.6 每屏只有一个 .btn.primary", metrics["primaryCount"] == 1,
          metrics["primaryCount"])

    # 首屏截图必须在滚动之前拍：内容就是老师打开这份作答第一眼看到的东西
    scroll_state = cdp.evaluate("(() => { window.scrollTo(0, 0); return {y: window.scrollY}; })()")
    check("13.9.9 截图前回到首屏（scrollY=0，画面就是第一屏）", scroll_state["y"] == 0,
          scroll_state)
    path, size = cdp.screenshot("ui-a6-first-bad-1366x768.png")
    check("13.9.9b 1366×768 首屏截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))

    # sticky 操作栏不能遮住正文末段：滚到底部后最后一张卡片的文字仍可命中
    cover = cdp.evaluate(
        "(() => { window.scrollTo(0, document.documentElement.scrollHeight);"
        " const cards = document.querySelectorAll('.check-body .card');"
        " const last = cards[cards.length - 1];"
        " const bar = document.querySelector('.check-bar');"
        " const rb = last ? last.getBoundingClientRect() : null;"
        " const rbar = bar ? bar.getBoundingClientRect() : null;"
        " let hit = null;"
        " if (rb) { const x = rb.left + Math.min(40, rb.width / 2);"
        "   const y = rb.top + Math.min(20, Math.max(1, rb.height - 4));"
        "   const el = document.elementFromPoint(x, y);"
        "   hit = el ? {insideLastCard: last.contains(el), tag: el.tagName,"
        "                x: x, y: y} : null; }"
        " return {lastBottom: rb ? rb.bottom : null, barTop: rbar ? rbar.top : null,"
        "         scrollY: window.scrollY, hit: hit}; })()")
    log("A6 遮挡检查：%s" % json.dumps(cover, ensure_ascii=False))
    check("13.9.7 滚到底部后操作栏没有压住最后一张卡片（lastBottom <= barTop）",
          cover["lastBottom"] is not None and cover["barTop"] is not None
          and cover["lastBottom"] <= cover["barTop"] + 1,
          (cover["lastBottom"], cover["barTop"]))
    check("13.9.8 末张卡片里的元素仍能被 elementFromPoint 命中",
          cover["hit"] and cover["hit"]["insideLastCard"] is True, cover["hit"])

    # 追问按钮在 sticky 操作栏上方，最容易被遮挡：滚到底部后必须仍可点
    ask_hit = cdp.evaluate(
        "(() => { window.scrollTo(0, document.documentElement.scrollHeight);"
        " const list = Array.from(document.querySelectorAll('.check-ask .check-actions .btn'));"
        " const out = [];"
        " for (const el of list) { const r = el.getBoundingClientRect();"
        "   const x = r.left + r.width / 2, y = r.top + r.height / 2;"
        "   const hit = document.elementFromPoint(x, y);"
        "   out.push({text: el.textContent.trim(), y: Math.round(y),"
        "             ok: !!(hit && (hit === el || el.contains(hit)))}); }"
        " return {buttons: out, vh: window.innerHeight}; })()")
    log("A6 追问按钮命中检查：%s" % json.dumps(ask_hit, ensure_ascii=False))
    check("13.9.8b 滚到底部后「记录学生回答」「暂时跳过」都仍可点（没有被操作栏压住）",
          len(ask_hit["buttons"]) == 2 and all(item["ok"] for item in ask_hit["buttons"]),
          ask_hit)


def stage_a6_long(cdp, ctx):
    section("13.9b A6：长作答默认折叠、展开后仍无横向滚动")

    # 长作答：15 行，折叠 5 行，展开后仍无横向滚动
    long_id = ctx["long_answer"]
    if not long_id:
        hold("13.9.10 长作答折叠与展开", "造长作答失败，未能取到证据")
        return
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class2_id"], long_id))
    ok = cdp.wait_selector(".step-line", 25)
    check("13.9.10 长作答页已渲染", bool(ok), cdp.hash())
    folded = cdp.evaluate(
        "(() => { const d = document.querySelector('details.more-steps');"
        " if (!d) return null;"
        " return {open: d.open, inner: d.querySelectorAll('.step-line').length,"
        "         total: document.querySelectorAll('.step-line').length}; })()")
    log("长作答折叠状态：%s" % json.dumps(folded, ensure_ascii=False))
    check("13.9.11 长作答默认折叠（前 10 行在正文里，其余 %s 行藏在折叠区里）"
          % (folded["inner"] if folded else "?"),
          folded is not None and folded["open"] is False and folded["total"] > 10
          and folded["inner"] == folded["total"] - 10, folded)
    width_before = cdp.evaluate("document.documentElement.scrollWidth")
    check("13.9.12 折叠状态下仍无横向滚动", width_before <= 1366, width_before)
    cdp.click("details.more-steps summary", settle=0.4)
    opened = cdp.evaluate(
        "(() => { const d = document.querySelector('details.more-steps'); return d ? d.open : null; })()")
    total = cdp.count(".step-line")
    width_after = cdp.evaluate("document.documentElement.scrollWidth")
    log("长作答展开：open=%s 行数=%s scrollWidth=%s" % (opened, total, width_after))
    check("13.9.13 点开后长作答完整显示（全部 %s 行都在页面上）" % total,
          opened is True and folded is not None and total == folded["total"], (opened, total))
    check("13.9.14 展开长作答后仍无横向滚动（scrollWidth<=1366）", width_after <= 1366,
          width_after)
    path, size = cdp.screenshot("ui-a6-long-answer-expanded-1366x768.png")
    check("13.9.15 长作答展开截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))


def stage_save_failure(cdp, ctx):
    section("13.10 保存失败留页：拦截保存请求返回 500，页面不跳走、输入不丢")

    long_id = ctx["long_answer"]
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class2_id"], long_id))
    cdp.wait_selector(".check-bar", 25)
    cdp.click(".check-ask .check-actions .btn", "记录学生回答", settle=0.3)
    observed_text = "3x-2=12"
    type_text = "这一处是他自己改的。"
    cdp.fill("input.observed-input", observed_text)
    cdp.fill(".follow-wrap textarea", type_text)

    cdp.call("Fetch.enable", {"patterns": [
        {"urlPattern": "*/check/answers/*/save*", "requestStage": "Request"}]})
    cdp.fetch_mode = (500, json.dumps({"error": "保存失败，这次没有写入任何记录；本页内容还在，可以直接重试。",
                                      "code": "save_failed"}, ensure_ascii=False))
    hash_before = cdp.hash()
    cdp.click(".check-bar .btn.primary", "保存错步，原因待判断", settle=1.0)
    ok = cdp.wait_js(
        "(() => { const el = document.querySelector('.banner.danger');"
        " return el && !el.hidden && el.textContent.indexOf('保存失败') >= 0; })()", 20)
    banner = cdp.text(".banner.danger")
    log("拦截到的保存请求：%s" % cdp.fetch_urls)
    check("13.10.1 保存返回 500 后页面显示失败横幅", bool(ok), banner)
    check("13.10.2 横幅写明 500 与「可以再点一次」", "保存失败（500）" in (banner or ""),
          banner)
    check("13.10.3 页面没有跳走（hash 不变）", cdp.hash() == hash_before, cdp.hash())
    values = cdp.evaluate(
        "(() => ({observed: (document.querySelector('input.observed-input') || {}).value,"
        " follow: (document.querySelector('.follow-wrap textarea') || {}).value,"
        " receipt: !!document.querySelector('.receipt')}))()")
    check("13.10.4 老师填的错步没丢", values["observed"] == observed_text, values)
    check("13.10.5 老师记的原话没丢", values["follow"] == type_text, values)
    check("13.10.6 失败后没有出现成功回执", values["receipt"] is False, values["receipt"])
    rows = db_query("SELECT COUNT(*) AS n FROM answer_checks WHERE answer_id=?", (long_id,))
    check("13.10.7 失败时库里没有写入这条 check", rows[0]["n"] == 0, rows)
    path, size = cdp.screenshot("ui-save-failed-500-1366x768.png")
    check("13.10.8 失败留页截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))

    # 解除拦截后同一请求能保存成功，证明失败是拦截造成的而不是页面坏了
    cdp.fetch_mode = None
    cdp.call("Fetch.disable")
    cdp.click(".check-bar .btn.primary", "保存错步，原因待判断", settle=1.0)
    ok = cdp.wait_selector(".receipt", 25)
    rows = db_query("SELECT COUNT(*) AS n FROM answer_checks WHERE answer_id=?", (long_id,))
    check("13.10.9 解除拦截后重试保存成功（说明失败来自 mock）",
          bool(ok) and rows[0]["n"] == 1, rows)


def stage_states(cdp, ctx):
    section("13.11 首屏两态：no_answers / all_checked 各一张截图")

    # no_answers：一个新班级（截图后这个班级用来放长作答）
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check" % (PORT, ctx["class2_id"]))
    ok = cdp.wait_selector(".check-hero .state-title", 25)
    title = cdp.text(".check-hero .state-title")
    body = cdp.evaluate("document.querySelector('.check-overview').innerText") or ""
    check("13.11.1 空班级首屏文案「尚未导入作答」", bool(ok) and title == "尚未导入作答", title)
    check("13.11.2 空班级不渲染空表", cdp.count(".check-overview table") == 0,
          cdp.count(".check-overview table"))
    check("13.11.3 空班级也不出现「已核对完」", "本次作业已核对完" not in body)
    path, size = cdp.screenshot("ui-a1-no-answers-1366x768.png")
    check("13.11.4 no_answers 截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))

    # all_checked：演示班级四份全部保存之后（A、B 已保存，C 在记录找回那段保存）
    answer_id = ctx["answers"]["DEMO-OK-01"]
    detail = ctx["http"].get("/api/classes/%d/check/answers/%d" % (ctx["class_id"], answer_id))[1]
    body_payload = {
        "expected_version": detail["answer"]["version"],
        "observed_step": detail["locate"].get("first_bad_text") or "",
        "first_bad_index": detail["locate"].get("first_bad_index"),
        "cause_state": "undecided",
    }
    status, _ = save_via_api(ctx["http"], ctx["class_id"], answer_id, body_payload)
    check("13.11.5 用接口把最后一份（正确作答）保存掉（准备 all_checked 状态）",
          status == 200, status)

    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check" % (PORT, ctx["class_id"]))
    ok = cdp.wait_selector(".check-hero .state-title", 25)
    title = cdp.text(".check-hero .state-title")
    body = cdp.evaluate("document.querySelector('.check-overview').innerText") or ""
    check("13.11.6 全部核对完首屏文案「本次作业已核对完」",
          bool(ok) and title == "本次作业已核对完", title)
    check("13.11.7 已核对完时不把作业表渲染成空表（作业表有行）",
          cdp.count(".check-overview table tbody tr") >= 1,
          cdp.count(".check-overview table tbody tr"))
    check("13.11.8 已核对完时提示去「查看核对记录」找回原因待判断的记录",
          "查看核对记录" in body)
    all_primary = cdp.evaluate(
        "(() => { const p = document.querySelector('.check-hero .btn.primary');"
        " return p ? {tag: p.tagName, text: p.textContent.trim(), disabled: !!p.disabled} : null; })()")
    check("13.11.8b all_checked 态主按钮为可点的「查看核对记录」（契约 §4.2 R-06）",
          all_primary is not None and all_primary["tag"] == "A"
          and all_primary["text"] == "查看核对记录" and not all_primary["disabled"], all_primary)
    path, size = cdp.screenshot("ui-a1-all-checked-1366x768.png")
    check("13.11.9 all_checked 截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))


def stage_manual_cause(cdp, ctx):
    """R1-02：真实作答没有候选时，老师也能在页面上人工填写原因完成判断。

    这是「无候选也能完成判断」的界面取证：入口默认展开（没有候选就不该让它藏起来），
    保存后回执与数据库都把来源标成「教师填写」，与「采纳候选」可区分。
    """
    section("13.14 R1-02 无候选也能人工判断：入口可见 → 保存 → 来源标「教师填写」")

    long_id = ctx["long_answer"]
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class2_id"], long_id))
    cdp.wait_selector(".check-bar", 25)
    opened = cdp.evaluate(
        "(() => { const d = document.querySelector('.manual-cause');"
        " const btn = Array.from(document.querySelectorAll('.manual-cause .btn'))"
        "   .find((b) => b.textContent.trim() === '保存人工判断');"
        " const input = document.querySelector('.manual-label');"
        " return {has: !!d, open: d ? d.open : null, hasBtn: !!btn, hasInput: !!input,"
        "         candidates: document.querySelectorAll('.cause-item').length}; })()")
    check("13.14.1 真实作答没有候选时，人工填写入口默认展开且有「保存人工判断」",
          opened["has"] and opened["open"] is True and opened["hasBtn"] and opened["hasInput"]
          and opened["candidates"] == 0, opened)

    label = "人工判断：去括号漏乘"
    cdp.fill("input.manual-label", label)
    cdp.click(".manual-cause .btn", "保存人工判断", settle=1.0)
    ok = cdp.wait_selector(".receipt", 25)
    page = cdp.evaluate(
        "(() => ({receipt: (document.querySelector('.receipt') || {innerText: ''}).innerText,"
        " error: (document.querySelector('.banner.danger') || {innerText: ''}).innerText}))()")
    receipt_text = (page or {}).get("receipt") or ""
    check("13.14.2 人工判断回执：教师已判断 + 来源「教师填写」",
          bool(ok) and ("教师已判断" in receipt_text) and ("教师填写" in receipt_text),
          {"receipt": receipt_text[:200], "error": (page or {}).get("error")})
    rows = db_query("SELECT cause_state, cause_source, cause_label, progress"
                    " FROM answer_checks WHERE answer_id=?", (long_id,))
    check("13.14.3 库里落的是 cause_state=decided / cause_source=teacher 且只有一条当前结论",
          len(rows) == 1 and rows[0]["cause_state"] == "decided"
          and rows[0]["cause_source"] == "teacher" and rows[0]["cause_label"] == label
          and rows[0]["progress"] == "checked", rows)
    path, size = cdp.screenshot("ui-r1-02-manual-cause-1366x768.png")
    check("13.14.4 人工判断截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))


def stage_basis_required(cdp, ctx):
    """T11：规则判不了的作答（out_of_scope）人工改判必须先写判断依据。

    真正的闸门在服务端（400 basis_required，见 150 套件 11b）；这里证界面把「必填」说清楚、
    同一页对应的保存真的会被拒、且 first_bad 页不受打扰（A5 快路径不显示必填）。
    """
    section("13.16 T11 依据必填：out_of_scope 页提示必填，first_bad 页保持可选")

    status, created = ctx["http"].post("/api/classes", {"name": "七年级 4 班（依据必填取证）"})
    class4 = created.get("id") if status == 200 else None
    check("13.16.0 造一个取证班级（测试夹具，不是产品路径）", bool(class4), created)
    if not class4:
        return
    scope_id = insert_scope_answer(class4)
    detail = ctx["http"].get("/api/classes/%d/check/answers/%d" % (class4, scope_id))[1]
    check("13.16.0b 造数自检：这份作答 applicability=optional（规则判不了）",
          bool(scope_id) and detail.get("applicability") == "optional"
          and (detail.get("locate") or {}).get("status") == "out_of_scope",
          (scope_id, detail.get("applicability"), (detail.get("locate") or {}).get("status")))

    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, class4, scope_id))
    cdp.wait_selector(".judge-card", 25)
    page = cdp.evaluate(
        "(() => { const card = document.querySelector('.judge-card');"
        " const labels = Array.from(card.querySelectorAll('.lab')).map((el) => el.textContent.trim());"
        " const hint = card.querySelector('.basis-hint');"
        " const area = card.querySelector('textarea[placeholder]');"
        " return {labels: labels, hint: hint ? hint.textContent.trim() : '',"
        "         placeholder: area ? area.getAttribute('placeholder') : '',"
        "         scrollWidth: document.documentElement.scrollWidth,"
        "         innerWidth: window.innerWidth}; })()")
    check("13.16.1 out_of_scope 页：「判断依据」标签写明必填 + 有一句解释提示",
          any("必填" in label for label in page["labels"])
          and "basis_required" in (page["hint"] or "")
          and "必填" in (page["placeholder"] or ""), page)
    check("13.16.2 提示不改变布局约束（1366 宽无横向滚动）",
          page["scrollWidth"] <= page["innerWidth"] == 1366,
          (page["scrollWidth"], page["innerWidth"]))
    status_bad, payload = ctx["http"].post(
        "/api/classes/%d/check/answers/%d/save" % (class4, scope_id),
        {"expected_version": (detail.get("answer") or {}).get("version", ""),
         "observed_step": "", "first_bad_index": None,
         "cause_state": "decided", "cause_label": "人工判断：不等式方向写反"})
    check("13.16.3 同一页对应的保存真的被拒（400 basis_required，提示不是装饰）",
          status_bad == 400 and (payload or {}).get("code") == "basis_required",
          (status_bad, (payload or {}).get("code")))

    # 记成「无需判断」之后不该再要求依据（老师并没有形成原因判断）
    status_na, payload_na = ctx["http"].post(
        "/api/classes/%d/check/answers/%d/save" % (class4, scope_id),
        {"expected_version": (detail.get("answer") or {}).get("version", ""),
         "observed_step": "", "first_bad_index": None, "cause_state": "undecided"})
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, class4, scope_id))
    cdp.wait_selector(".judge-card", 25)
    after_na = cdp.evaluate(
        "(() => { const card = document.querySelector('.judge-card');"
        " const labels = Array.from(card.querySelectorAll('.lab')).map((el) => el.textContent.trim());"
        " return {hint: !!card.querySelector('.basis-hint'), labels: labels}; })()")
    check("13.16.4 记成「无需判断」后不再要求依据（提示消失，标签回到可选）",
          status_na == 200
          and ((payload_na.get("judgement") or {}).get("cause_state") == "not_applicable")
          and after_na["hint"] is False
          and any("可选" in label for label in after_na["labels"]),
          {"save": status_na, "judgement": (payload_na.get("judgement") or {}).get("cause_state"),
           "page": after_na})

    long_id = ctx["long_answer"]
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class2_id"], long_id))
    cdp.wait_selector(".judge-card", 25)
    bad_page = cdp.evaluate(
        "(() => { const card = document.querySelector('.judge-card');"
        " const labels = Array.from(card.querySelectorAll('.lab')).map((el) => el.textContent.trim());"
        " return {labels: labels, hint: !!card.querySelector('.basis-hint')}; })()")
    check("13.16.5 first_bad 页不显示必填（A5 快路径保持可选）",
          bad_page["hint"] is False
          and any("可选" in label for label in bad_page["labels"]), bad_page)


def stage_other_status(cdp, ctx):
    section("13.13 A2 补充：正确作答不标错；只有答案显示「缺少步骤」且不猜行")

    ok_id = ctx["answers"]["DEMO-OK-01"]
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class_id"], ok_id))
    cdp.wait_selector(".step-line", 25)
    body = cdp.evaluate("document.querySelector('.check-body').innerText") or ""
    check("13.13.1 正确作答页没有任何 bad 行（不标错）", cdp.count(".step-line.bad") == 0,
          cdp.count(".step-line.bad"))
    check("13.13.2 正确作答页没有「首处不等价」文字标记", cdp.count(".step-mark") == 0,
          cdp.count(".step-mark"))
    check("13.13.3 正确作答页说明「全程等价 … 不标错步」",
          "全程等价" in body and "不标错步" in body, body[:160])
    check("13.13.4 正确作答页仍只有一个 .btn.primary", cdp.count(".btn.primary") == 1,
          cdp.count(".btn.primary"))

    missing_id = ctx["answers"]["DEMO-C-01"]
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, ctx["class_id"], missing_id))
    cdp.wait_selector(".step-line", 25)
    body = cdp.evaluate("document.querySelector('.check-body').innerText") or ""
    check("13.13.5 只有答案的作答不标错（没有 bad 行）", cdp.count(".step-line.bad") == 0,
          cdp.count(".step-line.bad"))
    check("13.13.6 只有答案显示「缺少步骤」并说明不猜错在哪一行",
          "缺少步骤" in body and "不猜" in body, body[:200])
    check("13.13.7 只有答案仍能保存处理进度（主按钮在）",
          cdp.texts(".check-bar .btn.primary") == ["保存错步，原因待判断"],
          cdp.texts(".check-bar .btn.primary"))
    path, size = cdp.screenshot("ui-a2-missing-steps-1366x768.png")
    check("13.13.8 缺少步骤截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))


def stage_reverify(cdp, ctx):
    section("15. T11 聚焦复验：A7 演示入口 / A6 四态与回执首屏 / 提交唯一 / toast / 记录列 / A1 空状态与用词")

    class2 = ctx["class2_id"]
    long_id = ctx["long_answer"]

    def state_metrics(label):
        metrics = cdp.evaluate(
            "(() => ({label: %s,"
            " overflowX: document.documentElement.scrollWidth - window.innerWidth,"
            " scrollWidth: document.documentElement.scrollWidth,"
            " innerWidth: window.innerWidth,"
            " primary: document.querySelectorAll('.btn.primary').length}))()" % json.dumps(label))
        log("A6 态测量 %s：%s" % (label, json.dumps(metrics, ensure_ascii=False)))
        return metrics

    # ---------------- A7：只经界面按钮载入/重置合成演示数据 ----------------
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check" % (PORT, class2))
    ok = cdp.wait_selector(".demo-card", 25)
    demo_text = cdp.evaluate("document.querySelector('.demo-card').innerText") or ""
    check("15.1 A7 总览页有「合成演示数据」卡，写明不需要选文件/配模型/等网络",
          bool(ok) and "演示不需要选文件、配模型或等网络" in demo_text, demo_text[:200])
    check("15.2 A7 卡上写明「只影响合成数据，真实导入的作答不受影响」",
          "只影响合成数据，真实导入的作答不受影响" in demo_text, demo_text[:240])
    demo_buttons = cdp.texts(".demo-card .btn")
    check("15.3 A7 卡上有「载入合成演示数据」「重置合成数据」两个可见按钮",
          demo_buttons == ["载入合成演示数据", "重置合成数据"], demo_buttons)

    before = db_query("SELECT COUNT(*) AS n FROM answers WHERE class_id=?", (class2,))[0]["n"]
    cdp.click(".demo-card .btn", "载入合成演示数据", settle=1.0)
    notice = cdp.wait_js(
        "(() => { const el = document.querySelector('.demo-card .banner.info');"
        " return el ? el.textContent.trim() : ''; })()", 20)
    after = db_query("SELECT COUNT(*) AS n FROM answers WHERE class_id=?", (class2,))[0]["n"]
    sources = db_query("SELECT source, COUNT(*) AS n FROM answers WHERE class_id=? GROUP BY source",
                       (class2,))
    # 原断言：`"新增 4 份" in notice`。
    # 原依据：演示数据只有 4 条，页面文案按 `seeded.answers` 生成。
    # 新断言：`"新增 %d 份" % len(build())`（精确文案，不是「包含某个数字」）。
    # 改写依据：R1-T12 演示数据 4 → 6 条（其中 2 条落在第二演示批次），份数以 build() 为准。
    demo_count = len(_demo_answers.build()["answers"])
    check("15.4 A7 点「载入合成演示数据」→ 页面显示 seeded 数字（新增 %d 份）" % demo_count,
          ("新增 %d 份" % demo_count) in (notice or ""), notice)
    # 原断言：`after - before == 4` 且「合成演示」行数 == 4。
    # 原依据：4 条演示作答。
    # 新断言：`after - before == len(build())` 且「合成演示」行数 == len(build())（仍精确相等）。
    # 改写依据：R1-T12 演示数据 4 → 6 条。
    check("15.5 A7 载入后库里确实多出 %d 份合成作答（%d → %d）" % (demo_count, before, after),
          after - before == demo_count
          and any(row["source"] == "合成演示" and row["n"] == demo_count for row in sources), sources)
    hero_text = cdp.evaluate("document.querySelector('.check-hero').innerText") or ""
    # R1-01 改写（依据：任务书 R1-01 第 6 条「演示数据不进待核对队列、不影响 overview 的
    # expected」、基线审查 §14.2 P1「resolve_batch 无 source 过滤，演示可抢占『当前作业』」）。
    # 原 15.6 要求载入演示后演示批次直接顶替「当前作业」并显示 4 份待核对——那正是本轮要修的污染：
    # 这个班级此时有真实作业「长作答作业」，演示数据没有资格把它挤掉。
    # 现在断言：① 当前作业仍是真实作业；② 演示批次作为单独一行出现在作业表里；
    # ③ 从作业表进入演示批次后，才是演示的 4 份待核对。
    check("15.6 A7 载入后「当前作业」仍是真实作业（演示批次不抢占当前作业）",
          "长作答作业" in hero_text and ASSIGNMENT_NAME not in hero_text, hero_text[:200])
    batch_rows = cdp.texts(".check-overview table tbody td:nth-child(1)")
    check("15.6a A7 作业表里单独列出演示批次（4/0/4 那一行）",
          any(ASSIGNMENT_NAME in (cell or "") for cell in batch_rows), batch_rows)
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check?batch=%s"
                 % (PORT, class2, urllib.parse.quote(ASSIGNMENT_NAME)))
    demo_hero = cdp.wait_js(
        "(() => { const el = document.querySelector('.check-hero');"
        " return el ? el.innerText : ''; })()", 25) or ""
    ctx["pending_hero"] = demo_hero
    check("15.6b A7 演示批次页：待核对作答 4 份 + 主按钮「查看待核对作答」",
          "待核对作答 4 份" in demo_hero
          and cdp.texts(".check-hero .btn.primary") == ["查看待核对作答"], demo_hero[:200])
    path, size = cdp.screenshot("ui-a7-demo-seeded-1366x768.png")
    check("15.7 A7 载入截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))

    # ---------------- 提交唯一 + A4 跳过 + A6 回执首屏 + toast ----------------
    cdp.click(".check-hero .btn.primary", "查看待核对作答", settle=0.5)
    cdp.wait_selector(".step-line", 25)
    bar_texts = cdp.texts(".check-bar .check-actions .btn")
    adopt_texts = cdp.texts(".adopt-row .btn")
    check("15.8 提交动作唯一化：底部操作栏只有一个提交按钮",
          bar_texts == ["保存错步，原因待判断"], bar_texts)
    adopt_box = cdp.box(".adopt-row .btn", "采纳选中原因并保存")
    check("15.9「采纳选中原因并保存」在候选区、可点、不是 primary",
          adopt_texts == ["采纳选中原因并保存"] and bool(adopt_box)
          and adopt_box["w"] > 0 and cdp.count(".btn.primary") == 1,
          {"adopt": adopt_texts, "box": adopt_box, "primary": cdp.count(".btn.primary")})

    hash_before = cdp.hash()
    cdp.click(".check-ask .check-actions .btn", "暂时跳过", settle=0.6)
    toast_text = cdp.evaluate("(() => { const el = document.getElementById('toast');"
                              " return el ? el.textContent : ''; })()")
    check("15.10 A4 回归：点「暂时跳过」当场给出提示（后面验它不会跨页残留）",
          "已跳过提问" in (toast_text or ""), toast_text)
    cdp.click(".check-bar .btn.primary", "保存错步，原因待判断", settle=0.8)
    cdp.wait_selector(".receipt", 25)
    check("15.11 A5 回归：保存后仍停在原作答（hash 不变）", cdp.hash() == hash_before, cdp.hash())
    receipt_text = cdp.evaluate("document.querySelector('.receipt').innerText") or ""
    check("15.12 A5 回归：回执仍有「已保存到：」与「两轴状态：已核对 · 原因待判断」",
          "已保存到：" in receipt_text and "两轴状态：已核对 · 原因待判断" in receipt_text,
          receipt_text[:200])
    page_text = cdp.evaluate("document.body.innerText") or ""
    check("15.13 toast 不跨页：回执页不再出现「已跳过提问，未记录回答」",
          "已跳过提问，未记录回答" not in page_text, page_text[:120])

    receipt_metrics = cdp.evaluate(
        "(() => { const a = document.querySelector('.receipt .btn.primary');"
        " if (!a) return null; const r = a.getBoundingClientRect();"
        " return {text: a.textContent.trim(), top: Math.round(r.top),"
        "         bottom: Math.round(r.bottom), vh: window.innerHeight,"
        "         scrollY: Math.round(window.scrollY),"
        "         overflowX: document.documentElement.scrollWidth - window.innerWidth,"
        "         primary: document.querySelectorAll('.btn.primary').length}; })()")
    log("A6 回执态测量：%s" % json.dumps(receipt_metrics, ensure_ascii=False))
    check("15.14 A6 回执态「查看下一份」在 1366×768 首屏可见（top<768）",
          receipt_metrics is not None and receipt_metrics["text"] == "查看下一份"
          and receipt_metrics["top"] < 768, receipt_metrics)
    check("15.15 A6 回执态无横向滚动且 .btn.primary=1",
          receipt_metrics is not None and receipt_metrics["overflowX"] <= 0
          and receipt_metrics["primary"] == 1, receipt_metrics)

    # 【R-05 同轮追加·sticky 遮挡】回执态滚到最大位置：正文最后一张卡片不得被操作栏盖住
    cdp.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    cdp.sleep(0.2)
    receipt_cover = cdp.evaluate(
        "(() => { const bar = document.querySelector('.receipt .check-bar');"
        " const cards = document.querySelectorAll('.receipt > .card');"
        " const last = cards[cards.length - 1];"
        " const rb = last ? last.getBoundingClientRect() : null;"
        " const rbar = bar ? bar.getBoundingClientRect() : null;"
        " let hit = null;"
        " if (rb) { const x = rb.left + Math.min(40, rb.width / 2);"
        "   const y = rb.top + Math.min(20, Math.max(1, rb.height - 4));"
        "   const el = document.elementFromPoint(x, y);"
        "   hit = el ? {inside: last.contains(el), tag: el.tagName} : null; }"
        " return {lastBottom: rb ? Math.round(rb.bottom) : null,"
        "         barTop: rbar ? Math.round(rbar.top) : null,"
        "         selector: '.receipt > .card:last-of-type',"
        "         scrollY: Math.round(window.scrollY), hit: hit}; })()")
    log("A6 回执态遮挡检查：%s" % json.dumps(receipt_cover, ensure_ascii=False))
    check("15.15b 【sticky 遮挡·回执】滚到底后最后一张卡片 bottom <= 操作栏 top",
          receipt_cover["lastBottom"] is not None and receipt_cover["barTop"] is not None
          and receipt_cover["lastBottom"] <= receipt_cover["barTop"] + 1,
          receipt_cover)
    check("15.15c 【sticky 遮挡·回执】最后一张卡片里的元素仍能 elementFromPoint 命中",
          receipt_cover["hit"] and receipt_cover["hit"]["inside"] is True, receipt_cover["hit"])
    cdp.evaluate("window.scrollTo(0, 0)")

    path, size = cdp.screenshot("ui-a6-receipt-first-screen-1366x768.png")
    check("15.16 A6 回执首屏截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))

    # ---------------- 采纳仍落 decided + cause_code ----------------
    cdp.click(".receipt .btn.primary", "查看下一份", settle=0.5)
    cdp.wait_selector(".cause-item", 25)
    answer_id = int(cdp.hash().rsplit("/", 1)[-1])
    cdp.click_radio(0)
    cdp.click(".adopt-row .btn", "采纳选中原因并保存", settle=0.8)
    cdp.wait_selector(".receipt", 25)
    adopted = db_query("SELECT cause_state, cause_code FROM answer_checks WHERE answer_id=?",
                       (answer_id,))
    adopt_receipt = cdp.evaluate("document.querySelector('.receipt').innerText") or ""
    check("15.17 采纳仍落 cause_state=decided 且 cause_code 有值（一次真实保存）",
          len(adopted) == 1 and adopted[0]["cause_state"] == "decided"
          and bool(adopted[0]["cause_code"]), adopted)
    check("15.18 采纳回执显示「教师已判断」", "教师已判断" in adopt_receipt, adopt_receipt[:200])

    # 【R-04·T14 并行】decided 态主按钮应改为「保存错步（沿用已判断原因）」，
    # 另给次级「改为原因待判断」。T14 未落地时记未验证，不判本次失败。
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, class2, answer_id))
    cdp.wait_selector(".check-bar", 25)
    decided_ui = cdp.evaluate(
        "(() => { const bar = document.querySelector('.check-bar');"
        " const judge = document.querySelector('.judge-card');"
        " const primary = bar ? bar.querySelector('.btn.primary') : null;"
        " const buttons = bar ? Array.from(bar.querySelectorAll('.btn, button'))"
        "   .map((b) => b.textContent.trim()) : [];"
        " const judgeButtons = judge ? Array.from(judge.querySelectorAll('.btn, button'))"
        "   .map((b) => b.textContent.trim()) : [];"
        " return {primary: primary ? primary.textContent.trim() : null,"
        "         buttons: buttons, judgeButtons: judgeButtons}; })()")
    log("R-04 decided 态按钮：%s" % json.dumps(decided_ui, ensure_ascii=False))
    if decided_ui and "沿用已判断原因" in (decided_ui.get("primary") or ""):
        check("15.18b R-04 decided 态主按钮文案为「保存错步（沿用已判断原因）」",
              True, decided_ui)
        check("15.18d0 R-04「改为原因待判断」是「教师判断」卡里的次级动作（不在底部主操作栏）",
              "改为原因待判断" in (decided_ui.get("judgeButtons") or []), decided_ui)
        decided_before = db_query(
            "SELECT cause_state, cause_code, cause_history FROM answer_checks WHERE answer_id=?",
            (answer_id,))[0]
        cdp.click(".check-bar .btn.primary", decided_ui["primary"], settle=0.8)
        cdp.wait_selector(".receipt", 25)
        kept = db_query("SELECT cause_state, cause_code FROM answer_checks WHERE answer_id=?",
                        (answer_id,))
        check("15.18c R-04 decided 态点主按钮仍 decided 且 cause_code 不变",
              len(kept) == 1 and kept[0]["cause_state"] == "decided"
              and kept[0]["cause_code"] == decided_before["cause_code"]
              and bool(kept[0]["cause_code"]), {"before": decided_before, "after": kept})
        cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d" % (PORT, class2, answer_id))
        cdp.wait_selector(".check-bar", 25)
        cdp.click(".judge-card .btn", "改为原因待判断", settle=0.8)
        cdp.wait_selector(".receipt", 25)
        flipped = db_query(
            "SELECT cause_state, cause_code, cause_history FROM answer_checks WHERE answer_id=?",
            (answer_id,))
        history = json.loads(flipped[0]["cause_history"] or "[]") if flipped else []
        history_before = len(json.loads(decided_before["cause_history"] or "[]"))
        check("15.18d R-04「改为原因待判断」→ undecided 且 cause_history +1",
              len(flipped) == 1 and flipped[0]["cause_state"] == "undecided"
              and flipped[0]["cause_code"] == ""
              and len(history) == history_before + 1,
              {"before": history_before, "after": len(history)})
    else:
        hold("R-04 decided 态主按钮文案与「改为原因待判断」",
             "等 T14 落地：当前 decided 态主按钮仍是「%s」，底部栏按钮 %s"
             % ((decided_ui or {}).get("primary"), (decided_ui or {}).get("buttons")))


    # ---------------- 记录页：作业批次列 + 时间格式 + toast + 四态计数 ----------------
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/records?cause_state=all" % (PORT, class2))
    ok = cdp.wait_selector(".check-records table", 25)
    records_text = cdp.evaluate("document.body.innerText") or ""
    check("15.19 toast 不跨页：记录页不再出现「已跳过提问，未记录回答」",
          "已跳过提问，未记录回答" not in records_text)
    heads = cdp.texts(".check-records thead th")
    check("15.20 记录表含「作业批次」列", "作业批次" in heads, heads)
    batch_cells = cdp.texts(".check-records tbody td:nth-child(3)")
    batch_visible = cdp.evaluate(
        "(() => { const th = Array.from(document.querySelectorAll('.check-records thead th'))"
        "  .find((el) => el.textContent.trim() === '作业批次');"
        " return th ? th.getBoundingClientRect().width > 0 : false; })()")
    check("15.21 作业批次列在 1366 宽度下可见且有值",
          batch_visible is True and bool(batch_cells)
          and all(item and item != '—' for item in batch_cells),
          {"visible": batch_visible, "cells": batch_cells})
    times = cdp.texts(".check-records tbody td:nth-child(7)")
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
    check("15.22 保存时间是 YYYY-MM-DD HH:MM，不是原始 ISO 串",
          bool(times) and all(pattern.match(item or "") for item in times)
          and not any("T" in (item or "") for item in times), times)
    open_buttons = cdp.evaluate(
        "(() => Array.from(document.querySelectorAll('.check-records tbody tr')).map((tr) => {"
        " const b = tr.querySelector('button'); return b ? b.textContent.trim() : null; }))()")
    check("15.22b 记录行内「打开」是按钮，不是纯文字",
          bool(open_buttons) and all(item == "打开" for item in open_buttons), open_buttons)
    records_metrics = state_metrics("记录")
    check("15.23 A6 记录态无横向滚动且 .btn.primary=1",
          records_metrics["overflowX"] <= 0 and records_metrics["primary"] == 1, records_metrics)

    # ---------------- A7 重置：只删合成数据，真实导入的作答与判断都还在 ----------------
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check" % (PORT, class2))
    cdp.wait_selector(".demo-card", 25)
    cdp.click(".demo-card .btn", "重置合成数据", settle=1.0)
    reset_notice = cdp.wait_js(
        "(() => { const el = document.querySelector('.demo-card .banner.info');"
        " return el ? el.textContent.trim() : ''; })()", 20)
    left = db_query("SELECT record_key, source FROM answers WHERE class_id=?", (class2,))
    kept_check = db_query("SELECT COUNT(*) AS n FROM answer_checks WHERE answer_id=?", (long_id,))
    # 原断言：`"删除 4 份作答" in reset_notice`。
    # 原依据：演示数据只有 4 条，重置会把它们一起删掉。
    # 新断言：`"删除 %d 份作答" % len(build())`（精确文案）。
    # 改写依据：R1-T12 演示数据 4 → 6 条、两个演示批次——顺带证明「两个批次的演示行一起被清」。
    check("15.24 A7 点「重置合成数据」→ 页面显示 deleted/kept 数字（删除 %d 份）" % demo_count,
          ("删除 %d 份作答" % demo_count) in (reset_notice or "")
          and "真实导入的作答保留 1 份" in (reset_notice or ""), reset_notice)
    check("15.25 A7 重置只删合成数据：班里只剩那条真实导入作答",
          len(left) == 1 and left[0]["record_key"] == "LONG-1"
          and left[0]["source"] == "真实导入", left)
    check("15.26 A7 重置没有动真实导入作答的判断（check 仍在）",
          kept_check[0]["n"] == 1, kept_check)
    path, size = cdp.screenshot("ui-a7-demo-reset-1366x768.png")
    check("15.27 A7 重置截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))

    # ---------------- A6 详情态 ----------------
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check/%d"
                 % (PORT, ctx["class_id"], ctx["answers"]["DEMO-A-01"]))
    cdp.wait_selector(".check-bar", 25)
    detail_metrics = state_metrics("详情")
    check("15.28 A6 详情态无横向滚动且 .btn.primary=1",
          detail_metrics["overflowX"] <= 0 and detail_metrics["primary"] == 1, detail_metrics)

    # 【sticky 遮挡·详情】同样在最大滚动位置验证操作栏没盖住正文
    cdp.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    cdp.sleep(0.2)
    detail_cover = cdp.evaluate(
        "(() => { const bar = document.querySelector('.check-bar');"
        " const cards = document.querySelectorAll('.check-body .card');"
        " const last = cards[cards.length - 1];"
        " const rb = last ? last.getBoundingClientRect() : null;"
        " const rbar = bar ? bar.getBoundingClientRect() : null;"
        " let hit = null;"
        " if (rb) { const x = rb.left + Math.min(40, rb.width / 2);"
        "   const y = rb.top + Math.min(20, Math.max(1, rb.height - 4));"
        "   const el = document.elementFromPoint(x, y);"
        "   hit = el ? {inside: last.contains(el), tag: el.tagName} : null; }"
        " return {lastBottom: rb ? Math.round(rb.bottom) : null,"
        "         barTop: rbar ? Math.round(rbar.top) : null,"
        "         selector: '.check-body .card:last-of-type',"
        "         scrollY: Math.round(window.scrollY), hit: hit}; })()")
    log("A6 详情态遮挡检查：%s" % json.dumps(detail_cover, ensure_ascii=False))
    check("15.28b 【sticky 遮挡·详情】滚到底后最后一张卡片 bottom <= 操作栏 top",
          detail_cover["lastBottom"] is not None and detail_cover["barTop"] is not None
          and detail_cover["lastBottom"] <= detail_cover["barTop"] + 1, detail_cover)
    check("15.28c 【sticky 遮挡·详情】最后一张卡片里的元素仍能 elementFromPoint 命中",
          detail_cover["hit"] and detail_cover["hit"]["inside"] is True, detail_cover["hit"])
    cdp.evaluate("window.scrollTo(0, 0)")


    # ---------------- A1 空状态：主按钮可直接点并指向导入 ----------------
    status, created = ctx["http"].post("/api/classes", {"name": "七年级 3 班（空状态取证）"})
    class3 = created.get("id") if status == 200 else None
    check("15.29 造一个空班级用于 A1 空状态取证（测试夹具，不是产品路径）", bool(class3), created)
    if class3:
        cdp.navigate("http://127.0.0.1:%d/#/class/%s/check" % (PORT, class3))
        cdp.wait_selector(".check-hero", 25)
        empty = cdp.evaluate(
            "(() => { const p = document.querySelector('.check-hero .btn.primary');"
            " const r = p ? p.getBoundingClientRect() : null;"
            " return {tag: p ? p.tagName : null, text: p ? p.textContent.trim() : null,"
            "         href: p ? p.getAttribute('href') : null,"
            "         disabled: p ? !!p.disabled : null,"
            "         w: r ? Math.round(r.width) : 0, h: r ? Math.round(r.height) : 0,"
            "         hero: (document.querySelector('.check-hero') || {}).innerText || ''}; })()")
        log("A1 空状态主按钮：%s" % json.dumps(empty, ensure_ascii=False))
        check("15.30 A1(a) 空状态主按钮是可点的链接（不再是 disabled 按钮）",
              empty["tag"] == "A" and empty["w"] > 0 and empty["h"] > 0 and not empty["disabled"],
              empty)
        check("15.31 A1(a) 空状态主按钮逐字「导入作答」且指向 #/upload?class={id}",
              empty["text"] == "导入作答" and empty["href"] == "#/upload?class=%s" % class3, empty)
        check("15.32 A1 空状态不再显示「当前作业：未命名批次」",
              "当前作业：" not in (empty["hero"] or ""), empty["hero"][:200])
        empty_metrics = state_metrics("no_answers")
        check("15.33 A6 空状态无横向滚动且 .btn.primary=1",
              empty_metrics["overflowX"] <= 0 and empty_metrics["primary"] == 1, empty_metrics)
        path, size = cdp.screenshot("ui-a1-empty-primary-1366x768.png")
        check("15.34 A1 空状态截图已保存且尺寸正确", size == VIEWPORT, "%s %s" % (path, size))

    # ---------------- A1(b) 用词不再同名不同义 ----------------
    cdp.navigate("http://127.0.0.1:%d/#/classes" % PORT)
    cdp.wait_selector(".class-card", 25)
    card_texts = cdp.texts(".class-card .stat-inline")
    joined_cards = " | ".join(card_texts)
    check("15.35 A1(b) 班级卡用「待确认错题 / 已确认错题」，不再出现孤立的「待核对」",
          "待确认错题" in joined_cards and "已确认错题" in joined_cards
          and "待核对" not in joined_cards, card_texts)
    cdp.navigate("http://127.0.0.1:%d/#/class/%d/check" % (PORT, ctx["class_id"]))
    cdp.wait_selector(".check-hero", 25)
    overview_text = cdp.evaluate("document.querySelector('.check-overview').innerText") or ""
    overview_heads = cdp.texts(".check-overview thead th")
    pending_hero = ctx.get("pending_hero") or ""
    # all_checked 时不渲染 kv-row，所以「待核对作答」的取证落在两处：
    # ① 有作答待核对那次总览的 hero 文案（15.6 抓的）；② 作业表的「待核对作答」表头（非空班级都在）。
    log("A1(b) 取证：hero=%r 表头=%r 班级卡=%r" % (pending_hero[:80], overview_heads, card_texts))
    has_answer_word = ("待核对作答" in pending_hero) or ("待核对作答" in overview_heads)
    check("15.36 A1(b) 作答核对页用「待核对作答」，不再出现班级卡那一侧的错题口径词",
          has_answer_word and "待确认错题" not in overview_text,
          {"hero": pending_hero[:80], "heads": overview_heads,
           "has_mistake_word": "待确认错题" in overview_text})
    check("15.37 A1(b) 两处用词不同且各自可解释（错题 vs 作答）",
          "待确认错题" in joined_cards and has_answer_word,
          {"cards": card_texts, "hero": pending_hero[:80], "heads": overview_heads})


def stage_t12_flows(cdp, ctx):
    """R1-T12：五条新增能力的网页入口逐条点通（批次 / 人工核对 / 补录校正 / 追问复核 / 演示数据）。

    为什么自成一体：这条 stage 排最后，前面的 A5/13.14/15.x 已经在演示作答上改过结论与数据，
    所以这里先 reset 再 seed，从干净的演示数据起步——演示页本来就有这两个按钮，不算后门。
    """
    section("13.17 R1-T12：五条新增能力的网页入口（批次选择 / 人工核对 / 补录校正 / 追问复核 / 演示数据）")

    http = ctx["http"]
    class_id = ctx["class_id"]
    base_url = "http://127.0.0.1:%d" % PORT

    status, reset = http.post("/api/classes/%d/check/demo/reset" % class_id)
    status, seeded = http.post("/api/classes/%d/check/demo/seed" % class_id)
    demo_count = len(_demo_answers.build()["answers"])
    demo_batches = list(_demo_answers.DEMO_BATCHES)
    seeded_info = seeded.get("seeded") or {}
    check("13.17.0 一键载入演示数据：%d 份、%d 个演示批次，逐批次都有作答"
          % (demo_count, len(demo_batches)),
          status == 200 and seeded_info.get("answers") == demo_count
          and len(seeded.get("batches") or []) == len(demo_batches)
          and all(n > 0 for n in (seeded_info.get("by_batch") or {}).values()),
          {"seeded": seeded_info, "batches": seeded.get("batches")})

    def read_selector(selector, timeout=25, settle=0.35):
        """先等元素出现、再稳定一下再读：切批次会整页重渲染，读太早会拿到重渲染中途的空 DOM。"""
        cdp.wait_selector(selector, timeout)
        cdp.sleep(settle)
        return cdp.evaluate(
            "(() => { const el = document.querySelector(%s);"
            " return el ? el.innerText : null; })()" % json.dumps(selector))

    def wait_text(selector, needle, timeout=20):
        """等到「元素里真的出现这句话」再返回文本；等不到也返回最后一次读到的文本（由断言判红）。"""
        last = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            last = cdp.evaluate(
                "(() => { const el = document.querySelector(%s);"
                " return el ? el.innerText : ''; })()" % json.dumps(selector)) or ""
            if needle in last:
                return last
            cdp.sleep(0.25)
        return last

    def set_batch(value):
        """切批次下拉：先确认下拉还在（重渲染中会短暂消失），再改值并派发 change。"""
        cdp.wait_selector(".batch-bar .batch-select", 25)
        return cdp.evaluate(
            "(() => { const s = document.querySelector('.batch-bar .batch-select');"
            " if (!s) return 'no-select'; s.value = %s; s.dispatchEvent(new Event('change'));"
            " return s.value; })()" % json.dumps(value))

    ids = {row["record_key"]: row["id"] for row in db_query(
        "SELECT id, record_key FROM answers WHERE class_id=? AND source='合成演示'", (class_id,))}
    check("13.17.0b 六份演示作答的 id 已取到（供本段导航用）",
          set(ids) == {item["record_key"] for item in _demo_answers.build()["answers"]}, ids)

    # ---------------- 流程⑤：批次选择（总览） ----------------
    cdp.navigate(base_url + "/#/class/%d/check" % class_id)
    cdp.wait_selector(".batch-bar .batch-select", 25)
    overview_body = cdp.evaluate("(() => document.querySelector('.check-overview').innerText)()")
    options = cdp.evaluate(
        "(() => Array.from(document.querySelectorAll('.batch-bar .batch-select option'))"
        ".map((o) => o.value + '|' + o.textContent.trim()))()")
    check("13.17.1 总览页有批次选择：当前作业 + 全部批次 + 每个演示批次都在选项里",
          len(options) >= 2 and any(o.startswith("|") or "当前作业" in o for o in options)
          and any("*all*" in o for o in options)
          and all(any(b in o for o in options) for b in demo_batches), options)
    check("13.17.1b 选择器下方写明「当前作业」与「全部批次」的区别",
          "全部批次" in overview_body and "分母不同" in overview_body, overview_body[:160])

    # 切到「全部批次」：标题变成合计，主按钮变成「查看核对记录」
    check("13.17.2a 下拉切到「全部批次」后页面重新渲染（下拉可用）",
          set_batch("*all*") == "*all*")
    hero_all = wait_text(".check-hero", "全部批次")
    body_all = wait_text(".check-overview", "分母不同")
    check("13.17.2 切到「全部批次」：标题与合计数字随之变化，并说明与「当前作业」的分母不同",
          "全部批次" in hero_all and "合计" in hero_all and "作业数" in hero_all
          and "分母不同" in body_all, hero_all[:200])
    path, size = cdp.screenshot("t12-1-batches-overview-1366x768.png")
    check("13.17.2b 「全部批次」总览截图（1366×768）",
          size == VIEWPORT and os.path.isfile(path), "%s %s" % (path, size))

    # 切到第二个演示批次：本批演示行必须可见（不是 0/0）
    second_batch = demo_batches[-1]
    second_pending = db_query(
        "SELECT COUNT(*) AS n FROM answers WHERE class_id=? AND batch=? AND source='合成演示'",
        (class_id, second_batch))[0]["n"]
    check("13.17.3a 下拉切到第二个演示批次（值已生效）",
          set_batch(second_batch) == second_batch)
    hero2 = wait_text(".check-hero", second_batch)
    check("13.17.3 切到第二个演示批次：本批演示行可见（待核对 %d 份，不是 0/0）" % second_pending,
          ("待核对作答 %d 份" % second_pending) in hero2, hero2[:200])
    path, size = cdp.screenshot("t12-2-demo-batch2-1366x768.png")
    check("13.17.3b 第二个演示批次截图（1366×768）", size == VIEWPORT, size)

    # 真实批次里看不到任何演示行（库与接口双向对账）
    assignments = (http.get("/api/classes/%d/check/overview" % class_id)[1] or {}).get("assignments") or []
    real_rows = [item for item in assignments if not item.get("synthetic")]
    leaks = []
    for item in real_rows:
        leaked = db_query(
            "SELECT COUNT(*) AS n FROM answers WHERE class_id=? AND batch=? AND source='合成演示'",
            (class_id, item["batch"]))[0]["n"]
        if leaked:
            leaks.append((item["batch"], leaked))
    check("13.17.4 真实批次里看不到任何演示行（逐批次对账）",
          not leaks and all(item["batch"] not in demo_batches for item in real_rows),
          {"leaks": leaks, "real": [item["batch"] for item in real_rows]})

    # ---------------- 流程⑤续：记录页批次选择 ----------------
    cdp.navigate(base_url + "/#/class/%d/check/records?cause_state=all" % class_id)
    cdp.wait_selector(".batch-bar .batch-select", 25)
    records_options = cdp.evaluate(
        "(() => Array.from(document.querySelectorAll('.batch-bar .batch-select option'))"
        ".map((o) => o.value + '|' + o.textContent.trim()))()")
    records_note = cdp.evaluate("(() => { const el = document.querySelector('.batch-note');"
                                " return el ? el.textContent.trim() : ''; })()")
    check("13.17.5 记录页有批次选择：默认「全部批次」并写明范围",
          any("全部批次" in o for o in records_options) and "全部批次" in records_note,
          {"options": records_options, "note": records_note})

    # ---------------- 流程②：规则判不了时的人工核对 ----------------
    oos_id = ids.get("DEMO-OOS-01")
    cdp.navigate(base_url + "/#/class/%d/check/%d" % (class_id, oos_id))
    cdp.wait_selector(".manual-check-entry", 25)
    entry_text = wait_text(".manual-check-entry", "人工核对并记录错误")
    check("13.17.6 out_of_scope 作答页出现「人工核对并记录错误」入口",
          "人工核对并记录错误" in entry_text and "规则判不了" in entry_text, entry_text[:160])

    cdp.click(".manual-check-entry .btn", "人工核对并记录错误", settle=0.5)
    opened = cdp.evaluate(
        "(() => { const d = document.querySelector('.manual-cause');"
        " const area = document.querySelector('.judge-card textarea');"
        " return {open: d ? d.open : null, basisFocused: document.activeElement === area}; })()")
    check("13.17.7 点入口后展开人工填写区，并把焦点放到「判断依据」",
          opened.get("open") is True and opened.get("basisFocused") is True, opened)

    # 先制造一次「依据为空」的拒绝：填原因不填依据
    cdp.fill(".manual-label", "不等式方向写反")
    cdp.click(".manual-cause .btn", "保存人工判断", settle=0.8)
    err_text = cdp.evaluate(
        "(() => { const el = document.querySelector('.check-bar .banner.danger');"
        " return el && !el.hidden ? el.textContent.trim() : ''; })()")
    check("13.17.8 依据为空时保存被拒：页面写明要填判断依据（服务端 400 basis_required）",
          ("判断依据" in err_text or "basis_required" in err_text), err_text[:160])

    cdp.fill(".judge-card textarea", "作业本原图第二行写的是 2x>2，他抄成 x<1（依据逐字取自原图）")
    cdp.click(".manual-cause .btn", "保存人工判断", settle=1.0)
    ok = cdp.wait_selector(".receipt", 25)
    receipt = wait_text(".receipt", "教师已判断") if ok else ""
    check("13.17.9 填好依据后保存成功：回执显示「教师已判断」与「教师填写」来源",
          bool(ok) and "教师已判断" in receipt and "教师填写" in receipt, receipt[:200])
    # 统计/记录可单独看到这一档（记录页按原因筛选能找到，且不再挂「原因待判断」）
    records = (http.get("/api/classes/%d/check/records?cause_state=decided&batch=%s"
                        % (class_id, urllib.parse.quote(second_batch)))[1] or {}).get("items") or []
    decided_ids = [item["answer_id"] for item in records]
    check("13.17.9b 这条结论在「记录找回」里按「教师已判断」可查（不再永远挂账）",
          oos_id in decided_ids, {"decided": decided_ids, "oos": oos_id})
    path, size = cdp.screenshot("t12-3-manual-check-1366x768.png")
    check("13.17.9c 人工核对截图（1366×768）", size == VIEWPORT, size)

    # ---------------- 流程③：补录 / 校正原始作答 ----------------
    c_id = ids.get("DEMO-C-01")
    cdp.navigate(base_url + "/#/class/%d/check/%d" % (class_id, c_id))
    cdp.wait_selector(".check-edit", 25)
    cdp.evaluate("(() => { const d = document.querySelector('.check-edit'); d.open = true; return d.open; })()")
    edit_visible = cdp.evaluate(
        "(() => { const d = document.querySelector('.check-edit');"
        " const area = d ? d.querySelector('textarea') : null;"
        " return {open: d ? d.open : null, steps: area ? area.value : ''}; })()")
    check("13.17.10 缺步骤作答提供「补录 / 校正原始作答」入口，且预填当前学生步骤",
          edit_visible.get("open") is True and "x=1" in (edit_visible.get("steps") or ""), edit_visible)

    before_version = ((http.get("/api/classes/%d/check/answers/%d" % (class_id, c_id))[1] or {})
                      .get("answer") or {}).get("version", "")
    cdp.fill(".check-edit textarea", "4(x-1)=8\nx-1=2\nx=3")
    cdp.click(".check-edit .btn", "保存补录并重新分析", settle=1.2)
    cdp.wait_selector(".check-body .banner.info", 25)
    notice = wait_text(".check-body .banner.info", "已按新内容重新分析")
    check("13.17.11 补录保存后页内回执给出旧/新版本号，并说明本份回到待核对",
          "已按新内容重新分析" in notice and "旧版本" in notice and "待核对" in notice, notice[:200])
    after_edit = http.get("/api/classes/%d/check/answers/%d" % (class_id, c_id))[1] or {}
    after_steps = (after_edit.get("answer") or {}).get("student_steps") or ""
    # 这份（DEMO-C-01「只有答案」）之前没有任何结论，所以这里不预设 stale；
    # 要验的是「版本换了 + 按新内容重新分析 + 回到待核对」三件事。
    check("13.17.12 补录后按新内容重新分析：版本变化、定位重算成 correct、本份回到待核对",
          "x=3" in after_steps
          and (after_edit.get("answer") or {}).get("version") not in ("", before_version)
          and (after_edit.get("locate") or {}).get("status") == "correct"
          and (after_edit.get("judgement") or {}).get("progress") == "pending",
          {"steps": after_steps[:40],
           "version_changed": (after_edit.get("answer") or {}).get("version") != before_version,
           "locate": (after_edit.get("locate") or {}).get("status"),
           "progress": (after_edit.get("judgement") or {}).get("progress")})
    check("13.17.12b 演示作答被校正后明确说明「预置已失效 / 重置可恢复」",
          "预置" in notice and "重置" in notice, notice[:220])
    path, size = cdp.screenshot("t12-4-supplement-1366x768.png")
    check("13.17.12c 补录校正截图（1366×768）", size == VIEWPORT, size)

    # ---------------- 流程④：追问 + 证据更新后的复核 ----------------
    mv_id = ids.get("DEMO-MV-01")
    cdp.navigate(base_url + "/#/class/%d/check/%d" % (class_id, mv_id))
    cdp.wait_selector(".check-ask", 25)
    ask = cdp.evaluate(
        "(() => { const card = document.querySelector('.check-ask');"
        " const line = card.querySelector('.ask-line');"
        " const input = card.querySelector('.ask-input');"
        " return {line: line ? line.textContent.trim() : '', value: input ? input.value : ''}; })()")
    check("13.17.13 移项题的追问句按本条错步生成（不含「括号」），并给出来源标注",
          "括号" not in ask["line"] and ("移" in ask["line"] or "符号" in ask["line"])
          and bool(ask["value"]), ask)

    cdp.evaluate("(() => { const d = document.querySelector('.check-ask');"
                 " const b = Array.from(d.querySelectorAll('.check-actions .btn'))"
                 "   .find((el) => el.textContent.indexOf('记录学生回答') >= 0); b.click(); return b.textContent; })()")
    cdp.sleep(0.3)
    cdp.fill(".check-ask .follow-wrap textarea", "我以为 7 移过去还是 7。")
    cdp.click(".check-bar .btn.primary", "保存错步，原因待判断", settle=1.2)
    ok = cdp.wait_selector(".receipt", 25)
    receipt1 = wait_text(".receipt", "学生原话") if ok else ""
    check("13.17.14 记录学生回答后回执给出问句与来源（预置问句可由老师改写）",
          bool(ok) and "学生原话" in receipt1 and "问：" in receipt1, receipt1[:200])

    # 改关键证据（错步）→ 旧确认失效，回执出现「依据已更新，需复核」
    cdp.navigate(base_url + "/#/class/%d/check/%d" % (class_id, mv_id))
    cdp.wait_selector(".observed-input", 25)
    # 复核演示的关键：老师改了依据，但**没有**重新记录同一句原话。页面默认会把上次的原话
    # 预填回输入框，直接保存等于「在新证据下重新确认同一条」（T5 的正确行为），演不出失效；
    # 所以先点「暂时跳过」把原话清空，保存时不带 followup。
    cdp.click(".check-ask .check-actions .btn", "暂时跳过", settle=0.3)
    # headless 下 insertText 偶尔落到别的元素上，observed_step 会变成空串——那样「证据没变」，
    # 回执当然不会提示复核。这里直接写值 + 派发 input，并回读确认，避免把测试自己搞成假阴性。
    written = cdp.evaluate(
        "(() => { const el = document.querySelector('.observed-input');"
        " if (!el) return null; el.focus(); el.value = '2x=15+7（原图这一行下面还划了线）';"
        " el.dispatchEvent(new Event('input', {bubbles: true})); return el.value; })()")
    check("13.17.15a 观察到的错步已改成与规则不同的写法（人工核验）",
          written == "2x=15+7（原图这一行下面还划了线）", written)
    cdp.fill(".judge-card textarea", "作业本原图上 7 没有变号，这一行下面还划了线")
    cdp.click(".check-bar .btn.primary", "保存错步，原因待判断", settle=1.2)
    ok = cdp.wait_selector(".receipt", 25)
    if ok:
        cdp.sleep(0.4)
    receipt2 = wait_text(".receipt", "需复核") if ok else ""
    check("13.17.15 改了关键错步后回执提示「依据已更新，需复核」",
          bool(ok) and "需复核" in receipt2 and "依据" in receipt2, receipt2[:240])
    follow = (http.get("/api/classes/%d/check/answers/%d" % (class_id, mv_id))[1] or {}).get("followup") or {}
    check("13.17.15b 旧确认不再算「已记录」：recorded=false、needs_review=true",
          follow.get("recorded") is False and follow.get("needs_review") is True, follow)
    path, size = cdp.screenshot("t12-5-followup-review-1366x768.png")
    check("13.17.15c 追问复核截图（1366×768）", size == VIEWPORT, size)

    # ---------------- 不做视觉重构：三页都要在 1366 内，且主按钮唯一 ----------------
    metrics = []
    for name, path_hash in (("总览", "/#/class/%d/check" % class_id),
                            ("记录", "/#/class/%d/check/records?cause_state=all" % class_id),
                            ("核对", "/#/class/%d/check/%d" % (class_id, ids.get("DEMO-MV-01")))):
        cdp.navigate(base_url + path_hash)
        cdp.wait_selector(".check-hero, .check-body, .check-records", 25)
        cdp.sleep(0.8)
        item = cdp.evaluate(
            "(() => ({overflowX: document.documentElement.scrollWidth - window.innerWidth,"
            " primary: document.querySelectorAll('.btn.primary').length,"
            " hash: location.hash,"
            " bar: Array.from(document.querySelectorAll('.check-bar .btn, .check-bar a.btn'))"
            "   .map((el) => el.textContent.trim())}))()")
        item["label"] = name
        log("T12 三页度量 %s：%s" % (name, json.dumps(item, ensure_ascii=False)[:200]))
        metrics.append(item)
    # 核对页在「保存回执」与「详情」两种形态下都只有一个 primary；这里按「≤1 且 ≥1」的
    # 语义核：每页必须恰好 1 个，且不能出现第二个（A6 的「一屏一个主动作」）。
    check("13.17.16 三页在 1366×768 下无横向滚动、主按钮仍唯一",
          all(item["overflowX"] <= 0 for item in metrics)
          and all(item["primary"] == 1 for item in metrics), metrics)

    # ---------------- 隔离最后一根钉子：真实行冒用演示批名，reset 也不能删 ----------------
    # 口径：reset 三条同时成立才删（source='合成演示' 且 record_key 带 DEMO- 前缀 且 批次属于
    # DEMO_BATCHES）。这里故意把一条真实导入的作答**批名写成主演示批次**、record_key 不带前缀，
    # 重置后它必须还在；演示行必须全清。
    conn = sqlite3.connect(DB_PATH, timeout=15)
    keep_id = None
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, 1, "REAL-IN-DEMO-BATCH", demo_batches[0], "真学生",
                 "", "1", "3(x-2)=12", "一元一次方程", "x=6", "3(x-2)=12\nx=6",
                 "真实导入", "2026-11-20", 0, "", "fp-real-in-demo-batch", "2026-11-20T08:00:00+08:00",
                 "2026-11-20T08:00:00+08:00", "2026-11-20T08:00:00+08:00"))
            keep_id = cur.lastrowid
    finally:
        conn.close()

    http.post("/api/classes/%d/check/demo/reset" % class_id)
    kept_rows = db_query("SELECT id, record_key, batch, source FROM answers WHERE id=?", (keep_id,))
    demo_left = db_query(
        "SELECT COUNT(*) AS n FROM answers WHERE class_id=? AND source='合成演示'", (class_id,))[0]["n"]
    check("13.17.17 真实作答冒用演示批名时，reset 也不删它（三条口径少一条都不删）",
          bool(kept_rows) and kept_rows[0]["record_key"] == "REAL-IN-DEMO-BATCH"
          and kept_rows[0]["source"] == "真实导入" and demo_left == 0,
          {"kept": kept_rows, "demo_left": demo_left})

    # 这条只是本段的取证夹具，用完删掉，别把真实班级弄脏
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        with conn:
            conn.execute("DELETE FROM answers WHERE id=?", (keep_id,))
    finally:
        conn.close()


def stage_old_tabs(cdp, ctx):
    section("13.12 旧五个 tab 回归：overview / students / mistakes / stats / advice")

    tabs = [
        ("overview", ["待确认错题", "已确认错题"]),
        ("students", ["学生名单"]),
        ("mistakes", ["全部", "已确认"]),
        ("stats", ["统计"]),
        ("advice", ["建议"]),
    ]
    for tab, keywords in tabs:
        cdp.navigate("http://127.0.0.1:%d/#/class/%d/%s" % (PORT, ctx["class_id"], tab))
        cdp.wait_selector("#app .card, #app .tabs", 25)
        cdp.sleep(1.2)
        text = cdp.evaluate("document.querySelector('#app').innerText") or ""
        problem = None
        if "出错了" in text:
            problem = "页面渲染出错了"
        elif "加载中…" in text:
            problem = "一直停在加载中"
        missing = [word for word in keywords if word not in text]
        check("13.12.%d 旧 tab %s 正常渲染（关键词 %s 都在，console 无未捕获异常）"
              % (tabs.index((tab, keywords)) + 1, tab, "/".join(keywords)),
              problem is None and not missing and not cdp.exceptions,
              {"problem": problem, "missing": missing, "exceptions": cdp.exceptions[:3],
               "console": cdp.console_errors[:3]})


# --------------------------------------------------------------------------
# 造数
# --------------------------------------------------------------------------
def insert_long_answer(class_id, lines=15):
    """直接往库里放一份 15 行长作答：真实导入接口本轮不造这种数据，这里只为 A6 的折叠/滚动取证。"""
    import hashlib
    from datetime import datetime

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with conn:
            cur = conn.execute(
                "INSERT INTO students(class_id, name, code, note, active, created_at, updated_at)"
                " VALUES (?,?,?,?,1,?,?)", (class_id, "学生戊", "", "", stamp, stamp))
            student_id = cur.lastrowid
        steps = "\n".join(["3(x-2)=12"] + ["3x-%d=%d" % (index + 2, 12) for index in range(lines - 1)])
        payload = {
            "student_id": int(student_id),
            "question_no": "9",
            "question_text": "3(x-2)=12",
            "unit": "一元一次方程",
            "standard_solution": "3x-6=12；3x=18；x=6",
            "student_steps": steps,
            "occurred_on": "2026-11-24",
            "missing": 0,
            "missing_reason": "",
        }
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                                     .encode("utf-8")).hexdigest()
        with conn:
            cur = conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, student_id, "LONG-1", "长作答作业", "学生戊", "", "9",
                 "3(x-2)=12", "一元一次方程", "3x-6=12；3x=18；x=6", steps, "真实导入",
                 "2026-11-24", 0, "", fingerprint, stamp, stamp, stamp))
            return cur.lastrowid
    finally:
        conn.close()


def insert_scope_answer(class_id):
    """T11：直接往库里放一份「规则判不了」的作答（1 时不等式），用于依据必填提示取证。

    真实导入接口本轮不造这种数据；这里只为「out_of_scope 页面的必填提示」取证，
    与 insert_long_answer 同一套写法（自己算内容指纹，保证保存时版本一致）。
    """
    import hashlib
    from datetime import datetime

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with conn:
            cur = conn.execute(
                "INSERT INTO students(class_id, name, code, note, active, created_at, updated_at)"
                " VALUES (?,?,?,?,1,?,?)", (class_id, "学生己", "", "", stamp, stamp))
            student_id = cur.lastrowid
        steps = "2x+1>3\n2x>2\nx<1"
        payload = {
            "student_id": int(student_id),
            "question_no": "10",
            "question_text": "2x+1>3",
            "unit": "不等式",
            "standard_solution": "x>1",
            "student_steps": steps,
            "occurred_on": "2026-11-24",
            "missing": 0,
            "missing_reason": "",
        }
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                                     .encode("utf-8")).hexdigest()
        with conn:
            cur = conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, student_id, "SCOPE-1", "核验范围作业", "学生己", "", "10",
                 "2x+1>3", "不等式", "x>1", steps, "真实导入",
                 "2026-11-24", 0, "", fingerprint, stamp, stamp, stamp))
            return cur.lastrowid
    finally:
        conn.close()


def main():
    reset_work()
    os.makedirs(SHOTS, exist_ok=True)

    log("数智伴学｜作答核对网页：浏览器端到端（Edge headless + CDP，1366×768）")
    for line in environment_lines():
        log(line)
    import hashlib as _hashlib
    for name in ("src/web/app.js", "src/web/styles.css", "src/app/equation_steps.py",
                 "src/app/demo_answers.py", "src/app/answer_check.py"):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                log("被测文件 %s sha256=%s mtime=%s size=%d" % (
                    name, _hashlib.sha256(fh.read()).hexdigest()[:12].upper(),
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
                    os.path.getsize(path)))
    log("服务：http://127.0.0.1:%d（数据目录 %s）" % (PORT, WORK))
    log("截图目录：%s" % SHOTS)

    browser = None
    server = None
    thread = None
    try:
        try:
            server = server_mod.make_server("127.0.0.1", PORT, WORK, use_demo=True)
        except OSError as exc:
            log("端口 %d 不可用：%s（契约 §6 规定 8899 归 verify 专用，请先释放端口）" % (PORT, exc))
            check("13.0 服务能在 8899 上启动", False, str(exc))
            raise SystemExit(1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        check("13.0 服务在 127.0.0.1:%d 上启动（演示数据已种）" % PORT, True)

        http = HTTP("http://127.0.0.1:%d" % PORT)
        status, payload = http.post("/api/auth/login", {"username": "demo", "password": "demo1234"})
        check("13.0.1 演示账号能登录", status == 200 and http.token, status)
        if not http.token:
            raise RuntimeError("登录没有拿到会话令牌")

        classes = http.get("/api/classes")[1].get("items") or []
        target = next((item for item in classes if item.get("name") == CLASS_NAME), None) or classes[0]
        class_id = target["id"]
        class_name = target["name"]
        log("演示班级：%s (id=%d)" % (class_name, class_id))

        status, seeded = http.post("/api/classes/%d/check/demo/seed" % class_id)
        # 原断言：`seeded["seeded"]["answers"] == 4`。
        # 原依据：演示数据只有 A/B/C/OK 四条。
        # 新断言：`== len(_demo_answers.build()["answers"])`（精确相等，不是 >=）。
        # 改写依据：R1-T12 演示数据扩展——4 条 → 6 条、两个演示批次（correct / first_bad /
        #   missing_steps / out_of_scope 各 ≥1，另有「人工核对」「补录校正」「追问复核」样本）。
        demo_count = len(_demo_answers.build()["answers"])
        check("13.0.2 合成演示数据已载入（%d 份，与 build() 同数）" % demo_count, status == 200
              and seeded.get("seeded", {}).get("answers") == demo_count, seeded)
        answers = {row["record_key"]: row["id"] for row in db_query(
            "SELECT id, record_key FROM answers WHERE class_id=? AND source='合成演示'"
            " ORDER BY id", (class_id,))}
        log("合成作答 id：%s" % answers)
        # 原断言：`set(answers) == {"DEMO-A-01", "DEMO-B-01", "DEMO-C-01", "DEMO-OK-01"}`。
        # 原依据：演示数据只有这四条 record_key。
        # 新断言：与 `build()` 里的 record_key 集合**完全相等**（仍然精确，不是「至少包含」）。
        # 改写依据：R1-T12 扩展为 6 条（新增 DEMO-OOS-01 超范围 / DEMO-MV-01 移项追问），
        #   集合同样由 build() 决定，避免以后每加一条演示数据就改一次测试。
        expect_keys = {item["record_key"] for item in _demo_answers.build()["answers"]}
        check("13.0.3 合成作答齐备（%d 份、record_key 与 build() 完全一致）" % len(expect_keys),
              set(answers) == expect_keys, sorted(answers))

        status, created = http.post("/api/classes", {"name": "七年级 2 班", "grade": "七年级"})
        check("13.0.4 新建一个空班级（no_answers 取证用）", status == 200 and created.get("id"),
              created)
        class2_id = created.get("id")

        browser = Browser()
        cdp = browser.cdp
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable")
        cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": VIEWPORT[0], "height": VIEWPORT[1], "deviceScaleFactor": 1, "mobile": False})
        login(cdp, http.token)

        ctx = {
            "class_id": class_id, "class_name": class_name, "assignment": ASSIGNMENT_NAME,
            "answers": answers, "first_answer": answers["DEMO-A-01"], "class2_id": class2_id,
            "http": http, "long_answer": None,
        }

        stages = [
            ("A1 班级入口", lambda: stage_a1_classes(cdp, ctx)),
            ("A2 首处错步", lambda: stage_a2(cdp, ctx)),
            ("A3 三区块与两轴", lambda: stage_a3(cdp, ctx)),
            ("A4 暂时跳过", lambda: stage_a4_skip(cdp, ctx)),
            ("A5a 主按钮保存", lambda: stage_a5_save(cdp, ctx)),
            ("A5b 采纳原因", lambda: stage_a5_adopt(cdp, ctx)),
            ("A5c 改判", lambda: stage_a5_rejudge(cdp, ctx)),
            ("A5 记录找回", lambda: stage_records(cdp, ctx)),
            ("A6 视觉与投影", lambda: stage_a6(cdp, ctx)),
            ("首屏两态", lambda: stage_states(cdp, ctx)),
        ]
        for name, stage in stages:
            try:
                stage()
            except Exception:
                RESULTS["fail"] += 1
                log("FAIL 阶段「%s」中断：%s" % (name, traceback.format_exc().splitlines()[-1]))
                log(traceback.format_exc())

        # 长作答必须先建出来才能跑 A6 与保存失败两段，所以放在两态截图之后单独建
        try:
            ctx["long_answer"] = insert_long_answer(class2_id)
            log("长作答 id=%s（15 行）" % ctx["long_answer"])
            check("13.0.5 长作答已写入（15 行，用于折叠与横向滚动取证）",
                  ctx["long_answer"] is not None, ctx["long_answer"])
        except Exception:
            RESULTS["fail"] += 1
            log("FAIL 造长作答失败：%s" % traceback.format_exc().splitlines()[-1])

        for name, stage in [("A6 长作答", lambda: stage_a6_long(cdp, ctx)),
                            ("保存失败留页", lambda: stage_save_failure(cdp, ctx)),
                            ("R1-02 人工判断", lambda: stage_manual_cause(cdp, ctx)),
                            ("T11 聚焦复验", lambda: stage_reverify(cdp, ctx)),
                            ("A2 其余状态", lambda: stage_other_status(cdp, ctx)),
                            ("T12 五条入口", lambda: stage_t12_flows(cdp, ctx)),
                            ("旧五 tab 回归", lambda: stage_old_tabs(cdp, ctx)),
                            ("T11 依据必填", lambda: stage_basis_required(cdp, ctx))]:
            try:
                stage()
            except Exception:
                RESULTS["fail"] += 1
                log("FAIL 阶段「%s」中断：%s" % (name, traceback.format_exc().splitlines()[-1]))
                log(traceback.format_exc())
    finally:
        if browser is not None:
            browser.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=10)

    log("")
    log("合计 %d 项，通过 %d，失败 %d（另：未验证 %d 项；已知缺口 %d 项，见上面「未通过（缺口）」行）"
        % (RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"], RESULTS["fail"],
           RESULTS["hold"], RESULTS["gap"]))
    log("截图清单：%s" % ", ".join(sorted(os.listdir(SHOTS))))
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
