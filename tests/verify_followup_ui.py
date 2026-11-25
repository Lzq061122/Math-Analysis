"""1.8.0 教师短追问窗口测试（offscreen，本地桩，零真实云端）。

覆盖：两个入口、页面显示当前学生与问题、依据原文与隐私、手写追问、
候选追问与确认框、老师确认才记录、回答保存、按回答更新错因候选、
拒绝与接受、AI 失败不覆盖、重开后记录仍在、错题详情入口。
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

WORK = os.path.join(ROOT, "tests", ".tmp-followup-ui")
LOG_PATH = os.path.join(ROOT, "docs", "verify-followup-ui.txt")

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


def add_mistake(data_dir, class_id, student_id, kps, days_ago, question, steps,
                causes=("去括号时漏乘",)):
    occurred = (date.today() - timedelta(days=days_ago)).isoformat()
    db = Database(os.path.join(data_dir, "app.db"))
    mistake_id = records.insert_mistake(
        db, class_id, student_id, "", "", 0, 0, {"occurred_on": occurred},
        {"question_text": question, "solution_text": steps,
         "observed_errors": ["移项没有变号"], "possible_causes": list(causes), "mode": "mock"},
    )
    with db.tx() as conn:
        conn.execute("UPDATE mistakes SET status='confirmed', knowledge_points=? WHERE id=?",
                     (json.dumps(list(kps), ensure_ascii=False), mistake_id))
    return mistake_id


def causes_of(data_dir, mistake_id):
    conn = __import__("sqlite3").connect(os.path.join(data_dir, "app.db"))
    try:
        raw = conn.execute("SELECT possible_causes FROM mistakes WHERE id=?",
                           (mistake_id,)).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw or "[]")


def link_answer(data_dir, class_id, student_id, mistake_id, question, steps,
                record_key="R-1"):
    """造一条作答与审核记录，让追问能取到学生步骤原文。"""
    db = Database(os.path.join(data_dir, "app.db"))
    today = date.today().isoformat()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO answers (class_id, student_id, record_key, batch, question_text,"
            " standard_solution, student_steps, occurred_on, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, record_key, "第一批", question, "x=2", steps,
             today, "fp-" + record_key, today, today, today),
        )
        answer_id = cur.lastrowid
        conn.execute(
            "INSERT INTO answer_reviews (class_id, student_id, answer_id, status, provider, model,"
            " causes, note, uncertain, raw, answer_fingerprint, mistake_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, answer_id, "confirmed", "本地模拟服务", "stub-vision",
             json.dumps([{"cause": "去括号时漏乘", "evidence": steps, "reason": "丢了一项"}],
                        ensure_ascii=False),
             "", 0, "{}", "fp-" + record_key, mistake_id, today, today),
        )


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

    def clear(self):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def tick(dialog, app):
    for box in dialog.findChildren(ui.QCheckBox):
        box.setChecked(True)
    app.processEvents()


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
    blank = svc.create_student(cls["id"], "周八", "")
    mistake_id = add_mistake(data_dir, cls["id"], zhang["id"], ["移项"], 5,
                             "解方程 2x+6=10", "2x+6=10\n2x=10+6\nx=8")
    link_answer(data_dir, cls["id"], zhang["id"], mistake_id, "解方程 2x+6=10",
                "2x+6=10\n2x=10+6\nx=8")
    add_mistake(data_dir, cls["id"], zhang["id"], ["去括号"], 12,
                "解方程 2(x+3)=10", "2x+3=10\nx=3.5")

    app = QApplication.instance() or QApplication([])
    stub_server = Stub()

    section("一、从个人学情进入追问")
    students = ui.StudentsWindow(svc, cls)
    students.reload()
    wait_for(app, lambda: students.table.rowCount() == 2, 20)
    students.table.selectRow(0)
    app.processEvents()
    students.open_insight()
    app.processEvents()
    insight = getattr(students, "_insight_window", None)
    check("打开了个人学情窗口", insight is not None)
    wait_for(app, lambda: insight is not None and insight.table.rowCount() > 0, 20)
    follow_btn = insight.findChild(ui.QPushButton, "insightFollowup")
    check("问题汇总里有「追问这个问题」按钮", follow_btn is not None)
    check("按钮文字是「追问这个问题」",
          follow_btn is not None and follow_btn.text() == "追问这个问题",
          follow_btn.text() if follow_btn else "")
    armed = wait_for(app, lambda: follow_btn.isEnabled(), 20)
    check("有选中问题时按钮可用", armed)

    target_row = 0
    for row in range(insight.table.rowCount()):
        if insight.table.item(row, 0).text() == "移项":
            target_row = row
            break
    insight.table.selectRow(target_row)
    app.processEvents()
    follow_btn.click()
    app.processEvents()
    window = getattr(insight, "_followup_window", None)
    check("打开了追问窗口", window is not None)
    ready = wait_for(app, lambda: window is not None and window.evidence.toPlainText() != "", 20)
    check("依据框加载出内容", ready, window.evidence.toPlainText()[:40] if window else None)

    section("二、页面显示当前学生、问题与原文依据")
    check("标题写明学生和问题",
          "张三" in window.findChild(ui.QLabel, "followupStudentName").text(),
          window.findChild(ui.QLabel, "followupStudentName").text())
    check("标明当前问题",
          "移项" in window.findChild(ui.QLabel, "followupProblemName").text(),
          window.findChild(ui.QLabel, "followupProblemName").text())
    evidence = window.evidence.toPlainText()
    check("依据里有题目原文", "2x+6=10" in evidence, evidence[:60])
    check("依据里有学生过程", "2x=10+6" in evidence, evidence[:80])
    check("依据里有已确认错因", "去括号时漏乘" in evidence, evidence[:120])
    check("说明写明不发送身份信息",
          "不发送姓名" in window.hint.text(), window.hint.text()[:60])
    check("说明写明 AI 只是候选",
          "候选" in window.hint.text(), window.hint.text()[-40:])
    headers = [window.candidates.horizontalHeaderItem(i).text()
               for i in range(window.candidates.columnCount())]
    check("候选表两列", headers == ["追问", "依据（原文）"], headers)
    headers = [window.history.horizontalHeaderItem(i).text()
               for i in range(window.history.columnCount())]
    check("历史表四列", headers == ["提问时间", "追问", "学生回答", "状态"], headers)
    headers = [window.compare.horizontalHeaderItem(i).text()
               for i in range(window.compare.columnCount())]
    check("对比表两列", headers == ["更新前（已确认的错因）", "更新后（候选，待核对）"], headers)
    check("一开始没有追问记录", window.history.rowCount() == 0, window.history.rowCount())

    section("三、老师自己写一句追问并记录")
    window.question.setText("   ")
    app.processEvents()
    check("空追问时按钮不可点", not window.use_button.isEnabled())
    window.use_button.click()
    app.processEvents()
    check("空追问没有记录", window.history.rowCount() == 0, window.history.rowCount())

    window.question.setText("6 移到右边时，你写成 +6 是怎么想的？")
    window.use_button.click()
    saved = wait_for(app, lambda: window.history.rowCount() == 1, 10)
    check("点使用后记下一条提问", saved, window.history.rowCount())
    check("记录显示追问内容",
          "怎么想" in window.history.item(0, 1).text(), window.history.item(0, 1).text())
    check("记录初始状态是已提问",
          "已提问" in window.history.item(0, 3).text(), window.history.item(0, 3).text())
    check("记录来源是老师自己写的",
          "老师" in window.source_label.text(), window.source_label.text())

    section("四、模拟模式不生成候选")
    check("初始是模拟分析", svc.ai_settings()["mode"] == "mock", svc.ai_settings()["mode"])
    armed = wait_for(app, lambda: not window.runner.busy and window.suggest_button.isEnabled(), 20)
    window.suggest()
    done = "模拟" in window.status.text()
    check("模拟模式给出明确提示", done and armed, window.status.text())
    check("模拟模式没有候选", window.candidates.rowCount() == 0, window.candidates.rowCount())
    check("模拟模式一条请求都没发", stub_server.records() == [], stub_server.records())

    section("五、真实模式先确认，取消不发送")
    svc.save_ai_settings("custom", stub_server.base, "stub-vision", "sk-x",
                         service_name="本地模拟服务")
    # T26 改写①（本节前提）
    # 原断言：配好真实/自定义通道后，点「生成候选追问」→ 与模型通信并拿到候选。
    # 原依据：1.8.1「真实模式 = 老师点确认就发送」（数智伴学_Agent开发规格.md 1.8.1 章）。
    # 新断言：先把当前账号的个体 AI 开关显式置为开启（并断言真的生效），下游断言不变。
    # 改写依据：R1-07 + R1-14 裁定「默认不外发；教师账号显式开启后才真调用」。
    individual = svc.set_ai_individual(True)
    check("五.0 账号级个体 AI 开关已显式开启（否则真实通道一律 403 ai_isolation）",
          individual.get("ai_individual_allowed") is True,
          (individual.get("ai_individual_allowed"), individual.get("ai_egress_available")))
    window.reload()
    wait_for(app, lambda: not window.runner.busy, 20)
    captured = []
    original_exec = ui.FollowupConsentDialog.exec

    def cancel_exec(dialog):
        captured.append(dialog)
        return 0

    ui.FollowupConsentDialog.exec = cancel_exec
    try:
        wait_for(app, lambda: not window.runner.busy and window.suggest_button.isEnabled(), 20)
        window.suggest()
        opened = wait_for(app, lambda: len(captured) == 1, 10)
        check("真实模式先弹确认框", opened, len(captured))
        dialog = captured[0]
        check("确认框写明不发送身份信息",
              "不会发送" in " ".join(label.text() for label in dialog.findChildren(ui.QLabel)))
        check("确认框默认不能点", dialog.ok_button.isEnabled() is False)
        tick(dialog, app)
        check("勾选后才能继续", dialog.ok_button.isEnabled() is True)
        done = wait_for(app, lambda: "已取消" in window.status.text(), 10)
        check("取消后说明没有发送", done, window.status.text())
        check("取消后没有请求", stub_server.records() == [], stub_server.records())
    finally:
        ui.FollowupConsentDialog.exec = original_exec

    section("六、确认后生成候选并选一条")
    captured.clear()

    def accept_exec(dialog):
        captured.append(dialog)
        for box in dialog.findChildren(ui.QCheckBox):
            box.setChecked(True)
        dialog.ok_button.click()
        return 1

    ui.FollowupConsentDialog.exec = accept_exec
    try:
        wait_for(app, lambda: not window.runner.busy and window.suggest_button.isEnabled(), 20)
        window.suggest()
        got = wait_for(app, lambda: window.candidates.rowCount() > 0, 20)
        check("确认后生成出候选", got, window.candidates.rowCount())
        # T26 改写②（空态不再以崩溃形式出现）
        # 原断言：text = window.candidates.item(0, 0).text()（隐含「一定有一行候选」）
        # 原依据：1.8.1「老师点确认后必定拿到候选」，于是旧套件直接下标取第 0 行。
        # 新断言：先用一条断言确认候选表非空，非空才取第 0 行；空态本身就是一条断言。
        # 改写依据：R1-14 下开关关闭时是 403、候选表本来就是空，`item(0,0)` 返回 None
        #           会抛 AttributeError（旧套件在 :315 崩溃）——崩溃不是断言失败，必须显式写成断言。
        check("六.1 候选表非空（空态有明确断言，不再以崩溃形式出现）",
              window.candidates.rowCount() > 0, window.candidates.rowCount())
        text = window.candidates.item(0, 0).text() if window.candidates.rowCount() else ""
        check("候选是一条问学生的话", "？" in text or "?" in text, text)
        check("候选带原文依据",
              bool(window.candidates.item(0, 1).text()) if window.candidates.rowCount() else False,
              window.candidates.item(0, 1).text()[:40] if window.candidates.rowCount() else "")
        records_seen = stub_server.records()
        check("桩收到一条候选请求", len(records_seen) == 1, len(records_seen))
        check("请求标为 followup",
              records_seen and records_seen[0].get("kind") == "followup",
              records_seen[0].get("kind") if records_seen else None)
        body = records_seen[0].get("text") or "" if records_seen else ""
        for forbidden in ("张三", "XY001", "七年级 1 班", "teacher"):
            check("候选请求不含 %s" % forbidden, forbidden not in body)
    finally:
        ui.FollowupConsentDialog.exec = original_exec

    window.candidates.selectRow(0)
    app.processEvents()
    window.use_button.click()
    used = wait_for(app, lambda: window.history.rowCount() == 2, 10)
    check("使用候选后再记一条提问", used, window.history.rowCount())
    check("来源标为 AI 候选",
          "AI" in window.source_label.text() or "候选" in window.source_label.text(),
          window.source_label.text())

    section("七、记录学生回答")
    window.history.selectRow(0)
    app.processEvents()
    window.answer.setPlainText("")
    window.save_answer_button.click()
    app.processEvents()
    check("空回答给出提示", "不能" in window.status.text() or "回答" in window.status.text(),
          window.status.text())
    window.answer.setPlainText("我以为 -6 移过去还是 -6")
    window.save_answer_button.click()
    answered = wait_for(app, lambda: "已保存学生回答" in window.status.text(), 10)
    check("保存回答后给出提示", answered, window.status.text())
    check("记录状态变成已记录回答",
          "已记录回答" in window.history.item(0, 3).text(), window.history.item(0, 3).text())
    check("记录里能看到学生回答",
          "我以为" in window.history.item(0, 2).text(), window.history.item(0, 2).text())
    check("显示回答时间", "回答时间" in window.answer_meta.text(), window.answer_meta.text())
    check("有回答后可以更新错因候选", window.update_button.isEnabled())

    section("八、按回答更新错因候选")
    before = causes_of(data_dir, mistake_id)
    ui.FollowupConsentDialog.exec = accept_exec
    window.update_button.click()
    updated = wait_for(app, lambda: window.compare.rowCount() > 0, 20)
    check("生成出更新前后的对比", updated, window.compare.rowCount())
    check("对比里显示更新前的错因",
          "去括号时漏乘" in window.compare.item(0, 0).text(), window.compare.item(0, 0).text())
    check("对比里显示候选错因",
          bool(window.compare.item(0, 1).text()), window.compare.item(0, 1).text()[:40])
    check("候选还没写进错题", causes_of(data_dir, mistake_id) == before,
          causes_of(data_dir, mistake_id))
    check("接受与拒绝按钮可用",
          window.accept_button.isEnabled() and window.reject_button.isEnabled())

    section("九、拒绝候选")
    window.reject_button.click()
    rejected = wait_for(app, lambda: "拒绝" in window.status.text(), 10)
    check("拒绝后给出提示", rejected, window.status.text())
    check("拒绝后错题错因没变", causes_of(data_dir, mistake_id) == before,
          causes_of(data_dir, mistake_id))
    check("记录标为候选已拒绝",
          "已拒绝" in window.history.item(0, 3).text(), window.history.item(0, 3).text())

    section("十、再更新一次并接受")
    window.update_button.click()
    again = wait_for(app, lambda: window.accept_button.isEnabled(), 20)
    check("可以再次更新候选", again)
    window.accept_button.click()
    accepted = wait_for(app, lambda: "接受" in window.status.text(), 10)
    check("接受后给出提示", accepted, window.status.text())
    merged = causes_of(data_dir, mistake_id)
    check("接受的候选写进错题", "移项时没有变号" in merged, merged)
    check("原来的错因还在", "去括号时漏乘" in merged, merged)
    check("记录标为候选已接受",
          "已接受" in window.history.item(0, 3).text(), window.history.item(0, 3).text())

    section("十一、AI 失败时不覆盖已有记录")
    rows_before = window.history.rowCount()
    stub.FAIL["mode"] = "400"
    window.history.selectRow(0)
    app.processEvents()
    window.update_button.click()
    failed = wait_for(app, lambda: "没有" in window.status.text() or "失败" in window.status.text(), 20)
    check("更新失败时给出提示", failed, window.status.text())
    check("失败后追问记录还在", window.history.rowCount() == rows_before, window.history.rowCount())
    check("失败后错题错因没变", causes_of(data_dir, mistake_id) == merged,
          causes_of(data_dir, mistake_id))
    stub.FAIL["mode"] = "ok"

    section("十二、重开后记录仍在")
    ui.FollowupConsentDialog.exec = original_exec
    other = DesktopService(data_dir)
    other.login("teacher", "pw123456")
    items = other.list_followups(cls["id"], zhang["id"])
    check("重开后能读到追问记录", len(items) == 2, len(items))
    check("重开后回答还在", any(i.get("answer") for i in items), [i.get("answer") for i in items])
    detail = other.get_followup(cls["id"], zhang["id"], items[0]["id"])
    check("重开后能看到候选决定",
          any(u["status"] in ("accepted", "rejected") for u in detail["updates"]),
          [u["status"] for u in detail["updates"]])

    section("十三、已确认错题详情里的入口")
    mistake = other.mistakes_by_ids([mistake_id], full=True)[0]
    dialog = ui.MistakeDialog(other, cls, zhang, mistake=mistake)
    check("错题详情里有「追问这个问题」按钮",
          dialog.followup_button is not None and dialog.followup_button.text() == "追问这个问题")
    check("已确认的错题才显示按钮",
          dialog.followup_button is not None and not dialog.followup_button.isHidden())

    section("十四、空状态")
    blank_id = blank["id"]
    window2 = ui.FollowupWindow(other, cls, blank, "移项")
    wait_for(app, lambda: window2.evidence.toPlainText() != "", 20)
    check("没有已确认错题时说明原文读不到",
          "没有读到" in window2.evidence.toPlainText() or "刷新" in window2.evidence.toPlainText(),
          window2.evidence.toPlainText()[:40])

    section("十五、个体 AI 开关关闭 → 追问两条链路都 403 ai_isolation；开启 → 立刻可用")
    # T26 新增（整段为新断言，不替换任何既有断言）：追问相关的两个 AI 入口在
    # R1-14 下的双路径。桌面「追问候选生成」与「追问后的错因候选生成」是两个独立闸门，
    # 必须分别验，不能用一个代替另一个。
    from app import api as api_mod

    off = svc.set_ai_individual(False)
    check("T26.1 关闭后账号状态是「未授权」",
          off.get("ai_individual_allowed") is False, off.get("ai_individual_allowed"))
    before_requests = len(stub_server.records())

    def expect_isolation(name, call):
        try:
            result = call()
        except api_mod.ApiError as exc:
            check(name,
                  exc.status == 403 and exc.code == "ai_isolation" and "已停用" in exc.message,
                  (exc.status, exc.code, (exc.message or "")[:60]))
            return
        except Exception as exc:  # noqa: BLE001
            check(name, False, "未按 403 拦断，而是 %s: %s" % (type(exc).__name__, exc))
            return
        check(name, False, "没有抛 403，而是返回了 %r" % (result,))

    expect_isolation("T26.2 追问候选生成 → 403 ai_isolation",
                     lambda: svc.suggest_followup(cls["id"], zhang["id"], "移项", consent=True))
    expect_isolation("T26.3 追问后的错因候选生成 → 403 ai_isolation",
                     lambda: svc.update_followup_causes(cls["id"], zhang["id"],
                                                        window.followup_id, consent=True))
    check("T26.4 关闭期间桩一条请求都没收到（拦在组装请求之前）",
          len(stub_server.records()) == before_requests,
          [r.get("kind") for r in stub_server.records()])
    on = svc.set_ai_individual(True)
    check("T26.5 重新开启后账号状态是「已授权」",
          on.get("ai_individual_allowed") is True, on.get("ai_individual_allowed"))
    suggested = svc.suggest_followup(cls["id"], zhang["id"], "移项", consent=True)
    check("T26.6 开启后同一条业务路径立刻能拿到候选（不是「反正会 403」）",
          suggested.get("ok") is True and len(suggested.get("candidates") or []) > 0,
          (suggested.get("ok"), suggested.get("error"), len(suggested.get("candidates") or [])))
    check("T26.7 开启后确有真实外发（桩收到 followup 请求）",
          any(r.get("kind") == "followup" for r in stub_server.records()[before_requests:]),
          [r.get("kind") for r in stub_server.records()[before_requests:]])

    stub_server.close()
    app.processEvents()
    for widget in app.topLevelWidgets():
        widget.close()
    code = 0 if FAILED[0] == 0 else 1
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (PASSED[0] + FAILED[0], PASSED[0], FAILED[0]))
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
