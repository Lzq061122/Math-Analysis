"""打包 Windows 便携版：PyInstaller 单文件夹 + 使用说明，输出 zip。

用法：python -X utf8 tools/build_portable.py [--skip-zip]
产物：dist/数智伴学/ 与 dist/数智伴学-便携版-<版本>.zip
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from app.version import VERSION  # noqa: E402

DIST = os.path.join(ROOT, "build", "便携版")
BUILD = os.path.join(ROOT, "build", "portable-work")
NAME = "数智伴学"
APP_DIR = os.path.join(DIST, NAME)
ZIP_PATH = os.path.join(DIST, NAME + "-便携版-" + VERSION + ".zip")


def run_pyinstaller():
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--add-data", os.path.join(ROOT, "src", "web") + ";web",
        "--hidden-import", "app.demo",
        "--hidden-import", "app.backup",
        "--hidden-import", "app.version",
        "--paths", os.path.join(ROOT, "src"),
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
        os.path.join(ROOT, "src", "launcher.py"),
    ]
    print("运行:", " ".join(args))
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"  # 子进程 PyInstaller 也按 UTF-8 输出，避免日志里中文路径乱码
    subprocess.run(args, cwd=ROOT, check=True, env=env)


def copy_extras():
    for source, name in (
        (os.path.join(ROOT, "docs", "便携版使用说明.txt"), "使用说明.txt"),
        (os.path.join(ROOT, "README.md"), "README.md"),
    ):
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(APP_DIR, name))
    data_dir = os.path.join(APP_DIR, "data")
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir)
        print("已删除打包目录里的 data/，确保分发包不带任何数据")


def make_zip():
    if os.path.isfile(ZIP_PATH):
        os.remove(ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as bundle:
        for folder, _dirs, files in os.walk(APP_DIR):
            for name in files:
                path = os.path.join(folder, name)
                bundle.write(path, os.path.relpath(path, DIST))
    return ZIP_PATH


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-zip", action="store_true")
    args = parser.parse_args(argv)

    if os.path.isdir(APP_DIR):
        shutil.rmtree(APP_DIR)
    run_pyinstaller()
    copy_extras()

    exe = os.path.join(APP_DIR, NAME + ".exe")
    if not os.path.isfile(exe):
        raise SystemExit("打包失败：没有生成 %s" % exe)
    total = sum(
        os.path.getsize(os.path.join(folder, name))
        for folder, _dirs, files in os.walk(APP_DIR)
        for name in files
    )
    print("程序目录：%s（%.1f MB）" % (APP_DIR, total / 1024 / 1024))
    if not args.skip_zip:
        path = make_zip()
        print("压缩包：%s（%.1f MB）" % (path, os.path.getsize(path) / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
