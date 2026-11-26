"""教学资源导入工具（R2-05 A）：把资源包 JSON 写进教学资源库，默认新建即「待审核」。

用法（后端解释器）：

    C:\\Python314\\python.exe tools\\import_resources.py --data-dir <数据目录> --list
    C:\\Python314\\python.exe tools\\import_resources.py --data-dir <数据目录> --dry-run
    C:\\Python314\\python.exe tools\\import_resources.py --data-dir <数据目录> --package resources\\teaching_resources\\001-qukuohao.json

约定：
1. 资源包是一个 JSON 对象：`unit` / `knowledge_point` / `cause_code` / `source` 为包级默认值，
   `resources` 是资源列表（每项可覆盖包级默认值）；
2. **导入即送审**（`--no-submit` 可关）：首批真实资源一律先进「待审核」，未审不得用于正式推荐；
3. `resource_key` 由「类型 + 知识点 + 错因 + 标题」稳定推导，重复导入同一份包不会堆版本；
   内容真的变了才出新版本并重新送审；
4. `source` 含「合成测试」的包只用于自动化验收，导入后也不会进入正式推荐。

退出码：0 = 全部成功（含跳过），2 = 有资源校验失败，3 = 参数/文件错误。
"""

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from app import db as DB                              # noqa: E402
from app import teaching_resources as T               # noqa: E402

DEFAULT_DIR = os.path.join(ROOT, "resources", "teaching_resources")


def load_packages(paths):
    packages = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            try:
                payload = json.load(handle)
            except ValueError as exc:
                raise SystemExit("资源包不是合法 JSON：%s（%s）" % (path, exc))
        if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
            raise SystemExit("资源包格式不对（需要 unit/knowledge_point + resources 列表）：%s" % path)
        packages.append((path, payload))
    return packages


def collect(paths, data_dir):
    files = []
    for item in paths:
        if os.path.isdir(item):
            files.extend(sorted(glob.glob(os.path.join(item, "*.json"))))
        elif os.path.isfile(item):
            files.append(item)
        else:
            raise SystemExit("找不到资源包或目录：%s" % item)
    if not files:
        raise SystemExit("没有找到任何 .json 资源包（默认目录：%s）" % DEFAULT_DIR)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description="导入 R2-05 教学资源包（默认送审）")
    parser.add_argument("--data-dir", required=True, help="数据目录（含 app.db）")
    parser.add_argument("--package", action="append", default=[],
                        help="资源包文件或目录，可重复；缺省用 resources/teaching_resources")
    parser.add_argument("--dry-run", action="store_true", help="只报告会做什么，不写库")
    parser.add_argument("--no-submit", action="store_true", help="只建档，不送审（默认送审）")
    parser.add_argument("--list", action="store_true", help="只列出资源包里的条目，不连数据库")
    parser.add_argument("--created-by", type=int, default=None, help="导入账号 id（可选）")
    args = parser.parse_args(argv)

    targets = args.package or [DEFAULT_DIR]
    files = collect(targets, args.data_dir)
    packages = load_packages(files)

    if args.list:
        total = 0
        for path, payload in packages:
            print("[包] %s  %s / %s  资源 %d 条"
                  % (os.path.basename(path), payload.get("unit", ""),
                     payload.get("knowledge_point", ""), len(payload["resources"])))
            for item in payload["resources"]:
                total += 1
                print("  - %-8s %-28s 错因=%s"
                      % (item.get("kind", ""), item.get("title", ""),
                         item.get("cause_code", "") or "通用"))
        print("合计 %d 条资源（%d 个包）" % (total, len(packages)))
        return 0

    db_file = os.path.join(args.data_dir, "app.db")
    if not os.path.exists(db_file):
        raise SystemExit("数据目录里没有 app.db：%s" % db_file)
    database = DB.Database(db_file, create=False)
    if not DB.r2_schema_ready(db_file):
        raise SystemExit("库里还没有 R2 结构，先让桌面端启动一次（ensure_r2_schema）再导入。")

    totals = {"created": 0, "updated": 0, "skipped": 0, "submitted": 0, "errors": []}
    for path, payload in packages:
        report = T.import_resources(database, payload, created_by=args.created_by,
                                    submit=not args.no_submit, dry_run=args.dry_run)
        print("[导入] %s → 新建 %d / 改版 %d / 跳过 %d / 送审 %d / 失败 %d"
              % (os.path.basename(path), len(report["created"]), len(report["updated"]),
                 len(report["skipped"]), len(report["submitted"]), len(report["errors"])))
        for name in ("created", "updated", "skipped", "submitted"):
            totals[name] += len(report[name])
        for error in report["errors"]:
            totals["errors"].append((path, error))
            print("  ! 第 %d 条「%s」失败：%s（%s）"
                  % (error["index"], error["title"], error["message"], error["code"]))
    print("[合计] 新建 %(created)d / 改版 %(updated)d / 跳过 %(skipped)d / 送审 %(submitted)d"
          % totals)
    if totals["errors"]:
        print("[失败] %d 条资源没有导入" % len(totals["errors"]))
        return 2
    print("[提示] 导入的资源是「待审核」状态：双审核通过并发布后才会进入正式推荐。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
