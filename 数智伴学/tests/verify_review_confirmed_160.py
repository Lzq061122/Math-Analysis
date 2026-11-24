"""补充专项：已确认的作答仍可查看已确认的建议（offscreen，无窗口弹出）。

只读检查，不修改产品代码。对应 1.6.2 的两个行为：
1. 已生成错题的作答，「分析这条作答」不可用，直接调用也只给提示、不发请求；
2. 「查看建议」仍然可用，打开的是已确认的建议，确认/拒绝按钮不可再操作。
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

from PySide6.QtWidgets import QApplication

from app.db import Database
from desktop.service import DesktopService
import desktop.ui as ui
import ai_stub_server as stub

WORK = os.path.join(ROOT, "tests", ".tmp-review-confirmed")
LOG_PATH = os.path.join(ROOT, "docs", "verify-review-confirmed.txt")
TOTAL = [0]
FAILED = [0]
LINES = []

HEAD = ["记录编号", "学生姓名", "学校编号", "题号", "题目内容", "单元", "标准解法",
        "学生步骤", "来源批次", "作答日期", "缺测", "缺测原因"]

ROW = {"记录编号": "C1", "学生姓名": "李四", "学校编号": "XY900", "题号": "1",
       "题目内容": "3(x-2)=2x+5", "单元": "一元一次方程",
       "标准解法": "3x-6=2x+5；3x-2x=5+6；x=11",
       "学生步骤": "3x-2=2x+5;3x-2x=5-6;x=-1", "来源批次": "十月作业",
       "作答日期": "2026-10-08", "缺测": "否", "缺测原因": ""}


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


def csv_text(row):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=HEAD)
    writer.writeheader()
    writer.writerow({key: row.get(key, "") for key in HEAD})
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
    svc.create_student(cid, "李四", "XY900")
    svc.import_answers(cid, csv_text(ROW), "十月作业")

    api_stub = Stub(os.path.join(WORK, "stub.jsonl"))
    svc.save_ai_settings("custom", api_stub.base, "stub-vision", "sk-check", service_name="本地模拟服务")

    log("== 一、先完成一次分析与确认 ==")
    analysed = svc.analyse_answer(cid, 1, consent=True)
    check("分析成功", analysed.get("ok"), analysed.get("error"))
    review = analysed.get("review") or {}
    check("建议为待核对", review.get("status") == "pending", review.get("status"))
    confirmed = svc.confirm_answer_review(cid, review["id"])
    confirmed_review = confirmed if isinstance(confirmed, dict) else {}
    check("确认成功", confirmed_review.get("status") == "confirmed", confirmed_review.get("status"))
    mistake_id = confirmed_review.get("mistake_id")
    check("生成了一条错题", bool(mistake_id), mistake_id)
    request_count = len(api_stub.records())

    log("")
    log("== 二、列表里这一行已确认 ==")
    window = ui.AnswerListWindow(svc, cls)
    ready = wait_for(app, lambda: any(window.table.item(i, 0).text().startswith("已确认")
                                      for i in range(window.table.rowCount())), 15)
    check("列表显示已确认", ready, [window.table.item(i, 0).text() for i in range(window.table.rowCount())])
    wait_for(app, lambda: not window.runner.busy, 10)
    row_index = next((i for i in range(window.table.rowCount())
                      if window.table.item(i, 0).text().startswith("已确认")), -1)
    if row_index >= 0:
        window.table.selectRow(row_index)
        app.processEvents()
    check("已确认行不能再分析", not window.analyse_button.isEnabled(), window.analyse_button.isEnabled())
    check("查看建议按钮仍可用", window.review_button.isEnabled(), window.review_button.isEnabled())

    log("")
    log("== 三、打开查看建议，看到的是已确认内容 ==")
    captured = []
    original_exec = ui.AnswerReviewDialog.exec

    def capture(dialog):
        captured.append(dialog)
        return 0

    ui.AnswerReviewDialog.exec = capture
    try:
        window.open_review()
        opened = wait_for(app, lambda: bool(captured), 15)
        check("查看建议打开对话框", opened, window.status.text())
    finally:
        ui.AnswerReviewDialog.exec = original_exec

    if captured:
        dialog = captured[-1]
        check("打开的是已确认建议", (dialog.review or {}).get("status") == "confirmed",
              (dialog.review or {}).get("status"))
        check("提示写明已确认", "已确认" in dialog.status.text(), dialog.status.text())
        check("确认按钮不能再点", not dialog.confirm_button.isEnabled(), dialog.confirm_button.isEnabled())
        check("拒绝按钮不能再点", not dialog.reject_button.isEnabled(), dialog.reject_button.isEnabled())
        body = " ".join(dialog.answer_text.toPlainText().split())
        check("正文保留原始学生步骤", "3x-2x=5-6" in body and "x=-1" in body, body[:200])
        dialog.close()
    else:
        check("打开的是已确认建议", False, "没有打开的对话框")

    log("")
    log("== 四、没有多发生的请求与改动 ==")
    check("查看建议没有发出新请求", len(api_stub.records()) == request_count,
          len(api_stub.records()) - request_count)
    answer = svc.answer_review(cid, 1)["answer"]
    check("原始作答没有被改写", answer["student_steps"] == ROW["学生步骤"], answer["student_steps"])
    stored = svc.answer_review(cid, 1)["review"]
    check("审核记录仍指向该错题", stored.get("mistake_id") == mistake_id, stored.get("mistake_id"))
    check("状态仍为已确认", stored.get("status") == "confirmed", stored.get("status"))

    window.close()
    api_stub.close()

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    code = 0 if FAILED[0] == 0 else 1
    log(exit_line(code))
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
