"""当前版本的 EXE 自定义 AI 服务全流程测试（连本地桩，不是真实服务）。

流程：解压当前版本 ZIP → 干净环境启动 EXE → 注册 → 建班/建虚构学生 →
AI 设置里先验证 Go（旧环境变量不生效），再选自定义服务填地址/模型/密钥并保存 →
自制题图试分析 → 录入错题并正式分析 → 老师修改并确认 → 关闭重开检查结果与密钥清除。
"""
import atexit
import ctypes
import faulthandler
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from app.version import VERSION  # noqa: E402
os.environ["PACK_TEST_VERSION"] = VERSION
import verify_mistakes_pack as V  # noqa: E402

LOG_PATH = ROOT / "docs" / "verify-ai-exe.txt"
SECRET = "sk-go-exe-stub-146"
CUSTOM_SECRET = "sk-custom-exe-stub-146"
USERNAME = "exe_146_" + str(int(time.time()))[-6:]
PASSWORD = "exe-pass-146"
CLASS_NAME = "初一（3）班"
STUDENT_NAME = "虚构学生甲"
CUSTOM_NAME = "测试云"
CUSTOM_MODEL = "vision-x"


class Tee:
    def __init__(self, path):
        self.fh = open(str(path), "w", encoding="utf-8", newline="\n")
        self.stdout = sys.stdout

    def write(self, text):
        self.fh.write(text)
        self.fh.flush()
        encoding = self.stdout.encoding or "utf-8"
        self.stdout.write(text.encode(encoding, "replace").decode(encoding))

    def flush(self):
        self.stdout.flush()
        self.fh.flush()


def check(name, ok, detail=""):
    V.check(name, bool(ok), detail)


def _stub_control(stub_base, path, mode, stub_proc=None):
    """把设置发给本地测试桩；本机回环偶发 WinError 10013，短重试。

    仍失败时把桩的错误输出带出来，避免只看到连接异常。
    """
    data = json.dumps({"mode": mode}).encode("utf-8")
    last = None
    for _ in range(6):
        request = urllib.request.Request(
            stub_base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            return
        except OSError as exc:
            last = exc
            time.sleep(1.0)
    state = "未知"
    if stub_proc is not None:
        state = "运行中" if stub_proc.poll() is None else "已退出，退出码 %s" % stub_proc.poll()
    raise RuntimeError("桩控制请求失败：%s；桩进程%s" % (last, state))


def set_stub_usage(stub_base, mode, stub_proc=None):
    """让本地桩在响应里返回指定形态的用量（full/partial/zeros/nulls/none）。"""
    _stub_control(stub_base, "/__usage", mode, stub_proc)


def set_stub_fail(stub_base, mode, stub_proc=None):
    """让本地桩对图片请求返回失败（400）或恢复正常（ok），用于覆盖失败分支。"""
    _stub_control(stub_base, "/__fail", mode, stub_proc)


def trial_expect(classes, keyword, timeout=60):
    """点「开始试分析」，等状态出现关键字；遇到连接类失败时重试。

    本机回环连接偶发 WinError 10013，可能连续几秒不可用，所以多试几次。
    """
    text = ""
    for attempt in range(6):
        V.click(classes, "aiTrialStart")
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = V.text_of(classes, "aiTrialStatus", "") or ""
            if keyword in text:
                return text
            if "无法连接" in text or "读取服务响应失败" in text:
                break
            time.sleep(0.4)
        if attempt < 5:
            time.sleep(2.0)
    return text


def sha256_of(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def make_math_photo(path):
    """画一张真实数学题图：题面 + 学生的解答过程（含一处去括号错误）。"""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 900, 1150
    image = Image.new("RGB", (width, height), (252, 251, 247))
    draw = ImageDraw.Draw(image)
    draw.rectangle([18, 18, width - 18, height - 18], outline=(210, 205, 195), width=2)

    font_path = None
    for candidate in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                      "C:/Windows/Fonts/simsun.ttc"):
        if os.path.exists(candidate):
            font_path = candidate
            break
    title_font = ImageFont.truetype(font_path, 34) if font_path else ImageFont.load_default()
    body_font = ImageFont.truetype(font_path, 31) if font_path else ImageFont.load_default()

    lines = [
        ("三、解方程（写出必要步骤）", title_font, 46),
        ("2(x − 3) = 5x + 6", title_font, 128),
        ("学生解答：", body_font, 240),
        ("2x − 3 = 5x + 6", body_font, 310),
        ("2x − 5x = 6 + 3", body_font, 376),
        ("−3x = 9", body_font, 442),
        ("x = −3", body_font, 508),
        ("答：x = −3", body_font, 620),
    ]
    for text, font, top in lines:
        draw.text((70, top), text, fill=(35, 35, 40), font=font)
    image.save(str(path), "PNG")
    return path


def kill_stub(proc):
    """连同子进程一起结束；venv 的 python.exe 会再拉起一个解释器。"""
    if proc.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        proc.wait(10)
    except Exception:
        pass


def start_stub(log_path):
    port = V.free_port()
    err_path = Path(str(log_path)).with_suffix(".err")
    err_file = open(str(err_path), "wb")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "ai_stub_server.py"),
         "--port", str(port), "--log", str(log_path), "--billing", "go"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=err_file,
        stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    err_file.close()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            import socket

            sock = socket.create_connection(("127.0.0.1", port), timeout=1)
            sock.close()
            atexit.register(kill_stub, proc)
            return proc, port
        except OSError:
            time.sleep(0.2)
    kill_stub(proc)
    raise RuntimeError("本地 Go 桩没有起来")


def read_stub_log(path):
    records = []
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def visible(window, auto_id):
    try:
        return bool(V.ctl(window, auto_id, timeout=2).is_visible())
    except Exception:
        return False


MODE_ORDER = (
    "模拟分析（默认，不联网）",
    "OpenCode Go（订阅）",
    "OpenCode Console（按量付费）",
    "自定义 AI 服务（OpenAI 兼容）",
)


def send_key(hwnd, vk, scan):
    """把按键直接送进对话框窗口：离屏桌面上没有输入焦点，真实键盘事件到不了。"""
    user32 = ctypes.windll.user32
    lparam = 1 | (scan << 16)
    user32.SendMessageW(hwnd, 0x0100, vk, lparam)
    time.sleep(0.15)
    user32.SendMessageW(hwnd, 0x0101, vk, lparam | 0xC0000000)
    time.sleep(0.25)


def select_mode(window, target, page_ids):
    """用下拉框的键盘导航选模式：UIA 取不到 Qt 下拉的子项，改向对话框窗口发按键消息。"""
    combo = V.ctl(window, "aiModeCombo")
    dialog = V.scan(window, "aiSettingsDialog")
    if dialog is None:
        return False
    hwnd = dialog.handle
    index = MODE_ORDER.index(target)
    for _ in range(3):
        if all(visible(window, auto_id) for auto_id in page_ids):
            return True
        try:
            if combo.is_expanded():
                combo.collapse()
                time.sleep(0.3)
            combo.set_focus()
            time.sleep(0.3)
            send_key(hwnd, 0x24, 0x47)  # Home
            for _ in range(index):
                send_key(hwnd, 0x28, 0x50)  # Down
        except Exception as exc:
            print("选择模式时出错：", type(exc).__name__, exc)
        time.sleep(0.4)
    return all(visible(window, auto_id) for auto_id in page_ids)


def select_go_mode(window):
    return select_mode(window, "OpenCode Go（订阅）", ("aiGoModelCombo", "aiKeyEdit"))


def select_custom_mode(window):
    return select_mode(window, "自定义 AI 服务（OpenAI 兼容）",
                       ("aiCustomNameEdit", "aiCustomBaseEdit", "aiCustomModelEdit", "aiKeyEdit"))


def start_watchdog(path, idle_limit=240):
    """日志长时间没有新行时转储线程栈并退出，避免 UIA 调用卡死整轮测试。"""

    def run():
        last = -1.0
        idle = 0.0
        while True:
            time.sleep(5)
            try:
                mtime = os.path.getmtime(str(path))
            except OSError:
                continue
            if mtime != last:
                last = mtime
                idle = 0.0
                continue
            idle += 5
            if idle >= idle_limit:
                print("\n[watchdog] 超过 %d 秒没有日志输出，转储线程栈" % idle_limit, flush=True)
                faulthandler.dump_traceback()
                os._exit(3)

    threading.Thread(target=run, daemon=True).start()


def main():
    log_file = Tee(LOG_PATH)
    sys.stdout = log_file
    sys.stderr = log_file
    work = ROOT / "tests" / ".tmp-test" / ("exe-146-" + str(int(time.time()))[-6:])
    work.mkdir(parents=True)
    V.WORK = work
    V.PROFILE_PATH = work / "profile"
    V.PROG_ROOT = work / "解压件"
    V.PROG = V.PROG_ROOT / V.NAME
    V.EXE = V.PROG / (V.NAME + ".exe")
    V.DATA = work / "数据 主库"
    V.PHOTOS = work / "题图"
    V.SHOTS = ROOT / "docs" / "screenshots"
    V.SHOTS.mkdir(parents=True, exist_ok=True)
    V.PHOTOS.mkdir(parents=True, exist_ok=True)
    V.DATA.mkdir(parents=True, exist_ok=True)
    start_watchdog(LOG_PATH)

    from logmeta import environment_lines

    for line in environment_lines():
        print(line)
    print("")

    print("== 一、%s 交付包与 EXE" % VERSION)
    zip_path = ROOT / "build" / "程序包" / (V.NAME + "-" + VERSION + ".zip")
    check("%s 桌面 ZIP 存在" % VERSION, zip_path.is_file(), zip_path)
    if not zip_path.is_file():
        return 1
    print("ZIP：%s 字节，SHA-256 %s" % (zip_path.stat().st_size, sha256_of(zip_path)))
    with zipfile.ZipFile(str(zip_path)) as zf:
        zf.extractall(str(V.PROG_ROOT))
    check("从 %s ZIP 解压出 EXE" % VERSION, V.EXE.is_file(), V.EXE)
    print("EXE：%s 字节，SHA-256 %s" % (V.EXE.stat().st_size, sha256_of(V.EXE)))

    stub_log = work / "go-stub.jsonl"
    stub, port = start_stub(stub_log)
    stub_base = "http://127.0.0.1:%d" % port
    photo = make_math_photo(V.PHOTOS / "自制题图-解方程.png")

    env = V.clean_env({
        "MISTAKE_PHOTO_PATH": str(photo),
        "MISTAKE_TRIAL_PHOTO_PATH": str(photo),
        "MISTAKE_SHOT_DIR": str(V.SHOTS),
        "MISTAKE_AI_BASE_URL": "https://opencode.ai/inference/openai/v1",
        "MISTAKE_AI_MODE": "real",
        "MISTAKE_AI_API_KEY": "sk-poison-console-145",
        "MISTAKE_AI_MODEL": "qwen3.8-max",
        "MISTAKE_AI_TIMEOUT": "30",
    })

    print("\n== 二、首次启动默认模拟，注册并建班/学生")
    proc = V.spawn(["--data-dir", str(V.DATA), "--create-data-dir",
                    "--ai-test-base", stub_base], "go1", env=env)
    app = V.attach(proc)
    login = V.find_window(app, V.TITLE, require_id="loginUsername", timeout=90)
    V.click(login, "registerLink")
    V.fill(login, "registerUsername", USERNAME)
    V.fill(login, "registerPassword", PASSWORD)
    V.fill(login, "registerConfirm", PASSWORD)
    V.click(login, "registerButton")
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=90)
    V.create_class(app, classes, CLASS_NAME)
    V.wait_text(classes, "classStatus", "共 1 个班级", timeout=60)
    V.click(classes, "openAiSettingsButton")
    V.wait_child(classes, "aiSettingsDialog", timeout=60)
    default_status = V.text_of(classes, "aiSettingsStatus", "") or ""
    check("首次启动状态写明模拟（不发送）", "模拟分析（不发送）" in default_status, default_status)
    check("旧 Console 环境变量未改变首次启动模式", "模拟分析（不发送）" in default_status, default_status)
    check("首次启动不显示真实模式密钥框", not visible(classes, "aiKeyEdit"))
    V.click(classes, "aiSettingsCancel")
    V.wait_child_gone(classes, "aiSettingsDialog", timeout=30)
    check("启动后没有任何云端请求", not read_stub_log(stub_log), len(read_stub_log(stub_log)))

    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    V.add_student(app, students, STUDENT_NAME)
    V.wait_text(students, "studentStatus", "共 1 人", timeout=60)
    students.close()
    time.sleep(0.8)
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=60)

    print("\n== 二b、在打包版自己的数据库里开启「个体数据外发」开关")
    # T30 改写①（本节前提，与 t26 源码级口径一致）
    # 原断言：配好自定义通道后，点「分析这条错题」→ 确认 → 弹出 AI 建议并采用（1.8.1 口径）。
    # 原依据：1.8.1「真实模式 = 老师点确认就发送」（数智伴学_Agent开发规格.md 1.8.1 章）。
    # 新断言：先按 R1-14 在**EXE 自己那个库**里把账号开关置为开启并读回确认；后续断言不变。
    # 改写依据：R1-07 + R1-14 裁定「默认不外发；教师账号显式开启后才真调用」。
    #           桌面 UI 同一开关的入口是「个体数据外发（默认关闭）」（src/desktop/ui.py:4289），
    #           这里写的是它背后同一个 users.ai_individual_allowed 列。
    conn = sqlite3.connect(str(V.DATA / "app.db"))
    try:
        conn.execute("UPDATE users SET ai_individual_allowed=1 WHERE username=?", (USERNAME,))
        conn.commit()
        allowed = conn.execute(
            "SELECT ai_individual_allowed FROM users WHERE username=?", (USERNAME,)).fetchone()[0]
    finally:
        conn.close()
    check("二b.1 EXE 库里的账号开关已开启（否则六入口一律 403 ai_isolation）",
          int(allowed) == 1, allowed)


    print("\n== 三、AI 设置里选择 Go、填写密钥并保存")
    V.click(classes, "openAiSettingsButton")
    dialog = V.wait_child(classes, "aiSettingsDialog", timeout=60)
    check("AI 设置窗口已打开", bool(dialog))
    selected = select_go_mode(classes)
    check("分析方式选中 Go", selected)
    check("Go 页出现图片模型下拉", visible(classes, "aiGoModelCombo"))
    V.fill_ascii(classes, "aiKeyEdit", SECRET)
    V.click(classes, "aiSettingsSave")
    time.sleep(1.0)
    check("保存后设置窗口不自动关闭", V.scan(classes, "aiSettingsDialog") is not None)
    status = V.text_of(classes, "aiSettingsStatus", "") or ""
    check("保存后状态为已配置但未验证", "尚未验证" in status, status)
    sources = V.text_of(classes, "aiSettingsSources", "") or ""
    check("界面显示 Go 服务名", "OpenCode Go（订阅）" in sources, sources[:120])
    check("界面显示完整请求地址", stub_base + "/messages" in sources, sources[:160])
    check("Go 页标明本机测试地址", "测试地址" in sources, sources[:200])
    check("界面不出现旧 Console 地址", "inference/openai/v1" not in sources, sources[:200])
    check("保存不触发云端请求", not read_stub_log(stub_log), len(read_stub_log(stub_log)))
    V.copy_auto_shot("aiSettingsDialog", "90-go-app-ai-settings.png")
    V.click(classes, "aiSettingsCancel")
    V.wait_child_gone(classes, "aiSettingsDialog", timeout=30)

    print("\n== 四、自制题图试分析（本地桩，图片实际发送）")
    V.click(classes, "openAiTrialButton")
    V.wait_child(classes, "aiTrialPath", timeout=60)
    trial = V.scan(classes, "aiTrialDialog")
    config = V.text_of(classes, "aiTrialConfig", "") or ""
    check("试分析显示 Go 服务名", "OpenCode Go（订阅）" in config, config[:120])
    check("试分析显示 /messages 地址", "/messages" in config, config[:160])
    check("试分析题图已带入", str(photo) in (V.text_of(classes, "aiTrialPath", "") or ""),
          V.text_of(classes, "aiTrialPath", ""))
    V.ctl(classes, "aiTrialConsent").toggle()
    trial_status = trial_expect(classes, "试分析成功")
    check("试分析成功", "试分析成功" in trial_status, trial_status)
    check("标明 Go 计费来源", "计费来源：go" in trial_status, trial_status)
    check("标明费用为 0", "费用：$0" in trial_status, trial_status)
    check("标明额度计入", "额度计入" in trial_status, trial_status)
    result = V.text_of(classes, "aiTrialResult", "") or ""
    check("试分析结果含结构化建议", "一元一次方程" in result, result[:100])
    requests = read_stub_log(stub_log)
    check("试分析确实发出一次请求", len(requests) == 1, len(requests))
    first = requests[0] if requests else {}
    check("请求走 /messages", str(first.get("path", "")).endswith("/messages"), first.get("path"))
    check("请求带 Go 会话标识", bool(first.get("session")), first.get("session"))
    check("请求带真实软件 UA", ("math-mistakes-desktop/" + VERSION) in str(first.get("user_agent", "")),
          first.get("user_agent"))
    check("请求实际包含图片", bool(first.get("has_image")), first)
    check("请求文字不含学生姓名", STUDENT_NAME not in str(first.get("text", "")), "未出现学生姓名")
    V.copy_auto_shot("aiTrialDialog", "91-go-app-trial.png")
    if trial is not None:
        V.shot(trial, "91b-go-app-trial-result.png")
        trial.close()
    V.wait_child_gone(classes, "aiTrialDialog", timeout=30)

    print("\n== 四之二、自定义 AI 服务：填写服务名/地址/模型/密钥并试分析")
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=60)
    V.click(classes, "openAiSettingsButton")
    V.wait_child(classes, "aiSettingsDialog", timeout=60)
    check("分析方式选中自定义服务", select_custom_mode(classes))
    check("自定义页出现服务名输入", visible(classes, "aiCustomNameEdit"))
    check("自定义页出现接口地址输入", visible(classes, "aiCustomBaseEdit"))
    check("自定义页出现模型输入", visible(classes, "aiCustomModelEdit"))
    print("[步骤] 填写自定义服务配置", flush=True)
    V.fill(classes, "aiCustomNameEdit", CUSTOM_NAME)
    V.fill(classes, "aiCustomBaseEdit", stub_base + "/custom/v1/")
    V.fill(classes, "aiCustomModelEdit", CUSTOM_MODEL)
    V.fill_ascii(classes, "aiKeyEdit", CUSTOM_SECRET)
    print("[步骤] 自定义服务配置已填写", flush=True)
    time.sleep(0.5)
    preview = V.text_of(classes, "aiCustomUrl", "") or ""
    check("自定义页显示完整请求地址",
          preview == "完整请求地址：" + stub_base + "/custom/v1/chat/completions", preview)
    check("自定义页地址去掉了重复斜杠", preview.count("/custom/v1/chat/completions") == 1, preview)
    V.click(classes, "aiSettingsSave")
    time.sleep(1.0)
    status = V.text_of(classes, "aiSettingsStatus", "") or ""
    check("自定义保存后为已配置未验证", "尚未验证" in status, status)
    sources = V.text_of(classes, "aiSettingsSources", "") or ""
    check("设置页显示自定义服务名", CUSTOM_NAME in sources, sources[:160])
    check("设置页显示自定义完整地址", "/custom/v1/chat/completions" in sources, sources[:200])
    check("自定义页说明不代表已接通", "不代表已经连接成功" in sources, sources[:240])
    check("自定义保存不触发请求", len(read_stub_log(stub_log)) == 1, len(read_stub_log(stub_log)))
    V.copy_auto_shot("aiSettingsDialog", "92-custom-app-ai-settings.png")
    V.click(classes, "aiSettingsCancel")
    V.wait_child_gone(classes, "aiSettingsDialog", timeout=30)

    V.click(classes, "openAiTrialButton")
    V.wait_child(classes, "aiTrialPath", timeout=60)
    trial = V.scan(classes, "aiTrialDialog")
    config = V.text_of(classes, "aiTrialConfig", "") or ""
    check("自定义试分析显示服务名", CUSTOM_NAME in config, config[:120])
    check("自定义试分析显示完整地址", "/custom/v1/chat/completions" in config, config[:200])
    before = len(read_stub_log(stub_log))
    V.click(classes, "aiTrialStart")
    time.sleep(0.8)
    blocked = V.text_of(classes, "aiTrialStatus", "") or ""
    check("自定义试分析未勾选被拦下", "勾选同意" in blocked, blocked)
    check("自定义试分析未勾选不发请求", len(read_stub_log(stub_log)) == before, len(read_stub_log(stub_log)) - before)
    V.ctl(classes, "aiTrialConsent").toggle()
    trial_status = trial_expect(classes, "自定义服务试分析成功")
    check("自定义试分析成功", "自定义服务试分析成功" in trial_status, trial_status)
    check("自定义结果不把 Go 计费来源当成自定义服务的",
          "计费来源：尚未确认" in trial_status and "计费来源：go" not in trial_status, trial_status)
    check("自定义结果不显示额度计入", "额度计入" not in trial_status, trial_status)
    result = V.text_of(classes, "aiTrialResult", "") or ""
    check("自定义试分析结果含结构化建议", "一元一次方程" in result, result[:100])
    requests = read_stub_log(stub_log)
    last = requests[-1] if requests else {}
    check("自定义试分析走 chat 路径",
          str(last.get("path", "")).endswith("/custom/v1/chat/completions"), last.get("path"))
    check("自定义请求不带 Go 会话标识", not last.get("session"), last.get("session"))
    check("自定义请求带 %s UA" % VERSION, ("math-mistakes-desktop/" + VERSION) in str(last.get("user_agent", "")),
          last.get("user_agent"))
    check("自定义请求实际包含图片", bool(last.get("has_image")), last)
    check("自定义请求不含学生姓名", STUDENT_NAME not in str(last.get("text", "")), "未出现学生姓名")
    V.copy_auto_shot("aiTrialDialog", "93-custom-app-trial.png")
    if trial is not None:
        V.shot(trial, "93b-custom-app-trial-result.png")

    print("\n== 四之三、用量缺失与 0 的显示（同一 EXE、同一下拉配置）")
    set_stub_usage(stub_base, "partial", stub)
    partial = trial_expect(classes, "completion 尚未确认")
    check("EXE 部分用量缺失时缺项写尚未确认", "completion 尚未确认" in partial, partial)
    check("EXE 部分用量缺失时费用写尚未确认", "费用：尚未确认" in partial, partial)
    check("EXE 部分用量缺失时不显示 None", "None" not in partial, partial)
    set_stub_usage(stub_base, "zeros", stub)
    zeros = trial_expect(classes, "prompt 0 / completion 0 / total 0")
    check("EXE 用量为 0 时正常显示 0", "prompt 0 / completion 0 / total 0" in zeros, zeros)
    check("EXE 费用为 0 时显示 $0", "费用：$0" in zeros, zeros)
    set_stub_usage(stub_base, "none", stub)
    no_usage = trial_expect(classes, "用量：尚未确认")
    check("EXE 服务未返回用量时写尚未确认", "用量：尚未确认" in no_usage, no_usage)
    check("EXE 服务未返回用量时不显示 None", "None" not in no_usage, no_usage)
    set_stub_usage(stub_base, "full", stub)

    if trial is not None:
        trial.close()
    V.wait_child_gone(classes, "aiTrialDialog", timeout=30)

    print("\n== 五、录入错题、正式分析、老师修改并确认")
    V.click(classes, "openStudentsButton")
    students2 = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    V.click(students2, "openMistakesButton")
    mistakes = V.open_mistakes(app, students2)
    V.click(mistakes, "newMistakeButton")
    V.wait_child(mistakes, "questionText", timeout=60)
    V.fill(mistakes, "questionText", "解方程：2(x − 3) = 5x + 6")
    V.fill(mistakes, "solutionText", "2x − 3 = 5x + 6；2x − 5x = 6 + 3；−3x = 9；x = −3")
    V.click(mistakes, "choosePhoto")
    V.wait_text(mistakes, "photoInfo", "像素", timeout=20)
    V.click(mistakes, "mistakeSave")
    V.wait_child_gone(mistakes, "mistakeSave", timeout=60)
    V.wait_text(mistakes, "mistakeDetail", "解方程", timeout=60)

    set_stub_fail(stub_base, "400")
    V.click(mistakes, "analyzeButton")
    check("正式分析前弹出确认框", V.has_child(mistakes, "aiConsentDialog", timeout=40))
    consent_text = V.text_of(mistakes, "aiConsentText", "") or ""
    check("确认框写明可能产生费用或消耗额度", "费用或消耗" in consent_text, consent_text[:120])
    check("确认框说明不单独发送姓名班级账号", "不会单独发送学生姓名" in consent_text, consent_text[:200])
    V.ctl(mistakes, "aiConsentCheck").toggle()
    deadline = time.time() + 30
    while time.time() < deadline and not V.ctl(mistakes, "aiConsentOk").is_enabled():
        time.sleep(0.1)
    V.click(mistakes, "aiConsentOk")
    suggestion = None
    try:
        suggestion = V.wait_child(mistakes, "aiSuggestionDialog", timeout=90)
    except RuntimeError:
        failed = V.text_of(mistakes, "mistakeStatus", "") or ""
        check("分析失败时照片与录入内容保留", "已保存" in failed, failed[:160])
        V.click(mistakes, "analyzeButton")
        check("失败后主动重试会再确认", V.has_child(mistakes, "aiConsentDialog", timeout=40))
        set_stub_fail(stub_base, "ok")
        V.ctl(mistakes, "aiConsentCheck").toggle()
        deadline = time.time() + 30
        while time.time() < deadline and not V.ctl(mistakes, "aiConsentOk").is_enabled():
            time.sleep(0.1)
        V.click(mistakes, "aiConsentOk")
        # T30 改写②（把「等不到控件」从崩溃改成空态断言）
        # 原断言：suggestion = V.wait_child(mistakes, "aiSuggestionDialog", timeout=90)
        #         等不到就抛 RuntimeError，整套件在 :552 被打断
        #         （t29 实测：RuntimeError: 等不到控件 aiSuggestionDialog）。
        # 原依据：1.8.1「失败后重试必定弹出 AI 建议窗」。
        # 新断言：捕获后返回 None，由下面「正式分析返回建议预览」写成带原因的空态断言。
        # 改写依据：R1-14 下账号开关关闭时界面只写 403 文案、不弹建议窗，这是可预期空态；
        #           空态必须是断言（有失败标题），不是异常替我们报错。
        try:
            suggestion = V.wait_child(mistakes, "aiSuggestionDialog", timeout=90)
        except RuntimeError:
            suggestion = None
    check("正式分析返回建议预览（空态：等不到建议窗时给明确原因，不再抛异常）",
          bool(suggestion),
          "等不到 aiSuggestionDialog；错题状态：%s"
          % ((V.text_of(mistakes, "mistakeStatus", "") or "")[:100]))
    if suggestion is None:
        # T30：建议窗没出现时不再往下点 aiApplySuggestion（那会抛「找不到控件」把整套件打断）。
        # 这里显式收尾：把失败原因留在日志里，退出码由失败条数决定。
        kill_stub(stub)
        failed_now = [item for item in V.results if not item[1]]
        print("\n合计 %d 项，通过 %d 项，失败 %d 项"
              % (len(V.results), len(V.results) - len(failed_now), len(failed_now)))
        print("退出码：%d" % (1 if failed_now else 0))
        return 1 if failed_now else 0
    body = V.text_of(mistakes, "aiSuggestionBody", "") or ""
    check("建议来自实际请求", "一元一次方程" in body, body[:80])
    V.click(mistakes, "aiApplySuggestion")
    pending = V.wait_text(mistakes, "mistakeDetail", "状态：待核对", timeout=60)
    check("采用建议后进入待核对", "状态：待核对" in pending, pending[:120])
    check("分析来源标为自定义服务", "自定义" in pending, pending[:160])
    V.click(mistakes, "editMistakeButton")
    V.wait_child(mistakes, "questionText", timeout=60)
    V.fill(mistakes, "observedErrors", "老师核对：去括号时 2 没有乘 −3，正确应为 2x − 6。")
    V.click(mistakes, "mistakeSave")
    V.wait_child_gone(mistakes, "mistakeSave", timeout=60)
    V.click(mistakes, "confirmMistakeButton")
    V.wait_button_text(mistakes, "确认无误", timeout=30)
    V.click_button_text(mistakes, "确认无误")
    confirmed = V.wait_text(mistakes, "mistakeDetail", "状态：已确认", timeout=60)
    check("老师确认后状态为已确认", "状态：已确认" in confirmed)
    check("老师修改内容已保存", "老师核对：去括号时 2 没有乘 −3" in confirmed, confirmed[:120])
    V.shot(mistakes, "94-custom-app-confirmed.png")
    requests = read_stub_log(stub_log)
    formal = [item for item in requests
              if str(item.get("path", "")).endswith("/custom/v1/chat/completions")]
    check("正式分析也走自定义 chat 路径", len(formal) >= 2,
          [item.get("path") for item in requests])
    check("自定义请求都不带 Go 会话标识",
          not any(item.get("session") for item in formal), [item.get("session") for item in formal])

    print("\n== 六、关闭重开：结果在、密钥不在、仍走自定义配置")
    V.kill_tree(proc.pid)
    proc.wait(30)
    env2 = V.clean_env({
        "MISTAKE_PHOTO_PATH": str(photo),
        "MISTAKE_SHOT_DIR": str(V.SHOTS),
        "MISTAKE_AI_BASE_URL": "https://opencode.ai/inference/openai/v1",
        "MISTAKE_AI_MODE": "real",
        "MISTAKE_AI_API_KEY": "sk-poison-console-146",
        "MISTAKE_AI_MODEL": "qwen3.8-max",
    })
    proc2 = V.spawn(["--data-dir", str(V.DATA), "--ai-test-base", stub_base], "app2", env=env2)
    app2 = V.attach(proc2)
    try:
        cls2 = V.find_window(app2, V.TITLE, require_id="currentUser", timeout=15)
    except RuntimeError:
        login2 = V.find_window(app2, V.TITLE, require_id="loginUsername", timeout=90)
        V.fill(login2, "loginUsername", USERNAME)
        V.fill(login2, "loginPassword", PASSWORD)
        V.click(login2, "loginButton")
        cls2 = V.find_window(app2, V.TITLE, require_id="currentUser", timeout=90)
    V.wait_text(cls2, "classStatus", "共 1 个班级", timeout=60)
    V.click(cls2, "openStudentsButton")
    students3 = V.find_window(app2, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    V.click(students3, "openMistakesButton")
    mistakes2 = V.open_mistakes(app2, students3)
    reopened = V.wait_text(mistakes2, "mistakeDetail", "状态：已确认", timeout=90)
    check("重开后记录仍为已确认", "状态：已确认" in reopened)
    check("重开后分析内容仍在", "一元一次方程" in reopened or "自定义" in reopened, reopened[:160])
    check("重开后老师修改仍在", "老师核对：去括号时 2 没有乘 −3" in reopened)
    note2 = V.text_of(mistakes2, "aiConfigNote", "") or ""
    check("重开后仍显示自定义服务名", CUSTOM_NAME in note2, note2[:120])
    check("重开后密钥已清除", "未配置" in note2 or "密钥" in note2, note2[:160])
    V.shot(mistakes2, "95-custom-app-reopened.png")

    print("\n== 七、数据库与密钥")
    conn = sqlite3.connect(str(V.DATA / "app.db"))
    try:
        mode_row = conn.execute("SELECT value FROM app_meta WHERE key = ?",
                                ("ai_mode:%d" % 1,)).fetchone()
        status_row = conn.execute("SELECT status FROM mistakes ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    check("数据库保存了自定义模式", mode_row is not None and mode_row[0] == "custom", mode_row)
    check("数据库记录为已确认", status_row is not None and status_row[0] == "confirmed", status_row)
    leaked = []
    for base, _dirs, files in os.walk(str(V.DATA)):
        for name in files:
            path = Path(base) / name
            if path.suffix.lower() in (".db", ".db-wal", ".db-shm"):
                try:
                    if CUSTOM_SECRET.encode("utf-8") in path.read_bytes() or SECRET.encode("utf-8") in path.read_bytes():
                        leaked.append(str(path))
                except OSError:
                    continue
    check("数据库与 WAL 不含密钥", not leaked, leaked)

    V.stop(proc2)

    print("\n== 八、个体 AI 开关：关闭 → 六入口 403；开启 → 立即可用")
    # T30 新增（整段为新断言，不替换任何既有断言）：EXE 关掉后在同一个库上验双路径。
    from app import api as api_mod
    from desktop.service import ApiError as ServiceApiError, DesktopService

    gate_service = DesktopService(str(V.DATA))
    gate_service.login(USERNAME, PASSWORD)
    gate_service.save_ai_settings("custom", stub_base + "/custom/v1", "vision-x",
                                  CUSTOM_SECRET, service_name="测试云")
    conn = sqlite3.connect(str(V.DATA / "app.db"))
    try:
        class_id, student_id = conn.execute(
            "SELECT c.id, s.id FROM classes c JOIN students s ON s.class_id=c.id LIMIT 1").fetchone()
        mistake_id = conn.execute(
            "SELECT id FROM mistakes ORDER BY id DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    off = gate_service.set_ai_individual(False)
    check("八.1 关闭后账号状态是「未授权」",
          off.get("ai_individual_allowed") is False, off.get("ai_individual_allowed"))

    def expect_isolation(name, call):
        try:
            result = call()
        except ServiceApiError as exc:
            check(name,
                  exc.status == 403 and exc.code == "ai_isolation" and "已停用" in exc.message,
                  (exc.status, exc.code, (exc.message or "")[:60]))
            return
        except Exception as exc:  # noqa: BLE001
            check(name, False, "未按 403 拦断，而是 %s: %s" % (type(exc).__name__, exc))
            return
        check(name, False, "没有抛 403，而是返回了 %r" % (result,))

    expect_isolation("八.2 错题照片分析建议 → 403 ai_isolation",
                     lambda: gate_service.preview_analysis(mistake_id, consent=True,
                                                           task_id="t30-ai"))
    expect_isolation("八.3 错题重新分析（旧入口同一道闸门）→ 403 ai_isolation",
                     lambda: api_mod.api_reanalyze(
                         gate_service._ctx(params={"id": mistake_id}, body={"consent": True})))
    expect_isolation("八.4 个人辅导建议生成 → 403 ai_isolation",
                     lambda: gate_service.generate_student_advice(class_id, student_id,
                                                                  consent=True))
    on = gate_service.set_ai_individual(True)
    check("八.5 重新开启后账号状态是「已授权」",
          on.get("ai_individual_allowed") is True, on.get("ai_individual_allowed"))
    preview = gate_service.preview_analysis(mistake_id, consent=True, task_id="t30-ai-on")
    check("八.6 开启后同一条业务路径不再被闸门拦（失败原因不再是 ai_isolation / 已停用）",
          preview.get("error_code") != "ai_isolation"
          and "已停用" not in (preview.get("error") or ""),
          (preview.get("ok"), preview.get("error_code"), (preview.get("error") or "")[:60]))

    kill_stub(stub)
    failed = [item for item in V.results if not item[1]]
    print("\n合计 %d 项，通过 %d 项，失败 %d 项" % (len(V.results), len(V.results) - len(failed), len(failed)))
    code = 1 if failed else 0
    print("退出码：%d" % code)
    return code


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.exit(code)
