"""作答 CSV 导入的界面层测试（offscreen，无窗口弹出）。

覆盖：导入窗口的提示文案、预览表格、按钮启用条件、同名学生改为手动选择、
确认导入、问题明细导出、模板保存、名单页学校编号列与新增学生编号框。

运行：$env:QT_QPA_PLATFORM="offscreen"; .venv\\Scripts\\python.exe tests\\verify_answers_ui.py
"""
import csv
import io
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox, QTableWidgetItem

from app import answers as A
from app.db import Database
from desktop.service import DesktopService
import desktop.ui as ui

WORK = os.path.join(ROOT, "tests", ".tmp-answers-ui")
LOG_PATH = os.path.join(ROOT, "docs", "verify-answers-ui.txt")
TOTAL = [0]
FAILED = [0]
LINES = []


def log(text=""):
    LINES.append(text)
    print(text)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if ok:
        log("PASS  %s" % name)
    else:
        FAILED[0] += 1
        log("FAIL  %s%s" % (name, ("  -> " + str(detail)) if detail else ""))


def wait_for(app, predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if predicate():
            app.processEvents()
            return True
        time.sleep(0.02)
    return False


def wait(app, window, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if not window.runner.busy:
            app.processEvents()
            return True
        time.sleep(0.02)
    return False


HEAD = [label for label, _ in A.HEADERS]


def csv_text(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=HEAD)
    writer.writeheader()
    for item in rows:
        writer.writerow({key: item.get(key, "") for key in HEAD})
    return out.getvalue()


def row(**kw):
    base = {
        "记录编号": "A1", "学生姓名": "张三", "学校编号": "", "题号": "1",
        "题目内容": "3(x-2)=2x+5", "单元": "一元一次方程", "标准解法": "x=11",
        "学生步骤": "3x-6=2x+5", "来源批次": "9月作业", "作答日期": "2026-09-02",
        "缺测": "否", "缺测原因": "",
    }
    base.update(kw)
    return base


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    from logmeta import environment_lines

    for line in environment_lines():
        log(line)
    log("")

    app = QApplication.instance() or QApplication([])
    data_dir = os.path.join(WORK, "data")
    os.makedirs(data_dir)
    Database(os.path.join(data_dir, "app.db"))
    svc = DesktopService(data_dir)
    svc.register("teacher", "pw123456")
    svc.login("teacher", "pw123456")
    cls = svc.create_class("初三 1 班")
    cid = cls["id"]
    svc.create_student(cid, "张三", "XY001")
    svc.create_student(cid, "同名", "N1")
    svc.create_student(cid, "同名", "N2")

    log("== 一、窗口与文案 ==")
    window = ui.AnswerImportWindow(svc, cls)
    check("窗口对象名", window.objectName() == "answerImportWindow", window.objectName())
    check("提示写明不调用 AI", "不会调用 AI" in window.hint.text(), window.hint.text())
    check("提示写明不发送姓名照片", "不会发送学生姓名" in window.hint.text(), window.hint.text())
    check("表格 6 列", window.table.columnCount() == 6, window.table.columnCount())
    headers = [window.table.horizontalHeaderItem(i).text() for i in range(window.table.columnCount())]
    check("表格表头", headers == ["行", "状态", "记录编号", "学生", "题号", "说明"], headers)
    check("未选文件时导入按钮禁用", not window.import_button.isEnabled())
    check("未选文件时导出按钮禁用", not window.export_button.isEnabled())

    log("")
    log("== 二、预览与按钮状态 ==")
    window._text = csv_text([row(), row(记录编号="A2", 学生姓名="查无此人")])
    window._batch = "9月作业"
    window.preview()
    check("预览线程结束", wait_for(app, lambda: window.table.rowCount() == 2))
    check("预览显示两行", window.table.rowCount() == 2, window.table.rowCount())
    check("可导入行显示绿字", window.table.item(0, 1).text() == "可导入", window.table.item(0, 1).text())
    check("可导入行颜色为绿", window.table.item(0, 1).foreground().color().name() == "#1b7f3b",
          window.table.item(0, 1).foreground().color().name())
    check("问题行显示需处理", window.table.item(1, 1).text() == "需处理", window.table.item(1, 1).text())
    check("问题行说明非空", window.table.item(1, 5).text() != "", window.table.item(1, 5).text())
    check("状态栏计数", "可导入 1" in window.status.text() and "需处理 1" in window.status.text(),
          window.status.text())
    check("有可导入行时按钮可用", window.import_button.isEnabled())

    window._text = csv_text([row(学生姓名="查无此人")])
    window.preview()
    wait_for(app, lambda: window.table.rowCount() == 1)
    check("只有问题行时导入禁用", not window.import_button.isEnabled(), window.status.text())

    log("")
    log("== 三、同名学生手动选择 ==")
    window._text = csv_text([row(学生姓名="同名")])
    window.preview()
    wait_for(app, lambda: window.table.rowCount() == 1 and "待选择" in window.table.item(0, 3).text())
    check("同名行显示待选择", "待选择" in window.table.item(0, 3).text(), window.table.item(0, 3).text())
    check("同名行导入禁用", not window.import_button.isEnabled())
    window.table.selectRow(0)
    app.processEvents()
    check("选中待选行后可用「选择学生」", window.pick_button.isEnabled())

    picked = {}

    def fake_get_item(parent, title, label, labels, current, editable):
        picked["labels"] = labels
        return labels[0], True

    original_get_item = QInputDialog.getItem
    QInputDialog.getItem = staticmethod(fake_get_item)
    try:
        window.pick_student()
        check("选择学生对话框列出候选", len(picked.get("labels", [])) == 2, picked.get("labels"))
        wait_for(app, lambda: window.import_button.isEnabled())
        check("选择后变为可导入", window.import_button.isEnabled(), window.status.text())
        check("选择记录已保存在窗口状态", bool(window._selections), window._selections)
        check("选择后学生列显示姓名", window.table.item(0, 3).text() == "同名", window.table.item(0, 3).text())
    finally:
        QInputDialog.getItem = original_get_item

    log("")
    log("== 四、确认导入 ==")
    asked = {}

    def fake_question(parent, title, text, buttons, default):
        asked["text"] = text
        return QMessageBox.Yes

    original_question = QMessageBox.question
    QMessageBox.question = staticmethod(fake_question)
    try:
        window.confirm_import()
        check("导入前弹确认框", "确认" in asked.get("text", "") or "导入" in asked.get("text", ""), asked.get("text"))
        check("导入线程结束", wait_for(app, lambda: "导入完成" in window.status.text(), 12))
        check("状态栏显示导入完成", "导入完成" in window.status.text(), window.status.text())
        check("结果区显示批次", "9月作业" in window.report.toPlainText(), window.report.toPlainText()[:60])
        check("导入后数据库有 1 条", svc.answer_batches(cid)["total"] == 1, svc.answer_batches(cid)["total"])
        check("重复行改显示重复", window.table.item(0, 1).text() == "重复", window.table.item(0, 1).text())
        check("只剩重复行时导入禁用", not window.import_button.isEnabled())
    finally:
        QMessageBox.question = original_question

    log("")
    log("== 五、冲突与覆盖 ==")
    changed = csv_text([row(学生步骤="3x-2x=5+6")])
    window._text = changed
    window._selections = {}
    window.preview()
    wait_for(app, lambda: window.table.rowCount() == 1 and window.table.item(0, 1).text() == "冲突")
    check("同编号改内容显示冲突", window.table.item(0, 1).text() == "冲突", window.table.item(0, 1).text())
    check("未勾选覆盖时导入禁用", not window.import_button.isEnabled())
    window.overwrite_check.setChecked(True)
    window._sync_buttons()
    check("勾选覆盖后导入可用", window.import_button.isEnabled())
    QMessageBox.question = staticmethod(fake_question)
    try:
        window.confirm_import()
        wait_for(app, lambda: "覆盖 1" in window.status.text(), 12)
        check("覆盖后状态显示覆盖 1", "覆盖 1" in window.status.text(), window.status.text())
        shown = wait_for(app, lambda: window.table.rowCount() == 1
                         and window.table.item(0, 1).text() == "已覆盖", 12)
        check("覆盖成功后表格状态显示“已覆盖”", shown,
              window.table.item(0, 1).text() if window.table.rowCount() else "无行")
        check("覆盖成功后不显示英文 overwritten", "overwritten" not in window.table.item(0, 1).text(),
              window.table.item(0, 1).text() if window.table.rowCount() else "无行")
    finally:
        QMessageBox.question = original_question

    log("")
    log("== 六、导出问题明细 ==")
    window._text = csv_text([row(记录编号="E1", 学生姓名="查无此人")])
    window._selections = {}
    window.preview()
    ready = wait_for(app, lambda: window.table.rowCount() == 1 and window.table.item(0, 1).text() == "需处理")
    check("问题行预览就绪", ready, (window.table.rowCount(), window.status.text(),
                                    [r.get("status") for r in window._rows]))
    target = os.path.join(WORK, "problems.csv")
    original_save = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (target, "CSV 文件 (*.csv)"))
    try:
        window.export_problems()
        check("问题明细已写出", os.path.isfile(target), (target, window.status.text()))
        with open(target, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        check("问题明细表头", rows[0] == ["行号", "状态", "记录编号", "学生姓名", "题号", "原因"], rows[0])
        check("问题明细含问题行", len(rows) == 2 and rows[1][1] == "需处理", rows)
    finally:
        QFileDialog.getSaveFileName = original_save

    log("")
    log("== 七、模板保存 ==")
    template_path = os.path.join(WORK, "template.csv")
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (template_path, "CSV 文件 (*.csv)"))
    try:
        window.save_template()
        with open(template_path, "rb") as fh:
            raw = fh.read()
        check("模板写成 UTF-8 BOM", raw.startswith(b"\xef\xbb\xbf"), raw[:6])
        check("模板表头为中文", "记录编号" in raw.decode("utf-8-sig").splitlines()[0], raw[:20])
        check("状态栏提示模板路径", "模板已保存" in window.status.text(), window.status.text())
    finally:
        QFileDialog.getSaveFileName = original_save

    log("")
    log("== 八、名单页与新增学生 ==")
    students_window = ui.StudentsWindow(svc, cls)
    check("名单表 5 列", students_window.table.columnCount() == 5, students_window.table.columnCount())
    titles = [students_window.table.horizontalHeaderItem(i).text() for i in range(5)]
    check("名单表含学校编号", titles == ["序号", "内部编号", "姓名", "学校编号", "状态"], titles)
    check("工具栏有导入按钮", students_window.import_button.text() == "导入作答 CSV",
          students_window.import_button.text())
    students_window._show_students(svc.list_students(cid))
    codes = [students_window.table.item(i, 3).text() for i in range(students_window.table.rowCount())]
    check("名单显示学校编号", "XY001" in codes, codes)

    dialog = ui.StudentDialog(svc, cid, None)
    check("新增学生有学校编号输入框", dialog.code_input.objectName() == "studentCode",
          dialog.code_input.objectName())
    dialog.name_input.setText("新同学")
    dialog.code_input.setText("ZZ9")
    dialog.submit()
    wait_for(app, lambda: dialog.saved is not None)
    check("新增学生保存学校编号", bool(dialog.saved) and dialog.saved.get("code") == "ZZ9",
          dialog.saved)

    emitted = {"n": 0}
    window.closed.connect(lambda: emitted.__setitem__("n", emitted["n"] + 1))
    window.close()
    app.processEvents()
    check("关闭导入窗口会通知名单刷新", emitted["n"] == 1, emitted)

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    code = 1 if FAILED[0] else 0
    log("退出码：%d" % code)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
