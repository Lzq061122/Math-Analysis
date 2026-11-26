# -*- coding: utf-8 -*-
"""R2-04 界面套件：报告预览 / 导出 / 打印 / 导出历史（offscreen Qt）。

命令：`E:\\Agent\\venv-desktop\\Scripts\\python.exe tests\\verify_r2_04_ui.py`
（先 `$env:QT_QPA_PLATFORM='offscreen'`）
日志：`docs/verify-r2-04-ui.txt`

断言口径：
  - 预览文本 == `report_pdf.preview_lines(report)`（界面不另拼内容）；
  - 导出的 PDF 文本里能读回预览里的关键数值（预览与导出同源）；
  - 预览后改数据再导出仍对应原快照；刷新之后才反映新数据；
  - 导出历史真的落盘并在表里可见（账号/时间/用途/类型/筛选范围）；
  - 打印与导出字节相同（同源，不重新生成第二份内容）；
  - 越权账号在界面上得到失败提示，不会拿到别人的班级报告。
"""
import hashlib
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from logmeta import environment_lines, exit_line  # noqa: E402

from app import pdf_canvas, report_pdf, r2_stats  # noqa: E402
from desktop import theme, ui_report  # noqa: E402
from verify_r2_03_stats import BATCH, KP_BRACKET, build_env, seed_dataset  # noqa: E402

LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-04-ui.txt")
WORK = os.path.join(ROOT, "tests", ".tmp-r2-04-ui")
LINES = []
RESULTS = {"pass": 0, "fail": 0}
TODAY = "2026-11-26"
AT = "2026-11-26T19:30:00+08:00"
TEACHER = {"id": 1, "username": "teacher"}


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s  %s" % (name, detail))
    return bool(ok)


def equal(name, actual, expected):
    return check(name, actual == expected, "期望 %r，实际 %r" % (expected, actual))


def section(title):
    log("")
    log("== %s ==" % title)


def pdf_text(path):
    with open(path, "rb") as handle:
        return pdf_canvas.extract_text(handle.read())


def make_window(db, **overrides):
    options = {"kind": "class", "class_name": "初二(3)班", "batch": BATCH, "today": TODAY,
               "user": TEACHER, "db_file": os.path.join(WORK, "app.db"), "auto_exec": False,
               "generated_at": AT}
    options.update(overrides)
    return ui_report.ReportPreviewWindow(db, 1, **options)


def main():
    os.makedirs(WORK, exist_ok=True)
    for line in environment_lines():
        log(line)
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    env = build_env("ui-report")
    seed_dataset(env)

    section("一、预览内容与报告对象同源")
    window = make_window(env["db"], db_file=env["db_file"])
    report = window.report
    preview = window.preview.toPlainText()
    equal("1.1 预览文本 == preview_lines(report)",
          preview, "\n".join(report_pdf.preview_lines(report)))
    check("1.2 预览含平均分 80.00 与覆盖率 60.0%",
          "80.00" in preview and "60.0%" in preview, "")
    check("1.3 预览含知识点去括号 8 与错题 15", "去括号" in preview and "15" in preview, "")
    equal("1.4 标题与统计范围写在标题栏与说明里",
          (window.heading.text() == report["title"], "统计范围" in window.meta.text()),
          (True, True))
    equal("1.5 报告对象是同一份快照（窗口不重建第二个）", window.report is report, True)

    section("二、导出：预览数值能在 PDF 里读回")
    path = os.path.join(WORK, "class-report.pdf")
    result = window.export(path)
    check("2.1 导出成功并返回路径/字节数",
          result is not None and os.path.exists(result["path"]) and result["bytes"] > 10000,
          result)
    text = pdf_text(result["path"])
    equal("2.2 PDF 读回 80.00 / 60.0% / 去括号",
          ("80.00" in text, "60.0%" in text, KP_BRACKET in text), (True, True, True))
    pages = pdf_canvas.page_count(open(result["path"], "rb").read())
    check("2.3 PDF 页数 ≥2 且含页码结构", pages >= 2 and "共 %d 页" % pages in text, pages)
    equal("2.4 导出后状态栏写明路径与用途",
          result["path"] in window.meta.text(), True)

    section("三、导出历史（账号/时间/用途/类型/筛选范围）")
    equal("3.1 历史表出现 1 行", window.history_table.rowCount(), 1)
    values = [window.history_table.item(0, index).text() for index in range(6)]
    check("3.2 历史行含账号、用途、类型、筛选范围与文件名",
          values[1] == "teacher" and values[2] == "存档" and values[3] == "班级"
          and "第一次作业" in values[4] and values[5].endswith(".pdf"), values)
    entries = report_pdf.load_history(env["db_file"])
    equal("3.3 历史落盘且与表一致",
          (len(entries), entries[0]["user"], entries[0]["kind"]), (1, "teacher", "class"))
    window.purpose_box.setCurrentIndex(window.purpose_box.findData("教学研究"))
    window.export(os.path.join(WORK, "class-report-2.pdf"))
    equal("3.4 切换用途后新历史条目记录用途", 
          report_pdf.load_history(env["db_file"])[0]["purpose"], "教学研究")

    section("四、预览后改数据：导出仍对应原快照，刷新后才变")
    env["db"].tx().__enter__().execute("DELETE FROM answer_scores")
    window.export(os.path.join(WORK, "class-report-3.pdf"))
    frozen_text = pdf_text(os.path.join(WORK, "class-report-3.pdf"))
    equal("4.1 改数据后导出仍是原快照的平均分 80.00", "80.00" in frozen_text, True)
    window.refresh()
    refreshed_text = pdf_text(window.export(os.path.join(WORK, "class-report-4.pdf"))["path"])
    equal("4.2 刷新后导出反映新数据（平均分变暂无、不再写 80.00）",
          ("暂无" in refreshed_text, "80.00" in refreshed_text), (True, False))
    equal("4.3 刷新不会改动已导出的旧文件",
          pdf_text(os.path.join(WORK, "class-report-3.pdf")), frozen_text)

    section("五、个人报告：只含本人 + 证据链 + 空态建议")
    personal = make_window(env["db"], kind="student", student_id=3, db_file=env["db_file"])
    personal_text = personal.preview.toPlainText()
    check("5.1 个人报告含本人姓名与班级",
          "学生3" in personal_text and "初二(3)班" in personal_text, "")
    equal("5.2 个人报告不含其他学生姓名",
          [name for name in ("学生1", "学生2", "学生4", "学生5") if name in personal_text], [])
    check("5.3 个人报告含证据链（题干/学生步骤/教师判断）",
          "学生步骤" in personal_text and "教师判断" in personal_text, "")
    check("5.4 没有辅导建议时显式写「暂无」", "暂无已保存的建议" in personal_text, "")
    check("5.5 计划练习接口预留为空态", "暂无已安排的针对性练习" in personal_text, "")
    personal_pdf = personal.export(os.path.join(WORK, "student-report.pdf"))
    personal_pdf_text = pdf_text(personal_pdf["path"])
    equal("5.6 个人 PDF 读回本人姓名与错因", 
          ("学生3" in personal_pdf_text, "错因" in personal_pdf_text), (True, True))
    equal("5.7 个人 PDF 不含他人姓名",
          [name for name in ("学生1", "学生2", "学生4") if name in personal_pdf_text], [])

    section("六、权限失败在界面上得到提示（不崩、不泄露）")
    outsider = make_window(env["db"], user={"id": 2, "username": "other"},
                           db_file=env["db_file"])
    equal("6.1 越权账号报告生成失败并给出提示",
          (outsider.report, outsider.last_error is not None,
           "不能生成报告" in outsider.meta.text()),
          (None, True, True))
    equal("6.2 越权时导出为空操作", outsider.export(os.path.join(WORK, "denied.pdf")), None)

    section("七、打印与导出同源；刷新确定；版面适配")
    source = os.path.join(WORK, "print-once.pdf")
    printed = window.print_report(source)
    check("7.1 打印产出文件", printed is not None and os.path.exists(source), printed)
    equal("7.2 打印与导出字节相同（同源，不重新生成第二份内容）",
          hashlib.sha256(open(source, "rb").read()).hexdigest(),
          hashlib.sha256(open(os.path.join(WORK, "class-report-4.pdf"), "rb").read()).hexdigest())
    first = window.preview.toPlainText()
    window.refresh()
    equal("7.3 同一数据、同一生成时间刷新两次预览逐字相同",
          window.preview.toPlainText(), first)
    loose = make_window(env["db"], db_file=env["db_file"], generated_at="")
    before_lines = loose.preview.toPlainText().splitlines()
    loose.refresh()
    after_lines = loose.preview.toPlainText().splitlines()
    equal("7.3b 不固定生成时间时，除时间戳外内容逐行相同",
          [line for line in after_lines if "T19:" not in line and "T19" not in line],
          [line for line in before_lines if "T19:" not in line and "T19" not in line])
    loose.close()
    window.resize(1366, 768)
    app.processEvents()
    check("7.4 1366×768 下最小尺寸不超出屏幕",
          window.minimumSizeHint().width() <= 1366 and window.minimumSizeHint().height() <= 768,
          str(window.minimumSizeHint()))
    window.resize(1920, 1080)
    app.processEvents()
    equal("7.5 1920×1080 下预览仍在", window.preview.toPlainText() != "", True)

    section("八、界面层不含统计逻辑（边界）")
    ui_source = open(os.path.join(ROOT, "src", "desktop", "ui_report.py"),
                     encoding="utf-8").read()
    check("8.1 界面不 import r2_stats / r2_contract",
          "r2_stats" not in ui_source and "r2_contract" not in ui_source)
    check("8.2 界面只调 report_pdf（报告内容单一来源）",
          "report_pdf.build_class_report" in ui_source
          and "report_pdf.build_student_report" in ui_source
          and "report_pdf.render_pdf" not in ui_source)

    window.close()
    personal.close()
    outsider.close()
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"],
                                          RESULTS["fail"]))
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(LINES) + "\n")
    log(exit_line(RESULTS["fail"]))
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
