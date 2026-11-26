# -*- coding: utf-8 -*-
"""R2-03 界面套件：班级看板与个人看板（offscreen Qt）。

命令：`E:\\Agent\\venv-desktop\\Scripts\\python.exe tests\\verify_r2_03_ui.py`
（必须先 `$env:QT_QPA_PLATFORM='offscreen'`）
日志：`docs/verify-r2-03-ui.txt`

断言口径：
  - 界面上的每个数字都必须等于 `r2_stats` 同一份快照的值（界面不算指标）；
  - 交互按派单 §5.5：点知识点 → 筛选、点热力图格子 → 该生该知识点、点学生 → 个人看板、
    双击错题 → 证据链、清除筛选 → 复位、筛选变化 → 图表与明细联动刷新；
  - 适配 1366×768 / 1920×1080 与 125% 缩放（用最小尺寸与滚动容器，不写死像素）；
  - 确定性：同一数据刷新两次卡片文本逐字段相等。
"""
import json
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

from app import r2_stats  # noqa: E402
from desktop import theme, ui_dashboard  # noqa: E402
from verify_r2_03_stats import (  # noqa: E402
    BATCH, KP_BRACKET, KP_LIKE, KP_MOVE, build_env, seed_dataset,
)

LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-03-ui.txt")
LINES = []
RESULTS = {"pass": 0, "fail": 0}
TODAY = "2026-11-26"


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


def card_text(window, key):
    card = window.cards.get(key) if isinstance(window, ui_dashboard.ClassDashboardWindow) \
        else None
    if card is not None:
        return card.value.text()
    for index in range(window.card_grid.count()):
        widget = window.card_grid.itemAt(index).widget()
        if widget is not None and getattr(widget, "key", "") == key:
            return widget.value.text()
    return None


def main():
    for line in environment_lines():
        log(line)
    app = QApplication.instance() or QApplication([])
    theme.apply(app)

    env = build_env("ui")
    seed_dataset(env)
    db = env["db"]

    section("一、班级看板：18 张卡片与图表的数值必须等于快照")
    window = ui_dashboard.ClassDashboardWindow(db, 1, class_name="初二(3)班",
                                               batch=BATCH, today=TODAY)
    window.auto_exec = False
    snap = r2_stats.class_snapshot(db, 1, BATCH, today=TODAY)
    equal("1.1 指标卡片 18 张", len(window.cards), 18)
    equal("1.2 应作答人数卡片 = 10", card_text(window, "expected_students"), "10")
    equal("1.3 作答人数卡片 = 9", card_text(window, "submitted_students"), "9")
    equal("1.4 有效作答数卡片 = 45", card_text(window, "valid_answers"), "45")
    equal("1.5 已确认错题卡片 = 15", card_text(window, "confirmed_mistakes"), "15")
    equal("1.6 平均分卡片 = 80.00", card_text(window, "average_score"), "80.00")
    equal("1.7 评分覆盖率卡片 = 60.0%", card_text(window, "score_coverage"), "60.0%")
    equal("1.8 作业总分卡片写「完整成绩学生 6 / 应参与评分 10」（不显示 Σ满分）",
          card_text(window, "assignment_total_score"), "完整成绩学生 6 / 应参与评分 10")
    equal("1.9 卡片文本与快照逐项相等",
          {key: window.cards[key].value.text() for key in sorted(window.cards)},
          {key: snap["metrics"][key]["text"] for key in sorted(window.cards)})

    equal("1.10 知识点横向柱 3 根", len(window.kp_chart._rows), 3)
    equal("1.11 知识点柱子顺序与数值",
          window.kp_chart._rows, [(KP_BRACKET, 8.0), (KP_MOVE, 5.0), (KP_LIKE, 2.0)])
    equal("1.12 错因条形 4 根", len(window.cause_chart._rows), 4)
    equal("1.13 各题错误柱 3 根（错题只落在 1–3 题）", len(window.question_chart._rows), 3)
    equal("1.14 第 1 题错误数 = 7、第 2 题 = 6",
          window.question_chart._rows[:2], [("1题", 7.0), ("2题", 6.0)])
    equal("1.15 各题得分率柱 5 根", len(window.rate_chart._rows), 5)
    equal("1.16 完整成绩分布 5 档", len(window.distribution_chart._rows), 5)
    equal("1.17 热力图 8 行 × 3 列",
          (window.heatmap.rowCount(), window.heatmap.columnCount()), (8, 3))
    equal("1.18 共性问题表 3 行，首行「去括号」涉及 7 名学生",
          (window.issue_table.rowCount(), window.issue_table.item(0, 0).text(),
           window.issue_table.item(0, 1).text()), (3, KP_BRACKET, "7"))
    equal("1.19 明细表 15 行", window.detail_table.rowCount(), 15)
    equal("1.20 批次下拉含「全部批次」+ 1 个已登记批次",
          (window.batch_box.count(), window.batch_box.currentData()), (2, BATCH))
    check("1.21 状态栏写明统计范围", "统计范围" in window.status.text(), window.status.text())

    section("二、交互（§5.5）：点图表/热力图/学生/错题，筛选联动刷新")
    window.kp_chart.activate(0)  # 点「去括号」
    equal("2.1 点知识点柱 → 该知识点被选中", window.filters.get("knowledge_point"), KP_BRACKET)
    equal("2.2 明细按知识点筛出 8 条", window.detail_table.rowCount(), 8)
    equal("2.3 筛选后班级人数不变（D7）", card_text(window, "class_students"), "10")
    equal("2.4 筛选后平均分不变（成绩指标不受知识点筛选影响）",
          card_text(window, "average_score"), "80.00")
    equal("2.5 筛选后评分覆盖率不变", card_text(window, "score_coverage"), "60.0%")
    equal("2.6 筛选后知识点柱高亮该类别", window.kp_chart._highlight, KP_BRACKET)

    # 点热力图：S3 × 去括号（S3 行索引 2、去括号列索引 0）
    point_col = window.heatmap.horizontalHeaderItem(0).text()
    row_index = [row["student_key"] for row in window.snapshot["distributions"]["heatmap"]["rows"]
                 ].index("S3")
    window._on_heat_clicked(row_index, 0)
    equal("2.7 点热力图格子 → 记住学生与知识点",
          (window.filters.get("knowledge_point"), window.filters.get("student_ids")),
          (point_col, [3]))
    equal("2.8 热力图联动明细 = S3 的去括号错题 2 条", window.detail_table.rowCount(), 2)

    window.clear_filters()
    equal("2.9 清除筛选 → 明细复位 15 条", window.detail_table.rowCount(), 15)
    equal("2.10 清除筛选 → 知识点筛选清空", window.filters.get("knowledge_point"), "")
    equal("2.11 清除筛选 → 学生筛选清空", window.filters.get("student_ids"), [])

    section("三、点学生 → 个人看板；双击错题 → 证据链")
    personal = window.open_student(3)
    personal.auto_exec = False
    check("3.1 个人看板标题含学生姓名与班级",
          "学生3" in personal.heading.text() and "初二(3)班" in personal.heading.text(),
          personal.heading.text())
    equal("3.2 个人看板已确认错题 = 2",
          personal.snapshot["metrics"]["confirmed_mistakes"]["value"], 2)
    equal("3.3 个人看板有效个人成绩 = 完整成绩学生 1 / 应参与评分 1",
          card_text(personal, "assignment_total_score"), "完整成绩学生 1 / 应参与评分 1")
    equal("3.4 个人看板平均分 = 80.00", card_text(personal, "average_score"), "80.00")
    equal("3.5 个人看板重复错误明细 2 行（知识点 1 + 错因 1）",
          personal.repeat_table.rowCount(), 2)
    equal("3.6 个人看板重点问题排序 1 行", personal.priority_table.rowCount(), 1)
    equal("3.7 个人看板历史记录 2 行", personal.history_table.rowCount(), 2)
    equal("3.8 个人看板错题明细 2 行", personal.detail_table.rowCount(), 2)
    check("3.9 没有辅导建议时显式写「暂无」",
          "暂无" in personal.advice_label.text(), personal.advice_label.text())
    check("3.10 个人看板有效成绩表含各题得分率",
          "各题得分率" in personal.score_table.item(3, 0).text(),
          personal.score_table.item(3, 0).text())
    dialog = personal._open_evidence(0, 0)
    check("3.11 双击错题 → 证据弹窗（含教师判断与原始步骤）",
          dialog is not None and "错题证据" in dialog.windowTitle()
          and bool(dialog.evidence["teacher"]["locate_status"])
          and bool(dialog.evidence["student_steps"]), "弹窗未构造")
    evidence = r2_stats.mistake_evidence(db, 1, dialog.evidence["answer_id"])
    equal("3.12 证据链判定当前版本且错因已确认",
          (evidence["is_current_version"], evidence["teacher"]["cause_state"]), (True, "decided"))

    section("四、筛选控件：日期 / 学生 / 错因（联动刷新，不改成绩指标）")
    window.date_from.setText("2026-12-01")
    window.date_to.setText("2026-12-31")
    window.refresh()
    equal("4.1 批次业务日期不在范围 → 明细清空", window.detail_table.rowCount(), 0)
    equal("4.2 批次不在日期范围 → 成绩指标一并按「暂无」显示（不拿 0 冒充）",
          card_text(window, "average_score"), "暂无")
    window.clear_filters()
    equal("4.3 清除日期筛选 → 明细回到 15 条", window.detail_table.rowCount(), 15)
    window.student_box.setCurrentIndex(window.student_box.findData(1))
    window.refresh()
    equal("4.4 选学生 S1 → 明细 1 条", window.detail_table.rowCount(), 1)
    equal("4.5 选学生 S1 → 班级人数变 1（D7：学生筛选改变班级人数）",
          card_text(window, "class_students"), "1")
    window.clear_filters()
    index = window.cause_box.findData("bracket_rule")
    window.cause_box.setCurrentIndex(index)
    window.refresh()
    equal("4.6 选错因 bracket_rule → 明细 6 条", window.detail_table.rowCount(), 6)
    equal("4.7 选错因不改平均分", card_text(window, "average_score"), "80.00")
    window.clear_filters()

    section("五、适配与确定性：1366×768 / 1920×1080 / 125% 缩放")
    window.resize(1366, 768)
    app.processEvents()
    check("5.1 1366×768 下窗口最小尺寸不超出屏幕",
          window.minimumSizeHint().width() <= 1366 and window.minimumSizeHint().height() <= 768,
          str(window.minimumSizeHint()))
    window.resize(1920, 1080)
    app.processEvents()
    check("5.2 1920×1080 下正常布局（18 张卡片仍在）", len(window.cards) == 18)
    font = app.font()
    original_point = font.pointSizeF() if font.pointSizeF() > 0 else 9.0
    font.setPointSizeF(original_point * 1.25)  # 模拟 125% 缩放
    app.setFont(font)
    window.refresh()
    app.processEvents()
    equal("5.3 125% 字距下刷新仍 18 张卡片且明细 15 条",
          (len(window.cards), window.detail_table.rowCount()), (18, 15))
    check("5.4 125% 下最小宽度仍不超 1366", window.minimumSizeHint().width() <= 1366,
          str(window.minimumSizeHint()))
    font.setPointSizeF(original_point)
    app.setFont(font)
    window.refresh()

    first = {key: window.cards[key].value.text() for key in sorted(window.cards)}
    window.refresh()
    second = {key: window.cards[key].value.text() for key in sorted(window.cards)}
    equal("5.5 同一数据刷新两次卡片逐字段相等", json.dumps(first, sort_keys=True),
          json.dumps(second, sort_keys=True))
    equal("5.6 看板数值全部来自 r2_stats 同一份快照",
          {key: window.cards[key].value.text() for key in sorted(window.cards)},
          {key: r2_stats.class_snapshot(db, 1, BATCH, today=TODAY)["metrics"][key]["text"]
           for key in sorted(window.cards)})

    section("六、界面层不含统计逻辑（边界）")
    ui_source = open(os.path.join(ROOT, "src", "desktop", "ui_dashboard.py"),
                     encoding="utf-8").read()
    check("6.1 界面不 import r2_contract（不在 UI 里判口径）",
          "r2_contract" not in ui_source)
    check("6.2 界面不自己算平均分/覆盖率",
          "sum(" not in ui_source and "/ len(" not in ui_source and "round(100" not in ui_source)
    check("6.3 界面只从 r2_stats 取数", "r2_stats.class_snapshot" in ui_source
          and "r2_stats.student_snapshot" in ui_source)

    window.close()
    personal.close()
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"],
                                          RESULTS["fail"]))
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(LINES) + "\n")
    log(exit_line(RESULTS["fail"]))
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
