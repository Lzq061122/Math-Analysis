"""构建 Windows 桌面测试包（PySide6 + PyInstaller，64 位）。

用法：.venv\\Scripts\\python.exe tools\\build_desktop.py
产物：build\\数智伴学\\（onedir，含 exe 与依赖）
      build\\程序包\\数智伴学-<版本>.zip
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from app.version import VERSION  # noqa: E402

DIST = os.path.join(ROOT, "build")
BUILD = os.path.join(ROOT, "build-desktop")
NAME = "数智伴学"
ENTRY = os.path.join(ROOT, "tools", "desktop_entry.py")
LICENSE_DIR = os.path.join(ROOT, "licenses-desktop")

EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtPdf",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
]


def main():
    if not os.path.exists(ENTRY):
        raise SystemExit("缺少入口脚本：%s" % ENTRY)
    # 只清自己的产物，保留 build 里其它版本的包。
    for path in (os.path.join(DIST, NAME), BUILD):
        if os.path.isdir(path):
            shutil.rmtree(path)
    if os.path.isdir(DIST):
        os.makedirs(DIST, exist_ok=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", NAME,
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
        "--paths", os.path.join(ROOT, "src"),
        "--log-level", "WARN",
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(ENTRY)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    started = time.time()
    completed = subprocess.run(command, cwd=ROOT, env=env)
    print("PyInstaller 退出码：%s，用时 %.1f 秒" % (completed.returncode, time.time() - started), flush=True)
    if completed.returncode != 0:
        return completed.returncode

    folder = os.path.join(DIST, NAME)
    exe = os.path.join(folder, NAME + ".exe")
    guide = os.path.join(ROOT, "packaging", "desktop", "使用说明.txt")
    if os.path.exists(guide):
        # 带 UTF-8 BOM 写出，保证中文 Windows 的记事本双击打开不乱码。
        with open(guide, encoding="utf-8") as fh:
            guide_text = fh.read()
        with open(os.path.join(folder, "使用说明.txt"), "w", encoding="utf-8-sig") as fh:
            fh.write(guide_text)
    shutil.copytree(LICENSE_DIR, os.path.join(folder, "licenses"))
    archive = make_zip(folder)
    print("产物：%s" % exe, flush=True)
    print("压缩包：%s（%d 字节）" % (archive, os.path.getsize(archive)), flush=True)
    print("大小：%.1f MB" % (sum(os.path.getsize(os.path.join(base, name))
                                 for base, _dirs, files in os.walk(folder) for name in files) / 1048576.0), flush=True)
    return 0


def make_zip(folder):
    """打成 zip 发给老师：条目用正斜杠，解压后是同一个文件夹。"""
    import zipfile

    archive = os.path.join(os.path.dirname(folder), "程序包", os.path.basename(folder) + "-" + VERSION + ".zip")
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    if os.path.exists(archive):
        os.remove(archive)
    base = os.path.dirname(folder)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for current, dirs, files in os.walk(folder):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(current, name)
                zf.write(full, os.path.relpath(full, base).replace(os.sep, "/"))
    return archive


if __name__ == "__main__":
    sys.exit(main())
