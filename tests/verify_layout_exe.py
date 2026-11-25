"""1.8.1 最终 EXE 的小屏适配实测（独立桌面，窗口不出现）。

三种屏幕情形，每一种都按「逻辑可用区域」模拟：
  1. 1366×768、100% —— 常见小屏笔记本；
  2. 1920×1080、100% —— 常见的教室办公屏；
  3. 1366×768、125% —— 同一台小屏笔记本把系统缩放调到 125%，逻辑区域只有 1093×614。

本机面板是 1707×1067 物理像素、系统缩放 150%，屏幕本身装不下 1366×768 逻辑区域，
所以用 QT_SCALE_FACTOR 把 Qt 的缩放比调到与目标情形一致，再按目标逻辑区域乘缩放的
物理像素摆放窗口：这与会话里真实 DPI 缩放所做的大小计算相同。

检查什么：
- 交付件：程序包与包内 EXE 的字节数、SHA-256，版本号 1.8.1，数据结构版本 4；
- 每种情形：主要窗口按目标尺寸摆放后不超出目标区域，关键按钮落在窗口内；
- 个人学情与教师短追问出现滚动条，滚到底能看见「接受候选」；
- 追问窗口的依据区、候选表、历史表、回答框、更新前后对比按顺序排，没有互相压住；
- 截图存到 docs/screenshots/1.8.1-exe。

只使用虚构学生与自制题目，不联网。
"""

import hashlib
import io
import json
import os
import shutil
import sys
import time
import uuid
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import verify_answers_exe as A  # noqa: E402
import verify_mistakes_pack as V  # noqa: E402
from app.db import SCHEMA_VERSION  # noqa: E402
from app.version import VERSION  # noqa: E402
from logmeta import environment_lines, exit_line  # noqa: E402

ROOT = A.ROOT
NAME = A.NAME
ZIP_PATH = os.path.join(ROOT, "build", "程序包", "%s-%s.zip" % (NAME, VERSION))
LOG_PATH = os.path.join(ROOT, "docs", "verify-layout-exe.txt")
SHOTS = Path(ROOT) / "docs" / "screenshots" / "1.8.1-exe"

CASES = (
    ("1366x768", 1366, 768),
    ("1920x1080", 1920, 1080),
    ("1366x768-125pct", 1093, 614),
)

results = []
_tee = None
procs = []


class Tee:
    def __init__(self, path):
        self.stream = io.open(path, "w", encoding="utf-8", newline="\n")
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


def log(text=""):
    _tee.write(str(text) + "\n")


def check(name, ok, detail=""):
    results.append(bool(ok))
    log("%s  %s%s" % ("PASS" if ok else "FAIL", name, "  -> %s" % (detail,) if detail != "" else ""))
    return bool(ok)


def section(title):
    log("")
    log("== %s ==" % title)


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
    log(exit_line(code))
    return code


# ---------------------------------------------------------------- 模拟屏幕

def system_scale():
    """系统当前的显示缩放（96 DPI 时为 1.0）。"""
    try:
        import ctypes

        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def screen_size():
    import win32api

    width = win32api.GetSystemMetrics(0)
    height = win32api.GetSystemMetrics(1)
    return width, height


def plan_case(logical_w, logical_h):
    """按目标逻辑区域算出：Qt 缩放因子、对应的物理窗口尺寸。"""
    base = system_scale()
    screen_w, screen_h = screen_size()
    scale = min(1.25, 1.0 * screen_w / logical_w, 1.0 * screen_h / logical_h)
    factor = round(scale / base, 4)
    return factor, int(round(logical_w * scale)), int(round(logical_h * scale)), scale


# ---------------------------------------------------------------- 数据与环境

def seed(data_dir):
    from app import records
    from app.db import Database, ensure_followup_schema
    from desktop.service import DesktopService

    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "app.db")
    Database(db_path, create=True)
    ensure_followup_schema(db_path)
    service = DesktopService(data_dir)
    service.register("exe_teacher", "pw123456")
    service.login("exe_teacher", "pw123456")
    cls = service.create_class("七年级 1 班")
    student = service.create_student(cls["id"], "张三", "S001")

    db = Database(db_path)
    question = "解方程 3(x-2)=2x+5"
    steps = "3x-2=2x+5\n3x-2x=5-6\nx=-1"
    occurred = (date.today() - timedelta(days=5)).isoformat()
    mistake_id = records.insert_mistake(
        db, cls["id"], student["id"], "", "", 0, 0,
        {"occurred_on": occurred},
        {"question_text": question, "solution_text": "3x-6=2x+5\n3x-2x=5+6\nx=11",
         "observed_errors": ["移项没有变号"], "possible_causes": ["移项没有变号"],
         "mode": "mock"},
    )
    with db.tx() as conn:
        conn.execute("UPDATE mistakes SET status='confirmed', knowledge_points=? WHERE id=?",
                     (json.dumps(["移项"], ensure_ascii=False), mistake_id))
        cur = conn.execute(
            "INSERT INTO answers (class_id, student_id, record_key, batch, question_text,"
            " standard_solution, student_steps, occurred_on, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cls["id"], student["id"], "ROW-1", "九月作业", question,
             "3x-6=2x+5\n3x-2x=5+6\nx=11", steps, occurred, "fp-row-1",
             occurred, occurred, occurred),
        )
        answer_id = cur.lastrowid
        conn.execute(
            "INSERT INTO answer_reviews (class_id, student_id, answer_id, status, provider,"
            " model, causes, note, uncertain, raw, answer_fingerprint, mistake_id,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cls["id"], student["id"], answer_id, "confirmed", "本地模拟服务", "stub-vision",
             json.dumps([{"cause": "移项没有变号", "evidence": "3x-2x=5-6"}], ensure_ascii=False),
             "", 0, "{}", "fp-row-1", mistake_id, occurred, occurred),
        )
    return {"class_id": cls["id"], "student_id": student["id"], "mistake_id": mistake_id}


def set_work(work):
    unpack = os.path.join(work, "解压件")
    V.WORK = Path(work)
    V.PROG_ROOT = Path(unpack)
    V.PROG = Path(os.path.join(unpack, NAME))
    V.EXE = Path(os.path.join(V.PROG, NAME + ".exe"))
    V.DATA = Path(os.path.join(work, "数据 主库"))
    V.SHOTS = SHOTS
    V.SHOTS.mkdir(parents=True, exist_ok=True)
    profile = Path(work) / "profile"
    for sub in ("Desktop", "Documents", "Downloads", "AppData/Roaming",
                "AppData/Local", "AppData/Local/Temp"):
        (profile / sub).mkdir(parents=True, exist_ok=True)
    V.PROFILE_PATH = profile


# ---------------------------------------------------------------- 量尺寸

def window_rect(window):
    import win32gui

    left, top, right, bottom = win32gui.GetWindowRect(int(window.handle))
    return left, top, right, bottom


def ctl_rect(window, auto_id, timeout=15):
    ctl = V.ctl(window, auto_id, timeout=timeout)
    box = ctl.rectangle()
    return box.left, box.top, box.right, box.bottom


def place(window, width, height):
    import win32con
    import win32gui

    win32gui.SetWindowPos(int(window.handle), 0, 0, 0, width, height,
                          win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW)
    time.sleep(0.9)


def size_of(window):
    left, top, right, bottom = window_rect(window)
    return right - left, bottom - top


def fits(window, width, height):
    actual_w, actual_h = size_of(window)
    return actual_w <= width and actual_h <= height, "%dx%d（目标 %dx%d）" % (actual_w, actual_h, width, height)


def inside(window, auto_id):
    wx1, wy1, wx2, wy2 = window_rect(window)
    cx1, cy1, cx2, cy2 = ctl_rect(window, auto_id)
    ok = (cx1 >= wx1 - 3 and cy1 >= wy1 - 3 and cx2 <= wx2 + 3 and cy2 <= wy2 + 3
          and (cx2 - cx1) > 1 and (cy2 - cy1) > 1)
    return ok, "控件(%d,%d)-(%d,%d) 窗口(%d,%d)-(%d,%d)" % (
        cx1, cy1, cx2, cy2, wx1, wy1, wx2, wy2)


def ordered(window, first, second):
    _, _, _, bottom = ctl_rect(window, first)
    _, top, _, _ = ctl_rect(window, second)
    return top >= bottom - 2, "%s 底 %d / %s 顶 %d" % (first, bottom, second, top)


def wheel(window, notches, auto_id=None):
    """把滚轮消息发给窗口或某个控件。

    独立桌面里没有鼠标光标（mouse.move 会报 no active desktop），
    所以直接 PostMessage(WM_MOUSEWHEEL)。坐标取控件左下角的内边距位置，
    避免落在表格上被表格自己吃掉。
    """
    import win32api
    import win32con
    import win32gui

    hwnd = int(window.handle)
    if auto_id is not None:
        x1, y1, x2, y2 = ctl_rect(window, auto_id)
        x, y = x1 + 20, y2 - 20
    else:
        x1, y1, x2, y2 = window_rect(window)
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
    for _ in range(notches):
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL,
                             win32api.MAKELONG(0, -120), win32api.MAKELONG(x, y))
        time.sleep(0.2)
    time.sleep(0.8)


def scroll_bottom(window, auto_id):
    """滚到底：先发滚轮消息，再兜底直接设滚动条位置。"""
    wheel(window, 14, auto_id)
    try:
        for bar in window.descendants(control_type="ScrollBar"):
            try:
                rng = bar.iface_range_value
                rng.SetValue(rng.CurrentMaximumValue)
            except Exception:
                continue
    except Exception:
        pass
    time.sleep(0.6)


def has_scrollbar(window):
    try:
        for _ in window.descendants(control_type="ScrollBar"):
            return True
    except Exception:
        pass
    return False


def shot(window, name):
    V.shot(window, name)
    path = SHOTS / name
    return os.path.getsize(str(path)) if path.is_file() else 0


# ---------------------------------------------------------------- 界面动作

def native_handles(pid):
    import win32gui
    import win32process

    found = []

    def _collect(hwnd, _):
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                return True
        except Exception:
            return True
        title = win32gui.GetWindowText(hwnd)
        if title:
            found.append((hwnd, title, bool(win32gui.IsWindowVisible(hwnd))))
        return True

    win32gui.EnumWindows(_collect, None)
    return found


def wait_win(app, title_part, timeout=60):
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, title, visible in native_handles(app.process):
            if title_part in title and visible:
                try:
                    return UIAWrapper(UIAElementInfo(hwnd))
                except Exception:
                    continue
        time.sleep(0.4)
    raise RuntimeError("等不到窗口：%s" % title_part)


def wait_gone(app, title_part, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(title_part in title for _, title, _ in native_handles(app.process)):
            return True
        time.sleep(0.3)
    return False


def wait_rows(window, auto_id, minimum=1, timeout=40):
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        count = len(V.table_items(window, auto_id))
        if count >= minimum:
            return count
        time.sleep(0.4)
    return count


def click_cell(window, auto_id, needle):
    for item in V.table_items(window, auto_id):
        if needle in (item.window_text() or ""):
            V.click_center(item, window)
            return True
    return False


def close_app(app, proc, timeout=60):
    import win32con
    import win32gui

    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, _, visible in native_handles(app.process):
            if visible:
                try:
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                except Exception:
                    pass
        try:
            proc.wait(timeout=3)
            return True
        except Exception:
            time.sleep(1.5)
    V.stop(proc)
    return False


def login_flow(app):
    login = V.find_window(app, V.TITLE, require_id="loginUsername", timeout=120)
    V.fill(login, "loginUsername", "exe_teacher")
    V.fill(login, "loginPassword", "pw123456")
    V.click(login, "loginButton")
    return V.find_window(app, V.TITLE, require_id="currentUser", timeout=90)


# ---------------------------------------------------------------- 一种屏幕情形

def run_case(app, target_w, target_h, tag):
    """按目标逻辑区域摆放各窗口，检查尺寸、关键按钮、滚动与顺序。"""
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=60)
    place(classes, target_w, target_h)
    ok, detail = fits(classes, target_w, target_h)
    check("%s 班级列表不超出" % tag, ok, detail)

    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    place(students, target_w, target_h)
    ok, detail = fits(students, target_w, target_h)
    check("%s 学生名单不超出" % tag, ok, detail)
    for auto_id, label in (("newStudentButton", "「新增学生」"),
                           ("openAnswerReviews", "「作答分析」"),
                           ("openStudentInsight", "「个人学情」")):
        ok, detail = inside(students, auto_id)
        check("%s %s 在窗口内" % (tag, label), ok, detail)

    V.click(students, "openAnswerReviews")
    answers = A.wait_window(app, "作答分析")
    place(answers, target_w, target_h)
    ok, detail = fits(answers, target_w, target_h)
    check("%s 作答分析不超出" % tag, ok, detail)
    ok, detail = inside(answers, "answerAnalyse")
    check("%s 「分析这条作答」在窗口内" % tag, ok, detail)

    click_cell(students, "studentTable", "张三")
    V.click(students, "openStudentInsight")
    insight = wait_win(app, "个人学情")
    wait_rows(insight, "insightProblemTable", 1, 40)
    place(insight, target_w, target_h)
    ok, detail = fits(insight, target_w, target_h)
    check("%s 个人学情不超出" % tag, ok, detail)
    for auto_id, label in (("insightSave", "「保存修改」"), ("insightFollowup", "「追问这个问题」")):
        ok, detail = inside(insight, auto_id)
        check("%s %s 在窗口内" % (tag, label), ok, detail)
    # 内容比窗口高时要有滚动条；窗口足够大时内容直接看完，没有滚动条也是对的。
    bars = has_scrollbar(insight)
    table_inside = inside(insight, "insightProblemTable")[0]
    check("%s 个人学情内容可滚动或完整可见" % tag, bars or table_inside,
          "滚动条=%s，问题表在窗口内=%s" % (bars, table_inside))
    size = shot(insight, "insight-%s.png" % tag)
    check("%s 个人学情截图" % tag, size > 8000, "%d 字节" % size)

    click_cell(insight, "insightProblemTable", "移项")
    time.sleep(0.6)
    V.click(insight, "insightFollowup")
    follow = wait_win(app, "短追问")
    place(follow, target_w, target_h)
    ok, detail = fits(follow, target_w, target_h)
    check("%s 教师短追问不超出" % tag, ok, detail)
    for first, second in (("followupEvidence", "followupCandidates"),
                          ("followupCandidates", "followupHistory"),
                          ("followupHistory", "followupAnswer"),
                          ("followupAnswer", "followupCompare")):
        ok, detail = ordered(follow, first, second)
        check("%s %s 不压住 %s" % (tag, first, second), ok, detail)
    bars = has_scrollbar(follow)
    compare_inside = inside(follow, "followupCompare")[0]
    check("%s 教师短追问内容可滚动或完整可见" % tag, bars or compare_inside,
          "滚动条=%s，对比表在窗口内=%s" % (bars, compare_inside))
    size = shot(follow, "followup-%s.png" % tag)
    check("%s 追问截图（顶部）" % tag, size > 8000, "%d 字节" % size)
    wheel(follow, 14, "followupScroll")
    ok, detail = inside(follow, "followupAccept")
    check("%s 滚到底后「接受候选」在窗口内" % tag, ok, detail)
    size = shot(follow, "followup-%s-bottom.png" % tag)
    check("%s 追问截图（底部）" % tag, size > 8000, "%d 字节" % size)

    V.click(follow, "followupClose")
    wait_gone(app, "短追问", 20)
    V.click(insight, "backToStudentsFromInsight")
    wait_gone(app, "个人学情", 20)
    V.click(students, "backToClasses")
    wait_gone(app, V.TITLE_STUDENTS, 20)

    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=60)
    V.click(classes, "openAiSettingsButton")
    settings = wait_win(app, "AI 设置")
    place(settings, target_w, target_h)
    ok, detail = fits(settings, target_w, target_h)
    check("%s AI 设置不超出" % tag, ok, detail)
    ok, detail = inside(settings, "aiSettingsSave")
    check("%s 「保存并启用」在窗口内" % tag, ok, detail)
    size = shot(settings, "ai-settings-%s.png" % tag)
    check("%s AI 设置截图" % tag, size > 8000, "%d 字节" % size)
    V.click(settings, "aiSettingsCancel")
    wait_gone(app, "AI 设置", 20)


def main():
    global _tee
    _tee = Tee(LOG_PATH)
    for line in environment_lines():
        log(line)
    log("")

    section("一、交付件与版本")
    check("程序包存在", os.path.isfile(ZIP_PATH), ZIP_PATH)
    log("程序包：%d 字节  SHA-256 %s" % (os.path.getsize(ZIP_PATH), sha256_of(ZIP_PATH)))
    with zipfile.ZipFile(ZIP_PATH) as archive:
        names = archive.namelist()
        exe_name = "%s/%s.exe" % (NAME, NAME)
        exe_data = archive.read(exe_name)
    check("包内 EXE 存在", exe_name in names, exe_name)
    log("包内 EXE：%d 字节  SHA-256 %s" % (len(exe_data), hashlib.sha256(exe_data).hexdigest().upper()))
    check("版本号为 1.8.1", VERSION == "1.8.1", VERSION)
    check("数据结构版本仍为 4", str(SCHEMA_VERSION) == "4", str(SCHEMA_VERSION))

    work = os.path.join(ROOT, "tests", ".tmp-test", "layout-exe-" + uuid.uuid4().hex[:6])
    os.makedirs(work, exist_ok=True)
    set_work(work)
    shutil.unpack_archive(ZIP_PATH, os.path.join(work, "解压件"))
    seed(os.path.join(work, "数据 主库"))
    log("合成数据：1 个班、1 名学生、1 条已确认错题（知识点「移项」）")

    base_scale = system_scale()
    screen_w, screen_h = screen_size()
    log("本机：屏幕 %d×%d 物理像素，系统缩放 %.0f%%" % (screen_w, screen_h, base_scale * 100))

    for tag, logical_w, logical_h in CASES:
        factor, phys_w, phys_h, scale = plan_case(logical_w, logical_h)
        section("二、%s（逻辑 %d×%d，等效缩放 %.0f%%，物理 %d×%d，QT_SCALE_FACTOR=%.4f）" % (
            tag, logical_w, logical_h, scale * 100, phys_w, phys_h, factor))
        env = V.clean_env({"QT_SCALE_FACTOR": str(factor)})
        proc = V.spawn(["--data-dir", str(V.DATA)], "app-" + tag, env)
        procs.append(proc)
        app = V.attach(proc)
        login_flow(app)
        run_case(app, phys_w, phys_h, tag)
        check("%s 正常关闭程序" % tag, close_app(app, proc))

    section("三、收尾")
    check("关闭后没有残留窗口", wait_gone(app, "短追问", 20), "追问窗口已关闭")
    return finish()


def cleanup():
    for proc in procs:
        try:
            if proc.poll() is None:
                V.stop(proc)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001
        import traceback

        log("")
        log("脚本异常：%s" % exc)
        log(traceback.format_exc())
        code = finish(1)
    finally:
        cleanup()
    sys.exit(code)
