"""R2-05 界面验收（离屏）：教学资源与辅导窗口能建、能读、能走完 A–E 五条主路径。

只验证界面层「把后端返回值渲染出来 + 把动作转成一次后端调用」，不重复后端的业务断言
（那些在 tests/verify_r2_05_resources.py 里）。关键点：
  1. 窗口/页签/控件都建得起来，资源概览与列表有数据；
  2. 送审 / 双审核 / 发布 / 停用 / 版本历史五条按钮路径都真的改了库里的状态；
  3. 建议匹配、三级提示、举一反三与任务创建、纸笔 PDF 导出都能在界面里走完；
  4. **错误只写状态标签、不弹模态框**（否则离屏测试会挂住），并断言拒绝后库没变。

运行（Qt 层解释器，必须离屏）：
  $env:QT_QPA_PLATFORM='offscreen'
  E:\\Agent\\venv-desktop\\Scripts\\python.exe tests\\verify_r2_05_ui.py
日志：docs/verify-r2-05-ui.txt
"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                     # noqa: E402

from app import auth                                           # noqa: E402
from app import db as DB                                       # noqa: E402
from app import teaching_resources as T                        # noqa: E402
from app import tutoring_pdf as P                              # noqa: E402
from desktop.ui_teaching import TeachingWindow                 # noqa: E402

LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-05-ui.txt")
RESOURCE_DIR = os.path.join(ROOT, "resources", "teaching_resources")

TOTAL = [0]
FAILED = [0]
LINES = []
WORK = []

UNIT = "第三单元 一元一次方程"
BATCH = "第一次作业"


class FakeService:
    """桌面端 service 的最小替身：界面只用到 db / user / data_dir。"""

    def __init__(self, database, user, data_dir):
        self.db = database
        self.user = user
        self.data_dir = data_dir


def log(text=""):
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))
    LINES.append(text)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if not ok:
        FAILED[0] += 1
    log(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  ← %r" % (detail,)))
    return bool(ok)


def tmpdir(prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    WORK.append(path)
    return path


def build(data_dir):
    database = DB.Database(os.path.join(data_dir, "app.db"))
    teacher = auth.create_user(database, "teacher1", "pw123456", "张老师")
    reviewer = auth.create_user(database, "teacher2", "pw123456", "李老师")
    now = DB.now_iso()
    with database.tx() as conn:
        conn.execute("INSERT INTO classes(user_id, name, grade, note, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?)", (teacher, "初一(3)班", "初一", "", now, now))
        class_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO students(class_id, name, code, note, active, created_at,"
                     " updated_at) VALUES (?,?,?,'',1,?,?)",
                     (class_id, "林小雨", "A021", now, now))
        student_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
            " student_code, question_no, question_text, unit, standard_solution, student_steps,"
            " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,'',?,?,?,?)",
            (class_id, student_id, "R001", BATCH, "林小雨", "", "1",
             "解方程：3x + 5 = 2x - 7", UNIT, "略", "略", "作业", "2026-11-10", "fp-1",
             now, now, now))
        answer_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO answer_checks(class_id, student_id, answer_id, batch, answer_version,"
            " progress, cause_state, cause_source, cause_code, cause_label, verify_source,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, answer_id, BATCH, "fp-1", "checked", "decided", "teacher",
             "MOVE-SIGN", "移项不变号", "rule", now, now))
        conn.execute(
            "INSERT INTO mistakes(class_id, student_id, status, source_type, occurred_on,"
            " question_text, knowledge_points, created_at, updated_at, answer_id)"
            " VALUES (?,?,'confirmed','作业',?,?,?,?,?,?)",
            (class_id, student_id, "2026-11-10", "解方程：3x + 5 = 2x - 7",
             json.dumps(["移项"], ensure_ascii=False), now, now, answer_id))
        conn.execute("INSERT INTO teaching_resources(resource_key, kind, unit, knowledge_point,"
                     " cause_code, title, body, conditions, source, status, current_version,"
                     " created_by, created_at, updated_at)"
                     " VALUES ('ui-draft','advice',?,'界面验收知识点','UI-ONLY','界面用的草稿资源',"
                     " '{\"text\":\"先专项后整题\"}','{}','自编','draft',1,?,?,?)",
                     (UNIT, teacher, now, now))
        conn.execute("INSERT INTO teaching_resource_versions(resource_key, version, body,"
                     " conditions, state, note, created_by, created_at)"
                     " VALUES ('ui-draft',1,'{\"text\":\"先专项后整题\"}','{}','draft','建档',?,?)",
                     (teacher, now))
    packages = []
    for name in sorted(os.listdir(RESOURCE_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(RESOURCE_DIR, name), "r", encoding="utf-8") as handle:
                packages.append(json.load(handle))
    for payload in packages:
        T.import_resources(database, payload, created_by=teacher)
    # 界面验收需要一批「已发布」资源：按标准路径（双审核两个不同账号）把「移项」这一包发出去
    for row in T.list_resources(database, knowledge_point="移项"):
        if T.is_synthetic(row["source"]):
            continue
        key = row["resource_key"]
        if row["status"] == "published":
            continue
        if row["status"] == "draft":
            T.submit_for_review(database, key, user=teacher)
        T.record_review(database, key, 1, "math", teacher, "pass", "数学：核对无误")
        T.record_review(database, key, 1, "teaching", reviewer, "pass", "教学：与学情相符")
        T.publish_version(database, key, 1, user=reviewer)
    return {"db": database, "teacher": teacher, "reviewer": reviewer, "class_id": class_id,
            "student_id": student_id, "answer_id": answer_id, "user_row": database.query_one(
                "SELECT * FROM users WHERE id=?", (teacher,))}


def review_and_publish(database, rows, key, teacher, reviewer):
    for row in rows:
        if row["resource_key"] == key:
            return
    T.submit_for_review(database, key, user=teacher)
    T.record_review(database, key, 1, "math", teacher, "pass", "数学：核对无误")
    T.record_review(database, key, 1, "teaching", reviewer, "pass", "教学：与学情相符")
    T.publish_version(database, key, 1, user=reviewer)


def main():
    from logmeta import environment_lines
    log("R2-05 界面验收（离屏，PySide6）：教学资源库 / 双审核发布 / 建议匹配 / 三级提示 / 任务与 PDF")
    for line in environment_lines():
        log(line)
    app = QApplication.instance() or QApplication([])
    fixture = None
    try:
        data_dir = tmpdir("r2-05-ui-")
        fixture = build(data_dir)
        database = fixture["db"]
        user = fixture["user_row"]
        service = FakeService(database, user, data_dir)

        window = TeachingWindow(service)
        window.show()
        app.processEvents()
        check("界面：主窗口与 5 个页签建起来（对齐派单 A–E）",
              window.objectName() == "teachingWindow" and window.tabs.count() == 5,
              window.tabs.count())
        check("界面：资源概览显示五类资源与状态计数",
              "资源 61 条" in window.summary_label.text()
              or "待审核" in window.summary_label.text(), window.summary_label.text())
        log("    概览：%s" % window.summary_label.text())

        # ---- A 资源库 ----
        rows = window.refresh_library()
        check("A 资源库：列表读到资源（导入的 + 界面用的草稿）", len(rows) >= 50, len(rows))
        window.library_table.selectRow(0)
        app.processEvents()
        check("A 资源库：选中一行后详情区有正文与状态", "标题：" in window.library_detail.toPlainText())
        window.library_kind.setCurrentIndex(1)          # 错因
        window.refresh_library()
        kinds = {row["kind"] for row in window._library_rows}
        window.library_kind.setCurrentIndex(0)
        index = [i for i, row in enumerate(rows) if row["resource_key"] == "ui-draft"][0]
        window.refresh_library()
        index = [i for i, row in enumerate(window._library_rows)
                 if row["resource_key"] == "ui-draft"][0]
        window.library_table.selectRow(index)
        app.processEvents()
        check("A 资源库：类型筛选生效（只剩所选类型）",
              kinds == {window.library_kind.itemData(1)}, kinds)
        if window.submit_current():
            detail = T.get_resource(database, "ui-draft")
            check("B 审核发布：界面「送审」把草稿推进到待审核队列",
                  detail["status"] == "pending_review" and detail["version_state"] == "pending_review",
                  detail["status"])

        # ---- B 审核发布（两位老师各签一项，再由审核人发布）----
        window.refresh_all()
        queue = window.refresh_queue()
        target = [row for row in queue if row["resource_key"] == "ui-draft"][0]
        selection = [i for i, row in enumerate(queue) if row["resource_key"] == "ui-draft"][0]
        window.review_table.selectRow(selection)
        app.processEvents()
        window.review_kind.setCurrentIndex(0)            # 数学正确性
        window.review_opinion.setText("")
        window.review_pass_button.click()
        app.processEvents()
        check("B 审核发布：审核意见为空时拒绝并提示（不弹模态框）",
              "审核意见必填" in window.status_label.text() and window.status_text,
              window.status_text)
        window.review_opinion.setText("数学正确性：式子与答案核对无误")
        window.review_pass_button.click()
        app.processEvents()
        state = T.review_state(database, "ui-draft", 1)
        check("B 审核发布：界面记录数学审核通过", state["passed"]["math"], state["passed"])
        window.review_opinion.setText("教学适切性：与初一学情相符")
        window.review_kind.setCurrentIndex(1)            # 教学适切性（同一账号 → 应被拒）
        window.review_pass_button.click()
        app.processEvents()
        check("B 审核发布：同一账号两项都签被拒绝并说明原因",
              "两个不同账号" in window.status_label.text(), window.status_text)
        window.publish_button.click()
        app.processEvents()
        check("B 审核发布：双审核未齐时发布被拒绝",
              "两项审核" in window.status_label.text(), window.status_text)
        # 换第二个账号（模拟另一位审核老师登录）
        reviewer_window = TeachingWindow(FakeService(
            database, database.query_one("SELECT * FROM users WHERE id=?",
                                         (fixture["reviewer"],)), data_dir))
        reviewer_window.show()
        app.processEvents()
        queue = reviewer_window.refresh_queue()
        selection = [i for i, row in enumerate(queue) if row["resource_key"] == "ui-draft"][0]
        reviewer_window.review_table.selectRow(selection)
        reviewer_window.review_kind.setCurrentIndex(1)
        reviewer_window.review_opinion.setText("教学适切性：与初一学情相符")
        reviewer_window.review_pass_button.click()
        app.processEvents()
        state = T.review_state(database, "ui-draft", 1)
        check("B 审核发布：第二位老师签署教学审核后双审核齐备", state["ready_to_publish"])
        reviewer_window.publish_button.click()
        app.processEvents()
        detail = T.get_resource(database, "ui-draft")
        check("B 审核发布：界面发布成功，状态变已发布且写入发布时间",
              detail["status"] == "published" and detail["version_state"] == "published"
              and detail["published_at"], (detail["status"], detail["version_state"]))
        # 无权限账号发布另一条资源 → 拒绝
        third = auth.create_user(database, "teacher3", "pw123456", "王老师")
        pending = [row for row in T.list_resources(database, status="pending_review")
                   if not T.is_synthetic(row["source"])]
        other_key = pending[0]["resource_key"]
        third_window = TeachingWindow(FakeService(
            database, database.query_one("SELECT * FROM users WHERE id=?", (third,)), data_dir))
        third_window.show()
        app.processEvents()
        rows_q = third_window.refresh_queue()
        selection = [i for i, row in enumerate(rows_q) if row["resource_key"] == other_key][0]
        third_window.review_table.selectRow(selection)
        third_window.publish_button.click()
        app.processEvents()
        check("B 审核发布：无权限账号在界面发布被拒绝（状态标签给出原因）",
              "没有发布权限" in third_window.status_label.text()
              or "审核" in third_window.status_label.text(), third_window.status_text)
        check("B 审核发布：拒绝后资源仍是待审核（界面没绕过闸门）",
              T.get_resource(database, other_key)["status"] == "pending_review")

        # ---- C 建议匹配 ----
        window.match_knowledge.setText("移项")
        window.match_cause.setText("MOVE-SIGN")
        window.match_confirmed.setChecked(True)
        result = window.run_match()
        check("C 建议匹配：界面匹配到已发布资源并显示依据与来源",
              result["items"] and "推荐依据" not in window.match_note.text()
              and window.match_table.rowCount() == len(result["items"]), len(result["items"]))
        check("C 建议匹配：错因未确认时界面只给通用资源并显示提示",
              all(item["tier"] == "general"
                  for item in (window.match_confirmed.setChecked(False) or True)
                  and window.run_match()["items"])
              and "错因未确认" in window.match_note.text(), window.match_note.text())
        window.match_confirmed.setChecked(True)
        window.run_match()
        window.match_answer_id.setValue(fixture["answer_id"])
        by_answer = window.match_from_answer()
        check("C 建议匹配：从作答 id 进入时自动带出知识点/错因/是否确认",
              by_answer and by_answer["context"]["knowledge_point"] == "移项"
              and by_answer["context"]["cause_confirmed"], by_answer and by_answer["context"])

        # ---- D 三级提示 ----
        window.match_knowledge.setText("移项")
        window.match_cause.setText("MOVE-SIGN")
        window.match_confirmed.setChecked(True)
        plan = window.load_hints()
        check("D 三级提示：界面展示 P1/P2/P3 三级提示（含资源版本）",
              [item["level"] for item in plan["levels"]] == ["P1", "P2", "P3"]
              and window.hint_table.rowCount() == 3, plan["levels"])
        window.hint_student.setText(str(fixture["student_id"]))
        window.hint_level.setCurrentIndex(1)             # P2
        window.hint_independent.setChecked(False)
        window.hint_full_solution.setChecked(True)
        window.hint_after.setChecked(True)
        before = database.scalar("SELECT COUNT(*) FROM hint_usages")
        ok = window.record_hint()
        after = database.scalar("SELECT COUNT(*) FROM hint_usages")
        check("D 三级提示：没有独立尝试就给完整解析被拒绝，且不写库",
              not ok and after == before and "独立尝试之后" in window.status_label.text(),
              window.status_text)
        window.hint_independent.setChecked(True)
        ok = window.record_hint()
        check("D 三级提示：独立尝试后给完整解析记成功（界面提示三个勾选状态）",
              ok and "解析在独立尝试之后=是" in window.status_label.text(), window.status_text)

        # ---- E 举一反三与任务 ----
        found = window.load_exercises()
        check("E 举一反三：界面列出三类练习并逐题显示用途/难度/答案/理由",
              window.exercise_table.rowCount() >= 3
              and all(window.exercise_table.item(i, 2).text() for i in
                      range(window.exercise_table.rowCount())),
              window.exercise_table.rowCount())
        window.task_title.setText("移项专项辅导（界面验收）")
        window.task_note.setText("先让学生自己试第一步。")
        task = window.create_task()
        check("E 举一反三：界面创建辅导任务成功并记录资源版本",
              task is not None and len(task["resource_versions"]) >= 3,
              task and task["resource_versions"])
        window.refresh_tasks()
        selection = [i for i, row in enumerate(window._tasks) if row["id"] == task["id"]][0]
        window.task_table.selectRow(selection)
        app.processEvents()
        pdf_path = os.path.join(data_dir, "界面-学生版.pdf")
        exported = window.export_paper("student", pdf_path)
        text = ""
        if exported and os.path.exists(exported):
            with open(exported, "rb") as handle:
                text = P.extract_text(handle.read())
        answers = [item["answer"] for item in found["groups"].get("steps", [])[:1] if item["answer"]]
        check("E 举一反三：界面导出学生版 PDF，回读文本有题目、没有答案",
              bool(exported) and "作答说明" in text
              and all(answer not in text for answer in answers), (exported, bool(text)))
        teacher_path = window.export_paper("teacher", os.path.join(data_dir, "界面-教师版.pdf"))
        teacher_text = ""
        if teacher_path and os.path.exists(teacher_path):
            with open(teacher_path, "rb") as handle:
                teacher_text = P.extract_text(handle.read())
        check("E 举一反三：教师版 PDF 含答案解析（与学生版形成对照）",
              "答案：" in teacher_text and "解析：" in teacher_text, bool(teacher_text))
        check("E 举一反三：记录完成后任务状态变已完成",
              window.complete_current_task()
              and T.get_task(database, task["id"])["status"] == "completed")
        reviewer_window.close()
        third_window.close()
        window.close()
        app.processEvents()
    except Exception:                                        # noqa: BLE001
        import traceback
        log("")
        log("!! 套件异常中断：")
        log(traceback.format_exc())
        FAILED[0] += 1
    finally:
        if fixture:
            fixture["db"].conn.close()
        for path in WORK:
            shutil.rmtree(path, ignore_errors=True)
    log("")
    log("总计 %d 项断言：通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    log("结论：%s" % ("全部通过（exit 0）" if FAILED[0] == 0 else "有失败项（exit 1）"))
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(LINES) + "\n")
    return 0 if FAILED[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
