"""作答分析界面的测试（offscreen，无窗口弹出）。

覆盖：学生名单入口、作答列表与状态文案、缺测禁用分析、模拟模式不联网、
真实模式必须先勾选同意、服务失败提示与主动重试、审核对话框的错因编辑、
保存、确认生成错题、拒绝不生成错题，以及确认后列表状态。
"""
import csv
import io
import json
import os
import shutil
import sys
import threading
import time
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QLabel

from app.db import Database
from desktop.service import DesktopService
import desktop.ui as ui
import ai_stub_server as stub

WORK = os.path.join(ROOT, "tests", ".tmp-review-ui")
LOG_PATH = os.path.join(ROOT, "docs", "verify-review-ui.txt")
TOTAL = [0]
FAILED = [0]
LINES = []

HEAD = ["记录编号", "学生姓名", "学校编号", "题号", "题目内容", "单元", "标准解法",
        "学生步骤", "来源批次", "作答日期", "缺测", "缺测原因"]


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


def row(**kw):
    base = {"记录编号": "A1", "学生姓名": "张三", "学校编号": "XY001", "题号": "1",
            "题目内容": "3(x-2)=2x+5", "单元": "一元一次方程",
            "标准解法": "3x-6=2x+5；x=11",
            "学生步骤": "3(x-2)=2x+5\n3x-2=2x+5\nx=7", "来源批次": "9月作业",
            "作答日期": "2026-09-02", "缺测": "否", "缺测原因": ""}
    base.update(kw)
    return base


def csv_text(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=HEAD)
    writer.writeheader()
    for item in rows:
        writer.writerow({key: item.get(key, "") for key in HEAD})
    return out.getvalue()


class Stub:
    def __init__(self, log_path):
        stub.Handler.log_path = log_path
        self.server = stub.make_server(stub.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def records(self):
        if not os.path.exists(stub.Handler.log_path):
            return []
        with open(stub.Handler.log_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    from logmeta import environment_lines, exit_line

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
    cls = svc.create_class("七年级 1 班")
    cid = cls["id"]
    svc.create_student(cid, "张三", "XY001")
    svc.import_answers(cid, csv_text([row(), row(记录编号="A2", 学生步骤="", 缺测="是",
                                                 缺测原因="请假")]), "9月作业")

    log("== 一、学生名单入口 ==")
    students = ui.StudentsWindow(svc, cls)
    check("名单页有「作答分析」按钮", students.answers_button.objectName() == "openAnswerReviews",
          students.answers_button.objectName())
    check("按钮文字", students.answers_button.text() == "作答分析", students.answers_button.text())
    check("按钮可用", students.answers_button.isEnabled())

    log("")
    log("== 二、作答列表 ==")
    window = ui.AnswerListWindow(svc, cls)
    check("窗口对象名", window.objectName() == "answerListWindow", window.objectName())
    check("提示写明导入不自动分析", "CSV 导入不会自动分析" in window.hint.text(), window.hint.text())
    check("提示写明不发送身份信息", "不发送姓名" in window.hint.text(), window.hint.text())
    headers = [window.table.horizontalHeaderItem(i).text() for i in range(window.table.columnCount())]
    check("表格 7 列", headers == ["状态", "记录编号", "学生", "题号", "单元", "批次", "作答日期"], headers)
    check("加载出两条作答", wait_for(app, lambda: window.table.rowCount() == 2), window.table.rowCount())

    def row_index(key):
        for index in range(window.table.rowCount()):
            if window.table.item(index, 1).text() == key:
                return index
        return -1

    first = row_index("A1")
    missing_row = row_index("A2")
    check("找到两条记录的表格行", first >= 0 and missing_row >= 0, (first, missing_row))
    check("第一行未分析", window.table.item(first, 0).text() == "未分析", window.table.item(first, 0).text())
    check("缺测行有前缀", window.table.item(missing_row, 0).text().startswith("缺测"),
          window.table.item(missing_row, 0).text())
    window.table.selectRow(missing_row)
    app.processEvents()
    check("缺测行不能分析", not window.analyse_button.isEnabled())
    window.table.selectRow(first)
    app.processEvents()
    check("普通行可以分析", window.analyse_button.isEnabled())

    log("")
    log("== 三、模拟模式 ==")
    captured = []
    window._open_dialog = lambda review: captured.append(review)
    before_status = window.status.text()
    window.analyse()
    ok = wait_for(app, lambda: "分析完成" in window.status.text(), 12)
    check("模拟分析完成", ok, window.status.text())
    check("打开的建议只有一条", len(captured) == 1, captured)
    review = captured[0] if captured else {}
    check("模拟建议标为待核对", review.get("status") == "pending", review.get("status"))
    check("模拟建议说不确定", review.get("uncertain") is True, review.get("causes"))
    check("模拟建议没有错因", review.get("causes") == [], review.get("causes"))

    log("")
    log("== 四、真实模式必须先确认发送范围 ==")
    st = Stub(os.path.join(WORK, "stub.jsonl"))
    svc.save_ai_settings("real", st.base, "stub-vision", "sk-test")
    # T26 改写①（本节前提）
    # 原断言：配好真实通道后，老师点确认即与模型通信，建议里出现桩返回的两条错因。
    # 原依据：1.8.1「真实模式 = 老师点确认就发送」（数智伴学_Agent开发规格.md 1.8.1 章）。
    # 新断言：先把当前账号的个体 AI 开关显式置为开启（并断言真的生效），下游断言不变。
    # 改写依据：R1-07 + R1-14 裁定「默认不外发；教师账号显式开启后才真调用」。
    individual = svc.set_ai_individual(True)
    check("四.0 账号级个体 AI 开关已显式开启（否则真实通道一律 403 ai_isolation）",
          individual.get("ai_individual_allowed") is True,
          (individual.get("ai_individual_allowed"), individual.get("ai_egress_available")))
    before = len(st.records())

    original_exec = ui.AnswerConsentDialog.exec

    def cancel_exec(self):
        return QDialog.Rejected

    ui.AnswerConsentDialog.exec = cancel_exec
    try:
        window.analyse()
        check("取消后状态说明没有发送", wait_for(app, lambda: "没有发送" in window.status.text(), 8),
              window.status.text())
        check("取消后没有网络请求", len(st.records()) == before, len(st.records()) - before)
    finally:
        ui.AnswerConsentDialog.exec = original_exec

    seen = {}

    def accept_exec(self):
        for label in self.findChildren(QLabel):
            if label.objectName() == "answerConsentText":
                seen["text"] = label.text()
        seen["unchecked_ok"] = self.ok_button.isEnabled()
        self.check.setChecked(True)
        seen["checked_ok"] = self.ok_button.isEnabled()
        return QDialog.Accepted

    ui.AnswerConsentDialog.exec = accept_exec
    try:
        window.analyse()
        check("同意后才发出请求", wait_for(app, lambda: len(st.records()) == before + 1, 15),
              len(st.records()) - before)
        consent_text = seen.get("text") or ""
        check("确认框列出会发送的三项", all(k in consent_text for k in ("题目文字", "标准解法", "学生写的步骤")),
              consent_text)
        check("确认框写明不发身份字段", "不会发送学生姓名" in consent_text, consent_text)
        check("未勾选时不能继续", seen.get("unchecked_ok") is False)
        check("勾选后按钮可用", seen.get("checked_ok") is True)
        wait_for(app, lambda: len(captured) >= 2, 10)
        real_review = captured[-1]
        check("真实建议有两条错因", len(real_review.get("causes") or []) == 2, real_review.get("causes"))
        # T26 改写②（空态不再以崩溃形式出现）
        # 原断言：record = st.records()[-1]（隐含「同意后桩一定收到过请求」）
        # 原依据：1.8.1「同意即外发」，于是旧套件直接取最后一条记录。
        # 新断言：先用一条断言确认桩确实收到 ≥1 条，再取最后一条做字段检查；空态本身是断言。
        # 改写依据：R1-14 下开关关闭时 403、桩 0 条，`st.records()[-1]` 抛 IndexError
        #           （旧套件在 :224 崩溃）——崩溃不是断言失败，空态必须显式写成断言。
        check("四.1 桩至少收到一条请求（空态有明确断言，不再以崩溃形式出现）",
              len(st.records()) > before, len(st.records()) - before)
        record = st.records()[-1] if len(st.records()) > before else {}
        check("请求不含学生姓名", "张三" not in (record.get("text") or ""))
        check("请求不含学校编号", "XY001" not in (record.get("text") or ""))
    finally:
        ui.AnswerConsentDialog.exec = original_exec

    log("")
    log("== 五、服务失败与主动重试 ==")
    pending_before = svc.answer_review(cid, 1)["review"]
    consents = []

    def counting_accept(dialog):
        consents.append(dialog)
        return accept_exec(dialog)

    ui.AnswerConsentDialog.exec = counting_accept
    try:
        wait_for(app, lambda: not window.runner.busy, 20)
        stub.FAIL["mode"] = "400"
        window.table.selectRow(row_index("A1"))
        app.processEvents()
        window.analyse()
        failed = wait_for(app, lambda: "分析没有完成" in window.status.text(), 15)
        check("失败时给出可读提示", failed, window.status.text())
        check("失败时不打开审核框", len(captured) == 2, len(captured))
        kept = svc.answer_review(cid, 1)["review"]
        check("失败后原建议仍是待核对", kept["status"] == "pending", kept["status"])
        check("失败后原建议内容不变",
              [c["cause"] for c in kept["causes"]] == [c["cause"] for c in pending_before["causes"]],
              kept["causes"])
        check("失败没有写入模拟结果", all(not c["cause"].startswith("模拟") for c in kept["causes"]),
              kept["causes"])

        stub.FAIL["mode"] = "ok"
        captured.clear()
        sent = len(consents)
        idle = wait_for(app, lambda: not window.runner.busy, 10)
        check("失败回调之后任务会先收尾", idle, window.runner.busy)
        window.analyse()
        retried = wait_for(app, lambda: len(captured) == 1 or "分析完成" in window.status.text(), 20)
        check("重试后分析完成", retried, window.status.text())
        check("重试前重新确认发送范围", len(consents) == sent + 1, len(consents) - sent)
        check("重试后打开一条建议", len(captured) == 1, len(captured))
    finally:
        stub.FAIL["mode"] = "ok"
        ui.AnswerConsentDialog.exec = original_exec

    log("")
    log("== 六、审核对话框：编辑与保存 ==")
    answer = svc.answer_review(cid, 1)["answer"]
    pending = svc.answer_review(cid, 1)["review"]
    dialog = ui.AnswerReviewDialog(svc, cid, answer, pending)
    check("对话框对象名", dialog.objectName() == "answerReviewDialog", dialog.objectName())
    check("错因表格 3 列", dialog.table.columnCount() == 3, dialog.table.columnCount())
    check("表格带出两条错因", dialog.table.rowCount() == 2, dialog.table.rowCount())
    check("状态提示待核对", "还没有确认" in dialog.status.text(), dialog.status.text())
    check("确认按钮可用", dialog.confirm_button.isEnabled())
    dialog.table.item(0, 0).setText("去括号漏乘 6")
    while dialog.table.rowCount() > 1:
        dialog.table.removeRow(dialog.table.rowCount() - 1)
    dialog.note_edit.setText("老师补充")
    dialog.save_button.click()
    saved = wait_for(app, lambda: "已保存" in dialog.status.text(), 10)
    check("保存后提示仍待核对", saved, dialog.status.text())
    latest = svc.answer_review(cid, 1)["review"]
    check("保存的错因写进记录", latest["causes"][0]["cause"] == "去括号漏乘 6", latest["causes"])
    check("保存的备注写进记录", latest["note"] == "老师补充", latest["note"])

    log("")
    log("== 七、确认生成错题 ==")
    dialog.confirm_button.click()
    done = wait_for(app, lambda: "已确认" in dialog.status.text(), 15)
    check("确认后状态为已确认", done, dialog.status.text())
    check("提示里带错题编号", "错题编号" in dialog.status.text(), dialog.status.text())
    check("确认后按钮禁用", not dialog.confirm_button.isEnabled())
    latest = svc.answer_review(cid, 1)["review"]
    check("记录关联错题", bool(latest["mistake_id"]), latest)
    mistake = svc.get_mistake(latest["mistake_id"])
    check("错题已确认", mistake["status"] == "confirmed", mistake["status"])
    check("错题保留老师改过的错因", mistake["possible_causes"] == ["去括号漏乘 6"], mistake["possible_causes"])
    dialog.close()

    log("")
    log("== 八、已生成错题的作答不再重新分析 ==")
    window.reload()
    ready = wait_for(app, lambda: any(window.table.item(i, 0).text().startswith("已确认")
                                      for i in range(window.table.rowCount())), 20)
    check("刷新后能看到已确认状态", ready, [window.table.item(i, 0).text() for i in range(window.table.rowCount())])
    wait_for(app, lambda: not window.runner.busy, 10)
    confirmed_row = next((i for i in range(window.table.rowCount())
                if window.table.item(i, 0).text().startswith("已确认")), -1)
    check("列表里有一条已确认", confirmed_row >= 0, [window.table.item(i, 0).text() for i in range(window.table.rowCount())])
    if confirmed_row >= 0:
        window.table.selectRow(confirmed_row)
        app.processEvents()
    check("已确认的作答不能再点分析", not window.analyse_button.isEnabled(), window.analyse_button.isEnabled())
    check("按钮提示说明原因", "不再重新分析" in window.analyse_button.toolTip(), window.analyse_button.toolTip())
    before_confirm = len(captured)
    window.analyse()
    check("强行点分析会给提示", "已经生成过错题" in window.status.text(), window.status.text())
    check("没有发出任何请求", len(captured) == before_confirm, len(captured) - before_confirm)

    log("")
    log("== 九、拒绝不生成错题 ==")
    second = svc.analyse_answer(cid, 1, consent=True)["review"]
    dialog2 = ui.AnswerReviewDialog(svc, cid, answer, second)
    dialog2.reject_button.click()
    rejected = wait_for(app, lambda: "已拒绝" in dialog2.status.text(), 12)
    check("拒绝后状态为已拒绝", rejected, dialog2.status.text())
    check("拒绝后确认按钮禁用", not dialog2.confirm_button.isEnabled())
    dialog2.close()

    log("")
    log("== 十、列表刷新 ==")
    window.reload()
    wait_for(app, lambda: window.table.rowCount() == 2)
    shown = wait_for(app, lambda: any(window.table.item(i, 0).text().startswith("已拒绝")
                                      for i in range(window.table.rowCount())), 15)
    statuses = [window.table.item(i, 0).text() for i in range(window.table.rowCount())]
    check("列表显示已拒绝", shown and any(text.startswith("已拒绝") for text in statuses), statuses)
    check("没有审核记录的普通行仍显示未分析",
          all(text.startswith("缺测") or text != "未分析" for text in statuses), statuses)
    check("批次下拉有批次", window.batch_combo.count() == 2, window.batch_combo.count())

    log("")
    log("== 十一、任务未收尾时重复点击（偶发失败专项）==")
    declined = next((i for i in range(window.table.rowCount())
                     if window.table.item(i, 0).text().startswith("已拒绝")), -1)
    if declined >= 0:
        window.table.selectRow(declined)
        app.processEvents()
    window.runner.run(lambda: time.sleep(0.8), lambda _result: None, lambda _err: None)
    busy_seen = wait_for(app, lambda: window.runner.busy, 3)
    check("上一个任务未收尾时标为忙", busy_seen, window.runner.busy)
    before_retry = len(captured)
    window.analyse()
    app.processEvents()
    check("忙时点击不弹确认框", len(captured) == before_retry, len(captured) - before_retry)
    check("忙时点击不动列表状态", "分析完成" not in window.status.text(), window.status.text())
    check("忙时点击给出提示", "上一个操作还在处理" in window.status.text(), window.status.text())
    idle_again = wait_for(app, lambda: not window.runner.busy, 5)
    check("任务收尾后恢复空闲", idle_again, window.runner.busy)

    st.close()
    window.close()
    students.close()
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    code = 0 if FAILED[0] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
