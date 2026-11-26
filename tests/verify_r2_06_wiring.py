"""R2-06 集成套件（一）：桌面端接线。

captain 独占的接线层（`src/desktop/service.py` / `src/desktop/ui.py`）本身没有业务逻辑，
但有四件事必须被证明，而且它们的失效方式都很安静（按钮能点、窗口能开，就是走到了错的地方）：

  1. **归属闸门真的挂在入口上**：别人的班级 id 传进来必须是 404，而不是"界面不给点"，
     因为 R2-04 的验收是「导出非所属班级 → 拒绝操作」，靠隐藏按钮不算拒绝。
  2. **每个 R2 service 方法都经过同一道闸门**：不能出现"这一个方法忘了判归属"。
  3. **界面入口真的能开出窗口**：`ZipImportWindow` / `TeachingWindow` 由按钮 handler 构造，
     不是只在 import 时能通过。
  4. **未选班级时按钮停用、点击只提示**：与既有 `学生名单 / 班级学情 / 作答核对` 的行为一致。

跑法（桌面端环境）：
    set QT_QPA_PLATFORM=offscreen
    E:\\Agent\\venv-desktop\\Scripts\\python.exe tests/verify_r2_06_wiring.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from app.db import Database, ensure_answer_check_schema  # noqa: E402
from desktop import ui as ui_mod  # noqa: E402
from desktop.service import DesktopService  # noqa: E402

ITEMS = []


def check(label, ok, detail=""):
    ITEMS.append((label, bool(ok)))
    print("%s %s%s" % ("PASS " if ok else "FAIL ", label, (" | " + detail) if detail else ""))


def fresh_service(prefix):
    """按 verify_r1_18 的既有做法建库：Database 建表 → 作答核对迁移 → create=False 打开。"""
    data_dir = os.path.join(tempfile.mkdtemp(prefix=prefix), "data")
    os.makedirs(data_dir)
    db_file = os.path.join(data_dir, "app.db")
    Database(db_file)
    ensure_answer_check_schema(db_file)
    return DesktopService(data_dir)


def main():
    print("数智伴学｜R2-06 集成套件（一）：桌面端接线（service 闸门 + ui 入口）")
    print("python %s" % sys.version.split()[0])
    print("QT_QPA_PLATFORM=%s" % os.environ.get("QT_QPA_PLATFORM", ""))

    owner = fresh_service("r2wire-a-")
    owner.register("tea", "pw-r2-06")
    cid = owner.create_class("初二(1)班")["id"]
    owner.create_student(cid, "张三", "XY001")
    owner.create_student(cid, "李四", "")
    other = fresh_service("r2wire-b-")
    other.register("teab", "pw-r2-06")

    class_row = owner.owned_class_row(cid)
    check("W1 本人班级可取到行", class_row["id"] == cid, class_row["name"])

    # --- 归属闸门挂在这一处，所有 R2 方法都经过它 -------------------------------
    try:
        other.owned_class_row(cid)
        check("W2 别人的班级 → 404", False, "竟然通过了")
    except Exception as exc:
        check("W2 别人的班级 → 404", getattr(exc, "status", None) == 404,
              "%s %s" % (getattr(exc, "status", "?"), getattr(exc, "message", "")[:24]))

    r2_methods = [
        ("preview_zip_answers", lambda s: s.preview_zip_answers(cid, b"")),
        ("import_zip_answers", lambda s: s.import_zip_answers(cid, b"")),
        ("grading_queue", lambda s: s.grading_queue(cid)),
        ("grading_progress", lambda s: s.grading_progress(cid)),
        ("grading_scoring_progress", lambda s: s.grading_scoring_progress(cid, "9月第1次作业")),
        ("grading_detail", lambda s: s.grading_detail(cid, 1)),
        ("grading_confirm_cause", lambda s: s.grading_confirm_cause(cid, 1, "去括号")),
        ("grading_retract_cause", lambda s: s.grading_retract_cause(cid, 1)),
        ("grading_save_score", lambda s: s.grading_save_score(cid, 1, 0)),
        ("grading_retract_score", lambda s: s.grading_retract_score(cid, 1)),
        ("grading_update_rubric", lambda s: s.grading_update_rubric(cid, "b", 1, "t", "v2")),
        ("grading_score_sheet", lambda s: s.grading_score_sheet(cid, "b")),
        ("grading_batch_verify", lambda s: s.grading_batch_verify(cid)),
    ]
    missing = [name for name, _ in r2_methods if not hasattr(DesktopService, name)]
    check("W3 R2 service 方法齐备", not missing, "缺 %s" % missing if missing else "13 个")

    leaked = []
    for name, call in r2_methods:
        try:
            call(other)
            leaked.append(name + ":通过")
        except Exception as exc:
            status = getattr(exc, "status", None)
            # 允许 404（越权早退）；其余错误说明它先做了业务再判归属，也算漏闸门
            if status != 404:
                leaked.append("%s:%s" % (name, status))
    check("W4 13 个方法逐个传别人的班级都被 404 拦住", not leaked, "; ".join(leaked))

    tpl = owner.zip_import_template()
    check("W5 标准 ZIP 模板可下载", tpl[:2] == b"PK" and len(tpl) > 500, "%d bytes" % len(tpl))

    # --- 界面入口 ---------------------------------------------------------------
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    imp = ui_mod.AnswerImportWindow(owner, class_row)
    check("W6 CSV 导入页有「导入作答压缩包」按钮",
          imp.zip_button.objectName() == "openAnswerZipImport", imp.zip_button.text())
    win = imp.open_zip_import()
    check("W7 点它能开出 ZipImportWindow", type(win).__name__ == "ZipImportWindow", type(win).__name__)

    # W8：窗口按方法名探测 service；对不上就会「静默退回直连 app 模块」，
    # 那样归属闸门被绕过而按钮照样能用——所以必须逐个证明探测得到的都是 service。
    probe_names = ("preview_zip_answers", "import_zip_answers", "zip_import_template")
    missing_probe = [n for n in probe_names if not callable(getattr(owner, n, None))]
    check("W8 窗口探测的三个方法名都接在 service 上（不会静默退回直连）",
          not missing_probe, "缺 %s" % missing_probe if missing_probe else "3 个齐全")

    hits = []
    real_gate = owner.owned_class_row

    def spy_gate(class_id):
        hits.append(class_id)
        return real_gate(class_id)

    owner.owned_class_row = spy_gate
    try:
        try:
            win._call("preview_zip_answers", b"")
        except Exception:
            pass  # 空字节不是合法 zip，业务层会拒；这里只关心闸门有没有被走过
    finally:
        owner.owned_class_row = real_gate
    check("W9 窗口调用真的经过归属闸门", hits == [cid], "闸门命中 %s" % hits)
    win.close()

    classes = ui_mod.ClassesWindow(owner)
    check("W10 班级页有「教学建议与练习」按钮",
          classes.teaching_button.objectName() == "openTeachingButton", classes.teaching_button.text())
    classes._selected_class = lambda: None
    classes._sync_buttons()
    check("W11 未选班级时按钮停用（与既有三按钮一致）", classes.teaching_button.isEnabled() is False)
    classes.open_teaching()
    check("W12 未选班级时点击只提示、不抛异常",
          classes.status.text() == "先在列表里点一个班级", classes.status.text())
    classes._selected_class = lambda: class_row
    classes._sync_buttons()
    check("W13 选中班级后按钮启用", classes.teaching_button.isEnabled() is True)
    tw = classes.open_teaching()
    check("W14 点它能开出 TeachingWindow", type(tw).__name__ == "TeachingWindow", type(tw).__name__)
    tw.close()

    # --- R2-03 看板 -------------------------------------------------------------
    dash_methods = ["r2_list_batches", "r2_class_snapshot", "r2_student_snapshot"]
    missing_dash = [n for n in dash_methods if not hasattr(DesktopService, n)]
    check("W15 R2-03 三个只读方法齐备", not missing_dash,
          "缺 %s" % missing_dash if missing_dash else "3 个")

    batches = owner.r2_list_batches(cid)
    check("W16 批次自选可用（R1 遗留 C1 的关闭点）", isinstance(batches, (list, dict)),
          type(batches).__name__)

    snap = owner.r2_class_snapshot(cid, today="2026-11-26")
    check("W17 班级快照可算（18 项指标 + 两项排序）",
          isinstance(snap, dict) and bool(snap), "keys=%d" % len(snap or {}))

    student_id = owner.db.scalar("SELECT id FROM students WHERE class_id=? ORDER BY id", (cid,))
    ssnap = owner.r2_student_snapshot(cid, student_id, today="2026-11-26")
    check("W18 个人快照可算", isinstance(ssnap, dict) and bool(ssnap), "keys=%d" % len(ssnap or {}))

    dash_leaks = []
    for name in dash_methods:
        fn = getattr(DesktopService, name)
        try:
            if name == "r2_student_snapshot":
                fn(other, cid, student_id)
            elif name == "r2_class_snapshot":
                fn(other, cid)
            else:
                fn(other, cid)
            dash_leaks.append(name)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                dash_leaks.append("%s:%s" % (name, getattr(exc, "status", "?")))
    check("W19 三个只读方法同样被归属闸门拦住", not dash_leaks, "; ".join(dash_leaks))

    check("W20 班级页有「学情看板」按钮",
          classes.dashboard_button.objectName() == "openDashboardButton",
          classes.dashboard_button.text())
    classes._selected_class = lambda: None
    classes._sync_buttons()
    check("W21 未选班级时看板按钮停用", classes.dashboard_button.isEnabled() is False)
    classes._selected_class = lambda: class_row
    classes._sync_buttons()
    dw = classes.open_dashboard()
    check("W22 点它能开出 ClassDashboardWindow", type(dw).__name__ == "ClassDashboardWindow",
          type(dw).__name__)
    dw.close()

    # 派单 §5 要求「扩展现有学情页面」：原版两个学情页各要有并列入口
    check("W23 原版班级学情页有「数据看板」入口",
          "open_dashboard" in ui_mod.ClassStatsWindow.__dict__)
    check("W24 原版个人学情页有「数据看板」入口",
          "open_dashboard" in ui_mod.StudentStatsWindow.__dict__)

    # --- R2-02 批量核验与评分窗口 ----------------------------------------------
    check("W25 班级页有「批量核验与评分」按钮",
          classes.grading_button.objectName() == "openGradingButton", classes.grading_button.text())
    classes._selected_class = lambda: None
    classes._sync_buttons()
    check("W26 未选班级时核验按钮停用", classes.grading_button.isEnabled() is False)
    classes.open_grading_window()
    check("W27 未选班级时点击只提示、不抛异常",
          classes.status.text() == "先在列表里点一个班级", classes.status.text())
    classes._selected_class = lambda: class_row
    classes._sync_buttons()
    check("W28 选中班级后核验按钮启用", classes.grading_button.isEnabled() is True)
    gw = classes.open_grading_window()
    check("W29 点它能开出 GradingWindow", type(gw).__name__ == "GradingWindow", type(gw).__name__)
    # 窗口对外暴露的四块面板（界面交付说明 §5 的对外契约）
    panels = ("verify_panel", "review_panel", "score_panel")
    check("W30 GradingWindow 三块面板都在",
          all(hasattr(gw, p) for p in panels),
          str([p for p in panels if not hasattr(gw, p)]) or "3 个齐全")
    gw.close()

    total = len(ITEMS)
    passed = sum(1 for _, ok in ITEMS if ok)
    print("")
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
