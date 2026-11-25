"""桌面测试版入口：双击启动，或用 python -m desktop --data-dir DIR 指定数据目录。

数据目录必须明确：要么是已存在的 1.3.0 数据目录，要么是老师点「新建」创建的。
桌面端不会在背后另建一份数据库；打开 1.3.0 的库时只为学生表补一个「停用状态」字段，
补之前先留一份完整备份。
"""
import argparse
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from app import backup  # noqa: E402
from app.db import MigrationError  # noqa: E402
from desktop import data_dir as data_dir_state  # noqa: E402
from desktop.service import DesktopService, RestoreBlockedError  # noqa: E402
from desktop.ui import DataDirDialog, DesktopApp  # noqa: E402

LOCKED_EXIT = 2
DATA_DIR_EXIT = 3
CANCELLED_EXIT = 4


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return ROOT


def default_data_dir():
    return os.path.join(app_dir(), "data")


def remembered_file():
    return os.path.join(app_dir(), "last-data-dir.txt")


def read_remembered():
    """返回 (状态, 值)。状态：none=从没记忆过；path=记忆了路径；bad=记忆文件无法读取或内容异常。"""
    path = remembered_file()
    if not os.path.exists(path):
        return "none", None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
    except UnicodeDecodeError:
        return "bad", "记忆文件不是 UTF-8 文本，无法读取"
    except OSError as exc:
        return "bad", "记忆文件无法读取：%s" % exc
    if not text:
        return "bad", "记忆文件是空的，内容异常"
    if "\x00" in text:
        return "bad", "记忆文件里的路径含有非法字符，内容异常"
    return "path", text


def write_remembered(path):
    try:
        with open(remembered_file(), "w", encoding="utf-8") as handle:
            handle.write(path)
        return True
    except OSError:
        return False


def _report(title, text):
    """自动化测试用 MISTAKE_DESKTOP_NO_DIALOG=1 把提示写到 stderr，避免弹窗阻塞。"""
    if os.environ.get("MISTAKE_DESKTOP_NO_DIALOG"):
        print("%s：%s" % (title, text), file=sys.stderr, flush=True)
        return
    QMessageBox.warning(None, title, text)


def _write_crash_log(data_dir, text):
    try:
        log_dir = os.path.join(data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "desktop-error.log"), "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


def resolve_data_dir(args, app):
    """确定这次使用哪个数据目录；返回路径，失败时返回 None 并已给出提示。"""
    if args.data_dir:
        path = os.path.abspath(args.data_dir)
        state, message = data_dir_state.describe(path)
        if state == data_dir_state.READY:
            return path
        if args.create_data_dir and state in (data_dir_state.EMPTY, data_dir_state.MISSING):
            try:
                return data_dir_state.create_new(path)
            except (OSError, ValueError) as exc:
                _report("无法使用这个数据目录", "%s\n%s" % (path, exc))
                return None
        _report("无法使用这个数据目录", "%s\n%s" % (path, message))
        return None

    status, remembered = read_remembered()
    if status == "path":
        candidate = os.path.abspath(remembered)
        state, message = data_dir_state.describe(candidate)
        if state == data_dir_state.READY:
            return candidate
        # 记忆过的目录失效：明确提示原路径和原因，让老师重新选择或退出，
        # 即使旁边的默认 data 有效也不能自动切过去。
        dialog = DataDirDialog(
            candidate,
            notice="上次使用的数据目录已失效：\n%s\n%s\n请重新选择，或点「退出」。" % (candidate, message),
        )
        if dialog.exec() != QDialog.Accepted or not dialog.data_dir:
            return None
        return dialog.data_dir

    if status == "bad":
        dialog = DataDirDialog(
            default_data_dir(),
            notice="无法读取数据目录记忆（%s）。\n请重新选择数据目录，或点「退出」。" % remembered,
        )
        if dialog.exec() != QDialog.Accepted or not dialog.data_dir:
            return None
        return dialog.data_dir

    # 从未记忆过目录：沿用默认目录识别或首次选择流程。
    if data_dir_state.describe(default_data_dir())[0] == data_dir_state.READY:
        return os.path.abspath(default_data_dir())
    dialog = DataDirDialog(default_data_dir())
    if dialog.exec() != QDialog.Accepted or not dialog.data_dir:
        return None
    return dialog.data_dir


def main(argv=None):
    parser = argparse.ArgumentParser(prog="desktop", description="数智伴学桌面测试版")
    parser.add_argument("--data-dir", default=None, help="数据目录；不给则启动时选择")
    parser.add_argument("--create-data-dir", action="store_true",
                        help="允许在空目录里新建数据（不给则只接受已有 1.3.0 数据目录）")
    parser.add_argument("--ai-test-base", default="",
                        help="仅自动化测试：本机回环 AI 地址，正式使用不要填")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("数智伴学")
    # 退出只由明确的关闭动作决定（关登录窗或班级窗），
    # 不让「当前没有可见窗口」这种中间状态把程序带走。
    app.setQuitOnLastWindowClosed(False)

    data_dir = resolve_data_dir(args, app)
    if not data_dir:
        return CANCELLED_EXIT if args.data_dir is None else DATA_DIR_EXIT
    print("数据目录：%s" % data_dir, file=sys.stderr, flush=True)

    # 恢复保护：在任何写入（锁文件、记忆文件、数据库）之前检查，
    # 发现恢复异常或待处理任务时拒绝进入，避免绕过原网页版入口处的数据保护。
    problem = backup.restore_problem(data_dir)
    if problem:
        _report("无法使用这个数据目录", "%s\n%s" % (data_dir, problem))
        return DATA_DIR_EXIT

    def handle_exception(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _write_crash_log(data_dir, text)
        _report("程序出错", "程序遇到了问题：\n%s" % exc_value)

    sys.excepthook = handle_exception

    import launcher

    handles = []
    try:
        for role in ("instance", "serve"):
            handles.append(launcher.acquire_data_lock(data_dir, role))
    except RuntimeError as exc:
        for handle in handles:
            handle.close()
        _report("无法启动", str(exc))
        return LOCKED_EXIT
    app.aboutToQuit.connect(lambda: [handle.close() for handle in handles])

    try:
        service = DesktopService(data_dir, test_ai_base=args.ai_test_base)
    except RestoreBlockedError as exc:
        # 业务入口的双保险：即使上面的检查被绕过，这里也会拒绝并说明。
        _report("无法使用这个数据目录", "%s\n%s" % (data_dir, exc.message))
        return DATA_DIR_EXIT
    except MigrationError as exc:
        # 结构升级失败：原库没动，备份还在，明确告诉老师真实状态。
        _report("无法使用这个数据目录", "%s\n%s" % (data_dir, exc))
        return DATA_DIR_EXIT
    except ValueError as exc:
        _report("无法启动", "测试地址配置无效：%s" % exc)
        return DATA_DIR_EXIT
    window = DesktopApp(service, data_dir)
    window.start()

    # 只有老师明确确认并成功打开目录后，才更新目录记忆；保存失败也要明确说明，不悄悄换库。
    if not write_remembered(data_dir):
        _report("提示", "数据目录记忆无法保存（可能是软件所在文件夹只读）。\n当前数据不受影响，但下次启动需要重新选择数据目录。")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
