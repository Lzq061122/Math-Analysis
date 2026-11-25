"""便携版启动器：双击打开小窗口，后台运行网页服务；关闭窗口时停止服务。

- 数据保存在软件文件夹的 data/ 里，首次启动自动创建空数据库。
- 启动器以子进程方式运行内置服务（同一 exe 加 --serve），只监听 127.0.0.1。
- 恢复备份时，服务先校验并解压到独立任务暂存目录，然后以退出码 75 结束；
  启动器在服务停止后把数据合并进数据库，再重新启动服务。
- 同一数据目录有两把进程锁：.instance.lock（启动窗口角色）与 .serve.lock
  （写入角色）。服务进程与命令行恢复持写入锁；启动窗口只持界面锁。这样
  即使启动窗口异常退出、后台服务还活着，新启动的服务也会因写入锁被占用而
  退出（代码 76），不会出现两个实例同时写同一份数据。
- 命令行（供测试或无界面使用）：
    --serve --port N --data-dir DIR     直接运行服务
    --restore-staged DIR --data-dir DIR  仅执行第 2 步的数据合并
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    SRC_DIR = os.path.join(BASE_DIR, "src")
else:
    SRC_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = os.path.dirname(SRC_DIR)
APP_NAME = "数学错题学情分析"
RESTORE_EXIT = 75
LOCKED_EXIT = 76
MIGRATION_EXIT = 77  # 与 server.py 的 MIGRATION_EXIT 一致：数据目录结构升级失败

_UI_LOCK = None  # 启动窗口角色锁文件句柄；进程结束自动释放
_SERVE_LOCK = None  # 写入角色锁文件句柄


def default_data_dir():
    return os.path.join(BASE_DIR, "data")


def _lock_message(data_dir):
    return (
        "另一份软件正在使用这个数据目录（%s）。\n"
        "同一份数据不能被两个软件同时打开，请先关闭另一个窗口。" % data_dir
    )


def acquire_data_lock(data_dir, role):
    """按角色独占 data 目录；失败抛 RuntimeError。role: instance / serve。"""
    os.makedirs(data_dir, exist_ok=True)
    handle = open(os.path.join(data_dir, "." + role + ".lock"), "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(_lock_message(data_dir))
    return handle


def acquire_instance_lock(data_dir):
    """启动窗口角色锁：同一个数据目录只允许一个启动窗口。"""
    global _UI_LOCK
    _UI_LOCK = acquire_data_lock(data_dir, "instance")
    return _UI_LOCK


def acquire_serve_lock(data_dir):
    """写入角色锁：服务进程、命令行恢复、启动器的恢复阶段使用。"""
    global _SERVE_LOCK
    _SERVE_LOCK = acquire_data_lock(data_dir, "serve")
    return _SERVE_LOCK


def release_serve_lock():
    global _SERVE_LOCK
    if _SERVE_LOCK:
        try:
            _SERVE_LOCK.close()
        except OSError:
            pass
        _SERVE_LOCK = None


def free_port(start=8765, tries=40):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用的本机端口")


def child_command(args):
    if getattr(sys, "frozen", False):
        return [sys.executable] + args
    return [sys.executable, os.path.abspath(__file__)] + args


def wait_ready(port, proc, timeout=40):
    url = "http://127.0.0.1:%d/api/info" % port
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def read_marker(data_dir):
    path = os.path.join(data_dir, ".restore-pending.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- 服务子进程
def run_serve(argv):
    sys.path.insert(0, SRC_DIR)
    if "--data-dir" not in argv:
        # 打包后直接以 --serve 启动时，数据目录必须落在 exe 所在文件夹，
        # 而不是 server.py 自身所在的 _internal 目录
        argv = list(argv) + ["--data-dir", default_data_dir()]
    data_dir = default_data_dir()
    if "--data-dir" in argv:
        data_dir = argv[argv.index("--data-dir") + 1]
    if sys.stdout is None or sys.stderr is None:
        # 打包成窗口程序后没有控制台，日志写进数据目录，避免 print 报错
        log_dir = os.path.join(data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        stream = open(os.path.join(log_dir, "server.log"), "a", encoding="utf-8")
        sys.stdout = stream
        sys.stderr = stream
    try:
        acquire_serve_lock(os.path.abspath(data_dir))
    except RuntimeError as exc:
        print(str(exc))
        return LOCKED_EXIT
    import server

    return server.run_serve(argv)


def run_restore_swap(argv):
    import argparse

    sys.path.insert(0, SRC_DIR)
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-staged", required=True)
    parser.add_argument("--data-dir", default=default_data_dir())
    parser.add_argument("--restore-user", type=int, default=0)
    args = parser.parse_args(argv)
    data_dir = os.path.abspath(args.data_dir)
    try:
        acquire_serve_lock(data_dir)
    except RuntimeError as exc:
        if sys.stdout is not None:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    from app import backup

    user_id = args.restore_user
    marker = read_marker(data_dir)
    if not user_id:
        user_id = int((marker or {}).get("user_id") or 0)
    try:
        result = backup.apply_staged(
            args.restore_staged,
            data_dir,
            user_id,
            mode=str((marker or {}).get("mode") or "restore"),
            legacy_confirmed=bool((marker or {}).get("legacy")),
        )
    except backup.BackupError as exc:
        rollback_failed = bool(getattr(exc, "rollback_failed", False))
        finalize_failed = bool(getattr(exc, "finalize_failed", False))
        if rollback_failed or finalize_failed:
            # 这两种失败都已写入真实的 .restore-result.json，不得用「原数据未改动」覆盖
            pass
        else:
            backup.write_failure(data_dir, str(exc))
        # 无论哪种失败都移除待恢复标记，否则用户无法再次发起恢复；
        # 回滚失败的暂存目录由 apply_staged 保留，不在这里清理。
        try:
            os.remove(os.path.join(data_dir, ".restore-pending.json"))
        except OSError:
            pass
        if sys.stdout is not None:
            print(json.dumps(
                {"ok": False, "rollback_failed": rollback_failed, "finalize_failed": finalize_failed,
                 "error": str(exc)},
                ensure_ascii=False,
            ))
        return 1
    if sys.stdout is not None:
        print(json.dumps({"ok": True, "safety_backup": result["safety"], "counts": result["counts"]},
                         ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 图形启动器
class Launcher:
    def __init__(self, root, data_dir=None, port=None):
        self.root = root
        self.preferred_port = port
        self.data_dir = data_dir or default_data_dir()
        os.makedirs(self.data_dir, exist_ok=True)
        self.port = None
        self.proc = None
        self.log = None
        self.closing = False
        self.restarting = False
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.start)

    def build_ui(self):
        import tkinter as tk
        from tkinter import messagebox  # noqa: F401

        root = self.root
        root.title(APP_NAME + "（便携版）")
        root.geometry("480x330")
        root.minsize(420, 300)

        frame = tk.Frame(root, padx=18, pady=14)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text=APP_NAME, font=("Microsoft YaHei UI", 15, "bold"), anchor="w").pack(fill="x")
        tk.Label(frame, text="教师端 · 数据保存在本软件文件夹内", anchor="w", fg="#555").pack(fill="x", pady=(2, 10))

        self.status_var = tk.StringVar(value="正在启动…")
        tk.Label(frame, textvariable=self.status_var, anchor="w", fg="#1a5fb4").pack(fill="x")

        self.url_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=self.url_var, anchor="w", fg="#333").pack(fill="x", pady=(4, 0))

        tk.Label(frame, text="数据目录：", anchor="w", fg="#555").pack(fill="x", pady=(10, 0))
        tk.Label(frame, text=self.data_dir, anchor="w", fg="#333", wraplength=430, justify="left").pack(fill="x")

        buttons = tk.Frame(frame)
        buttons.pack(fill="x", pady=(16, 0))
        tk.Button(buttons, text="打开界面", width=12, command=self.open_browser).pack(side="left")
        tk.Button(buttons, text="备份文件夹", width=12, command=self.open_backups).pack(side="left", padx=8)
        tk.Button(buttons, text="使用说明", width=10, command=self.open_help).pack(side="left")
        self.quit_button = tk.Button(buttons, text="关闭软件", width=12, command=self.shutdown)
        self.quit_button.pack(side="right")

        tk.Label(
            frame,
            text="账号只保存在这台电脑上；换电脑时，请在新电脑注册账号后用「导入备份」迁移数据。",
            anchor="w", fg="#777", wraplength=430, justify="left",
        ).pack(fill="x", pady=(14, 0))

    # -- 启停 -------------------------------------------------------------
    def start(self):
        if read_marker(self.data_dir):
            # 上次恢复中断（软件在恢复前被关闭），重新打开时把恢复做完
            self.restarting = True
            self.status_var.set("检测到未完成的恢复任务，正在继续…")
            threading.Thread(target=self.restore_then_restart, daemon=True).start()
            return
        self.port = self.preferred_port or free_port()
        self.preferred_port = self.port
        self.status_var.set("正在启动服务…")
        self.restarting = False
        log_dir = os.path.join(self.data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        if self.log:
            try:
                self.log.close()
            except OSError:
                pass
        self.log = open(os.path.join(log_dir, "server.log"), "a", encoding="utf-8")
        self.log.write("\n--- %s 启动 ---\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        self.log.flush()
        args = [
            "--serve", "--host", "127.0.0.1", "--port", str(self.port),
            "--data-dir", self.data_dir, "--no-demo", "--portable",
        ]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            child_command(args), cwd=BASE_DIR, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        threading.Thread(target=self.watch, daemon=True).start()

    def watch(self):
        ok = wait_ready(self.port, self.proc)
        if not ok:
            self.root.after(0, lambda: self.failed("服务启动失败。请查看 data/logs/server.log。"))
            return
        self.root.after(0, self.ready)
        code = self.proc.wait()
        self.root.after(0, lambda: self.exited(code))

    def ready(self):
        url = "http://127.0.0.1:%d/" % self.port
        self.url_var.set(url)
        self.status_var.set("运行中（关闭此窗口会停止服务）")
        webbrowser.open(url)

    def exited(self, code):
        if self.closing or self.restarting:
            return
        if code == RESTORE_EXIT:
            self.restarting = True
            self.status_var.set("正在恢复备份…")
            threading.Thread(target=self.restore_then_restart, daemon=True).start()
            return
        if code == LOCKED_EXIT:
            self.failed(
                "同一份数据正被另一个软件实例使用（可能是残留的后台服务）。\n"
                "请先关闭所有正在运行的窗口；如果还提示占用，请重启电脑后再打开本软件。"
            )
            return
        if code == MIGRATION_EXIT:
            self.failed(
                "这个数据目录暂时无法使用，软件没有改动里面的数据。\n\n"
                "具体原因已经写进 data/logs/server.log，请打开查看；\n"
                "如果是「另一份软件正在使用这个数据目录」，请先关闭其它窗口再重试。"
            )
            return
        if code == 0:
            self.status_var.set("服务已停止")
            self.on_close()
            return
        self.failed("服务意外停止（代码 %s）。请查看 data/logs/server.log。" % code)

    def failed(self, message):
        from tkinter import messagebox

        self.status_var.set("启动失败")
        messagebox.showerror(APP_NAME, message)
        self.on_close()

    def restore_then_restart(self):
        marker = read_marker(self.data_dir)
        status, error, rollback_failed, finalize_failed = "恢复完成", "", False, False
        if not marker:
            status, error = "没有找到待恢复的备份", "暂存信息缺失"
        else:
            try:
                acquire_serve_lock(self.data_dir)
            except RuntimeError as exc:
                status, error = "恢复失败", str(exc)
            else:
                try:
                    from app import backup

                    result = backup.apply_staged(
                        marker.get("staging") or "",
                        self.data_dir,
                        int(marker.get("user_id") or 0),
                        mode=str(marker.get("mode") or "restore"),
                        legacy_confirmed=bool(marker.get("legacy")),
                    )
                    safety = result.get("safety") or ""
                    status = "恢复完成" + ("，原数据已备份到 " + os.path.basename(safety) if safety else "")
                except Exception as exc:  # noqa: BLE001 - 恢复失败要保住原数据并把真实原因告诉老师
                    rollback_failed = bool(getattr(exc, "rollback_failed", False))
                    finalize_failed = bool(getattr(exc, "finalize_failed", False))
                    status, error = "恢复失败", str(exc)
                finally:
                    release_serve_lock()
                    try:
                        os.remove(os.path.join(self.data_dir, ".restore-pending.json"))
                    except OSError:
                        pass
                    # 回滚失败时保留暂存目录与自动备份：它们可能是唯一还能恢复的数据，不得清理
                    if not rollback_failed:
                        leftover = os.path.join(self.data_dir, ".restore")
                        if os.path.isdir(leftover):
                            import shutil

                            shutil.rmtree(leftover, ignore_errors=True)
        self.root.after(0, lambda: self.after_restore(status, error, rollback_failed, finalize_failed))

    def after_restore(self, status, error, rollback_failed=False, finalize_failed=False):
        from tkinter import messagebox

        self.status_var.set(status)
        self.start()
        if error:
            if rollback_failed:
                messagebox.showerror(
                    APP_NAME,
                    "恢复没有完成，且数据库回滚没有成功，当前数据状态不确定。\n\n"
                    "恢复前的数据已自动备份到 data/backups/ 目录，暂存文件也已保留。\n"
                    "请保留整个软件文件夹，联系技术人员处理，不要继续操作。",
                )
            elif finalize_failed:
                messagebox.showerror(
                    APP_NAME,
                    "数据已经恢复，但收尾没有全部完成：%s\n\n"
                    "恢复前的数据已自动备份到 data/backups/ 目录。\n"
                    "请重新登录并核对班级、学生、错题与照片是否完整。"
                    % error,
                )
            else:
                messagebox.showerror(APP_NAME, "恢复没有完成：%s\n\n原数据未被改动，软件会继续使用原来的数据。" % error)
        else:
            messagebox.showinfo(APP_NAME, "备份已恢复。浏览器会重新打开登录页，请用恢复数据里的账号登录。")

    def shutdown(self):
        self.closing = True
        self.status_var.set("正在关闭…")
        self.stop_child()
        self.root.after(400, self.root.destroy)

    def stop_child(self):
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:  # noqa: BLE001 - 退出时尽力而为
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def on_close(self):
        self.closing = True
        self.stop_child()
        try:
            if self.log:
                self.log.close()
        except OSError:
            pass
        self.root.destroy()

    # -- 按钮 -------------------------------------------------------------
    def open_browser(self):
        if self.port:
            webbrowser.open("http://127.0.0.1:%d/" % self.port)

    def open_backups(self):
        folder = os.path.join(self.data_dir, "backups")
        os.makedirs(folder, exist_ok=True)
        self._open_folder(folder)

    def open_help(self):
        for path in (
            os.path.join(BASE_DIR, "使用说明.txt"),
            os.path.join(BASE_DIR, "docs", "便携版使用说明.txt"),
            os.path.join(BASE_DIR, "便携版说明.txt"),
        ):
            if os.path.isfile(path):
                os.startfile(path)  # noqa: S606 - 本地说明文件
                return
        self.open_backups()

    def _open_folder(self, folder):
        if os.name == "nt":
            os.startfile(folder)  # noqa: S606
        else:
            webbrowser.open("file://" + folder)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--serve":
        return run_serve(argv[1:])
    if argv and argv[0] == "--restore-staged":
        try:
            return run_restore_swap(argv)
        except Exception:
            import traceback

            data_dir = default_data_dir()
            if "--data-dir" in argv:
                data_dir = argv[argv.index("--data-dir") + 1]
            try:
                log_dir = os.path.join(data_dir, "logs")
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, "launcher-error.log"), "a", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc() + "\n")
            except OSError:
                pass
            return 1
    if argv and argv[0] == "--version":
        sys.path.insert(0, SRC_DIR)
        from app.version import VERSION

        print(VERSION)
        return 0

    import tkinter as tk

    root = tk.Tk()
    data_dir = default_data_dir()
    port = None
    if "--data-dir" in argv:
        data_dir = argv[argv.index("--data-dir") + 1]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    try:
        acquire_instance_lock(os.path.abspath(data_dir))
    except RuntimeError as exc:
        root.withdraw()
        from tkinter import messagebox

        messagebox.showerror(APP_NAME, str(exc))
        root.destroy()
        return 1
    Launcher(root, data_dir=data_dir, port=port)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
