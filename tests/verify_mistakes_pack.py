"""桌面版错题管理：最终交付 ZIP 解压后的 EXE 实测。

流程：检查 ZIP 内容 → 解压到临时目录 → 干净环境启动 EXE → 用真实窗口完成注册、
建班、建学生、录入错题（附照片）、看列表与详情、看原图并缩放、修改保存、
关闭重开后复查、换账号确认看不到别人的数据 → 检查没有开浏览器、没有监听端口。

用法：.venv\\Scripts\\python.exe -X utf8 tests\\verify_mistakes_pack.py
"""
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "数智伴学"
VERSION = (os.environ.get("PACK_TEST_VERSION") or "1.4.4").strip()
ZIP = ROOT / "build" / (NAME + "-" + VERSION + ".zip")
SHIPPED = ROOT / "build" / NAME
WORK = ROOT / "tests" / ".tmp-test" / ("pack-mistakes-" + uuid.uuid4().hex[:6])
PROG_ROOT = WORK / "解压件"
PROG = PROG_ROOT / NAME
EXE = PROG / (NAME + ".exe")
DATA = WORK / "数据 主库"
PORTABLE = ROOT / "dist" / "数智伴学" / "数智伴学.exe"
PHOTOS = WORK / "照片 目录 带空格"
SHOTS = Path(os.environ.get("PACK_TEST_SHOTS") or (ROOT / "docs" / "screenshots")).expanduser().resolve()
TITLE = "数智伴学 · 桌面版"
TITLE_STUDENTS = "数智伴学 · 学生名单"
TITLE_MISTAKES = "数智伴学 · 错题"
TITLE_STATS = "数智伴学 · 学情统计"
TITLE_CLASS_STATS = "数智伴学 · 班级学情统计"
PNG_SIG = b"\x89PNG\r\n\x1a\n"

import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
from pywinauto import Application  # noqa: E402
from pywinauto import mouse as pwmouse  # noqa: E402

results = []
procs = []
spawned_pids = set()
_apps = {}
PROFILE_PATH = WORK / "profile"


class Tee:
    """同时写控制台和 UTF-8 日志，避免 PowerShell 重定向把中文写坏。"""

    def __init__(self, path):
        self.stream = open(str(path), "w", encoding="utf-8", newline="\n")
        self.console = sys.stdout

    def write(self, text):
        self.console.write(text)
        self.stream.write(text)
        self.stream.flush()
        return len(text)

    def flush(self):
        self.console.flush()
        self.stream.flush()

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"


def note(message):
    print("[info] %s" % message, flush=True)


def check(name, ok, detail=""):
    text = str(detail).replace("\r", " ").replace("\n", " / ") if detail != "" else ""
    results.append((name, bool(ok), text))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" ｜ " + text) if text else ""), flush=True)
    return ok


def sha256_of(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)
    except Exception:
        pass


def stop(proc):
    if proc is None:
        return
    if proc.poll() is None:
        kill_tree(proc.pid)
        try:
            proc.wait(20)
        except Exception:
            pass


def clean_env(extra=None):
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("PYTHON") or upper in ("VIRTUAL_ENV", "CONDA_PREFIX"):
            continue
        env[key] = value
    env["PATH"] = r"C:\Windows\System32"
    env["TEMP"] = env["TMP"] = str(WORK / "temp")
    env["USERPROFILE"] = str(PROFILE_PATH)
    env["APPDATA"] = str(PROFILE_PATH / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(PROFILE_PATH / "AppData" / "Local")
    for key in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    env["MISTAKE_DESKTOP_NO_DIALOG"] = "1"
    if extra:
        env.update(extra)
    return env


def free_port():
    """挑一个现在能绑定、且没有旧监听的本机端口。

    Windows 在 SO_REUSEADDR 下允许两个进程绑同一端口，因此 bind 成功不代表没人
    在听；之前遗留的桩进程会悄悄接管新请求，这里再连一次确认。
    """
    for candidate in (0, 8765, 8766, 8767):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", candidate))
            port = sock.getsockname()[1]
        except OSError:
            continue
        finally:
            sock.close()
        if not _someone_listening(port):
            return port
    return 0


def _someone_listening(port, attempts=3, timeout=0.4):
    """连三次都失败才算空闲，避免旧桩偶发不响应时误判。"""
    for _ in range(attempts):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(timeout)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            continue
        finally:
            probe.close()
    return False


AI_STUB = {"proc": None, "port": 0, "log": None}


def start_ai_stub():
    """启动本地 OpenAI 兼容桩；只供自动化测试，不访问真实云端。"""
    stop_ai_stub()
    port = free_port()
    log_path = WORK / "ai-stub-requests.jsonl"
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "ai_stub_server.py"),
         "--port", str(port), "--log", str(log_path)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    AI_STUB.update({"proc": proc, "port": port, "log": log_path})
    deadline = time.time() + 30
    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1)
            sock.connect(("127.0.0.1", port))
            return port
        except OSError:
            time.sleep(0.2)
        finally:
            sock.close()
    raise RuntimeError("本地 AI 桩没有启动")


def stop_ai_stub():
    proc = AI_STUB.get("proc")
    if proc is not None and proc.poll() is None:
        kill_tree(proc.pid)
        try:
            proc.wait(10)
        except Exception:
            pass
    AI_STUB.update({"proc": None, "port": 0, "log": None})


def spawn(args, tag, env=None):
    log_dir = WORK / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / (tag + ".out"), "wb")
    err = open(log_dir / (tag + ".err"), "wb")
    proc = subprocess.Popen([str(EXE)] + list(args), cwd=str(PROG),
                            env=env if env is not None else clean_env(),
                            stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    proc.log_tag = tag
    procs.append(proc)
    spawned_pids.add(proc.pid)
    return proc


def read_log(tag):
    for stream in ("out", "err"):
        try:
            raw = (WORK / "logs" / (tag + "." + stream)).read_bytes()
        except OSError:
            continue
        for encoding in ("utf-8", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
    return ""


def attach(proc, timeout=90):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("进程已退出，退出码 %s\n%s" % (proc.returncode, read_log(proc.log_tag)))
        try:
            app = Application(backend="uia").connect(process=proc.pid, timeout=3)
            app.windows()
            return app
        except Exception as exc:
            last = str(exc)
            time.sleep(0.5)
    raise RuntimeError("连接不到进程窗口：%s\n%s" % (last, read_log(proc.log_tag)))


def window_pid(window):
    try:
        pid = window.element_info.process_id
    except Exception:
        pid = None
    if pid:
        return pid
    try:
        import win32process
        return win32process.GetWindowThreadProcessId(window.handle)[1]
    except Exception:
        return None


def scan(window, auto_id):
    try:
        for item in window.descendants():
            try:
                aid = item.element_info.automation_id or ""
            except Exception:
                continue
            if aid == auto_id or aid.endswith("." + auto_id):
                return item
    except Exception:
        pass
    return None


def ctl(window, auto_id, timeout=10):
    pid = window_pid(window)
    if not pid:
        raise RuntimeError("窗口已失效，找不到控件：%s" % auto_id)
    app = _apps.get(pid)
    if app is None:
        app = Application(backend="uia").connect(process=pid)
        _apps[pid] = app
    deadline = time.time() + timeout
    while True:
        try:
            spec = app.window(handle=window.handle).child_window(auto_id=auto_id)
            if spec.exists(timeout=0.2):
                return spec
        except Exception:
            pass
        item = scan(window, auto_id)
        if item is not None:
            return item
        if time.time() >= deadline:
            raise RuntimeError("找不到控件：%s" % auto_id)
        time.sleep(0.3)


def has_child(window, auto_id, timeout=0.5):
    try:
        ctl(window, auto_id, timeout=timeout)
        return True
    except Exception:
        return False


def find_window(app, title_part, require_id=None, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in app.windows():
            try:
                text = window.window_text()
            except Exception:
                continue
            if title_part not in text:
                continue
            if require_id and not has_child(window, require_id):
                continue
            return window
        time.sleep(0.4)
    raise RuntimeError("等不到窗口：%s（%s）" % (title_part, require_id or "任意"))


def native_visible(window):
    """UIA 包装对象会缓存可见性；这里直接问 Win32 窗口样式。"""
    try:
        return bool(win32gui.IsWindowVisible(window.handle))
    except Exception:
        return False


def wait_gone(app, title_part, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = False
        for window in app.windows():
            try:
                if title_part in window.window_text():
                    found = True
            except Exception:
                pass
        if not found:
            return True
        time.sleep(0.3)
    return False


def click(window, auto_id):
    ctl(window, auto_id).invoke()


def _top_window(pid, title_part):
    found = []

    def collect(hwnd, _):
        try:
            _, owner = win32process.GetWindowThreadProcessId(hwnd)
            if pid is not None and owner != pid:
                return True
            if not win32gui.IsWindowVisible(hwnd):
                return True
            text = win32gui.GetWindowText(hwnd)
            if title_part in text:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return found[0] if found else None


def message_click(window, point):
    """把一次左键点击作为窗口消息发进去。

    这台机器上 pywinauto 的鼠标注入到不了 Qt 窗口（勾选框点不动、表格行选不中），
    但窗口消息可以，所以表格行、勾选框这类只认鼠标的操作统一走这里。
    """
    import win32api

    pid = window.element_info.process_id
    hwnd = _top_window(pid, window.window_text()) or _top_window(None, window.window_text())
    if not hwnd:
        raise RuntimeError("找不到窗口句柄：%s" % window.window_text())
    x, y = win32gui.ScreenToClient(hwnd, point)
    position = win32api.MAKELONG(x, y)
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, position)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, position)
    time.sleep(0.05)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, position)
    time.sleep(0.3)


def click_center(widget, window):
    rect = widget.rectangle()
    message_click(window, ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2))


def click_table_text(window, auto_id, text):
    """按单元格文字点表格里的一行（表格行只响应鼠标，用窗口消息）。"""
    for child in ctl(window, auto_id).children():
        try:
            if (child.element_info.control_type or "") != "DataItem":
                continue
            if text not in (child.window_text() or ""):
                continue
            click_center(child, window)
            return True
        except Exception:
            continue
    return False


def _find_button(window, text):
    """在窗口（含挂在上面的对话框）里按按钮文字找控件；提示框按钮没有 automation_id。"""
    try:
        for item in window.descendants():
            try:
                if (item.element_info.control_type or "") != "Button":
                    continue
                if (item.window_text() or "").strip() == text:
                    return item
            except Exception:
                continue
    except Exception:
        pass
    return None


def wait_button_text(window, text, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _find_button(window, text) is not None:
            return True
        time.sleep(0.3)
    return False


def click_button_text(window, text, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        button = _find_button(window, text)
        if button is not None:
            button.invoke()
            return True
        time.sleep(0.3)
    raise RuntimeError("找不到按钮：%s" % text)


def start_analysis(window):
    click(window, "analyzeButton")
    if wait_button_text(window, "继续重新分析", timeout=5):
        click_button_text(window, "继续重新分析")


def fill(window, auto_id, value):
    widget = ctl(window, auto_id)
    try:
        widget.set_edit_text(value)
    except Exception:
        pass
    try:
        got = widget.get_value()
    except Exception:
        return True
    if not got or got == value:
        return True
    try:
        widget.click_input()
        widget.type_keys("^a{DEL}" + value, with_spaces=True)
    except Exception:
        return False
    return True


def fill_ascii(window, auto_id, value):
    """只用于地址/模型/密钥等纯 ASCII 字段，避免输入法把键盘输入组合成别的字。"""
    widget = ctl(window, auto_id)
    for _ in range(3):
        try:
            widget.set_edit_text(value)
        except Exception:
            pass
        try:
            if widget.get_value() == value:
                return True
        except Exception:
            return True
        time.sleep(0.1)
    return False


def text_of(window, auto_id, default=None):
    try:
        return ctl(window, auto_id, timeout=0).window_text()
    except Exception:
        return default


def value_of(window, auto_id, default=""):
    """读编辑框里的文字（QPlainTextEdit 的 window_text 可能为空，改读 UIA Value）。"""
    try:
        widget = ctl(window, auto_id, timeout=0)
    except Exception:
        return default
    try:
        got = widget.get_value()
        if got:
            return got
    except Exception:
        pass
    try:
        return widget.window_text() or default
    except Exception:
        return default


def wait_value(window, auto_id, needle, timeout=60):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = value_of(window, auto_id, "")
        if needle in last:
            return last
        time.sleep(0.3)
    return last


def wait_child(window, auto_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = scan(window, auto_id)
        if item is not None:
            return item
        time.sleep(0.3)
    raise RuntimeError("等不到控件：%s（窗口 %r）" % (auto_id, window.window_text()))


def wait_child_gone(window, auto_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if scan(window, auto_id) is None:
            return True
        time.sleep(0.3)
    return False


def open_mistakes(app, students, timeout=60):
    """点「查看错题」后返回可以操作的窗口：错题窗正常是独立窗口；
    某些情况下它挂在学生窗口的控件树里，这里两种都兼容。"""
    click(students, "openMistakesButton")
    try:
        return find_window(app, TITLE_MISTAKES, require_id="mistakeStudentName", timeout=timeout)
    except RuntimeError:
        pass
    wait_child(students, "mistakeStudentName", timeout=timeout)
    return students


def create_class(app, classes, name):
    click(classes, "newClassButton")
    wait_child(classes, "className", timeout=60)
    fill(classes, "className", name)
    click(classes, "createConfirm")
    return wait_child_gone(classes, "createConfirm", timeout=60)


def add_student(app, students, name):
    click(students, "newStudentButton")
    wait_child(students, "studentName", timeout=60)
    fill(students, "studentName", name)
    click(students, "studentConfirm")
    return wait_child_gone(students, "studentConfirm", timeout=60)


def wait_text(window, auto_id, needle, timeout=30):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = text_of(window, auto_id, "") or ""
        if needle in last:
            return last
        time.sleep(0.3)
    return last


def grab_window_pixels(hwnd):
    from ctypes import windll

    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("窗口尺寸异常：%dx%d" % (width, height))
    window_dc = win32gui.GetWindowDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(window_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source_dc, width, height)
    memory_dc.SelectObject(bitmap)
    try:
        windll.user32.PrintWindow(hwnd, memory_dc.GetSafeHdc(), 2)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    finally:
        for _cleanup in (
            lambda: win32gui.DeleteObject(bitmap.GetHandle()),
            lambda: memory_dc.DeleteDC(),
            lambda: source_dc.DeleteDC(),
            lambda: win32gui.ReleaseDC(hwnd, window_dc),
        ):
            try:
                _cleanup()
            except Exception:
                pass
    if image.getextrema() == ((0, 0), (0, 0), (0, 0)):
        raise RuntimeError("窗口画面是全黑")
    return image


def proc_windows(pid):
    """同一个进程里的可见顶层窗口（QDialog 是独立原生窗口，uia 的 windows() 里看不到）。"""
    found = []

    def visit(hwnd, _extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                return True
            found.append((hwnd, win32gui.GetWindowText(hwnd) or ""))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(visit, None)
    return found


def copy_auto_shot(tag, name, timeout=15):
    """对话框自己交出的画面（MISTAKE_SHOT_DIR/<objectName>.png），复制成正式截图名。"""
    source = SHOTS / ("%s.png" % tag)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if source.is_file() and time.time() - source.stat().st_mtime < 5:
                shutil.copyfile(str(source), str(SHOTS / name))
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def shot_window(pid, title_part, name):
    """按窗口标题截那个窗口本身：对话框的画面不在父窗口上，截父窗口会得到旧画面。"""
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        last = None
        for _ in range(4):
            try:
                for hwnd, title in proc_windows(pid):
                    if title_part in title:
                        grab_window_pixels(hwnd).save(str(SHOTS / name))
                        return True
                last = RuntimeError("找不到窗口：%s" % title_part)
            except Exception as exc:
                last = exc
            time.sleep(0.4)
        note("截图失败 %s：%s" % (name, last))
        return False
    except Exception as exc:
        note("截图失败 %s：%s" % (name, exc))
        return False


def shot(window, name):
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        last = None
        for _ in range(4):
            try:
                hwnd = int(window.handle)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
                grab_window_pixels(hwnd).save(str(SHOTS / name))
                return True
            except Exception as exc:
                last = exc
                time.sleep(0.4)
        note("截图失败 %s：%s" % (name, last))
        return False
    except Exception as exc:
        note("截图失败 %s：%s" % (name, exc))
        return False


def listening_ports(pid):
    try:
        data = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=60).stdout
    except Exception:
        return []
    ports = []
    for line in data.decode("gbk", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-1] == str(pid) and parts[3] == "LISTENING":
            ports.append(parts[1])
    return ports


def browser_parents():
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
              "'^(msedge|chrome|firefox|iexplore)\\.exe$' } | "
              "ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }")
    try:
        data = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, timeout=120).stdout
    except Exception:
        return {}
    pairs = {}
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            pairs[parts[0]] = parts[1]
    return pairs


def browsers_opened_by_us():
    """只统计父进程是本脚本启动过的浏览器，老师自己开的浏览器不算。"""
    mine = {int(pid) for pid in spawned_pids}
    return [pid for pid, parent in browser_parents().items() if parent.isdigit() and int(parent) in mine]


def make_photo(path):
    """用 PNG 头 + 随机像素生成一张真实可解码的照片（Qt 之外也能解码）。"""
    from PIL import Image
    import random

    random.seed(20261109)
    image = Image.new("RGB", (480, 360))
    pixels = image.load()
    for y in range(360):
        for x in range(480):
            pixels[x, y] = ((x * 3 + y) % 256, (y * 5) % 256, (x + y * 2) % 256)
    image.save(str(path), "PNG")
    return path


def black_hole():
    """一个只握手、不回话的本地端口：用来把「真实分析」拖到超时，制造迟到结果。

    它会记下两件事：收到请求的时刻，以及客户端放弃、连接关闭的时刻。
    这样「请求真的发出去了」和「任务真的结束了」都有证据，不用固定等待去猜。
    """
    srv = None
    port = 0
    # 端口不能落在 Windows 的保留段里（本题里 50000-50059 就是），
    # 否则连过去会立刻 WinError 10013，迟到就成了「立刻失败」，测不到要测的东西。
    errors = []
    # 先让系统分配端口：Windows 的排除端口段会变，固定候选可能全部连不通。
    candidates = [0] + list(range(18750, 18800)) + list(range(8800, 8830)) + [8765, 8766, 8767]
    for candidate in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", candidate))
            actual = sock.getsockname()[1]
            sock.listen(16)
            probe = socket.create_connection(("127.0.0.1", actual), timeout=3)
            caught, _addr = sock.accept()
            caught.close()
            probe.close()
        except OSError as exc:
            errors.append("%d:%r" % (candidate, exc))
            sock.close()
            continue
        srv = sock
        port = actual
        break
    if srv is None:
        raise SystemExit("找不到可用的本地延迟端口：" + " | ".join(errors[:5]))
    state = {
        "port": port,
        "server": srv,
        "stop": threading.Event(),
        "received": threading.Event(),
        "closed": threading.Event(),
        "hits": 0,
        "closed_count": 0,
        "received_at": None,
        "closed_at": None,
        "closed_times": [],
        "held": [],
    }

    def watch(conn):
        try:
            conn.settimeout(180)
            while True:
                if not conn.recv(4096):
                    break
        except Exception:
            pass
        state["closed_count"] += 1
        state["closed_times"].append(time.time())
        if state["closed_at"] is None:
            state["closed_at"] = state["closed_times"][-1]
        state["closed"].set()

    def loop():
        srv.settimeout(0.5)
        while not state["stop"].is_set():
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            state["hits"] += 1
            if state["received_at"] is None:
                state["received_at"] = time.time()
            state["received"].set()
            state["held"].append(conn)
            threading.Thread(target=watch, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return state


def read_mistake_row(data_dir):
    """直接读测试数据里这条错题的整行，用来比对记录有没有被改过。"""
    path = str(data_dir / "app.db")
    deadline = time.time() + 30
    while True:
        try:
            conn = sqlite3.connect(path, timeout=15)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM mistakes ORDER BY id LIMIT 1").fetchone()
            finally:
                conn.close()
            return {} if row is None else {key: row[key] for key in row.keys()}
        except sqlite3.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


def row_diff(before, after):
    """整行比对：返回所有发生变化的列名。"""
    keys = sorted(set(before) | set(after))
    return [key for key in keys if before.get(key) != after.get(key)]


def wait_received(state, count, timeout=60):
    """等到延迟服务真的收到第 count 个请求。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state["hits"] >= count:
            return True
        time.sleep(0.1)
    return False


def wait_closed(state, count, timeout=60):
    """等到第 count 个请求真的结束（客户端自己关掉了连接）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state["closed_count"] >= count:
            return True
        time.sleep(0.1)
    return False


def table_rows(window):
    """表格里的数据行数（Qt 表格的单元格在 UIA 里是 DataItem，没有 automation_id）。"""
    rows = 0
    try:
        for item in window.descendants():
            try:
                if (item.element_info.control_type or "") == "DataItem":
                    rows += 1
            except Exception:
                pass
    except Exception:
        pass
    return rows


def table_items(window, auto_id):
    try:
        table = ctl(window, auto_id, timeout=2)
        return [
            item for item in table.descendants()
            if (item.element_info.control_type or "") == "DataItem"
        ]
    except Exception:
        return []


def table_item_count(window, auto_id):
    """某个表格里的 DataItem 数（每行按列数计），空表为 0。"""
    try:
        ctl(window, auto_id, timeout=2)
    except Exception:
        return -1
    return len(table_items(window, auto_id))


def table_text(window, auto_id):
    return " | ".join(item.window_text() for item in table_items(window, auto_id))


def basis_state(window):
    title = text_of(window, "statsBasisTitle", "") or ""
    try:
        opened = ctl(window, "statsOpenMistakeButton", timeout=0).is_enabled()
    except Exception:
        opened = False
    return title, table_item_count(window, "statsBasisTable"), opened


def wait_stats_done(window, needle, timeout=90):
    """等到摘要出现指定文字且查询按钮已经恢复可点（本次查询的界面处理已完成）。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = text_of(window, "statsSummary", "") or ""
        if needle in last:
            try:
                if ctl(window, "statsQueryButton", timeout=0).is_enabled():
                    return last
            except Exception:
                pass
        time.sleep(0.2)
    return last


def wait_basis_loaded(window, name, timeout=30):
    """等到依据标题出现这一项、表格有行、打开按钮可点。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        title, cells, opened = basis_state(window)
        last = title
        if "依据错题：" in title and name in title and "正在加载" not in title and cells > 0 and opened:
            return title
        time.sleep(0.2)
    return last


def wait_basis_cleared(window, timeout=10):
    """等到依据区回到未选择状态：默认标题、空表、打开按钮禁用。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        title, cells, opened = basis_state(window)
        last = title
        if title == "依据错题（点上面的统计项后显示）" and cells == 0 and not opened:
            return True, last
        time.sleep(0.2)
    return False, last


def class_basis_state(window):
    title = text_of(window, "classStatsBasisTitle", "") or ""
    try:
        opened = ctl(window, "classStatsOpenMistakeButton", timeout=0).is_enabled()
    except Exception:
        opened = False
    return title, table_item_count(window, "classStatsBasisTable"), opened


def wait_class_stats_done(window, needle, timeout=90):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = text_of(window, "classStatsSummary", "") or ""
        if needle in last:
            try:
                if ctl(window, "classStatsQueryButton", timeout=0).is_enabled():
                    return last
            except Exception:
                pass
        time.sleep(0.2)
    return last


def wait_class_basis_loaded(window, name, timeout=30):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        title, cells, opened = class_basis_state(window)
        last = title
        if "依据错题：" in title and name in title and "正在加载" not in title and cells > 0 and opened:
            return title
        time.sleep(0.2)
    return last


def wait_class_basis_cleared(window, timeout=10):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        title, cells, opened = class_basis_state(window)
        last = title
        if title == "依据错题（点上面的统计项后显示）" and cells == 0 and not opened:
            return True, last
        time.sleep(0.2)
    return False, last


def mistake_count():
    conn = sqlite3.connect(str(DATA / "app.db"))
    try:
        return conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    finally:
        conn.close()


def class_stats_db(class_name):
    """从最终 EXE 的本地库独立取一份预期值，只读，不改数据。"""
    conn = sqlite3.connect(str(DATA / "app.db"))
    try:
        row = conn.execute("SELECT id FROM classes WHERE name = ?", (class_name,)).fetchone()
        if row is None:
            raise AssertionError("数据库里找不到班级：%s" % class_name)
        class_id = row[0]
        counts = {"confirmed": 0, "pending": 0, "returned": 0}
        points = {}
        errors = {}
        for status, raw_points, error_type in conn.execute(
                "SELECT status, knowledge_points, error_type FROM mistakes WHERE class_id = ?", (class_id,)):
            if status in counts:
                counts[status] += 1
            if status != "confirmed":
                continue
            try:
                names = json.loads(raw_points or "[]")
            except Exception:
                names = []
            for name in names:
                if name:
                    points[name] = points.get(name, 0) + 1
            if error_type:
                errors[error_type] = errors.get(error_type, 0) + 1
        student_count = conn.execute(
            "SELECT COUNT(*) FROM students WHERE class_id = ?", (class_id,)).fetchone()[0]
        return class_id, counts, points, errors, student_count
    finally:
        conn.close()


def class_summary_counts(text):
    result = {}
    for key, label in (("confirmed", "已确认错题"), ("pending", "待核对"), ("returned", "已退回")):
        matched = re.search(label + r"\s*(\d+)\s*条", text or "")
        result[key] = int(matched.group(1)) if matched else None
    return result


def main():
    log_path = os.environ.get("PACK_TEST_LOG")
    if log_path:
        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = Tee(log_file)
        sys.stderr = sys.stdout
    os.makedirs(str(WORK), exist_ok=True)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 1 包体
    check("交付 ZIP 存在", ZIP.is_file(), ZIP)
    zip_hash = sha256_of(ZIP)
    check("交付 ZIP 的 SHA-256", bool(zip_hash), "%s ｜ %d 字节 ｜ %s" % (ZIP.name, ZIP.stat().st_size, zip_hash))
    with zipfile.ZipFile(str(ZIP)) as zf:
        names = zf.namelist()
    tops = {item.split("/")[0] for item in names}
    check("ZIP 只有一个顶层文件夹", tops == {NAME}, sorted(tops))
    check("ZIP 内含主程序 exe", (NAME + "/" + NAME + ".exe") in names)
    check("ZIP 内含使用说明与许可", (NAME + "/使用说明.txt") in names
          and any(item.startswith(NAME + "/licenses/") for item in names))
    banned = [item for item in names if any(part in item for part in (
        "/data/", "/uploads/", "/backups/", "/.restore", "last-data-dir.txt", ".env", "app.db", ".key"))]
    check("ZIP 里没有用户数据或测试残留", not banned, banned[:3])
    check("ZIP 里没有 WebEngine", not any("WebEngine" in item or "QtWebView" in item for item in names))
    check("ZIP 条目名没有乱码", not any("\ufffd" in item for item in names))
    with zipfile.ZipFile(str(ZIP)) as zf:
        guide = zf.read(NAME + "/使用说明.txt")
    check("使用说明是 UTF-8 BOM 且写明错题入口", guide.startswith(b"\xef\xbb\xbf") and "错题" in guide.decode("utf-8-sig"))
    check("使用说明覆盖本轮功能", "原图" in guide.decode("utf-8-sig") and "停用" in guide.decode("utf-8-sig"))

    # ---------------------------------------------------------------- 2 解压
    with zipfile.ZipFile(str(ZIP)) as zf:
        zf.extractall(str(PROG_ROOT))
    check("从最终 ZIP 解压出 EXE", EXE.is_file(), "%d 字节" % (EXE.stat().st_size if EXE.is_file() else 0))
    exe_hash = sha256_of(EXE)
    check("解压出的 EXE 的 SHA-256（本次实测的就是这个文件）", bool(exe_hash),
          "%s ｜ %d 字节 ｜ %s" % (EXE.name, EXE.stat().st_size, exe_hash))

    photo = make_photo(PHOTOS / "错题 照片 第一张.png")
    SHOTS.mkdir(parents=True, exist_ok=True)
    env = clean_env({"MISTAKE_PHOTO_PATH": str(photo), "MISTAKE_SHOT_DIR": str(SHOTS),
                     "MISTAKE_AI_MODE": "mock"})

    # ---------------------------------------------------------------- 3 启动
    proc = spawn(["--data-dir", str(DATA), "--create-data-dir"], "app1", env=env)
    app = attach(proc)
    login = find_window(app, TITLE, require_id="loginUsername", timeout=90)
    check("双击后出现原生登录窗口", login is not None, login.window_text())
    check("启动后没有打开浏览器", not browsers_opened_by_us(), browsers_opened_by_us())
    check("启动后没有监听端口", not listening_ports(proc.pid), listening_ports(proc.pid))
    check("数据目录由程序自己建好", (DATA / "app.db").is_file())
    shot(login, "64-pack-mistakes-login.png")

    # ---------------------------------------------------------------- 4 注册与建数据
    username = "pack_a_" + uuid.uuid4().hex[:6]
    click(login, "registerLink")
    wait_text(login, "registerStatus", "", timeout=2)
    fill(login, "registerUsername", username)
    fill(login, "registerPassword", "pack-pass-1234")
    fill(login, "registerConfirm", "pack-pass-1234")
    click(login, "registerButton")
    try:
        classes = find_window(app, TITLE, require_id="currentUser", timeout=90)
    except RuntimeError:
        status = text_of(login, "registerStatus", "") or text_of(login, "loginStatus", "")
        raise RuntimeError("注册后没有进入班级窗口，界面提示：%r；窗口：%s；日志：%s"
                           % (status, [w.window_text() for w in app.windows()], read_log("app1")[-400:]))
    check("注册后进入班级列表", classes is not None)
    check("班级列表标题带账号名", username in (text_of(classes, "currentUser", "") or ""),
          text_of(classes, "currentUser", ""))

    create_class(app, classes, "初一（1）班")
    wait_text(classes, "classStatus", "共 1 个班级", timeout=60)
    check("新建班级成功", "共 1 个班级" in (text_of(classes, "classStatus", "") or ""),
          text_of(classes, "classStatus", ""))

    click(classes, "openStudentsButton")
    students = find_window(app, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    add_student(app, students, "李小明")
    add_student(app, students, "王小华")
    deadline = time.time() + 60
    while time.time() < deadline and "共 2 人" not in (text_of(students, "studentStatus", "") or ""):
        time.sleep(0.3)
    check("学生名单里建了两名学生", "共 2 人" in (text_of(students, "studentStatus", "") or ""),
          text_of(students, "studentStatus", ""))

    # ---------------------------------------------------------------- 5 空错题提示
    click(students, "openMistakesButton")
    mistakes = open_mistakes(app, students)
    empty_text = wait_text(mistakes, "mistakeEmpty", "还没有错题", timeout=30)
    check("没有错题时给出明确提示", "还没有错题" in empty_text, empty_text)
    shot(mistakes, "64-pack-mistakes-empty.png")

    # ---------------------------------------------------------------- 6 录入错题
    click(mistakes, "newMistakeButton")
    wait_child(mistakes, "questionText", timeout=60)
    fill(mistakes, "questionText", "解方程 2x + 3 = 11")
    fill(mistakes, "solutionText", "2x = 11 + 3")
    fill(mistakes, "observedErrors", "移项没有变号")
    fill(mistakes, "sourceDetail", "第 3 页第 2 题")
    click(mistakes, "choosePhoto")
    wait_text(mistakes, "photoInfo", "像素", timeout=20)
    check("选好照片后显示文件名与像素", "像素" in (text_of(mistakes, "photoInfo", "") or ""),
          text_of(mistakes, "photoInfo", ""))
    time.sleep(1.2)
    check("录入窗口截图已保存", copy_auto_shot("mistakeDialog", "65-pack-mistake-new.png"), "65-pack-mistake-new.png")
    click(mistakes, "mistakeSave")
    check("保存后录入窗口自动关闭", wait_child_gone(mistakes, "mistakeSave", timeout=60))
    deadline = time.time() + 60
    while time.time() < deadline and table_rows(mistakes) < 4:
        time.sleep(0.3)
    check("保存后列表立即出现这条错题", table_rows(mistakes) >= 4, "表格单元格数 %d" % table_rows(mistakes))
    detail_text = wait_text(mistakes, "mistakeDetail", "解方程 2x + 3 = 11", timeout=60)
    check("详情显示刚保存的题目内容", "解方程 2x + 3 = 11" in detail_text)
    check("详情显示学生错误表现", "移项没有变号" in detail_text, detail_text[:40])
    check("详情显示本地录入来源", "本地录入" in detail_text, detail_text[:80])
    shot(mistakes, "66-pack-mistake-detail.png")

    # ---------------------------------------------------------------- 6b 取消与未保存提示
    uploads = DATA / "uploads"
    before_files = len(list(uploads.iterdir())) if uploads.is_dir() else 0
    click(mistakes, "newMistakeButton")
    wait_child(mistakes, "questionText", timeout=60)
    click(mistakes, "mistakeCancel")
    check("还没填内容时点取消直接关掉", wait_child_gone(mistakes, "mistakeSave", timeout=60))
    check("取消后没有留下新错题", table_rows(mistakes) == 4, "表格单元格数 %d" % table_rows(mistakes))

    click(mistakes, "newMistakeButton")
    wait_child(mistakes, "questionText", timeout=60)
    fill(mistakes, "questionText", "这条不保存")
    click(mistakes, "mistakeCancel")
    check("填了内容再取消会先提醒", wait_button_text(mistakes, "放弃修改", timeout=30),
          "提示窗出现「放弃修改」按钮")
    click_button_text(mistakes, "放弃修改")
    check("确认放弃后录入窗口关闭", wait_child_gone(mistakes, "mistakeSave", timeout=60))
    time.sleep(1)
    after_files = len(list(uploads.iterdir())) if uploads.is_dir() else 0
    check("取消后没有留下多余照片文件", before_files == after_files,
          "取消前 %d 个，取消后 %d 个" % (before_files, after_files))

    # ---------------------------------------------------------------- 7 原图与缩放
    click(mistakes, "viewPhotoButton")
    wait_child(mistakes, "photoCanvas", timeout=60)
    start_zoom = text_of(mistakes, "photoZoom", "")
    click(mistakes, "photoZoomIn")
    time.sleep(0.8)
    zoomed = text_of(mistakes, "photoZoom", "")
    click(mistakes, "photoZoomOut")
    time.sleep(0.8)
    shrunk = text_of(mistakes, "photoZoom", "")
    check("原图窗口显示缩放比例", "%" in (start_zoom or ""), start_zoom)
    check("原图可以放大和缩小", zoomed != start_zoom and shrunk != zoomed, "%s/%s/%s" % (start_zoom, zoomed, shrunk))
    check("原图窗口显示原始像素尺寸", "480" in (text_of(mistakes, "photoSize", "") or ""),
          text_of(mistakes, "photoSize", ""))
    time.sleep(1.2)
    check("原图窗口截图已保存", copy_auto_shot("photoViewer", "67-pack-photo-viewer.png"), "67-pack-photo-viewer.png")
    click(mistakes, "photoClose")
    wait_child_gone(mistakes, "photoCanvas", timeout=60)

    # ---------------------------------------------------------------- 8 修改
    click(mistakes, "editMistakeButton")
    wait_child(mistakes, "questionText", timeout=60)
    check("修改窗口不允许换已有照片", not ctl(mistakes, "choosePhoto").is_enabled())
    fill(mistakes, "questionText", "解方程 2x + 3 = 11（改后）")
    fill(mistakes, "observedErrors", "移项没有变号\n两边没有同时除以 2")
    click(mistakes, "mistakeSave")
    check("修改保存后窗口关闭", wait_child_gone(mistakes, "mistakeSave", timeout=60))
    edited = wait_text(mistakes, "mistakeDetail", "（改后）", timeout=60)
    check("修改后详情显示新内容", "（改后）" in edited)
    check("修改后记录了老师改过哪些字段", "老师已修改过" in edited and "question_text" in edited,
          [line for line in edited.splitlines() if "老师已修改过" in line][:1])
    check("修改后仍显示原有照片", ctl(mistakes, "viewPhotoButton").is_enabled())
    shot(mistakes, "68-pack-mistake-edited.png")

    # ---------------------------------------------------------- 8b AI 分析与核对（模拟）
    mode_note = text_of(mistakes, "aiConfigNote", "") or ""
    check("默认不启用云端，界面写明模拟分析", "模拟分析" in mode_note and "不联网" in mode_note, mode_note[:80])
    click(mistakes, "analyzeButton")
    check("点「AI 分析」后弹出建议预览", wait_child(mistakes, "aiSuggestionDialog", timeout=60))
    body = text_of(mistakes, "aiSuggestionBody", "")
    check("建议预览里写着知识点与教学建议", "知识点" in body and "教学建议" in body, body[:60])
    check("预览框写明未采用不会保存", "不会保存" in (text_of(mistakes, "aiSuggestionNote", "") or ""),
          text_of(mistakes, "aiSuggestionNote", ""))
    time.sleep(1)
    check("建议预览框截图已保存", copy_auto_shot("aiSuggestionDialog", "74-pack-ai-preview.png"), "74-pack-ai-preview.png")
    click(mistakes, "aiApplySuggestion")
    check("采用后预览框关闭", wait_child_gone(mistakes, "aiSuggestionDialog", timeout=60))
    applied = wait_text(mistakes, "mistakeDetail", "知识点", timeout=60)
    check("采用后详情出现知识点", "知识点" in applied, applied[:60])
    check("采用后分析来源变成模拟建议", "模拟建议" in applied,
          [line for line in applied.splitlines() if "分析来源" in line][:1])
    check("采用后状态仍是待核对", "状态：待核对" in applied,
          [line for line in applied.splitlines() if "状态：" in line][:1])
    shot(mistakes, "75-pack-ai-applied.png")
    click(mistakes, "confirmMistakeButton")
    check("确认前先问一次", wait_button_text(mistakes, "确认无误", timeout=30))
    click_button_text(mistakes, "确认无误")
    confirmed = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        confirmed = text_of(mistakes, "mistakeDetail", "") or ""
        if "状态：已确认" in confirmed:
            break
        time.sleep(0.3)
    check("确认后状态变成已确认", "状态：已确认" in confirmed,
          [line for line in confirmed.splitlines() if "状态：" in line][:1])
    shot(mistakes, "76-pack-ai-confirmed.png")

    # ------------------------------- 8c 出了建议后老师又改题：旧建议不能覆盖新内容
    start_analysis(mistakes)
    check("再次分析弹出建议预览", wait_child(mistakes, "aiSuggestionDialog", timeout=60))
    ctl(mistakes, "aiSuggestionDialog").close()
    check("关掉预览框但没采用", wait_child_gone(mistakes, "aiSuggestionDialog", timeout=60))
    check("关预览后「采用建议」仍可点（建议还在业务层）", ctl(mistakes, "applySuggestionButton").is_enabled())
    click(mistakes, "editMistakeButton")
    wait_child(mistakes, "questionText", timeout=60)
    fill(mistakes, "questionText", "解方程 2x + 3 = 11（老师又改了）")
    click(mistakes, "mistakeSave")
    check("改完保存关闭修改窗口", wait_child_gone(mistakes, "mistakeSave", timeout=60))
    click(mistakes, "applySuggestionButton")
    rejected = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        rejected = text_of(mistakes, "mistakeStatus", "") or ""
        if "没有采用" in rejected:
            break
        time.sleep(0.2)
    check("用旧建议采用被拒并给出「已被修改」提示",
          "没有采用" in rejected and "已被修改" in rejected, rejected)
    detail_now = text_of(mistakes, "mistakeDetail", "") or ""
    check("被拒后老师新改的内容保留", "（老师又改了）" in detail_now, detail_now[:60])
    check("被拒后「采用建议」不能再用", not ctl(mistakes, "applySuggestionButton").is_enabled())
    shot(mistakes, "79-pack-stale-suggestion.png")

    # ------------------------------------------- 8d 放弃建议：界面与业务层同时失效
    start_analysis(mistakes)
    check("再分析一次弹出建议预览", wait_child(mistakes, "aiSuggestionDialog", timeout=60))
    click(mistakes, "aiDiscardSuggestion")
    check("点「放弃这条建议」后预览框关闭", wait_child_gone(mistakes, "aiSuggestionDialog", timeout=60))
    discarded = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        discarded = text_of(mistakes, "mistakeStatus", "") or ""
        if "已放弃这条建议" in discarded:
            break
        time.sleep(0.2)
    check("放弃后界面写明记录没有改动", "已放弃这条建议" in discarded and "没有改动" in discarded,
          discarded)
    check("放弃后「采用建议」不能再用", not ctl(mistakes, "applySuggestionButton").is_enabled())
    shot(mistakes, "80-pack-suggestion-discarded.png")

    # ---------------------------------------------------------------- 9 关闭重开
    kill_tree(proc.pid)
    proc.wait(30)
    check("关闭后程序退出", proc.poll() is not None, proc.poll())
    try:
        gone = wait_gone(app, TITLE, timeout=60)
    except Exception:
        gone = True
    check("关闭后窗口消失", gone)

    proc2 = spawn(["--data-dir", str(DATA)], "app2", env=clean_env({
        "MISTAKE_PHOTO_PATH": str(photo), "MISTAKE_SHOT_DIR": str(SHOTS),
        "MISTAKE_AI_MODE": "real", "DASHSCOPE_API_KEY": "sk-invalid-for-test",
        "MISTAKE_AI_BASE_URL": "http://127.0.0.1:1/v1",
        "MISTAKE_AI_MODEL": "qwen3.8-max"}))
    app2 = attach(proc2)
    login2 = find_window(app2, TITLE, require_id="loginUsername", timeout=90)
    check("重开后回到登录窗口", login2 is not None)
    fill(login2, "loginUsername", username)
    fill(login2, "loginPassword", "pack-pass-1234")
    click(login2, "loginButton")
    classes2 = find_window(app2, TITLE, require_id="currentUser", timeout=90)
    wait_text(classes2, "classStatus", "共 1 个班级", timeout=60)
    check("重开后班级还在", "共 1 个班级" in (text_of(classes2, "classStatus", "") or ""),
          text_of(classes2, "classStatus", ""))
    click(classes2, "openStudentsButton")
    students2 = find_window(app2, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    check("重开后学生名单还在", "共 2 人" in (text_of(students2, "studentStatus", "") or ""),
          text_of(students2, "studentStatus", ""))
    click(students2, "openMistakesButton")
    mistakes2 = open_mistakes(app2, students2)
    detail2 = wait_text(mistakes2, "mistakeDetail", "（老师又改了）", timeout=60)
    check("重开后错题内容还在", "（老师又改了）" in detail2, detail2[:60])
    check("重开后仍有照片按钮可用", ctl(mistakes2, "viewPhotoButton").is_enabled())
    shot(mistakes2, "69-pack-mistakes-reopened.png")
    click(mistakes2, "viewPhotoButton")
    wait_child(mistakes2, "photoCanvas", timeout=60)
    check("重开后原图还能看", "480" in (text_of(mistakes2, "photoSize", "") or ""),
          text_of(mistakes2, "photoSize", ""))
    click(mistakes2, "photoClose")
    wait_child_gone(mistakes2, "photoCanvas", timeout=60)

    # ------------------------------------------- 9c 真实模式没配密钥：失败但不动原记录
    before9c = text_of(mistakes2, "mistakeDetail", "") or ""
    status_before = [line for line in before9c.splitlines() if line.startswith("状态：")]
    click(mistakes2, "analyzeButton")
    appeared = has_child(mistakes2, "aiConsentDialog", timeout=40)
    check("真实分析前先弹同意框", appeared)
    if appeared:
        consent_text = text_of(mistakes2, "aiConsentText", "")
        check("同意框写清服务、发送内容与费用提示",
              "OpenCode Console" in consent_text and "照片" in consent_text and "费用" in consent_text,
              consent_text[:60])
        toggle = str(ctl(mistakes2, "aiConsentCheck").get_toggle_state())
        check("同意勾选框默认不勾", toggle.strip().lower() in ("0", "off"), toggle)
        time.sleep(1)
        check("真实调用确认框截图已保存", copy_auto_shot("aiConsentDialog", "77-pack-consent.png"), "77-pack-consent.png")
        click(mistakes2, "aiConsentCheck")
        enabled = False
        for _ in range(60):
            if ctl(mistakes2, "aiConsentOk").is_enabled():
                enabled = True
                break
            time.sleep(0.1)
        check("勾选同意后「继续分析」才能点", enabled)
        click(mistakes2, "aiConsentOk")
        failed = wait_text(mistakes2, "mistakeDetail", "最近一次分析失败", timeout=120)
        check("失败后详情写明最近一次分析失败", "最近一次分析失败" in failed,
              [line for line in failed.splitlines() if "失败" in line][:1])
        check("失败不把原来的分析来源改成真实分析", "分析来源：模拟建议" in failed,
              [line for line in failed.splitlines() if "分析来源" in line][:1])
        check("失败不改原来的核对状态", bool(status_before) and all(line in failed for line in status_before),
              status_before)
        shot(mistakes2, "78-pack-ai-failed.png")

    # ------------------- 9d/9e 迟到失败：关窗、退出登录都不许把失败原因写进记录
    kill_tree(proc2.pid)
    proc2.wait(30)
    hole = black_hole()
    proc4 = spawn(["--data-dir", str(DATA)], "app4", env=clean_env({
        "MISTAKE_PHOTO_PATH": str(photo), "MISTAKE_SHOT_DIR": str(SHOTS),
        "MISTAKE_AI_MODE": "real", "DASHSCOPE_API_KEY": "sk-slow-for-test",
        "DASHSCOPE_BASE_URL": "http://127.0.0.1:%d/v1" % hole["port"],
        "MISTAKE_AI_MODEL": "qwen3.8-max",
        "MISTAKE_AI_TIMEOUT": "3"}))
    app4 = attach(proc4)
    login4 = find_window(app4, TITLE, require_id="loginUsername", timeout=90)
    fill(login4, "loginUsername", username)
    fill(login4, "loginPassword", "pack-pass-1234")
    click(login4, "loginButton")
    classes6 = find_window(app4, TITLE, require_id="currentUser", timeout=90)
    wait_text(classes6, "classStatus", "共 1 个班级", timeout=60)
    click(classes6, "openStudentsButton")
    students4 = find_window(app4, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    click(students4, "openMistakesButton")
    mistakes4 = open_mistakes(app4, students4)
    before4 = wait_text(mistakes4, "mistakeDetail", "状态：", timeout=90)

    def start_slow_analysis(window):
        """点分析 → 勾同意 → 继续；请求会停在只握手的端口上，3 秒后才失败。"""
        click(window, "analyzeButton")
        if not has_child(window, "aiConsentDialog", timeout=40):
            return False
        click(window, "aiConsentCheck")
        deadline = time.time() + 30
        while time.time() < deadline and not ctl(window, "aiConsentOk").is_enabled():
            time.sleep(0.1)
        click(window, "aiConsentOk")
        return True

    row_before = read_mistake_row(DATA)
    check("迟到失败测试：先记下这条错题在数据库里的整行", bool(row_before),
          "列数=%d 进测试前的失败原因=%s" % (len(row_before), str(row_before.get("ai_error"))[:24]))

    started = start_slow_analysis(mistakes4)
    check("迟到失败测试：真实分析已经发出（同意框已确认）", started)
    hit = wait_received(hole, 1)
    check("迟到失败测试：本地延迟服务确认收到这次请求（请求已进入等待，之后才关窗）", hit,
          "收到时刻=%.1f" % (hole["received_at"] or 0))
    mistakes4.close()
    wait_gone(app4, TITLE_MISTAKES, timeout=60)
    ended = wait_closed(hole, 1)
    check("迟到失败测试：这次请求已经结束（客户端超时后关闭了连接）", ended,
          "已结束的请求数=%d 结束时刻=%.1f" % (hole["closed_count"], (hole["closed_times"] or [0])[0]))
    time.sleep(0.8)
    click(students4, "openMistakesButton")
    reopened = open_mistakes(app4, students4)
    after4 = wait_text(reopened, "mistakeDetail", "状态：", timeout=90)
    check("分析中关窗后，迟到的失败结果没有写进记录（详情一字未变）", after4 == before4,
          [line for line in after4.splitlines() if "失败" in line][:1])

    started2 = start_slow_analysis(reopened)
    check("迟到失败测试：第二轮分析已经发出", started2)
    hit2 = wait_received(hole, 2)
    check("迟到失败测试：本地延迟服务确认收到第二轮请求（之后才退出登录）", hit2,
          "已收到的请求数=%d" % hole["hits"])
    click(reopened, "backToStudents")
    wait_child_gone(reopened, "mistakeStudentName", timeout=60)
    click(students4, "backToClasses")
    classes7 = find_window(app4, TITLE, require_id="currentUser", timeout=60)
    click(classes7, "logoutButton")
    login5 = find_window(app4, TITLE, require_id="loginUsername", timeout=90)
    fill(login5, "loginUsername", username)
    fill(login5, "loginPassword", "pack-pass-1234")
    click(login5, "loginButton")
    classes8 = find_window(app4, TITLE, require_id="currentUser", timeout=90)
    wait_text(classes8, "classStatus", "共 1 个班级", timeout=60)
    ended2 = wait_closed(hole, 2)
    check("迟到失败测试：第二轮请求也已经结束（不是靠固定等待）", ended2,
          "已结束的请求数=%d 结束时刻=%.1f" % (hole["closed_count"], (hole["closed_times"] or [0, 0])[-1]))
    time.sleep(0.8)
    click(classes8, "openStudentsButton")
    students5 = find_window(app4, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    click(students5, "openMistakesButton")
    mistakes5 = open_mistakes(app4, students5)
    after5 = wait_text(mistakes5, "mistakeDetail", "状态：", timeout=90)
    check("分析中退出登录再登录后，迟到的失败结果没有写进记录（详情一字未变）",
          after5 == before4, [line for line in after5.splitlines() if "失败" in line][:1])
    shot(mistakes5, "82-pack-late-failure.png")
    kill_tree(proc4.pid)
    proc4.wait(30)

    row_after = read_mistake_row(DATA)
    changed = row_diff(row_before, row_after)
    check("迟到失败之后：数据库里这条错题的整行所有列都没有变化", changed == [], changed)
    check("迟到失败之后：失败原因仍是进迟到测试前的那一次",
          row_after.get("ai_error") == row_before.get("ai_error"),
          str(row_after.get("ai_error"))[:24])
    check("迟到失败之后：updated_at 没有被刷新",
          row_after.get("updated_at") == row_before.get("updated_at"), row_after.get("updated_at"))

    hole["stop"].set()
    for conn in hole["held"]:
        try:
            conn.close()
        except Exception:
            pass
    hole["server"].close()

    # ------------------- 8e 双向互通：最终 EXE 写的 → 便携版读；便携版写的 → EXE 再读
    kill_tree(proc2.pid)
    proc2.wait(30)
    check("互通前先关掉桌面 EXE（同一份数据不能同时对开）", proc2.poll() is not None, proc2.poll())
    port = free_port()
    plog = open(str(WORK / "portable-8e.out"), "wb")
    portable = subprocess.Popen(
        [str(PORTABLE), "--serve", "--data-dir", str(DATA), "--port", str(port)],
        cwd=str(PORTABLE.parent), stdout=plog, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=0x08000000)
    base = "http://127.0.0.1:%d" % port
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def api(method, path, payload=None):
        raw = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(base + path, data=raw, method=method,
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        with opener.open(request, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    portable_id = None
    try:
        ready = False
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with opener.open(base + "/api/info", timeout=3) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.5)
        check("便携版用同一份数据目录启动", ready, base)
        if ready:
            status, _me = api("POST", "/api/auth/login",
                              {"username": username, "password": "pack-pass-1234"})
            check("便携版能用桌面版建的账号登录", status == 200, "status=%s" % status)
            _st, classes = api("GET", "/api/classes")
            class_rows = classes.get("items") if isinstance(classes, dict) else classes
            class_rows = class_rows or []
            class_id = class_rows[0]["id"] if class_rows else 1
            _st, one = api("GET", "/api/classes/%d" % class_id)
            student_id = one["students"][0]["id"] if one.get("students") else 1
            _st, listed = api("GET", "/api/mistakes?class_id=%d&student_id=%d" % (class_id, student_id))
            rows = listed.get("items") if isinstance(listed, dict) else listed
            rows = rows or []
            row = next((item for item in rows if "（老师又改了）" in (item.get("question_text") or "")), None)
            check("便携版读到桌面 EXE 录入并修改过的错题", row is not None,
                  "%d 条记录" % len(rows))
            if row is not None:
                _st, seen = api("GET", "/api/mistakes/%d" % row["id"])
                check("便携版读到桌面 EXE 采用的分析结果",
                      seen.get("ai_mode") == "mock" and bool(seen.get("ai")),
                      "ai_mode=%s 有AI建议=%s" % (seen.get("ai_mode"), bool(seen.get("ai"))))
                check("便携版读到桌面 EXE 记下的老师修改",
                      "question_text" in (seen.get("teacher_edited_fields") or []),
                      "%s" % (seen.get("teacher_edited_fields"),))
                request = urllib.request.Request(base + "/api/mistakes/%d/image" % row["id"])
                with opener.open(request, timeout=60) as resp:
                    portable_photo = resp.read()
                stored = [item for item in (DATA / "uploads").glob("*")
                          if item.is_file() and item.stat().st_size == len(portable_photo)]
                same = any(hashlib.sha256(item.read_bytes()).hexdigest()
                           == hashlib.sha256(portable_photo).hexdigest() for item in stored)
                check("便携版读到的照片与数据目录里存的照片字节一致（sha256 相同）", same,
                      "%d 字节 / %s" % (len(portable_photo),
                                       hashlib.sha256(portable_photo).hexdigest()[:16]))

            data_url = "data:image/png;base64," + base64.b64encode(photo.read_bytes()).decode("ascii")
            status, made = api("POST", "/api/mistakes", {
                "class_id": class_id, "student_id": student_id,
                "question_text": "便携版录入：解方程 5x - 2 = 13", "solution_text": "x = 3",
                "occurred_on": time.strftime("%Y-%m-%d"), "source_type": "作业",
                "image": {"data_url": data_url}})
            portable_id = made.get("id") if isinstance(made, dict) else None
            check("便携版能在桌面 EXE 的数据里录入带图错题", status == 200 and bool(portable_id),
                  "status=%s id=%s" % (status, portable_id))
            if portable_id:
                api("POST", "/api/mistakes/%d/analyze" % portable_id, {"consent": False})
                api("PATCH", "/api/mistakes/%d" % portable_id,
                    {"question_text": "便携版录入：解方程 5x - 2 = 13（网页改过）",
                     "knowledge_points": ["一元一次方程"], "error_type": "计算错误"})
                status, done = api("POST", "/api/mistakes/%d/confirm" % portable_id, {})
                check("便携版把这条错题确认为已确认",
                      status == 200 and done.get("status") == "confirmed", "status=%s" % status)
            status, listed = api("GET", "/api/mistakes?class_id=%d&student_id=%d" % (class_id, student_id), None)
            rows = listed.get("items", []) if isinstance(listed, dict) else []
            for row in rows:
                api("PATCH", "/api/mistakes/%d" % row["id"], {"occurred_on": "2020-01-05"})
            print("把 %d 条错题的发生日期改到 2020-01-05，供学情页的日期筛选检查" % len(rows), flush=True)
    finally:
        try:
            portable.terminate()
            portable.wait(10)
        except Exception:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(portable.pid)],
                               capture_output=True)
            except Exception:
                pass
        plog.close()

    proc3 = spawn(["--data-dir", str(DATA)], "app3", env=clean_env({
        "MISTAKE_PHOTO_PATH": str(photo), "MISTAKE_AI_MODE": "mock"}))
    app3 = attach(proc3)
    login3 = find_window(app3, TITLE, require_id="loginUsername", timeout=90)
    fill(login3, "loginUsername", username)
    fill(login3, "loginPassword", "pack-pass-1234")
    click(login3, "loginButton")
    classes5 = find_window(app3, TITLE, require_id="currentUser", timeout=90)
    wait_text(classes5, "classStatus", "共 1 个班级", timeout=60)
    click(classes5, "openStudentsButton")
    students2 = find_window(app3, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    click(students2, "openMistakesButton")
    mistakes2 = open_mistakes(app3, students2)
    app2 = app3
    if portable_id:
        shown = wait_text(mistakes2, "mistakeDetail", "便携版录入", timeout=90)
        check("桌面 EXE 重开列表能看到便携版录入的错题", "便携版录入" in shown, shown[:60])
        check("桌面 EXE 看到便携版写的老师修改",
              "老师已修改过" in shown and "question_text" in shown,
              [line for line in shown.splitlines() if "老师已修改过" in line][:1])
        check("桌面 EXE 看到便携版确认的结果", "状态：已确认" in shown,
              [line for line in shown.splitlines() if "状态：" in line][:1])
        check("桌面 EXE 能打开便携版录入的照片", ctl(mistakes2, "viewPhotoButton").is_enabled())
        shot(mistakes2, "81-pack-interop.png")

    # ---------------------------------------------------------------- 9b 停用学生的历史错题
    click(mistakes2, "backToStudents")
    wait_child_gone(mistakes2, "mistakeStudentName", timeout=60)
    click(students2, "deactivateStudentButton")
    stopped = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        stopped = text_of(students2, "studentStatus", "") or ""
        if "已停用" in stopped:
            break
        time.sleep(0.3)
    check("停用学生后名单标注已停用", "已停用" in stopped, stopped)
    click(students2, "openMistakesButton")
    mistakes3 = open_mistakes(app2, students2)
    history = wait_text(mistakes3, "mistakeDetail", "状态：", timeout=60)
    check("停用学生的历史错题还能看",
          "状态：" in history and ("便携版录入" in history or "（老师又改了）" in history),
          history[:60])
    check("停用学生不能录新错题", not ctl(mistakes3, "newMistakeButton").is_enabled())
    check("错题页标明学生已停用", "已停用" in (text_of(mistakes3, "mistakeStudentState", "") or ""),
          text_of(mistakes3, "mistakeStudentState", ""))
    shot(mistakes3, "71-pack-mistakes-inactive.png")
    click(mistakes3, "backToStudents")
    wait_child_gone(mistakes3, "mistakeStudentName", timeout=60)

    # ---------------------------------------------------------------- 9f 学情统计页
    click(students2, "openStatsButton")
    stats = find_window(app2, TITLE_STATS, require_id="statsSummary", timeout=90)
    summary_text = ""
    deadline = time.time() + 90
    while time.time() < deadline:
        summary_text = text_of(stats, "statsSummary", "") or ""
        if "已确认错题" in summary_text:
            break
        time.sleep(0.3)
    check("学生名单能打开学情统计页", bool(summary_text), summary_text[:36])
    check("统计页写明已确认条数（停用学生的历史也能统计）", "已确认错题" in summary_text, summary_text[:60])
    ident = text_of(stats, "statsStudentIdent", "") or ""
    check("统计页显示内部编号", "内部编号" in ident, ident)
    rule = text_of(stats, "statsRule", "") or ""
    check("统计页写明知识点条数相加可能超过错题总数", "超过" in rule, rule[:60])
    shot(stats, "97-pack-stats.png")

    table_texts = [row.window_text() for row in ctl(stats, "statsKnowledgeTable").children()]
    check("知识点表里有便携版写的那一条", any("一元一次方程" == t for t in table_texts), table_texts[:6])
    clicked = click_table_text(stats, "statsKnowledgeTable", "一元一次方程")
    kp_basis = wait_basis_loaded(stats, "一元一次方程")
    kp_cells = table_item_count(stats, "statsBasisTable")
    check("准确路径：点知识点后出现依据错题（带条数）",
          clicked and "依据错题：一元一次方程" in kp_basis and kp_cells > 0, kp_basis)
    check("依据列表出来后「打开这条错题」可点", ctl(stats, "statsOpenMistakeButton").is_enabled())
    shot(stats, "98-pack-stats-basis.png")

    click(stats, "statsResetButton")
    after_reset = wait_stats_done(stats, "已确认错题 1 条")
    check("重置后统计回到全量", "已确认错题 1 条" in after_reset, after_reset[:60])
    cleared, cleared_title = wait_basis_cleared(stats)
    check("重置后旧依据已清空、打开按钮禁用（不再能点开上一项）", cleared, cleared_title)
    shot(stats, "98b-pack-stats-reset.png")

    clicked = click_table_text(stats, "statsKnowledgeTable", "一元一次方程")
    retry_basis = wait_basis_loaded(stats, "一元一次方程")
    check("准确路径：重置后不切换其他项，再点同一知识点能重新出依据",
          clicked and "依据错题：一元一次方程" in retry_basis
          and table_item_count(stats, "statsBasisTable") > 0, retry_basis)
    shot(stats, "98c-pack-stats-retry-kp.png")

    error_texts = [row.window_text() for row in ctl(stats, "statsErrorTypeTable").children()]
    check("错误类型表里有便携版写的那一条", any("计算错误" == t for t in error_texts), error_texts[:6])
    clicked = click_table_text(stats, "statsErrorTypeTable", "计算错误")
    type_basis = wait_basis_loaded(stats, "计算错误")
    check("点错误类型也能看依据（切换时不残留上一个知识点）",
          clicked and "计算错误" in type_basis and "一元一次方程" not in type_basis, type_basis)

    click(stats, "statsResetButton")
    after_type_reset = wait_stats_done(stats, "已确认错题 1 条")
    check("错误类型路径重置后统计回到全量", "已确认错题 1 条" in after_type_reset, after_type_reset[:60])
    cleared, cleared_title = wait_basis_cleared(stats)
    check("错误类型路径重置后旧依据已清空、打开按钮禁用", cleared, cleared_title)

    clicked = click_table_text(stats, "statsErrorTypeTable", "计算错误")
    type_retry = wait_basis_loaded(stats, "计算错误")
    check("准确路径：重置后不切换其他项，再点同一错误类型能重新出依据",
          clicked and "依据错题：计算错误" in type_retry
          and table_item_count(stats, "statsBasisTable") > 0, type_retry)
    shot(stats, "98d-pack-stats-retry-error.png")

    if not (table_item_count(stats, "statsBasisTable") > 0
            and ctl(stats, "statsOpenMistakeButton").is_enabled()):
        note("依据没有加载成功，改用知识点行继续检查详情和窗口返回")
        click_table_text(stats, "statsKnowledgeTable", "一元一次方程")
        wait_basis_loaded(stats, "一元一次方程")

    click(stats, "statsOpenMistakeButton")
    from_stats = find_window(app3, TITLE_MISTAKES, require_id="mistakeStudentName", timeout=90)
    detail = wait_text(from_stats, "mistakeDetail", "状态：", timeout=90)
    check("从依据能打开这条错题的详情", "状态：已确认" in detail,
          [line for line in detail.splitlines() if "状态：" in line][:1])
    stats.close()
    check("先关学情页后，统计窗口不再显示", wait_gone(app3, TITLE_STATS, timeout=30))
    click(from_stats, "backToStudents")
    wait_child_gone(from_stats, "mistakeStudentName", timeout=60)
    students3 = find_window(app3, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    visible = [w.window_text() for w in app3.windows() if w.is_visible()]
    check("从学情页打开详情、先关学情页，关详情后回到学生名单",
          any(t == TITLE_STUDENTS for t in visible) and not [t for t in visible if t == TITLE_STATS],
          visible)
    click(students3, "openStatsButton")
    stats2 = find_window(app3, TITLE_STATS, require_id="statsSummary", timeout=90)
    again = wait_stats_done(stats2, "已确认错题 1 条")
    check("关掉再打开，统计数字一致", again == summary_text, again[:60])

    print("日期筛选：这批题的发生日期已被便携版改到 2020-01-05，只勾日期框（默认今天）就该查不到",
          flush=True)
    for box in ("statsFromCheck", "statsToCheck"):
        widget = ctl(stats2, box)
        if widget.get_toggle_state() == 0:
            rect = widget.rectangle()
            message_click(stats2, (rect.left + 8, (rect.top + rect.bottom) // 2))
            time.sleep(0.3)
    on_state = [ctl(stats2, "statsFromCheck").get_toggle_state(), ctl(stats2, "statsToCheck").get_toggle_state()]
    check("两个日期勾选框都能勾上", on_state == [1, 1], on_state)
    click(stats2, "statsQueryButton")
    ranged = wait_stats_done(stats2, "已确认错题 0 条", timeout=60)
    check("日期范围里没有记录时，已确认 0 条", "已确认错题 0 条" in ranged, ranged[:60])
    check("同一次查询里待核对也按日期算 0 条（修复前仍是全量）", "待核对 0 条" in ranged, ranged[:60])
    shot(stats2, "99-pack-stats-date.png")
    for box in ("statsFromCheck", "statsToCheck"):
        rect = ctl(stats2, box).rectangle()
        message_click(stats2, (rect.left + 8, (rect.top + rect.bottom) // 2))
        time.sleep(0.3)
    click(stats2, "statsQueryButton")
    back = wait_stats_done(stats2, "已确认错题 1 条", timeout=60)
    check("取消日期勾选后统计回到全量", "已确认错题 1 条" in back, back[:60])
    stats2.close()
    wait_gone(app3, TITLE_STATS, timeout=30)

    # ---------------------------------------------------------- 9g 班级学情统计
    print("\n--- 9g 班级学情统计：班级入口、分布、依据、详情返回、重置重试、空状态 ---", flush=True)
    click(students3, "backToClasses")
    classes_cls = find_window(app3, TITLE, require_id="currentUser", timeout=60)
    wait_text(classes_cls, "classStatus", "共 1 个班级", timeout=60)
    click_table_text(classes_cls, "classTable", "初一（1）班")
    check("选中班级后「班级学情」按钮可点", ctl(classes_cls, "openClassStatsButton").is_enabled())
    click(classes_cls, "openClassStatsButton")
    class_stats = find_window(app3, TITLE_CLASS_STATS, require_id="classStatsSummary", timeout=90)
    class_id, expected_counts, expected_points, expected_errors, expected_students = class_stats_db("初一（1）班")
    expected_text = "已确认错题 %d 条" % expected_counts["confirmed"]
    initial_summary = wait_class_stats_done(class_stats, expected_text, timeout=90)
    actual_counts = class_summary_counts(initial_summary)
    check("班级统计三种数量与数据库一致", actual_counts == expected_counts,
          "界面=%s 数据库=%s" % (actual_counts, expected_counts))
    check("班级统计写明三个数字同日期口径",
          "三个数字都按所选日期统计" in (text_of(class_stats, "classStatsScopeNote", "") or ""),
          text_of(class_stats, "classStatsScopeNote", ""))
    rule_text = text_of(class_stats, "classStatsRule", "") or ""
    check("班级统计说明停用学生历史仍计入", "停用学生" in rule_text, rule_text[:80])
    check("班级统计说明不能算正确率/掌握率", "正确率" in rule_text and "掌握率" in rule_text, rule_text[:80])
    check("班级统计说明多知识点相加可能超过总数", "超过" in rule_text, rule_text[:80])
    check("班级统计显示当前班级", "初一（1）班" in (text_of(class_stats, "classStatsClassName", "") or ""),
          text_of(class_stats, "classStatsClassName", ""))

    def table_pairs(window, auto_id):
        texts = [row.window_text() for row in table_items(window, auto_id)]
        return {texts[i]: texts[i + 1] for i in range(0, len(texts) - 1, 2)}

    kp_pairs = table_pairs(class_stats, "classStatsKnowledgeTable")
    error_pairs = table_pairs(class_stats, "classStatsErrorTypeTable")
    check("班级知识点分布与数据库一致",
          {name: int(count) for name, count in kp_pairs.items()} == expected_points,
          "界面=%s 数据库=%s" % (kp_pairs, expected_points))
    check("班级错误类型分布与数据库一致",
          {name: int(count) for name, count in error_pairs.items()} == expected_errors,
          "界面=%s 数据库=%s" % (error_pairs, expected_errors))

    first_kp = sorted(expected_points)[0]
    first_error = sorted(expected_errors)[0]
    check("班级知识点行可点", click_table_text(class_stats, "classStatsKnowledgeTable", first_kp))
    class_kp_basis = wait_class_basis_loaded(class_stats, first_kp)
    class_kp_cells = table_item_count(class_stats, "classStatsBasisTable")
    check("班级知识点依据条数与分布一致", class_kp_cells == expected_points[first_kp] * 5,
          "依据单元格=%d 预期条数=%d ｜ %s" % (class_kp_cells, expected_points[first_kp], class_kp_basis))
    basis_text = table_text(class_stats, "classStatsBasisTable")
    check("班级依据显示学生姓名", "李小明" in basis_text, basis_text[:120])
    check("班级依据可打开错题", ctl(class_stats, "classStatsOpenMistakeButton").is_enabled())
    shot(class_stats, "98e-pack-class-stats.png")

    click(class_stats, "classStatsResetButton")
    reset_summary = wait_class_stats_done(class_stats, expected_text)
    cleared, cleared_title = wait_class_basis_cleared(class_stats)
    check("班级统计重置后回到全量", expected_text in reset_summary, reset_summary[:60])
    check("班级统计重置后依据清空、打开按钮禁用", cleared, cleared_title)
    shot(class_stats, "98f-pack-class-stats-reset.png")

    clicked = click_table_text(class_stats, "classStatsKnowledgeTable", first_kp)
    retry_kp = wait_class_basis_loaded(class_stats, first_kp)
    check("班级统计重置后不切换其他项，再点同一知识点能重新出依据",
          clicked and first_kp in retry_kp and table_item_count(class_stats, "classStatsBasisTable") > 0,
          retry_kp)

    click(class_stats, "classStatsResetButton")
    wait_class_stats_done(class_stats, expected_text)
    wait_class_basis_cleared(class_stats)
    clicked = click_table_text(class_stats, "classStatsErrorTypeTable", first_error)
    retry_error = wait_class_basis_loaded(class_stats, first_error)
    check("班级统计重置后不切换其他项，再点同一错误类型能重新出依据",
          clicked and first_error in retry_error
          and table_item_count(class_stats, "classStatsBasisTable") == expected_errors[first_error] * 5,
          retry_error)

    click(class_stats, "classStatsOpenMistakeButton")
    from_class_stats = find_window(app3, TITLE_MISTAKES, require_id="mistakeStudentName", timeout=90)
    check("班级依据能打开错题详情", from_class_stats is not None)
    hidden = False
    for _ in range(50):
        if not native_visible(class_stats):
            hidden = True
            break
        time.sleep(0.1)
    check("打开详情时班级统计页隐藏", hidden, "IsWindowVisible=%s" % native_visible(class_stats))
    from_class_stats.close()
    wait_gone(app3, TITLE_MISTAKES, timeout=30)
    shown = False
    for _ in range(100):
        if native_visible(class_stats):
            shown = True
            break
        time.sleep(0.1)
    check("关闭详情后回到班级统计页", shown, "IsWindowVisible=%s" % native_visible(class_stats))
    check("返回班级统计后依据重新加载",
          wait_class_basis_loaded(class_stats, first_error) != "" and
          table_item_count(class_stats, "classStatsBasisTable") == expected_errors[first_error] * 5,
          class_basis_state(class_stats))
    check("返回班级统计后打开按钮仍可用", ctl(class_stats, "classStatsOpenMistakeButton").is_enabled())
    shot(class_stats, "98g-pack-class-stats-return.png")

    for box in ("classStatsFromCheck", "classStatsToCheck"):
        widget = ctl(class_stats, box)
        if widget.get_toggle_state() == 0:
            rect = widget.rectangle()
            message_click(class_stats, (rect.left + 8, (rect.top + rect.bottom) // 2))
            time.sleep(0.3)
    click(class_stats, "classStatsQueryButton")
    empty_summary = wait_class_stats_done(class_stats, "已确认错题 0 条", timeout=60)
    empty_text = text_of(class_stats, "classStatsEmpty", "") or ""
    check("班级统计空筛选时三种数量都是 0", class_summary_counts(empty_summary) ==
          {"confirmed": 0, "pending": 0, "returned": 0}, empty_summary)
    check("班级统计空筛选显示空状态", "所选日期范围内没有错题" in empty_text, empty_text)
    shot(class_stats, "99b-pack-class-stats-empty.png")
    click(class_stats, "classStatsResetButton")
    back_summary = wait_class_stats_done(class_stats, expected_text, timeout=60)
    check("取消日期后班级统计回到全量", expected_text in back_summary, back_summary[:60])
    click(class_stats, "backToClassesFromStats")
    stats_gone = wait_gone(app3, TITLE_CLASS_STATS, timeout=30)
    check("班级统计返回后回到班级列表", native_visible(classes_cls) and stats_gone)

    # ---------------------------------------------------------------- 9h 真实 AI 设置、试分析与正式核对
    print("\n--- 9h AI 设置、试分析入口、真实分析到老师确认、重启保存 ---", flush=True)
    # 同一数据目录只允许一个程序打开；先关掉 9f/9g 的窗口，9h 用新的进程登录。
    kill_tree(proc3.pid)
    proc3.wait(30)
    stub_port = start_ai_stub()
    stub_base = "http://127.0.0.1:%d/compatible-mode/v1" % stub_port
    proc5 = spawn(["--data-dir", str(DATA)], "app5", env=clean_env({
        "MISTAKE_PHOTO_PATH": str(photo), "MISTAKE_SHOT_DIR": str(SHOTS),
        "MISTAKE_TRIAL_PHOTO_PATH": str(photo),
        "MISTAKE_AI_MODE": "real", "MISTAKE_AI_API_KEY": "sk-env-stub-142",
        "MISTAKE_AI_BASE_URL": stub_base, "MISTAKE_AI_TIMEOUT": "30"}))
    app5 = attach(proc5)
    login6 = find_window(app5, TITLE, require_id="loginUsername", timeout=90)
    fill(login6, "loginUsername", username)
    fill(login6, "loginPassword", "pack-pass-1234")
    click(login6, "loginButton")
    classes_ai = find_window(app5, TITLE, require_id="currentUser", timeout=90)
    wait_text(classes_ai, "classStatus", "共 1 个班级", timeout=60)

    click(classes_ai, "openAiSettingsButton")
    # Qt 把带父窗口的 QDialog 挂在主窗口的 UIA 树里，不是独立顶层窗口；
    # 这里按子控件等待，避免 find_window 等不到。
    wait_child(classes_ai, "aiBaseUrlEdit", timeout=60)
    check("桌面端有 AI 设置入口", scan(classes_ai, "aiSettingsDialog") is not None)
    check("设置窗口显示实际服务地址", stub_base in (text_of(classes_ai, "aiBaseUrlEdit", "") or ""),
          text_of(classes_ai, "aiBaseUrlEdit", ""))
    fill_ascii(classes_ai, "aiBaseUrlEdit", stub_base)
    fill_ascii(classes_ai, "aiModelEdit", "qwen3.8-max")
    fill_ascii(classes_ai, "aiKeyEdit", "sk-ui-stub-141")
    check("AI 设置地址写入准确", (text_of(classes_ai, "aiBaseUrlEdit", "") or "").strip() == stub_base,
          text_of(classes_ai, "aiBaseUrlEdit", ""))
    click(classes_ai, "aiSettingsSave")
    gone = wait_child_gone(classes_ai, "aiSettingsDialog", timeout=30)
    check("保存 AI 设置后窗口关闭", gone)
    click(classes_ai, "openAiSettingsButton")
    wait_child(classes_ai, "aiBaseUrlEdit", timeout=60)
    sources = text_of(classes_ai, "aiSettingsSources", "") or ""
    check("重新打开设置显示本次登录已填写密钥", "本次登录已填写" in sources, sources)
    check("重新打开设置显示实际地址", stub_base in sources, sources[:100])
    copy_auto_shot("aiSettingsDialog", "83-pack-ai-settings.png")
    click(classes_ai, "aiSettingsCancel")

    click(classes_ai, "openAiTrialButton")
    wait_child(classes_ai, "aiTrialPath", timeout=60)
    trial_dialog = scan(classes_ai, "aiTrialDialog")
    trial_config = text_of(classes_ai, "aiTrialConfig", "") or ""
    trial_title = (trial_dialog.window_text() if trial_dialog else "") or ""
    trial_note = text_of(classes_ai, "aiTrialSendNote", "") or ""
    check("试分析窗口显示实际地址和模型", stub_base in trial_config and "qwen3.8-max" in trial_config,
          trial_config[:120])
    check("试分析窗口写明不保存为错题",
          "不保存" in trial_title or "不写入任何错题" in trial_note,
          trial_title or trial_note)
    check("试分析题图已带入", str(photo) in (text_of(classes_ai, "aiTrialPath", "") or ""),
          text_of(classes_ai, "aiTrialPath", ""))
    before_trial = mistake_count()
    click(classes_ai, "aiTrialConsent")
    click(classes_ai, "aiTrialStart")
    trial_status = wait_text(classes_ai, "aiTrialStatus", "试分析成功", timeout=90)
    check("试分析成功", "试分析成功" in trial_status, trial_status)
    check("试分析显示耗时", "耗时" in trial_status, trial_status)
    check("试分析显示接口实际用量", "prompt 1200" in trial_status and "total 1380" in trial_status,
          trial_status)
    check("试分析显示接口实际费用", "接口计费 $0.00348" in trial_status, trial_status)
    trial_result = text_of(classes_ai, "aiTrialResult", "") or ""
    check("试分析结果显示实际服务地址", stub_base in trial_result, trial_result[:120])
    check("试分析结果显示结构化建议", "一元一次方程" in trial_result, trial_result[:120])
    check("试分析不生成正式错题", mistake_count() == before_trial,
          "前 %d 条，后 %d 条" % (before_trial, mistake_count()))
    time.sleep(0.3)
    shot(trial_dialog, "84b-pack-ai-trial-result.png")
    if trial_dialog is not None:
        trial_dialog.close()
    wait_child_gone(classes_ai, "aiTrialDialog", timeout=30)

    click(classes_ai, "openStudentsButton")
    students_ai = find_window(app5, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    click(students_ai, "openMistakesButton")
    mistakes_ai = open_mistakes(app5, students_ai)
    detail_ai = wait_text(mistakes_ai, "mistakeDetail", "状态：已确认", timeout=90)
    check("正式分析前记录是已确认", "状态：已确认" in detail_ai,
          [line for line in detail_ai.splitlines() if "状态：" in line][:1])
    click(mistakes_ai, "analyzeButton")
    if wait_button_text(mistakes_ai, "继续重新分析", timeout=30):
        check("已确认记录重新分析前有明确提示", True)
        click_button_text(mistakes_ai, "继续重新分析")
    else:
        check("已确认记录重新分析前有明确提示", False, "没有出现提示")
    check("真实分析前弹出云端发送确认", has_child(mistakes_ai, "aiConsentDialog", timeout=40))
    click(mistakes_ai, "aiConsentCheck")
    deadline = time.time() + 30
    while time.time() < deadline and not ctl(mistakes_ai, "aiConsentOk").is_enabled():
        time.sleep(0.1)
    click(mistakes_ai, "aiConsentOk")
    suggestion = wait_child(mistakes_ai, "aiSuggestionDialog", timeout=90)
    check("真实分析返回建议预览", bool(suggestion))
    body = text_of(mistakes_ai, "aiSuggestionBody", "") or ""
    check("真实建议含结构化知识点与观察错误", "一元一次方程" in body and "移项" in body, body[:100])
    time.sleep(0.3)
    shot(suggestion, "85-pack-ai-real-suggestion.png")
    click(mistakes_ai, "aiApplySuggestion")
    check("采用真实建议后回到待核对", wait_child_gone(mistakes_ai, "aiSuggestionDialog", timeout=60))
    pending = wait_text(mistakes_ai, "mistakeDetail", "状态：待核对", timeout=60)
    check("采用真实建议后状态为待核对", "状态：待核对" in pending,
          [line for line in pending.splitlines() if "状态：" in line][:1])

    click(mistakes_ai, "editMistakeButton")
    wait_child(mistakes_ai, "questionText", timeout=60)
    fill(mistakes_ai, "observedErrors", "老师核对：移项时符号没有变")
    click(mistakes_ai, "mistakeSave")
    check("老师修改真实建议后保存成功", wait_child_gone(mistakes_ai, "mistakeSave", timeout=60))
    click(mistakes_ai, "confirmMistakeButton")
    wait_button_text(mistakes_ai, "确认无误", timeout=30)
    click_button_text(mistakes_ai, "确认无误")
    confirmed_ai = wait_text(mistakes_ai, "mistakeDetail", "状态：已确认", timeout=60)
    check("老师确认后状态为已确认", "状态：已确认" in confirmed_ai,
          [line for line in confirmed_ai.splitlines() if "状态：" in line][:1])
    check("确认结果标注真实 AI 建议", "真实 AI 建议" in confirmed_ai,
          [line for line in confirmed_ai.splitlines() if "分析来源" in line][:1])
    check("老师修改后的观察错误保留", "老师核对：移项时符号没有变" in confirmed_ai, confirmed_ai[:120])
    shot(mistakes_ai, "86-pack-ai-real-confirmed.png")

    kill_tree(proc5.pid)
    proc5.wait(30)
    proc6 = spawn(["--data-dir", str(DATA)], "app6", env=clean_env({
        "MISTAKE_PHOTO_PATH": str(photo), "MISTAKE_SHOT_DIR": str(SHOTS),
        "MISTAKE_AI_MODE": "real", "MISTAKE_AI_API_KEY": "sk-env-stub-142",
        "MISTAKE_AI_BASE_URL": stub_base}))
    app6 = attach(proc6)
    login7 = find_window(app6, TITLE, require_id="loginUsername", timeout=90)
    fill(login7, "loginUsername", username)
    fill(login7, "loginPassword", "pack-pass-1234")
    click(login7, "loginButton")
    classes_ai2 = find_window(app6, TITLE, require_id="currentUser", timeout=90)
    wait_text(classes_ai2, "classStatus", "共 1 个班级", timeout=60)
    click(classes_ai2, "openStudentsButton")
    students_ai2 = find_window(app6, TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    click(students_ai2, "openMistakesButton")
    mistakes_ai2 = open_mistakes(app6, students_ai2)
    reopened_ai = wait_text(mistakes_ai2, "mistakeDetail", "状态：已确认", timeout=90)
    check("重启后真实分析结果与确认状态一致", "状态：已确认" in reopened_ai and "真实 AI 建议" in reopened_ai,
          [line for line in reopened_ai.splitlines() if "状态：" in line or "分析来源" in line][:2])
    shot(mistakes_ai2, "87-pack-ai-reopened.png")
    kill_tree(proc6.pid)
    proc6.wait(30)
    stop_ai_stub()

    # ---------------------------------------------------------------- 10 换账号看不到别人的数据
    proc7 = spawn(["--data-dir", str(DATA)], "app7", env=clean_env({}))
    app7 = attach(proc7)
    login7b = find_window(app7, TITLE, require_id="loginUsername", timeout=90)
    fill(login7b, "loginUsername", username)
    fill(login7b, "loginPassword", "pack-pass-1234")
    click(login7b, "loginButton")
    classes3 = find_window(app7, TITLE, require_id="currentUser", timeout=90)
    click(classes3, "logoutButton")
    login3 = find_window(app7, TITLE, require_id="loginUsername", timeout=90)
    check("退出登录回到登录窗口", login3 is not None)
    click(login3, "registerLink")
    wait_text(login3, "registerStatus", "", timeout=2)
    fill(login3, "registerUsername", "pack_b_" + uuid.uuid4().hex[:6])
    fill(login3, "registerPassword", "pack-pass-5678")
    fill(login3, "registerConfirm", "pack-pass-5678")
    click(login3, "registerButton")
    classes4 = find_window(app7, TITLE, require_id="currentUser", timeout=90)
    deadline = time.time() + 60
    while time.time() < deadline and (text_of(classes4, "classStatus", "") or ""):
        time.sleep(0.3)
    check("换账号后看不到别人的班级", not (text_of(classes4, "classStatus", "") or ""),
          text_of(classes4, "classStatus", ""))
    check("换账号后显示空列表提示", has_child(classes4, "emptyLabel", timeout=3))
    shot(classes4, "70-pack-other-account.png")

    # ---------------------------------------------------------------- 汇总
    failed = [item for item in results if not item[1]]
    print("", flush=True)
    print("共 %d 项，通过 %d，失败 %d" % (len(results), len(results) - len(failed), len(failed)), flush=True)
    for name, _ok, detail in failed:
        print("失败：%s ｜ %s" % (name, detail), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        stop_ai_stub()
        for item in list(procs):
            stop(item)
