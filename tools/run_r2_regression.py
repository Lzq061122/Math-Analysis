"""R2-06 全量回归：一次跑完 R2 与 R1 的既有套件，产出可核验的原始日志。

为什么要一个脚本而不是手敲一长串命令：R2-06 §9.2 的回归清单要「一次跑完并且数字可核对」，
手敲容易漏套件、也容易把上一次的输出当这一次的。这里固定三件事：
  1. 每个套件用**它该用的解释器**跑（app 层 py314；Qt/打包层 venv-desktop + offscreen）；
  2. 逐套件记录「项数/通过/失败/退出码」，任何一条失败即整体非零退出；
  3. 日志头部写清 git 提交、两个解释器版本与时间，便于以后复现。

跑法（在本机项目根）：
    C:\\Python314\\python.exe tools/run_r2_regression.py
    C:\\Python314\\python.exe tools/run_r2_regression.py --only r2      # 只跑 R2 新增
    C:\\Python314\\python.exe tools/run_r2_regression.py --list
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_APP = r"C:\Python314\python.exe"
PY_QT = r"E:\Agent\venv-desktop\Scripts\python.exe"
LOG = os.path.join(ROOT, "docs", "verify-r2-06-full-regression.txt")

# (套件文件, 解释器, 分组, 说明)
SUITES = [
    # ---- R2 新增 ----
    ("verify_r2_00_contract.py", "app", "r2", "R2 接口契约 / 四维状态 / 11 条失效规则 / 迁移 / 18 项口径"),
    ("verify_r2_01_zip_import.py", "qt", "r2", "R2-01 压缩包批量导入（含 offscreen 界面段）"),
    ("verify_r2_02_grading.py", "app", "r2", "R2-02 批量核验 / 评分 / 失效链 / 错题同步"),
    ("verify_r2_02_ui.py", "qt", "r2", "R2-02 批量核验与评分界面"),
    ("verify_r2_03_stats.py", "app", "r2", "R2-03 18 项指标口径 + §5.6 数据集"),
    ("verify_r2_03_ui.py", "qt", "r2", "R2-03 班级/个人看板"),
    ("verify_r2_04_pdf.py", "app", "r2", "R2-04 报告模型 / 预览与导出同快照 / A4 排版"),
    ("verify_r2_04_ui.py", "qt", "r2", "R2-04 报告预览窗口"),
    ("verify_r2_05_resources.py", "app", "r2", "R2-05 资源库 / 双审核 / 匹配 / 提示 / 任务 / 纸笔 PDF"),
    ("verify_r2_05_ui.py", "qt", "r2", "R2-05 教学资源界面"),
    ("verify_r2_05b_pdf_canvas.py", "app", "r2", "R2-05b 纯标准库 PDF 画布"),
    ("verify_r2_06_wiring.py", "qt", "r2", "R2-06 桌面接线（service 闸门 + 各入口）"),
    ("verify_r2_06_e2e.py", "app", "r2", "R2-06 全链路（导入→核验→错因/成绩→看板→报告）"),
    # ---- 原版与 R1 既有（回归）----
    ("verify_core_150.py", "app", "legacy", "原版核心：账号/班级/学生/错题/统计分析"),
    ("verify_answers_150.py", "app", "legacy", "原版 CSV 作答导入"),
    ("verify_answer_check_150.py", "app", "legacy", "R1 作答核验 / 改判 / 撤回 / 校正"),
    ("verify_tutor_170.py", "app", "legacy", "原版个人学情与教学建议"),
    ("verify_followup_180.py", "app", "legacy", "原版教师追问"),
    ("verify_r1_15_backup_tables.py", "app", "legacy", "备份/恢复表清单与完整性"),
    ("verify_r1_16_check_to_mistake.py", "app", "legacy", "作答核对 → 原版错题同步"),
    ("verify_r1_18_desktop_service.py", "qt", "legacy", "桌面 service 层（作答核对 + AI 开关）"),
    ("verify_r1_19_desktop_check_ui.py", "qt", "legacy", "桌面作答核对界面（原版 + R1）"),
    ("verify_r1_22_legacy_bridge.py", "app", "legacy", "旧数据只读桥"),
    ("verify_r1_27_batch_name_collision.py", "app", "legacy", "批次名撞演示批次的隔离"),
    ("verify_r1_31_image_egress.py", "app", "legacy", "真实作答照片外发限制"),
    ("verify_r1_counter_examples.py", "app", "legacy", "R1 反例集（真实 server + 真实库）"),
    ("verify_review_160.py", "app", "history", "原版 AI 分析（历史版本号断言：升级后应为 1.9.0）"),
]

COUNTS = [
    # 各套件的合计行写法并不统一（「合计 213 项，通过 213 项，失败 0 项」/「总计 39 项断言：通过 39，失败 0」/
    # 「通过 72 / 失败 0」），所以这里用一条宽容的正则，并且**取最后一条**：
    # 有几个套件会起子进程跑别的套件（答案核对会跑 answers/followup/review 三条回归线），
    # 取第一条会把子套件的数字当成父套件的（曾把 172 项记成 60 项）。
]
PATTERN = re.compile(
    # 注意 `[^，,、\n]*` 里的 \n：不加它会把下一行的「合计 …」拼进来
    # （曾把 135 项的行和紧随其后的 172 项行匹配成「135 项 / 通过 172」）。
    r"合计\s*(\d+)\s*项[^，,、\n]*[，,、]\s*通过\s*(\d+)\s*项?[^，,、\n]*[，,、]\s*失败\s*(\d+)")
PATTERN_TOTAL = re.compile(
    r"总计\s*(\d+)\s*项[^：:]*[：:]\s*通过\s*(\d+)\s*[，,]\s*失败\s*(\d+)")
PATTERN_PAIR = re.compile(r"通过\s*(\d+)\s*[/／]\s*失败\s*(\d+)")


def parse_counts(text):
    """从套件输出里取「项数/通过/失败」。多写法兼容 + 取最后一处（见上方注释）。"""
    found = PATTERN.findall(text)
    if found:
        total, passed, failed = found[-1]
        return int(total), int(passed), int(failed)
    found = PATTERN_TOTAL.findall(text)
    if found:
        total, passed, failed = found[-1]
        return int(total), int(passed), int(failed)
    found = PATTERN_PAIR.findall(text)
    if found:
        passed, failed = found[-1]
        return int(passed) + int(failed), int(passed), int(failed)
    return 0, 0, -1


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip() or "?"
    except Exception:
        return "?"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["r2", "legacy", "history", "all"], default="all")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for name, interp, group, note in SUITES:
            print("%-34s %-5s %-8s %s" % (name, interp, group, note))
        return 0

    picked = [s for s in SUITES if args.only == "all" or s[2] == args.only]
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"

    lines = []
    lines.append("# R2-06 全量回归原始日志")
    lines.append("")
    lines.append("- 生成时间：%s" % subprocess.run(
        [PY_APP, "-c", "import datetime;print(datetime.datetime.now().isoformat(timespec='seconds'))"],
        capture_output=True, text=True, env=env).stdout.strip())
    lines.append("- git 提交：%s（分支 %s）" % (git_rev(), subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True,
        text=True).stdout.strip()))
    for label, exe in (("app 解释器", PY_APP), ("Qt 解释器", PY_QT)):
        ver = subprocess.run([exe, "-c", "import sys;print(sys.version.split()[0])"],
                             capture_output=True, text=True, env=env).stdout.strip()
        lines.append("- %s：`%s` → Python %s" % (label, exe, ver))
    lines.append("- 环境：QT_QPA_PLATFORM=offscreen（Qt 套件）")
    lines.append("")
    lines.append("| 套件 | 分组 | 项数 | 通过 | 失败 | 退出码 |")
    lines.append("|---|---|---|---|---|---|")

    total = passed = failed = 0
    bad = []
    for name, interp, group, note in picked:
        path = os.path.join(ROOT, "tests", name)
        if not os.path.exists(path):
            lines.append("| %s | %s | — | — | — | 缺失 |" % (name, group))
            bad.append((name, "missing", ""))
            continue
        exe = PY_APP if interp == "app" else PY_QT
        proc = subprocess.run([exe, path], cwd=ROOT, capture_output=True, text=True,
                              env=env, encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        n, p, f = parse_counts(out)
        if f < 0:
            f = 0 if proc.returncode == 0 else 1
            p = 0 if f else 1
            n = p + f
        total += n
        passed += p
        failed += f
        ok = proc.returncode == 0 and f == 0
        if not ok:
            bad.append((name, proc.returncode, note))
        lines.append("| %s | %s | %d | %d | %d | %d |" % (name, group, n, p, f, proc.returncode))
        print("%-34s %-8s %4d 项 通过 %4d 失败 %d  exit=%d"
              % (name, group, n, p, f, proc.returncode))

    lines.append("")
    lines.append("**合计 %d 项，通过 %d 项，失败 %d 项；失败套件 %d 个。**"
                 % (total, passed, failed, len(bad)))
    if bad:
        lines.append("")
        lines.append("未通过的套件：")
        for name, code, note in bad:
            lines.append("- `%s`（exit=%s）%s" % (name, code, note))
    lines.append("")

    with open(LOG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))

    print("")
    print("合计 %d 项，通过 %d 项，失败 %d 项；失败套件 %d 个" % (total, passed, failed, len(bad)))
    print("日志：%s" % LOG)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
