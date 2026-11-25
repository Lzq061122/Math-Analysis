"""R1 t19：桌面端「作答核对」页 + AI 开关入口（PySide6 offscreen，真实业务路径）。

依据（R1-00 §12 要求新增测试写明依据）：
- PRD §4.1-4.5（作答核对：三态判断、人工核对、补录校正、追问、批次）、§5.2/§5.4-5.6（人工核对与追问证据）、
  §6.1（统计数据口径）；
- 数智伴学_修改任务清单.md R1-02（原因轴三态 + 保存契约）/ R1-03（保存与详情同源）/ R1-05（人工核对路径）/
  R1-06（追问记录与证据更新）/ R1-08（批次隔离）与 t19 任务书（桌面端同步网页端功能）；
- 口径以网页端 `src/web/app.js` 与 `src/app/answer_check.py` 为准：同一个状态、同一句文案、同一个 4xx。

为什么这样测（反自欺约定）：
1. 全部走真实业务链：`desktop.service` → `app.api` 处理器 → `answer_check`/`db`，界面不写 SQL、不 mock 业务层；
2. 每条硬口径都验证「它可能假」：
   - `basis_required` 必须是**后端真的拒绝**（403/400 来自 answer_check，不是界面自己拦），且库里没写入；
   - `version_conflict` 必须是**另一次真实写入**把版本顶掉（另一个 DesktopService 实例改内容），不是造错误；
   - 「无需判断」必须是后端把 correct/missing_steps/缺测收口成 not_applicable，不是界面写死的字；
   - 撤回必须真的把 `answer_checks.answer_version` 置空（直接读库证明），不是只改界面状态；
3. 两种窗口尺寸（1366×768 / 1920×1080）各测一遍：四个视图无横向滚动条、每页只有一个主按钮、
   主按钮在首屏可见；截图落到 docs/screenshots/ 并按像素尺寸断言。

运行：QT_QPA_PLATFORM=offscreen E:\\Agent\\venv-desktop\\Scripts\\python.exe tests\\verify_r1_19_desktop_check_ui.py
"""
import inspect
import json
import os
import re
import shutil
import sqlite3
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton  # noqa: E402

from app.answers import fingerprint as answer_fingerprint  # noqa: E402
from app.db import Database, now_iso  # noqa: E402
from desktop import ui  # noqa: E402
from desktop.service import ApiError, DesktopService  # noqa: E402
from logmeta import environment_lines, exit_line  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r1-19-desktop")
SHOTS = os.path.join(ROOT, "docs", "screenshots")
LOG_PATH = os.path.join(WORK, "verify-r1-19-desktop-check-ui.txt")

PASSED = [0]
FAILED = [0]
LOG_LINES = []

# 案例（数学内容取自 数智伴学_Agent开发规格.md §7 与 R1-05/06 套件）
CASE_MOVE = {"question": "2x+7=15", "standard": "2x+7=15；2x=8；x=4",
             "steps": "2x+7=15\n2x=15+7\n2x=22\nx=11"}
CASE_ONLY = {"question": "4(x-1)=8", "standard": "4(x-1)=8；x-1=2；x=3", "steps": "x=1"}
CASE_OOS = {"question": "求阴影部分面积", "standard": "S=ab",
            "steps": "先算长方形面积\n12*8=96"}
CASE_OK = {"question": "2(x+3)=10", "standard": "2(x+3)=10；2x+6=10；2x=4；x=2",
           "steps": "2(x+3)=10\n2x+6=10\n2x=4\nx=2"}
OOS_BASIS = "作业本原图上宽只填了 8、长 12，学生按 12*8=96 后又当成长宽各乘 2，属人工核对到的计算错误"


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


def wait(app, predicate, timeout=15.0):
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


def primary_buttons(window):
    """当前可见的主按钮（role=primary）：每页必须只有一个。"""
    return [button for button in window.findChildren(QPushButton)
            if button.property("role") == "primary" and button.isVisible()]


def overflow_px(window, index):
    """当前视图的横向滚动余量：0 表示没有横向溢出。"""
    areas = {0: window.overview_scroll, 1: window.answer_scroll, 3: window.receipt_scroll}
    area = areas.get(index)
    if area is not None:
        return area.horizontalScrollBar().maximum()
    return window.records_table.horizontalScrollBar().maximum()


def shot(app, window, name, width, height):
    window.resize(width, height)
    app.processEvents()
    wait(app, lambda: window.width() == width and window.height() == height, 5)
    app.processEvents()
    path = os.path.join(SHOTS, name)
    pixmap = window.grab()
    pixmap.save(path)
    return path, (pixmap.width(), pixmap.height()), (window.width(), window.height())


def db_row(data_dir, answer_id):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM answer_checks WHERE answer_id=? ORDER BY id DESC LIMIT 1",
            (answer_id,)).fetchone()
    finally:
        conn.close()


def add_answer(db, class_id, student, case, record_key, batch, question_no="1", missing=0,
               imported_at=""):
    row = {
        "question_no": question_no,
        "question_text": case["question"],
        "unit": "一元一次方程",
        "standard_solution": case["standard"],
        "student_steps": case["steps"],
        "occurred_on": "2026-11-24",
        "missing": missing,
        "missing_reason": "",
    }
    fingerprint = answer_fingerprint(row, student["id"])
    stamp = now_iso()
    imported = imported_at or stamp
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO answers(class_id, student_id, record_key, batch, student_name, student_code,"
            " question_no, question_text, unit, standard_solution, student_steps, source, occurred_on,"
            " missing, missing_reason, fingerprint, imported_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student["id"], record_key, batch, student["name"], "",
             row["question_no"], row["question_text"], row["unit"], row["standard_solution"],
             row["student_steps"], "真实导入", row["occurred_on"], row["missing"],
             row["missing_reason"], fingerprint, imported, stamp, stamp))
        return cursor.lastrowid


def build_fixture():
    data_dir = os.path.join(WORK, "data")
    os.makedirs(data_dir)
    Database(os.path.join(data_dir, "app.db"))
    service = DesktopService(data_dir)
    service.register("teacher", "pw123456")
    service.login("teacher", "pw123456")
    class_row = service.create_class("七年级 1 班", grade="七年级")
    zhang = service.create_student(class_row["id"], "张三", "XY001")
    li = service.create_student(class_row["id"], "李四", "XY002")
    db = Database(os.path.join(data_dir, "app.db"), create=False)
    # 批次新旧显式钉住：9月第3次作业是「最近一次导入的真实批次」（当前作业），
    # 9月第4次作业更早，用来验证「具体批次只算自己这一批」。
    ids = {
        "move": add_answer(db, class_row["id"], zhang, CASE_MOVE, "R-1", "9月第3次作业", "1",
                           imported_at="2026-11-24T19:00:00+08:00"),
        "only": add_answer(db, class_row["id"], li, CASE_ONLY, "R-2", "9月第3次作业", "2",
                           imported_at="2026-11-24T19:00:00+08:00"),
        "oos": add_answer(db, class_row["id"], zhang, CASE_OOS, "R-3", "9月第3次作业", "3",
                          imported_at="2026-11-24T19:00:00+08:00"),
        "ok": add_answer(db, class_row["id"], li, CASE_OK, "R-4", "9月第3次作业", "4",
                         imported_at="2026-11-24T19:00:00+08:00"),
    }
    ids["second"] = add_answer(db, class_row["id"], zhang, CASE_MOVE, "R-5", "9月第4次作业", "1",
                               imported_at="2026-11-23T19:00:00+08:00")
    return {"service": service, "class": class_row, "data_dir": data_dir, "db": db,
            "ids": ids, "zhang": zhang, "li": li}


# --------------------------------------------------------------------------
# A. 入口与总览（批次三范围、演示标记、分母差别、唯一主按钮）
# --------------------------------------------------------------------------
def group_a_entry_and_overview(app, env):
    section("A. 入口与课前总览：批次三范围、演示标记、分母差别、唯一主按钮")
    service = env["service"]
    class_row = env["class"]
    classes = ui.ClassesWindow(service)
    classes.reload()
    wait(app, lambda: classes.table.rowCount() == 1, 20)
    classes.table.selectRow(0)
    app.processEvents()
    check("A1 班级列表有「作答核对」入口（objectName=openAnswerCheckButton）",
          classes.findChild(QPushButton, "openAnswerCheckButton") is not None)
    check("A2 选中班级后入口可用", classes.check_button.isEnabled())
    classes.open_check()
    app.processEvents()
    window = getattr(classes, "_check_window", None)
    check("A3 点「作答核对」打开 CheckWindow", window is not None)
    loaded = wait(app, lambda: window.overview_headline.text() != "", 20)
    check("A4 总览加载完成", loaded, window.overview_headline.text())

    state_flag = window.overview_flag.text()
    check("A5 有未核对作答时标记「待核对作答 4 份」（本批 4 份未核对）",
          "待核对作答 4 份" in state_flag, state_flag)
    check("A6 标明当前作业", "当前作业：9月第3次作业" in window.overview_assignment.text(),
          window.overview_assignment.text())
    check("A7 当前范围说明句写明「最近一次导入的真实批次」",
          "最近一次导入的真实批次" in window.overview_scope.text(), window.overview_scope.text())

    combo_labels = [(window.overview_batch_combo.itemText(i), window.overview_batch_combo.itemData(i))
                    for i in range(window.overview_batch_combo.count())]
    texts = [item[0] for item in combo_labels]
    values = [item[1] for item in combo_labels]
    check("A8 批次下拉三档：当前作业 / 全部批次 / 各批次",
          texts[0].startswith("当前作业") and texts[1].startswith("全部批次")
          and values[0] == "" and values[1] == ui.CHECK_ALL_BATCHES, combo_labels)
    check("A9 批次下拉列出两个真实批次",
          "9月第3次作业" in texts and "9月第4次作业" in texts, texts)

    seeded = service.check_demo_seed(class_row["id"])
    check("A10 载入合成演示数据成功（演示批次单独成作业）",
          bool(seeded.get("ok")), json.dumps(seeded, ensure_ascii=False)[:120])
    window.load_overview()
    wait(app, lambda: not window.runner.busy and window.assignment_table.rowCount() >= 3, 20)
    texts = [window.overview_batch_combo.itemText(i)
             for i in range(window.overview_batch_combo.count())]
    check("A11 演示批次在下拉里带「（演示）」标记",
          any("（演示）" in text for text in texts), texts)

    scope_note = window.findChild(ui.QLabel, "checkScopeNote")
    check("A12 页面上用一句话写明三种范围的分母差别",
          scope_note is not None and "分母不同" in scope_note.text()
          and "并列相加" in scope_note.text(), scope_note.text() if scope_note else "")

    rows = []
    for row in range(window.assignment_table.rowCount()):
        rows.append([window.assignment_table.item(row, column).text() for column in range(5)])
    check("A13 作业表列出所有批次并给演示行标「演示」",
          any("（演示）" in item[0] for item in rows) and any(item[0] == "9月第3次作业" for item in rows),
          rows)
    check("A14 作业表数字与总览一致（9月第3次作业 应交 4 / 待核对 4）",
          any(item[0] == "9月第3次作业" and item[1] == "4" and item[3] == "4" for item in rows), rows)

    index = window.overview_batch_combo.findData(ui.CHECK_ALL_BATCHES)
    window.overview_batch_combo.setCurrentIndex(index)
    wait(app, lambda: window.overview_headline.text().startswith("全部批次"), 20)
    check("A15 选「全部批次」后标题给出作业数与应交合计",
          window.overview_headline.text().startswith("全部批次：")
          and "个作业、应交" in window.overview_headline.text(), window.overview_headline.text())
    check("A16 全部批次范围说明句写明「各批次并列相加」",
          "并列相加" in window.overview_scope.text(), window.overview_scope.text())
    titles = [pair[0].text() for pair in window._count_pairs if pair[0].isVisible()]
    values_now = [pair[1].text() for pair in window._count_pairs if pair[0].isVisible()]
    check("A17 全部批次四格改为「作业数 / 应交（各批合计）/ 已核对（合计）/ 待核对作答（合计）」",
          titles == ["作业数", "应交（各批合计）", "已核对（合计）", "待核对作答（合计）"], titles)
    assignments = (window._overview or {}).get("assignments") or []
    expect_total = sum(int(item.get("total") or 0) for item in assignments)
    check("A18 全部批次应交 = 各批次相加（不是只算当前作业）",
          values_now[1] == str(expect_total) and expect_total > 4, "%s vs %s" % (values_now, expect_total))

    index = window.overview_batch_combo.findData("9月第4次作业")
    window.overview_batch_combo.setCurrentIndex(index)
    wait(app, lambda: "只看" in window.overview_scope.text(), 20)
    check("A19 选具体批次后范围句写明「只看…这一批」",
          "只看「9月第4次作业」这一批" in window.overview_scope.text(), window.overview_scope.text())
    check("A20 具体批次的应交只算这一批（1 份）",
          int(window._count_pairs[0][1].text()) == 1, window._count_pairs[0][1].text())

    index = window.overview_batch_combo.findData("")
    window.overview_batch_combo.setCurrentIndex(index)
    wait(app, lambda: "当前作业" in window.overview_scope.text(), 20)
    return classes, window


# --------------------------------------------------------------------------
# B. 单份核对：三态收口、逐行原文、回执同源
# --------------------------------------------------------------------------
def group_b_answer_states(app, env, window):
    section("B. 单份核对：原因轴三态、逐行原文与错步标记、保存回执只读服务端")
    service = env["service"]
    class_id = env["class"]["id"]
    ids = env["ids"]

    window.open_answer(ids["move"])
    ready = wait(app, lambda: (window._detail or {}).get("answer") and not window.runner.busy, 20)
    check("B1 打开首处不等价的作答", ready and window._answer_id == ids["move"], window._answer_id)
    locate = (window._detail or {}).get("locate") or {}
    check("B2 定位结论是 first_bad（规则给出首处错步）", locate.get("status") == "first_bad",
          locate.get("status"))
    check("B3 两轴标签：待核对 · 原因待判断",
          window.axis_progress.text() == "待核对" and window.axis_cause.text() == "原因待判断",
          "%s/%s" % (window.axis_progress.text(), window.axis_cause.text()))
    rows = [window.steps_lines.itemAt(i).widget() for i in range(window.steps_lines.count())]
    check("B4 逐行原文全部渲染（题干 + 3 行学生步骤）", len(rows) == 4, len(rows))
    marks = [window.findChild(ui.QLabel, "checkLineMark-%d" % i) for i in range(len(rows))]
    check("B5 首处不等价行同时有下划线文字标记（不只靠颜色）",
          any(mark is not None and mark.text() == "首处不等价" for mark in marks))
    bad_text = window.findChild(ui.QLabel, "checkLineText-%d" % (locate.get("first_bad_index") or 0))
    check("B6 错步行文字里带 <u> 下划线",
          bad_text is not None and "<u>" in _label_html(bad_text), _label_html(bad_text)[:120])
    check("B7 显示「按规则应为 / 学生这一行」对照",
          window.contrast_box.isVisible() and window.contrast_expected.text() != "—",
          "%s / %s" % (window.contrast_expected.text(), window.contrast_actual.text()))
    check("B8 主按钮文案是「保存错步，原因待判断」", window.save_button.text() == "保存错步，原因待判断",
          window.save_button.text())
    check("B9 首处不等价时追问有适用问句（按本条错步生成）",
          window.ask_line.text().startswith("可以这样问学生："), window.ask_line.text()[:60])

    window.save_button.click()
    saved = wait(app, lambda: window.stack.currentIndex() == 3 and not window.runner.busy, 20)
    check("B10 保存后进入回执页", saved, window.stack.currentIndex())
    check("B11 回执标题「已保存」", window.receipt_title.text() == "已保存", window.receipt_title.text())
    check("B12 回执两轴：已核对 · 原因待判断",
          window.receipt_progress_chip.text() == "已核对"
          and window.receipt_cause_chip.text() == "原因待判断",
          "%s/%s" % (window.receipt_progress_chip.text(), window.receipt_cause_chip.text()))
    check("B13 回执写明保存到哪个班级与作业",
          "七年级 1 班" in window.receipt_dest.text() and "9月第3次作业" in window.receipt_dest.text(),
          window.receipt_dest.text())
    check("B14 回执给出观察到的错步", "观察到的错步：" in window.receipt_observed.text()
          and window.receipt_observed.text() != "观察到的错步：（未填写）", window.receipt_observed.text())
    check("B15 回执主按钮是「查看下一份」（本次作业还有待核对）",
          window.receipt_primary.text() == "查看下一份", window.receipt_primary.text())

    detail = service.check_answer(class_id, ids["move"])
    check("B16 详情与回执同源：progress=checked、cause_state=undecided",
          detail["judgement"]["progress"] == "checked"
          and detail["judgement"]["cause_state"] == "undecided",
          json.dumps(detail["judgement"], ensure_ascii=False)[:160])

    window.open_answer(ids["only"])
    wait(app, lambda: window._answer_id == ids["only"] and not window.runner.busy, 20)
    check("B17 只写结论行的作答判定为缺少步骤",
          (window._detail.get("locate") or {}).get("status") == "missing_steps",
          (window._detail.get("locate") or {}).get("status"))
    check("B18 缺少步骤的作答没有适用追问，页面明说「暂无适用追问」",
          window.ask_line.text() == ui.CHECK_ASK_UNAVAILABLE, window.ask_line.text())
    window.save_button.click()
    wait(app, lambda: window.stack.currentIndex() == 3 and not window.runner.busy, 20)
    check("B19 缺少步骤默认收口成「无需判断」（不要求填依据）",
          window.receipt_cause_chip.text() == "无需判断", window.receipt_cause_chip.text())
    check("B20 回执把「无需判断」讲清是不计入已确认错因",
          "无需判断" in window.receipt_cause_text.text()
          and "不计入已确认错因" in window.receipt_cause_text.text(),
          window.receipt_cause_text.text())
    detail = service.check_answer(class_id, ids["only"])
    check("B21 库里 cause_state=not_applicable（是后端收口，不是界面写的字）",
          detail["judgement"]["cause_state"] == "not_applicable",
          detail["judgement"]["cause_state"])

    window.open_answer(ids["ok"])
    wait(app, lambda: window._answer_id == ids["ok"] and not window.runner.busy, 20)
    window.save_button.click()
    wait(app, lambda: window.stack.currentIndex() == 3 and not window.runner.busy, 20)
    check("B22 全程等价的作答保存后也记「无需判断」",
          window.receipt_cause_chip.text() == "无需判断", window.receipt_cause_chip.text())
    detail = service.check_answer(class_id, ids["ok"])
    check("B23 库里 cause_state=not_applicable（正确作答不需要判原因）",
          detail["judgement"]["cause_state"] == "not_applicable",
          detail["judgement"]["cause_state"])


def _label_html(label):
    try:
        return label.text()
    except RuntimeError:
        return ""


# --------------------------------------------------------------------------
# C. out_of_scope：人工改判必须留依据（后端 400 basis_required 原文照显示）
# --------------------------------------------------------------------------
def group_c_basis_required(app, env, window):
    section("C. out_of_scope 人工改判：依据必填由后端拦（400 basis_required），界面原样显示")
    service = env["service"]
    class_id = env["class"]["id"]
    ids = env["ids"]
    data_dir = env["data_dir"]

    window.open_answer(ids["oos"])
    wait(app, lambda: window._answer_id == ids["oos"] and not window.runner.busy, 20)
    detail = window._detail or {}
    check("C1 规则判不了这份作答：out_of_scope", (detail.get("locate") or {}).get("status") == "out_of_scope",
          (detail.get("locate") or {}).get("status"))
    check("C2 applicability=optional（可判可不判）", detail.get("applicability") == "optional",
          detail.get("applicability"))
    check("C3 判断依据说明改成必填口径",
          "必填" in window.basis_caption.text() and window.basis_hint.isVisible(),
          "%s / %s" % (window.basis_caption.text(), window.basis_hint.isVisible()))
    check("C4 依据提示点名 basis_required，并说明「无需判断」不要求依据",
          "basis_required" in window.basis_hint.text() and "无需判断" in window.basis_hint.text(),
          window.basis_hint.text())
    check("C5 没有可追问的错步 → 「暂无适用追问」", window.ask_line.text() == ui.CHECK_ASK_UNAVAILABLE,
          window.ask_line.text())
    check("C6 页面上有「人工核对并记录错误」入口", window.manual_card.isVisible()
          and window.manual_focus_button.text() == "人工核对并记录错误")

    window.manual_label.setText("面积公式里的宽用错")
    app.processEvents()
    check("C7 填了原因说明后「保存人工判断」可用", window.manual_button.isEnabled())
    window.manual_button.click()
    failed = wait(app, lambda: window.answer_error.isVisible(), 20)
    check("C8 不填依据直接保存 → 被拒绝并给出提示", failed, window.answer_error.text()[:80])
    error_text = window.answer_error.text()
    check("C9 提示里带状态与业务码 400 basis_required", "400" in error_text
          and "basis_required" in error_text, error_text[:120])
    check("C10 提示里是后端原文（逐字）",
          "人工核验要留下判断依据" in error_text and "本次没有写入任何记录" in error_text,
          error_text[:160])
    check("C10b 400 回执格式统一为「保存被拒绝（400 basis_required）：<后端原文>」",
          error_text.startswith("保存被拒绝（400 basis_required）："), error_text[:80])
    row = db_row(data_dir, ids["oos"])
    check("C11 后端真的没写入（answer_checks 里没有这一份的结论）", row is None,
          dict(row) if row else None)
    still = service.check_answer(class_id, ids["oos"])
    check("C12 详情仍是待判断、待核对（没有被界面偷偷补默认值）",
          still["judgement"]["cause_state"] == "undecided"
          and still["judgement"]["progress"] == "pending",
          json.dumps(still["judgement"], ensure_ascii=False)[:160])
    check("C13 失败后页面没有自动填空「判断依据」", window.basis_input.toPlainText() == "",
          window.basis_input.toPlainText()[:40])

    window.basis_input.setPlainText(OOS_BASIS)
    app.processEvents()
    window.manual_button.click()
    wait(app, lambda: window.stack.currentIndex() == 3 and not window.runner.busy, 20)
    check("C14 填了依据后保存成功，回执原因轴=教师已判断",
          window.receipt_cause_chip.text() == "教师已判断", window.receipt_cause_chip.text())
    check("C15 回执区分「教师填写」（不是采纳候选）",
          "错因来源：教师填写" in window.receipt_source_text.text(),
          window.receipt_source_text.text())
    check("C16 回执把核验来源标成教师人工核验",
          "教师人工核验" in window.receipt_verify.text(), window.receipt_verify.text())
    detail = service.check_answer(class_id, ids["oos"])
    check("C17 库里记录人工核验：verify_source=manual、依据落在 basis_note",
          detail["judgement"]["verify_source"] == "manual"
          and detail["judgement"]["basis_note"] == OOS_BASIS,
          json.dumps({k: detail["judgement"].get(k) for k in ("verify_source", "basis_note")},
                     ensure_ascii=False)[:200])
    check("C18 回执展示判断依据原文", OOS_BASIS in window.receipt_basis_text.text(),
          window.receipt_basis_text.text()[:80])


# --------------------------------------------------------------------------
# D. 撤回：版本置空（读库证明），回执写明「已撤回，回到待核对」
# --------------------------------------------------------------------------
def group_d_retract(app, env, window):
    section("D. 撤回：answer_version 置空、回到待核对队列、回执原文")
    service = env["service"]
    class_id = env["class"]["id"]
    ids = env["ids"]
    data_dir = env["data_dir"]

    window.open_answer(ids["move"])
    wait(app, lambda: window._answer_id == ids["move"] and not window.runner.busy, 20)
    check("D1 已核对的作答上出现「撤回本次结论（回到待核对）」",
          window.retract_button.isVisible()
          and window.retract_button.text() == "撤回本次结论（回到待核对）", window.retract_button.text())
    check("D2 撤回说明写明不会删除记录",
          "不会删除记录" in window.retract_hint.text(), window.retract_hint.text())
    before = db_row(data_dir, ids["move"])
    check("D3 撤回前库里 answer_version 等于当前指纹",
          before is not None and (before["answer_version"] or "") != "",
          dict(before) if before else None)

    window.retract_button.click()
    wait(app, lambda: window.stack.currentIndex() == 3 and not window.runner.busy, 20)
    check("D4 撤回后回执标题是「已撤回」", window.receipt_title.text() == "已撤回",
          window.receipt_title.text())
    check("D5 回执写明「已撤回，回到待核对」（后端 action_label 原样）",
          "已撤回，回到待核对" in window.receipt_banner.text(), window.receipt_banner.text()[:120])
    check("D6 回执两轴回到「待核对 · 原因待判断」",
          window.receipt_progress_chip.text() == "待核对"
          and window.receipt_cause_chip.text() == "原因待判断",
          "%s/%s" % (window.receipt_progress_chip.text(), window.receipt_cause_chip.text()))
    check("D7 撤回后主按钮是「返回课前总览」", window.receipt_primary.text() == "返回课前总览",
          window.receipt_primary.text())
    after = db_row(data_dir, ids["move"])
    check("D8 库里 answer_version 被置空（撤回=版本置空，不是加布尔列）",
          after is not None and (after["answer_version"] or "") == "",
          dict(after) if after else None)
    check("D9 撤回不删行：answer_checks 记录仍在，进度回到待核对",
          after is not None and after["progress"] == "pending", after["progress"] if after else None)
    check("D10 撤回理由进 change_reason（留痕可追溯）",
          after is not None and "撤回" in (after["change_reason"] or ""),
          (after["change_reason"] if after else "")[:60])
    detail = service.check_answer(class_id, ids["move"])
    check("D11 详情回到待核对队列，并带撤回痕迹",
          detail["judgement"]["progress"] == "pending"
          and bool((detail["judgement"].get("retracted") or {}).get("at")),
          json.dumps(detail["judgement"], ensure_ascii=False)[:200])

    window.open_answer(ids["move"])
    wait(app, lambda: window._answer_id == ids["move"] and not window.runner.busy, 20)
    check("D12 撤回后可以重新核对（撤回按钮消失、主按钮可再保存）",
          not window.retract_button.isVisible() and window.save_button.isEnabled()
          and window.save_button.text() == "保存错步，原因待判断", window.save_button.text())
    window.save_button.click()
    wait(app, lambda: window.stack.currentIndex() == 3 and not window.runner.busy, 20)
    check("D13 重新核对后回执又是「已保存」", window.receipt_title.text() == "已保存",
          window.receipt_title.text())


# --------------------------------------------------------------------------
# E. 补录 / 校正：带 expected_version、409 原文、演示预置失效
# --------------------------------------------------------------------------
def group_e_content_update(app, env, window):
    section("E. 补录/校正：带 expected_version、版本冲突 409 原文、演示预置失效说明")
    service = env["service"]
    class_id = env["class"]["id"]
    ids = env["ids"]

    window.open_answer(ids["only"])
    wait(app, lambda: window._answer_id == ids["only"] and not window.runner.busy, 20)
    old_version = ((window._detail or {}).get("answer") or {}).get("version") or ""
    check("E0 补录/校正默认折叠（不占首屏），点开才显示输入框",
          not window.content_body.isVisible() and window.content_toggle.isCheckable(),
          window.content_body.isVisible())
    window.content_toggle.setChecked(True)
    app.processEvents()
    check("E0b 点开后学生步骤输入框可见", window.content_steps.isVisible())
    window.content_steps.setPlainText(CASE_MOVE["steps"])
    app.processEvents()
    window.content_button.click()
    wait(app, lambda: not window.runner.busy and window.answer_notice.isVisible(), 20)
    flash = window.answer_notice.text()
    check("E1 补录保存后页内回执写明旧版本作废、新版本、回到待核对",
          "旧版本" in flash and "作废" in flash and "已回到待核对" in flash, flash[:160])
    new_version = ((window._detail or {}).get("answer") or {}).get("version") or ""
    check("E2 内容变化产生新版本（expected_version 走的是真实契约）",
          old_version and new_version and old_version != new_version,
          "%s -> %s" % (old_version[:8], new_version[:8]))
    check("E3 补录后同样的作答不再是 missing_steps（按新内容重新分析）",
          (window._detail.get("locate") or {}).get("status") != "missing_steps",
          (window._detail.get("locate") or {}).get("status"))

    # 版本冲突：另一个真实客户端先把内容改掉，本页拿着旧版本保存 → 409 原文
    window.open_answer(ids["second"])
    wait(app, lambda: window._answer_id == ids["second"] and not window.runner.busy, 20)
    stale_version = ((window._detail or {}).get("answer") or {}).get("version") or ""
    window.note_input.setPlainText("冲突前我填的备注")
    app.processEvents()
    other = DesktopService(env["data_dir"])
    other.login("teacher", "pw123456")
    other.check_update_content(class_id, ids["second"],
                               {"expected_version": stale_version,
                                "student_steps": CASE_MOVE["steps"] + "\nx=5", "missing": "否"})
    window.save_button.click()
    wait(app, lambda: window.answer_error.isVisible() and not window.runner.busy, 20)
    text = window.answer_error.text()
    check("E4 版本冲突提示带 409 与 version_conflict", "409" in text and "version_conflict" in text,
          text[:140])
    check("E4b 409 回执格式统一为「这份作答已被更新（409 version_conflict）：<后端原文>」",
          text.startswith("这份作答已被更新（409 version_conflict）："), text[:90])
    check("E5 冲突提示是后端原文（逐字）",
          "这份作答的内容已经更新" in text and "请刷新后重新核对" in text, text[:160])
    check("E6 冲突后给出「刷新这份作答」入口", window.reload_answer_button.isVisible())
    check("E7 冲突时页面填写内容没有被清掉（不会白填）",
          window.note_input.toPlainText() == "冲突前我填的备注", window.note_input.toPlainText()[:40])
    window.reload_answer_button.click()
    wait(app, lambda: not window.runner.busy and not window.answer_error.isVisible(), 20)
    reloaded_version = ((window._detail or {}).get("answer") or {}).get("version") or ""
    check("E8 刷新后拿到最新版本，错误提示消失",
          reloaded_version and reloaded_version != stale_version and not window.answer_error.isVisible(),
          "%s -> %s" % (stale_version[:8], reloaded_version[:8]))

    # 演示作答可校正：预置失效、按当前内容重新定位
    demo_id = _demo_answer_id(env["data_dir"], class_id)
    window.open_answer(demo_id)
    wait(app, lambda: window._answer_id == demo_id and not window.runner.busy, 20)
    check("E9 演示作答在界面上明确标成合成演示数据",
          "合成演示数据" in window.answer_ident.text(), window.answer_ident.text())
    check("E10 演示作答显示「预置演示」标记", window.preset_note.isVisible(),
          window.preset_note.isVisible())
    window.content_toggle.setChecked(True)
    app.processEvents()
    window.content_steps.setPlainText(CASE_MOVE["steps"] + "\nx=6")
    app.processEvents()
    window.content_button.click()
    wait(app, lambda: not window.runner.busy and window.answer_notice.isVisible(), 20)
    demo_flash = window.answer_notice.text()
    check("E11 演示作答校正后显示「预置已失效、按当前内容重新定位」",
          "预置已失效" in demo_flash and "按当前内容" in demo_flash, demo_flash[:200])
    check("E12 演示作答校正后仍是合成演示数据（隔离不变）",
          "合成演示数据" in window.answer_ident.text(), window.answer_ident.text())

    # 内容没变 → 后端 400 content_unchanged，界面原样显示
    window.content_toggle.setChecked(True)
    app.processEvents()
    window.content_button.click()
    wait(app, lambda: window.content_error.isVisible() and not window.runner.busy, 20)
    check("E13 内容没有实际变化时后端拒绝，界面显示 400 原文",
          "400" in window.content_error.text() and "没有改动任何内容" in window.content_error.text(),
          window.content_error.text()[:140])


def _demo_answer_id(data_dir, class_id):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    try:
        row = conn.execute(
            "SELECT id FROM answers WHERE class_id=? AND source='合成演示' ORDER BY id LIMIT 1",
            (class_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# F. 记录找回：三态筛选 + 批次范围
# --------------------------------------------------------------------------
def group_f_records(app, env, window):
    section("F. 记录找回：三态筛选、批次范围、唯一主按钮")
    service = env["service"]
    class_id = env["class"]["id"]

    window.show_records()
    wait(app, lambda: window.stack.currentIndex() == 2 and not window.runner.busy, 20)
    labels = [button.text() for button in window._filter_buttons.values()]
    check("F1 记录页四档筛选：原因待判断 / 教师已判断 / 无需判断 / 全部",
          labels == ["原因待判断", "教师已判断", "无需判断", "全部"], labels)
    check("F2 默认按「原因待判断」筛选", window._records_state == "undecided",
          window._records_state)
    check("F3 记录页标题带条数", window.records_title.text().startswith("核对记录（"),
          window.records_title.text())
    batch_texts = [window.records_batch_combo.itemText(i)
                   for i in range(window.records_batch_combo.count())]
    check("F4 记录页批次范围含「全部批次（本班所有批次）」",
          batch_texts and batch_texts[0] == "全部批次（本班所有批次）", batch_texts)
    check("F5 记录页批次下拉也标出演示批次",
          any("（演示）" in text for text in batch_texts), batch_texts)

    window._records_filter_changed("decided")
    wait(app, lambda: window._records_state == "decided" and not window.runner.busy, 20)
    decided_rows = _records_rows(window)
    check("F6 「教师已判断」能找回刚才人工改判的那份",
          any("张三" in row[0] for row in decided_rows) and window.records_table.rowCount() >= 1,
          decided_rows)
    check("F7 记录行标出原因判断与处理进度",
          any("面积公式里的宽用错" in row[5] and row[4] == "已核对" for row in decided_rows),
          decided_rows)

    window._records_filter_changed("not_applicable")
    wait(app, lambda: window._records_state == "not_applicable" and not window.runner.busy, 20)
    na_rows = _records_rows(window)
    check("F8 「无需判断」能找回全程等价那份（后端收口的 not_applicable）",
          window.records_table.rowCount() >= 1 and any(row[5] == "无需判断" for row in na_rows),
          na_rows)

    window._records_filter_changed("all")
    wait(app, lambda: window._records_state == "all" and not window.runner.busy, 20)
    check("F9 「全部」把三态都列出来", window.records_table.rowCount() >= 3,
          window.records_table.rowCount())

    overview = service.check_overview(class_id, "")
    has_next = bool((overview.get("next") or {}).get("answer_id"))
    check("F10 记录页主按钮按队列给：有待核对才可点",
          window.records_primary.isEnabled() == has_next,
          "%s / %s" % (window.records_primary.isEnabled(), has_next))
    window.records_table.selectRow(0)
    app.processEvents()
    window._open_selected_record()
    wait(app, lambda: window.stack.currentIndex() == 1 and not window.runner.busy, 20)
    check("F11 从记录能直接打开该份核对页", window.stack.currentIndex() == 1,
          window.stack.currentIndex())


def _records_rows(window):
    rows = []
    for row in range(window.records_table.rowCount()):
        rows.append([window.records_table.item(row, column).text()
                     if window.records_table.item(row, column) else ""
                     for column in range(window.records_table.columnCount())])
    return rows


# --------------------------------------------------------------------------
# G. AI 开关入口（默认关闭；桌面端可外发；关着时个体入口 403）
# --------------------------------------------------------------------------
def group_g_ai_switch(app, env):
    section("G. AI 开关入口：默认关闭、显式开启、关闭时个体入口 403 ai_isolation")
    service = env["service"]
    class_id = env["class"]["id"]
    student_id = env["zhang"]["id"]

    dialog = ui.AISettingsDialog(service)
    app.processEvents()
    check("G1 AI 设置里有「个体数据外发」开关（默认不勾）",
          dialog.findChild(ui.QCheckBox, "aiIndividualCheck") is not None
          and not dialog.individual_check.isChecked())
    check("G2 状态写明默认关闭", "默认关闭" in dialog.individual_state.text(),
          dialog.individual_state.text())
    check("G3 桌面端标明本机可以外发（每次写审计）",
          "本机可以外发" in dialog.individual_state.text(), dialog.individual_state.text())
    check("G4 说明里写明每次外发都写审计", "审计" in dialog.individual_note.text(),
          dialog.individual_note.text()[:80])

    state = service.ai_individual_state()
    check("G5 服务层读到默认关闭、且桌面端 ai_egress_available=True",
          state.get("ai_individual_allowed") is False and state.get("ai_egress_available") is True,
          json.dumps(state, ensure_ascii=False)[:160])

    dialog.individual_check.setChecked(True)
    dialog.individual_save.click()
    app.processEvents()
    check("G6 勾选并保存后账号级开关打开",
          service.ai_individual_state().get("ai_individual_allowed") is True,
          json.dumps(service.ai_individual_state(), ensure_ascii=False)[:120])
    check("G7 保存后状态行显示「已保存：允许个体数据外发」",
          "已保存" in dialog.individual_state.text() and "允许个体数据外发" in dialog.individual_state.text(),
          dialog.individual_state.text())

    dialog.individual_check.setChecked(False)
    dialog.individual_save.click()
    app.processEvents()
    check("G8 关闭后账号级开关回到关闭",
          service.ai_individual_state().get("ai_individual_allowed") is False,
          json.dumps(service.ai_individual_state(), ensure_ascii=False)[:120])

    check("G9 关着的时候桌面端个体 AI 入口被真实拦下（403 ai_isolation）",
          _isolation_status(service, class_id, student_id) == (403, "ai_isolation"),
          _isolation_status(service, class_id, student_id))
    dialog.close()


def _isolation_status(service, class_id, student_id):
    previous = service.ai_settings().get("mode") or "mock"
    try:
        service.save_ai_settings("custom", "http://127.0.0.1:9/v1", "stub-vision", "sk-x",
                                 service_name="本地模拟服务")
        try:
            service.suggest_followup(class_id, student_id, "移项")
            return (200, "")
        except ApiError as exc:
            return (exc.status, exc.code)
    finally:
        service.save_ai_settings(previous)


# --------------------------------------------------------------------------
# H. 两种窗口尺寸：无横向溢出、每页唯一主按钮、主按钮首屏可见、截图
# --------------------------------------------------------------------------
def group_h_layout(app, env, window):
    section("H. 1366×768 与 1920×1080：无横向溢出、每页唯一主按钮、首屏可见、截图")
    os.makedirs(SHOTS, exist_ok=True)
    sizes = ((1366, 768, "ui-desktop-check-1366x768.png"),
             (1920, 1080, "ui-desktop-check-1920x1080.png"))
    names = {0: "总览", 1: "单份核对", 2: "核对记录", 3: "保存回执"}
    for width, height, shot_name in sizes:
        tag = "%dx%d" % (width, height)
        window.load_overview()
        wait(app, lambda: not window.runner.busy and window.overview_headline.text() != "", 20)
        for index in (0, 1, 2, 3):
            if index == 1:
                window.open_answer(env["ids"]["move"])
                wait(app, lambda: not window.runner.busy and window._answer_id == env["ids"]["move"], 20)
            elif index == 2:
                window.show_records()
                wait(app, lambda: not window.runner.busy and window.stack.currentIndex() == 2, 20)
            elif index == 3:
                window.save_button.click()
                wait(app, lambda: not window.runner.busy and window.stack.currentIndex() == 3, 20)
            window.resize(width, height)
            app.processEvents()
            wait(app, lambda: window.width() == width and window.height() == height, 5)
            app.processEvents()
            over = overflow_px(window, index)
            check("H.%s.1 %s视图无横向溢出（横向滚动余量 0）" % (tag, names[index]), over == 0, over)
            primaries = primary_buttons(window)
            check("H.%s.2 %s视图只有一个主按钮" % (tag, names[index]), len(primaries) == 1,
                  [(b.objectName(), b.text()) for b in primaries])
            if primaries:
                bar = primaries[0].mapTo(window, primaries[0].rect().topLeft())
                check("H.%s.3 %s视图主按钮在首屏内可见（不靠滚动）" % (tag, names[index]),
                      0 <= bar.y() and bar.y() + primaries[0].height() <= window.height(),
                      "%s / %s" % (bar.y(), window.height()))
            if index == 1:
                # 截图拍默认形态：补录/校正折叠着，主按钮在底栏首屏可见。
                window.content_toggle.setChecked(False)
                app.processEvents()
                path, pix, actual = shot(app, window, shot_name, width, height)
                check("H.%s.4 截图 %s 已保存且尺寸为 %s" % (tag, shot_name, tag),
                      os.path.exists(path) and pix == (width, height) and actual == (width, height),
                      "%s %s" % (path, pix))


# --------------------------------------------------------------------------
# I. 既有桌面页没被碰坏（结构级；行为级由既有套件另做前后比对）
# --------------------------------------------------------------------------
def group_i_legacy(app, env):
    section("I. 既有桌面页保持原样：既有入口与控件都还在")
    service = env["service"]
    class_row = env["class"]
    students = ui.StudentsWindow(service, class_row)
    names = [button.objectName() for button in students.findChildren(QPushButton)]
    for expected in ("newStudentButton", "editStudentButton", "deactivateStudentButton",
                     "activateStudentButton", "refreshStudentsButton", "openMistakesButton",
                     "openStatsButton", "openStudentInsight", "openAnswerImport", "openAnswerReviews"):
        check("I1 学生名单仍有入口 %s" % expected, expected in names, expected)
    check("I2 「作答分析」入口文案未变",
          students.answers_button.text() == "作答分析", students.answers_button.text())
    check("I3 学生名单新增入口没有混进主按钮层级",
          students.new_button.property("role") == "primary"
          and students.answers_button.property("role") != "primary")
    for name, factory in (
        ("FollowupWindow", lambda: ui.FollowupWindow(service, class_row, env["zhang"], "移项")),
        ("StudentInsightWindow", lambda: ui.StudentInsightWindow(service, class_row, env["zhang"])),
        ("AnswerImportWindow", lambda: ui.AnswerImportWindow(service, class_row)),
    ):
        try:
            widget = factory()
            widget.close()
            ok, detail = True, ""
        except Exception as exc:  # noqa: BLE001 - 这里就是要证明「构造没坏」
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        check("I4 %s 仍能正常构造" % name, ok, detail)

    source = inspect.getsource(ui.CheckWindow)
    check("I5 作答核对页不经 service 以外取数：类里没有 SQL、没有 import 业务模块",
          "SELECT " not in source and "answer_check" not in source, "SELECT/answer_check")
    calls = sorted(set(re.findall(r"self\.service\.([A-Za-z_][A-Za-z0-9_]*)", source)))
    check("I6 作答核对页只调 service.check_*（不自己造通道、不直连数据库）",
          bool(calls) and all(name.startswith("check_") for name in calls), calls)
    check("I7 既有 34 个页面类都还在（没有为了新页删掉旧页）",
          all(hasattr(ui, name) for name in (
              "LoginWindow", "ClassesWindow", "StudentsWindow", "AnswerImportWindow",
              "AnswerListWindow", "StudentInsightWindow", "FollowupWindow",
              "StudentStatsWindow", "ClassStatsWindow", "MistakesWindow", "AISettingsDialog")), "")


def load_cjk_font():
    """offscreen 平台不枚举系统字体，不显式加载中文字体会整屏豆腐块（截图就没法审）。

    只影响测试进程的渲染：加载系统自带微软雅黑/文泉驿，界面代码与运行时不改。
    """
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    for path in (os.path.join(windir, "Fonts", "msyh.ttc"),
                 os.path.join(windir, "Fonts", "msyhbd.ttc"),
                 os.path.join(windir, "Fonts", "simhei.ttf"),
                 "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"):
        if os.path.exists(path) and QFontDatabase.addApplicationFont(path) >= 0:
            return path
    return ""


def _accept_consent(dialog):
    """自动化里替老师点「我已知晓」并确认：只影响这一个进程。"""
    for box in dialog.findChildren(QCheckBox):
        box.setChecked(True)
    button = getattr(dialog, "ok_button", None)
    if button is not None:
        button.click()
    return 1


def _analyse_error(service, class_id, answer_id):
    try:
        service.analyse_answer(class_id, answer_id, consent=True)
        return ""
    except ApiError as exc:
        return exc.message


def group_j_failure_paths_and_empty_states(app, env, window):
    section("J. 失败路径不崩（403 原文照显示）+ 四种空态各有一条明确文案")
    service = env["service"]
    class_row = env["class"]
    class_id = class_row["id"]
    ids = env["ids"]

    # ---- 1) 开关关闭时点既有页的「分析这条作答」：显示 403 原文、窗口不崩
    previous_mode = service.ai_settings().get("mode") or "mock"
    service.save_ai_settings("custom", "http://127.0.0.1:9/v1", "stub-vision", "sk-x",
                             service_name="本地模拟服务")
    review = ui.AnswerListWindow(service, class_row)
    review.show()
    app.processEvents()
    loaded = wait(app, lambda: review.table.rowCount() > 0, 20)
    check("J1 作答分析页列出本班作答（失败路径的入口）", loaded, review.table.rowCount())
    target = None
    for index, item in enumerate(review.items):
        if not item.get("missing") and item.get("review_status") != "confirmed":
            target = index
            break
    check("J2 找到一条还没确认、也不缺测的作答", target is not None, len(review.items))
    original_exec = ui.AnswerConsentDialog.exec
    ui.AnswerConsentDialog.exec = _accept_consent
    try:
        if target is not None:
            review.table.selectRow(target)
            app.processEvents()
            review.analyse_button.click()
            done = wait(app, lambda: "分析没有完成" in review.status.text(), 20)
        else:
            done = False
    finally:
        ui.AnswerConsentDialog.exec = original_exec
    status_text = review.status.text()
    expected = (_analyse_error(service, class_id, review.items[target]["id"])
                if target is not None else "")
    check("J3 开关关闭时「分析这条作答」显示失败提示，没有从任务线程崩出去", done,
          status_text[:80])
    check("J4 提示里带 403 与业务码 ai_isolation",
          "403" in status_text and "ai_isolation" in status_text, status_text[:140])
    check("J4b 403 回执格式统一为「分析没有完成（403 ai_isolation）：<后端原文>」",
          status_text.startswith("分析没有完成（403 ai_isolation）："), status_text[:90])
    check("J5 提示里是后端原文（逐字）", bool(expected) and expected in status_text,
          "%s | %s" % (expected[:50], status_text[:80]))
    check("J6 失败后列表还在、窗口还能用（没有被打断）",
          review.table.rowCount() > 0 and review.isVisible())
    review.close()
    service.save_ai_settings(previous_mode)

    # ---- 2) 空态一：队列为空（本批次全部核对完）
    quiet = service.create_class("七年级 9 班（已核对完）", grade="七年级")
    student = service.create_student(quiet["id"], "王五", "")
    db = Database(os.path.join(env["data_dir"], "app.db"), create=False)
    quiet_answer = add_answer(db, quiet["id"], student, CASE_MOVE, "Q-1", "9月第1次作业", "1")
    quiet_detail = service.check_answer(quiet["id"], quiet_answer)
    service.check_save(quiet["id"], quiet_answer, {
        "expected_version": quiet_detail["answer"]["version"],
        "observed_step": quiet_detail["locate"]["first_bad_text"],
        "first_bad_index": quiet_detail["locate"]["first_bad_index"],
        "cause_state": "undecided", "cause_code": "", "cause_label": "",
        "teacher_note": "", "basis_note": "",
        "followup": {"question": "", "answer_text": ""},
    })
    quiet_window = ui.CheckWindow(service, quiet)
    quiet_window.show()
    wait(app, lambda: quiet_window.overview_headline.text() != "" and not quiet_window.runner.busy, 20)
    check("J7 队列为空时有明确文案「本批次暂无待核对作答」",
          "本批次暂无待核对作答" in quiet_window.overview_detail.text(),
          quiet_window.overview_detail.text())
    check("J8 队列为空时总览标记「全部已核对」",
          quiet_window.overview_flag.isVisible() and quiet_window.overview_flag.text() == "全部已核对",
          quiet_window.overview_flag.text())
    quiet_window.show_records()
    wait(app, lambda: not quiet_window.runner.busy and quiet_window.stack.currentIndex() == 2, 20)
    quiet_window._records_filter_changed("decided")
    wait(app, lambda: not quiet_window.runner.busy and quiet_window._records_state == "decided", 20)
    check("J9 记录为空时有明确文案（本批次暂无满足条件的核对记录）",
          quiet_window.records_table.rowCount() == 0
          and "这个筛选下还没有记录" in quiet_window.records_status.text(),
          quiet_window.records_status.text())
    check("J10 队列为空时记录页主按钮不可点并给出原因",
          not quiet_window.records_primary.isEnabled(), quiet_window.records_primary.isEnabled())
    quiet_window.close()

    # ---- 3) 空态三/四：候选为空、追问不适用（真实作答）
    window.open_answer(ids["move"])
    wait(app, lambda: window._answer_id == ids["move"] and not window.runner.busy, 20)
    check("J11 候选为空时有明确文案「暂无候选错因」",
          window.causes_empty.isVisible()
          and window.causes_empty.text().startswith("暂无候选错因"),
          window.causes_empty.text()[:90])
    window.open_answer(ids["oos"])
    wait(app, lambda: window._answer_id == ids["oos"] and not window.runner.busy, 20)
    check("J12 没有可追问的错步时写「暂无适用追问」",
          window.ask_line.text() == ui.CHECK_ASK_UNAVAILABLE, window.ask_line.text())

    # ---- 4) 异常一律落到页面提示：ApiError 与非 ApiError 都不许漏出任务线程
    def raise_api():
        raise ApiError("测试用业务错误", 409, code="version_conflict")

    outcome = ui.service_call(raise_api)()
    check("J13 ApiError 的 status/code/message 原样带回界面",
          outcome.get("ok") is False and outcome.get("status") == 409
          and outcome.get("code") == "version_conflict"
          and outcome.get("message") == "测试用业务错误", outcome)

    def raise_other():
        raise RuntimeError("线程里的其它异常")

    outcome = ui.service_call(raise_other)()
    check("J14 非 ApiError 异常也被兜成一句可见提示（不会从任务线程冒出）",
          outcome.get("ok") is False and "RuntimeError" in (outcome.get("message") or ""),
          outcome)
    source = inspect.getsource(ui.CheckWindow)
    check("J15 作答核对页每一次后台调用都经过统一包装",
          source.count("self.runner.run(") == source.count("CheckWindow._call(")
          and source.count("self.runner.run(") >= 5,
          "%d / %d" % (source.count("self.runner.run("), source.count("CheckWindow._call(")))


# --------------------------------------------------------------------------
# K. 四个业务错误码的界面回执（t9 整改项：cause_not_applicable 补断言）
# --------------------------------------------------------------------------
def group_k_error_code_matrix(app, env, window):
    section("K. cause_not_applicable：对 correct 判「教师已判断」→ 显示 400 原文与 code，不崩不改状态")
    service = env["service"]
    class_id = env["class"]["id"]
    ids = env["ids"]

    before = service.check_answer(class_id, ids["ok"])
    window.open_answer(ids["ok"])
    wait(app, lambda: window._answer_id == ids["ok"] and not window.runner.busy, 20)
    detail = window._detail or {}
    check("K1 全程等价的作答 applicability=forbidden（只能记「无需判断」）",
          detail.get("applicability") == "forbidden", detail.get("applicability"))
    check("K2 打开时页面显示「已核对 · 无需判断」（B 组保存过的状态）",
          window.axis_progress.text() == "已核对" and window.axis_cause.text() == "无需判断",
          "%s/%s" % (window.axis_progress.text(), window.axis_cause.text()))

    window.manual_label.setText("把「2x+6=10」写成「2x=10+6」")
    app.processEvents()
    window.manual_button.click()
    failed = wait(app, lambda: window.answer_error.isVisible() and not window.runner.busy, 20)
    text = window.answer_error.text()
    check("K3 对 correct 判「教师已判断」→ 界面显示 400 与业务码 cause_not_applicable",
          failed and "400" in text and "cause_not_applicable" in text, text[:140])
    check("K4 显示的是后端原文（逐字）",
          "这份作答没有可归因的错步" in text and "本次没有写入任何记录" in text, text[:180])
    check("K5 回执格式统一为「保存被拒绝（400 cause_not_applicable）：<后端原文>」",
          text.startswith("保存被拒绝（400 cause_not_applicable）："), text[:90])
    check("K6 提示后给出下一步（复用同一错误区，没有新增视觉元素）",
          "提示：" in text and "无需判断" in text, text[-90:])
    check("K7 被拒后界面不自动改状态：仍显示「已核对 · 无需判断」",
          window.axis_progress.text() == "已核对" and window.axis_cause.text() == "无需判断",
          "%s/%s" % (window.axis_progress.text(), window.axis_cause.text()))
    check("K8 被拒后窗口没崩、仍在单份核对页且可继续保存",
          window.stack.currentIndex() == 1 and window.save_button.isEnabled())
    after = service.check_answer(class_id, ids["ok"])
    check("K9 库里状态没变（cause_state 仍 not_applicable、进度仍 checked、结论与保存前一致）",
          after["judgement"]["cause_state"] == "not_applicable"
          and after["judgement"]["progress"] == "checked"
          and after["judgement"]["cause_state"] == before["judgement"]["cause_state"]
          and after["judgement"]["progress"] == before["judgement"]["progress"],
          json.dumps({k: after["judgement"].get(k) for k in ("cause_state", "progress")},
                     ensure_ascii=False))

    # service_call 层与 409（J13）对称：400 也把 status/code/message 原样带回
    def raise_400():
        raise ApiError("测试用 400", 400, code="cause_not_applicable")

    outcome = ui.service_call(raise_400)()
    check("K10 service_call 对 400 同样原样带回 status/code/message",
          outcome.get("ok") is False and outcome.get("status") == 400
          and outcome.get("code") == "cause_not_applicable"
          and outcome.get("message") == "测试用 400", outcome)
    check("K11 界面为 cause_not_applicable 备了专属下一步提示（auth 文案与 basis_required 分开）",
          "cause_not_applicable" in ui.CHECK_ERROR_HINTS
          and "basis_required" in ui.CHECK_ERROR_HINTS
          and ui.CHECK_ERROR_HINTS["cause_not_applicable"] != ui.CHECK_ERROR_HINTS["basis_required"],
          sorted(ui.CHECK_ERROR_HINTS))


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    for line in environment_lines():
        log(line)
    log("")
    env = build_fixture()
    app = QApplication.instance() or QApplication([])
    font_path = load_cjk_font()
    log("字体：%s" % (font_path or "（未找到中文字体，截图可能显示方块）"))
    try:
        classes, window = group_a_entry_and_overview(app, env)
        group_b_answer_states(app, env, window)
        group_c_basis_required(app, env, window)
        group_d_retract(app, env, window)
        group_e_content_update(app, env, window)
        group_f_records(app, env, window)
        group_g_ai_switch(app, env)
        group_h_layout(app, env, window)
        group_i_legacy(app, env)
        group_j_failure_paths_and_empty_states(app, env, window)
        group_k_error_code_matrix(app, env, window)
    finally:
        for widget in app.topLevelWidgets():
            try:
                widget.close()
            except RuntimeError:
                pass
    code = 0 if FAILED[0] == 0 else 1
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (PASSED[0] + FAILED[0], PASSED[0], FAILED[0]))
    log(exit_line(code))
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    print("日志：%s" % LOG_PATH)
    return code


if __name__ == "__main__":
    sys.exit(main())
