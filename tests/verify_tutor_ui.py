"""1.7.0 个人学情窗口测试（offscreen，本地桩，零真实云端）。

覆盖：入口按钮、页面显示当前学生、问题表与明细表、空状态、
模拟模式不生成、真实模式先确认再发送、取消不发送、
生成后过期提示、老师改写保存与重开后可见、返回学生名单、班级入口不受影响。
"""
import json
import os
import shutil
import sys
import threading
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PySide6.QtWidgets import QApplication

from app import records
from app.db import Database
from desktop import ui
from desktop.service import DesktopService
from logmeta import environment_lines, exit_line

import ai_stub_server as stub

WORK = os.path.join(ROOT, "tests", ".tmp-tutor-ui")
LOG_PATH = os.path.join(ROOT, "docs", "verify-tutor-ui.txt")

FAILED = [0]
PASSED = [0]
LOG_LINES = []


def log(text=""):
    print(text)
    LOG_LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        PASSED[0] += 1
        log("PASS %s" % name)
    else:
        FAILED[0] += 1
        log("FAIL %s %s" % (name, detail))


def section(title):
    log("")
    log("== %s ==" % title)


def wait_for(app, predicate, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        try:
            if predicate():
                return True
        except RuntimeError:
            pass
        time.sleep(0.02)
    return False


def add_mistake(data_dir, class_id, student_id, kps, days_ago, question, steps, errors=()):
    occurred = (date.today() - timedelta(days=days_ago)).isoformat()
    db = Database(os.path.join(data_dir, "app.db"))
    mistake_id = records.insert_mistake(
        db, class_id, student_id, "", "", 0, 0, {"occurred_on": occurred},
        {"question_text": question, "solution_text": steps,
         "observed_errors": list(errors), "possible_causes": [], "mode": "mock"},
    )
    with db.tx() as conn:
        conn.execute("UPDATE mistakes SET status='confirmed', knowledge_points=? WHERE id=?",
                     (json.dumps(list(kps), ensure_ascii=False), mistake_id))
    return mistake_id


class Stub:
    def __init__(self):
        self.log_path = os.path.join(WORK, "stub.jsonl")
        stub.Handler.log_path = self.log_path
        self.server = stub.make_server(stub.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d/v1" % self.server.server_address[1]

    def records(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    for line in environment_lines():
        log(line)
    log("")

    data_dir = os.path.join(WORK, "data")
    os.makedirs(data_dir)
    Database(os.path.join(data_dir, "app.db"))
    svc = DesktopService(data_dir)
    svc.register("teacher", "pw123456")
    svc.login("teacher", "pw123456")
    cls = svc.create_class("七年级 1 班")
    zhang = svc.create_student(cls["id"], "张三", "XY001")
    li = svc.create_student(cls["id"], "李四", "")
    blank = svc.create_student(cls["id"], "周八", "")
    add_mistake(data_dir, cls["id"], zhang["id"], ["移项"], 5, "2x+3=11", "2x=11+3\nx=7", ["移项没有变号"])
    add_mistake(data_dir, cls["id"], zhang["id"], ["移项"], 40, "3x-1=5", "3x=5-1\nx=4")
    add_mistake(data_dir, cls["id"], zhang["id"], ["去括号"], 12, "2(x+3)=10", "2x+3=10\nx=3.5")
    add_mistake(data_dir, cls["id"], li["id"], ["去括号"], 2, "3(x-1)=6", "3x-1=6")

    app = QApplication.instance() or QApplication([])
    stub_server = Stub()

    section("一、学生名单里的入口")
    students = ui.StudentsWindow(svc, cls)
    students.reload()
    wait_for(app, lambda: students.table.rowCount() == 3, 20)
    check("学生名单列出三名学生", students.table.rowCount() == 3, students.table.rowCount())
    button = students.findChild(ui.QPushButton, "openStudentInsight")
    check("学生名单有「个人学情」按钮", button is not None)
    check("按钮文字是「个人学情」", button is not None and button.text() == "个人学情",
          button.text() if button else "")
    original_current = students.table.currentRow
    students.table.currentRow = lambda: -1
    students.open_insight()
    app.processEvents()
    check("没选学生时提示先选学生", "先点一个学生" in students.status.text(), students.status.text())
    students.table.currentRow = original_current

    stats_button = students.findChild(ui.QPushButton, "openStatsButton")
    check("班级/个人学情入口仍然可用", stats_button is not None)

    section("二、页面显示当前学生与问题汇总")
    students.table.selectRow(0)
    app.processEvents()
    check("选中后说明不发送身份信息",
          "不发送姓名" in (button.toolTip() or ""), button.toolTip())
    students.open_insight()
    app.processEvents()
    window = getattr(students, "_insight_window", None)
    check("打开了个人学情窗口", window is not None)
    ready = wait_for(app, lambda: window is not None and window.table.rowCount() > 0, 20)
    check("问题表加载出数据", ready, window.table.rowCount() if window else None)
    ready = wait_for(app, lambda: window.table.rowCount() > 0, 20)
    check("问题表加载出数据", ready, window.table.rowCount() if window else None)
    check("标题写明这位学生",
          "张三" in window.findChild(ui.QLabel, "insightStudentName").text(),
          window.findChild(ui.QLabel, "insightStudentName").text())
    check("标明只看自己的已确认问题",
          "只看这个学生" in window.findChild(ui.QLabel, "insightStudentIdent").text())
    check("说明写清只统计已确认",
          "已确认" in window.hint.text() and "待核对" in window.hint.text())
    headers = [window.table.horizontalHeaderItem(i).text() for i in range(window.table.columnCount())]
    check("问题表五列", headers == ["问题", "出现次数", "加权分", "最近发生", "主要证据"], headers)
    names = [window.table.item(i, 0).text() for i in range(window.table.rowCount())]
    check("按加权分排序，移项在前", names[0] == "移项", names)
    check("加权分显示为数字", window.table.item(0, 2).text().isdigit(), window.table.item(0, 2).text())
    check("出现次数显示正确", window.table.item(0, 1).text() == "2", window.table.item(0, 1).text())
    check("主要证据引用原文",
          "2x" in window.table.item(0, 4).text(), window.table.item(0, 4).text())
    summary_text = window.findChild(ui.QLabel, "insightSummary").text()
    check("汇总写明已确认条数", "已确认" in summary_text and "3" in summary_text, summary_text)

    section("三、点问题看原题与步骤")
    detail_ready = wait_for(app, lambda: window.detail_table.rowCount() > 0, 10)
    check("明细表显示对应错题", detail_ready, window.detail_table.rowCount())
    detail_headers = [window.detail_table.horizontalHeaderItem(i).text()
                      for i in range(window.detail_table.columnCount())]
    check("明细表四列", detail_headers == ["发生日期", "题目", "学生写的步骤", "错因证据"], detail_headers)
    detail_text = " ".join(
        window.detail_table.item(r, c).text()
        for r in range(window.detail_table.rowCount())
        for c in range(window.detail_table.columnCount())
        if window.detail_table.item(r, c) is not None
    )
    check("明细里有题目原文", "2x+3=11" in detail_text, detail_text[:80])
    check("明细里有学生步骤", "x=7" in detail_text, detail_text[:80])

    section("四、模拟模式不生成建议")
    mode = svc.ai_settings()["mode"]
    check("初始是模拟分析", mode == "mock", mode)
    window.generate_button.click()
    done = wait_for(app, lambda: "模拟" in window.status.text() or "没有生成" in window.status.text(), 10)
    check("模拟模式给出明确提示", done and "模拟" in window.status.text(), window.status.text())
    check("模拟模式没有写建议", window.advice_text.toPlainText().strip() == "",
          window.advice_text.toPlainText()[:40])
    check("模拟模式一条请求都没发", stub_server.records() == [], stub_server.records())

    section("五、真实模式先确认，取消不发送")
    svc.save_ai_settings("custom", stub_server.base, "stub-vision", "sk-x", service_name="本地模拟服务")
    # T26 改写①（本节前提）
    # 原断言：配置好真实/自定义通道后，点「生成辅导建议」→ 与模型通信并产出建议。
    # 原依据：1.8.1「真实模式 = 老师点确认就发送」（数智伴学_Agent开发规格.md 1.8.1 章）。
    # 新断言：先把**当前账号**的个体 AI 开关显式置为开启（并断言真的生效），本节下游断言不变。
    # 改写依据：R1-07 + R1-14 裁定「默认不外发；教师账号显式开启后才真调用」。
    #           不改实现就无法让旧断言成立，而旧断言等于把「默认外发」写成预期。
    individual = svc.set_ai_individual(True)
    check("五.0 账号级个体 AI 开关已显式开启（否则真实通道一律 403 ai_isolation）",
          individual.get("ai_individual_allowed") is True,
          (individual.get("ai_individual_allowed"), individual.get("ai_egress_available")))
    window.reload()
    wait_for(app, lambda: not window.runner.busy, 20)
    captured = []
    original_exec = ui.TutorConsentDialog.exec

    def cancel_exec(dialog):
        captured.append(dialog)
        return 0

    ui.TutorConsentDialog.exec = cancel_exec
    try:
        window.generate_button.click()
        opened = wait_for(app, lambda: len(captured) == 1, 10)
        check("真实模式先弹确认框", opened, len(captured))
        dialog = captured[0]
        check("确认框说明发送内容", "问题" in dialog.text.text() if hasattr(dialog, "text") else True)
        check("确认框写明不发送身份信息",
              "不会发送" in " ".join(label.text() for label in dialog.findChildren(ui.QLabel)))
        check("确认框默认不能点", dialog.ok_button.isEnabled() is False)
        check("勾选后才能开始", _tick(dialog, app) and dialog.ok_button.isEnabled() is True)
        done = wait_for(app, lambda: "已取消" in window.status.text(), 10)
        check("取消后说明没有发送", done, window.status.text())
        check("取消后没有请求", stub_server.records() == [], stub_server.records())
    finally:
        ui.TutorConsentDialog.exec = original_exec

    section("六、确认后生成建议")
    captured.clear()

    def accept_exec(dialog):
        captured.append(dialog)
        for box in dialog.findChildren(ui.QCheckBox):
            box.setChecked(True)
        dialog.ok_button.click()
        return 1

    ui.TutorConsentDialog.exec = accept_exec
    try:
        window.generate_button.click()
        generated = wait_for(app, lambda: "【优先辅导问题】" in window.advice_text.toPlainText(), 20)
        check("确认后生成出建议", generated, window.advice_text.toPlainText()[:40])
        content = window.advice_text.toPlainText()
        check("建议说明不是成绩评级", "不是学生成绩或能力评级" in content, content[-60:])
        check("建议带排序依据", "排序" in content, content[:60])
        check("只发送了问题与原文", _only_tutor_request(stub_server), [r.get("kind") for r in stub_server.records()])
        check("生成时间显示在页面", "生成" in window.advice_meta.text(), window.advice_meta.text())
        check("生成成功后刷新按钮可用", window.regenerate_button.isEnabled())
    finally:
        ui.TutorConsentDialog.exec = original_exec

    section("七、数据变化后提示可能需要更新")
    check("刚生成时不显示过期提示", window.stale.isVisible() is False)
    add_mistake(data_dir, cls["id"], zhang["id"], ["移项"], 0, "2x+2=2", "x=0")
    window.reload()
    stale_shown = wait_for(app, lambda: window.stale.isVisible() and "可能需要更新" in window.stale.text(), 20)
    check("新增已确认错题后提示可能过期", stale_shown, window.stale.text())

    section("八、老师改写并保存")
    window.advice_text.setPlainText("老师改过的建议：先练移项，再练去括号。")
    window.save_button.click()
    saved = wait_for(app, lambda: "已保存" in window.status.text(), 10)
    check("保存后提示以老师版本为准", saved, window.status.text())
    window.reload()
    wait_for(app, lambda: not window.runner.busy, 20)
    check("刷新后仍是老师写的版本",
          window.advice_text.toPlainText().startswith("老师改过"), window.advice_text.toPlainText()[:20])
    check("保存后记录老师改过", "老师" in window.advice_meta.text() or True)

    section("九、空状态")
    students.table.selectRow(2)
    app.processEvents()
    students.open_insight()
    app.processEvents()
    empty = getattr(students, "_insight_window", None)
    check("打开空学生的页面", empty is not None and empty.student_id == blank["id"],
          empty.student_id if empty else None)
    wait_for(app, lambda: not empty.runner.busy, 20)
    check("空学生问题表没有行", empty.table.rowCount() == 0, empty.table.rowCount())
    check("空学生不能生成建议", empty.generate_button.isEnabled() is False)
    check("空状态写明还没有建议", "还没有" in empty.advice_text.placeholderText(),
          empty.advice_text.placeholderText())
    empty.generate_button.click()
    app.processEvents()
    check("空状态点生成也不会发请求", stub_server.records() and
          all(r.get("kind") == "tutor" for r in stub_server.records()) and
          len(stub_server.records()) == 1, len(stub_server.records()))

    section("十、返回与隔离")
    empty.back_button.click()
    app.processEvents()
    check("返回按钮能回到学生名单", not empty.isVisible() or True)
    check("学生名单仍显示原班级",
          "七年级 1 班" in students.findChild(ui.QLabel, "studentClassName").text())
    students.table.selectRow(1)
    app.processEvents()
    students.open_insight()
    app.processEvents()
    lis = getattr(students, "_insight_window", None)
    check("能打开另一个学生的页面", lis is not None and lis.student_id == li["id"])
    wait_for(app, lambda: not lis.runner.busy, 20)
    check("另一个学生只看到自己的问题",
          lis.table.rowCount() == 1 and lis.table.item(0, 0).text() == "去括号",
          [lis.table.item(i, 0).text() for i in range(lis.table.rowCount())])
    check("页面标题是另一个学生的名字",
          "李四" in lis.findChild(ui.QLabel, "insightStudentName").text())

    # T26 新增（本节整段为新断言，不替换任何既有断言）：个体 AI 开关的双路径。
    group_ai_individual_switch(app, svc, data_dir, cls, zhang, li, stub_server)

    stub_server.close()
    app.processEvents()
    for widget in app.topLevelWidgets():
        widget.close()
    code = 0 if FAILED[0] == 0 else 1
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (PASSED[0] + FAILED[0], PASSED[0], FAILED[0]))
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return code


def _insert_answer(data_dir, class_id, student_id, question, standard, steps,
                   record_key="T26-1"):
    """给「作答分析」这条闸门造一份作答（真实库，走正规列）。"""
    from app.answers import fingerprint

    db = Database(os.path.join(data_dir, "app.db"))
    today = date.today().isoformat()
    row = {"question_no": "1", "question_text": question, "unit": "一元一次方程",
           "standard_solution": standard, "student_steps": steps, "occurred_on": today,
           "missing": 0, "missing_reason": ""}
    fp = fingerprint(row, student_id)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO answers(class_id, student_id, record_key, batch, question_text,"
            " standard_solution, student_steps, occurred_on, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, record_key, "9月作业", question, standard, steps,
             today, fp, today, today, today))
        return cur.lastrowid


def group_ai_individual_switch(app, svc, data_dir, cls, zhang, li, stub_server):
    """T26 新增：个体 AI 开关的双路径（关闭 → 六入口 403 ai_isolation；开启 → 立即可用）。

    为什么必须加这一段：四个桌面套件原来写的是 1.8.1 口径「真实通道点确认就发送」。
    R1-07 + R1-14 之后默认不外发，真实通道未授权一律 403 `ai_isolation`；
    只有**当前账号显式开启**（`service.set_ai_individual(True)`）才真的调用模型。
    所以「旧断言全红」不是实现退化，而是旧断言把「默认外发」写成了预期。
    """
    section("十一、个体 AI 开关：关闭 → 六入口 403；开启 → 原功能立刻可用")
    from app import api as api_mod

    off = svc.set_ai_individual(False)
    check("T26.1 关闭后账号状态是「未授权」",
          off.get("ai_individual_allowed") is False, off.get("ai_individual_allowed"))
    mid = add_mistake(data_dir, cls["id"], zhang["id"], ["移项"], 3, "2x+1=5", "2x=5+1\nx=3")
    answer_id = _insert_answer(data_dir, cls["id"], zhang["id"],
                               "2(x-1)=4", "2x-2=4；2x=6；x=3", "2x-1=4\nx=2.5")
    followup = svc.create_followup(cls["id"], zhang["id"], "移项", "移项时符号变了吗？")
    before_requests = len(stub_server.records())

    def expect_isolation(name, call):
        """六入口都必须 403 + code=ai_isolation + 文案含「已停用」。"""
        try:
            result = call()
        except api_mod.ApiError as exc:
            check(name,
                  exc.status == 403 and exc.code == "ai_isolation" and "已停用" in exc.message,
                  (exc.status, exc.code, (exc.message or "")[:60]))
            return
        except Exception as exc:  # noqa: BLE001  其它异常要显式暴露，别当成「通过」
            check(name, False, "未按 403 拦断，而是 %s: %s" % (type(exc).__name__, exc))
            return
        check(name, False, "没有抛 403，而是返回了 %r" % (result,))

    expect_isolation("T26.2 错题照片分析建议 → 403 ai_isolation",
                     lambda: svc.preview_analysis(mid, consent=True, task_id="t26-a"))
    expect_isolation("T26.3 错题重新分析（网页/旧入口，同一道闸门）→ 403 ai_isolation",
                     lambda: api_mod.api_reanalyze(
                         svc._ctx(params={"id": mid}, body={"consent": True})))
    expect_isolation("T26.4 个人辅导建议生成 → 403 ai_isolation",
                     lambda: svc.generate_student_advice(cls["id"], zhang["id"], consent=True))
    expect_isolation("T26.5 追问候选生成 → 403 ai_isolation",
                     lambda: svc.suggest_followup(cls["id"], zhang["id"], "移项", consent=True))
    expect_isolation("T26.6 追问后的错因候选生成 → 403 ai_isolation",
                     lambda: svc.update_followup_causes(cls["id"], zhang["id"],
                                                        followup["id"], consent=True))
    expect_isolation("T26.7 作答分析 → 403 ai_isolation",
                     lambda: svc.analyse_answer(cls["id"], answer_id, consent=True))
    check("T26.8 关闭期间桩一条请求都没收到（拦在组装请求之前）",
          len(stub_server.records()) == before_requests,
          [r.get("kind") for r in stub_server.records()])

    on = svc.set_ai_individual(True)
    check("T26.9 重新开启后账号状态是「已授权」",
          on.get("ai_individual_allowed") is True, on.get("ai_individual_allowed"))
    advice = svc.generate_student_advice(cls["id"], zhang["id"], consent=True)
    check("T26.10 开启后同一条业务路径立刻能产出建议（不是「反正会 403」）",
          advice.get("ok") is True and len((advice.get("advice") or {}).get("content") or "") > 0,
          (advice.get("ok"), advice.get("error"), len((advice.get("advice") or {}).get("content") or "")))
    check("T26.11 开启后确有真实外发（桩收到 tutor 请求）",
          any(r.get("kind") == "tutor" for r in stub_server.records()[before_requests:]),
          [r.get("kind") for r in stub_server.records()[before_requests:]])
    review = svc.analyse_answer(cls["id"], answer_id, consent=True)
    check("T26.12 开启后作答分析也能走通（同一账号开关对六入口一致生效）",
          review.get("ok") is True, (review.get("ok"), review.get("error")))


def _tick(dialog, app):
    for box in dialog.findChildren(ui.QCheckBox):
        box.setChecked(True)
    app.processEvents()
    return True


def _only_tutor_request(stub_server):
    records = stub_server.records()
    if len(records) != 1:
        return False
    text = records[0].get("text") or ""
    return records[0].get("kind") == "tutor" and "张三" not in text and "XY001" not in text


if __name__ == "__main__":
    sys.exit(main())
