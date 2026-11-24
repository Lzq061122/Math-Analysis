"""1.5.0 最终 EXE 的作答导入实测（独立桌面，窗口不出现）。

流程：校验 ZIP → 解压 → 启动 EXE → 注册、建班、建学生 → 打开「导入作答 CSV」
→ 选择自制 CSV → 预览与计数 → 确认导入 → 查数据库 → 重复导入 → 改内容触发冲突
→ 勾选覆盖 → 关闭重开确认数据仍在。

运行：.venv\\Scripts\\python.exe tests\\run_hidden.py -- .venv\\Scripts\\python.exe tests\\verify_answers_exe.py
"""
import csv
import ctypes
import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(ROOT, "src"))

from app.version import VERSION  # noqa: E402
import verify_mistakes_pack as V  # noqa: E402
from pywinauto import Application  # noqa: E402
import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

from app import answers as A  # noqa: E402

NAME = "数智伴学"
ZIP = os.path.join(ROOT, "build", "程序包", "%s-%s.zip" % (NAME, VERSION))
LOG_PATH = os.path.join(ROOT, "docs", "verify-answers-exe.txt")
TOTAL = [0]
FAILED = [0]
LINES = []


def log(text=""):
    LINES.append(text)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("gbk", "replace").decode("gbk"), flush=True)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if ok:
        log("PASS  %s" % name)
    else:
        FAILED[0] += 1
        log("FAIL  %s%s" % (name, ("  -> " + str(detail)) if detail else ""))


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def csv_text(rows):
    head = [label for label, _ in A.HEADERS]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=head)
    writer.writeheader()
    for item in rows:
        writer.writerow({key: item.get(key, "") for key in head})
    return out.getvalue()


def row(**kw):
    base = {
        "记录编号": "EX1", "学生姓名": "张三", "学校编号": "", "题号": "1",
        "题目内容": "解方程：3(x-2)=2x+5", "单元": "一元一次方程", "标准解法": "x=11",
        "学生步骤": "3x-6=2x+5", "来源批次": "九月作业", "作答日期": "2026-09-02",
        "缺测": "否", "缺测原因": "",
    }
    base.update(kw)
    return base


def _visible_hwnds(pid):
    found = []

    def collect(hwnd, _):
        try:
            _, owner = win32process.GetWindowThreadProcessId(hwnd)
            if owner == pid and win32gui.IsWindowVisible(hwnd):
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return found


def wait_window(app, part, timeout=60):
    """按标题找真实 HWND：UIA 的顶层窗口列表在新窗口打开后可能不刷新。"""
    end = time.time() + timeout
    while time.time() < end:
        for hwnd in _visible_hwnds(app.process):
            if part in win32gui.GetWindowText(hwnd):
                try:
                    return app.window(handle=hwnd)
                except Exception:
                    continue
        time.sleep(0.3)
    raise RuntimeError("等不到窗口：%s" % part)


def _native_edit(dlg_hwnd):
    """原生文件对话框里取文件名编辑框（递归找，优先标准控件 id 1148）。"""
    found = []

    def each(hwnd, _):
        if win32gui.GetClassName(hwnd) == "Edit":
            found.append((win32gui.GetDlgCtrlID(hwnd), win32gui.GetWindowRect(hwnd)[3], hwnd))
        return True

    win32gui.EnumChildWindows(dlg_hwnd, each, None)
    for ctrl_id, _top, hwnd in found:
        if ctrl_id == 1148:
            return hwnd
    return max(found, key=lambda item: item[1])[2] if found else None


def _native_open_button(dlg_hwnd):
    """原生对话框的「打开」按钮（控件 id 为 1）。"""
    found = []

    def each(hwnd, _):
        if win32gui.GetClassName(hwnd) == "Button":
            text = win32gui.GetWindowText(hwnd)
            if text.startswith("打开") or text.startswith("Open"):
                found.append((win32gui.GetDlgCtrlID(hwnd), hwnd))
        return True

    win32gui.EnumChildWindows(dlg_hwnd, each, None)
    for ctrl_id, hwnd in found:
        if ctrl_id == 1:
            return hwnd
    return found[0][1] if found else None


def _wait_dialog(app, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        for hwnd in _visible_hwnds(app.process):
            title = win32gui.GetWindowText(hwnd)
            if "打开" in title or "Open" in title or win32gui.GetClassName(hwnd) == "#32770":
                try:
                    return app.window(handle=hwnd)
                except Exception:
                    continue
        time.sleep(0.3)
    dump = [(win32gui.GetClassName(h), win32gui.GetWindowText(h))
            for h in _visible_hwnds(app.process)]
    raise RuntimeError("没有出现文件选择对话框：%s" % dump)


def choose_file(app, path):
    """把路径写进文件名框并确认；Qt 对话框与 Windows 原生对话框都走通。"""
    dlg = _wait_dialog(app)
    hwnd = dlg.handle
    long_path = os.path.abspath(path)
    target = None
    try:
        target = V.ctl(dlg, "fileNameEdit", timeout=4)
    except Exception:
        target = None
    if target is not None:
        try:
            target.set_edit_text(long_path)
        except Exception:
            target.iface_value.SetValue(long_path)
    else:
        edit = _native_edit(hwnd)
        if not edit:
            children = dlg.descendants()
            dump = [(c.element_info.control_type, c.window_text()) for c in children[:30]]
            raise RuntimeError("对话框里找不到文件名输入框：%s" % dump)
        win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, long_path)
        time.sleep(0.3)
        buffer = ctypes.create_unicode_buffer(2048)
        win32gui.SendMessage(edit, win32con.WM_GETTEXT, 2048, buffer)
        typed = buffer.value
        if typed.strip() and typed.strip() != long_path:
            raise RuntimeError("文件名被改成了别的值：%r" % typed)
    time.sleep(0.4)
    if target is None:
        button = _native_open_button(hwnd)
        if button:
            win32gui.SendMessage(button, win32con.BM_CLICK, 0, 0)
        else:
            win32gui.PostMessage(hwnd, win32con.WM_COMMAND, 1, 0)
    else:
        buttons = [c for c in dlg.descendants()
                   if (c.element_info.control_type or "") == "Button"
                   and (c.window_text() or "").startswith("打开") and c.is_enabled()]
        if buttons:
            # 右下角那个才是确认打开的按钮，左上角的「打开(&H)」是菜单。
            max(buttons, key=lambda c: (c.rectangle().bottom, c.rectangle().right)).invoke()
        else:
            win32gui.PostMessage(hwnd, win32con.WM_COMMAND, 1, 0)

    end = time.time() + 8
    while time.time() < end:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return
        time.sleep(0.2)
    win32gui.PostMessage(hwnd, win32con.WM_COMMAND, 1, 0)
    time.sleep(0.8)
    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return
    dump = {
        "class": win32gui.GetClassName(hwnd),
        "title": win32gui.GetWindowText(hwnd),
        "buttons": [(c.window_text(), c.is_enabled()) for c in dlg.descendants()
                    if (c.element_info.control_type or "") == "Button"],
    }
    raise RuntimeError("对话框没有关闭：%s" % dump)


def answer_window(app, timeout=60):
    return wait_window(app, "导入作答", timeout)


def open_import(app, students):
    V.click(students, "openAnswerImport")
    return answer_window(app)


def confirm_box(app, before_hwnds=None, timeout=20):
    """点掉确认导入的 QMessageBox：只看点击后新出现的顶层窗口。"""
    before = set(before_hwnds or [])
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        for hwnd in _visible_hwnds(app.process):
            if hwnd in before:
                continue
            try:
                win = app.window(handle=hwnd)
                items = win.descendants()
            except Exception:
                continue
            texts = [(i.window_text() or "").strip() for i in items
                     if (i.element_info.control_type or "") == "Button"]
            title = win.window_text()
            if title not in seen:
                seen.append(title)
            for item in items:
                text = (item.window_text() or "").strip()
                if text in ("Yes", "&Yes", "是(Y)", "是", "确定", "OK"):
                    log("确认框：%s / 按钮：%s" % (title, texts))
                    item.invoke()
                    return True
        time.sleep(0.3)
    log("没有找到确认框，新窗口：%s" % seen)
    return False


def main():
    work = os.path.join(ROOT, "tests", ".tmp-test", "answers-exe-" + uuid.uuid4().hex[:6])
    unpack = os.path.join(work, "解压件")
    prog = os.path.join(unpack, NAME)
    exe = os.path.join(prog, NAME + ".exe")
    data = os.path.join(work, "数据 主库")
    os.makedirs(work)

    V.WORK = Path(work)
    V.PROG_ROOT = Path(unpack)
    V.PROG = Path(prog)
    V.EXE = Path(exe)
    V.DATA = Path(data)
    V.SHOTS = Path(os.path.join(ROOT, "docs", "screenshots"))
    profile = Path(work) / "profile"
    for sub in ("Desktop", "Documents", "Downloads", "AppData/Roaming",
                "AppData/Local", "AppData/Local/Temp"):
        (profile / sub).mkdir(parents=True, exist_ok=True)
    V.PROFILE_PATH = profile

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
        shutil.unpack_archive(ZIP, unpack)
    check("包内有 EXE", any(name.endswith(NAME + ".exe") for name in names), names[:5])
    check("包内有老师使用说明", any(name.endswith("使用说明.txt") for name in names))
    check("EXE 已解出", os.path.isfile(exe), exe)
    log("EXE %d 字节 SHA-256 %s" % (os.path.getsize(exe), sha256_of(exe)))

    csv1 = os.path.join(work, "九月作业.csv")
    with open(csv1, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(csv_text([row(), row(记录编号="EX2", 题号="2", 学生步骤="", 缺测="是", 缺测原因="没交")]))

    log("")
    log("== 二、启动并准备班级 ==")
    proc = V.spawn(["--data-dir", data, "--create-data-dir"], "app")
    app = V.attach(proc)
    login = V.find_window(app, V.TITLE, require_id="loginUsername", timeout=90)
    V.click(login, "registerLink")
    V.fill(login, "registerUsername", "exe_teacher")
    V.fill(login, "registerPassword", "pw123456")
    V.fill(login, "registerConfirm", "pw123456")
    V.click(login, "registerButton")
    classes = V.find_window(app, V.TITLE, require_id="currentUser", timeout=90)
    V.create_class(app, classes, "初三 1 班")
    V.wait_text(classes, "classStatus", "共 1 个班级", timeout=60)
    V.click(classes, "openStudentsButton")
    students = V.find_window(app, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    V.add_student(app, students, "张三")
    V.wait_text(students, "studentStatus", "共 1 人", timeout=60)
    check("学生名单建好", "共 1 人" in (V.text_of(students, "studentStatus", "") or ""),
          V.text_of(students, "studentStatus", ""))

    log("")
    log("== 三、打开导入窗口 ==")
    import_win = open_import(app, students)
    check("导入窗口打开", bool(import_win))
    hint = V.text_of(import_win, "answerHint", "") or ""
    check("窗口说明不调用 AI", "不会调用 AI" in hint, hint[:120])
    check("窗口说明不发送姓名", "不会发送学生姓名" in hint, hint[:160])
    check("初始导入按钮禁用", not V.ctl(import_win, "answerImportButton").is_enabled())

    log("")
    log("== 四、选择 CSV 并预览 ==")
    V.click(import_win, "answerChooseFile")
    choose_file(app, csv1)
    deadline = time.time() + 60
    status = ""
    while time.time() < deadline:
        status = V.text_of(import_win, "answerStatus", "") or ""
        if "可导入" in status and "共 2 行" in status:
            break
        time.sleep(0.3)
    check("预览显示两行且可导入", "共 2 行" in status and "可导入 2" in status, status)
    check("预览后导入按钮可用", V.ctl(import_win, "answerImportButton").is_enabled())
    V.shot(import_win, "answers-import-preview.png")

    log("")
    log("== 五、确认导入并核对数据库 ==")
    before = _visible_hwnds(app.process)
    V.click(import_win, "answerImportButton")
    check("导入前出现确认框", confirm_box(app, before))
    deadline = time.time() + 60
    status = ""
    while time.time() < deadline:
        status = V.text_of(import_win, "answerStatus", "") or ""
        if "导入完成" in status:
            break
        time.sleep(0.3)
    check("状态栏显示导入完成", "导入完成" in status, status)
    report = V.text_of(import_win, "answerReport", "") or ""
    check("结果区写明批次", "九月作业" in report, report[:120])
    conn = sqlite3.connect(os.path.join(data, "app.db"))
    count = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    missing = conn.execute("SELECT missing FROM answers WHERE record_key='EX2'").fetchone()
    batch = conn.execute("SELECT batch FROM answers WHERE record_key='EX1'").fetchone()
    conn.close()
    check("数据库写入 2 条作答", count == 2, count)
    check("缺测记录按缺测保存", missing and missing[0] == 1, missing)
    check("批次写入为文件名", batch and batch[0] == "九月作业", batch)
    V.shot(import_win, "answers-import-done.png")

    log("")
    log("== 六、重复导入与冲突 ==")
    V.click(import_win, "answerChooseFile")
    choose_file(app, csv1)
    deadline = time.time() + 60
    status = ""
    while time.time() < deadline:
        status = V.text_of(import_win, "answerStatus", "") or ""
        if "重复 2" in status:
            break
        time.sleep(0.3)
    check("再次导入显示重复 2", "重复 2" in status, status)
    check("只有重复行时导入禁用", not V.ctl(import_win, "answerImportButton").is_enabled())

    csv2 = os.path.join(work, "九月作业-改.csv")
    with open(csv2, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(csv_text([row(学生步骤="3x-2x=5+6")]))
    V.click(import_win, "answerChooseFile")
    choose_file(app, csv2)
    deadline = time.time() + 60
    status = ""
    while time.time() < deadline:
        status = V.text_of(import_win, "answerStatus", "") or ""
        if "冲突 1" in status:
            break
        time.sleep(0.3)
    check("同编号改内容显示冲突", "冲突 1" in status, status)
    check("冲突时导入禁用", not V.ctl(import_win, "answerImportButton").is_enabled())
    box = V.ctl(import_win, "answerOverwrite")
    box.toggle()
    time.sleep(0.4)
    if not box.get_toggle_state():
        try:
            box.iface_toggle.Toggle()
        except Exception as exc:
            log("复选框切换退路失败：%s" % exc)
    time.sleep(0.6)
    check("勾选覆盖后导入可用", V.ctl(import_win, "answerImportButton").is_enabled())
    before = _visible_hwnds(app.process)
    V.click(import_win, "answerImportButton")
    check("覆盖前再次确认", confirm_box(app, before))
    deadline = time.time() + 60
    while time.time() < deadline:
        status = V.text_of(import_win, "answerStatus", "") or ""
        if "覆盖 1" in status:
            break
        time.sleep(0.3)
    check("覆盖后状态显示覆盖 1", "覆盖 1" in status, status)
    cells = V.table_text(import_win, "answerTable")
    check("覆盖后表格状态显示“已覆盖”", "已覆盖" in cells, cells)
    check("覆盖后不显示英文 overwritten", "overwritten" not in cells, cells)
    conn = sqlite3.connect(os.path.join(data, "app.db"))
    steps = conn.execute("SELECT student_steps FROM answers WHERE record_key='EX1'").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    conn.close()
    check("覆盖后内容更新且不重复计数", steps == "3x-2x=5+6" and total == 2, (steps, total))
    V.shot(import_win, "answers-import-conflict.png")
    import_win.close()
    time.sleep(0.8)

    log("")
    log("== 七、关闭重开 ==")
    V.stop(proc)
    time.sleep(1.5)
    proc2 = V.spawn(["--data-dir", data], "app2")
    app2 = V.attach(proc2)
    login2 = V.find_window(app2, V.TITLE, require_id="loginUsername", timeout=90)
    V.fill(login2, "loginUsername", "exe_teacher")
    V.fill(login2, "loginPassword", "pw123456")
    V.click(login2, "loginButton")
    classes2 = V.find_window(app2, V.TITLE, require_id="currentUser", timeout=90)
    V.click(classes2, "openStudentsButton")
    students2 = V.find_window(app2, V.TITLE_STUDENTS, require_id="studentClassName", timeout=60)
    import_win2 = open_import(app2, students2)
    V.click(import_win2, "answerChooseFile")
    choose_file(app2, csv2)
    deadline = time.time() + 60
    status = ""
    while time.time() < deadline:
        status = V.text_of(import_win2, "answerStatus", "") or ""
        if "重复 1" in status:
            break
        time.sleep(0.3)
    check("重开后已导入内容仍在（再选同一文件为重复）", "重复 1" in status, status)
    V.shot(import_win2, "answers-import-reopened.png")
    V.stop(proc2)

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    return finish()


def finish(force=None):
    code = force if force is not None else (1 if FAILED[0] else 0)
    LINES.append("退出码：%d" % code)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - 测试脚本要把现场写进日志
        import traceback
        log("")
        log("异常中止：")
        log(traceback.format_exc())
        code = finish(force=1)
    sys.exit(code)
