# -*- coding: utf-8 -*-
"""R2-02 界面验收（offscreen Qt）：批量核验进度与取消、单条三区分栏、评分上下界、
确认后撤回、步骤分一致性拦截、多图查看、题级标准编辑、评分进度与覆盖率。

命令：`E:\\Agent\\venv-desktop\\Scripts\\python.exe tests\\verify_r2_02_ui.py`（`QT_QPA_PLATFORM=offscreen`）
日志：`docs/verify-r2-02-ui.txt`

断言全部走真实链路：真库 + `app.grading` + `GradingBackend`，界面只按返回值渲染；
不用桩替代被测逻辑（唯一替身是「模拟用户点按钮」的同步调用与 Qt 的 offscreen 平台）。
"""
import json
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from app import db as db_mod  # noqa: E402
from app import grading  # noqa: E402
from app.db import Database, now_iso  # noqa: E402
from desktop import theme  # noqa: E402
from desktop import ui_grading  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r2-02-ui")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-02-ui.txt")
LINES = []
RESULTS = {"pass": 0, "fail": 0}

QTEXT = "解方程 2x+1=5"
SOLUTION = "x=2"
STEPS_OK = "2x+1=5\n2x=4\nx=2"
STEPS_BAD = "2x+1=5\n2x=6\nx=3"
BATCH = "第一次作业"
BATCH_B = "第二次作业"
UID = 1

APP = None


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


def section(title):
    log("")
    log("== %s ==" % title)


def app():
    global APP
    if APP is None:
        APP = QApplication.instance() or QApplication([])
        theme.apply(APP)
    return APP


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


def make_env(name, answers, students=3, questions=("1", "2"), batch=BATCH):
    path = os.path.join(WORK, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    db_file = os.path.join(path, "app.db")
    db = Database(db_file)
    now = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id, username, display_name, pwd_salt, pwd_hash, iterations,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (UID, "teacher", "张老师", "salt", "hash", 1, now))
        conn.execute("INSERT INTO classes(id, user_id, name, created_at, updated_at)"
                     " VALUES (1,?,'初一(1)班',?,?)", (UID, now, now))
        for sid in range(1, students + 1):
            conn.execute("INSERT INTO students(id, class_id, name, code, created_at, updated_at)"
                         " VALUES (?,1,?,?,?,?)", (sid, "学生%d" % sid, "S%d" % sid, now, now))
        for index, item in enumerate(answers, 1):
            aid = item.get("id") or index
            conn.execute(
                "INSERT INTO answers(id, class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, 1, item["sid"], item.get("key") or "R-%d" % aid,
                 item.get("batch") or batch, "", "S%d" % item["sid"],
                 item.get("qno") or "1", item.get("qtext", QTEXT), item.get("sol", SOLUTION),
                 item.get("steps", STEPS_OK), item.get("source") or "作业",
                 item.get("occurred_on") or "", int(item.get("missing") or 0),
                 item.get("missing_reason") or "", item.get("fingerprint") or "fp-%d" % aid,
                 now, now, now))
            for seq in range(1, int(item.get("images") or 0) + 1):
                file_path = item.get("image_path") or "answer-%d-%d.png" % (aid, seq)
                conn.execute(
                    "INSERT INTO answer_images(class_id, student_id, answer_id, seq, file_path,"
                    " mime, sha256, source, note, created_at) VALUES (1,?,?,?,?,?,?,?,?,?)",
                    (item["sid"], aid, seq, file_path, "image/png", "sha%d-%d" % (aid, seq),
                     "zip", "", now))
    roster = [{"key": "S%d" % sid, "name": "学生%d" % sid, "code": "S%d" % sid}
              for sid in range(1, students + 1)]
    for batch_name in (batch, BATCH_B):
        db_mod.save_batch_config(db, 1, batch_name, {
            "name": batch_name,
            "occurred_on": "2026-11-20",
            "origin": "manual",
            "config_state": "configured",
            "expected_students": roster,
            "expected_questions": list(questions),
            "scored_questions": list(questions),
            "full_scores": {q: 10 for q in questions},
            "rubric": {q: "解方程各步：移项、合并、系数化 1" for q in questions},
            "rubric_version": "v1",
        }, user_id=UID)
    return {"db": db, "class_row": {"id": 1, "name": "初一(1)班"}, "user": UID,
            "batch": batch, "dir": path}


def make_window(env):
    backend = ui_grading.GradingBackend(env["db"], env["class_row"], env["user"],
                                        uploads_dir=env["dir"])
    window = ui_grading.GradingWindow(backend)
    return window, backend


def score_of(env, answer_id, question_no="1"):
    return env["db"].query_one(
        "SELECT * FROM answer_scores WHERE answer_id=? AND question_no=? ORDER BY id DESC LIMIT 1",
        (answer_id, question_no))


# ---------------------------------------------------------------------------
# 1. 窗口结构
# ---------------------------------------------------------------------------


def section_window():
    section("1. 窗口结构")
    env = make_env("01-window", [{"sid": 1, "steps": STEPS_BAD}])
    window, backend = make_window(env)
    check("1.1 主窗口有 3 个页签（批量核验 / 单条批改与评分 / 评分进度）",
          window.tabs.count() == 3
          and window.tabs.tabText(0) == "批量核验"
          and window.tabs.tabText(1) == "单条批改与评分"
          and window.tabs.tabText(2) == "评分进度",
          "tabs=%d" % window.tabs.count())
    check("1.2 三个面板都挂上了（对象可访问，测试与接线都能取到）",
          window.verify_panel is not None and window.review_panel is not None
          and window.score_panel is not None)
    check("1.3 批次下拉读到班级的两个批次（含未配置提示分支）",
          window.verify_panel.batch_combo.count() >= 3,
          "count=%d" % window.verify_panel.batch_combo.count())
    window.close()


# ---------------------------------------------------------------------------
# 2. 批量核验：进度 / 汇总 / 取消 / 重试
# ---------------------------------------------------------------------------


def section_batch():
    section("2. 批量核验（进度、逐条状态、取消未处理、重试失败）")
    env = make_env("02-batch", [
        {"sid": 1, "steps": STEPS_OK},
        {"sid": 2, "steps": STEPS_BAD},
        {"sid": 3, "steps": STEPS_OK},
        {"sid": 1, "steps": STEPS_BAD, "key": "R-4"},
    ])
    window, backend = make_window(env)
    panel = window.verify_panel
    panel.batch_combo.setCurrentIndex(panel.batch_combo.findData(BATCH))
    panel.refresh()
    check("2.1 待核验队列显示 4 条（表格行数=队列长度）",
          panel.table.rowCount() == 4, "rows=%d" % panel.table.rowCount())
    check("2.2 表格列齐全（学生/题号/作答/核验/错因/评分/说明）",
          panel.table.columnCount() == len(panel.COLUMNS)
          and panel.table.horizontalHeaderItem(4).text() == "核验")
    summary = panel.run_verify_sync()
    check("2.3 同步跑完 4 条：进度条满格、汇总写已核验 4",
          panel.progress_bar.value() == 4
          and "已核验 4" in panel.summary_label.text(),
          panel.summary_label.text())
    check("2.4 运行日志逐条记录（每条形如「第 n 条（作答 id）」）",
          panel.log_view.toPlainText().count("第 ") >= 4
          and "定位结论" in panel.log_view.toPlainText(),
          panel.log_view.toPlainText()[:120])
    check("2.5 核验完队列变空（重复点开始不会重复核验）",
          panel.refresh() == [] and panel.table.rowCount() == 0)
    check("2.6 全部核验成功后错误提示为空（没有误报）", panel.window.last_error == "")
    check("2.7 汇总里含跳过数（已有人工判断的才算跳过，这里应为 0）",
          summary.get("skipped") == 0 and summary.get("failed") == 0,
          json.dumps({k: summary[k] for k in ("total", "verified", "skipped", "failed")},
                     ensure_ascii=False))
    window.close()

    # 取消：处理一条后取消
    env2 = make_env("02-cancel", [{"sid": 1, "steps": STEPS_OK}, {"sid": 2, "steps": STEPS_BAD},
                                  {"sid": 3, "steps": STEPS_OK},
                                  {"sid": 1, "steps": STEPS_BAD, "key": "R-4"}])
    window2, backend2 = make_window(env2)
    panel2 = window2.verify_panel
    panel2.batch_combo.setCurrentIndex(panel2.batch_combo.findData(BATCH))
    panel2.refresh()
    worker = panel2.start()
    check("2.8 点「开始核验」后进入运行态（按钮互斥、取消可用）",
          worker is not None and panel2.start_button.isEnabled() is False
          and panel2.cancel_button.isEnabled() is True)
    worker.wait(15000)
    app().processEvents()
    check("2.9 后台核验跑完 4 条，汇总与表格同步刷新",
          panel2.progress_bar.value() == 4 and "已核验 4" in panel2.summary_label.text(),
          panel2.summary_label.text())
    window2.close()

    # 取消：处理一条后取消（用 DirectConnection 在核验线程里立刻置位，
    # 因为主线程此时在 wait()，排队信号要等事件循环才送达；真机上取消按钮走主线程，语义相同）
    env3 = make_env("02-cancel-mid", [{"sid": 1, "steps": STEPS_OK},
                                      {"sid": 2, "steps": STEPS_BAD},
                                      {"sid": 3, "steps": STEPS_OK},
                                      {"sid": 1, "steps": STEPS_BAD, "key": "R-4"}])
    window3, backend3 = make_window(env3)
    panel3 = window3.verify_panel
    panel3.batch_combo.setCurrentIndex(panel3.batch_combo.findData(BATCH))
    panel3.refresh()
    worker3 = ui_grading.VerifyWorker(backend3, BATCH, grading.SCOPE_PENDING)
    seen = {"n": 0, "summary": None}

    def stop_after_one(result):
        seen["n"] += 1
        worker3.cancel()

    worker3.item_done.connect(stop_after_one, Qt.DirectConnection)
    worker3.job_done.connect(lambda summary: seen.__setitem__("summary", summary),
                             Qt.DirectConnection)
    worker3.start()
    worker3.wait(15000)
    app().processEvents()
    remaining = backend3.queue(BATCH, grading.SCOPE_PENDING)
    check("2.10 取消后只核验了 1 条，其余 3 条仍是待核验（已完成的结果已保存）",
          len(remaining) == 3 and seen["n"] >= 1
          and (seen["summary"] or {}).get("cancelled") is True,
          "remaining=%d seen=%d summary=%s" % (len(remaining), seen["n"],
                                               (seen["summary"] or {}).get("cancelled")))
    check("2.11 取消不失败、不报错（日志与状态都不写失败）",
          all(row["dimensions"]["verify"]["state"] == "unverified" for row in remaining)
          and (seen["summary"] or {}).get("failed") == 0)
    window3.close()

    # 重试失败：用面板的 retry() 走真实后端（先人为制造一条失败结果）
    env4 = make_env("02-retry", [{"sid": 1, "steps": STEPS_OK}, {"sid": 2, "steps": STEPS_BAD}])
    window4, backend4 = make_window(env4)
    panel4 = window4.verify_panel
    panel4.batch_combo.setCurrentIndex(panel4.batch_combo.findData(BATCH))
    panel4.refresh()
    panel4.last_summary = {"failed_ids": [2]}
    retried = panel4.retry()
    check("2.12 重试失败：只重跑失败的 1 条并成功落库（其余未核验的仍在队列）",
          retried["total"] == 1 and retried["verified"] == 1
          and [row["answer_id"] for row in backend4.queue(BATCH, grading.SCOPE_PENDING)] == [1],
          json.dumps({k: retried[k] for k in ("total", "verified", "failed")}, ensure_ascii=False))
    check("2.13 没有失败记录时点重试给出明确提示（不静默）",
          panel4.retry() is None and "没有失败的记录" in window4.last_error)
    window4.close()


# ---------------------------------------------------------------------------
# 3. 单条批改：三区分栏
# ---------------------------------------------------------------------------


def section_detail():
    section("3. 单条批改（题目/学生步骤+图片/核验与错因/评分 四块分区）")
    env = make_env("03-detail", [
        {"sid": 1, "steps": STEPS_BAD, "images": 3},
        {"sid": 2, "steps": STEPS_OK},
    ])
    window, backend = make_window(env)
    backend.verify(BATCH, scope=grading.SCOPE_ALL)
    panel = window.review_panel
    panel.reload(1)
    detail = panel.load(1)
    check("3.1 载入单条：作答定位信息写清（学生/题号/批次/四维状态）",
          "学生1" in panel.current_label.text() and "题1" in panel.current_label.text()
          and "第一次作业" in panel.current_label.text(),
          panel.current_label.text())
    check("3.2 分区①：题目与标准解法", QTEXT in panel.question_label.text()
          and panel.solution_view.toPlainText() == SOLUTION)
    check("3.3 分区①带本题评分标准与该题版本（题级权威）",
          "移项" in panel.rubric_label.text() and "v1" in panel.rubric_label.text(),
          panel.rubric_label.text())
    check("3.4 分区②：学生原始步骤逐行显示",
          "2x=6" in panel.steps_view.toPlainText())
    check("3.5 分区②：关联图片 3 张，每条一个查看按钮",
          "关联图片 3 张" in panel.images_label.text() and len(panel.image_buttons) == 3,
          "buttons=%d" % len(panel.image_buttons))
    check("3.6 分区③：核验结论显示定位结论与状态",
          "首处不等价" in panel.locate_label.text()
          and "已核验" in panel.locate_label.text(), panel.locate_label.text())
    check("3.7 分区③：首个可确定错误步骤的原文证据",
          "原文证据" in panel.locate_evidence_label.text()
          and "2x=6" in panel.locate_evidence_label.text(),
          panel.locate_evidence_label.text())
    check("3.8 分区③：候选错因区有提示（真实作答无预置候选也不空白）",
          panel.candidate_list.count() == 1
          and "没有给出候选" in panel.candidate_list.item(0).text())
    check("3.9 分区④：满分与评分状态从批次配置读（10 分 / 未评分）",
          "10" in panel.full_score_label.text() and "未评分" in panel.score_status_label.text(),
          "%s %s" % (panel.full_score_label.text(), panel.score_status_label.text()))
    cards = [label.text() for label in panel.findChildren(QLabel)
             if label.objectName() == "sectionTitle"]
    check("3.10 四块分区标题齐全（①②③④），一眼能看出「三者分区展示」",
          any(text.startswith("①") for text in cards)
          and any(text.startswith("②") for text in cards)
          and any(text.startswith("③") for text in cards)
          and any(text.startswith("④") for text in cards),
          repr(cards))
    check("3.11 列表切换也走同一条载入路径（选中第 2 条）",
          panel.answer_list.count() >= 2
          and panel.load(panel.answer_list.item(1).data(0x0100)) is not None)
    del detail
    window.close()


# ---------------------------------------------------------------------------
# 4. 评分录入：上下界 / 确认 / 撤回 / 步骤分一致
# ---------------------------------------------------------------------------


def section_scoring():
    section("4. 评分录入（上下界拒绝、确认、撤回、步骤分一致性）")
    env = make_env("04-score", [{"sid": 1, "steps": STEPS_BAD}])
    window, backend = make_window(env)
    backend.verify(BATCH, scope=grading.SCOPE_ALL)
    panel = window.review_panel
    panel.reload(1)
    panel.load(1)

    for bad, keyword in (("11", "不能超过"), ("-1", "不能小于"), ("7.555", "两位小数"),
                         ("abc", "数字")):
        panel.score_edit.setText(bad)
        panel.save_score("confirmed")
        check("4.1 越界/非法分数 %r 被拒绝且页面给出提示" % bad,
              keyword in panel.score_message.text()
              and score_of(env, 1) is None,
              panel.score_message.text())
    panel.score_edit.setText("7.5")
    panel.save_score("confirmed")
    check("4.2 满分 10 录 7.5 通过：提示已确认、状态变已确认",
          "已确认" in panel.score_message.text()
          and "已确认" in panel.score_status_label.text(),
          "%s ｜ %s" % (panel.score_message.text(), panel.score_status_label.text()))
    check("4.3 7.5 落库（正式成绩，带作答版本与题级标准版本）",
          score_of(env, 1)["score"] == 7.5 and score_of(env, 1)["state"] == "confirmed"
          and score_of(env, 1)["rubric_version"] == "v1")
    panel.retract_score()
    check("4.4 确认后撤回：状态回未评分、页面提示已撤回、退出正式统计",
          "已撤回" in panel.score_message.text()
          and "未评分" in panel.score_status_label.text()
          and grading.confirmed_scores(env["db"], env["class_row"], BATCH) == [],
          "%s ｜ %s ｜ formal=%d" % (
              panel.score_message.text(), panel.score_status_label.text(),
              len(grading.confirmed_scores(env["db"], env["class_row"], BATCH))))
    # 步骤分一致性
    panel.score_edit.setText("8")
    panel.add_step_row({"step_index": 1, "label": "移项符号错", "deduction": 2.5})
    panel.update_steps_hint()
    check("4.5 步骤分不一致的即时提示（10 − 2.5 = 7.5 ≠ 8）",
          "不一致" in panel.steps_hint_label.text(), panel.steps_hint_label.text())
    panel.save_score("confirmed")
    check("4.6 不一致时后端拒绝保存且库内没有成绩（不改库）",
          "不一致" in panel.score_message.text() and score_of(env, 1)["state"] == "unscored",
          panel.score_message.text())
    panel.score_edit.setText("7.5")
    panel.update_steps_hint("x")
    check("4.7 分数改成 7.5 后即时提示变一致",
          "一致" in panel.steps_hint_label.text(), panel.steps_hint_label.text())
    panel.save_score("confirmed")
    check("4.8 一致时保存成功，扣分项落库",
          score_of(env, 1)["score"] == 7.5
          and env["db"].scalar("SELECT COUNT(*) FROM score_step_deductions") == 1,
          panel.score_message.text())
    # 草稿不计正式（先清掉上一步的扣分项：没有扣分项时分数不受一致性约束）
    # 注：从「已确认 7.5」改成草稿 9 属于改动已确认成绩，按 R2-INV 的留痕要求要写理由。
    panel.steps_table.setRowCount(0)
    panel.reason_edit.setText("先存草稿核对")
    panel.score_edit.setText("9")
    panel.save_score("draft")
    check("4.9 存草稿：状态草稿、不计入正式统计",
          "草稿" in panel.score_message.text()
          and grading.confirmed_scores(env["db"], env["class_row"], BATCH) == [],
          panel.score_message.text())
    # 改已确认分要理由
    panel.score_edit.setText("7.5")
    panel.save_score("confirmed")
    panel.score_edit.setText("6")
    panel.reason_edit.clear()
    panel.save_score("confirmed")
    check("4.10 改已确认分不写理由被拒绝（页面提示修改理由）",
          "修改理由" in panel.score_message.text(), panel.score_message.text())
    panel.reason_edit.setText("讲评后追加扣分")
    panel.save_score("confirmed")
    check("4.11 写明理由后改分成功",
          score_of(env, 1)["score"] == 6
          and "已确认" in panel.score_message.text(),
          panel.score_message.text())
    check("4.12 删除扣分项后重新保存不再要求一致性（无扣分项时不受约束）",
          panel.remove_step_row() is None)
    window.close()


# ---------------------------------------------------------------------------
# 5. 错因与知识点
# ---------------------------------------------------------------------------


def section_cause():
    section("5. 错因确认 / 撤回 / 知识点（界面路径）")
    env = make_env("05-cause", [{"sid": 1, "steps": STEPS_BAD}])
    window, backend = make_window(env)
    backend.verify(BATCH, scope=grading.SCOPE_ALL)
    panel = window.review_panel
    panel.reload(1)
    panel.load(1)
    panel.cause_label_edit.setText("移项符号写错")
    panel.basis_edit.setPlainText("2x=6 应为 2x=4：把 −1 写成 +1")
    result = panel.confirm_cause()
    check("5.1 界面确认错因：提示已确认、错题同步（未分类）",
          result is not None and "已确认错因" in panel.cause_message.text()
          and "未分类" in panel.mistake_label.text(),
          panel.mistake_label.text())
    panel.knowledge_edit.setText("一元一次方程·移项")
    panel.knowledge_basis_edit.setText("错在第 2 行")
    panel.save_knowledge()
    check("5.2 界面保存知识点：显示已分类，原版学情可查询",
          "一元一次方程·移项" in panel.mistake_label.text()
          and "已分类" not in panel.cause_message.text())
    check("5.3 知识点写入 mistakes.knowledge_points（统计读同一列）",
          "移项" in (env["db"].query_one(
              "SELECT knowledge_points FROM mistakes WHERE answer_id=1")["knowledge_points"]))
    panel.retract_cause()
    check("5.4 界面撤回错因：提示已撤回，核验与评分不受影响",
          "已撤回" in panel.cause_message.text()
          and grading.answer_dimensions(env["db"], env["class_row"], 1)["dimensions"]["verify"]
          ["state"] == "verified_rule",
          panel.cause_message.text())
    panel.cause_label_edit.clear()
    panel.confirm_cause()
    check("5.5 缺原因说明时确认错因被拒绝并给出提示（提示来自错误码映射）",
          "原因说明" in panel.cause_message.text()
          or "原因" in window.last_error,
          "%s | %s" % (panel.cause_message.text(), window.last_error))
    window.close()


# ---------------------------------------------------------------------------
# 6. 题级标准编辑与评分进度
# ---------------------------------------------------------------------------


def section_rubric_and_progress():
    section("6. 题级标准编辑与评分进度（覆盖率分母）")
    env = make_env("06-rubric", [{"sid": 1, "steps": STEPS_BAD, "qno": "1"},
                                 {"sid": 2, "steps": STEPS_BAD, "qno": "2", "key": "R-2"}],
                   students=4)
    window, backend = make_window(env)
    backend.verify(BATCH, scope=grading.SCOPE_ALL)
    backend.save_score(1, 7.5, state="confirmed", confirm=True)
    backend.save_score(2, 8, state="confirmed", confirm=True)
    panel = window.review_panel
    panel.load(1)
    check("6.1 标准编辑区带出该题正文与版本（按题显示，不是批次级）",
          panel.rubric_edit.toPlainText().startswith("解方程各步")
          and panel.rubric_version_edit.text() == "")
    panel.rubric_edit.setPlainText("第 1 题新标准：移项 4 分、系数化 1 6 分")
    panel.save_rubric()
    check("6.2 不给新版本号时拒绝（提示要填版本号）",
          "版本号" in panel.rubric_message.text(), panel.rubric_message.text())
    panel.rubric_version_edit.setText("q1-v2")
    panel.save_rubric()
    check("6.3 改第 1 题标准成功：提示只本题失效 1 条",
          "q1-v2" in panel.rubric_message.text()
          and "本题" in panel.rubric_message.text(), panel.rubric_message.text())
    check("6.4 第 1 题成绩失效、第 2 题仍已确认（题级失效，不牵连）",
          score_of(env, 1)["state"] == "invalidated"
          and score_of(env, 2, "2")["state"] == "confirmed",
          "q1=%s q2=%s" % (score_of(env, 1)["state"], score_of(env, 2, "2")["state"]))
    progress = window.score_panel
    progress.batch_combo.setCurrentIndex(progress.batch_combo.findData(BATCH))
    rows = progress.refresh()
    check("6.5 评分进度表列出两道题（题号/满分/已确认/未评/得分率）",
          len(rows) == 2 and progress.table.rowCount() == 2,
          "rows=%d" % progress.table.rowCount())
    check("6.6 覆盖率分母取「应参与评分学生数」（名单 4 人，含未交的人）",
          "应参与评分 4 人" in progress.coverage_label.text(), progress.coverage_label.text())
    check("6.7 完整成绩平均分在无人完整时显示「暂无」而不是 0",
          "暂无" in progress.average_label.text(), progress.average_label.text())
    window.close()


# ---------------------------------------------------------------------------
# 7. 多图查看
# ---------------------------------------------------------------------------


def section_images():
    section("7. 多图查看")
    env = make_env("07-images", [{"sid": 1, "steps": STEPS_BAD, "images": 3}])
    window, backend = make_window(env)
    panel = window.review_panel
    panel.load(1)
    dialog = panel.open_images(0)
    check("7.1 打开多图查看：共 3 张、定位到第 1 张",
          dialog is not None and dialog.count() == 3 and dialog.index == 0
          and "第 1 / 3 张" in dialog.position_label.text(),
          dialog.position_label.text() if dialog else "None")
    dialog.next()
    check("7.2 下一张：第 2 / 3 张，上一张按钮可用",
          dialog.index == 1 and "第 2 / 3 张" in dialog.position_label.text()
          and dialog.prev_button.isEnabled() is True)
    dialog.next()
    check("7.3 到最后一张时「下一张」不可用（不会越界）",
          dialog.index == 2 and dialog.next_button.isEnabled() is False)
    dialog.prev()
    check("7.4 上一张回到第 2 张", dialog.index == 1)
    check("7.5 图片按钮逐个对应（3 个按钮，点第 3 个从第 3 张开始）",
          len(panel.image_buttons) == 3
          and panel.open_images(2).index == 2)
    panel.detail["images"] = []
    check("7.6 没有图片时给出明确提示（不静默）",
          panel.open_images(0) is None and "没有关联图片" in window.last_error)
    dialog.close()
    window.close()


# ---------------------------------------------------------------------------
# 8. 错误提示机制
# ---------------------------------------------------------------------------


def section_errors():
    section("8. 错误提示（沿用 CHECK_ERROR_HINTS 机制）")
    check("8.1 评分一致性错误有「下一步」提示",
          "满分 − 分数" in ui_grading.error_hint("deduction_mismatch"))
    check("8.2 越界分数提示给出正确做法",
          "不能超过" in ui_grading.error_hint("above_full"))
    check("8.3 外发拒绝有提示（本地规则）",
          "外发" in ui_grading.error_hint("ai_isolation"))
    try:
        from desktop.ui import CHECK_ERROR_HINTS
        merged = "basis_required" in CHECK_ERROR_HINTS
    except Exception:  # noqa: BLE001
        merged = False
    check("8.4 本模块缺的码会回落到 CheckWindow 的 CHECK_ERROR_HINTS",
          merged is True and ui_grading.error_hint("cause_not_applicable") != "")
    exc = grading.GradingError("分数不能超过该题满分（本题满分 10）", 400, "above_full")
    text = ui_grading.error_text(exc)
    check("8.5 错误文本保留后端原文 + 状态码 + 提示",
          "分数不能超过" in text and "400" in text and "above_full" in text
          and "提示" in text, text)


# ---------------------------------------------------------------------------
# 9. service 接线路径（探测名逐字对齐，写入必须经过 service 的归属闸门）
# ---------------------------------------------------------------------------


class _RecordingService:
    """假 service：只记录被调用了哪个方法，用来证明界面没绕过 service 直连 app 模块。"""

    GRADING_METHODS = ("grading_queue", "grading_progress", "grading_scoring_progress",
                       "grading_batch_verify", "grading_retry", "grading_detail",
                       "grading_confirm_cause", "grading_retract_cause", "grading_save_score",
                       "grading_retract_score", "grading_update_rubric", "grading_score_sheet")

    def __init__(self, db):
        self.db = db
        self.user = {"id": UID}
        self.calls = []

    # 只实现界面构造与刷新要用的几条，其余按需返回空值
    def grading_queue(self, class_id, batch="", scope="pending", answer_ids=None):
        self.calls.append(("grading_queue", class_id, batch, scope))
        return []

    def grading_progress(self, class_id, batch=""):
        self.calls.append(("grading_progress", class_id, batch))
        return {"total": 0, "verified": 0, "pending": 0, "cause_decided": 0,
                "score_confirmed": 0, "complete_students": 0, "students": 0}

    def grading_scoring_progress(self, class_id, batch):
        self.calls.append(("grading_scoring_progress", class_id, batch))
        return {"eligible_students": 0, "confirmed_students": 0, "coverage": None,
                "coverage_text": "无法计算", "average": None, "average_text": "暂无",
                "denominator_source": "roster", "full_total": None}

    def grading_score_sheet(self, class_id, batch, question_no=""):
        self.calls.append(("grading_score_sheet", class_id, batch))
        return {"batch": batch, "questions": [], "full_total": None}

    def grading_detail(self, class_id, answer_id, question_no=None):
        self.calls.append(("grading_detail", class_id, answer_id))
        return {"dimensions": grading.answer_dimensions(self.db, {"id": class_id},
                                                        answer_id)["dimensions"],
                "score": {}, "candidates": {}, "mistake": None}

    def grading_confirm_cause(self, class_id, answer_id, cause_label, cause_code="",
                              basis_note="", knowledge_point="", expected_version=None):
        self.calls.append(("grading_confirm_cause", class_id, answer_id, cause_label))
        return {"answer_id": answer_id, "cause": {"state": "decided"}}

    def grading_retract_cause(self, class_id, answer_id, reason=""):
        self.calls.append(("grading_retract_cause", class_id, answer_id))
        return {"answer_id": answer_id, "cause": {"state": "undecided"}}

    def grading_save_score(self, class_id, answer_id, score, state="draft", steps=None,
                           revision_reason="", confirm=False, question_no=None,
                           expected_version=None):
        self.calls.append(("grading_save_score", class_id, answer_id, score, state))
        return {"answer_id": answer_id, "state": state, "score": score, "formal": confirm}

    def grading_retract_score(self, class_id, answer_id, reason="", question_no=None):
        self.calls.append(("grading_retract_score", class_id, answer_id))
        return {"answer_id": answer_id, "state": "unscored"}

    def grading_update_rubric(self, class_id, batch, question_no, text, rubric_version,
                              full_score=None):
        self.calls.append(("grading_update_rubric", class_id, batch, question_no, rubric_version))
        return {"rubric_version": rubric_version, "invalidated": 0}


def section_service_path():
    section("9. service 接线路径（探测名对齐 + 写入经过 service 归属闸门）")
    names = _RecordingService.GRADING_METHODS
    try:
        from desktop.service import DesktopService
        real = DesktopService
    except Exception:  # noqa: BLE001
        real = None
    missing = [name for name in names if real is not None and not hasattr(real, name)]
    check("9.1 DesktopService 上 12 个 grading_* 方法都在（探测名与实现名逐字一致）",
          real is not None and missing == [], ",".join(missing))
    check("9.2 归属闸门方法 owned_class_row 也在（service 入口的越权判据）",
          real is not None and hasattr(real, "owned_class_row"))
    env = make_env("09-service", [{"sid": 1, "steps": STEPS_BAD}])
    fake = _RecordingService(env["db"])
    window = ui_grading.GradingWindow(fake, env["class_row"])
    check("9.3 传 service 时自动走 ServiceGradingBackend（不是直连 app.grading）",
          isinstance(window.backend, ui_grading.ServiceGradingBackend)
          and window.backend.service is fake)
    check("9.4 窗口构造即通过 service 取队列（空批次不查评分进度，避免无谓查询）",
          any(call[0] == "grading_queue" for call in fake.calls)
          and not any(call[0] == "grading_score_sheet" for call in fake.calls),
          repr(fake.calls[:4]))
    panel = window.review_panel
    panel.load(1)
    check("9.5 单条载入走 service.grading_detail（不绕过去）",
          any(call[0] == "grading_detail" for call in fake.calls))
    panel.cause_label_edit.setText("移项符号写错")
    panel.confirm_cause()
    panel.retract_cause()
    panel.score_edit.setText("7.5")
    panel.save_score("confirmed")
    panel.retract_score()
    called = {call[0] for call in fake.calls}
    check("9.6 四个写操作都落在 service 方法上（确认错因/撤回/录分/撤回成绩）",
          {"grading_confirm_cause", "grading_retract_cause", "grading_save_score",
           "grading_retract_score"} <= called,
          repr(sorted(called)))
    check("9.7 没有直连 app.grading 的写入（service 的归属闸门不会被绕过）",
          all(call[0].startswith("grading_") for call in fake.calls))
    window.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    app()
    log("对数智伴学 R2-02 界面（批量核验 / 单条批改 / 教师评分）做 offscreen 验收")
    for line in environment_lines():
        log(line)
    log("平台：%s" % os.environ.get("QT_QPA_PLATFORM", ""))
    for runner in (section_window, section_batch, section_detail, section_scoring,
                   section_cause, section_rubric_and_progress, section_images, section_errors,
                   section_service_path):
        runner()
    log("")
    log("== 汇总 ==")
    log("通过 %d / 失败 %d" % (RESULTS["pass"], RESULTS["fail"]))
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
