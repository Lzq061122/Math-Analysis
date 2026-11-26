"""R2-01 压缩包批量导入窗口（PySide6）。

与 R1 的 `AnswerImportWindow`（`src/desktop/ui.py`）**并存**：那边是 CSV，这边是
`assignment.zip{answers.csv, images/}`。窗口本身不做业务判断，一律调用 `app.answer_import_zip`
（预览/提交/模板），所以「限制、分类、回滚」的口径只有一处实现，界面不会与后端漂移。

三条交互约定（与 R1 保存错步页保持同一套习惯）：
1. **先预览后导入**：选包后立刻在同一页给出逐行分类；「确认导入」按钮只在有可导入行时可用。
2. **冲突由老师决定**：默认「跳过冲突」；勾选「覆盖编号相同、内容不同的行」才替换，
   且会在确认框里写明将被覆盖多少条。
3. **失败留在原页**：包不合格（超限/加密/路径非法等）时只显示原因、不复位已选文件；
   导入失败保留预览内容，提示可重试。

接线（由 captain 在 `src/desktop/ui.py` 与 `src/desktop/service.py` 里注册）：
    from desktop.ui_import_zip import ZipImportWindow
    self._zip_import_window = ZipImportWindow(self.service, self.class_row, parent=self)
    self._zip_import_window.show()
service 需要三个方法（签名见 docs/R2-01_交付说明.md §5）：
    preview_zip_answers(class_id, data, batch="", selections=None) -> dict
    import_zip_answers(class_id, data, batch="", selections=None, overwrite=False) -> dict
    zip_import_template() -> bytes
"""

import csv
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app import answer_import_zip as zip_mod


class _InlineRunner:
    """没有 `desktop.ui.TaskRunner` 时的退路：同步执行（预览/提交都是本地操作）。"""

    def run(self, work, done, failed):
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 — 界面层只负责把错误原样交给 failed
            failed(str(getattr(exc, "message", exc)))
            return
        done(result)


def _runner():
    """优先用仓库既有的后台执行器；拿不到就同步跑。

    延迟导入的原因：`ui.py` 里全是业务界面，测试时要能单独 import 本模块而不连带拉起整页；
    真机上 `ui.py` 一定已经加载，所以延迟 import 不会多付代价。
    """
    try:
        from desktop.ui import TaskRunner
    except Exception:  # noqa: BLE001 — 拿不到就跑同步，功能不受影响
        return _InlineRunner()
    return TaskRunner()


class ZipImportWindow(QMainWindow):
    """按班级导入一份作答压缩包：本地校验 → 老师确认 → 写入。"""

    closed = Signal()

    COLUMNS = ["行", "分类", "记录编号", "学生", "题号", "图片", "状态", "说明"]

    def __init__(self, service, class_row, parent=None):
        super().__init__(parent)
        self.service = service
        self.class_row = class_row or {}
        self.class_id = self.class_row.get("id")
        self.runner = _runner()
        self._data = b""
        self._name = ""
        self._batch = ""
        self._rows = []
        self._preview = None
        self._result = None

        self.setObjectName("zipImportWindow")
        self.setWindowTitle("数智伴学 · 导入作答（压缩包）")
        self.resize(1080, 720)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · 导入作答压缩包" % self.class_row.get("name", ""))
        heading.setObjectName("zipImportClassName")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.back_button = QPushButton("返回")
        self.back_button.setObjectName("zipImportBack")
        self.back_button.clicked.connect(self.close)
        top = QHBoxLayout()
        top.addWidget(heading, 1)
        top.addWidget(self.back_button)

        self.file_label = QLineEdit()
        self.file_label.setObjectName("zipImportFileLabel")
        self.file_label.setReadOnly(True)
        self.file_label.setPlaceholderText("还没有选择压缩包（assignment.zip）")
        self.choose_button = QPushButton("选择压缩包…")
        self.choose_button.setObjectName("zipImportChoose")
        self.choose_button.clicked.connect(self.choose_zip)
        self.template_button = QPushButton("下载模板")
        self.template_button.setObjectName("zipImportTemplate")
        self.template_button.clicked.connect(self.save_template)
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("压缩包"))
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.choose_button)
        file_row.addWidget(self.template_button)

        self.hint = QLabel(
            "压缩包里放 answers.csv 与 images/ 下的图片（JPG/JPEG/PNG）。先在本地核对每一行，"
            "再点「确认导入」。整个包只在本机读取：不调用 AI、不发送学生姓名或作答内容；"
            "单包上限 100 MB、解压后 300 MB、最多 2000 个文件、单图 10 MB。"
        )
        self.hint.setObjectName("zipImportHint")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #555;")

        self.summary = QLabel("")
        self.summary.setObjectName("zipImportSummary")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: #333;")

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setObjectName("zipImportTable")
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        header = self.table.horizontalHeader()
        for index in range(len(self.COLUMNS) - 1):
            header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(self.COLUMNS) - 1, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.itemSelectionChanged.connect(self._sync_buttons)

        self.overwrite_check = QCheckBox("覆盖编号相同、内容不同的行")
        self.overwrite_check.setObjectName("zipImportOverwrite")
        self.overwrite_check.stateChanged.connect(self._sync_buttons)
        self.import_button = QPushButton("确认导入")
        self.import_button.setObjectName("zipImportConfirm")
        self.import_button.clicked.connect(self.confirm_import)
        self.export_button = QPushButton("导出问题明细")
        self.export_button.setObjectName("zipImportExport")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_problems)
        self.status = QLabel("")
        self.status.setObjectName("zipImportStatus")
        self.status.setWordWrap(True)
        actions = QHBoxLayout()
        actions.addWidget(self.overwrite_check)
        actions.addWidget(self.import_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.status, 1)

        self.report = QPlainTextEdit()
        self.report.setObjectName("zipImportReport")
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("导入结果会显示在这里：新增、覆盖、跳过重复、冲突、文件异常。")
        self.report.setMaximumHeight(150)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(file_row)
        layout.addWidget(self.hint)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(self.report)
        self._sync_buttons()

    # ---------------------------------------------------------------- 文件
    def choose_zip(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择作答压缩包", os.path.dirname(self._name) or "",
            "压缩包 (*.zip);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            self.status.setText("读不了这个文件：%s" % exc)
            return
        self.load_package(data, os.path.splitext(os.path.basename(path))[0][:60], path)

    def load_package(self, data, batch="", path=""):
        """选包后的统一入口（测试直接调它，不经过文件对话框）。

        `batch` 为空时用压缩包文件名；`path` 只用于界面显示。
        """
        self._data = data
        self._name = path or (batch + ".zip" if batch else "")
        self._batch = (batch or "").strip()
        self._rows = []
        self._preview = None
        self._result = None
        self.file_label.setText(self._name)
        self.report.clear()
        self.table.setRowCount(0)
        self.preview()

    def save_template(self, path=""):
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "保存压缩包模板", "作答导入模板.zip",
                                                  "压缩包 (*.zip)")
        if not path:
            return ""
        try:
            with open(path, "wb") as handle:
                handle.write(zip_mod.template_zip())
        except OSError as exc:
            self.status.setText("保存失败：%s" % exc)
            return ""
        self.status.setText("模板已保存：%s" % path)
        return path

    # ---------------------------------------------------------------- 后端调用
    def _call(self, name, *args, **kwargs):
        """调 service 的方法；service 还没接线时退回直接调 app 模块（口径同一处实现）。

        除 `zip_import_template` 外，service 方法的第一个参数都是 `class_id`，
        这里统一补上，避免窗口与 service 各自记一套参数顺序。
        """
        method = getattr(self.service, name, None)
        if callable(method):
            if name == "zip_import_template":
                return method()
            return method(self.class_id, *args, **kwargs)
        db = getattr(self.service, "db", None)
        uploads = getattr(self.service, "uploads", None)
        if name == "preview_zip_answers":
            return zip_mod.preview(db, self.class_id, *args, **kwargs)
        if name == "import_zip_answers":
            kwargs.setdefault("uploads", uploads)
            return zip_mod.commit(db, self.class_id, *args, **kwargs)
        if name == "zip_import_template":
            return zip_mod.template_zip()
        raise AttributeError(name)

    # ---------------------------------------------------------------- 预览
    def preview(self):
        if not self._data:
            return
        self.choose_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.status.setText("正在本地校验压缩包…")
        data, batch = self._data, self._batch
        self.runner.run(lambda: self._call("preview_zip_answers", data, batch),
                        self.show_preview, self._preview_failed)

    def _preview_failed(self, message):
        self.choose_button.setEnabled(True)
        self._rows = []
        self.table.setRowCount(0)
        self.summary.setText("")
        self.status.setText(message)
        self._sync_buttons()

    def show_preview(self, data):
        self.choose_button.setEnabled(True)
        self._preview = data
        counts = data.get("counts") or {}
        package = data.get("package") or {}
        self.summary.setText(
            "压缩包：%s · %d 个文件 · 解压后 %d 字节 · 图片 %d 张"
            % (package.get("csv_name") or "answers.csv", package.get("files", 0),
               package.get("uncompressed_bytes", 0), package.get("image_count", 0)))
        self._fill_rows(data.get("rows") or [])
        self.status.setText(
            "共 %d 行：有效 %d，缺测 %d，重复 %d，冲突 %d，未匹配学生 %d，文件异常 %d"
            "（其中内容不足 %d 行）"
            % (data.get("total", 0),
               counts.get(zip_mod.CATEGORY_VALID, 0), counts.get(zip_mod.CATEGORY_MISSING, 0),
               counts.get(zip_mod.CATEGORY_DUPLICATE, 0), counts.get(zip_mod.CATEGORY_CONFLICT, 0),
               counts.get(zip_mod.CATEGORY_UNMATCHED, 0), counts.get(zip_mod.CATEGORY_FILE_ERROR, 0),
               counts.get("insufficient", 0)))
        self._sync_buttons()

    def _fill_rows(self, rows):
        self._rows = list(rows)
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            category = row.get("category", zip_mod.CATEGORY_FILE_ERROR)
            images = row.get("images") or []
            present = len([item for item in images if item.get("present")])
            note = row.get("reason", "")
            warnings = row.get("warnings") or []
            if warnings:
                note = (note + "；" if note else "") + "；".join(warnings)
            values = [
                str(row.get("line", "")),
                zip_mod.category_text(category),
                row.get("record_key", ""),
                row.get("student_name_matched") or "",
                row.get("question_no", ""),
                ("%d/%d" % (present, len(images))) if images else "—",
                zip_mod.status_text(row.get("status", "error")),
                note,
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 1:
                    cell.setForeground(self._category_color(category))
                if column == 7:
                    cell.setToolTip(value)
                self.table.setItem(index, column, cell)

    @staticmethod
    def _category_color(category):
        return {
            zip_mod.CATEGORY_VALID: QColor("#1b7f3b"),
            zip_mod.CATEGORY_MISSING: QColor("#1b5e9c"),
            zip_mod.CATEGORY_DUPLICATE: QColor("#777777"),
            zip_mod.CATEGORY_CONFLICT: QColor("#b26a00"),
            zip_mod.CATEGORY_UNMATCHED: QColor("#b00020"),
            zip_mod.CATEGORY_FILE_ERROR: QColor("#b00020"),
        }.get(category, QColor("#000000"))

    def _sync_buttons(self):
        importable = 0
        conflicts = 0
        for row in self._rows:
            if row.get("category") in (zip_mod.CATEGORY_VALID, zip_mod.CATEGORY_MISSING):
                importable += 1
            elif row.get("category") == zip_mod.CATEGORY_CONFLICT:
                conflicts += 1
        self.import_button.setEnabled(bool(self._rows) and (
            importable > 0 or (conflicts > 0 and self.overwrite_check.isChecked())))
        self.export_button.setEnabled(bool(self._rows))

    # ---------------------------------------------------------------- 导入
    def confirm_import(self):
        return self.do_import(self.overwrite_check.isChecked(), confirm=True)

    def do_import(self, overwrite=False, confirm=True):
        """执行导入；`confirm=False` 供自动化测试直接调用（不弹确认框）。"""
        if not self._data or not self._rows:
            return None
        if confirm:
            counts = {}
            for row in self._rows:
                counts[row.get("category")] = counts.get(row.get("category"), 0) + 1
            parts = ["将导入 %d 条" % (counts.get(zip_mod.CATEGORY_VALID, 0)
                                       + counts.get(zip_mod.CATEGORY_MISSING, 0))]
            if counts.get(zip_mod.CATEGORY_DUPLICATE):
                parts.append("跳过重复 %d 条" % counts[zip_mod.CATEGORY_DUPLICATE])
            if counts.get(zip_mod.CATEGORY_CONFLICT):
                parts.append("%s %d 条" % ("覆盖冲突" if overwrite else "暂不导入冲突",
                                           counts[zip_mod.CATEGORY_CONFLICT]))
            if counts.get(zip_mod.CATEGORY_FILE_ERROR):
                parts.append("%d 条文件异常不会导入" % counts[zip_mod.CATEGORY_FILE_ERROR])
            text = "，".join(parts) + "。\n\n导入只写入当前班级；确认后开始。"
            if QMessageBox.question(self, "确认导入", text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) != QMessageBox.Yes:
                return None
        self.import_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.status.setText("正在导入…")
        data, batch = self._data, self._batch
        holder = {}
        self.runner.run(lambda: self._call("import_zip_answers", data, batch,
                                          overwrite=overwrite),
                        lambda result: holder.update({"result": self.show_import(result)}),
                        self._import_failed)
        return holder.get("result")

    def _import_failed(self, message):
        self.status.setText(message)
        self._sync_buttons()

    def show_import(self, data):
        self._result = data
        counts = data.get("counts") or {}
        lines = ["批次：%s" % (data.get("batch") or self._batch or "未命名"),
                 "一共 %d 行" % data.get("total", len(self._rows)),
                 "新增 %d 条，覆盖 %d 条，跳过重复 %d 条，未导入冲突 %d 条，文件异常 %d 条"
                 % (counts.get("new", 0), counts.get("overwritten", 0),
                    counts.get("duplicate", 0), counts.get("conflict", 0),
                    counts.get("failed", 0)),
                 "图片落盘 %d 张（文件名由程序生成）" % data.get("images", 0)]
        for item in data.get("invalidated") or []:
            lines.append("覆盖作答 #%s：按 %s 失效（追问 %d 条、评分 %d 条）"
                         % (item.get("answer_id"), "、".join(item.get("rules") or []),
                            item.get("followups", 0), item.get("scores", 0)))
        self.report.setPlainText("\n".join(lines))
        self.status.setText("导入完成：新增 %d，覆盖 %d，跳过重复 %d，未导入冲突 %d，文件异常 %d"
                            % (counts.get("new", 0), counts.get("overwritten", 0),
                               counts.get("duplicate", 0), counts.get("conflict", 0),
                               counts.get("failed", 0)))
        self._sync_buttons()
        return data

    # ---------------------------------------------------------------- 导出
    def export_problems(self, path=""):
        rows = [row for row in self._rows
                if row.get("category") not in (zip_mod.CATEGORY_VALID, zip_mod.CATEGORY_MISSING)]
        if not rows:
            self.status.setText("没有需要导出的问题行（全部可导入）")
            return ""
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "导出问题明细", "导入问题明细.csv",
                                                  "CSV 文件 (*.csv)")
        if not path:
            return ""
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["行", "分类", "记录编号", "学生", "题号", "说明"])
                for row in rows:
                    writer.writerow([row.get("line", ""), zip_mod.category_text(
                        row.get("category", "")), row.get("record_key", ""),
                        row.get("student_name_matched") or row.get("student_name", ""),
                        row.get("question_no", ""),
                        "；".join(([row.get("reason", "")] if row.get("reason") else [])
                                 + list(row.get("warnings") or []))])
        except OSError as exc:
            self.status.setText("导出失败：%s" % exc)
            return ""
        self.status.setText("问题明细已导出：%s（%d 行）" % (path, len(rows)))
        return path

    # ---------------------------------------------------------------- 生命周期
    def closeEvent(self, event):  # noqa: N802 — Qt 固定命名
        self.closed.emit()
        super().closeEvent(event)


__all__ = ["ZipImportWindow", "_InlineRunner"]
