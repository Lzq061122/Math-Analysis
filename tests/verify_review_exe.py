"""1.6.0 最终 EXE 的作答分析实测（独立桌面，窗口不出现）。

流程：交付件校验 → 启动 → 注册/建班/建学生 → 导入 CSV → 关闭并在配置里
打开自定义 AI 服务（本地桩）→ 老师主动分析 → 确认发送范围 → 查看建议、
拒绝 → 再分析、确认生成错题 → 重开后状态与关联仍在，已确认的作答不能再分析。
只使用自制题图与虚构数据，只连本机桩，不访问真实云端。
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import verify_answers_exe as A  # noqa: E402
import verify_mistakes_pack as V  # noqa: E402
from app.version import VERSION  # noqa: E402

ROOT = A.ROOT
NAME = A.NAME
ZIP = os.path.join(ROOT, "build", "程序包", "%s-%s.zip" % (NAME, VERSION))
LOG_PATH = os.path.join(ROOT, "docs", "verify-review-exe.txt")

results = []


class Tee:
    def __init__(self, path):
        self.stream = open(path, "w", encoding="utf-8", newline="\n")
        self.console = sys.stdout

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        try:
            self.console.write(text)
            self.console.flush()
        except Exception:
            pass

    def flush(self):
        self.stream.flush()


_tee = None


def log(text=""):
    _tee.write(text + "\n")


def check(name, ok, detail=""):
    results.append(bool(ok))
    log("%s  %s%s" % ("PASS" if ok else "FAIL", name, "  -> %s" % (detail,) if detail != "" else ""))
    return bool(ok)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def finish(force=None):
    failed = results.count(False)
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (len(results), len(results) - failed, failed))
    code = 0 if failed == 0 else 1
    if force is not None:
        code = force
    log("退出码：%d" % code)
    return code


def set_work(work):
    unpack = os.path.join(work, "解压件")
    prog = os.path.join(unpack, NAME)
    V.WORK = Path(work)
    V.PROG_ROOT = Path(unpack)
    V.PROG = Path(prog)
    V.EXE = Path(os.path.join(prog, NAME + ".exe"))
    V.DATA = Path(os.path.join(work, "数据 主库"))
    V.SHOTS = Path(os.path.join(ROOT, "docs", "screenshots"))
    profile = Path(work) / "profile"
    for sub in ("Desktop", "Documents", "Downloads", "AppData/Roaming",
                "AppData/Local", "AppData/Local/Temp"):
        (profile / sub).mkdir(parents=True, exist_ok=True)
    V.PROFILE_PATH = profile
    return prog


def csv_text():
    headers = ("记录编号,学生姓名,学校编号,题号,题目内容,单元,标准解法,学生步骤,"
               "来源批次,作答日期,缺测,缺测原因")
    rows = [
        "EX1,张三,,1,解方程 3(x-2)=2x+5,一元一次方程,3x-6=2x+5;3x-2x=5+6;x=11,"
        "3x-2=2x+5;3x-2x=5-6;x=-1,九月作业,2026-09-12,否,",
        "EX2,张三,,2,解方程 2x+1=7,一元一次方程,2x=6;x=3,,九月作业,2026-09-12,是,没交",
    ]
    return "\n".join([headers] + rows) + "\n"


SEND_KEY = {"home": (0x24, 0x47), "down": (0x28, 0x50)}


def send_key(hwnd, vk, scan, pause=0.15):
    import ctypes
    lparam = 1 | (scan << 16)
    ctypes.windll.user32.SendMessageW(hwnd, 0x0100, vk, lparam)
    time.sleep(0.05)
    ctypes.windll.user32.SendMessageW(hwnd, 0x0101, vk, lparam | 0xC0000000)
    time.sleep(pause)


def pick_custom_mode(app, dialog):
    """把“分析方式”切到自定义服务：聚焦后按 HOME 再按 3 次 DOWN。"""
    combo = V.scan(dialog, "aiModeCombo")
    combo.set_focus()
    time.sleep(0.3)
    send_key(dialog.handle, *SEND_KEY["home"])
    for _ in range(3):
        send_key(dialog.handle, *SEND_KEY["down"])
    deadline = time.time() + 15
    text = ""
    while time.time() < deadline:
        text = combo.iface_value.CurrentValue or ""
        if "自定义" in text:
            return text
        time.sleep(0.3)
    return text


def stub_requests(log_path):
    records = []
    if not os.path.isfile(log_path):
        return records
    with open(log_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def login(app):
    win = V.find_window(app, V.TITLE, require_id="loginUsername", timeout=120)
    V.fill(win, "loginUsername", "exe_teacher")
    V.fill(win, "loginPassword", "pw123456")
    V.click(win, "loginButton")
    return V.find_window(app, V.TITLE, require_id="currentUser", timeout=90)


def open_answers(app, students):
    V.click(students, "openAnswerReviews")
    return A.wait_window(app, "作答分析")


def stub_state():
    """本地桩进程是否还活着、端口是否可连。"""
    import socket
    proc = V.AI_STUB.get("proc")
    port = V.AI_STUB.get("port") or 0
    alive = bool(proc is not None and proc.poll() is None)
    reachable = False
    if port:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            sock.connect(("127.0.0.1", port))
            reachable = True
        except OSError:
            reachable = False
        finally:
            sock.close()
    return alive, reachable, port


def native_windows(pid):
    """按进程号列出全部顶层窗口：[(句柄, 标题, 是否可见)]，含 UIA 不报告的弹窗。"""
    import win32gui
    import win32process
    found = []

    def _collect(hwnd, _):
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                return True
        except Exception:
            return True
        text = win32gui.GetWindowText(hwnd)
        if text:
            found.append((hwnd, text, bool(win32gui.IsWindowVisible(hwnd))))
        return True

    win32gui.EnumWindows(_collect, None)
    return found


def wait_review(app, answers_win, timeout=90):
    """等分析建议窗口；等不到时把作答列表的状态文字记进日志，便于定位原因。

    建议窗口是作答窗口的子窗口，UIA 的顶层枚举有时看不到它，所以先按进程句柄找。
    """
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, text, visible in native_windows(app.process):
            if "分析建议" not in text or not visible:
                continue
            try:
                return UIAWrapper(UIAElementInfo(hwnd))
            except Exception:
                continue
        time.sleep(0.4)
    status = V.text_of(answers_win, "answerListStatus", "") or ""
    log("等不到分析建议，列表状态：%s" % status)
    alive, reachable, port = stub_state()
    log("本地桩：端口 %d，进程存活=%s，可连接=%s" % (port, alive, reachable))
    titles = []
    for window in app.windows():
        try:
            titles.append(window.window_text())
        except Exception:
            continue
    log("当前窗口标题：%s" % titles)
    log("进程内所有窗口：%s" % [
        "%s(visible=%s)" % (text, visible) for _, text, visible in native_windows(app.process)])
    raise RuntimeError("等不到窗口：分析建议")


def analyse_again(app, answers_win, attempts=3):
    """点「分析这条作答」→ 勾选确认 → 等建议窗。

    本机回环偶发连不上（同 verify_ai_146_ui 已记录的现象），此时产品会提示
    「无法连接 AI 服务…」并保留数据。这里按老师的真实做法再点一次，最多 attempts 次。
    """
    last = None
    for attempt in range(1, attempts + 1):
        V.click(answers_win, "answerAnalyse")
        consent = A.wait_window(app, "确认把这条作答", timeout=20)
        if not consent:
            raise RuntimeError("等不到确认框：确认把这条作答")
        V.click(consent, "answerConsentCheck")
        time.sleep(0.3)
        V.click(consent, "answerConsentOk")
        try:
            return wait_review(app, answers_win, timeout=60)
        except RuntimeError:
            last = V.text_of(answers_win, "answerListStatus", "") or ""
            if "无法连接 AI 服务" not in last or attempt == attempts:
                raise
            log("第 %d 次分析遇到本机回环失败，按老师重试的做法再点一次：%s" % (attempt, last[:40]))
            time.sleep(1.0)
    raise RuntimeError("最后一次状态：%s" % last)


def click_row(win, key):
    """点表格里含指定文字的单元格，选中那一行。"""
    for item in V.table_items(win, "answerListTable"):
        if key in (item.window_text() or ""):
            V.click_center(item, win)
            return True
    return False


def row_texts(win):
    return V.table_text(win, "answerListTable").split(" | ")


def main():
    work = os.path.join(ROOT, "tests", ".tmp-test", "review-exe-" + uuid.uuid4().hex[:6])
    os.makedirs(work)
    prog = set_work(work)
    sub = Path(work) / "sub"
    sub.mkdir()

    from logmeta import environment_lines
    for line in environment_lines():
        log(line)
    log("")

    log("== 一、交付件校验 ==")
    check("%s ZIP 存在" % VERSION, os.path.isfile(ZIP), ZIP)
    if not os.path.isfile(ZIP):
        return finish()
    log("ZIP %d 字节 SHA-256 %s" % (os.path.getsize(ZIP), sha256_of(ZIP)))
    with zipfile.ZipFile(ZIP) as bundle:
        names = bundle.namelist()
        shutil.unpack_archive(ZIP, V.PROG_ROOT)
    check("包内有 EXE", any(name.endswith(NAME + ".exe") for name in names), names[:5])
    check("包内有老师使用说明", any(name.endswith("使用说明.txt") for name in names))
    check("EXE 已解出", os.path.isfile(V.EXE), V.EXE)
    log("EXE %d 字节 SHA-256 %s" % (os.path.getsize(V.EXE), sha256_of(V.EXE)))

    csv_path = os.path.join(work, "九月作业.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(csv_text())

    port = V.start_ai_stub()
    stub_base = "http://127.0.0.1:%d/v1" % port
    log("本地桩：%s" % stub_base)

    log("")
    log("== 二、启动、注册并把作答导入 ==")
    proc = V.spawn(["--data-dir", str(V.DATA), "--create-data-dir"], "app")
    app = V.attach(proc)
    login_win = V.find_window(app, V.TITLE, require_id="loginUsername", timeout=90)
    V.click(login_win, "registerLink")
    V.fill(login_win, "registerUsername", "exe_teacher")
    V.fill(login_win, "registerPassword", "pw123456")
    V.fill(login_win, "registerConfirm", "pw123456")
    V.click(login_win, "registerButton")
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=90)
    V.create_class(app, classes, "初一 2 班")
    V.wait_text(classes, "classStatus", "共 1 个班级", timeout=60)
    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    V.add_student(app, students, "张三")
    V.wait_text(students, "studentStatus", "共 1 人", timeout=60)

    import_win = A.open_import(app, students)
    V.click(import_win, "answerChooseFile")
    A.choose_file(app, csv_path)
    deadline = time.time() + 60
    status = ""
    while time.time() < deadline:
        status = V.text_of(import_win, "answerStatus", "") or ""
        if "共 2 行" in status:
            break
        time.sleep(0.3)
    check("预览两行可导入", "可导入 2" in status, status)
    log("预览表格：%s" % V.table_text(import_win, "answerTable")[:400])
    check("导入按钮已可用", V.ctl(import_win, "answerImportButton").is_enabled())
    before = A._visible_hwnds(app.process)
    V.click(import_win, "answerImportButton")
    check("导入确认框出现", A.confirm_box(app, before))
    deadline = time.time() + 60
    while time.time() < deadline:
        if "导入完成" in (V.text_of(import_win, "answerStatus", "") or ""):
            break
        time.sleep(0.3)
    check("两条作答导入完成", "导入完成" in (V.text_of(import_win, "answerStatus", "") or ""),
          V.text_of(import_win, "answerStatus", ""))
    V.click(import_win, "backToStudents")

    conn = sqlite3.connect(os.path.join(str(V.DATA), "app.db"))
    answers_before = dict((key, (steps, fp)) for key, steps, fp in conn.execute(
        "SELECT record_key, student_steps, fingerprint FROM answers"))
    uid = conn.execute("SELECT id FROM users WHERE username='exe_teacher'").fetchone()[0]
    conn.close()
    check("导入后作答内容已保存", set(answers_before) == {"EX1", "EX2"})

    log("")
    log("== 三、在软件里配置自定义服务（本机桩） ==")
    V.click(students, "backToClasses")
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=60)
    V.click(classes, "openAiSettingsButton")
    dialog = V.wait_child(classes, "aiSettingsDialog", timeout=60)
    mode_text = pick_custom_mode(app, dialog)
    check("分析方式切到自定义服务", "自定义" in mode_text, mode_text)
    V.fill(dialog, "aiCustomNameEdit", "本地模拟服务")
    V.fill_ascii(dialog, "aiCustomBaseEdit", stub_base)
    V.fill_ascii(dialog, "aiCustomModelEdit", "stub-vision")
    V.fill_ascii(dialog, "aiKeyEdit", "sk-exe-review-1234")
    V.click(dialog, "aiSettingsSave")
    V.wait_child_gone(classes, "aiSettingsDialog", timeout=30)
    V.click(classes, "openAiSettingsButton")
    dialog = V.wait_child(classes, "aiSettingsDialog", timeout=60)
    status_note = V.text_of(dialog, "aiSettingsStatus", "") or ""
    check("保存后提示尚未验证", "尚未验证" in status_note, status_note[:120])
    V.click(dialog, "aiSettingsCancel")
    V.wait_child_gone(classes, "aiSettingsDialog", timeout=30)
    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)

    log("")
    log("== 四、老师主动分析并确认发送范围 ==")
    answers_win = open_answers(app, students)
    check("作答分析窗口打开", bool(answers_win))
    hint = V.text_of(answers_win, "answerListHint", "") or ""
    check("窗口说明导入不自动分析", "不会自动分析" in hint, hint[:80])
    headers = V.table_text(answers_win, "answerListTable")
    check("列表有七列内容", headers.count(" | ") >= 6, headers[:120])
    click_row(answers_win, "EX1")
    time.sleep(0.3)
    alive, reachable, stub_port = stub_state()
    check("分析前本地桩可连接", alive and reachable, "端口 %d 存活=%s 可连=%s" % (stub_port, alive, reachable))
    before_windows = A._visible_hwnds(app.process)
    V.click(answers_win, "answerAnalyse")
    deadline = time.time() + 40
    status = ""
    while time.time() < deadline:
        status = V.text_of(answers_win, "answerListStatus", "") or ""
        if status and "正在分析" not in status:
            break
        time.sleep(0.3)
    log("点分析后的状态：%s" % status)
    consent = A.wait_window(app, "确认把这条作答", timeout=20)
    check("真实模式先弹出确认框", bool(consent))
    consent_text = V.text_of(consent, "answerConsentText", "") or ""
    check("确认框说明发送内容", "学生写的步骤" in consent_text, consent_text[:80])
    check("确认框说明不发送身份信息", "不会发送学生姓名" in consent_text)
    check("未勾选时不能开始", not V.ctl(consent, "answerConsentOk").is_enabled())
    check("没有提前发出请求", len(stub_requests(V.AI_STUB["log"])) == 0)
    V.click(consent, "answerConsentCheck")
    time.sleep(0.4)
    check("勾选后可以开始", V.ctl(consent, "answerConsentOk").is_enabled())
    V.click(consent, "answerConsentOk")

    try:
        review = wait_review(app, answers_win, timeout=90)
    except RuntimeError:
        status_now = V.text_of(answers_win, "answerListStatus", "") or ""
        if "无法连接 AI 服务" not in status_now:
            raise
        log("首次分析遇到本机回环失败，按老师重试的做法再点一次：%s" % status_now[:40])
        review = analyse_again(app, answers_win)
    check("建议对话框打开", bool(review))
    status_text = V.text_of(review, "answerReviewStatus", "") or ""
    check("建议标为待核对", "还没有确认" in status_text or "待核对" in status_text, status_text)
    table = V.table_text(review, "answerCauseTable")
    check("给出错因与原始步骤证据", "去括号" in table and "3x-2=2x+5" in table, table[:160])
    body = V.text_of(review, "answerReviewText", "") or ""
    check("建议正文保留学生步骤", "3x-2x=5-6" in body, body[:120])
    V.shot(review, "review-exe-suggestion.png")

    log("")
    log("== 五、拒绝建议不生成错题 ==")
    V.click(review, "answerReviewReject")
    deadline = time.time() + 60
    while time.time() < deadline:
        status_text = V.text_of(review, "answerReviewStatus", "") or ""
        if "已拒绝" in status_text:
            break
        time.sleep(0.3)
    check("拒绝后状态为已拒绝", "已拒绝" in status_text, status_text)
    V.click(review, "answerReviewClose")
    time.sleep(1.0)
    conn = sqlite3.connect(os.path.join(str(V.DATA), "app.db"))
    total = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    states = [row[0] for row in conn.execute("SELECT status FROM answer_reviews ORDER BY id")]
    conn.close()
    check("拒绝后没有生成错题", total == 0, total)
    check("拒绝记为已拒绝", states == ["rejected"], states)

    log("")
    log("== 六、老师确认后生成一条正式错题 ==")
    click_row(answers_win, "EX1")
    time.sleep(0.4)
    revived = False
    deadline = time.time() + 20
    while time.time() < deadline:
        if V.ctl(answers_win, "answerAnalyse").is_enabled():
            revived = True
            break
        time.sleep(0.3)
    check("拒绝后可以重新分析", revived, V.ctl(answers_win, "answerAnalyse").is_enabled())
    review = analyse_again(app, answers_win)
    check("第二次建议打开", bool(review))
    V.click(review, "answerReviewConfirm")
    deadline = time.time() + 60
    while time.time() < deadline:
        status_text = V.text_of(review, "answerReviewStatus", "") or ""
        if "已确认" in status_text:
            break
        time.sleep(0.3)
    check("确认后状态为已确认", "已确认" in status_text, status_text)
    check("显示错题编号", "错题编号" in status_text, status_text)
    V.click(review, "answerReviewClose")
    time.sleep(1.0)

    conn = sqlite3.connect(os.path.join(str(V.DATA), "app.db"))
    mistakes = conn.execute(
        "SELECT id, status, possible_causes, observed_errors, advice_text FROM mistakes").fetchall()
    reviews = conn.execute(
        "SELECT status, mistake_id, note FROM answer_reviews ORDER BY id").fetchall()
    steps_after = dict((key, steps) for key, steps in conn.execute(
        "SELECT record_key, student_steps FROM answers"))
    conn.close()
    check("只生成一条正式错题", len(mistakes) == 1, len(mistakes))
    check("错题已确认", mistakes and mistakes[0][1] == "confirmed", mistakes[:1])
    check("错题保存建议里的错因", "去括号" in (mistakes[0][2] or ""), mistakes[0][2])
    check("两次建议分别记为已拒绝与已确认", [row[0] for row in reviews] == ["rejected", "confirmed"], reviews)
    check("审核记录指向该错题", reviews[-1][1] == mistakes[0][0], reviews[-1])
    check("原始作答没有被改写", steps_after["EX1"] == answers_before["EX1"][0], steps_after["EX1"])

    log("")
    log("== 七、关闭重开后状态仍在 ==")
    V.click(answers_win, "backToStudents")
    V.stop(proc)
    proc = V.spawn(["--data-dir", str(V.DATA)], "app2")
    app = V.attach(proc)
    classes = login(app)
    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    answers_win = open_answers(app, students)
    deadline = time.time() + 60
    texts = []
    while time.time() < deadline:
        texts = row_texts(answers_win)
        if any("已确认" in text for text in texts):
            break
        time.sleep(0.3)
    check("重开后仍显示已确认", any("已确认" in text for text in texts), texts)
    click_row(answers_win, "EX1")
    time.sleep(0.6)
    check("已确认的作答不能再分析", not V.ctl(answers_win, "answerAnalyse").is_enabled())
    click_row(answers_win, "EX2")
    time.sleep(0.4)
    check("缺测记录不能分析", not V.ctl(answers_win, "answerAnalyse").is_enabled())
    V.shot(answers_win, "review-exe-reopened.png")

    log("")
    log("== 八、请求内容与密钥 ==")
    log("桩日志：%s（存在=%s）" % (V.AI_STUB.get("log"),
                                os.path.isfile(str(V.AI_STUB.get("log") or ""))))
    records = stub_requests(V.AI_STUB["log"])
    log("桩记录条数：%d" % len(records))
    reviews_sent = [item for item in records if item.get("kind") == "review"]
    check("桩收到两次作答分析请求", len(reviews_sent) == 2, len(reviews_sent))
    bodies = "\n".join(item.get("text") or "" for item in reviews_sent)
    check("请求带作答分析标记", "【作答分析】" in bodies)
    for token in ("张三", "XY001", "初一 2 班", "exe_teacher"):
        check("请求不含 %s" % token, token not in bodies)
    raw = open(os.path.join(str(V.DATA), "app.db"), "rb").read()
    wal = raw
    for suffix in ("-wal",):
        path = os.path.join(str(V.DATA), "app.db" + suffix)
        if os.path.isfile(path):
            wal += open(path, "rb").read()
    check("数据库里没有 API 密钥", b"sk-exe-review-1234" not in wal)
    V.stop(proc)

    return finish()


def entry():
    global _tee
    _tee = Tee(LOG_PATH)
    try:
        return main()
    except Exception as exc:
        import traceback
        log(traceback.format_exc())
        log("异常：%s" % exc)
        return finish(force=1)


if __name__ == "__main__":
    sys.exit(entry())
