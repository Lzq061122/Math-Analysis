# -*- coding: utf-8 -*-
"""R2-03 学情看板（桌面端）：指标卡片 + 图表 + 热力图 + 可筛选明细 + 个人看板。

边界（契约 §2.1）：**统计逻辑一行都不写在这里**——所有数值来自 `app/r2_stats.py`
的同一份快照（`class_snapshot` / `student_snapshot`），本文件只做「取值 + 摆放 + 交互」。
图表用本地 QPainter 组件（不引第三方绘图库），样式统一取 `desktop.theme`。

交互（派单 §5.5）：
  点知识点柱 → 筛选该知识点的学生与错题
  点热力图格子 → 该学生该知识点的错题
  点明细里的学生 → 打开个人看板（个人学情）
  双击明细行 → 追溯原始作答 / 教师判断 / 证据（`r2_stats.mistake_evidence`）
  清除筛选 / 筛选变化 → 整块联动刷新（同一份快照，不各算各的）

入口接线片段见 `docs/R2-03_交付说明.md` §4（菜单项 + service 方法由 captain 统一加）。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app import r2_stats

from . import theme

CARD_MIN_WIDTH = 150


def _short(text, limit=18):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class BarChart(QFrame):
    """本地柱状图组件（横向/纵向），点一根柱发 `clicked(label)`。"""

    clicked = Signal(str)

    def __init__(self, title="", horizontal=True, height=150, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumHeight(height)
        self._title = title
        self._horizontal = horizontal
        self._rows = []  # [(label, value)]
        self._highlight = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        self._title_label = QLabel(title)
        theme.section(self._title_label)
        layout.addWidget(self._title_label)
        self._hint = QLabel("暂无")
        self._hint.setObjectName("muted")
        layout.addWidget(self._hint)
        layout.addStretch(1)

    def set_data(self, rows, title="", highlight=""):
        """`rows` = [(标签, 数值)]；空列表显示「暂无」（不是 0 图）。"""
        self._rows = [(str(label), float(value or 0)) for label, value in (rows or [])]
        if title:
            self._title = title
            self._title_label.setText(title)
        self._highlight = str(highlight or "")
        self._hint.setText("暂无" if not self._rows else "点柱子可筛选")
        self.update()

    def label_at(self, index):
        return self._rows[index][0] if 0 <= index < len(self._rows) else ""

    def chart_rect(self):
        return self.rect().adjusted(8, 26, -8, -8)

    def index_at(self, x, y):
        """把点击坐标换算成柱序号（越界返回 -1）；测试直接用 `activate()`。"""
        area = self.chart_rect()
        if not self._rows or area.width() <= 0 or area.height() <= 0:
            return -1
        if self._horizontal:
            step = area.height() / float(len(self._rows))
            index = int((y - area.top()) / step)
        else:
            step = area.width() / float(len(self._rows))
            index = int((x - area.left()) / step)
        return index if 0 <= index < len(self._rows) else -1

    def activate(self, index):
        label = self.label_at(index)
        if label:
            self.clicked.emit(label)
        return label

    def mousePressEvent(self, event):  # pragma: no cover - 真实鼠标路径
        index = self.index_at(event.position().x(), event.position().y())
        label = self.activate(index)
        if label:
            self.setToolTip("已按「%s」筛选" % label)
        else:
            self.setToolTip("")
        super().mousePressEvent(event)

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        super().paintEvent(event)
        if not self._rows:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        area = self.chart_rect()
        top = max([value for _, value in self._rows] + [0.0]) or 1.0
        painter.setFont(QFont(theme.FONT_FAMILY, 8))
        if self._horizontal:
            step = area.height() / float(len(self._rows))
            bar_h = max(6.0, min(18.0, step - 4))
            for index, (label, value) in enumerate(self._rows):
                y = area.top() + index * step + (step - bar_h) / 2.0
                width = 0 if top <= 0 else (area.width() - 110) * (value / top)
                rect = area.adjusted(92, 0, -8, 0)
                bar = rect.__class__(rect.left(), int(y), max(2, int(width)), int(bar_h))
                color = QColor(theme.PRIMARY if label == self._highlight else theme.PRIMARY_DARK)
                if label == self._highlight:
                    color = QColor(theme.SUCCESS)
                painter.fillRect(bar, color)
                painter.setPen(QPen(QColor(theme.INK)))
                painter.drawText(area.left() + 2, int(y + bar_h - 2), _short(label, 10))
                painter.drawText(int(bar.right()) + 4, int(y + bar_h - 2), "%g" % value)
        else:
            step = area.width() / float(len(self._rows))
            bar_w = max(6.0, min(26.0, step - 6))
            for index, (label, value) in enumerate(self._rows):
                x = area.left() + index * step + (step - bar_w) / 2.0
                height = 0 if top <= 0 else (area.height() - 22) * (value / top)
                bar = area.__class__(int(x), int(area.bottom() - height), int(bar_w),
                                     max(2, int(height)))
                color = QColor(theme.PRIMARY if label != self._highlight else theme.SUCCESS)
                painter.fillRect(bar, color)
                painter.setPen(QPen(QColor(theme.INK)))
                painter.drawText(int(x) - 2, area.bottom() - 4, _short(label, 3))
                painter.drawText(int(x) - 2, int(bar.top()) - 8, "%g" % value)
        painter.end()


class Card(QFrame):
    """一个指标卡片：名称 + 数值 + 口径提示（数值全部来自快照）。"""

    def __init__(self, key, label, text, note="", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.key = key
        self.setMinimumWidth(CARD_MIN_WIDTH)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        self.label = QLabel(label)
        self.label.setObjectName("muted")
        self.value = QLabel(text)
        self.value.setObjectName("metric")
        font = QFont(theme.FONT_FAMILY, 15)
        font.setBold(True)
        self.value.setFont(font)
        layout.addWidget(self.label)
        layout.addWidget(self.value)
        if note:
            tip = QLabel(note)
            tip.setObjectName("muted")
            tip.setWordWrap(True)
            layout.addWidget(tip)
        self.setToolTip("%s\n%s" % (label, note))


class EvidenceDialog(QDialog):
    """错题证据：原始作答 + 图片清单 + 教师判断（点击错题 → 追溯）。"""

    def __init__(self, evidence, parent=None):
        super().__init__(parent)
        self.setWindowTitle("错题证据 · 作答 #%s" % evidence.get("answer_id"))
        self.evidence = evidence
        layout = QVBoxLayout(self)
        head = QLabel("%s · 第 %s 题 · %s" % (
            evidence.get("student_name") or "", evidence.get("question_no") or "",
            evidence.get("occurred_on") or "无日期"))
        theme.title(head)
        layout.addWidget(head)
        teacher = evidence.get("teacher") or {}
        info = QLabel(
            "错步位置：%s　错因：%s（%s）\n教师结论：%s　核验来源：%s\n教师依据：%s" % (
                teacher.get("locate_status") or "无",
                teacher.get("cause_label") or "未确认", teacher.get("cause_state") or "",
                teacher.get("teacher_error_text") or "无", teacher.get("verify_source") or "无",
                teacher.get("basis_note") or teacher.get("teacher_note") or "无"))
        info.setWordWrap(True)
        layout.addWidget(info)
        body = QTextEdit()
        body.setReadOnly(True)
        body.setPlainText(
            "【题目】\n%s\n\n【标准解法】\n%s\n\n【学生步骤】\n%s\n\n【错步原文】\n%s\n\n【图片】%s"
            % (evidence.get("question_text") or "",
               evidence.get("standard_solution") or "",
               evidence.get("student_steps") or "",
               teacher.get("observed_step") or "",
               "、".join("%s(#%s)" % (item.get("file_path"), item.get("seq"))
                        for item in (evidence.get("images") or [])) or "无"))
        layout.addWidget(body, 1)
        close = QPushButton("关闭")
        theme.ghost(close)
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        self.resize(720, 520)


class ClassDashboardWindow(QMainWindow):
    """班级学情看板：指标卡片 → 图表 → 热力图 → 共性问题 → 明细。"""

    def __init__(self, db, class_id, class_name="", batch="", today="", parent=None):
        super().__init__(parent)
        self.db = db
        self.class_id = int(class_id)
        self.class_name = class_name or ""
        self.today = today
        self.filters = {"batch": str(batch or ""), "date_from": "", "date_to": "",
                        "student_ids": [], "knowledge_point": "", "cause": ""}
        self.snapshot = {}
        self.cards = {}
        self.auto_exec = True   # 测试里置 False：只构造证据弹窗，不进入模态循环
        self.last_dialog = None
        self.setWindowTitle("数智伴学 · 班级学情看板")
        self._build()
        self.refresh()

    # -- 结构 ---------------------------------------------------------------
    def _build(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        head = QHBoxLayout()
        self.heading = QLabel("%s · 班级学情看板" % (self.class_name or "班级"))
        theme.title(self.heading)
        head.addWidget(self.heading)
        head.addStretch(1)
        head.addWidget(QLabel("批次"))
        self.batch_box = QComboBox()
        self.batch_box.setMinimumWidth(180)
        self.batch_box.currentIndexChanged.connect(self._on_batch_changed)
        head.addWidget(self.batch_box)
        head.addWidget(QLabel("从"))
        self.date_from = QLineEdit()
        self.date_from.setPlaceholderText("YYYY-MM-DD")
        self.date_from.setMaximumWidth(110)
        head.addWidget(self.date_from)
        head.addWidget(QLabel("到"))
        self.date_to = QLineEdit()
        self.date_to.setPlaceholderText("YYYY-MM-DD")
        self.date_to.setMaximumWidth(110)
        head.addWidget(self.date_to)
        head.addWidget(QLabel("学生"))
        self.student_box = QComboBox()
        self.student_box.setMinimumWidth(120)
        self.student_box.currentIndexChanged.connect(self._on_student_changed)
        head.addWidget(self.student_box)
        head.addWidget(QLabel("知识点"))
        self.kp_box = QComboBox()
        self.kp_box.setMinimumWidth(110)
        self.kp_box.currentIndexChanged.connect(self._on_kp_box_changed)
        head.addWidget(self.kp_box)
        head.addWidget(QLabel("错因"))
        self.cause_box = QComboBox()
        self.cause_box.setMinimumWidth(110)
        self.cause_box.currentIndexChanged.connect(self._on_cause_box_changed)
        head.addWidget(self.cause_box)
        self.apply_button = QPushButton("刷新")
        theme.primary(self.apply_button)
        self.apply_button.clicked.connect(self.refresh)
        head.addWidget(self.apply_button)
        self.clear_button = QPushButton("清除筛选")
        theme.ghost(self.clear_button)
        self.clear_button.clicked.connect(self.clear_filters)
        head.addWidget(self.clear_button)
        outer.addLayout(head)

        self.status = QLabel("正在加载学情…")
        self.status.setObjectName("muted")
        outer.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)

        self.card_grid = QGridLayout()
        self.card_grid.setSpacing(6)
        self.body_layout.addLayout(self.card_grid)

        charts = QGridLayout()
        charts.setSpacing(8)
        self.kp_chart = BarChart("知识点错误频次（横向柱状）", horizontal=True, height=170)
        self.kp_chart.clicked.connect(self._on_knowledge_clicked)
        self.cause_chart = BarChart("错因频次（条形）", horizontal=True, height=170)
        self.cause_chart.clicked.connect(self._on_cause_clicked)
        self.question_chart = BarChart("各题错误数（柱状）", horizontal=False, height=170)
        charts.addWidget(self.kp_chart, 0, 0)
        charts.addWidget(self.cause_chart, 0, 1)
        charts.addWidget(self.question_chart, 0, 2)
        self.body_layout.addLayout(charts)

        self.rate_chart = BarChart("各题得分率（柱状）", horizontal=False, height=150)
        self.distribution_chart = BarChart("完整成绩分布", horizontal=False, height=150)
        rate_row = QHBoxLayout()
        rate_row.setSpacing(8)
        rate_row.addWidget(self.rate_chart)
        rate_row.addWidget(self.distribution_chart)
        self.body_layout.addLayout(rate_row)

        heat_title = QLabel("学生 × 知识点热力图（以已确认错误次数为据，点格子可筛）")
        theme.section(heat_title)
        self.body_layout.addWidget(heat_title)
        self.heatmap = QTableWidget(0, 0)
        self.heatmap.setObjectName("table")
        self.heatmap.setMinimumHeight(200)
        self.heatmap.cellClicked.connect(self._on_heat_clicked)
        self.body_layout.addWidget(self.heatmap)

        issue_title = QLabel("班级共性问题（按涉及学生人数排序，频次辅助）")
        theme.section(issue_title)
        self.body_layout.addWidget(issue_title)
        self.issue_table = QTableWidget(0, 4)
        self.issue_table.setObjectName("table")
        self.issue_table.setHorizontalHeaderLabels(["知识点", "涉及学生", "错题数", "学生"])
        self.issue_table.setMinimumHeight(150)
        self.issue_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.body_layout.addWidget(self.issue_table)

        detail_title = QLabel("可筛选明细（双击一行追溯原始作答与证据；点学生列打开个人看板）")
        theme.section(detail_title)
        self.body_layout.addWidget(detail_title)
        self.detail_table = QTableWidget(0, 8)
        self.detail_table.setObjectName("table")
        self.detail_table.setHorizontalHeaderLabels(
            ["学生", "题号", "知识点", "错因", "日期", "错步原文", "已评得分", "图片"])
        self.detail_table.setMinimumHeight(220)
        self.detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.detail_table.cellDoubleClicked.connect(self._on_detail_double_clicked)
        self.detail_table.cellClicked.connect(self._on_detail_clicked)
        self.body_layout.addWidget(self.detail_table)

        self.notes_label = QLabel("")
        self.notes_label.setObjectName("muted")
        self.notes_label.setWordWrap(True)
        self.body_layout.addWidget(self.notes_label)
        self.body_layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.setCentralWidget(central)
        self.resize(1280, 800)

    # -- 数据 ---------------------------------------------------------------
    def _batch_options(self):
        try:
            return r2_stats.list_batches(self.db, self.class_id)
        except Exception:  # noqa: BLE001 - 旧库没有 R2 表时不要让看板打不开
            return []

    def _sync_combos(self, snapshot):
        options = self._batch_options()
        wanted = self.filters.get("batch") or ""
        self.batch_box.blockSignals(True)
        self.batch_box.clear()
        self.batch_box.addItem("全部批次（不跨批次相加）", "")
        for item in options:
            label = item["name"] or item["batch"]
            if item.get("config_state") != "configured":
                label += "（未配置）"
            if item.get("business_date"):
                label += " · %s" % item["business_date"]
            self.batch_box.addItem(label, item["batch"])
        index = self.batch_box.findData(wanted)
        self.batch_box.setCurrentIndex(index if index >= 0 else 0)
        self.batch_box.blockSignals(False)

        self.student_box.blockSignals(True)
        self.student_box.clear()
        self.student_box.addItem("全部学生", 0)
        for item in snapshot.get("students") or []:
            self.student_box.addItem("%s（%s）" % (item["student_name"], item["student_key"]),
                                     item["student_id"])
        current = (self.filters.get("student_ids") or [0])[0]
        index = self.student_box.findData(current)
        self.student_box.setCurrentIndex(index if index >= 0 else 0)
        self.student_box.blockSignals(False)

        for box, key, values in (
                (self.kp_box, "knowledge_point",
                 [item["name"] for item in snapshot["distributions"]["knowledge_points"]]),
                (self.cause_box, "cause",
                 [item["name"] for item in snapshot["distributions"]["causes"]])):
            box.blockSignals(True)
            box.clear()
            box.addItem("全部", "")
            for value in values:
                box.addItem(value, value)
            index = box.findData(self.filters.get(key) or "")
            box.setCurrentIndex(index if index >= 0 else 0)
            box.blockSignals(False)

    def _build_filters(self):
        """筛选状态唯一来源是 `self.filters`；下拉/图表点击都先写这里再 refresh。"""
        self.filters["date_from"] = self.date_from.text().strip()
        self.filters["date_to"] = self.date_to.text().strip()
        batch = self.filters.get("batch") or ""
        filters = {
            "date_from": self.filters["date_from"],
            "date_to": self.filters["date_to"],
            "knowledge_point": self.filters.get("knowledge_point") or "",
            "cause": self.filters.get("cause") or "",
            "student_ids": list(self.filters.get("student_ids") or []),
        }
        return batch, filters

    def refresh(self):
        batch, filters = self._build_filters()
        self.snapshot = r2_stats.class_snapshot(
            self.db, self.class_id, batch=batch, filters=filters, today=self.today)
        self.filters["batch"] = batch
        self._sync_combos(self.snapshot)
        self._fill_cards()
        self._fill_charts()
        self._fill_heatmap()
        self._fill_issues()
        self._fill_detail()
        notes = self.snapshot.get("notes") or []
        scope = self.snapshot["scope"]
        self.status.setText("统计范围：%s　记录 %d 条　统计版本 %s" % (
            scope.get("filter_text") or "全班", scope.get("row_count") or 0,
            self.snapshot.get("stats_version") or ""))
        self.notes_label.setText("　".join(notes))

    def clear_filters(self):
        self.filters.update({"date_from": "", "date_to": "", "student_ids": [],
                             "knowledge_point": "", "cause": ""})
        self.date_from.setText("")
        self.date_to.setText("")
        self.refresh()

    # -- 渲染 ---------------------------------------------------------------
    def _fill_cards(self):
        while self.card_grid.count():
            item = self.card_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards = {}
        for index, card in enumerate(self.snapshot.get("cards") or []):
            widget = Card(card["key"], card["label"], card["text"],
                          card.get("scope_note") or "")
            self.cards[card["key"]] = widget
            self.card_grid.addWidget(widget, index // 6, index % 6)

    def _fill_charts(self):
        dist = self.snapshot["distributions"]
        self.kp_chart.set_data([(item["name"], item["mistakes"])
                                for item in dist["knowledge_points"]],
                               highlight=self.filters.get("knowledge_point") or "")
        self.cause_chart.set_data([(item["name"], item["mistakes"])
                                   for item in dist["causes"]],
                                  highlight=self.filters.get("cause") or "")
        self.question_chart.set_data([("%s题" % item["question_no"], item["mistakes"])
                                      for item in dist["question_errors"]])
        self.rate_chart.set_data([("%s题" % item["question_no"], item.get("rate_percent") or 0.0)
                                  for item in self.snapshot["score"]["question_rates"]])
        self.distribution_chart.set_data(
            [(item["label"], item["count"])
             for item in self.snapshot["score"]["distribution"]["buckets"]])

    def _fill_heatmap(self):
        heat = self.snapshot["distributions"]["heatmap"]
        points = heat["points"]
        rows = heat["rows"]
        self.heatmap.clear()
        self.heatmap.setColumnCount(len(points))
        self.heatmap.setRowCount(len(rows))
        self.heatmap.setHorizontalHeaderLabels(points)
        self.heatmap.setVerticalHeaderLabels(
            ["%s（%s）" % (row["student_name"], row["student_key"]) for row in rows])
        top = max(1, int(heat.get("max_count") or 1))
        for row_index, row in enumerate(rows):
            for col_index, cell in enumerate(row["cells"]):
                item = QTableWidgetItem(str(cell["count"]) if cell["count"] else "")
                item.setData(Qt.UserRole, (row["student_key"], cell["point"]))
                item.setTextAlignment(Qt.AlignCenter)
                if cell["count"]:
                    shade = int(255 - 150.0 * (cell["count"] / float(top)))
                    item.setBackground(QColor(46, 107, 214, max(60, 255 - shade)))
                self.heatmap.setItem(row_index, col_index, item)

    def _fill_issues(self):
        issues = self.snapshot["rankings"]["class_common_issues"]
        self.issue_table.setRowCount(len(issues))
        for row, item in enumerate(issues):
            values = [item["name"], str(item["student_count"]), str(item["mistakes"]),
                      "、".join(item["students"])]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if col == 0:
                    cell.setData(Qt.UserRole, item["name"])
                self.issue_table.setItem(row, col, cell)

    def _fill_detail(self):
        rows = self.snapshot["mistakes"]
        self.detail_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [row["student_name"] or row["student_key"], row["question_no"],
                      row["knowledge_point_text"], row["cause_label"] or row["cause_code"],
                      row["occurred_on"] or "无日期", _short(row["observed_step"], 24),
                      "" if row["score"] is None else ("%g" % row["score"]),
                      str(row["images"] or "")]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, row)
                self.detail_table.setItem(index, col, cell)

    # -- 交互 ---------------------------------------------------------------
    def _on_batch_changed(self, _index):
        self.filters["batch"] = self.batch_box.currentData() or ""
        self.refresh()

    def _on_student_changed(self, _index):
        student_id = self.student_box.currentData() or 0
        self.filters["student_ids"] = [int(student_id)] if student_id else []
        self.refresh()

    def _on_kp_box_changed(self, _index):
        self.filters["knowledge_point"] = self.kp_box.currentData() or ""
        self.refresh()

    def _on_cause_box_changed(self, _index):
        self.filters["cause"] = self.cause_box.currentData() or ""
        self.refresh()

    def _on_knowledge_clicked(self, name):
        self.filters["knowledge_point"] = name
        self.refresh()

    def _on_cause_clicked(self, name):
        self.filters["cause"] = name
        self.refresh()

    def _on_heat_clicked(self, row, column):
        item = self.heatmap.item(row, column)
        if item is None:
            return
        payload = item.data(Qt.UserRole)
        if not payload:
            return
        student_key, point = payload
        for entry in self.snapshot.get("students") or []:
            if entry["student_key"] == student_key:
                self.filters["student_ids"] = [entry["student_id"]]
                break
        self.filters["knowledge_point"] = point
        self.refresh()

    def _on_detail_clicked(self, row, column):
        item = self.detail_table.item(row, column)
        if item is None or column != 0:
            return
        payload = item.data(Qt.UserRole) or {}
        self.open_student(payload.get("student_id"))

    def _on_detail_double_clicked(self, row, _column):
        item = self.detail_table.item(row, 0)
        payload = item.data(Qt.UserRole) if item else None
        if not payload:
            return
        evidence = r2_stats.mistake_evidence(self.db, self.class_id, payload["answer_id"])
        if not evidence:
            return None
        dialog = EvidenceDialog(evidence, self)
        self.last_dialog = dialog
        if self.auto_exec:
            dialog.exec()
        return dialog

    # -- 打开个人看板（个人学情） -------------------------------------------
    def open_student(self, student_id):
        if not student_id:
            return None
        window = StudentDashboardWindow(self.db, self.class_id, int(student_id),
                                        batch=self.filters.get("batch") or "",
                                        today=self.today, parent=None)
        self.child_windows = getattr(self, "child_windows", [])
        self.child_windows.append(window)  # 由打开方持有，避免被 GC
        window.show()
        return window


class StudentDashboardWindow(QMainWindow):
    """个人学情看板（派单 §5.4）：分布 / 重复错误 / 历史 / 重点问题 / 建议 / 有效成绩。"""

    def __init__(self, db, class_id, student_id, batch="", today="", parent=None):
        super().__init__(parent)
        self.db = db
        self.class_id = int(class_id)
        self.student_id = int(student_id)
        self.batch = str(batch or "")
        self.today = today
        self.snapshot = {}
        self.auto_exec = True
        self.last_dialog = None
        self.setWindowTitle("数智伴学 · 个人学情")
        self._build()
        self.refresh()

    def _build(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)
        self.heading = QLabel("个人学情")
        theme.title(self.heading)
        outer.addWidget(self.heading)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        outer.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.card_grid = QGridLayout()
        self.card_grid.setSpacing(6)
        layout.addLayout(self.card_grid)

        self.kp_chart = BarChart("个人知识点错误分布", horizontal=True, height=150)
        self.cause_chart = BarChart("个人错因分布", horizontal=True, height=150)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.kp_chart)
        row.addWidget(self.cause_chart)
        layout.addLayout(row)

        theme.section(QLabel("重复错误明细"))
        self.repeat_table = QTableWidget(0, 3)
        self.repeat_table.setObjectName("table")
        self.repeat_table.setHorizontalHeaderLabels(["类别", "重复项", "次数"])
        self.repeat_table.setMinimumHeight(120)
        self.repeat_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.repeat_table)

        theme.section(QLabel("重点问题排序（复用原版近期加权：越近权重越高）"))
        self.priority_table = QTableWidget(0, 3)
        self.priority_table.setObjectName("table")
        self.priority_table.setHorizontalHeaderLabels(["知识点", "加权分", "错题数"])
        self.priority_table.setMinimumHeight(120)
        self.priority_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.priority_table)

        theme.section(QLabel("有效个人成绩"))
        self.score_table = QTableWidget(0, 2)
        self.score_table.setObjectName("table")
        self.score_table.setHorizontalHeaderLabels(["项目", "值"])
        self.score_table.setMinimumHeight(140)
        self.score_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.score_table)

        theme.section(QLabel("历史记录（已确认错题，按日期倒序）"))
        self.history_table = QTableWidget(0, 3)
        self.history_table.setObjectName("table")
        self.history_table.setHorizontalHeaderLabels(["日期", "题号", "知识点"])
        self.history_table.setMinimumHeight(140)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.history_table)

        theme.section(QLabel("已保存的辅导建议"))
        self.advice_label = QLabel("暂无已保存的辅导建议")
        self.advice_label.setWordWrap(True)
        layout.addWidget(self.advice_label)

        theme.section(QLabel("个人错题明细（双击追溯证据）"))
        self.detail_table = QTableWidget(0, 5)
        self.detail_table.setObjectName("table")
        self.detail_table.setHorizontalHeaderLabels(["题号", "知识点", "错因", "日期", "错步原文"])
        self.detail_table.setMinimumHeight(160)
        self.detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.detail_table.cellDoubleClicked.connect(self._open_evidence)
        layout.addWidget(self.detail_table)
        layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.setCentralWidget(central)
        self.resize(1180, 760)

    def refresh(self):
        self.snapshot = r2_stats.student_snapshot(
            self.db, self.class_id, self.student_id, batch=self.batch, today=self.today)
        scope = self.snapshot["scope"]
        student_name = ""
        for row in self.snapshot.get("students") or []:
            if int(row["student_id"]) == self.student_id:
                student_name = row["student_name"]
        self.heading.setText("%s · %s 的个人学情" % (scope.get("class_name") or "",
                                                 student_name or "学生"))
        self.status.setText("统计范围：%s　批次：%s" % (
            scope.get("filter_text") or "个人", scope.get("batch_name") or "全部"))
        while self.card_grid.count():
            item = self.card_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, card in enumerate(self.snapshot.get("cards") or []):
            self.card_grid.addWidget(
                Card(card["key"], card["label"], card["text"], card.get("scope_note") or ""),
                index // 6, index % 6)
        dist = self.snapshot["distributions"]
        self.kp_chart.set_data([(item["name"], item["mistakes"])
                                for item in dist["knowledge_points"]])
        self.cause_chart.set_data([(item["name"], item["mistakes"])
                                   for item in dist["causes"]])
        self._fill_repeats()
        self._fill_priority()
        self._fill_scores()
        self._fill_history()
        self._fill_advice()
        self._fill_detail()

    def _fill_repeats(self):
        repeated = self.snapshot["metrics"]["repeated_errors"]["value"]
        rows = [("知识点重复", item["category"], item["count"])
                for item in repeated.get("knowledge_pairs") or []]
        rows += [("错因重复", item["category"], item["count"])
                 for item in repeated.get("cause_pairs") or []]
        self.repeat_table.setRowCount(len(rows))
        for index, values in enumerate(rows):
            for col, text in enumerate(values):
                self.repeat_table.setItem(index, col, QTableWidgetItem(str(text)))

    def _fill_priority(self):
        items = self.snapshot["rankings"].get("student_priority_issues") or []
        self.priority_table.setRowCount(len(items))
        for index, item in enumerate(items):
            values = [item["name"], str(item["weight"]), str(item["count"])]
            for col, text in enumerate(values):
                self.priority_table.setItem(index, col, QTableWidgetItem(str(text)))

    def _fill_scores(self):
        totals = self.snapshot["metrics"]["assignment_total_score"]
        average = self.snapshot["metrics"]["average_score"]
        coverage = self.snapshot["metrics"]["score_coverage"]
        rows = [("个人作业总分（有效确认得分之和）", totals.get("text") or "暂无"),
                ("平均分（仅完整成绩）", average.get("text") or "暂无"),
                ("评分覆盖率", coverage.get("text") or "无法计算")]
        rates = self.snapshot["score"]["question_rates"]
        rows.append(("各题得分率", "；".join("%s题 %s（已评 %d）" % (
            item["question_no"], item["rate_text"], item["scored_count"]) for item in rates) or "暂无"))
        self.score_table.setRowCount(len(rows))
        for index, values in enumerate(rows):
            for col, text in enumerate(values):
                self.score_table.setItem(index, col, QTableWidgetItem(str(text)))

    def _fill_history(self):
        history = r2_stats.student_history(self.db, self.class_id, self.student_id)
        self.history_table.setRowCount(len(history))
        for index, item in enumerate(history):
            values = [item["occurred_on"] or "无日期", item["question_no"] or "",
                      "、".join(item["knowledge_points"]) or "未分类"]
            for col, text in enumerate(values):
                self.history_table.setItem(index, col, QTableWidgetItem(str(text)))

    def _fill_advice(self):
        advice = r2_stats.saved_advice(self.db, self.class_id, self.student_id)
        if not advice["exists"]:
            self.advice_label.setText("暂无已保存的辅导建议")
            return
        flag = "（待复核：生成后错题有变化）" if advice["stale"] else ""
        self.advice_label.setText("%s%s\n%s" % (
            advice["generated_at"] or "已保存", flag, advice["content"] or ""))

    def _fill_detail(self):
        rows = self.snapshot["mistakes"]
        self.detail_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [row["question_no"], row["knowledge_point_text"],
                      row["cause_label"] or row["cause_code"], row["occurred_on"] or "无日期",
                      _short(row["observed_step"], 28)]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, row["answer_id"])
                self.detail_table.setItem(index, col, cell)

    def _open_evidence(self, row, _column):
        item = self.detail_table.item(row, 0)
        if item is None:
            return
        evidence = r2_stats.mistake_evidence(self.db, self.class_id, item.data(Qt.UserRole))
        if not evidence:
            return None
        dialog = EvidenceDialog(evidence, self)
        self.last_dialog = dialog
        if self.auto_exec:
            dialog.exec()
        return dialog


__all__ = ["BarChart", "Card", "EvidenceDialog", "ClassDashboardWindow",
           "StudentDashboardWindow"]
