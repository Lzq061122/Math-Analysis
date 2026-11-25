"""1.8.0 最终 EXE 的教师短追问实测（独立桌面，窗口不出现）。

流程：交付件校验 → 造合成数据（一个班、一名学生、一条已确认错题与它对应的作答）
→ 启动 EXE、登录、把 AI 服务指向本机桩 → 从个人学情进入追问 → 生成候选、
确认发送 → 用候选记下追问 → 保存回答 → 更新错因候选 → 接受 → 再做一条并拒绝
→ 关掉重开后记录仍在 → 已确认错题入口 → 取消不丢数据 → 五种窗口两种尺寸截图。

只使用虚构学生与自制题目，只连本机桩，不访问真实云端。
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
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
from app.version import VERSION  # noqa: E402

ROOT = A.ROOT
NAME = A.NAME
ZIP = os.path.join(ROOT, "build", "程序包", "%s-%s.zip" % (NAME, VERSION))
LOG_PATH = os.path.join(ROOT, "docs", "verify-followup-exe.txt")
SHOTS = Path(ROOT) / "docs" / "screenshots" / "1.8.0-exe"

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
    V.SHOTS = SHOTS
    V.SHOTS.mkdir(parents=True, exist_ok=True)
    profile = Path(work) / "profile"
    for sub in ("Desktop", "Documents", "Downloads", "AppData/Roaming",
                "AppData/Local", "AppData/Local/Temp"):
        (profile / sub).mkdir(parents=True, exist_ok=True)
    V.PROFILE_PATH = profile
    return prog


# ---------------------------------------------------------------- 合成数据

def seed(data_dir):
    """一个班、一名学生、一条已确认错题与它对应的作答。"""
    from app.db import Database, ensure_followup_schema
    from app import records
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
    steps = "3x-2=2x+5\n3x-2x=5-6\nx=-1"
    question = "解方程 3(x-2)=2x+5"
    mistake_id = records.insert_mistake(
        db, cls["id"], student["id"], "", "", 0, 0,
        {"occurred_on": (date.today() - timedelta(days=5)).isoformat()},
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
             "3x-6=2x+5\n3x-2x=5+6\nx=11", steps,
             (date.today() - timedelta(days=5)).isoformat(), "fp-row-1",
             date.today().isoformat(), date.today().isoformat(), date.today().isoformat()),
        )
        answer_id = cur.lastrowid
        conn.execute(
            "INSERT INTO answer_reviews (class_id, student_id, answer_id, status, provider,"
            " model, causes, note, uncertain, raw, answer_fingerprint, mistake_id,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cls["id"], student["id"], answer_id, "confirmed", "本地模拟服务",
             "stub-vision",
             json.dumps([{"cause": "移项没有变号", "evidence": "3x-2x=5-6",
                          "reason": "把 -6 移到右边没有变号"}], ensure_ascii=False),
             "", 0, "{}", "fp-row-1", mistake_id,
             date.today().isoformat(), date.today().isoformat()),
        )
    return {"class_id": cls["id"], "student_id": student["id"], "mistake_id": mistake_id}


def rows(sql, data_dir):
    conn = sqlite3.connect(os.path.join(str(V.DATA), "app.db"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def causes_of(mistake_id, data_dir):
    found = rows("SELECT possible_causes FROM mistakes WHERE id=%d" % mistake_id, data_dir)
    if not found:
        return []
    return json.loads(found[0][0] or "[]")


# ---------------------------------------------------------------- UI 工具

def wait_for_enabled(window, auto_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if V.ctl(window, auto_id, timeout=0).is_enabled():
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def resize(window, width, height):
    import win32con
    import win32gui

    hwnd = int(window.handle)
    win32gui.SetWindowPos(hwnd, 0, 0, 0, width, height,
                          win32con.SWP_NOMOVE | win32con.SWP_NOZORDER)
    time.sleep(0.6)


def shoot(window, name, width=None, height=None):
    if width and height:
        resize(window, width, height)
    V.shot(window, name)
    path = SHOTS / name
    return os.path.getsize(str(path)) if path.is_file() else 0


def wait_text(window, auto_id, needle, timeout=30):
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        text = V.value_of(window, auto_id, "") or ""
        if needle in text:
            return text
        time.sleep(0.3)
    return text


def wait_table_rows(window, auto_id, minimum=1, timeout=40):
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        count = len(V.table_items(window, auto_id))
        if count >= minimum:
            return count
        time.sleep(0.4)
    return count


def wait_rows(sql, data_dir, count=1, timeout=40):
    deadline = time.time() + timeout
    got = 0
    while time.time() < deadline:
        try:
            got = len(rows(sql, data_dir))
        except Exception:
            got = 0
        if got >= count:
            return got
        time.sleep(0.4)
    return got


def click_cell(window, auto_id, needle):
    for item in V.table_items(window, auto_id):
        if needle in (item.window_text() or ""):
            V.click_center(item, window)
            return True
    return False


def click_first(window, auto_id):
    items = V.table_items(window, auto_id)
    if not items:
        return False
    V.click_center(items[0], window)
    return True


def consent_of(app, timeout=30):
    """T30 改写②：等不到确认框时返回 None（交给调用处写成显式空态断言），不再抛 RuntimeError。

    原断言：`return wait_win(app, "确认把问题与证据发给", timeout=timeout)` —— 等不到就抛异常。
    原依据：1.8.1「真实模式点生成必定弹确认框」，所以调用处可以假定拿到窗口对象。
    新断言：内部捕获 RuntimeError 后返回 None；调用处必须先断言非 None，再操作控件。
    改写依据：R1-14 下账号开关关闭时只会显示 403 文案、不弹确认框，这是可预期空态；
              让异常替我们报错会把「断言失败」变成「崩溃」，也拿不到失败标题。
    """
    try:
        return wait_win(app, "确认把问题与证据发给", timeout=timeout)
    except RuntimeError:
        return None


def native_handles(pid):
    """按进程号列出全部顶层窗口：[(句柄, 标题, 是否可见)]，含 UIA 不报告的所属窗口。"""
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
    """等一个窗口；个人学情、追问这些窗口是主窗口的所属窗口，UIA 顶层枚举有时看不到。"""
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


def wait_no_win(app, title_part, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(title_part in title for _, title, _ in native_handles(app.process)):
            return True
        time.sleep(0.4)
    return False


def suggest_with_retry(app, follow, data_dir, attempts=3):
    """点生成 → 确认 → 等候选；本机回环偶发连不上时按老师重试的做法再点一次。"""
    for attempt in range(1, attempts + 1):
        V.click(follow, "followupSuggest")
        consent = consent_of(app, timeout=30)
        if not consent:
            log("第 %d 次没有弹出确认框" % attempt)
            time.sleep(1.0)
            continue
        V.click(consent, "followupConsentCheck")
        time.sleep(0.3)
        V.click(consent, "followupConsentOk")
        cells = wait_table_rows(follow, "followupCandidates", 2, 60)
        if cells >= 2:
            return attempt, cells
        status = V.text_of(follow, "followupStatus", "") or ""
        log("第 %d 次生成候选没成功：%s" % (attempt, status[:60]))
        if "无法连接" not in status or attempt == attempts:
            return attempt, cells
        time.sleep(1.0)
    return attempts, 0


def update_with_retry(app, follow, data_dir, attempts=3):
    """点更新 → 确认 → 等对比表；同样只对「无法连接」做有界重试。"""
    for attempt in range(1, attempts + 1):
        wait_for_enabled(follow, "followupUpdate", 20)
        V.click(follow, "followupUpdate")
        consent = consent_of(app, timeout=30)
        if not consent:
            log("第 %d 次更新没有弹出确认框" % attempt)
            time.sleep(1.0)
            continue
        text = V.value_of(consent, "followupConsentText", "") or ""
        V.click(consent, "followupConsentCheck")
        time.sleep(0.3)
        V.click(consent, "followupConsentOk")
        cells = wait_table_rows(follow, "followupCompare", 2, 60)
        stored = wait_rows("SELECT id FROM followup_updates", data_dir, 1, 15)
        if cells >= 2 and stored >= 1:
            return attempt, cells, text
        status = V.text_of(follow, "followupStatus", "") or ""
        log("第 %d 次更新候选没成功：%s" % (attempt, status[:60]))
        if "无法连接" not in status or attempt == attempts:
            return attempt, cells, text
        time.sleep(1.0)
    return attempts, 0, ""


def close_app(app, proc, timeout=60):
    """把每个窗口都关掉再等进程退出：只关主窗口时，其它已打开的窗口会留住进程。"""
    import win32con
    import win32gui

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        handles = [hwnd for hwnd, _, visible in native_handles(app.process) if visible]
        if not handles:
            time.sleep(0.5)
            continue
        for hwnd in handles:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                continue
        time.sleep(1.2)
    if proc.poll() is None:
        V.stop(proc)
        return False
    return True


def configure_stub(classes, stub_base):
    """打开 AI 设置，把分析方式换成自定义服务并填上本机桩。"""
    import ctypes

    V.click(classes, "openAiSettingsButton")
    dialog = V.wait_child(classes, "aiSettingsDialog", timeout=60)
    combo_text = ""
    try:
        combo = V.scan(dialog, "aiModeCombo")
        combo.set_focus()
        time.sleep(0.3)
        for vk, scan_key in ((0x24, 0x47), (0x28, 0x50), (0x28, 0x50), (0x28, 0x50)):
            lparam = 1 | (scan_key << 16)
            ctypes.windll.user32.SendMessageW(dialog.handle, 0x0100, vk, lparam)
            time.sleep(0.05)
            ctypes.windll.user32.SendMessageW(dialog.handle, 0x0101, vk,
                                              lparam | 0xC0000000)
            time.sleep(0.2)
        deadline = time.time() + 15
        while time.time() < deadline:
            combo_text = combo.iface_value.CurrentValue or ""
            if "自定义" in combo_text:
                break
            time.sleep(0.3)
    except Exception as exc:  # noqa: BLE001
        combo_text = "切换失败：%s" % exc
    V.fill(dialog, "aiCustomNameEdit", "本地模拟服务")
    V.fill_ascii(dialog, "aiCustomBaseEdit", stub_base)
    V.fill_ascii(dialog, "aiCustomModelEdit", "stub-vision")
    V.fill_ascii(dialog, "aiKeyEdit", "sk-exe-followup-1234")
    V.click(dialog, "aiSettingsSave")
    V.wait_child_gone(classes, "aiSettingsDialog", timeout=30)
    return combo_text


def main():
    global _tee
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _tee = Tee(LOG_PATH)
    SHOTS.mkdir(parents=True, exist_ok=True)

    work = os.path.join(ROOT, "tests", ".tmp-test", "followup-exe-" + uuid.uuid4().hex[:6])
    os.makedirs(work)
    set_work(work)
    data_dir = str(V.DATA)

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
    check("包内有 EXE", any(n.endswith(NAME + ".exe") for n in names))
    check("EXE 已解出", os.path.isfile(V.EXE), V.EXE)
    log("EXE %d 字节 SHA-256 %s" % (os.path.getsize(V.EXE), sha256_of(V.EXE)))

    log("")
    log("== 二、造合成数据（一个班、一名学生、一条已确认错题） ==")
    ids = seed(data_dir)
    log("班级 id=%s 学生 id=%s 错题 id=%s" % (ids["class_id"], ids["student_id"],
                                              ids["mistake_id"]))
    check("已确认错题一条",
          len(rows("SELECT id FROM mistakes WHERE status='confirmed'", data_dir)) == 1)
    check("作答与审核记录已关联错题",
          len(rows("SELECT id FROM answer_reviews WHERE mistake_id=%d" % ids["mistake_id"],
                   data_dir)) == 1)

    log("")
    log("== 二b、在打包版自己的数据目录里开启「个体数据外发」开关 ==")
    # T30 改写①（本节前提，与 t26 源码级口径一致）
    # 原断言：配好自定义通道后，点「生成候选」→ 弹确认框 → 勾选 → 出候选（1.8.1 口径）。
    # 原依据：1.8.1「真实模式 = 老师点确认就发送」（数智伴学_Agent开发规格.md 1.8.1 章）。
    # 新断言：先按 R1-14 在**EXE 自己那个库**里把账号开关置为开启并断言生效，后续断言不变。
    # 改写依据：R1-07 + R1-14 裁定「默认不外发；教师账号显式开启后才真调用」——
    #           旧口径等于把「默认外发」写成预期，改实现让旧断言变绿就等于把缺陷重新打开。
    from desktop.service import DesktopService

    switch_service = DesktopService(data_dir)
    switch_service.login("exe_teacher", "pw123456")
    switch = switch_service.set_ai_individual(True)
    check("二b.1 EXE 库里的账号开关已开启（否则六入口一律 403 ai_isolation）",
          switch.get("ai_individual_allowed") is True, switch)
    check("二b.2 打包版注入了 provider，具备真实外发能力",
          switch.get("ai_egress_available") is True, switch)

    port = V.start_ai_stub()
    stub_base = "http://127.0.0.1:%d/v1" % port
    log("本地桩：%s" % stub_base)

    log("")
    log("== 三、启动、登录、把 AI 服务指向本机桩 ==")
    env = V.clean_env({"MISTAKE_SHOT_DIR": str(V.SHOTS)})
    proc = V.spawn(["--data-dir", str(V.DATA)], "app", env)
    procs.append(proc)
    app = V.attach(proc)
    login_win = V.find_window(app, V.TITLE, require_id="loginUsername", timeout=120)
    V.fill(login_win, "loginUsername", "exe_teacher")
    V.fill(login_win, "loginPassword", "pw123456")
    V.click(login_win, "loginButton")
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=90)
    check("登录后进入班级列表", bool(classes))
    combo_text = configure_stub(classes, stub_base)
    check("分析方式切到自定义服务", "自定义" in combo_text, combo_text)

    V.click(classes, "openAiSettingsButton")
    settings = V.wait_child(classes, "aiSettingsDialog", timeout=60)
    note = V.text_of(settings, "aiSettingsStatus", "") or ""
    size1 = shoot(settings, "exe-ai-settings-1366x768.png", 1366, 768)
    check("AI 设置截图 1366x768", size1 > 10000, "%d 字节" % size1)
    size2 = shoot(settings, "exe-ai-settings-1920x1080.png", 1920, 1080)
    check("AI 设置截图 1920x1080", size2 > 10000, "%d 字节" % size2)
    check("保存后提示尚未验证", "尚未验证" in note, note[:100])
    V.click(settings, "aiSettingsCancel")
    V.wait_child_gone(classes, "aiSettingsDialog", timeout=30)

    log("")
    log("== 四、学生名单与个人学情入口 ==")
    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    check("学生名单显示一名学生",
          "共 1 人" in (V.text_of(students, "studentStatus", "") or ""),
          V.text_of(students, "studentStatus", ""))
    size3 = shoot(students, "exe-students-1366x768.png", 1366, 768)
    check("学生名单截图 1366x768", size3 > 10000, "%d 字节" % size3)
    check("点选学生", click_cell(students, "studentTable", "张三"))
    check("「个人学情」按钮可用", wait_for_enabled(students, "openStudentInsight", 20))
    V.click(students, "openStudentInsight")
    insight = wait_win(app, "个人学情")
    check("个人学情窗口打开", bool(insight))
    heading = V.text_of(insight, "insightStudentName", "") or ""
    check("页面写明当前学生", "张三" in heading, heading[:60])
    count = wait_table_rows(insight, "insightProblemTable", 1, 30)
    check("问题表里有已确认问题", count >= 1, "%d 个单元格" % count)
    size4 = shoot(insight, "exe-insight-1366x768.png", 1366, 768)
    check("个人学情截图 1366x768", size4 > 10000, "%d 字节" % size4)

    log("")
    log("== 五、从个人学情进入短追问 ==")
    check("问题行可选", click_cell(insight, "insightProblemTable", "移项"))
    check("「追问这个问题」可用", wait_for_enabled(insight, "insightFollowup", 20))
    V.click(insight, "insightFollowup")
    follow = wait_win(app, "短追问")
    check("追问窗口打开", bool(follow))
    head = V.text_of(follow, "followupStudentName", "") or ""
    check("追问窗口写明班级与学生", "七年级 1 班" in head and "张三" in head, head[:60])
    ident = V.text_of(follow, "followupProblemName", "") or ""
    check("写明当前问题", "移项" in ident, ident[:60])
    evidence = V.value_of(follow, "followupEvidence", "") or ""
    check("依据里有题目原文", "3(x-2)=2x+5" in evidence, evidence[:60])
    check("依据里有学生步骤原文", "3x-2x=5-6" in evidence, evidence[:60])
    check("依据里有已确认错因", "移项没有变号" in evidence, evidence[:60])
    hint = V.text_of(follow, "followupHint", "") or ""
    check("窗口说明不发送身份信息", "不发送姓名" in hint, hint[:80])
    size5 = shoot(follow, "exe-followup-1366x768.png", 1366, 768)
    check("追问窗口截图 1366x768", size5 > 10000, "%d 字节" % size5)

    log("")
    log("== 六、生成候选：先确认发送范围 ==")
    check("生成按钮可用", wait_for_enabled(follow, "followupSuggest", 20))
    V.click(follow, "followupSuggest")
    consent = consent_of(app)
    check("真实模式先弹确认框", bool(consent))
    # T30 改写②（把「等不到窗口」从崩溃改成空态断言）
    # 原断言：consent_of(app) 直接返回窗口对象；等不到就抛 RuntimeError 把整套件打断
    #         （t29 实测崩在：RuntimeError: 等不到窗口：确认把问题与证据发给）。
    # 原依据：1.8.1「真实模式点生成必定弹确认框」。
    # 新断言：先断言「确认框应当出现」；若等不到，显式记一条失败并说明原因，
    #         然后停止后续操作（不再对 None 取控件 → 不再 AttributeError/超时崩）。
    # 改写依据：R1-14 下账号开关关闭时界面只给 403 文案、不弹确认框，这属于可预期的空态，
    #           必须写成断言而不是让异常替我们报错。
    if consent is None:
        check("六.0 等不到确认框时的空态说明（不再抛异常打断整套件）", False,
              "等不到「确认把问题与证据发给」窗口；若账号开关未开启，界面只会显示 403 文案")
        return finish()
    text = V.value_of(consent, "followupConsentText", "") or ""
    check("确认框列出会发送的内容", "本次会发送" in text and "题目原文" in text, text[:80])
    check("确认框说明不发送身份信息", "不会发送学生姓名" in text)
    check("未勾选时不能生成", not V.ctl(consent, "followupConsentOk").is_enabled())
    V.click(consent, "followupConsentCheck")
    time.sleep(0.3)
    check("勾选后可以生成", V.ctl(consent, "followupConsentOk").is_enabled())
    before_count = len(rows("SELECT id FROM followups", data_dir))
    V.click(consent, "followupConsentOk")
    table_cells = wait_table_rows(follow, "followupCandidates", 2, 60)
    if table_cells < 2:
        status = V.text_of(follow, "followupStatus", "") or ""
        if "无法连接" in status:
            log("首次生成遇到本机回环失败，按老师重试的做法再点一次：%s" % status[:40])
            attempt, table_cells = suggest_with_retry(app, follow, data_dir, attempts=2)
    check("候选表出现候选", table_cells >= 2, "%d 个单元格" % table_cells)
    table_text = V.table_text(follow, "followupCandidates")
    check("候选带依据原文", "3x-2=2x+5" in table_text, table_text[:120])
    check("生成候选还没有记成提问",
          len(rows("SELECT id FROM followups", data_dir)) == before_count,
          "%d 条" % before_count)
    size6 = shoot(follow, "exe-followup-candidates-1366x768.png", 1366, 768)
    check("候选截图已保存", size6 > 10000, "%d 字节" % size6)

    log("")
    log("== 七、选一条候选、记下这次追问 ==")
    check("点选候选行", click_first(follow, "followupCandidates"))
    time.sleep(0.5)
    question_text = V.value_of(follow, "followupQuestion", "") or ""
    check("选中候选后填入输入框", bool(question_text.strip()), question_text[:40])
    V.click(follow, "followupUse")
    status = wait_text(follow, "followupStatus", "已记录", 20)
    if "已记录" not in status:
        status = wait_text(follow, "followupStatus", "共 1 条", 30)
    check("记下追问后给出提示", "已记录" in status or "共 1 条" in status, status[:80])
    follow_rows = rows("SELECT id, question, source, status, asked_at, answered_at,"
                       " mistake_id FROM followups", data_dir)
    check("数据库里有一条追问", len(follow_rows) == 1, "%d 条" % len(follow_rows))
    if follow_rows:
        rid, qtext, source, fstatus, asked, answered, mid = follow_rows[0]
        log("追问保存值：id=%s source=%s status=%s asked_at=%s mistake_id=%s"
            % (rid, source, fstatus, asked, mid))
        check("来源记为 AI 候选", source == "ai", source)
        check("状态为已提问", fstatus == "asked", fstatus)
        check("记下提问时间", bool(asked), asked)
        check("关联到这条错题", mid == ids["mistake_id"], str(mid))
    history = V.table_text(follow, "followupHistory")
    check("历史表出现这条追问", "已提问" in history, history[:100])

    log("")
    log("== 八、保存学生回答 ==")
    V.fill(follow, "followupAnswer", "我忘了移项要变号")
    V.click(follow, "followupSaveAnswer")
    status = wait_text(follow, "followupStatus", "已保存", 40)
    check("保存回答后给出提示", "已保存" in status, status[:80])
    answer_row = rows("SELECT answer, status, answered_at FROM followups", data_dir)
    check("回答写入数据库", bool(answer_row) and answer_row[0][0] == "我忘了移项要变号",
          str(answer_row[:1]))
    check("状态改为已记录回答", bool(answer_row) and answer_row[0][1] == "answered",
          str(answer_row[:1]))
    check("记下回答时间", bool(answer_row and answer_row[0][2]), str(answer_row[:1]))
    check("历史里显示已记录回答", "已记录回答" in V.table_text(follow, "followupHistory"),
          V.table_text(follow, "followupHistory")[:80])

    log("")
    log("== 九、按回答更新错因候选（不直接改错题） ==")
    causes_before = causes_of(ids["mistake_id"], data_dir)
    check("更新按钮可用", wait_for_enabled(follow, "followupUpdate", 20))
    V.click(follow, "followupUpdate")
    consent = consent_of(app)
    check("更新候选也先确认", bool(consent))
    # T30 改写②：与第六节同一处理——等不到确认框时给明确失败，不再对 None 取控件。
    if consent is None:
        check("九.0 等不到「更新确认框」时的空态说明（不再抛异常）", False,
              "等不到「确认把问题与证据发给」窗口；账号开关关闭时界面只给 403 文案")
        return finish()
    ctext = ""
    if consent:
        ctext = V.value_of(consent, "followupConsentText", "") or ""
        check("确认框说明这次会发送回答", "学生的回答" in ctext, ctext[:100])
        V.click(consent, "followupConsentCheck")
        time.sleep(0.3)
        V.click(consent, "followupConsentOk")
    compare_cells = wait_table_rows(follow, "followupCompare", 2, 60)
    if compare_cells < 2:
        status = V.text_of(follow, "followupStatus", "") or ""
        if "无法连接" in status:
            log("首次更新遇到本机回环失败，按老师重试的做法再点一次：%s" % status[:40])
            attempt, compare_cells, ctext = update_with_retry(app, follow, data_dir)
    check("出现更新前后两列", compare_cells >= 2, "%d 个单元格" % compare_cells)
    compare_text = V.table_text(follow, "followupCompare")
    check("对比里有更新前已确认错因", "移项没有变号" in compare_text, compare_text[:120])
    check("对比里有候选错因", "变号" in compare_text, compare_text[:120])
    updates = rows("SELECT status, causes FROM followup_updates", data_dir)
    check("候选存为待处理", bool(updates) and updates[0][0] == "pending", str(updates[:1]))
    check("候选还没写进错题", causes_of(ids["mistake_id"], data_dir) == causes_before,
          str(causes_before))
    check("接受按钮已可用", V.ctl(follow, "followupAccept").is_enabled())
    check("拒绝按钮已可用", V.ctl(follow, "followupReject").is_enabled())

    log("")
    log("== 十、接受候选 ==")
    V.click(follow, "followupAccept")
    status = wait_text(follow, "followupStatus", "接受", 40)
    check("接受后给出提示", "接受" in status, status[:80])
    statuses = [r[0] for r in rows("SELECT status FROM followup_updates", data_dir)]
    check("候选状态改为已接受", statuses == ["accepted"], str(statuses))
    merged = causes_of(ids["mistake_id"], data_dir)
    check("候选并入错题错因", len(merged) >= 1 and any("变号" in str(c) for c in merged),
          str(merged))
    check("原已确认错因保留", "移项没有变号" in merged, str(merged))
    check("错题仍是一条",
          len(rows("SELECT id FROM mistakes WHERE status='confirmed'", data_dir)) == 1)

    log("")
    log("== 十一、再做一条追问并拒绝候选 ==")
    V.click(follow, "followupClose")
    time.sleep(0.6)
    check("回到个人学情还能再开追问", wait_for_enabled(insight, "insightFollowup", 20))
    V.click(insight, "insightFollowup")
    follow = wait_win(app, "短追问")
    V.click(follow, "followupQuestion")
    V.fill(follow, "followupQuestion", "那这一步你是怎么想的？")
    check("手写追问可以记下", wait_for_enabled(follow, "followupUse", 20))
    V.click(follow, "followupUse")
    check("第二条追问已入库",
          wait_rows("SELECT id FROM followups", data_dir, 2, 40) >= 2)
    V.fill(follow, "followupAnswer", "我把减号当成加号了")
    V.click(follow, "followupSaveAnswer")
    wait_text(follow, "followupStatus", "已保存", 40)
    check("第二条追问可以更新", wait_for_enabled(follow, "followupUpdate", 20))
    attempt, compare_cells, _ = update_with_retry(app, follow, data_dir)
    check("第二条追问也生成了候选", compare_cells >= 2, "%d 个单元格" % compare_cells)
    causes_before_reject = causes_of(ids["mistake_id"], data_dir)
    V.click(follow, "followupReject")
    status = wait_text(follow, "followupStatus", "拒绝", 40)
    check("拒绝后给出提示", "拒绝" in status, status[:80])
    statuses = [r[0] for r in rows("SELECT status FROM followup_updates ORDER BY id",
                                   data_dir)]
    check("第二条候选记为已拒绝", statuses.count("rejected") == 1, str(statuses))
    check("拒绝不改错题错因", causes_of(ids["mistake_id"], data_dir) == causes_before_reject,
          str(causes_before_reject))
    check("两条追问都在", len(rows("SELECT id FROM followups", data_dir)) == 2,
          str(len(rows("SELECT id FROM followups", data_dir))))

    log("")
    log("== 十二、取消发送不丢数据 ==")
    before_followups = len(rows("SELECT id FROM followups", data_dir))
    V.click(follow, "followupSuggest")
    consent = consent_of(app)
    check("再点生成仍会先确认", bool(consent))
    if consent:
        V.click_button_text(consent, "取消", timeout=20)
        time.sleep(0.8)
    status = V.text_of(follow, "followupStatus", "") or ""
    check("取消发送有提示", "已取消" in status or "没有发送" in status, status[:80])
    check("取消不新增记录",
          len(rows("SELECT id FROM followups", data_dir)) == before_followups)
    size7 = shoot(follow, "exe-followup-1920x1080.png", 1920, 1080)
    check("追问窗口截图 1920x1080", size7 > 10000, "%d 字节" % size7)

    log("")
    log("== 十三、关掉重开后记录仍在 ==")
    check("正常关闭程序", close_app(app, proc))
    proc = V.spawn(["--data-dir", str(V.DATA)], "app-reopen", env)
    procs.append(proc)
    app = V.attach(proc)
    login_win = V.find_window(app, V.TITLE, require_id="loginUsername", timeout=120)
    V.fill(login_win, "loginUsername", "exe_teacher")
    V.fill(login_win, "loginPassword", "pw123456")
    V.click(login_win, "loginButton")
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=90)
    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    click_cell(students, "studentTable", "张三")
    wait_for_enabled(students, "openStudentInsight", 20)
    V.click(students, "openStudentInsight")
    insight = wait_win(app, "个人学情")
    wait_table_rows(insight, "insightProblemTable", 1, 30)
    open_ok = wait_for_enabled(insight, "insightFollowup", 20)
    click_cell(insight, "insightProblemTable", "移项")
    V.click(insight, "insightFollowup")
    follow = wait_win(app, "短追问")
    history = V.table_text(follow, "followupHistory")
    check("重开后历史里有两条追问与回答", "已记录回答" in history, history[:120])
    check("重开后看到已拒绝的候选", "已拒绝" in history, history[:120])
    answers = rows("SELECT answer FROM followups ORDER BY id", data_dir)
    check("重开后回答还在",
          [r[0] for r in answers] == ["我忘了移项要变号", "我把减号当成加号了"],
          str(answers))
    check("重开后错因仍含候选",
          any("变号" in str(c) for c in causes_of(ids["mistake_id"], data_dir)),
          str(causes_of(ids["mistake_id"], data_dir)))
    size8 = shoot(insight, "exe-insight-1920x1080.png", 1920, 1080)
    check("个人学情截图 1920x1080", size8 > 10000, "%d 字节" % size8)

    log("")
    log("== 十四、已确认错题详情入口 ==")
    V.click(follow, "followupClose")
    V.click(insight, "backToStudentsFromInsight")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    V.click(students, "openMistakesButton")
    mistakes = V.find_window(app, V.TITLE_MISTAKES, timeout=60)
    check("错题列表打开", bool(mistakes))
    check("选中错题行", click_first(mistakes, "mistakeTable"))
    V.wait_button_text(mistakes, "修改", timeout=30)
    V.click(mistakes, "editMistakeButton")
    detail = wait_win(app, "修改错题", timeout=60)
    check("错题详情打开", bool(detail))
    if detail:
        found = False
        try:
            ctl = V.ctl(detail, "mistakeFollowup", timeout=10)
            found = not ctl.is_hidden() if hasattr(ctl, "is_hidden") else ctl.is_visible()
        except Exception as exc:  # noqa: BLE001
            log("读追问按钮失败：%s" % exc)
        check("已确认错题有「追问这个问题」", bool(found))

    log("")
    log("== 十五、其余界面截图（另一种尺寸） ==")
    if detail:
        V.click(detail, "mistakeCancel")
        wait_no_win(app, "修改错题", timeout=30)
    V.click(mistakes, "backToStudents")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    size9 = shoot(students, "exe-students-1920x1080.png", 1920, 1080)
    check("学生名单截图 1920x1080", size9 > 10000, "%d 字节" % size9)
    V.click(students, "openAnswerReviews")
    answer_win = A.wait_window(app, "作答分析")
    check("作答分析窗口打开", bool(answer_win))
    size10 = shoot(answer_win, "exe-answers-1366x768.png", 1366, 768)
    check("作答分析截图 1366x768", size10 > 10000, "%d 字节" % size10)
    size11 = shoot(answer_win, "exe-answers-1920x1080.png", 1920, 1080)
    check("作答分析截图 1920x1080", size11 > 10000, "%d 字节" % size11)

    log("")
    log("== 十六、个体 AI 开关：关闭 → 六入口 403；开启 → 立即可用 ==")
    # T30 新增（整段为新断言，不替换任何既有断言）：打包版同一份库上验双路径。
    # 用的是 EXE 自己那个数据目录里的账号（与 EXE 内登录的是同一个 teacher 账号）。
    from app import api as api_mod
    from desktop.service import ApiError as ServiceApiError, DesktopService as _DS

    gate_service = _DS(data_dir)
    gate_service.login("exe_teacher", "pw123456")
    # 同一个库、同一个账号，但新开的 service 实例没有本次登录的密钥（密钥只在内存），
    # 所以要把本机桩配好，否则「开启后能产出内容」这条会因「未配置密钥」失败——
    # 那是配置缺失，不是闸门问题，不能混作一条断言。
    gate_service.save_ai_settings("custom", stub_base, "stub-vision", "sk-x",
                                  service_name="本地模拟服务")
    off = gate_service.set_ai_individual(False)
    check("十六.1 关闭后账号状态是「未授权」",
          off.get("ai_individual_allowed") is False, off.get("ai_individual_allowed"))
    answer_rows = rows("SELECT id FROM answers LIMIT 1", data_dir)
    answer_id = answer_rows[0][0] if answer_rows else None
    follow_ids = rows("SELECT id FROM followups ORDER BY id LIMIT 1", data_dir)
    follow_id = follow_ids[0][0] if follow_ids else None
    stub_before = 0

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

    expect_isolation("十六.2 错题照片分析建议 → 403 ai_isolation",
                     lambda: gate_service.preview_analysis(ids["mistake_id"], consent=True,
                                                           task_id="t30-a"))
    expect_isolation("十六.3 错题重新分析（旧入口同一道闸门）→ 403 ai_isolation",
                     lambda: api_mod.api_reanalyze(
                         gate_service._ctx(params={"id": ids["mistake_id"]},
                                           body={"consent": True})))
    expect_isolation("十六.4 个人辅导建议生成 → 403 ai_isolation",
                     lambda: gate_service.generate_student_advice(
                         ids["class_id"], ids["student_id"], consent=True))
    expect_isolation("十六.5 追问候选生成 → 403 ai_isolation",
                     lambda: gate_service.suggest_followup(
                         ids["class_id"], ids["student_id"], "移项", consent=True))
    expect_isolation("十六.6 追问后的错因候选生成 → 403 ai_isolation",
                     lambda: gate_service.update_followup_causes(
                         ids["class_id"], ids["student_id"], follow_id, consent=True))
    expect_isolation("十六.7 作答分析 → 403 ai_isolation",
                     lambda: gate_service.analyse_answer(
                         ids["class_id"], answer_id, consent=True))
    on = gate_service.set_ai_individual(True)
    check("十六.8 重新开启后账号状态是「已授权」",
          on.get("ai_individual_allowed") is True, on.get("ai_individual_allowed"))
    advice = gate_service.generate_student_advice(ids["class_id"], ids["student_id"], consent=True)
    check("十六.9 开启后同一条业务路径立刻能产出建议（不是「反正会 403」）",
          advice.get("ok") is True
          and bool((advice.get("advice") or {}).get("content")),
          (advice.get("ok"), advice.get("error")))

    close_app(app, proc)
    V.stop_ai_stub()
    return finish()


def cleanup():
    for proc in procs:
        try:
            if proc.poll() is None:
                V.stop(proc)
        except Exception:
            pass
    try:
        V.stop_ai_stub()
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
