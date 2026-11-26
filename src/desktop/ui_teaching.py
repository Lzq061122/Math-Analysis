"""R2-05 教学资源与辅导界面：资源库 / 双审核发布 / 建议匹配 / 三级提示 / 举一反三与纸笔任务。

本模块是**独立窗口类**，不改 `ui.py` 一行；入口接线由 captain 统一做，片段见
`docs/R2-05_交付说明.md` 第 5 节：

    from .ui_teaching import TeachingWindow
    ...
    def open_teaching(self):
        window = TeachingWindow(self.service)
        window.show()
        return window

界面只做三件事：读后端返回值、把状态字符串原样显示（标签一律取 `r2_contract`）、
把老师的动作转成一次后端调用。**统计与判定不在界面里重写**——发布闸门、双审核、
三级提示门槛、版本追溯都在 `app.teaching_resources` 里。

可测试性：所有动作都走公开方法（`refresh_library` / `submit_current` / `record_review` /
`publish_current` / `run_match` / `record_hint` / `create_task` / `export_paper`），
错误只写到状态标签（`self.status_label`），不弹模态框，所以离屏（offscreen）也能跑。
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app import r2_contract as C
from app import teaching_resources as T
from app import tutoring_pdf as P

from . import theme


def fit_to_screen(window, width, height):
    """与 ui.py 同样的窗口尺寸收敛（ui.py 里的私有 helper 不能跨模块引用，这里自留一份）。"""
    try:
        screen = window.screen()
        available = screen.availableGeometry() if screen else None
        if available:
            width = min(width, max(720, available.width() - 80))
            height = min(height, max(520, available.height() - 80))
    except Exception:                                        # noqa: BLE001
        pass
    window.resize(int(width), int(height))


def _cell(text, editable=False):
    item = QTableWidgetItem(str(text if text is not None else ""))
    if not editable:
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


class TeachingWindow(QMainWindow):
    """R2-05 主窗口：五个页签对应派单 A–E。"""

    TITLE = "数智伴学 · 教学资源与辅导"

    def __init__(self, service, class_row=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.db = getattr(service, "db", None)
        self.user = getattr(service, "user", None)
        self.class_row = class_row
        self.status_text = ""
        self._library_rows = []
        self._review_rows = []
        self._match_result = {}
        self._hint_plan = {}
        self._exercise_rows = []
        self._tasks = []

        self.setObjectName("teachingWindow")
        self.setWindowTitle(self.TITLE)
        fit_to_screen(self, 1180, 860)
        self.setMinimumSize(880, 560)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        heading = QLabel("教学资源与辅导")
        heading.setObjectName("teachingHeading")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("teachingSummary")
        self.summary_label.setStyleSheet("color: #5B6673;")
        self.status_label = QLabel("")
        self.status_label.setObjectName("teachingStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #a4631a;")
        top = QHBoxLayout()
        top.addWidget(heading)
        top.addWidget(self.summary_label)
        top.addStretch(1)
        root.addLayout(top)
        root.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("teachingTabs")
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_library_tab(), "资源库")
        self.tabs.addTab(self._build_review_tab(), "审核发布")
        self.tabs.addTab(self._build_match_tab(), "建议匹配")
        self.tabs.addTab(self._build_hint_tab(), "三级提示")
        self.tabs.addTab(self._build_task_tab(), "举一反三与任务")

        self.refresh_all()

    # ------------------------------------------------------------------
    # 公共小工具
    # ------------------------------------------------------------------
    def flash(self, text, error=False):
        self.status_text = str(text or "")
        self.status_label.setText(self.status_text)
        self.status_label.setStyleSheet("color: #a4631a;" if error else "color: #2f6b3f;")

    def _guard(self, action, label):
        """统一异常出口：业务错误写到状态标签，绝不弹模态框（离屏测试要能跑）。"""
        try:
            action()
        except T.TeachingResourceError as exc:
            self.flash("%s：%s（%s）" % (label, exc.message, exc.code), error=True)
            return False
        except Exception as exc:                             # noqa: BLE001
            self.flash("%s 失败：%s" % (label, exc), error=True)
            return False
        return True

    def selected_class_id(self):
        if self.class_row:
            return self.class_row["id"]
        row = self.db.query_one("SELECT id FROM classes ORDER BY id LIMIT 1") if self.db else None
        return row["id"] if row else None

    def user_id(self):
        """当前登录账号 id（后端的 created_by / reviewer 都只收整数）。"""
        return T._user_id(self.user)

    # ------------------------------------------------------------------
    # A. 资源库
    # ------------------------------------------------------------------
    def _build_library_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        filters = QHBoxLayout()
        self.library_keyword = QLineEdit()
        self.library_keyword.setObjectName("teachingKeyword")
        self.library_keyword.setPlaceholderText("按标题 / 知识点 / 正文关键词检索")
        self.library_kind = QComboBox()
        self.library_kind.setObjectName("teachingKindFilter")
        self.library_kind.addItem("全部类型", "")
        for kind, label in C.RESOURCE_KINDS.items():
            self.library_kind.addItem(label, kind)
        self.library_status = QComboBox()
        self.library_status.setObjectName("teachingStatusFilter")
        self.library_status.addItem("全部状态", "")
        for state, label in C.RESOURCE_STATES.items():
            self.library_status.addItem(label, state)
        search = QPushButton("检索")
        search.setObjectName("teachingSearch")
        search.clicked.connect(self.refresh_library)
        theme.primary(search)
        filters.addWidget(QLabel("检索"))
        filters.addWidget(self.library_keyword, 1)
        filters.addWidget(self.library_kind)
        filters.addWidget(self.library_status)
        filters.addWidget(search)
        layout.addLayout(filters)

        self.library_table = QTableWidget(0, 7)
        self.library_table.setObjectName("teachingLibraryTable")
        self.library_table.setHorizontalHeaderLabels(
            ["标题", "类型", "知识点", "错因", "状态", "版本", "来源"])
        header = self.library_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.library_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.library_table.setSelectionMode(QTableWidget.SingleSelection)
        self.library_table.itemSelectionChanged.connect(self.show_library_detail)
        layout.addWidget(self.library_table, 2)

        self.library_detail = QPlainTextEdit()
        self.library_detail.setObjectName("teachingLibraryDetail")
        self.library_detail.setReadOnly(True)
        layout.addWidget(self.library_detail, 2)

        buttons = QHBoxLayout()
        self.submit_button = QPushButton("送审当前资源")
        self.submit_button.setObjectName("teachingSubmit")
        self.submit_button.clicked.connect(self.submit_current)
        self.disable_button = QPushButton("停用当前资源")
        self.disable_button.setObjectName("teachingDisable")
        self.disable_button.clicked.connect(self.disable_current)
        self.version_button = QPushButton("查看版本历史")
        self.version_button.setObjectName("teachingVersions")
        self.version_button.clicked.connect(self.show_versions)
        for button in (self.submit_button, self.disable_button, self.version_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def refresh_library(self):
        if self.db is None:
            return []
        keyword = self.library_keyword.text().strip()
        rows = T.list_resources(
            self.db,
            kind=self.library_kind.currentData() or None,
            status=self.library_status.currentData() or None,
            keyword=keyword)
        self._library_rows = rows
        self.library_table.setRowCount(0)
        for row in rows:
            index = self.library_table.rowCount()
            self.library_table.insertRow(index)
            self.library_table.setItem(index, 0, _cell(row["title"]))
            self.library_table.setItem(index, 1, _cell(row["kind_label"]))
            self.library_table.setItem(index, 2, _cell(row["knowledge_point"]))
            self.library_table.setItem(index, 3, _cell(row["cause_code"] or "通用"))
            self.library_table.setItem(index, 4, _cell(row["status_label"]))
            self.library_table.setItem(index, 5, _cell(row["version"]))
            self.library_table.setItem(index, 6, _cell(row["source"]))
        return rows

    def current_library_row(self):
        index = self.library_table.currentRow()
        if index < 0 or index >= len(self._library_rows):
            return None
        return self._library_rows[index]

    def _restore_library_selection(self, key):
        """刷新列表后把选中行还给老师（否则送审/停用完一次，界面就「丢了」当前资源）。"""
        if not key:
            return
        for index, row in enumerate(self._library_rows):
            if row["resource_key"] == key:
                self.library_table.selectRow(index)
                self.show_library_detail()
                return

    def show_library_detail(self):
        row = self.current_library_row()
        if not row:
            return
        lines = [
            "标题：%s" % row["title"],
            "类型：%s　知识点：%s　错因：%s"
            % (row["kind_label"], row["knowledge_point"], row["cause_code"] or "通用"),
            "状态：%s　版本：%s（%s）"
            % (row["status_label"], row["version"], row["version_state_label"]),
            "来源：%s%s" % (row["source"] or "未标注",
                            "（合成测试资源，不进入正式推荐）" if row["synthetic"] else ""),
            "",
            "正文：",
            T._json_dump(row["body"]),
        ]
        self.library_detail.setPlainText("\n".join(lines))

    def submit_current(self):
        row = self.current_library_row()
        if not row:
            self.flash("先选中一条资源。", error=True)
            return False
        key = row["resource_key"]
        ok = self._guard(
            lambda: T.submit_for_review(self.db, key, user=self.user), "送审")
        if ok:
            self.flash("已送审：%s" % row["title"])
            self.refresh_all()
            self._restore_library_selection(key)
        return ok

    def disable_current(self):
        row = self.current_library_row()
        if not row:
            self.flash("先选中一条资源。", error=True)
            return False
        key = row["resource_key"]
        ok = self._guard(
            lambda: T.disable_resource(self.db, key, user=self.user, reason="界面停用"), "停用")
        if ok:
            self.flash("已停用：%s（不再用于新推荐，旧引用仍可追溯）" % row["title"])
            self.refresh_all()
            self._restore_library_selection(key)
        return ok

    def show_versions(self):
        row = self.current_library_row()
        if not row:
            return
        history = T.version_history(self.db, row["resource_key"])
        lines = []
        for item in history:
            lines.append("第 %s 版：%s　%s" % (item["version"], item["state_label"],
                                             item["note"] or ""))
            for review in item["reviews"]:
                if review["decision"] == "pending":
                    continue
                lines.append("    %s：%s（%s）%s" % (
                    review["review_kind"], review["decision_label"],
                    review["reviewer_name"] or review["reviewer"], review["opinion"]))
        self.library_detail.setPlainText("\n".join(lines) or "没有版本历史。")

    # ------------------------------------------------------------------
    # B. 审核发布
    # ------------------------------------------------------------------
    def _build_review_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel("双审核：数学正确性与教学适切性必须由两个不同账号分别签署，两项都通过才能发布。"
                      "发布权限属于这一版的审核人；未审资源不会进入正式推荐。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        layout.addWidget(hint)

        self.review_table = QTableWidget(0, 6)
        self.review_table.setObjectName("teachingReviewTable")
        self.review_table.setHorizontalHeaderLabels(
            ["标题", "版本", "数学审核", "教学审核", "可否发布", "来源"])
        header = self.review_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.review_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.review_table.setSelectionMode(QTableWidget.SingleSelection)
        self.review_table.itemSelectionChanged.connect(self.show_review_detail)
        layout.addWidget(self.review_table, 2)

        self.review_detail = QPlainTextEdit()
        self.review_detail.setObjectName("teachingReviewDetail")
        self.review_detail.setReadOnly(True)
        layout.addWidget(self.review_detail, 1)

        form = QHBoxLayout()
        self.review_kind = QComboBox()
        self.review_kind.setObjectName("teachingReviewKind")
        for kind, label in C.RESOURCE_REVIEW_KINDS.items():
            self.review_kind.addItem(label, kind)
        self.review_opinion = QLineEdit()
        self.review_opinion.setObjectName("teachingReviewOpinion")
        self.review_opinion.setPlaceholderText("审核意见（必填：写清依据）")
        form.addWidget(QLabel("审核类型"))
        form.addWidget(self.review_kind)
        form.addWidget(self.review_opinion, 1)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.review_pass_button = QPushButton("通过")
        self.review_pass_button.setObjectName("teachingReviewPass")
        self.review_pass_button.clicked.connect(lambda: self.record_review("pass"))
        self.review_fail_button = QPushButton("不通过")
        self.review_fail_button.setObjectName("teachingReviewFail")
        self.review_fail_button.clicked.connect(lambda: self.record_review("fail"))
        self.publish_button = QPushButton("发布这一版")
        self.publish_button.setObjectName("teachingPublish")
        self.publish_button.clicked.connect(self.publish_current)
        theme.primary(self.publish_button)
        for button in (self.review_pass_button, self.review_fail_button, self.publish_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def refresh_queue(self):
        if self.db is None:
            return []
        rows = T.review_queue(self.db)
        self._review_rows = rows
        self.review_table.setRowCount(0)
        for row in rows:
            progress = row["review_progress"]
            index = self.review_table.rowCount()
            self.review_table.insertRow(index)
            math_state = "通过" if progress["passed"]["math"] else "待审"
            teaching_state = "通过" if progress["passed"]["teaching"] else "待审"
            self.review_table.setItem(index, 0, _cell(row["title"]))
            self.review_table.setItem(index, 1, _cell(row["version"]))
            self.review_table.setItem(index, 2, _cell(math_state))
            self.review_table.setItem(index, 3, _cell(teaching_state))
            self.review_table.setItem(index, 4,
                                      _cell("可发布" if progress["ready_to_publish"] else "未就绪"))
            self.review_table.setItem(index, 5, _cell(row["source"]))
        return rows

    def current_review_row(self):
        index = self.review_table.currentRow()
        if index < 0 or index >= len(self._review_rows):
            return None
        return self._review_rows[index]

    def show_review_detail(self):
        row = self.current_review_row()
        if not row:
            return
        progress = row["review_progress"]
        lines = ["标题：%s　第 %s 版" % (row["title"], row["version"]),
                 "正文：%s" % T._json_dump(row["body"]), ""]
        for review in progress["reviews"]:
            lines.append("%s：%s　%s　%s" % (
                review["review_kind_label"], review["decision_label"],
                review["reviewer_name"] or review["reviewer"] or "-",
                review["opinion"] or ""))
        self.review_detail.setPlainText("\n".join(lines))

    def _restore_review_selection(self, key_version):
        if not key_version:
            return
        for index, row in enumerate(self._review_rows):
            if row["resource_key"] == key_version[0] and row["version"] == key_version[1]:
                self.review_table.selectRow(index)
                self.show_review_detail()
                return

    def record_review(self, decision):
        row = self.current_review_row()
        if not row:
            self.flash("先在待审核列表里选中一条资源。", error=True)
            return False
        opinion = self.review_opinion.text().strip()
        if not opinion:
            self.flash("审核意见必填：写清你依据什么给出结论。", error=True)
            return False
        remembered = (row["resource_key"], row["version"])
        ok = self._guard(
            lambda: T.record_review(self.db, row["resource_key"], row["version"],
                                    self.review_kind.currentData(), self.user, decision, opinion,
                                    reviewer_name=(self.user or {}).get("display_name", "")),
            "记录审核")
        if ok:
            self.flash("已记录%s审核结论（%s）"
                       % (self.review_kind.currentText(), "通过" if decision == "pass" else "不通过"))
            self.refresh_all()
            self._restore_review_selection(remembered)
        return ok

    def publish_current(self):
        row = self.current_review_row()
        if not row:
            self.flash("先选中一条待审核资源。", error=True)
            return False
        remembered = (row["resource_key"], row["version"])
        holder = {}

        def action():
            holder["view"] = T.publish_version(self.db, row["resource_key"], row["version"],
                                               user=self.user, note="界面发布")

        if self._guard(action, "发布"):
            marked = holder["view"].get("references_marked") or []
            self.flash("已发布第 %s 版：%s%s"
                       % (holder["view"]["version"], row["title"],
                          "" if not marked else "；%d 条引用旧版本的任务已标「依据已变，待复核」"
                          % len(marked)))
            self.refresh_all()
            self._restore_review_selection(remembered)
            return True
        return False

    # ------------------------------------------------------------------
    # C. 建议匹配
    # ------------------------------------------------------------------
    def _build_match_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QHBoxLayout()
        self.match_knowledge = QLineEdit()
        self.match_knowledge.setObjectName("teachingMatchKnowledge")
        self.match_knowledge.setPlaceholderText("知识点，例如：移项")
        self.match_cause = QLineEdit()
        self.match_cause.setObjectName("teachingMatchCause")
        self.match_cause.setPlaceholderText("错因码，例如：MOVE-SIGN（可空）")
        self.match_confirmed = QCheckBox("错因已由教师确认")
        self.match_confirmed.setObjectName("teachingMatchConfirmed")
        self.match_button = QPushButton("匹配建议")
        self.match_button.setObjectName("teachingMatchRun")
        self.match_button.clicked.connect(self.run_match)
        theme.primary(self.match_button)
        form.addWidget(QLabel("知识点"))
        form.addWidget(self.match_knowledge, 1)
        form.addWidget(QLabel("错因"))
        form.addWidget(self.match_cause, 1)
        form.addWidget(self.match_confirmed)
        form.addWidget(self.match_button)
        layout.addLayout(form)

        self.match_note = QLabel("")
        self.match_note.setObjectName("teachingMatchNote")
        self.match_note.setWordWrap(True)
        self.match_note.setStyleSheet("color: #a4631a;")
        layout.addWidget(self.match_note)

        self.match_table = QTableWidget(0, 5)
        self.match_table.setObjectName("teachingMatchTable")
        self.match_table.setHorizontalHeaderLabels(["档位", "标题", "类型", "推荐依据", "来源"])
        header = self.match_table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        for column in (0, 2, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.match_table, 1)

        buttons = QHBoxLayout()
        self.match_from_answer_button = QPushButton("从选中的作答匹配")
        self.match_from_answer_button.setObjectName("teachingMatchFromAnswer")
        self.match_from_answer_button.clicked.connect(self.match_from_answer)
        self.match_answer_id = QSpinBox()
        self.match_answer_id.setObjectName("teachingMatchAnswerId")
        self.match_answer_id.setRange(0, 10 ** 9)
        buttons.addWidget(QLabel("作答 id"))
        buttons.addWidget(self.match_answer_id)
        buttons.addWidget(self.match_from_answer_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def run_match(self):
        if self.db is None:
            return {}
        result = T.match_resources(self.db, self.match_unit_text(), self.match_knowledge.text().strip(),
                                   self.match_cause.text().strip(),
                                   self.match_confirmed.isChecked())
        self._match_result = result
        self.match_table.setRowCount(0)
        for item in result["items"]:
            index = self.match_table.rowCount()
            self.match_table.insertRow(index)
            self.match_table.setItem(index, 0, _cell(item["tier_label"]))
            self.match_table.setItem(index, 1, _cell(item["title"]))
            self.match_table.setItem(index, 2, _cell(item["kind_label"]))
            self.match_table.setItem(index, 3, _cell(item["basis_text"]))
            self.match_table.setItem(index, 4, _cell(item["source_text"]))
        self.match_note.setText(result["note"] or
                                "共 %d 条已发布资源（%s）" % (len(result["items"]), result["tier_label"]))
        return result

    def match_unit_text(self):
        knowledge = self.match_knowledge.text().strip()
        if not knowledge:
            return ""
        row = self.db.query_one(
            "SELECT DISTINCT unit FROM teaching_resources WHERE knowledge_point=?", (knowledge,))
        return (row or {}).get("unit", "")

    def match_from_answer(self):
        answer_id = self.match_answer_id.value()
        class_id = self.selected_class_id()
        if not answer_id or not class_id:
            self.flash("先填作答 id（并保证库里有班级）。", error=True)
            return None
        holder = {}
        if self._guard(lambda: holder.update(
                {"result": T.match_for_answer(self.db, class_id, answer_id)}), "按作答匹配"):
            context = holder["result"]["context"]
            self.match_knowledge.setText(context["knowledge_point"])
            self.match_cause.setText(context["cause_code"])
            self.match_confirmed.setChecked(context["cause_confirmed"])
            self.run_match()
            self.flash("依据：%s / %s / %s（错因%s）"
                       % (context["unit"], context["knowledge_point"],
                          context["cause_label"] or "未确认",
                          "已确认" if context["cause_confirmed"] else "未确认，只推通用"))
            return holder["result"]
        return None

    # ------------------------------------------------------------------
    # D. 三级提示
    # ------------------------------------------------------------------
    def _build_hint_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.hint_title = QLabel("三级提示：默认 P1 关键步骤 → P2 规则 → P3 局部示例；"
                                 "完整解析必须在学生独立尝试之后。")
        self.hint_title.setWordWrap(True)
        self.hint_title.setStyleSheet("color: #555;")
        layout.addWidget(self.hint_title)

        self.hint_table = QTableWidget(0, 3)
        self.hint_table.setObjectName("teachingHintTable")
        self.hint_table.setHorizontalHeaderLabels(["级别", "提示内容", "资源版本"])
        header = self.hint_table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.hint_table.setWordWrap(True)
        layout.addWidget(self.hint_table, 2)

        form = QFormLayout()
        self.hint_student = QLineEdit()
        self.hint_student.setObjectName("teachingHintStudent")
        self.hint_student.setPlaceholderText("学生 id（必填）")
        self.hint_level = QComboBox()
        self.hint_level.setObjectName("teachingHintLevel")
        for level, label in C.HINT_LEVELS.items():
            self.hint_level.addItem("%s %s" % (level, label), level)
        self.hint_independent = QCheckBox("学生已独立尝试")
        self.hint_independent.setObjectName("teachingHintIndependent")
        self.hint_full_solution = QCheckBox("已提供完整解析")
        self.hint_full_solution.setObjectName("teachingHintFullSolution")
        self.hint_after = QCheckBox("解析在独立尝试之后")
        self.hint_after.setObjectName("teachingHintAfter")
        self.hint_fixed = QCheckBox("学生自己修正了")
        self.hint_fixed.setObjectName("teachingHintFixed")
        form.addRow("学生", self.hint_student)
        form.addRow("实际级别", self.hint_level)
        row = QHBoxLayout()
        for box in (self.hint_independent, self.hint_full_solution, self.hint_after,
                    self.hint_fixed):
            row.addWidget(box)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("记录", holder)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.hint_load_button = QPushButton("取这套提示")
        self.hint_load_button.setObjectName("teachingHintLoad")
        self.hint_load_button.clicked.connect(self.load_hints)
        self.hint_save_button = QPushButton("记录一次提示使用")
        self.hint_save_button.setObjectName("teachingHintSave")
        self.hint_save_button.clicked.connect(self.record_hint)
        theme.primary(self.hint_save_button)
        buttons.addWidget(self.hint_load_button)
        buttons.addWidget(self.hint_save_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def load_hints(self):
        if self.db is None:
            return {}
        plan = T.hint_plan(self.db, self.match_unit_text(), self.match_knowledge.text().strip(),
                           self.match_cause.text().strip(), self.match_confirmed.isChecked())
        self._hint_plan = plan
        self.hint_table.setRowCount(0)
        for item in plan["levels"]:
            index = self.hint_table.rowCount()
            self.hint_table.insertRow(index)
            self.hint_table.setItem(index, 0, _cell("%s %s" % (item["level"], item["level_label"])))
            self.hint_table.setItem(index, 1, _cell(item["text"]))
            self.hint_table.setItem(index, 2, _cell("v%s" % item["version"]))
        if plan["note"]:
            self.flash(plan["note"])
        return plan

    def record_hint(self):
        student_id = self.hint_student.text().strip()
        if not student_id.isdigit():
            self.flash("先填学生 id。", error=True)
            return False
        class_id = self.selected_class_id()
        level = self.hint_level.currentData()
        current = [item for item in self._hint_plan.get("levels", []) if item["level"] == level]
        ok = self._guard(
            lambda: T.record_hint_usage(
                self.db, class_id, int(student_id), level=level,
                planned_level=self._hint_plan.get("start_level", "P1"), provided=1,
                independent_attempt=self.hint_independent.isChecked(),
                student_fixed=self.hint_fixed.isChecked(),
                full_solution_provided=self.hint_full_solution.isChecked(),
                independent_before_solution=self.hint_after.isChecked(),
                knowledge_point=self.match_knowledge.text().strip(),
                cause_code=self.match_cause.text().strip(),
                resource_key=(current[0]["resource_key"] if current else ""),
                resource_version=(current[0]["version"] if current else None),
                note="界面记录"),
            "记录提示使用")
        if ok:
            self.flash("已记录 %s 级提示：独立尝试=%s，完整解析=%s，解析在独立尝试之后=%s"
                       % (level, "是" if self.hint_independent.isChecked() else "否",
                          "是" if self.hint_full_solution.isChecked() else "否",
                          "是" if self.hint_after.isChecked() else "否"))
            return True
        return False

    # ------------------------------------------------------------------
    # E. 举一反三与任务
    # ------------------------------------------------------------------
    def _build_task_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.exercise_table = QTableWidget(0, 6)
        self.exercise_table.setObjectName("teachingExerciseTable")
        self.exercise_table.setHorizontalHeaderLabels(
            ["选", "用途", "难度", "题干", "标准答案", "推荐理由"])
        header = self.exercise_table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        for column in (0, 1, 2, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.exercise_table, 1)

        form = QHBoxLayout()
        self.task_title = QLineEdit()
        self.task_title.setObjectName("teachingTaskTitle")
        self.task_title.setPlaceholderText("任务标题，例如：移项专项辅导（第 3 次）")
        self.task_note = QLineEdit()
        self.task_note.setObjectName("teachingTaskNote")
        self.task_note.setPlaceholderText("教师说明：先做什么、卡住了怎么给提示")
        form.addWidget(self.task_title, 1)
        form.addWidget(self.task_note, 2)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.exercise_load_button = QPushButton("推荐练习")
        self.exercise_load_button.setObjectName("teachingExerciseLoad")
        self.exercise_load_button.clicked.connect(self.load_exercises)
        self.task_create_button = QPushButton("创建辅导任务")
        self.task_create_button.setObjectName("teachingTaskCreate")
        self.task_create_button.clicked.connect(self.create_task)
        self.task_complete_button = QPushButton("记录完成")
        self.task_complete_button.setObjectName("teachingTaskComplete")
        self.task_complete_button.clicked.connect(self.complete_current_task)
        self.pdf_student_button = QPushButton("导出纸笔任务（学生版）")
        self.pdf_student_button.setObjectName("teachingPdfStudent")
        self.pdf_student_button.clicked.connect(lambda: self.export_paper("student"))
        self.pdf_teacher_button = QPushButton("导出纸笔任务（教师版）")
        self.pdf_teacher_button.setObjectName("teachingPdfTeacher")
        self.pdf_teacher_button.clicked.connect(lambda: self.export_paper("teacher"))
        theme.primary(self.task_create_button)
        for button in (self.exercise_load_button, self.task_create_button,
                       self.task_complete_button, self.pdf_student_button,
                       self.pdf_teacher_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.task_table = QTableWidget(0, 5)
        self.task_table.setObjectName("teachingTaskTable")
        self.task_table.setHorizontalHeaderLabels(["标题", "对象", "状态", "依据复核", "创建时间"])
        header = self.task_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.task_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.task_table, 1)
        self.last_pdf_path = ""
        return page

    def load_exercises(self):
        if self.db is None:
            return {}
        found = T.recommend_exercises(self.db, self.match_unit_text(),
                                      self.match_knowledge.text().strip(),
                                      self.match_cause.text().strip(),
                                      self.match_confirmed.isChecked())
        self._exercise_rows = [item for purpose in T.EXERCISE_PURPOSES
                               for item in found["groups"].get(purpose, [])]
        self.exercise_table.setRowCount(0)
        for item in self._exercise_rows:
            index = self.exercise_table.rowCount()
            self.exercise_table.insertRow(index)
            box = QTableWidgetItem("")
            box.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            box.setCheckState(Qt.Checked)
            self.exercise_table.setItem(index, 0, box)
            self.exercise_table.setItem(index, 1, _cell(item["purpose_label"]))
            self.exercise_table.setItem(index, 2, _cell(item["difficulty"]))
            self.exercise_table.setItem(index, 3, _cell(item["stem"]))
            self.exercise_table.setItem(index, 4, _cell(item["answer"]))
            self.exercise_table.setItem(index, 5, _cell(item["reason"]))
        if found["note"]:
            self.flash(found["note"])
        return found

    def chosen_exercises(self):
        refs = []
        for index, item in enumerate(self._exercise_rows):
            cell = self.exercise_table.item(index, 0)
            if cell is not None and cell.checkState() == Qt.Checked:
                refs.append({"resource_key": item["resource_key"], "version": item["version"],
                             "purpose": item["purpose"], "phase": item["phase"],
                             "title": item["title"]})
        return refs

    def create_task(self):
        class_id = self.selected_class_id()
        if not class_id:
            self.flash("库里还没有班级。", error=True)
            return None
        student_text = self.hint_student.text().strip()
        student_id = int(student_text) if student_text.isdigit() else None
        if student_id and self.db.query_one(
                "SELECT id FROM students WHERE id=? AND class_id=?", (student_id, class_id)) is None:
            self.flash("这个学生不在当前班级里。", error=True)
            return None
        refs = self.chosen_exercises()
        if not refs:
            self.flash("先点「推荐练习」并勾选至少一道练习。", error=True)
            return None
        title = self.task_title.text().strip() or ("%s 辅导任务" % self.match_knowledge.text().strip())
        plan = self._hint_plan or self.load_hints()
        holder = {}

        def action():
            holder["task"] = T.create_task(
                self.db, class_id, student_id,
                "individual" if student_id else "class", title,
                basis={"knowledge_point": self.match_knowledge.text().strip(),
                       "unit": self.match_unit_text(),
                       "cause_code": self.match_cause.text().strip(),
                       "cause_confirmed": self.match_confirmed.isChecked()},
                hint_plan=plan, exercise_refs=refs,
                teacher_note=self.task_note.text().strip(), created_by=self.user_id(),
                status="scheduled")

        if self._guard(action, "创建任务"):
            task = holder["task"]
            self.flash("已创建任务 #%d：%s（引用 %d 个资源版本）"
                       % (task["id"], task["title"], len(task["resource_versions"])))
            self.refresh_tasks()
            return task
        return None

    def current_task(self):
        index = self.task_table.currentRow()
        if index < 0 or index >= len(self._tasks):
            return None
        return self._tasks[index]

    def refresh_tasks(self):
        class_id = self.selected_class_id()
        if not class_id or self.db is None:
            return []
        rows = T.list_tasks(self.db, class_id)
        self._tasks = rows
        self.task_table.setRowCount(0)
        for row in rows:
            index = self.task_table.rowCount()
            self.task_table.insertRow(index)
            self.task_table.setItem(index, 0, _cell(row["title"]))
            self.task_table.setItem(index, 1, _cell("个人辅导" if row["student_id"] else "班级辅导"))
            self.task_table.setItem(index, 2, _cell(row["status_label"]))
            self.task_table.setItem(index, 3, _cell(row["review_state_label"]))
            self.task_table.setItem(index, 4, _cell(row["created_at"]))
        return rows

    def complete_current_task(self):
        task = self.current_task()
        if not task:
            self.flash("先选中一条任务。", error=True)
            return False
        ok = self._guard(lambda: T.complete_task(self.db, task["id"], user=self.user), "记录完成")
        if ok:
            self.flash("任务 #%d 已标记完成（状态与完成时间已写库）" % task["id"])
            self.refresh_tasks()
        return ok

    def export_paper(self, variant="student", path=None):
        task = self.current_task()
        if not task:
            self.flash("先选中一条任务再导出纸笔任务。", error=True)
            return ""
        holder = {}

        def action():
            payload = P.paper_payload(self.db, task["id"])
            data = P.render_student(payload) if variant == "student" else P.render_teacher(payload)
            target = path
            if not target:
                default_dir = getattr(self.service, "data_dir", os.getcwd())
                target = os.path.join(default_dir, "纸笔任务-%d-%s.pdf"
                                      % (task["id"], "学生版" if variant == "student" else "教师版"))
                target = QFileDialog.getSaveFileName(
                    self, "保存纸笔任务 PDF", target, "PDF 文件 (*.pdf)")[0] or ""
            if not target:
                raise T.TeachingResourceError("没有选择保存位置。", "cancelled")
            folder = os.path.dirname(os.path.abspath(target))
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(data)
            holder["path"] = target

        if self._guard(action, "导出纸笔任务"):
            self.last_pdf_path = holder["path"]
            self.flash("已导出%s：%s" % ("学生版" if variant == "student" else "教师版",
                                        holder["path"]))
            return holder["path"]
        return ""

    # ------------------------------------------------------------------
    # 汇总刷新
    # ------------------------------------------------------------------
    def refresh_all(self):
        if self.db is None:
            self.flash("没有可用的数据库连接。", error=True)
            return
        stats = T.resource_stats(self.db)
        self.summary_label.setText(
            "资源 %d 条｜已发布 %d｜待审核 %d｜草稿 %d｜停用 %d｜合成测试 %d"
            % (stats["total"], stats["by_status"].get("published", 0),
               stats["by_status"].get("pending_review", 0),
               stats["by_status"].get("draft", 0),
               stats["by_status"].get("disabled", 0), stats["synthetic"]))
        self.refresh_library()
        self.refresh_queue()
        self.refresh_tasks()
