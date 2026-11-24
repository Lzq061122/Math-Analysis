"""打包源码 ZIP（src、tests、tools 与打包资源；排除数据、构建产物与临时文件）。"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from app.version import VERSION  # noqa: E402

OUT = ROOT / "build" / "源码" / ("数智伴学-源码-" + VERSION + ".zip")

pairs = []
for folder in ("src", "packaging", "licenses-desktop"):
    for path in sorted((ROOT / folder).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            pairs.append((path, path.relative_to(ROOT).as_posix()))
for path in sorted((ROOT / "tools").glob("*.py")):
    if not path.name.startswith(".tmp-"):
        pairs.append((path, path.relative_to(ROOT).as_posix()))
# tests 整树带走：测试脚本、说明与 fixtures/legacy146 样例都是复现测试所必需。
for path in sorted((ROOT / "tests").rglob("*")):
    relative = path.relative_to(ROOT).as_posix()
    if not path.is_file():
        continue
    if any(part.startswith(".tmp") or part == "__pycache__" for part in path.relative_to(ROOT).parts):
        continue
    if path.name == "real_ai.env":
        continue
    pairs.append((path, relative))
for source, arcname in (
    ("README.md", "README.md"),
    ("docs/源码说明.txt", "源码说明.txt"),
    ("docs/作答导入模板.csv", "作答导入模板.csv"),
    ("packaging/desktop/使用说明.txt", "使用说明.txt"),
    ("docs/便携版使用说明.txt", "便携版使用说明.txt"),
):
    path = ROOT / source
    if not path.is_file():
        raise SystemExit("缺少 %s" % source)
    pairs.append((path, arcname))

pairs = sorted(pairs, key=lambda item: item[1].lower())
OUT.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for path, arcname in pairs:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise SystemExit("过大，需要检查：%s" % path)
        zf.write(path, arcname)
print("files", len(pairs))
print("zip", OUT.stat().st_size)
