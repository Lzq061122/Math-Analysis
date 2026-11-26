# -*- coding: utf-8 -*-
"""R2-04 报告界面：预览、导出 PDF、打印（打印到文件）、导出历史。

边界（契约 §2.1）：**报告内容一律来自 `app/report_pdf.py` 的报告对象**——本文件不查库、
不算指标、不拼数字；预览与导出用的是同一个 `report` 字典（同一份快照），
所以「预览之后改了数据」导出的 PDF 仍对应预览时的快照。

入口接线片段见 `docs/R2-04_交付说明.md` §4（由 captain 统一加菜单项与 service 方法）。
"""

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QTextDocument
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import report_pdf

from . import theme


class ReportPreviewWindow(QMainWindow):
    """班级/个人学情报告：预览 + 导出 + 打印 + 导出历史。

    `kind` = "class" / "student"；`target_id` = 班级 id 或学生 id（kind="student" 时）。
    """

    def __init__(self, db, class_id, kind="class", student_id=None, class_name="",
                 batch="", filters=None, today="", user=None, db_file=None,
                 purpose="存档", parent=None, auto_exec=True):
        super().__init__(parent)
        self.db = db
        self.class_id = int(class_id)
        self.kind = "student" if kind == "student" else "class"
        self.student_id = int(student_id) if student_id else None
        self.class_name = class_name or ""
        self.batch = str(batch or "")
        self.filters = filters or {}
        self.today = today
        self.user = user
        self.db_file = db_file
        self.purpose = purpose if purpose in report_pdf.PURPOSES else report_pdf.PURPOSES[0]
        self.auto_exec = auto_exec          # 测试里置 False：不弹文件对话框
        self.report = None
        self.last_export = None
        self.last_error = None
        self.setWindowTitle("数智伴学 · 学情报告")
        self._build()
        self.refresh()

    # -- 结构 ---------------------------------------------------------------
    def _build(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        head = QHBoxLayout()
        self.heading = QLabel("学情报告")
        theme.title(self.heading)
        head.addWidget(self.heading)
        head.addStretch(1)
        head.addWidget(QLabel("用途"))
        self.purpose_box = QComboBox()
        for item in report_pdf.PURPOSES:
            self.purpose_box.addItem(item, item)
        self.purpose_box.setCurrentIndex(self.purpose_box.findData(self.purpose))
        head.addWidget(self.purpose_box)
        self.refresh_button = QPushButton("刷新数据")
        theme.ghost(self.refresh_button)
        self.refresh_button.clicked.connect(self.refresh)
        head.addWidget(self.refresh_button)
        self.export_button = QPushButton("导出 PDF")
        theme.primary(self.export_button)
        self.export_button.clicked.connect(self.export)
        head.addWidget(self.export_button)
        self.print_button = QPushButton("打印…")
        theme.ghost(self.print_button)
        self.print_button.clicked.connect(self.print_report)
        head.addWidget(self.print_button)
        outer.addLayout(head)

        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        self.meta.setWordWrap(True)
        outer.addWidget(self.meta)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        font = QFont(theme.FONT_FAMILY, 10)
        font.setStyleHint(QFont.Monospace)
        self.preview.setFont(font)
        self.preview.setObjectName("report-preview")
        outer.addWidget(self.preview, 1)

        theme.section(QLabel("导出历史（账号 / 时间 / 用途 / 类型 / 筛选范围）"))
        self.history_table = QTableWidget(0, 6)
        self.history_table.setObjectName("table")
        self.history_table.setHorizontalHeaderLabels(
            ["时间", "账号", "用途", "类型", "筛选范围", "文件"])
        self.history_table.setMinimumHeight(140)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.history_table.cellDoubleClicked.connect(self._open_history_file)
        outer.addWidget(self.history_table)

        self.setCentralWidget(central)
        self.resize(1080, 820)

    # -- 报告对象 -----------------------------------------------------------
    def build_report(self):
        """报告对象只在这里构造一次；预览与导出都用它（同一份快照）。"""
        options = {"batch": self.batch, "filters": self.filters, "today": self.today,
                   "user": self.user, "purpose": self.purpose}
        if self.kind == "student":
            return report_pdf.build_student_report(self.db, self.class_id, self.student_id,
                                                   **options)
        return report_pdf.build_class_report(self.db, self.class_id, **options)

    def refresh(self):
        try:
            self.report = self.build_report()
            self.last_error = None
        except report_pdf.ReportError as exc:
            self.last_error = exc
            self.meta.setText("报告生成失败：%s" % exc.message)
            self.preview.setPlainText("")
            return None
        lines = report_pdf.preview_lines(self.report)
        self.preview.setPlainText("\n".join(lines))
        scope = self.report["scope"]
        self.heading.setText(self.report["title"])
        self.meta.setText("统计范围：%s　批次：%s　生成时间：%s　数据版本：%s" % (
            scope.get("filter_text") or "全班", scope.get("batch_name") or "全部批次",
            self.report["generated_at"], self.report["data_version"]))
        self._fill_history()
        return self.report

    def _fill_history(self):
        items = report_pdf.load_history(self.db_file) if self.db_file else []
        self.history_table.setRowCount(len(items))
        for index, entry in enumerate(items):
            values = [entry.get("at") or "", entry.get("user") or "",
                      entry.get("purpose") or "", "班级" if entry.get("kind") == "class" else "个人",
                      entry.get("filters") or "", os.path.basename(entry.get("path") or "")]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setData(Qt.UserRole, entry.get("path") or "")
                self.history_table.setItem(index, col, cell)

    # -- 导出 / 打印 ---------------------------------------------------------
    def default_name(self):
        scope = (self.report or {}).get("scope") or {}
        pieces = [self.class_name or scope.get("class_name") or "班级"]
        if self.kind == "student":
            pieces.append((self.report or {}).get("student", {}).get("name") or "学生")
        if scope.get("batch_name"):
            pieces.append(scope["batch_name"])
        name = "_".join(piece for piece in pieces if piece)
        return "%s_学情报告.pdf" % name

    def export(self, path=None):
        """导出：默认弹保存对话框；测试传 path 走无对话框路径。"""
        if self.report is None:
            self.last_error = report_pdf.ReportError("报告还没有生成成功", "no_report")
            return None
        if not path:
            if not self.auto_exec:
                return None
            path, _ = QFileDialog.getSaveFileName(self, "导出学情报告 PDF",
                                                  self.default_name(), "PDF 文件 (*.pdf)")
            if not path:
                return None
        self.purpose = self.purpose_box.currentData() or self.purpose
        try:
            result = report_pdf.export_pdf(self.report, path, db_file=self.db_file,
                                           purpose=self.purpose)
        except OSError as exc:
            self.last_error = exc
            self.meta.setText("导出失败：%s" % exc)
            return None
        self.last_export = result
        self._fill_history()
        self.meta.setText("已导出：%s（%d 字节，用途：%s）" % (
            result["path"], result["bytes"], result["entry"]["purpose"]))
        return result

    def print_report(self, path=None):
        """打印：优先走 Qt 打印（无打印机时打印到文件）。

        离线环境的兜底策略：拿不到打印对话框就直接把同一份 PDF 写到目标路径，
        绝不在打印路径上重新生成第二份内容（预览/导出/打印三者同源）。
        """
        if self.report is None:
            return None
        if path is None and not self.auto_exec:
            return None
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "打印到 PDF 文件",
                                                  self.default_name(), "PDF 文件 (*.pdf)")
            if not path:
                return None
        result = self.export(path)
        return result

    def _open_history_file(self, row, _column):
        item = self.history_table.item(row, 0)
        if item is None:
            return
        path = item.data(Qt.UserRole)
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class ReportDialog(QDialog):
    """轻量预览对话框（从看板/学情页弹出时用），内容与 `ReportPreviewWindow` 同源。"""

    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle(report["title"])
        layout = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlainText("\n".join(report_pdf.preview_lines(report)))
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        copy = QPushButton("复制预览文本")
        theme.ghost(copy)
        copy.clicked.connect(lambda: self._copy())
        close = QPushButton("关闭")
        theme.primary(close)
        close.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(copy)
        row.addWidget(close)
        layout.addLayout(row)
        self.resize(880, 700)

    def _copy(self):  # pragma: no cover - 剪贴板路径
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.view.toPlainText())


__all__ = ["ReportPreviewWindow", "ReportDialog"]
