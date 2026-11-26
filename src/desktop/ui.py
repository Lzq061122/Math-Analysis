"""桌面测试版的窗口：登录、注册、班级列表、学生名单、错题查看与录入。

界面只处理输入与展示，账号与数据操作全部交给 DesktopService，
再由它调用网页版同一批处理器，保证两种界面共用一份账号与数据逻辑。
"""
import csv
import html
import os
import re
import uuid

from PySide6.QtCore import QDate, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import answers as answers_mod
from app import vision
from app.analysis import ERROR_TYPES
from . import data_dir as data_dir_state
from . import theme
from .service import ApiError


class FlowLayout(QLayout):
    """按钮按窗口宽度自动换行：一行放不下就换到下一行，不把窗口撑宽。"""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _arrange(self, rect, test_only):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self.spacing()
            if next_x - self.spacing() > area.right() and line_height > 0:
                x = area.x()
                y += line_height + self.spacing()
                next_x = x + hint.width() + self.spacing()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()

SOURCE_TYPES = ("作业", "考试", "练习", "其他")
STATUS_TEXT = {"pending": "待核对", "confirmed": "已确认", "returned": "已退回"}
AI_MODE_TEXT = {
    "real": "真实 AI 建议（Console）",
    "go": "真实 AI 建议（Go 订阅）",
    "custom": "真实 AI 建议（自定义服务）",
    "mock": "模拟建议",
    "local": "本地录入（未调用 AI）",
}
PHOTO_FILTER = "图片 (*.jpg *.jpeg *.png *.webp)"


def fit_to_screen(window, width, height, margin=40):
    """按本机可用屏幕区域收一下初始尺寸，1366×768 这类小屏上窗口也不会露到屏幕外。"""
    screen = QApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        width = max(320, min(width, avail.width() - margin))
        height = max(240, min(height, avail.height() - margin))
    window.resize(width, height)


def auto_shot(dialog, delay=800):
    """自动化测试用：设置 MISTAKE_SHOT_DIR 时，对话框每 0.8 秒把自己的画面存成 PNG。

    对话框是嵌在父窗口里的原生子窗口，从外面截图只能拿到父窗口的旧画面，
    所以由对话框自己交出画面。老师正常使用时不设这个变量，什么都不做。
    """
    folder = os.environ.get("MISTAKE_SHOT_DIR")
    if not folder:
        return
    path = os.path.join(folder, "%s.png" % dialog.objectName())

    def save():
        if dialog.isVisible():
            dialog.grab().save(path)

    timer = QTimer(dialog)
    timer.setInterval(delay)
    timer.timeout.connect(save)
    timer.start()
    save()


def data_dir_text(path, width=560):
    """窗口窄的时候把中间省略，鼠标悬停仍能看到完整路径。"""
    from PySide6.QtGui import QFontMetrics

    metrics = QFontMetrics(QLabel().font())
    return metrics.elidedText(path, Qt.ElideMiddle, width)


class Task(QThread):
    """把可能耗时的账号与数据库操作放到后台线程，窗口保持可响应。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, func, parent=None):
        super().__init__(parent)
        self._func = func

    def run(self):
        try:
            self.done.emit(self._func())
        except ApiError as exc:
            self.failed.emit(exc.message)
        except Exception as exc:  # 数据库不可写等意外情况也要给老师看得懂的提示
            self.failed.emit("操作没有完成：%s" % exc)


class TaskRunner:
    """持有后台任务的引用，避免线程对象被回收。"""

    def __init__(self):
        self._tasks = []

    @property
    def busy(self):
        return bool(self._tasks)

    def run(self, func, on_done, on_failed):
        task = Task(func)
        task.done.connect(lambda result: self._finish(task, on_done, result))
        task.failed.connect(lambda message: self._finish(task, on_failed, message))
        task.finished.connect(lambda: self._release(task))
        self._tasks.append(task)
        task.start()

    def _finish(self, task, callback, value):
        """先把这个任务从忙碌列表里拿掉，再回调，避免回调里刷新按钮时仍被判为忙碌。"""
        if task in self._tasks:
            self._tasks.remove(task)
        callback(value)

    def _release(self, task):
        if task in self._tasks:
            self._tasks.remove(task)
        task.deleteLater()

    def wait(self, timeout_ms=15000):
        for task in list(self._tasks):
            task.wait(timeout_ms)


class DataDirDialog(QDialog):
    """启动时的数据目录选择：明确显示当前目录，只允许「用现有」或「新建」。"""

    def __init__(self, current, parent=None, notice=None):
        super().__init__(parent)
        self.data_dir = None
        self.setObjectName("dataDirDialog")
        self.setWindowTitle("数智伴学 · 选择数据目录")
        self.setMinimumWidth(560)

        state, message = data_dir_state.describe(current)
        self._current = current

        heading = QLabel("选择数据目录")
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        hint = QLabel("桌面测试版直接使用老师的本地数据，不会另建一份，也不会自动改动现有数据。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")

        self.path_label = QLabel()
        self.path_label.setObjectName("dataDirPath")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setText(current)

        self.state_label = QLabel()
        self.state_label.setObjectName("dataDirState")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: %s;" % ("#2e7d32" if state == data_dir_state.READY else "#b3261e"))
        self.state_label.setText(message)

        self.use_button = QPushButton("使用这个目录")
        self.use_button.setObjectName("dataDirUse")
        self.use_button.setEnabled(state == data_dir_state.READY)
        self.use_button.clicked.connect(self._use_current)

        self.open_button = QPushButton("打开现有目录…")
        self.open_button.setObjectName("dataDirOpen")
        self.open_button.clicked.connect(self._pick_existing)

        self.new_button = QPushButton("新建数据目录…")
        self.new_button.setObjectName("dataDirNew")
        self.new_button.clicked.connect(self._pick_new)

        self.quit_button = QPushButton("退出")
        self.quit_button.setObjectName("dataDirQuit")
        self.quit_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self.use_button)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.new_button)
        buttons.addStretch(1)
        buttons.addWidget(self.quit_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(heading)
        layout.addWidget(hint)
        if notice:
            self.notice_label = QLabel(notice)
            self.notice_label.setObjectName("dataDirNotice")
            self.notice_label.setWordWrap(True)
            self.notice_label.setStyleSheet("color: #b3261e; background: #fdecea; padding: 8px;")
            layout.addWidget(self.notice_label)
        layout.addWidget(QLabel("当前数据目录："))
        layout.addWidget(self.path_label)
        layout.addWidget(self.state_label)
        layout.addLayout(buttons)

    # ---------------------------------------------------------------- 选择
    def _use_current(self):
        self.data_dir = self._current
        self.accept()

    def _set_message(self, path, message, ok):
        self.path_label.setText(path)
        self.state_label.setText(message)
        self.state_label.setStyleSheet("color: %s;" % ("#2e7d32" if ok else "#b3261e"))

    def _pick_existing(self):
        path = QFileDialog.getExistingDirectory(self, "选择 1.3.0 数据目录（里面有 app.db）")
        if not path:
            return
        state, message = data_dir_state.describe(path)
        self._set_message(path, message, state == data_dir_state.READY)
        if state == data_dir_state.READY:
            self.data_dir = os.path.abspath(path)
            self.accept()

    def _pick_new(self):
        path = QFileDialog.getExistingDirectory(self, "选择一个空文件夹作为新数据目录")
        if not path:
            return
        try:
            created = data_dir_state.create_new(path)
        except (OSError, ValueError) as exc:
            self._set_message(path, "没有建成：%s" % exc, False)
            return
        self._set_message(created, "已新建一份空数据", True)
        self.data_dir = created
        self.accept()


class LoginWindow(QWidget):
    """登录与注册共用一个窗口，用堆叠页切换。"""

    logged_in = Signal(dict)
    closed = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.setObjectName("loginWindow")
        self.setWindowTitle("数智伴学 · 桌面版")
        self.resize(420, 380)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_login_page())
        self.stack.addWidget(self._build_register_page())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        title = QLabel("数智伴学")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        subtitle = QLabel("桌面测试版：账号与班级")
        subtitle.setStyleSheet("color: #666;")
        self.data_dir_label = QLabel("")
        self.data_dir_label.setObjectName("dataDirLabel")
        self.data_dir_label.setStyleSheet("color: #777; font-size: 11px;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.data_dir_label)

    def set_data_dir(self, path):
        self.data_dir_label.setText("数据目录：%s" % data_dir_text(path))
        self.data_dir_label.setToolTip(path)

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()

    # ---------------------------------------------------------------- 登录页
    def _build_login_page(self):
        page = QWidget()
        form = QFormLayout()
        self.login_username = QLineEdit()
        self.login_username.setObjectName("loginUsername")
        self.login_username.setPlaceholderText("老师账号")
        self.login_password = QLineEdit()
        self.login_password.setObjectName("loginPassword")
        self.login_password.setPlaceholderText("密码")
        self.login_password.setEchoMode(QLineEdit.Password)
        form.addRow("账号", self.login_username)
        form.addRow("密码", self.login_password)

        self.login_button = QPushButton("登录")
        self.login_button.setObjectName("loginButton")
        self.login_button.setDefault(True)
        self.login_button.clicked.connect(self.submit_login)

        self.to_register = QPushButton("还没有账号？注册一个")
        self.to_register.setObjectName("registerLink")
        self.to_register.setFlat(True)
        self.to_register.setCursor(Qt.PointingHandCursor)
        self.to_register.clicked.connect(self.show_register)

        self.login_status = QLabel("")
        self.login_status.setObjectName("loginStatus")
        self.login_status.setWordWrap(True)

        layout = QVBoxLayout(page)
        layout.addLayout(form)
        layout.addWidget(self.login_button)
        layout.addWidget(self.to_register, 0, Qt.AlignLeft)
        layout.addWidget(self.login_status)
        layout.addStretch(1)
        return page

    # ---------------------------------------------------------------- 注册页
    def _build_register_page(self):
        page = QWidget()
        form = QFormLayout()
        self.register_username = QLineEdit()
        self.register_username.setObjectName("registerUsername")
        self.register_username.setPlaceholderText("老师账号")
        self.register_password = QLineEdit()
        self.register_password.setObjectName("registerPassword")
        self.register_password.setPlaceholderText("至少 8 位")
        self.register_password.setEchoMode(QLineEdit.Password)
        self.register_confirm = QLineEdit()
        self.register_confirm.setObjectName("registerConfirm")
        self.register_confirm.setPlaceholderText("再输入一次")
        self.register_confirm.setEchoMode(QLineEdit.Password)
        form.addRow("账号", self.register_username)
        form.addRow("密码", self.register_password)
        form.addRow("确认", self.register_confirm)

        self.register_button = QPushButton("注册并登录")
        self.register_button.setObjectName("registerButton")
        self.register_button.clicked.connect(self.submit_register)

        self.to_login = QPushButton("已有账号？返回登录")
        self.to_login.setObjectName("loginLink")
        self.to_login.setFlat(True)
        self.to_login.setCursor(Qt.PointingHandCursor)
        self.to_login.clicked.connect(self.show_login)

        self.register_status = QLabel("")
        self.register_status.setObjectName("registerStatus")
        self.register_status.setWordWrap(True)

        layout = QVBoxLayout(page)
        layout.addLayout(form)
        layout.addWidget(self.register_button)
        layout.addWidget(self.to_login, 0, Qt.AlignLeft)
        layout.addWidget(self.register_status)
        layout.addStretch(1)
        return page

    # ---------------------------------------------------------------- 状态
    def show_register(self):
        self.stack.setCurrentIndex(1)
        self.register_status.clear()
        self.register_username.setFocus()

    def show_login(self):
        self.stack.setCurrentIndex(0)
        self.login_status.clear()
        self.login_username.setFocus()

    def _set_busy(self, busy, text=""):
        for widget in (
            self.login_button,
            self.to_register,
            self.register_button,
            self.to_login,
            self.login_username,
            self.login_password,
            self.register_username,
            self.register_password,
            self.register_confirm,
        ):
            widget.setEnabled(not busy)
        self.login_status.setText(text)
        self.register_status.setText(text)

    def _fail(self, message):
        self._set_busy(False, "")
        if self.stack.currentIndex() == 0:
            self.login_status.setText(message)
        else:
            self.register_status.setText(message)

    # ---------------------------------------------------------------- 动作
    def submit_login(self):
        username = self.login_username.text().strip()
        password = self.login_password.text()
        if not username or not password:
            self.login_status.setText("请输入账号和密码")
            return
        self._set_busy(True, "正在登录…")
        self.runner.run(
            lambda: self.service.login(username, password),
            self._login_done,
            self._fail,
        )

    def submit_register(self):
        username = self.register_username.text().strip()
        password = self.register_password.text()
        if not username:
            self.register_status.setText("请输入账号")
            return
        if len(password) < 8:
            self.register_status.setText("密码至少 8 位")
            return
        if password != self.register_confirm.text():
            self.register_status.setText("两次输入的密码不一样")
            return
        self._set_busy(True, "正在注册…")
        self.runner.run(
            lambda: self.service.register(username, password),
            self._login_done,
            self._fail,
        )

    def _login_done(self, user):
        self._set_busy(False, "")
        self.login_password.clear()
        self.register_password.clear()
        self.register_confirm.clear()
        self.logged_in.emit(user)


class CreateClassDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.created = None
        self.setObjectName("createDialog")
        self.setWindowTitle("新建班级")
        self.setMinimumWidth(340)

        self.name_input = QLineEdit()
        self.name_input.setObjectName("className")
        self.name_input.setPlaceholderText("例如：初二（3）班")
        self.grade_input = QLineEdit()
        self.grade_input.setObjectName("classGrade")
        self.grade_input.setPlaceholderText("例如：八年级")
        self.note_input = QLineEdit()
        self.note_input.setObjectName("classNote")

        form = QFormLayout()
        form.addRow("班级名称", self.name_input)
        form.addRow("年级", self.grade_input)
        form.addRow("备注", self.note_input)

        self.status = QLabel("")
        self.status.setObjectName("createStatus")
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("创建")
        self.buttons.button(QDialogButtonBox.Ok).setObjectName("createConfirm")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.Cancel).setObjectName("createCancel")
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

    def submit(self):
        name = self.name_input.text().strip()
        if not name:
            self.status.setText("班级名称不能为空")
            return
        self.buttons.setEnabled(False)
        self.status.setText("正在创建…")
        self.runner.run(
            lambda: self.service.create_class(name, self.grade_input.text().strip(), self.note_input.text().strip()),
            self._done,
            self._fail,
        )

    def _done(self, row):
        self.buttons.setEnabled(True)
        self.created = row
        self.accept()

    def _fail(self, message):
        self.buttons.setEnabled(True)
        self.status.setText(message)


class StudentDialog(QDialog):
    """新增学生或修改姓名。只改姓名，学生的内部编号不会变。"""

    def __init__(self, service, class_id, student=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.class_id = class_id
        self.student = student
        self.saved = None
        self.setObjectName("studentDialog")
        self.setWindowTitle("修改学生姓名" if student else "新增学生")
        self.setMinimumWidth(340)

        self.name_input = QLineEdit()
        self.name_input.setObjectName("studentName")
        self.name_input.setMaxLength(20)
        self.name_input.setPlaceholderText("学生姓名，例如：李小明")
        if student:
            self.name_input.setText(student["name"])

        self.code_input = QLineEdit()
        self.code_input.setObjectName("studentCode")
        self.code_input.setMaxLength(20)
        self.code_input.setPlaceholderText("学校随机编号，可留空；用于导入时免姓名匹配")
        if student:
            self.code_input.setText(student.get("code") or "")

        form = QFormLayout()
        form.addRow("姓名", self.name_input)
        form.addRow("学校编号", self.code_input)

        self.status = QLabel("")
        self.status.setObjectName("studentDialogStatus")
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("保存")
        self.buttons.button(QDialogButtonBox.Ok).setObjectName("studentConfirm")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.Cancel).setObjectName("studentCancel")
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

    def submit(self):
        name = self.name_input.text().strip()
        if not name:
            self.status.setText("学生姓名不能为空")
            return
        code = self.code_input.text().strip()
        self.buttons.setEnabled(False)
        self.status.setText("正在保存…")
        if self.student:
            action = lambda: self.service.rename_student(self.student["id"], name, code)
        else:
            action = lambda: self.service.create_student(self.class_id, name, code)
        self.runner.run(action, self._done, self._fail)

    def _done(self, row):
        self.buttons.setEnabled(True)
        self.saved = row
        self.accept()

    def _fail(self, message):
        self.buttons.setEnabled(True)
        self.status.setText(message)


class StudentsWindow(QMainWindow):
    """一个班级的学生名单：新增、修改姓名、停用/启用、打开这个学生的错题。"""

    closed = Signal()
    show_mistakes = Signal(dict)
    show_stats = Signal(dict)

    def __init__(self, service, class_row, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.class_row = class_row
        self.class_id = class_row["id"]
        self._items = []
        self.setObjectName("studentsWindow")
        self.setWindowTitle("数智伴学 · 学生名单")
        fit_to_screen(self, 1080, 640)
        self.setMinimumSize(720, 420)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · 学生名单" % class_row.get("name", ""))
        heading.setObjectName("studentClassName")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.back_button = QPushButton("返回班级列表")
        self.back_button.setObjectName("backToClasses")
        self.back_button.clicked.connect(self.close)
        theme.ghost(self.back_button)

        top = QHBoxLayout()
        top.addWidget(heading, 1)
        top.addWidget(self.back_button)

        self.new_button = QPushButton("新增学生")
        self.new_button.setObjectName("newStudentButton")
        self.new_button.clicked.connect(self.open_create_dialog)
        theme.primary(self.new_button)
        self.edit_button = QPushButton("修改姓名")
        self.edit_button.setObjectName("editStudentButton")
        self.edit_button.clicked.connect(self.open_edit)
        self.deactivate_button = QPushButton("停用学生")
        self.deactivate_button.setObjectName("deactivateStudentButton")
        self.deactivate_button.clicked.connect(lambda: self.set_active(False))
        theme.danger(self.deactivate_button)
        self.activate_button = QPushButton("启用学生")
        self.activate_button.setObjectName("activateStudentButton")
        self.activate_button.clicked.connect(lambda: self.set_active(True))
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("refreshStudentsButton")
        self.refresh_button.clicked.connect(self.reload)
        self.mistakes_button = QPushButton("查看错题")
        self.mistakes_button.setObjectName("openMistakesButton")
        self.mistakes_button.clicked.connect(self.open_mistakes)
        self.stats_button = QPushButton("查看学情")
        self.stats_button.setObjectName("openStatsButton")
        self.stats_button.clicked.connect(self.open_stats)
        self.insight_button = QPushButton("个人学情")
        self.insight_button.setObjectName("openStudentInsight")
        self.insight_button.clicked.connect(self.open_insight)
        self.import_button = QPushButton("导入作答 CSV")
        self.import_button.setObjectName("openAnswerImport")
        self.import_button.clicked.connect(self.open_import)
        self.answers_button = QPushButton("作答分析")
        self.answers_button.setObjectName("openAnswerReviews")
        self.answers_button.clicked.connect(self.open_answers)
        self.status = QLabel("")
        self.status.setObjectName("studentStatus")
        self.status.setWordWrap(True)

        # 十个按钮用会换行的布局：窗口窄时自动折成两三行，不会把窗口撑宽，也不会截断文字。
        tools = FlowLayout()
        for button in (self.new_button, self.edit_button, self.deactivate_button,
                       self.activate_button, self.refresh_button, self.mistakes_button,
                       self.stats_button, self.insight_button, self.import_button,
                       self.answers_button):
            tools.addWidget(button)

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("studentTable")
        self.table.setHorizontalHeaderLabels(["序号", "内部编号", "姓名", "学校编号", "状态"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemDoubleClicked.connect(self.open_edit)

        self.empty = QLabel("这个班级还没有学生。点「新增学生」添加第一个。")
        self.empty.setObjectName("studentsEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet("color: #777;")

        self.body = QStackedWidget()
        self.body.setObjectName("studentsBody")
        self.body.addWidget(self.empty)
        self.body.addWidget(self.table)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addLayout(tools)
        layout.addWidget(self.status)
        layout.addWidget(self.body, 1)

        self._sync_buttons()

    # ---------------------------------------------------------------- 数据
    def _selected_student(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _sync_buttons(self):
        student = self._selected_student()
        enabled = student is not None
        self.edit_button.setEnabled(enabled)
        self.deactivate_button.setEnabled(enabled and bool(student.get("active", 1)))
        self.activate_button.setEnabled(enabled and not student.get("active", 1))
        self.mistakes_button.setEnabled(enabled)
        self.stats_button.setEnabled(enabled)
        self.insight_button.setEnabled(enabled)
        self.insight_button.setToolTip("看这个学生自己的已确认问题与辅导建议，不发送姓名、班级或账号"
                                       if enabled else "")

    def open_mistakes(self):
        student = self._selected_student()
        if student is None:
            self.status.setText("先在名单里点一个学生")
            return
        self.show_mistakes.emit(student)

    def open_stats(self):
        student = self._selected_student()
        if student is None:
            self.status.setText("先点一个学生，再看他的学情")
            return
        self.show_stats.emit(student)

    def open_insight(self):
        student = self._selected_student()
        if student is None:
            self.status.setText("先点一个学生，再看他的个人学情")
            return
        self._insight_window = StudentInsightWindow(self.service, self.class_row, student, parent=self)
        self._insight_window.show()
        self._insight_window.raise_()

    def open_import(self):
        self._import_window = AnswerImportWindow(self.service, self.class_row, parent=self)
        self._import_window.closed.connect(self.reload)
        self._import_window.show()
        self._import_window.raise_()

    def open_answers(self):
        self._answers_window = AnswerListWindow(self.service, self.class_row, parent=self)
        self._answers_window.closed.connect(self.reload)
        self._answers_window.show()
        self._answers_window.raise_()

    def reload(self):
        if self.runner.busy:
            # 停用/启用后立刻刷新时，保存任务可能还没释放；等它释放再刷，不能悄悄跳过。
            QTimer.singleShot(150, self.reload)
            return
        self.new_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载学生名单…")
        self.runner.run(lambda: self.service.list_students(self.class_id), self._show_students, self._load_failed)

    def _load_failed(self, message):
        self.new_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.status.setText(message)

    def _show_students(self, items):
        self.new_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self._items = list(items)
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            active = bool(item.get("active", 1))
            values = [str(row + 1), str(item["id"]), item.get("name", ""), item.get("code") or "",
                      "在读" if active else "已停用"]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 2:
                    cell.setToolTip(item.get("name", ""))
                if not active:
                    cell.setForeground(Qt.gray)
                self.table.setItem(row, column, cell)
        self.body.setCurrentIndex(1 if items else 0)
        inactive = len([item for item in items if not item.get("active", 1)])
        if not items:
            self.status.setText("")
        elif inactive:
            self.status.setText("共 %d 人，其中已停用 %d 人" % (len(items), inactive))
        else:
            self.status.setText("共 %d 人" % len(items))
        if items and self.table.currentRow() < 0:
            # 名单刷新后默认高亮第一个学生，按钮马上可用，不用先点一下。
            self.table.selectRow(0)
        self._sync_buttons()

    # ---------------------------------------------------------------- 动作
    def open_create_dialog(self):
        dialog = StudentDialog(self.service, self.class_id, parent=self)
        if dialog.exec() == QDialog.Accepted and dialog.saved:
            self.reload()

    def open_edit(self, *_):
        student = self._selected_student()
        if not student:
            self.status.setText("先在名单里点一个学生")
            return
        dialog = StudentDialog(self.service, self.class_id, student=student, parent=self)
        if dialog.exec() == QDialog.Accepted and dialog.saved:
            self.reload()

    def set_active(self, active):
        student = self._selected_student()
        if not student:
            self.status.setText("先在名单里点一个学生")
            return
        if bool(student.get("active", 1)) == active:
            self.status.setText("这个学生当前已经是%s状态" % ("在读" if active else "已停用"))
            return
        self.deactivate_button.setEnabled(False)
        self.activate_button.setEnabled(False)
        self.status.setText("正在保存…")
        self.runner.run(
            lambda: self.service.set_student_active(student["id"], active),
            self._set_active_done,
            self._set_active_failed,
        )

    def _set_active_done(self, row):
        self.status.setText("%s 已%s（数据仍然保留，可随时启用）" % (row["name"], "启用" if row["active"] else "停用"))
        self.reload()

    def _set_active_failed(self, message):
        self.status.setText(message)
        self._sync_buttons()

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()


class AnswerImportWindow(QMainWindow):
    """按班级导入一份作答 CSV：先在本地逐行校验，老师确认后才写入。

    这一页不发任何网络请求，也不读照片；CSV 内容只在本机内存与数据库里。
    """

    closed = Signal()

    STATUS_TEXT = {"ok": "可导入", "duplicate": "重复", "conflict": "冲突",
                   "overwritten": "已覆盖", "error": "需处理"}
    STATUS_COLOR = {"ok": QColor("#1b7f3b"), "duplicate": QColor("#777777"),
                    "conflict": QColor("#b26a00"), "overwritten": QColor("#1b5e9c"),
                    "error": QColor("#b00020")}

    def __init__(self, service, class_row, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.class_row = class_row
        self.class_id = class_row["id"]
        self._path = ""
        self._text = ""
        self._batch = ""
        self._rows = []
        self._selections = {}
        self._result = None
        self.setObjectName("answerImportWindow")
        self.setWindowTitle("数智伴学 · 导入作答（CSV）")
        fit_to_screen(self, 1040, 680)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · 导入作答" % class_row.get("name", ""))
        heading.setObjectName("answerClassName")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.back_button = QPushButton("返回学生名单")
        self.back_button.setObjectName("backToStudents")
        self.back_button.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(heading, 1)
        top.addWidget(self.back_button)

        self.file_label = QLineEdit()
        self.file_label.setObjectName("answerFileLabel")
        self.file_label.setReadOnly(True)
        self.file_label.setPlaceholderText("还没有选择 CSV 文件")
        self.choose_button = QPushButton("选择 CSV 文件…")
        self.choose_button.setObjectName("answerChooseFile")
        self.choose_button.clicked.connect(self.choose_file)
        self.template_button = QPushButton("下载模板")
        self.template_button.setObjectName("answerTemplateButton")
        self.template_button.clicked.connect(self.save_template)
        self.zip_button = QPushButton("导入作答压缩包…")
        self.zip_button.setObjectName("openAnswerZipImport")
        self.zip_button.clicked.connect(self.open_zip_import)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("文件"))
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.choose_button)
        file_row.addWidget(self.template_button)
        file_row.addWidget(self.zip_button)

        self.hint = QLabel(
            "先在软件里核对每一行，再点「确认导入」。CSV 只在本机读取；导入不会调用 AI，"
            "也不会发送学生姓名、照片或作答内容。模板里 12 列都要保留，缺列会直接提示。"
        )
        self.hint.setObjectName("answerHint")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #555;")

        self.table = QTableWidget(0, 6)
        self.table.setObjectName("answerTable")
        self.table.setHorizontalHeaderLabels(["行", "状态", "记录编号", "学生", "题号", "说明"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.itemSelectionChanged.connect(self._sync_buttons)

        self.pick_button = QPushButton("为这行选择学生…")
        self.pick_button.setObjectName("answerPickStudent")
        self.pick_button.clicked.connect(self.pick_student)
        self.overwrite_check = QCheckBox("覆盖编号相同、内容不同的行")
        self.overwrite_check.setObjectName("answerOverwrite")
        self.overwrite_check.stateChanged.connect(self._sync_buttons)
        self.import_button = QPushButton("确认导入")
        self.import_button.setObjectName("answerImportButton")
        self.import_button.clicked.connect(self.confirm_import)
        self.export_button = QPushButton("导出问题明细")
        self.export_button.setObjectName("answerExportErrors")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_problems)
        self.status = QLabel("")
        self.status.setObjectName("answerStatus")
        self.status.setWordWrap(True)

        actions = QHBoxLayout()
        actions.addWidget(self.pick_button)
        actions.addWidget(self.overwrite_check)
        actions.addWidget(self.import_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.status, 1)

        self.report = QPlainTextEdit()
        self.report.setObjectName("answerReport")
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("导入结果会显示在这里：新增、跳过重复、冲突、未导入的行。")
        self.report.setMaximumHeight(150)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(file_row)
        layout.addWidget(self.hint)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(self.report)

        self._sync_buttons()

    # ---------------------------------------------------------------- 文件
    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择作答 CSV", os.path.dirname(self._path) or "", "CSV 文件 (*.csv);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            self._text = answers_mod.decode(raw)
        except answers_mod.AnswerImportError as exc:
            self.status.setText(str(exc))
            return
        except OSError as exc:
            self.status.setText("读不了这个文件：%s" % exc)
            return
        self._path = path
        self._batch = os.path.splitext(os.path.basename(path))[0][:60]
        self._selections = {}
        self._result = None
        self.file_label.setText(path)
        self.report.clear()
        self.preview()

    def save_template(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存 CSV 模板", "作答导入模板.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            # 带 BOM，老师用 Excel 打开时中文表头不会乱码。
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(answers_mod.template_text())
        except OSError as exc:
            self.status.setText("保存失败：%s" % exc)
            return
        self.status.setText("模板已保存：%s" % path)

    # ------------------------------------------------------- R2-01 压缩包导入
    # CSV 之外的另一条入口：一个 ZIP 里带 answers.csv 与 images/，图片按相对路径
    # 关联到作答（多图用分号分隔）。两个入口互不影响，本页行为一行未改。
    def open_zip_import(self):
        from desktop.ui_import_zip import ZipImportWindow

        window = ZipImportWindow(self.service, self.class_row, parent=self)
        window.setAttribute(Qt.WA_DeleteOnClose, True)
        self._zip_import_window = window
        window.show()
        return window

    # ---------------------------------------------------------------- 预览
    def preview(self):
        if not self._text:
            return
        self.choose_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.status.setText("正在本地逐行校验…")
        text, batch, selections = self._text, self._batch, dict(self._selections)
        self.runner.run(
            lambda: self.service.preview_answers(self.class_id, text, batch, selections),
            self._show_preview,
            self._preview_failed,
        )

    def _preview_failed(self, message):
        self.choose_button.setEnabled(True)
        self._rows = []
        self.table.setRowCount(0)
        self.status.setText(message)
        self._sync_buttons()

    def _show_preview(self, data):
        self.choose_button.setEnabled(True)
        counts = data.get("counts") or {}
        self._fill_rows(data.get("rows") or [])
        self.status.setText(
            "共 %d 行：可导入 %d，重复 %d，冲突 %d，需处理 %d"
            % (len(self._rows), counts.get("ok", 0), counts.get("duplicate", 0),
               counts.get("conflict", 0), counts.get("error", 0))
        )
        self._sync_buttons()

    def _fill_rows(self, rows):
        self._rows = list(rows)
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            status = row.get("status", "error")
            student = row.get("student_name_matched") or ""
            candidates = row.get("candidates") or []
            if not student and candidates:
                student = "（待选择：%s）" % "、".join(c["name"] for c in candidates)
            values = [
                str(row.get("line", "")),
                self.STATUS_TEXT.get(status, status),
                row.get("record_key", ""),
                student,
                row.get("question_no", ""),
                row.get("reason", ""),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 1:
                    cell.setForeground(self.STATUS_COLOR.get(status, QColor("#000")))
                if column == 5:
                    cell.setToolTip(value)
                self.table.setItem(index, column, cell)

    def _selected_row(self):
        index = self.table.currentRow()
        if index < 0 or index >= len(self._rows):
            return None
        return self._rows[index]

    def _sync_buttons(self):
        row = self._selected_row()
        can_pick = bool(row and row.get("candidates"))
        self.pick_button.setEnabled(can_pick)
        counts = {"ok": 0, "conflict": 0}
        for item in self._rows:
            if item.get("status") in counts:
                counts[item["status"]] += 1
        self.import_button.setEnabled(can_pick is False and (
            counts["ok"] > 0 or (counts["conflict"] > 0 and self.overwrite_check.isChecked())
        ))

    # ---------------------------------------------------------------- 选择学生
    def pick_student(self):
        row = self._selected_row()
        if not row or not row.get("candidates"):
            self.status.setText("先在表格里点一行「待选择」的记录")
            return
        labels = ["%s（内部编号 %s）" % (c["name"], c["id"]) for c in row["candidates"]]
        choice, ok = QInputDialog.getItem(self, "选择学生", "这一行属于哪个学生？", labels, 0, False)
        if not ok:
            return
        chosen = row["candidates"][labels.index(choice)]
        self._selections[str(row["line"])] = chosen["id"]
        self.preview()

    # ---------------------------------------------------------------- 导入
    def confirm_import(self):
        if not self._rows:
            return
        counts = {"ok": 0, "duplicate": 0, "conflict": 0, "error": 0}
        for row in self._rows:
            counts[row.get("status", "error")] = counts.get(row.get("status", "error"), 0) + 1
        parts = ["将导入 %d 条" % counts["ok"]]
        if counts["duplicate"]:
            parts.append("跳过重复 %d 条" % counts["duplicate"])
        if counts["conflict"]:
            parts.append("%s %d 条" % ("覆盖冲突" if self.overwrite_check.isChecked() else "暂不导入冲突",
                                      counts["conflict"]))
        if counts["error"]:
            parts.append("%d 条有问题不会导入" % counts["error"])
        text = "，".join(parts) + "。\n\n导入只写入当前班级；确认后开始。"
        if QMessageBox.question(self, "确认导入", text,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.import_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.status.setText("正在导入…")
        text, batch = self._text, self._batch
        selections = dict(self._selections)
        overwrite = self.overwrite_check.isChecked()
        self.runner.run(
            lambda: self.service.import_answers(self.class_id, text, batch, selections, overwrite),
            self._show_import,
            self._import_failed,
        )

    def _import_failed(self, message):
        self.status.setText(message)
        self._sync_buttons()

    def _show_import(self, data):
        self._result = data
        counts = data.get("counts") or {}
        self.status.setText(
            "导入完成：新增 %d，覆盖 %d，跳过重复 %d，未导入冲突 %d，有问题 %d"
            % (counts.get("new", 0), counts.get("overwritten", 0), counts.get("duplicate", 0),
               counts.get("conflict", 0), counts.get("failed", 0))
        )
        lines = ["批次：%s" % (data.get("batch") or self._batch or "未命名"),
                 "一共 %d 行" % data.get("total", len(self._rows)),
                 "新增 %d 条；覆盖 %d 条；跳过重复 %d 条；未导入冲突 %d 条；有问题 %d 条"
                 % (counts.get("new", 0), counts.get("overwritten", 0), counts.get("duplicate", 0),
                    counts.get("conflict", 0), counts.get("failed", 0))]
        problems = [row for row in data.get("rows") or []
                    if row.get("status") in ("error", "conflict")]
        if problems:
            lines.append("")
            lines.append("需要处理的行：")
            for row in problems:
                lines.append("第 %s 行（%s）：%s" % (row.get("line"), row.get("record_key", ""),
                                                    row.get("reason", "")))
        self.report.setPlainText("\n".join(lines))
        rows = [dict(row) for row in data.get("rows") or self._rows]
        for row in rows:
            if row.get("status") == "ok":
                # 刚写进去的这几条，再预览时就是重复；表格按导入后的真实状态显示。
                row["status"] = "duplicate"
        self._fill_rows(rows)
        self.export_button.setEnabled(bool(problems))
        self._sync_buttons()

    def export_problems(self):
        rows = [row for row in self._rows if row.get("status") in ("error", "conflict")]
        if not rows:
            self.status.setText("没有需要导出的问题行")
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存问题明细", "作答导入问题.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["行号", "状态", "记录编号", "学生姓名", "题号", "原因"])
                for row in rows:
                    writer.writerow([
                        row.get("line", ""),
                        self.STATUS_TEXT.get(row.get("status"), row.get("status", "")),
                        row.get("record_key", ""),
                        row.get("student_name", ""),
                        row.get("question_no", ""),
                        row.get("reason", ""),
                    ])
        except OSError as exc:
            self.status.setText("保存失败：%s" % exc)
            return
        self.status.setText("问题明细已保存：%s" % path)

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()


REVIEW_STATUS_TEXT = {"pending": "待核对", "confirmed": "已确认", "rejected": "已拒绝"}


def review_status_text(item):
    """作答列表里的状态：先看缺测，再看审核状态。"""
    label = REVIEW_STATUS_TEXT.get(item.get("review_status") or "", "未分析")
    if item.get("review_status") == "pending" and item.get("review_uncertain"):
        label = "待核对（不确定）"
    if item.get("missing"):
        label = "缺测 · " + label
    return label


class AnswerConsentDialog(QDialog):
    """把一条作答发给云端前的确认框：默认不勾选，取消就不发。"""

    def __init__(self, service_name, model, mode, parent=None):
        super().__init__(parent)
        self.setObjectName("answerConsentDialog")
        self.setWindowTitle("确认把这条作答发给 %s？" % service_name)
        self.setMinimumWidth(560)
        if mode == "custom":
            billing = ("分析可能产生费用或消耗所选服务的额度，"
                       "关掉窗口也不能保证已经发出的请求撤回或免费。")
        else:
            billing = ("分析可能按次计费或计入订阅额度，"
                       "关掉窗口也不能保证已经发出的请求撤回或免费。")
        lines = [
            "服务：%s" % service_name,
            "模型：%s" % (model or "未配置"),
            "",
            "本次会发送：",
            "· 题目文字",
            "· 标准解法",
            "· 学生写的步骤",
            "",
            "不会发送学生姓名、学校编号、班级名称或账号。",
            "",
            billing,
        ]
        text = QLabel("\n".join(lines))
        text.setObjectName("answerConsentText")
        text.setWordWrap(True)
        self.check = QCheckBox("我已知晓发送内容和可能产生的费用，同意这一次发送")
        self.check.setObjectName("answerConsentCheck")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("开始分析")
        self.ok_button.setObjectName("answerConsentOk")
        self.ok_button.setEnabled(False)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.check.toggled.connect(self.ok_button.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(self.check)
        layout.addWidget(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)

    @property
    def agreed(self):
        return self.check.isChecked()


class AnswerReviewDialog(QDialog):
    """一条作答的分析建议：老师看证据、改文字、确认或拒绝。"""

    changed = Signal()

    def __init__(self, service, class_id, answer, review, parent=None):
        super().__init__(parent)
        self.service = service
        self.class_id = class_id
        self.answer = answer
        self.review = dict(review or {})
        self.runner = TaskRunner()
        self.setObjectName("answerReviewDialog")
        self.setWindowTitle("数智伴学 · 分析建议（待老师核对）")
        fit_to_screen(self, 880, 640)

        summary = [
            "学生：%s" % (answer.get("student_name") or ""),
            "题号：%s　单元：%s　作答日期：%s"
            % (answer.get("question_no") or "", answer.get("unit") or "", answer.get("occurred_on") or ""),
            "",
            "【题目】", (answer.get("question_text") or "").strip(),
            "【标准解法】", (answer.get("standard_solution") or "").strip(),
            "【学生步骤】", (answer.get("student_steps") or "").strip() or "（没有写步骤）",
        ]
        self.answer_text = QPlainTextEdit("\n".join(summary))
        self.answer_text.setObjectName("answerReviewText")
        self.answer_text.setReadOnly(True)
        self.answer_text.setMaximumHeight(170)

        self.uncertain_label = QLabel("")
        self.uncertain_label.setObjectName("answerReviewUncertain")
        self.uncertain_label.setWordWrap(True)
        self.uncertain_label.setStyleSheet("color: #8a5a00;")

        self.table = QTableWidget(0, 3)
        self.table.setObjectName("answerCauseTable")
        self.table.setHorizontalHeaderLabels(["可能错因", "学生步骤里的证据", "为什么这样判断"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)

        self.add_row_button = QPushButton("加一条错因")
        self.add_row_button.setObjectName("answerCauseAdd")
        self.add_row_button.clicked.connect(lambda: self._append_row({}))
        self.remove_row_button = QPushButton("删除选中错因")
        self.remove_row_button.setObjectName("answerCauseRemove")
        self.remove_row_button.clicked.connect(self._remove_row)

        self.note_edit = QLineEdit(self.review.get("note") or "")
        self.note_edit.setObjectName("answerReviewNote")
        self.note_edit.setPlaceholderText("补充说明（会写进错题的备注，可留空）")

        self.status = QLabel("")
        self.status.setObjectName("answerReviewStatus")

        self.save_button = QPushButton("保存修改")
        self.save_button.setObjectName("answerReviewSave")
        self.save_button.clicked.connect(self.save)
        self.confirm_button = QPushButton("确认并生成错题")
        self.confirm_button.setObjectName("answerReviewConfirm")
        self.confirm_button.clicked.connect(self.confirm)
        self.reject_button = QPushButton("拒绝这条建议")
        self.reject_button.setObjectName("answerReviewReject")
        self.reject_button.clicked.connect(self.reject_review)
        self.close_button = QPushButton("关闭")
        self.close_button.setObjectName("answerReviewClose")
        self.close_button.clicked.connect(self.close)

        row_buttons = QHBoxLayout()
        row_buttons.addWidget(self.add_row_button)
        row_buttons.addWidget(self.remove_row_button)
        row_buttons.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.reject_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.answer_text)
        layout.addWidget(self.uncertain_label)
        layout.addWidget(self.table, 1)
        layout.addLayout(row_buttons)
        layout.addWidget(self.note_edit)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

        self._fill(self.review)
        self._sync_buttons()

    # ---------------------------------------------------------------- 展示
    def _fill(self, review):
        self.review = dict(review or {})
        causes = self.review.get("causes") or []
        self.table.setRowCount(0)
        for cause in causes:
            self._append_row(cause)
        if not causes:
            self._append_row({})
        note = (self.review.get("note") or "").strip()
        if self.review.get("uncertain"):
            self.uncertain_label.setText("AI 认为信息不足，没有给出确定错因。" + ("说明：%s" % note if note else ""))
            self.uncertain_label.show()
        else:
            self.uncertain_label.hide()
        if note and not self.note_edit.text():
            self.note_edit.setText(note)
        status = self.review.get("status") or ""
        if status == "confirmed":
            self.status.setText("已确认，错题编号 %s。原作答没有被改动。" % (self.review.get("mistake_id") or ""))
        elif status == "rejected":
            self.status.setText("已拒绝这条建议，没有生成错题。")
        else:
            self.status.setText("建议由 AI 生成，还没有确认；确认后才会变成正式错题。")

    def _append_row(self, cause):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, key in enumerate(("cause", "evidence", "reasoning")):
            self.table.setItem(row, column, QTableWidgetItem(str(cause.get(key) or "")))

    def _remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _sync_buttons(self):
        pending = (self.review.get("status") or "pending") == "pending"
        for widget in (self.save_button, self.confirm_button, self.reject_button,
                       self.add_row_button, self.remove_row_button, self.note_edit, self.table):
            widget.setEnabled(pending)

    def causes(self):
        items = []
        for row in range(self.table.rowCount()):
            values = [(self.table.item(row, column).text().strip() if self.table.item(row, column) else "")
                      for column in range(3)]
            if any(values):
                items.append({"cause": values[0], "evidence": values[1], "reasoning": values[2]})
        return items

    # ---------------------------------------------------------------- 动作
    def _busy(self, text):
        self.status.setText(text)
        for widget in (self.save_button, self.confirm_button, self.reject_button):
            widget.setEnabled(False)

    def save(self):
        if self.runner.busy:
            return
        causes, note = self.causes(), self.note_edit.text().strip()
        self._busy("正在保存修改…")
        self.runner.run(lambda: self.service.update_answer_review(self.class_id, self.review["id"], causes, note),
                        self._saved, self._failed)

    def _saved(self, review):
        self._fill(review)
        self._sync_buttons()
        self.status.setText("修改已保存，仍然是待核对状态。")
        self.changed.emit()

    def confirm(self):
        if self.runner.busy:
            return
        if self.review.get("status") not in (None, "", "pending"):
            return
        causes, note = self.causes(), self.note_edit.text().strip()

        def work():
            self.service.update_answer_review(self.class_id, self.review["id"], causes, note)
            return self.service.confirm_answer_review(self.class_id, self.review["id"])

        self._busy("正在生成正式错题…")
        self.runner.run(work, self._confirmed, self._failed)

    def _confirmed(self, review):
        self._fill(review)
        self._sync_buttons()
        self.changed.emit()

    def reject_review(self):
        if self.runner.busy:
            return
        self._busy("正在记录拒绝…")
        self.runner.run(lambda: self.service.reject_answer_review(self.class_id, self.review["id"]),
                        self._rejected, self._failed)

    def _rejected(self, review):
        self._fill(review)
        self._sync_buttons()
        self.changed.emit()

    def _failed(self, message):
        self.status.setText(message)
        self._sync_buttons()

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)


class AnswerListWindow(QMainWindow):
    """一个班级的作答记录：老师选中一条，主动发起分析，再进入核对。"""

    closed = Signal()

    def __init__(self, service, class_row, parent=None):
        super().__init__(parent)
        self.service = service
        self.class_row = class_row
        self.class_id = class_row["id"]
        self.items = []
        self._flash = ""
        self.runner = TaskRunner()
        self.setObjectName("answerListWindow")
        self.setWindowTitle("数智伴学 · 作答分析")
        fit_to_screen(self, 1040, 680)

        central = QWidget()
        self.setCentralWidget(central)
        back = QPushButton("返回学生名单")
        back.setObjectName("backToStudents")
        back.clicked.connect(self.close)
        title = QLabel("%s · 作答分析" % (class_row.get("name") or "班级"))
        title.setObjectName("answerListTitle")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        top = QHBoxLayout()
        top.addWidget(back)
        top.addWidget(title, 1)

        self.batch_combo = QComboBox()
        self.batch_combo.setObjectName("answerBatchCombo")
        self.batch_combo.setMinimumWidth(200)
        self.batch_combo.currentIndexChanged.connect(lambda _index: self.reload())
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("answerRefresh")
        self.refresh_button.clicked.connect(self.reload)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("批次："))
        filters.addWidget(self.batch_combo)
        filters.addWidget(self.refresh_button)
        filters.addStretch(1)

        self.hint = QLabel("CSV 导入不会自动分析。选中一条作答点「分析这条作答」；"
                           "只发送题目文字、标准解法和学生步骤，不发送姓名、学校编号、班级或账号。")
        self.hint.setObjectName("answerListHint")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #555;")

        self.table = QTableWidget(0, 7)
        self.table.setObjectName("answerListTable")
        self.table.setHorizontalHeaderLabels(["状态", "记录编号", "学生", "题号", "单元", "批次", "作答日期"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self.open_review())

        self.analyse_button = QPushButton("分析这条作答")
        self.analyse_button.setObjectName("answerAnalyse")
        self.analyse_button.clicked.connect(self.analyse)
        theme.primary(self.analyse_button)
        self.review_button = QPushButton("查看建议")
        self.review_button.setObjectName("answerOpenReview")
        self.review_button.clicked.connect(self.open_review)
        self.status = QLabel("")
        self.status.setObjectName("answerListStatus")
        actions = QHBoxLayout()
        actions.addWidget(self.analyse_button)
        actions.addWidget(self.review_button)
        actions.addWidget(self.status, 1)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addLayout(filters)
        layout.addWidget(self.hint)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)

        self.reload()

    # ---------------------------------------------------------------- 数据
    def reload(self):
        if self.runner.busy:
            QTimer.singleShot(150, self.reload)
            return
        batch = self.batch_combo.currentData() or ""
        self.status.setText("正在加载作答记录…")
        self.runner.run(lambda: self.service.answer_reviews(self.class_id, batch), self._show, self._failed)

    def _show(self, data):
        items = data.get("items") or []
        batches = data.get("batches") or []
        current = self.batch_combo.currentData() or ""
        self.batch_combo.blockSignals(True)
        self.batch_combo.clear()
        self.batch_combo.addItem("全部批次", "")
        for name in batches:
            self.batch_combo.addItem(name, name)
        index = self.batch_combo.findData(current)
        self.batch_combo.setCurrentIndex(index if index >= 0 else 0)
        self.batch_combo.blockSignals(False)
        self.items = items
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = [
                review_status_text(item),
                item.get("record_key") or "",
                item.get("student_name") or "",
                item.get("question_no") or "",
                item.get("unit") or "",
                item.get("batch") or "",
                item.get("occurred_on") or "",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        if items and self.table.currentRow() < 0:
            first = next((row for row, item in enumerate(items) if not item.get("missing")), 0)
            self.table.selectRow(first)
        if self._flash:
            self.status.setText(self._flash)
            self._flash = ""
        else:
            self.status.setText("共 %d 条作答（筛选：%s）" % (len(items), self.batch_combo.currentText()))
        self._sync_buttons()

    def _failed(self, message):
        self.status.setText(message)
        self.items = []
        self.table.setRowCount(0)
        self._sync_buttons()

    def _selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.items):
            return None
        return self.items[row]

    def _sync_buttons(self):
        item = self._selected()
        self.analyse_button.setEnabled(item is not None and not item.get("missing")
                                       and item.get("review_status") != "confirmed")
        self.analyse_button.setToolTip("已生成错题的作答不再重新分析" if item is not None
                                       and item.get("review_status") == "confirmed" else "")
        self.review_button.setEnabled(item is not None)

    # ---------------------------------------------------------------- 动作
    def analyse(self):
        item = self._selected()
        if item is None:
            self.status.setText("先点一条作答")
            return
        if item.get("missing"):
            self.status.setText("这条作答是缺测，没有足够内容可以分析。")
            return
        if item.get("review_status") == "confirmed":
            self.status.setText("这条作答已经生成过错题，不会再重新分析。要看已确认的建议，点「查看建议」。")
            return
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        settings = self.service.ai_settings()
        mode = settings.get("mode") or "mock"
        if mode != "mock":
            dialog = AnswerConsentDialog(settings.get("service_name") or "云端服务",
                                         settings.get("model") or "", mode, self)
            if not dialog.exec() or not dialog.agreed:
                self.status.setText("已取消，没有发送任何内容。")
                return
        self.status.setText("正在分析这条作答…")
        self.analyse_button.setEnabled(False)
        self.runner.run(
            service_call(lambda: self.service.analyse_answer(self.class_id, item["id"], consent=True)),
            self._analysed, self._analyse_failed)

    def _analysed(self, outcome):
        self._sync_buttons()
        if not outcome.get("ok"):
            self._analyse_failed(outcome)
            return
        result = outcome.get("value") or {}
        if not result.get("ok"):
            self._flash = ""
            self.status.setText("分析没有完成：%s" % (result.get("error") or "请稍后重试。"))
            return
        review = result.get("review") or {}
        self._flash = "分析完成，建议等待老师核对。"
        self.status.setText(self._flash)
        self._open_dialog(review)
        self.reload()

    def _analyse_failed(self, outcome):
        self._sync_buttons()
        self._flash = ""
        if isinstance(outcome, dict):
            status = outcome.get("status") or ""
            code = outcome.get("code") or ""
            message = outcome.get("message") or "请稍后重试。"
            self.status.setText("分析没有完成（%s%s）：%s"
                                % (status or "—", (" " + code) if code else "", message))
        else:
            self.status.setText("分析没有完成：%s" % outcome)

    def open_review(self):
        item = self._selected()
        if item is None:
            self.status.setText("先点一条作答")
            return
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        self.status.setText("正在读取这条作答的建议…")
        self.runner.run(lambda: self.service.answer_review(self.class_id, item["id"]), self._opened, self._failed)

    def _opened(self, data):
        review = data.get("review")
        if not review:
            self.status.setText("这条作答还没有分析过。")
            return
        self._open_dialog(review)

    def _open_dialog(self, review):
        answer = self.service.answer_review(self.class_id, review["answer_id"])["answer"]
        dialog = AnswerReviewDialog(self.service, self.class_id, answer, review, self)
        dialog.changed.connect(self.reload)
        dialog.exec()
        self.reload()

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()


class TutorConsentDialog(QDialog):
    """把个人问题汇总发给云端前的确认框：默认不勾选，取消就不发。"""

    def __init__(self, service_name, model, mode, parent=None):
        super().__init__(parent)
        self.setObjectName("tutorConsentDialog")
        self.setWindowTitle("确认把个人问题发给 %s？" % service_name)
        self.setMinimumWidth(560)
        if mode == "custom":
            billing = ("生成可能产生费用或消耗所选服务的额度，"
                       "关掉窗口也不能保证已经发出的请求撤回或免费。")
        else:
            billing = ("生成可能按次计费或计入订阅额度，"
                       "关掉窗口也不能保证已经发出的请求撤回或免费。")
        lines = [
            "服务：%s" % service_name,
            "模型：%s" % (model or "未配置"),
            "",
            "本次会发送：",
            "· 问题名称与出现次数、加权分",
            "· 相关题目原文",
            "· 学生写的步骤与错因证据",
            "",
            "不会发送学生姓名、学校编号、班级名称或账号。",
            "",
            billing,
        ]
        text = QLabel("\n".join(lines))
        text.setObjectName("tutorConsentText")
        text.setWordWrap(True)
        self.check = QCheckBox("我已知晓发送内容和可能产生的费用，同意这一次发送")
        self.check.setObjectName("tutorConsentCheck")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("生成建议")
        self.ok_button.setObjectName("tutorConsentOk")
        self.ok_button.setEnabled(False)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.check.toggled.connect(self.ok_button.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(self.check)
        layout.addWidget(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)

    @property
    def agreed(self):
        return self.check.isChecked()


class FollowupConsentDialog(QDialog):
    """短追问的两类请求都要先说明发送内容：追问候选、按回答更新错因候选。"""

    def __init__(self, service_name, model, mode, purpose="生成追问候选", parent=None):
        super().__init__(parent)
        self.setObjectName("followupConsentDialog")
        self.setWindowTitle("确认把问题与证据发给 %s？" % service_name)
        self.setMinimumWidth(580)
        if mode == "custom":
            billing = ("发送可能产生费用或消耗所选服务的额度，"
                       "关掉窗口也不能保证已经发出的请求撤回或免费。")
        else:
            billing = ("发送可能按次计费或计入订阅额度，"
                       "关掉窗口也不能保证已经发出的请求撤回或免费。")
        sends = ["· 问题名称与关联的已确认错因", "· 相关题目原文", "· 学生写的步骤原文"]
        if purpose != "生成追问候选":
            sends.append("· 老师这次的追问和学生的回答")
        lines = [
            "服务：%s" % service_name,
            "模型：%s" % (model or "未配置"),
            "",
            "本次会发送：",
        ] + sends + [
            "",
            "不会发送学生姓名、学校编号、班级名称或账号。",
            "AI 给出的内容都是待老师核对的候选，不会替老师确认错因。",
            "",
            billing,
        ]
        text = QLabel("\n".join(lines))
        text.setObjectName("followupConsentText")
        text.setWordWrap(True)
        self.check = QCheckBox("我已知晓发送内容和可能产生的费用，同意这一次发送")
        self.check.setObjectName("followupConsentCheck")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText(purpose)
        self.ok_button.setObjectName("followupConsentOk")
        self.ok_button.setEnabled(False)
        theme.primary(self.ok_button)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.check.toggled.connect(self.ok_button.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(self.check)
        layout.addWidget(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)

    @property
    def agreed(self):
        return self.check.isChecked()


class StudentInsightWindow(QMainWindow):
    """一个学生的个人学情：已确认问题按近期权重排序，并可生成辅导建议。"""

    TUTOR_HINT = ("只统计老师已确认并生成错题的记录；待核对、已拒绝和缺测不计入。"
                  "加权分只用来安排辅导顺序：最近 30 天每次 3 分、31–90 天每次 2 分、更早每次 1 分。")

    def __init__(self, service, class_row, student, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.class_row = class_row
        self.class_id = class_row["id"]
        self.student = student
        self.student_id = student["id"]
        self._items = []
        self._advice = None
        self.setObjectName("studentInsightWindow")
        self.setWindowTitle("数智伴学 · 个人学情")
        fit_to_screen(self, 1080, 780)
        self.setMinimumSize(720, 460)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · %s 的个人学情" % (class_row.get("name", ""), student.get("name", "")))
        heading.setObjectName("insightStudentName")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.ident = QLabel("内部编号 %d · 只看这个学生自己的已确认问题" % student["id"])
        self.ident.setObjectName("insightStudentIdent")
        self.ident.setStyleSheet("color: #5B6673;")
        self.back_button = QPushButton("返回学生名单")
        self.back_button.setObjectName("backToStudentsFromInsight")
        self.back_button.clicked.connect(self.close)
        top = QHBoxLayout()
        top.addWidget(heading)
        top.addWidget(self.ident)
        top.addStretch(1)
        top.addWidget(self.back_button)

        self.hint = QLabel(self.TUTOR_HINT)
        self.hint.setObjectName("insightHint")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #555;")

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("insightSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-size: 15px; font-weight: 600;")

        self.notes = QLabel("")
        self.notes.setObjectName("insightNotes")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color: #a4631a;")

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("insightProblemTable")
        self.table.setHorizontalHeaderLabels(["问题", "出现次数", "加权分", "最近发生", "主要证据"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        detail_title = QLabel("这个问题的错题（点上面的问题查看原题与步骤）")
        detail_title.setStyleSheet("font-weight: 600;")
        self.detail_table = QTableWidget(0, 4)
        self.detail_table.setObjectName("insightMistakeTable")
        self.detail_table.setHorizontalHeaderLabels(["发生日期", "题目", "学生写的步骤", "错因证据"])
        dheader = self.detail_table.horizontalHeader()
        dheader.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        dheader.setSectionResizeMode(1, QHeaderView.Stretch)
        dheader.setSectionResizeMode(2, QHeaderView.Stretch)
        dheader.setSectionResizeMode(3, QHeaderView.Stretch)
        self.detail_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QTableWidget.SelectRows)

        advice_title = QLabel("辅导建议（老师可以改写，保存后以你的版本为准）")
        advice_title.setStyleSheet("font-weight: 600;")
        self.stale = QLabel("")
        self.stale.setObjectName("insightStale")
        self.stale.setWordWrap(True)
        self.stale.setStyleSheet("color: #a4631a; font-weight: 600;")
        self.stale.hide()
        self.advice_text = QPlainTextEdit()
        self.advice_text.setObjectName("insightAdviceText")
        self.advice_text.setPlaceholderText("还没有辅导建议。点「生成辅导建议」让 AI 提一份候选，或者自己直接写。")
        self.advice_meta = QLabel("")
        self.advice_meta.setObjectName("insightAdviceMeta")
        self.advice_meta.setStyleSheet("color: #666;")

        self.generate_button = QPushButton("生成辅导建议")
        self.generate_button.setObjectName("insightGenerate")
        self.generate_button.clicked.connect(self.generate)
        theme.primary(self.generate_button)
        self.regenerate_button = QPushButton("重新生成")
        self.regenerate_button.setObjectName("insightRegenerate")
        self.regenerate_button.clicked.connect(self.generate)
        self.save_button = QPushButton("保存修改")
        self.save_button.setObjectName("insightSave")
        self.save_button.clicked.connect(self.save)
        self.followup_button = QPushButton("追问这个问题")
        self.followup_button.setObjectName("insightFollowup")
        self.followup_button.setToolTip("针对选中的问题生成或手写一句短追问，记录学生回答")
        self.followup_button.clicked.connect(self.open_followup)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("insightRefresh")
        self.refresh_button.clicked.connect(self.reload)
        self.status = QLabel("")
        self.status.setObjectName("insightStatus")
        self.status.setWordWrap(True)
        actions = QHBoxLayout()
        actions.addWidget(self.generate_button)
        actions.addWidget(self.regenerate_button)
        actions.addWidget(self.save_button)
        actions.addWidget(self.followup_button)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.status, 1)

        # 表格与建议区放进可滚动区域：1366×768 的屏幕上内容放不下时出滚动条，区块不会互相压住。
        self.table.setMinimumHeight(140)
        self.detail_table.setMinimumHeight(120)
        self.advice_text.setMinimumHeight(110)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        body_layout.addWidget(self.hint)
        body_layout.addWidget(self.summary_label)
        body_layout.addWidget(self.notes)
        body_layout.addWidget(self.table, 2)
        body_layout.addWidget(detail_title)
        body_layout.addWidget(self.detail_table, 2)
        body_layout.addWidget(advice_title)
        body_layout.addWidget(self.stale)
        body_layout.addWidget(self.advice_text, 3)
        body_layout.addWidget(self.advice_meta)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("insightScroll")
        self.scroll.setWidgetResizable(True)
        # 不让内容的最小宽度把窗口撑宽（125% 缩放下会超过 1366 宽），内容改用滚动条查看。
        self.scroll.setMinimumSize(300, 160)
        self.scroll.setWidget(body)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(actions)

        self.reload()

    # ---------------------------------------------------------------- 数据
    def reload(self):
        if self.runner.busy:
            QTimer.singleShot(150, self.reload)
            return
        self.status.setText("正在加载个人学情…")
        self.runner.run(lambda: self.service.student_insight(self.class_id, self.student_id),
                        self._show, self._failed)

    def _show(self, data):
        summary = data.get("summary") or {}
        self._items = list(summary.get("items") or [])
        self._advice = data.get("advice") or None
        self.table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            evidence = item.get("evidence") or []
            first = evidence[0].get("question_text") if evidence else ""
            values = [
                item.get("name") or "",
                str(item.get("count") or 0),
                str(item.get("weight") or 0),
                item.get("last_on") or "日期缺失",
                first or "（没有可引用的原文）",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 4:
                    cell.setToolTip(value)
                self.table.setItem(row, column, cell)
        parts = ["已确认错题 %d 条，其中 %d 条参与加权。" % (
            summary.get("confirmed") or 0, summary.get("counted") or 0)]
        if not self._items:
            parts.append("这名学生还没有已确认的错题，暂时没有需要优先辅导的问题。")
        self.summary_label.setText("".join(parts))
        notes = []
        undated = summary.get("undated") or []
        anomalies = summary.get("anomalies") or []
        if undated:
            notes.append("有 %d 条已确认错题没有发生日期，单独列出，不参与加权。" % len(undated))
        for item in anomalies:
            notes.append("有一条错题的%s（错题编号 %s），已排除在加权之外。"
                         % (item.get("reason") or "日期异常", item.get("id")))
        self.notes.setText("\n".join(notes))
        self.notes.setVisible(bool(notes))
        self._show_advice()
        if self._items and self.table.currentRow() < 0:
            self.table.selectRow(0)
        elif not self._items:
            self.detail_table.setRowCount(0)
        self._selection_changed()
        if getattr(self, "_flash", ""):
            self.status.setText(self._flash)
            self._flash = ""
        else:
            self.status.setText("共 %d 个问题" % len(self._items) if self._items else "")

    def _show_advice(self):
        advice = self._advice or {}
        content = advice.get("content") or ""
        self.advice_text.setPlainText(content)
        stale = bool(advice.get("stale")) and bool(content)
        self.stale.setVisible(stale)
        if stale:
            self.stale.setText("这份建议生成之后，已确认错题有新增或变化，可能需要更新；"
                               "不要直接当成最新结论。")
        bits = []
        if advice.get("generated_at"):
            bits.append("生成时间 %s" % advice["generated_at"].replace("T", " ")[:16])
        if advice.get("teacher_edited"):
            bits.append("已按老师的修改保存")
        self.advice_meta.setText("　".join(bits))
        has_content = bool(content.strip())
        self.save_button.setEnabled(has_content)
        self.regenerate_button.setEnabled(has_content and bool(self._items))
        self.generate_button.setEnabled(bool(self._items))
        self.generate_button.setText("生成辅导建议" if not content else "生成辅导建议（覆盖当前内容）")

    def _failed(self, message):
        self.status.setText(message)
        self.summary_label.setText("")
        self.table.setRowCount(0)
        self.detail_table.setRowCount(0)
        self._items = []
        self._sync_actions()

    def _sync_actions(self):
        active = not self.runner.busy
        if not active:
            self.generate_button.setEnabled(False)
            self.regenerate_button.setEnabled(False)
            self.save_button.setEnabled(False)

    def _selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _selection_changed(self):
        item = self._selected()
        self.followup_button.setEnabled(item is not None)
        if item is None:
            self.detail_table.setRowCount(0)
            return
        ids = item.get("mistake_ids") or []
        try:
            rows = self.service.mistakes_by_ids(ids, full=True)
        except Exception as exc:  # 查看明细失败不影响汇总
            self.detail_table.setRowCount(0)
            self.status.setText("读取错题明细失败：%s" % exc)
            return
        self.detail_table.setRowCount(len(rows))
        for row, mistake in enumerate(rows):
            steps = mistake.get("solution_text") or mistake.get("student_steps") or ""
            evidence = mistake.get("observed_errors") or []
            values = [
                mistake.get("occurred_on") or "日期缺失",
                mistake.get("question_text") or "",
                steps,
                "；".join([str(e) for e in evidence]),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                self.detail_table.setItem(row, column, cell)

    # ---------------------------------------------------------------- 动作
    def generate(self):
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        if not self._items:
            self.status.setText("这名学生还没有已确认的错题，不能生成辅导建议。")
            return
        settings = self.service.ai_settings()
        mode = settings.get("mode") or "mock"
        if mode != "mock":
            dialog = TutorConsentDialog(settings.get("service_name") or "云端服务",
                                        settings.get("model") or "", mode, self)
            if not dialog.exec() or not dialog.agreed:
                self.status.setText("已取消，没有发送任何内容。")
                return
        self.status.setText("正在生成辅导建议…")
        self.generate_button.setEnabled(False)
        self.regenerate_button.setEnabled(False)
        self.runner.run(
            lambda: self.service.generate_student_advice(self.class_id, self.student_id, consent=True),
            self._generated, self._generate_failed,
        )

    def _generated(self, result):
        if not result.get("ok"):
            self.status.setText("没有生成辅导建议：%s" % (result.get("error") or "请稍后重试。"))
            self._show_advice()
            return
        self.status.setText("已生成候选建议，请核对后保存或改写。")
        self.reload()

    def _generate_failed(self, message):
        self.status.setText("没有生成辅导建议：%s" % message)
        self._show_advice()

    def save(self):
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        content = self.advice_text.toPlainText()
        if not content.strip():
            self.status.setText("辅导建议不能留空。")
            return
        self.status.setText("正在保存…")
        self.save_button.setEnabled(False)
        self.runner.run(lambda: self.service.save_student_advice(self.class_id, self.student_id, content),
                        self._saved, self._save_failed)

    def _saved(self, result):
        self._flash = "已保存，以你修改后的内容为准。"
        self.status.setText(self._flash)
        self.reload()

    def _save_failed(self, message):
        self.status.setText("没有保存：%s" % message)
        self.save_button.setEnabled(True)

    def open_followup(self):
        """从问题行进入追问：带上这个问题下的已确认错题编号。"""
        item = self._selected()
        if item is None:
            self.status.setText("先点一个问题，再开追问。")
            return
        window = FollowupWindow(self.service, self.class_row, self.student,
                                item.get("name") or "未分类", item.get("mistake_ids") or [], parent=self)
        self._followup_window = window
        window.show()
        window.raise_()

    def closeEvent(self, event):
        super().closeEvent(event)


class FollowupWindow(QMainWindow):
    """教师短追问：先看依据，再选一句追问，记录学生回答，最后核对错因候选。"""

    HINT = ("只发送问题名称、题目原文、学生步骤和已确认错因；不发送姓名、学校编号、班级或账号。"
            "AI 给出的追问和错因都只是候选，要老师点「使用」或「接受」才算数。")

    def __init__(self, service, class_row, student, problem, mistake_ids=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.class_row = class_row
        self.class_id = class_row["id"]
        self.student = student
        self.student_id = student["id"]
        self.problem = problem or "未分类"
        self.mistake_ids = list(mistake_ids or [])
        self._candidates = []
        self._context = {}
        self._followups = []
        self._current = None
        self._update = None
        self._flash = ""
        self._deciding = ""

        self.setObjectName("followupWindow")
        self.setWindowTitle("数智伴学 · 教师短追问")
        fit_to_screen(self, 1040, 820)
        self.setMinimumSize(760, 460)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · %s 的短追问" % (class_row.get("name", ""), student.get("name", "")))
        heading.setObjectName("followupStudentName")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.ident = QLabel("问题：%s · 内部编号 %d" % (self.problem, student["id"]))
        self.ident.setObjectName("followupProblemName")
        self.ident.setStyleSheet("color: #5B6673;")
        self.back_button = QPushButton("关掉追问")
        self.back_button.setObjectName("followupClose")
        self.back_button.clicked.connect(self.close)
        top = QHBoxLayout()
        top.addWidget(heading)
        top.addWidget(self.ident)
        top.addStretch(1)
        top.addWidget(self.back_button)

        self.hint = QLabel(self.HINT)
        self.hint.setObjectName("followupHint")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #555;")

        evidence_title = QLabel("这次追问的依据（原文，只读）")
        evidence_title.setStyleSheet("font-weight: 600;")
        self.evidence = QPlainTextEdit()
        self.evidence.setObjectName("followupEvidence")
        self.evidence.setReadOnly(True)
        self.evidence.setFixedHeight(120)

        suggest_title = QLabel("追问候选（最多 3 条；选一条点「使用这句」，也可以自己写）")
        suggest_title.setStyleSheet("font-weight: 600;")
        self.candidates = QTableWidget(0, 2)
        self.candidates.setObjectName("followupCandidates")
        self.candidates.setHorizontalHeaderLabels(["追问", "依据（原文）"])
        cheader = self.candidates.horizontalHeader()
        cheader.setSectionResizeMode(0, QHeaderView.Stretch)
        cheader.setSectionResizeMode(1, QHeaderView.Stretch)
        self.candidates.setEditTriggers(QTableWidget.NoEditTriggers)
        self.candidates.setSelectionBehavior(QTableWidget.SelectRows)
        self.candidates.setSelectionMode(QTableWidget.SingleSelection)
        self.candidates.itemSelectionChanged.connect(self._pick_candidate)
        self.candidates.setFixedHeight(110)
        self.suggest_note = QLabel("")
        self.suggest_note.setObjectName("followupSuggestNote")
        self.suggest_note.setWordWrap(True)
        self.suggest_note.setStyleSheet("color: #a4631a;")
        self.suggest_button = QPushButton("生成追问候选")
        self.suggest_button.setObjectName("followupSuggest")
        self.suggest_button.clicked.connect(self.suggest)

        self.question = QLineEdit()
        self.question.setObjectName("followupQuestion")
        self.question.setPlaceholderText("写一句问学生的话，例如：这一步你是把 6 看成减 6 了吗？")
        self.use_button = QPushButton("使用这句追问")
        self.use_button.setObjectName("followupUse")
        self.use_button.clicked.connect(self.use_question)
        theme.primary(self.use_button)
        self.question.textChanged.connect(self._sync)
        self.source_label = QLabel("")
        self.source_label.setObjectName("followupSource")
        self.source_label.setStyleSheet("color: #666;")
        use_row = QHBoxLayout()
        use_row.addWidget(QLabel("这次追问"))
        use_row.addWidget(self.question, 1)
        use_row.addWidget(self.use_button)
        use_row.addWidget(self.source_label)

        history_title = QLabel("这个学生的追问记录（点一条查看回答与错因候选）")
        history_title.setStyleSheet("font-weight: 600;")
        self.history = QTableWidget(0, 4)
        self.history.setObjectName("followupHistory")
        self.history.setHorizontalHeaderLabels(["提问时间", "追问", "学生回答", "状态"])
        hheader = self.history.horizontalHeader()
        hheader.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hheader.setSectionResizeMode(1, QHeaderView.Stretch)
        hheader.setSectionResizeMode(2, QHeaderView.Stretch)
        hheader.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.history.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history.setSelectionBehavior(QTableWidget.SelectRows)
        self.history.setSelectionMode(QTableWidget.SingleSelection)
        self.history.itemSelectionChanged.connect(self._history_changed)
        self.history.setFixedHeight(120)

        answer_title = QLabel("学生的回答（照学生说的记，不用改写）")
        answer_title.setStyleSheet("font-weight: 600;")
        self.answer = QPlainTextEdit()
        self.answer.setObjectName("followupAnswer")
        self.answer.setPlaceholderText("学生原话，或者你听写下来的回答。")
        self.answer.setFixedHeight(70)
        self.save_answer_button = QPushButton("保存回答")
        self.save_answer_button.setObjectName("followupSaveAnswer")
        self.save_answer_button.clicked.connect(self.save_answer)
        self.answer_meta = QLabel("")
        self.answer_meta.setObjectName("followupAnswerMeta")
        self.answer_meta.setStyleSheet("color: #666;")
        answer_row = QHBoxLayout()
        answer_row.addWidget(self.save_answer_button)
        answer_row.addWidget(self.answer_meta, 1)

        update_title = QLabel("按回答更新错因候选（AI 生成的只是候选，接受后才会写进这条错题）")
        update_title.setStyleSheet("font-weight: 600;")
        self.compare = QTableWidget(0, 2)
        self.compare.setObjectName("followupCompare")
        self.compare.setHorizontalHeaderLabels(["更新前（已确认的错因）", "更新后（候选，待核对）"])
        cmpheader = self.compare.horizontalHeader()
        cmpheader.setSectionResizeMode(0, QHeaderView.Stretch)
        cmpheader.setSectionResizeMode(1, QHeaderView.Stretch)
        self.compare.setEditTriggers(QTableWidget.NoEditTriggers)
        self.compare.setFixedHeight(110)
        self.update_note = QLabel("")
        self.update_note.setObjectName("followupUpdateNote")
        self.update_note.setWordWrap(True)
        self.update_note.setStyleSheet("color: #a4631a;")
        self.update_button = QPushButton("让 AI 更新错因候选")
        self.update_button.setObjectName("followupUpdate")
        self.update_button.clicked.connect(self.update_causes)
        self.accept_button = QPushButton("接受候选")
        self.accept_button.setObjectName("followupAccept")
        self.accept_button.clicked.connect(lambda: self.decide("accept"))
        theme.primary(self.accept_button)
        self.reject_button = QPushButton("拒绝候选")
        self.reject_button.setObjectName("followupReject")
        self.reject_button.clicked.connect(lambda: self.decide("reject"))
        theme.danger(self.reject_button)
        update_row = QHBoxLayout()
        update_row.addWidget(self.update_button)
        update_row.addWidget(self.accept_button)
        update_row.addWidget(self.reject_button)
        update_row.addWidget(self.update_note, 1)

        self.status = QLabel("")
        self.status.setObjectName("followupStatus")
        self.status.setWordWrap(True)

        # 内容较长，整块放进可滚动区域：小屏上出滚动条，不再把区块压到一起。
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        body_layout.addWidget(self.hint)
        body_layout.addWidget(evidence_title)
        body_layout.addWidget(self.evidence)
        suggest_head = QHBoxLayout()
        suggest_head.addWidget(suggest_title, 1)
        suggest_head.addWidget(self.suggest_button)
        body_layout.addLayout(suggest_head)
        body_layout.addWidget(self.candidates)
        body_layout.addWidget(self.suggest_note)
        body_layout.addLayout(use_row)
        body_layout.addWidget(history_title)
        body_layout.addWidget(self.history)
        body_layout.addWidget(answer_title)
        body_layout.addWidget(self.answer)
        body_layout.addLayout(answer_row)
        body_layout.addWidget(update_title)
        body_layout.addWidget(self.compare)
        body_layout.addLayout(update_row)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("followupScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumSize(320, 180)
        self.scroll.setWidget(body)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.status)

        self.reload()

    # ---------------------------------------------------------------- 数据
    def reload(self):
        if self.runner.busy:
            QTimer.singleShot(150, self.reload)
            return
        self.status.setText("正在加载…")
        self.runner.run(self._load, self._loaded, self._failed)

    def _load(self):
        context = self.service.followup_context(self.class_id, self.student_id, self.problem,
                                                self.mistake_ids[0] if self.mistake_ids else None)
        items = self.service.list_followups(self.class_id, self.student_id)
        return {"context": context, "items": items}

    def _loaded(self, data):
        self._context = data.get("context") or {}
        self._followups = list(data.get("items") or [])
        self._show_evidence()
        self._show_history()
        if self._flash:
            self.status.setText(self._flash)
            self._flash = ""
        else:
            self.status.setText("共 %d 条追问记录" % len(self._followups))
        self._sync()

    def _failed(self, message):
        self.status.setText(message)
        if not self.evidence.toPlainText().strip():
            self.evidence.setPlainText("没有读到这条问题的原文。%s" % message)
        self._show_history()

    def _show_evidence(self):
        context = self._context
        lines = []
        if context.get("question_text"):
            lines.append("【题目】%s" % context["question_text"])
        if context.get("student_steps"):
            lines.append("【学生过程】%s" % context["student_steps"])
        causes = context.get("causes") or []
        if causes:
            lines.append("【已确认错因】%s" % "；".join([str(c) for c in causes]))
        if not lines:
            lines.append("没有读到这条问题的原文，先回个人学情刷新再看。")
        self.evidence.setPlainText("\n".join(lines))

    def _status_text(self, item):
        status = item.get("status") or "asked"
        latest = item.get("latest") or None
        if status == "answered":
            text = "已记录回答"
        else:
            text = "已提问，等回答"
        if latest and latest.get("status") == "pending":
            text += " · 有候选取舍未决定"
        elif latest and latest.get("status") == "accepted":
            text += " · 候选已接受"
        elif latest and latest.get("status") == "rejected":
            text += " · 候选已拒绝"
        return text

    def _pick_candidate(self):
        row = self.candidates.currentRow()
        if 0 <= row < len(self._candidates):
            self.question.setText(self._candidates[row].get("question") or "")
            self._sync()

    def _show_history(self):
        self.history.setRowCount(len(self._followups))
        for row, item in enumerate(self._followups):
            values = [
                (item.get("asked_at") or "").replace("T", " ")[:16],
                item.get("question") or "",
                item.get("answer") or "（还没有记录回答）",
                self._status_text(item),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                self.history.setItem(row, column, cell)
        if self._followups and self.history.currentRow() < 0:
            self.history.selectRow(0)
        elif self._followups:
            # 列表刷新后选中行没变，Qt 不会重发选中信号，这里补一次，保证回答时间与候选跟着刷新
            self._history_changed()
        elif not self._followups:
            self._current = None
            self.compare.setRowCount(0)
            self.update_note.setText("")

    def _history_changed(self):
        row = self.history.currentRow()
        if row < 0 or row >= len(self._followups):
            return
        self._current = self._followups[row]
        self.answer.setPlainText(self._current.get("answer") or "")
        answered = (self._current.get("status") or "") == "answered"
        if self._current.get("answered_at"):
            self.answer_meta.setText("回答时间 %s" % self._current["answered_at"].replace("T", " ")[:16])
        else:
            self.answer_meta.setText("还没有保存回答")
        self.save_answer_button.setEnabled(True)
        self.update_button.setEnabled(answered)
        self._show_update(self._current.get("latest") or None)
        self._sync()

    def _show_update(self, latest):
        self.compare.setRowCount(0)
        self.update_note.setText("")
        self._update = latest
        if not latest:
            self.accept_button.setEnabled(False)
            self.reject_button.setEnabled(False)
            self.update_note.setText("这条追问还没有错因候选。" if self._current else "")
            return
        before = latest.get("before_json") or []
        causes = latest.get("causes") or []
        after = [item.get("cause") if isinstance(item, dict) else str(item) for item in causes]
        if not after:
            after = latest.get("after_json") or []
        rows = max(len(before), len(after))
        self.compare.setRowCount(rows)
        for row in range(rows):
            left = before[row] if row < len(before) else ""
            right = after[row] if row < len(after) else ""
            self.compare.setItem(row, 0, QTableWidgetItem(str(left)))
            self.compare.setItem(row, 1, QTableWidgetItem(str(right)))
        pending = (latest.get("status") or "") == "pending"
        self.accept_button.setEnabled(pending)
        self.reject_button.setEnabled(pending)
        note = latest.get("note") or ""
        if latest.get("uncertain"):
            note = ("AI 认为这次回答还不足以确定错因。" + note).strip()
        if not pending:
            note = "这条候选已经处理过了。" + note
        self.update_note.setText(note)

    def _sync(self):
        busy = self.runner.busy
        has_context = bool(self._context.get("question_text") or self._context.get("student_steps"))
        self.suggest_button.setEnabled(not busy and has_context)
        self.use_button.setEnabled(not busy and bool(self.question.text().strip()))
        self.save_answer_button.setEnabled(not busy and self._current is not None)
        self.update_button.setEnabled(not busy and self._current is not None
                                       and (self._current.get("status") or "") == "answered")
        if busy:
            self.accept_button.setEnabled(False)
            self.reject_button.setEnabled(False)

    # ---------------------------------------------------------------- 动作
    def suggest(self):
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        if not (self._context.get("question_text") or self._context.get("student_steps")):
            self.status.setText("没有读到这条问题的原文，先回个人学情刷新再看。")
            return
        settings = self.service.ai_settings()
        mode = settings.get("mode") or "mock"
        if mode != "mock":
            dialog = FollowupConsentDialog(settings.get("service_name") or "云端服务",
                                           settings.get("model") or "", mode, "生成追问候选", self)
            if not dialog.exec() or not dialog.agreed:
                self.status.setText("已取消，没有发送任何内容。")
                return
        else:
            self.status.setText("当前是模拟分析，不会生成追问候选；你可以自己写一句。")
            return
        self.status.setText("正在生成追问候选…")
        self.suggest_button.setEnabled(False)
        self.runner.run(
            lambda: self.service.suggest_followup(self.class_id, self.student_id, self.problem,
                                                  self.mistake_ids[0] if self.mistake_ids else None,
                                                  consent=True),
            self._suggested, self._suggest_failed,
        )

    def _suggested(self, result):
        if not result.get("ok"):
            self.status.setText("没有生成追问候选：%s" % (result.get("error") or "请稍后重试。"))
            self._sync()
            return
        self._candidates = list(result.get("candidates") or [])
        self.candidates.setRowCount(len(self._candidates))
        for row, item in enumerate(self._candidates):
            self.candidates.setItem(row, 0, QTableWidgetItem(item.get("question") or ""))
            self.candidates.setItem(row, 1, QTableWidgetItem(item.get("evidence") or ""))
        note = result.get("note") or ""
        self.suggest_note.setText(note)
        if not self._candidates:
            self.status.setText("这次没有可用的追问候选；可以自己写一句问学生。")
        else:
            self.candidates.selectRow(0)
            self.status.setText("已生成 %d 条候选，选一条点「使用这句追问」。" % len(self._candidates))
        self._sync()

    def _suggest_failed(self, message):
        self.status.setText("没有生成追问候选：%s" % message)
        self._sync()

    def use_question(self):
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        text = self.question.text().strip()
        if not text:
            self.status.setText("先写一句追问，或从候选里选一条。")
            return
        row = self.candidates.currentRow()
        suggestion = None
        source = "teacher"
        if row >= 0 and row < len(self._candidates):
            chosen = self._candidates[row]
            if (chosen.get("question") or "") == text:
                suggestion = chosen
                source = "ai"
        self.status.setText("正在记录这次追问…")
        self.use_button.setEnabled(False)
        self.runner.run(
            lambda: self.service.create_followup(self.class_id, self.student_id, self.problem, text,
                                                 source=source,
                                                 mistake_id=self.mistake_ids[0] if self.mistake_ids else None,
                                                 suggestion=suggestion),
            self._used, self._use_failed,
        )

    def _used(self, result):
        followup = result or {}
        self.status.setText("已记录这次追问；学生回答后，在下面填写并保存。")
        self.question.clear()
        self.source_label.setText("来源：%s" % ("AI 候选，老师确认使用" if (followup.get("source") == "ai") else "老师手写"))
        self.reload()

    def _use_failed(self, message):
        self.status.setText("没有记录：%s" % message)
        self._sync()

    def save_answer(self):
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        if self._current is None:
            self.status.setText("先在下面的记录里点一条追问。")
            return
        text = self.answer.toPlainText()
        if not text.strip():
            self.status.setText("学生回答不能留空。")
            return
        self.status.setText("正在保存回答…")
        self.save_answer_button.setEnabled(False)
        self.runner.run(
            lambda: self.service.save_followup_answer(self.class_id, self.student_id,
                                                      self._current["id"], text),
            self._answered, self._answer_failed,
        )

    def _answered(self, result):
        self._flash = "已保存学生回答。可以点「让 AI 更新错因候选」。"
        self.status.setText(self._flash)
        self.reload()

    def _answer_failed(self, message):
        self.status.setText("没有保存回答：%s" % message)
        self._sync()

    def update_causes(self):
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        if self._current is None:
            self.status.setText("先在下面的记录里点一条追问。")
            return
        if (self._current.get("status") or "") != "answered":
            self.status.setText("先记下学生的回答，再让 AI 更新错因候选。")
            return
        settings = self.service.ai_settings()
        mode = settings.get("mode") or "mock"
        if mode != "mock":
            dialog = FollowupConsentDialog(settings.get("service_name") or "云端服务",
                                           settings.get("model") or "", mode, "更新错因候选", self)
            if not dialog.exec() or not dialog.agreed:
                self.status.setText("已取消，没有发送任何内容。")
                return
        else:
            self.status.setText("当前是模拟分析，不会更新错因候选。")
            return
        self.status.setText("正在根据回答更新错因候选…")
        self.update_button.setEnabled(False)
        self.runner.run(
            lambda: self.service.update_followup_causes(self.class_id, self.student_id,
                                                        self._current["id"], consent=True),
            self._updated, self._update_failed,
        )

    def _updated(self, result):
        if not result.get("ok"):
            self.status.setText("没有更新错因候选：%s" % (result.get("error") or "请稍后重试。"))
            self._sync()
            return
        self.status.setText("已生成候选，请对比更新前后的内容，再决定接受或拒绝。")
        self.reload()

    def _update_failed(self, message):
        self.status.setText("没有更新错因候选：%s" % message)
        self._sync()

    def decide(self, action):
        if self.runner.busy:
            self.status.setText("上一个操作还在处理，请稍后再点。")
            return
        if self._current is None or not self._update:
            self.status.setText("先选一条有错因候选的追问。")
            return
        if (self._update.get("status") or "") != "pending":
            self.status.setText("这条候选已经处理过了。")
            return
        self.status.setText("正在保存决定…")
        self._deciding = action
        self.accept_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.runner.run(
            lambda: self.service.decide_followup_update(self.class_id, self.student_id,
                                                        self._current["id"], self._update["id"], action),
            self._decided, self._decide_failed,
        )

    def _decided(self, result):
        if getattr(self, "_deciding", "") == "reject":
            self._flash = "已拒绝这条候选；错题和追问记录都没有改动。"
        else:
            self._flash = "已接受这条候选，写进了这条错题的错因；老师可以再到错题详情里修改。"
        self.status.setText(self._flash)
        self.reload()

    def _decide_failed(self, message):
        self.status.setText("没有保存决定：%s" % message)
        self._sync()

    def closeEvent(self, event):
        super().closeEvent(event)


class StudentStatsWindow(QMainWindow):
    """一个学生的学情统计：只看已确认错题，可从统计数字找到对应错题。"""

    closed = Signal()
    open_mistake = Signal(dict, int)

    def __init__(self, service, class_row, student, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.basis_runner = TaskRunner()
        self.class_row = class_row
        self.student = student
        self._closed = False
        self._seq = 0
        self._basis_seq = 0
        self._basis_pending = None
        self._query_busy = False
        self._want_reload = False
        self._stats = None
        self._kp_items = []
        self._error_items = []
        self._basis_rows = []
        self.setObjectName("studentStatsWindow")
        self.setWindowTitle("数智伴学 · 学情统计")
        fit_to_screen(self, 1080, 720)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · %s" % (class_row.get("name", ""), student.get("name", "")))
        heading.setObjectName("statsStudentName")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        ident = QLabel("内部编号 %d · %s" % (
            student["id"], "在读" if student.get("active", 1) else "已停用（仍可查看历史学情）"
        ))
        ident.setObjectName("statsStudentIdent")
        ident.setStyleSheet("color: #666;")
        self.back_button = QPushButton("返回学生名单")
        self.back_button.setObjectName("backToStudentsFromStats")
        self.back_button.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(heading)
        top.addWidget(ident)
        top.addStretch(1)
        top.addWidget(self.back_button)

        self.from_check = QCheckBox("从")
        self.from_check.setObjectName("statsFromCheck")
        self.from_date = QDateEdit(QDate.currentDate())
        self.from_date.setObjectName("statsFromDate")
        self.from_date.setDisplayFormat("yyyy-MM-dd")
        self.from_date.setCalendarPopup(True)
        self.from_date.setEnabled(False)
        self.to_check = QCheckBox("到")
        self.to_check.setObjectName("statsToCheck")
        self.to_date = QDateEdit(QDate.currentDate())
        self.to_date.setObjectName("statsToDate")
        self.to_date.setDisplayFormat("yyyy-MM-dd")
        self.to_date.setCalendarPopup(True)
        self.to_date.setEnabled(False)
        self.from_check.toggled.connect(self.from_date.setEnabled)
        self.to_check.toggled.connect(self.to_date.setEnabled)
        self.query_button = QPushButton("查询")
        self.query_button.setObjectName("statsQueryButton")
        self.query_button.clicked.connect(self.reload)
        self.reset_button = QPushButton("重置")
        self.reset_button.setObjectName("statsResetButton")
        self.reset_button.clicked.connect(self.reset_filters)
        self.dashboard_button = QPushButton("数据看板")
        self.dashboard_button.setObjectName("statsOpenDashboard")
        self.dashboard_button.setToolTip("个人看板：知识点/错因分布、重复错误明细、重点问题排序")
        self.dashboard_button.clicked.connect(self.open_dashboard)
        self.report_button = QPushButton("导出报告")
        self.report_button.setObjectName("statsExportReport")
        self.report_button.setToolTip("预览个人学情报告并导出 PDF / 打印")
        self.report_button.clicked.connect(self.open_report)
        self.status = QLabel("")
        self.status.setObjectName("statsStatus")

        filters = QHBoxLayout()
        filters.addWidget(self.from_check)
        filters.addWidget(self.from_date)
        filters.addWidget(self.to_check)
        filters.addWidget(self.to_date)
        filters.addWidget(self.query_button)
        filters.addWidget(self.reset_button)
        filters.addWidget(self.dashboard_button)
        filters.addWidget(self.report_button)
        filters.addWidget(self.status, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("statsSummary")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.notes = QLabel("")
        self.notes.setObjectName("statsNotes")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color: #555;")

        kp_title = QLabel("知识点分布（点一行看依据）")
        kp_title.setStyleSheet("font-weight: 600;")
        self.kp_table = QTableWidget(0, 2)
        self.kp_table.setObjectName("statsKnowledgeTable")
        self.kp_table.setHorizontalHeaderLabels(["知识点", "错题条数"])
        self.kp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.kp_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._setup_table(self.kp_table)
        self.kp_table.itemSelectionChanged.connect(lambda: self._basis_from_table("kp"))
        self.kp_table.cellClicked.connect(lambda row, _column: self._basis_from_table("kp", row))

        error_title = QLabel("错误类型分布（点一行看依据）")
        error_title.setStyleSheet("font-weight: 600;")
        self.error_table = QTableWidget(0, 2)
        self.error_table.setObjectName("statsErrorTypeTable")
        self.error_table.setHorizontalHeaderLabels(["错误类型", "错题条数"])
        self.error_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.error_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._setup_table(self.error_table)
        self.error_table.itemSelectionChanged.connect(lambda: self._basis_from_table("error"))
        self.error_table.cellClicked.connect(lambda row, _column: self._basis_from_table("error", row))

        left = QVBoxLayout()
        left.addWidget(kp_title)
        left.addWidget(self.kp_table, 1)
        right = QVBoxLayout()
        right.addWidget(error_title)
        right.addWidget(self.error_table, 1)
        distributions = QHBoxLayout()
        distributions.addLayout(left, 1)
        distributions.addLayout(right, 1)

        self.basis_title = QLabel("依据错题（点上面的统计项后显示）")
        self.basis_title.setObjectName("statsBasisTitle")
        self.basis_title.setStyleSheet("font-weight: 600;")
        self.basis_table = QTableWidget(0, 4)
        self.basis_table.setObjectName("statsBasisTable")
        self.basis_table.setHorizontalHeaderLabels(["编号", "发生日期", "题目摘要", "确认状态"])
        self.basis_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.basis_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.basis_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.basis_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._setup_table(self.basis_table)
        self.open_button = QPushButton("打开这条错题")
        self.open_button.setObjectName("statsOpenMistakeButton")
        self.open_button.clicked.connect(self.open_selected)
        self.open_button.setEnabled(False)
        self.basis_table.itemSelectionChanged.connect(
            lambda: self.open_button.setEnabled(self._selected_basis() is not None)
        )
        self.basis_table.itemDoubleClicked.connect(lambda *_: self.open_selected())
        basis_tools = QHBoxLayout()
        basis_tools.addWidget(self.basis_title, 1)
        basis_tools.addWidget(self.open_button)

        self.rule = QLabel(
            "只统计老师已确认的错题，待核对与已退回的不计入。一条错题可以涉及多个知识点，"
            "所以知识点条数相加可能超过错题总数。这里只反映错题分布，不计算正确率或掌握率。"
        )
        self.rule.setObjectName("statsRule")
        self.rule.setWordWrap(True)
        self.rule.setStyleSheet("color: #666;")

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(filters)
        layout.addWidget(self.summary)
        layout.addWidget(self.notes)
        layout.addLayout(distributions, 3)
        layout.addLayout(basis_tools)
        layout.addWidget(self.basis_table, 2)
        layout.addWidget(self.rule)

        self.reload()

    @staticmethod
    def _setup_table(table):
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setTextElideMode(Qt.ElideRight)
        table.verticalHeader().setVisible(False)

    # ---------------------------------------------------------------- 查询
    def filters(self):
        return (
            self.from_date.date().toString("yyyy-MM-dd") if self.from_check.isChecked() else "",
            self.to_date.date().toString("yyyy-MM-dd") if self.to_check.isChecked() else "",
        )

    def reset_filters(self):
        self.from_check.setChecked(False)
        self.to_check.setChecked(False)
        # 重置立刻收掉上一份依据：不等新统计回来，旧依据也不能再点开。
        self._clear_table_choice(self.kp_table)
        self._clear_table_choice(self.error_table)
        self._clear_basis()
        self.reload()

    def reload(self):
        """查询按钮和重置都走这里。

        查询还在跑的时候再点一次，不丢掉这次操作：记下「还要再查一次」，
        等当前这次结束后用最新的日期条件重查，最后显示的一定是对应最新操作的。
        """
        self._want_reload = True
        self._start_query()

    def _start_query(self):
        if self._query_busy or self._closed:
            return
        self._want_reload = False
        self._query_busy = True
        date_from, date_to = self.filters()
        token = self.service.token
        student_id = self.student["id"]
        self._seq += 1
        seq = self._seq
        self.query_button.setEnabled(False)
        self.status.setText("正在统计…")
        self.runner.run(
            lambda: self.service.student_stats(student_id, date_from, date_to),
            lambda data: self._show_stats(data, seq, token),
            lambda message: self._failed(message, seq, token),
        )

    def _query_finished(self, seq):
        """一次查询真正结束（不管成功还是失败）后统一收尾。"""
        if seq != self._seq:
            return
        self._query_busy = False
        self.query_button.setEnabled(True)
        if self._want_reload:
            self._start_query()

    def _failed(self, message, seq, token):
        if self._closed:
            return
        if seq == self._seq and not self._want_reload:
            if self.service.token == token:
                self.status.setText(message)
                if "日期" in message:
                    # 日期不合法时清掉旧结果，避免老师把上一次的数字当成这次的结果。
                    self.summary.setText("")
                    self.notes.setText("")
                    self._kp_items, self._error_items = [], []
                    self.kp_table.setRowCount(0)
                    self.error_table.setRowCount(0)
                    self._clear_basis()
            else:
                self.status.setText("登录状态已变化，请重新打开学情统计")
        self._query_finished(seq)

    def _show_stats(self, data, seq, token):
        if self._closed:
            return
        if seq == self._seq and not self._want_reload and self.service.token != token:
            self.status.setText("登录状态已变化，请重新打开学情统计")
        if seq == self._seq and not self._want_reload and self.service.token == token:
            self._stats = data
            summary = data.get("summary") or {}
            self.summary.setText(
                "已确认错题 %d 条（待核对 %d 条、已退回 %d 条未计入）"
                % (summary.get("confirmed", 0), summary.get("pending", 0), summary.get("returned", 0))
            )
            self.notes.setText("\n".join(data.get("notes") or []))
            self.status.setText("统计完成" if summary.get("confirmed") else "")
            self._kp_items = list(data.get("knowledge_points") or [])
            self._error_items = list(data.get("error_types") or [])
            self._fill_bucket(self.kp_table, self._kp_items)
            self._fill_bucket(self.error_table, self._error_items)
            self._clear_basis()
        self._query_finished(seq)

    @staticmethod
    def _clear_table_choice(table):
        table.blockSignals(True)
        try:
            table.clearSelection()
            table.setCurrentItem(None)
        finally:
            table.blockSignals(False)

    @classmethod
    def _fill_bucket(cls, table, items):
        # 换统计结果时连选中行一起清掉；否则再点同一行不会触发选择变化。
        table.blockSignals(True)
        try:
            table.clearSelection()
            table.setCurrentItem(None)
            table.setRowCount(len(items))
            for row, item in enumerate(items):
                table.setItem(row, 0, QTableWidgetItem(item.get("name") or ""))
                cell = QTableWidgetItem(str(item.get("mistakes", 0)))
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row, 1, cell)
        finally:
            table.blockSignals(False)

    def _basis_from_table(self, kind, row=None):
        table = self.kp_table if kind == "kp" else self.error_table
        items = self._kp_items if kind == "kp" else self._error_items
        if row is None:
            row = table.currentRow()
        if row < 0 or row >= len(items):
            return
        other = self.error_table if kind == "kp" else self.kp_table
        self._clear_table_choice(other)
        key = (kind, row)
        if self._basis_pending == key:
            return
        self._load_basis(items[row], key)

    def _load_basis(self, bucket, key=None, keep_rows=False):
        ids = [int(i) for i in (bucket.get("mistake_ids") or [])]
        # 先把上一项的错题清掉：新依据还没回来时，不能让老师点到上一个统计项的错题。
        # 从错题详情返回时保留同一项的旧列表，等新结果回来再替换，按钮不会瞬间变灰。
        if not keep_rows:
            self._basis_rows = []
            self.basis_table.setRowCount(0)
            self.open_button.setEnabled(False)
        self.basis_title.setText("依据错题：%s（%d 条，正在加载…）" % (bucket.get("name"), bucket.get("mistakes", 0)))
        self._basis_pending = key
        self._basis_seq += 1
        seq = self._basis_seq
        token = self.service.token
        self.basis_runner.run(
            lambda: self.service.mistakes_by_ids(ids),
            lambda rows: self._show_basis(bucket, rows, seq, token),
            lambda message: self._basis_failed(message, seq, token),
        )

    def _basis_failed(self, message, seq, token):
        if self._closed or seq != self._basis_seq:
            return
        self._basis_pending = None
        if self.service.token != token:
            self._clear_basis()
            self.status.setText("登录状态已变化，请重新打开学情统计")
            return
        self._basis_rows = []
        self.basis_table.setRowCount(0)
        self.open_button.setEnabled(False)
        self.basis_title.setText("依据错题（没有加载成功，请再点一次统计项）")
        self.status.setText(message)

    def _show_basis(self, bucket, rows, seq, token):
        if self._closed or seq != self._basis_seq:
            return
        self._basis_pending = None
        if self.service.token != token:
            self._clear_basis()
            self.status.setText("登录状态已变化，请重新打开学情统计")
            return
        by_id = {row["id"]: row for row in rows}
        self._basis_rows = [by_id[i] for i in (bucket.get("mistake_ids") or []) if i in by_id]
        self.basis_table.setRowCount(len(self._basis_rows))
        for row, item in enumerate(self._basis_rows):
            text = (item.get("question_text") or "").replace("\n", " ").strip()
            values = [
                str(item["id"]),
                item.get("occurred_on") or "",
                text or "（没有填写题目内容）",
                STATUS_TEXT.get(item.get("status"), item.get("status") or ""),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 2:
                    cell.setToolTip(text)
                self.basis_table.setItem(row, column, cell)
        self.open_button.setEnabled(False)
        self.basis_title.setText("依据错题：%s（%d 条）" % (bucket.get("name"), len(self._basis_rows)))
        if self._basis_rows:
            self.basis_table.selectRow(0)
        if len(self._basis_rows) != bucket.get("mistakes", 0):
            self.status.setText(
                "注意：这条统计是 %d 条，列表里只找到 %d 条，请点刷新再查一次"
                % (bucket.get("mistakes", 0), len(self._basis_rows))
            )

    def _clear_basis(self):
        # 作废还在路上的依据查询：查完之后发现代数变了，就不会再往表里写。
        self._basis_seq += 1
        self._basis_pending = None
        self._basis_rows = []
        self.basis_table.setRowCount(0)
        self.basis_title.setText("依据错题（点上面的统计项后显示）")
        self.open_button.setEnabled(False)

    def _selected_basis(self):
        row = self.basis_table.currentRow()
        if row < 0 or row >= len(self._basis_rows):
            return None
        return self._basis_rows[row]

    def open_selected(self):
        item = self._selected_basis()
        if item is None:
            return
        self.open_mistake.emit(self.student, item["id"])

    def open_report(self):
        """R2-04：个人学情报告（不含他人记录；预览/导出/打印同一份快照）。"""
        from desktop.ui_report import ReportPreviewWindow

        try:
            self.service.owned_class_row(self.class_row["id"])
        except Exception as exc:
            self.status.setText(getattr(exc, "message", "") or "打开失败")
            return
        window = ReportPreviewWindow(
            self.service.db, self.class_row["id"], kind="student",
            student_id=self.student["id"],
            class_name=self.class_row.get("name", ""),
            today=QDate.currentDate().toString("yyyy-MM-dd"),
            user=self.service.user, db_file=self.service.db.path, parent=self)
        self._report_window = window
        window.show()
        window.raise_()
        return window

    def open_dashboard(self):
        """R2-03：个人学情看板（原版这个页面保留，看板是并列入口）。"""
        from desktop.ui_dashboard import StudentDashboardWindow

        try:
            self.service.owned_class_row(self.class_row["id"])
        except Exception as exc:
            self.status.setText(getattr(exc, "message", "") or "打开失败")
            return
        window = StudentDashboardWindow(
            self.service.db, self.class_row["id"], self.student["id"],
            today=QDate.currentDate().toString("yyyy-MM-dd"), parent=self)
        window.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dashboard_window = window
        window.show()
        window.raise_()
        return window

    def closeEvent(self, event):
        self._closed = True
        super().closeEvent(event)
        self.closed.emit()


class ClassStatsWindow(QMainWindow):
    """一个班级的错题分布：三种状态、知识点与错误类型，可追溯到学生和错题。"""

    closed = Signal()
    open_mistake = Signal(dict, int)

    def __init__(self, service, class_row, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.basis_runner = TaskRunner()
        self.class_row = class_row
        self._closed = False
        self._seq = 0
        self._basis_seq = 0
        self._basis_pending = None
        self._basis_kind = None
        self._basis_name = None
        self._keep_basis = False
        self._query_busy = False
        self._want_reload = False
        self._stats = None
        self._kp_items = []
        self._error_items = []
        self._basis_rows = []
        self._students_by_id = {}
        self._duplicate_student_ids = set()
        self._inactive_student_ids = set()
        self.setObjectName("classStatsWindow")
        self.setWindowTitle("数智伴学 · 班级学情统计")
        fit_to_screen(self, 1120, 760)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · 班级学情统计" % class_row.get("name", ""))
        heading.setObjectName("classStatsClassName")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.back_button = QPushButton("返回班级列表")
        self.back_button.setObjectName("backToClassesFromStats")
        self.back_button.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(heading)
        top.addStretch(1)
        top.addWidget(self.back_button)

        self.from_check = QCheckBox("从")
        self.from_check.setObjectName("classStatsFromCheck")
        self.from_date = QDateEdit(QDate.currentDate())
        self.from_date.setObjectName("classStatsFromDate")
        self.from_date.setDisplayFormat("yyyy-MM-dd")
        self.from_date.setCalendarPopup(True)
        self.from_date.setEnabled(False)
        self.to_check = QCheckBox("到")
        self.to_check.setObjectName("classStatsToCheck")
        self.to_date = QDateEdit(QDate.currentDate())
        self.to_date.setObjectName("classStatsToDate")
        self.to_date.setDisplayFormat("yyyy-MM-dd")
        self.to_date.setCalendarPopup(True)
        self.to_date.setEnabled(False)
        self.from_check.toggled.connect(self.from_date.setEnabled)
        self.to_check.toggled.connect(self.to_date.setEnabled)
        self.query_button = QPushButton("查询")
        self.query_button.setObjectName("classStatsQueryButton")
        self.query_button.clicked.connect(self.reload)
        self.reset_button = QPushButton("重置")
        self.reset_button.setObjectName("classStatsResetButton")
        self.reset_button.clicked.connect(self.reset_filters)
        self.dashboard_button = QPushButton("数据看板")
        self.dashboard_button.setObjectName("classStatsOpenDashboard")
        self.dashboard_button.setToolTip("交互式看板：知识点/错因分布、热力图、完整成绩分布、各题得分率")
        self.dashboard_button.clicked.connect(self.open_dashboard)
        self.report_button = QPushButton("导出报告")
        self.report_button.setObjectName("classStatsExportReport")
        self.report_button.setToolTip("预览班级学情报告并导出 PDF / 打印")
        self.report_button.clicked.connect(self.open_report)
        self.status = QLabel("")
        self.status.setObjectName("classStatsStatus")

        filters = QHBoxLayout()
        filters.addWidget(self.from_check)
        filters.addWidget(self.from_date)
        filters.addWidget(self.to_check)
        filters.addWidget(self.to_date)
        filters.addWidget(self.query_button)
        filters.addWidget(self.reset_button)
        filters.addWidget(self.dashboard_button)
        filters.addWidget(self.report_button)
        filters.addWidget(self.status, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("classStatsSummary")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.scope_note = QLabel("三个数字都按所选日期统计；下面的分布只统计已确认错题。")
        self.scope_note.setObjectName("classStatsScopeNote")
        self.scope_note.setWordWrap(True)
        self.scope_note.setStyleSheet("color: #555;")

        self.empty = QLabel("")
        self.empty.setObjectName("classStatsEmpty")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: #8a5a00; background: #fff8e6; padding: 8px; border-radius: 4px;")
        self.empty.hide()

        self.notes = QLabel("")
        self.notes.setObjectName("classStatsNotes")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color: #555;")

        kp_title = QLabel("知识点分布（点一行看依据）")
        kp_title.setStyleSheet("font-weight: 600;")
        self.kp_table = QTableWidget(0, 2)
        self.kp_table.setObjectName("classStatsKnowledgeTable")
        self.kp_table.setHorizontalHeaderLabels(["知识点", "错题条数"])
        self.kp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.kp_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._setup_table(self.kp_table)
        self.kp_table.itemSelectionChanged.connect(lambda: self._basis_from_table("kp"))
        self.kp_table.cellClicked.connect(lambda row, _column: self._basis_from_table("kp", row))

        error_title = QLabel("错误类型分布（点一行看依据）")
        error_title.setStyleSheet("font-weight: 600;")
        self.error_table = QTableWidget(0, 2)
        self.error_table.setObjectName("classStatsErrorTypeTable")
        self.error_table.setHorizontalHeaderLabels(["错误类型", "错题条数"])
        self.error_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.error_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._setup_table(self.error_table)
        self.error_table.itemSelectionChanged.connect(lambda: self._basis_from_table("error"))
        self.error_table.cellClicked.connect(lambda row, _column: self._basis_from_table("error", row))

        left = QVBoxLayout()
        left.addWidget(kp_title)
        left.addWidget(self.kp_table, 1)
        right = QVBoxLayout()
        right.addWidget(error_title)
        right.addWidget(self.error_table, 1)
        distributions = QHBoxLayout()
        distributions.addLayout(left, 1)
        distributions.addLayout(right, 1)

        self.basis_title = QLabel("依据错题（点上面的统计项后显示）")
        self.basis_title.setObjectName("classStatsBasisTitle")
        self.basis_title.setStyleSheet("font-weight: 600;")
        self.basis_table = QTableWidget(0, 5)
        self.basis_table.setObjectName("classStatsBasisTable")
        self.basis_table.setHorizontalHeaderLabels(["错题编号", "学生", "发生日期", "题目摘要", "确认状态"])
        self.basis_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.basis_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.basis_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.basis_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.basis_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._setup_table(self.basis_table)
        self.open_button = QPushButton("打开这条错题")
        self.open_button.setObjectName("classStatsOpenMistakeButton")
        self.open_button.clicked.connect(self.open_selected)
        self.open_button.setEnabled(False)
        self.basis_table.itemSelectionChanged.connect(
            lambda: self.open_button.setEnabled(self._selected_basis() is not None)
        )
        self.basis_table.itemDoubleClicked.connect(lambda *_: self.open_selected())
        basis_tools = QHBoxLayout()
        basis_tools.addWidget(self.basis_title, 1)
        basis_tools.addWidget(self.open_button)

        self.rule = QLabel(
            "只统计当前登录老师有权访问的当前班级；日期按错题发生日期处理，起止当天都算在内。"
            "知识点和错误类型只统计已确认错题；一条错题涉及多个知识点时，会分别计入，"
            "所以知识点条数相加可能超过错题总数。停用学生的历史错题仍计入，与个人学情统计一致。"
            "这里只反映错题分布，不能据此计算正确率、掌握率，也不能直接断言学生不会某个知识点。"
        )
        self.rule.setObjectName("classStatsRule")
        self.rule.setWordWrap(True)
        self.rule.setStyleSheet("color: #666;")

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(filters)
        layout.addWidget(self.summary)
        layout.addWidget(self.scope_note)
        layout.addWidget(self.empty)
        layout.addWidget(self.notes)
        layout.addLayout(distributions, 3)
        layout.addLayout(basis_tools)
        layout.addWidget(self.basis_table, 2)
        layout.addWidget(self.rule)

        self.reload()

    @staticmethod
    def _setup_table(table):
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setTextElideMode(Qt.ElideRight)
        table.verticalHeader().setVisible(False)

    # ---------------------------------------------------------------- 查询
    def filters(self):
        return (
            self.from_date.date().toString("yyyy-MM-dd") if self.from_check.isChecked() else "",
            self.to_date.date().toString("yyyy-MM-dd") if self.to_check.isChecked() else "",
        )

    def reset_filters(self):
        self.from_check.setChecked(False)
        self.to_check.setChecked(False)
        # 重置立刻收掉上一份依据：不等新统计回来，旧依据也不能再点开。
        self._clear_table_choice(self.kp_table)
        self._clear_table_choice(self.error_table)
        self._clear_basis()
        self.reload()

    def reload(self, keep_basis=False):
        self._keep_basis = bool(keep_basis and self._basis_rows)
        self._want_reload = True
        self._start_query()

    def _start_query(self):
        if self._query_busy or self._closed:
            return
        self._want_reload = False
        self._query_busy = True
        date_from, date_to = self.filters()
        token = self.service.token
        class_id = self.class_row["id"]
        self._seq += 1
        seq = self._seq
        self.query_button.setEnabled(False)
        self.status.setText("正在统计…")
        self.runner.run(
            lambda: self.service.class_stats(class_id, date_from, date_to),
            lambda data: self._show_stats(data, seq, token),
            lambda message: self._failed(message, seq, token),
        )

    def _query_finished(self, seq):
        if seq != self._seq:
            return
        self._query_busy = False
        self.query_button.setEnabled(True)
        if self._want_reload:
            self._start_query()

    def _failed(self, message, seq, token):
        if self._closed:
            return
        if seq == self._seq and not self._want_reload:
            if self.service.token != token:
                self._clear_after_account_change()
            else:
                self.status.setText(message)
                if "日期" in message:
                    # 日期不合法时清掉旧结果，避免老师把上一次的数字当成这次的结果。
                    self.summary.setText("")
                    self.notes.setText("")
                    self.empty.hide()
                    self._kp_items, self._error_items = [], []
                    self.kp_table.setRowCount(0)
                    self.error_table.setRowCount(0)
                    self._clear_basis()
        self._query_finished(seq)

    def _show_stats(self, data, seq, token):
        if self._closed:
            return
        if seq == self._seq and not self._want_reload:
            if self.service.token != token:
                self._clear_after_account_change()
            else:
                self._stats = data
                summary = data.get("summary") or {}
                confirmed = int(summary.get("confirmed", 0))
                pending = int(summary.get("pending", 0))
                returned = int(summary.get("returned", 0))
                self.summary.setText(
                    "已确认错题 %d 条　　｜　　待核对 %d 条　　｜　　已退回 %d 条"
                    % (confirmed, pending, returned)
                )
                self.notes.setText("\n".join(data.get("notes") or []))
                if confirmed:
                    self.status.setText("统计完成")
                elif pending or returned:
                    self.status.setText("没有已确认错题")
                else:
                    self.status.setText("所选日期范围内没有错题")
                self._kp_items = list(data.get("knowledge_points") or [])
                self._error_items = list(data.get("error_types") or [])
                students = list(data.get("students") or [])
                self._students_by_id = {int(s["id"]): s for s in students}
                name_counts = {}
                for student in students:
                    name = student.get("name") or ""
                    name_counts[name] = name_counts.get(name, 0) + 1
                self._duplicate_student_ids = {
                    int(s["id"]) for s in students if name_counts.get(s.get("name") or "", 0) > 1
                }
                self._inactive_student_ids = {
                    int(s["id"]) for s in students if not s.get("active", 1)
                }
                self._fill_bucket(self.kp_table, self._kp_items)
                self._fill_bucket(self.error_table, self._error_items)
                self._show_empty(confirmed, pending, returned)
                keep_kind, keep_name, keep = self._basis_kind, self._basis_name, self._keep_basis
                self._keep_basis = False
                found = self._find_bucket(keep_kind, keep_name) if keep else None
                if found is not None:
                    # 从错题详情返回：数字刷新后，把刚才那一项的错题依据也重新取一遍。
                    self._load_basis(found[1], (keep_kind, found[0]), keep_rows=True)
                else:
                    self._clear_basis()
        self._query_finished(seq)

    def _show_empty(self, confirmed, pending, returned):
        if confirmed:
            self.empty.hide()
            return
        if pending or returned:
            self.empty.setText(
                "没有已确认的错题，暂无可统计的知识点或错误类型。"
                "待核对 %d 条、已退回 %d 条；可调整日期或点「重置」查看全部。" % (pending, returned)
            )
        else:
            self.empty.setText("所选日期范围内没有错题。可调整日期，或点「重置」查看全部。")
        self.empty.show()

    def _clear_after_account_change(self):
        self.summary.setText("")
        self.notes.setText("")
        self.empty.hide()
        self._stats = None
        self._kp_items, self._error_items = [], []
        self._fill_bucket(self.kp_table, [])
        self._fill_bucket(self.error_table, [])
        self._clear_basis()
        self.status.setText("登录状态已变化，请重新打开班级统计")

    @staticmethod
    def _clear_table_choice(table):
        table.blockSignals(True)
        try:
            table.clearSelection()
            table.setCurrentItem(None)
        finally:
            table.blockSignals(False)

    @classmethod
    def _fill_bucket(cls, table, items):
        # 换统计结果时连选中行一起清掉；否则再点同一行不会触发选择变化。
        table.blockSignals(True)
        try:
            table.clearSelection()
            table.setCurrentItem(None)
            table.setRowCount(len(items))
            for row, item in enumerate(items):
                table.setItem(row, 0, QTableWidgetItem(item.get("name") or ""))
                cell = QTableWidgetItem(str(item.get("mistakes", 0)))
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row, 1, cell)
        finally:
            table.blockSignals(False)

    def _basis_from_table(self, kind, row=None):
        table = self.kp_table if kind == "kp" else self.error_table
        items = self._kp_items if kind == "kp" else self._error_items
        if row is None:
            row = table.currentRow()
        if row < 0 or row >= len(items):
            return
        other = self.error_table if kind == "kp" else self.kp_table
        self._clear_table_choice(other)
        key = (kind, row)
        if self._basis_pending == key:
            return
        self._load_basis(items[row], key)

    def _load_basis(self, bucket, key=None, keep_rows=False):
        ids = [int(i) for i in (bucket.get("mistake_ids") or [])]
        # 先把上一项的错题清掉：新依据还没回来时，不能让老师点到上一个统计项的错题。
        # 从错题详情返回时保留同一项的旧列表，等新结果回来再替换，按钮不会瞬间变灰。
        if not keep_rows:
            self._basis_rows = []
            self.basis_table.setRowCount(0)
            self.open_button.setEnabled(False)
        self.basis_title.setText("依据错题：%s（%d 条，正在加载…）" % (bucket.get("name"), bucket.get("mistakes", 0)))
        self._basis_pending = key
        self._basis_seq += 1
        seq = self._basis_seq
        token = self.service.token
        self.basis_runner.run(
            lambda: self.service.mistakes_by_ids(ids),
            lambda rows: self._show_basis(bucket, rows, seq, token),
            lambda message: self._basis_failed(message, seq, token),
        )

    def _basis_failed(self, message, seq, token):
        if self._closed or seq != self._basis_seq:
            return
        self._basis_pending = None
        if self.service.token != token:
            self._clear_after_account_change()
            return
        self._basis_rows = []
        self.basis_table.setRowCount(0)
        self.open_button.setEnabled(False)
        self.basis_title.setText("依据错题（没有加载成功，请再点一次统计项）")
        self.status.setText(message)

    def _show_basis(self, bucket, rows, seq, token):
        if self._closed or seq != self._basis_seq:
            return
        if self.service.token != token:
            self._basis_pending = None
            self._clear_after_account_change()
            return
        key = self._basis_pending
        if key:
            self._basis_kind = key[0]
        self._basis_name = bucket.get("name")
        self._basis_pending = None
        by_id = {row["id"]: row for row in rows}
        self._basis_rows = [by_id[i] for i in (bucket.get("mistake_ids") or []) if i in by_id]
        self._clear_table_choice(self.basis_table)
        self.basis_table.setRowCount(len(self._basis_rows))
        for row, item in enumerate(self._basis_rows):
            text = (item.get("question_text") or "").replace("\n", " ").strip()
            values = [
                str(item["id"]),
                self._student_label(item),
                item.get("occurred_on") or "",
                text or "（没有填写题目内容）",
                STATUS_TEXT.get(item.get("status"), item.get("status") or ""),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 3:
                    cell.setToolTip(text)
                if column == 1:
                    cell.setToolTip(value)
                self.basis_table.setItem(row, column, cell)
        self.open_button.setEnabled(False)
        self.basis_title.setText("依据错题：%s（%d 条）" % (bucket.get("name"), len(self._basis_rows)))
        if self._basis_rows:
            self.basis_table.selectRow(0)
        if len(self._basis_rows) != bucket.get("mistakes", 0):
            self.status.setText(
                "注意：这条统计是 %d 条，列表里只找到 %d 条，请点刷新再查一次"
                % (bucket.get("mistakes", 0), len(self._basis_rows))
            )

    def _find_bucket(self, kind, name):
        if not kind or not name:
            return None
        items = self._kp_items if kind == "kp" else self._error_items
        for row, item in enumerate(items):
            if item.get("name") == name:
                return row, item
        return None

    def _student_label(self, item):
        student_id = item.get("student_id")
        name = (item.get("student_name") or "").strip() or "（未知学生）"
        tags = []
        if student_id in self._duplicate_student_ids:
            tags.append("内部编号 %s" % student_id)
        if student_id in self._inactive_student_ids:
            tags.append("已停用")
        return name + ("（%s）" % "，".join(tags) if tags else "")

    def _clear_basis(self):
        # 作废还在路上的依据查询：查完之后发现代数变了，就不会再往表里写。
        self._basis_seq += 1
        self._basis_pending = None
        self._basis_rows = []
        self.basis_table.setRowCount(0)
        self.basis_title.setText("依据错题（点上面的统计项后显示）")
        self.open_button.setEnabled(False)

    def _selected_basis(self):
        row = self.basis_table.currentRow()
        if row < 0 or row >= len(self._basis_rows):
            return None
        return self._basis_rows[row]

    def open_selected(self):
        item = self._selected_basis()
        if item is None:
            return
        student_id = item.get("student_id")
        student = self._students_by_id.get(student_id)
        if student is None:
            student = {
                "id": student_id,
                "class_id": self.class_row["id"],
                "name": item.get("student_name") or "",
                "note": item.get("student_note") or "",
                "active": 0 if student_id in self._inactive_student_ids else 1,
            }
        self.open_mistake.emit(student, item["id"])

    def open_report(self):
        """R2-04：班级学情报告（预览 / 导出 PDF / 打印，三者共用同一份快照）。"""
        from desktop.ui_report import ReportPreviewWindow

        try:
            self.service.owned_class_row(self.class_row["id"])
        except Exception as exc:
            self.status.setText(getattr(exc, "message", "") or "打开失败")
            return
        window = ReportPreviewWindow(
            self.service.db, self.class_row["id"], kind="class",
            class_name=self.class_row.get("name", ""),
            today=QDate.currentDate().toString("yyyy-MM-dd"),
            user=self.service.user, db_file=self.service.db.path, parent=self)
        self._report_window = window
        window.show()
        window.raise_()
        return window

    def open_dashboard(self):
        """R2-03：班级学情看板（原版按日期的错题分布保留，看板是并列入口）。"""
        from desktop.ui_dashboard import ClassDashboardWindow

        try:
            self.service.owned_class_row(self.class_row["id"])
        except Exception as exc:
            self.status.setText(getattr(exc, "message", "") or "打开失败")
            return
        window = ClassDashboardWindow(
            self.service.db, self.class_row["id"],
            class_name=self.class_row.get("name", ""),
            today=QDate.currentDate().toString("yyyy-MM-dd"), parent=self)
        window.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dashboard_window = window
        window.show()
        window.raise_()
        return window

    def closeEvent(self, event):
        self._closed = True
        super().closeEvent(event)
        self.closed.emit()


class ImageViewerDialog(QDialog):
    """原图查看：可以放大、缩小、适应窗口；图片比窗口大时可以滚动查看。"""

    def __init__(self, blob, parent=None):
        super().__init__(parent)
        self.base = QPixmap()
        self.base.loadFromData(blob)
        self.scale = 1.0
        self._fitted = False
        self.setObjectName("photoViewer")
        self.setWindowTitle("错题原图")
        fit_to_screen(self, 900, 660)

        self.canvas = QLabel("")
        self.canvas.setObjectName("photoCanvas")
        self.canvas.setAlignment(Qt.AlignCenter)
        self.area = QScrollArea()
        self.area.setObjectName("photoArea")
        self.area.setWidgetResizable(True)
        self.area.setWidget(self.canvas)

        self.zoom_label = QLabel("")
        self.zoom_label.setObjectName("photoZoom")
        self.size_label = QLabel(
            "原图 %d × %d 像素" % (self.base.width(), self.base.height()) if not self.base.isNull() else "原图读不出来"
        )
        self.size_label.setObjectName("photoSize")

        zoom_in = QPushButton("放大")
        zoom_in.setObjectName("photoZoomIn")
        zoom_in.clicked.connect(lambda: self.scale_by(1.25))
        zoom_out = QPushButton("缩小")
        zoom_out.setObjectName("photoZoomOut")
        zoom_out.clicked.connect(lambda: self.scale_by(0.8))
        fit = QPushButton("适应窗口")
        fit.setObjectName("photoFit")
        fit.clicked.connect(self.fit_window)
        actual = QPushButton("原始大小")
        actual.setObjectName("photoActual")
        actual.clicked.connect(self.actual_size)
        close = QPushButton("关闭")
        close.setObjectName("photoClose")
        close.clicked.connect(self.accept)

        tools = QHBoxLayout()
        for widget in (zoom_out, zoom_in, fit, actual, self.zoom_label, self.size_label, close):
            tools.addWidget(widget)
        tools.insertStretch(4)

        layout = QVBoxLayout(self)
        layout.addWidget(self.area, 1)
        layout.addLayout(tools)
        self.render()

    def scale_by(self, factor):
        self.scale = min(max(self.scale * factor, 0.1), 8.0)
        self.render()

    def fit_window(self):
        if self.base.isNull():
            return
        width = self.area.viewport().width() - 8
        height = self.area.viewport().height() - 8
        if width <= 0 or height <= 0:
            return
        self.scale = min(width / float(self.base.width()), height / float(self.base.height()))
        self.render()

    def actual_size(self):
        self.scale = 1.0
        self.render()

    def render(self):
        if self.base.isNull():
            self.canvas.setText("这条错题的照片读不出来。")
            self.zoom_label.setText("")
            return
        width = max(1, int(self.base.width() * self.scale))
        height = max(1, int(self.base.height() * self.scale))
        self.canvas.setPixmap(self.base.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.zoom_label.setText("%d%%" % int(round(self.scale * 100)))

    def showEvent(self, event):
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            self.fit_window()
        auto_shot(self)


class MistakeDialog(QDialog):
    """录入或修改一条错题。

    选中的照片在点「保存」时才复制进数据目录；点取消或直接关掉不会留下任何文件。
    """

    def __init__(self, service, class_row, student, mistake=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.class_row = class_row
        self.student = student
        self.mistake = mistake
        self.saved = None
        self.photo_path = ""
        self.photo_size = (0, 0)
        self._saving = False
        self.setObjectName("mistakeDialog")
        self.setWindowTitle("修改错题" if mistake else "录入错题")
        self.setMinimumWidth(600)

        target = QLabel("%s · %s" % (class_row.get("name", ""), student.get("name", "")))
        target.setObjectName("mistakeTarget")
        target.setStyleSheet("font-size: 16px; font-weight: 600;")

        self.question = QPlainTextEdit()
        self.question.setObjectName("questionText")
        self.question.setPlaceholderText("例如：解方程 2x + 3 = 11")
        self.question.setFixedHeight(80)

        self.solution = QPlainTextEdit()
        self.solution.setObjectName("solutionText")
        self.solution.setPlaceholderText("学生写的过程（可选）")
        self.solution.setFixedHeight(70)

        self.observed = QPlainTextEdit()
        self.observed.setObjectName("observedErrors")
        self.observed.setPlaceholderText("每行一条，例如：移项时没有变号")
        self.observed.setFixedHeight(70)

        self.source_type = QComboBox()
        self.source_type.setObjectName("sourceType")
        for item in SOURCE_TYPES:
            self.source_type.addItem(item)

        self.source_detail = QLineEdit()
        self.source_detail.setObjectName("sourceDetail")
        self.source_detail.setPlaceholderText("可选，例如：第 3 页第 2 题")

        self.occurred = QDateEdit()
        self.occurred.setObjectName("occurredOn")
        self.occurred.setCalendarPopup(True)
        self.occurred.setDisplayFormat("yyyy-MM-dd")
        self.occurred.setDate(QDate.currentDate())

        self.teacher_note = QPlainTextEdit()
        self.teacher_note.setObjectName("teacherNote")
        self.teacher_note.setPlaceholderText("可选：给家长或其他老师看的说明")
        self.teacher_note.setFixedHeight(60)

        self.knowledge = QLineEdit()
        self.knowledge.setObjectName("knowledgePoints")
        self.knowledge.setPlaceholderText("可选，用「、」分隔，例如：一元一次方程、移项")

        self.error_type = QComboBox()
        self.error_type.setObjectName("errorType")
        self.error_type.addItem("（未填）")
        for item in ERROR_TYPES:
            self.error_type.addItem(item)

        self.causes = QPlainTextEdit()
        self.causes.setObjectName("possibleCauses")
        self.causes.setPlaceholderText("每行一条，用推测语气，例如：可能没有理解移项要变号")
        self.causes.setFixedHeight(60)

        self.advice = QPlainTextEdit()
        self.advice.setObjectName("adviceText")
        self.advice.setPlaceholderText("可选：下一步怎么练、怎么讲")
        self.advice.setFixedHeight(60)

        self.photo_button = QPushButton("选择照片")
        self.photo_button.setObjectName("choosePhoto")
        self.photo_button.clicked.connect(self.choose_photo)
        self.photo_info = QLabel("还没有选择照片")
        self.photo_info.setObjectName("photoInfo")
        self.photo_info.setWordWrap(True)
        self.photo_preview = QLabel("")
        self.photo_preview.setObjectName("photoPreview")
        self.photo_preview.setFixedHeight(110)
        self.photo_preview.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        photo_row = QHBoxLayout()
        photo_row.addWidget(self.photo_button)
        photo_row.addWidget(self.photo_info, 1)

        self.followup_button = None
        if mistake is not None:
            self.followup_button = QPushButton("追问这个问题")
            self.followup_button.setObjectName("mistakeFollowup")
            self.followup_button.setToolTip("针对这条已确认错题生成或手写一句短追问，记录学生回答")
            self.followup_button.clicked.connect(self.open_followup)
            self.followup_button.setVisible((mistake.get("status") or "") == "confirmed")

        form = QFormLayout()
        form.addRow("题目内容", self.question)
        form.addRow("解题过程", self.solution)
        form.addRow("学生错误表现", self.observed)
        form.addRow("知识点", self.knowledge)
        form.addRow("错误类型", self.error_type)
        form.addRow("可能原因", self.causes)
        form.addRow("教学建议", self.advice)
        form.addRow("来源", self.source_type)
        form.addRow("来源说明", self.source_detail)
        form.addRow("错题日期", self.occurred)
        form.addRow("老师备注", self.teacher_note)
        form.addRow("错题照片", photo_row)

        self.status = QLabel("")
        self.status.setObjectName("mistakeDialogStatus")
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("保存")
        self.buttons.button(QDialogButtonBox.Ok).setObjectName("mistakeSave")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.Cancel).setObjectName("mistakeCancel")
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(target)
        layout.addLayout(form)
        if self.followup_button is not None:
            layout.addWidget(self.followup_button)
        layout.addWidget(self.photo_preview)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        if mistake:
            self._load_mistake(mistake)
        else:
            self.photo_info.setText("还没有选择照片（录入错题需要一张照片）")
        self._baseline = self._snapshot()

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)

    # ---------------------------------------------------------------- 填充
    def _load_mistake(self, mistake):
        self.question.setPlainText(mistake.get("question_text") or "")
        self.solution.setPlainText(mistake.get("solution_text") or "")
        self.observed.setPlainText("\n".join(mistake.get("observed_errors") or []))
        index = self.source_type.findText(mistake.get("source_type") or "作业")
        self.source_type.setCurrentIndex(index if index >= 0 else 0)
        self.source_detail.setText(mistake.get("source_detail") or "")
        occurred = mistake.get("occurred_on") or ""
        if occurred:
            self.occurred.setDate(QDate.fromString(occurred, "yyyy-MM-dd"))
        self.teacher_note.setPlainText(mistake.get("teacher_note") or "")
        self.knowledge.setText("、".join(mistake.get("knowledge_points") or []))
        index = self.error_type.findText(mistake.get("error_type") or "")
        self.error_type.setCurrentIndex(index if index > 0 else 0)
        self.causes.setPlainText("\n".join(mistake.get("possible_causes") or []))
        self.advice.setPlainText(mistake.get("advice_text") or "")
        if self.followup_button is not None:
            confirmed = (mistake.get("status") or "") == "confirmed"
            self.followup_button.setVisible(confirmed)
            self.followup_button.setEnabled(confirmed)
        if mistake.get("has_image"):
            # 本轮不替换已有照片，只允许给没有照片的记录补一张。
            self.photo_button.setEnabled(False)
            self.photo_info.setText("这条错题已有照片，本轮不修改照片。")
            self._load_existing_photo()
        else:
            self.photo_info.setText("这条错题还没有照片，可以补一张。")

    def open_followup(self):
        """从已确认错题进入追问：问题名取这条错题的第一个知识点。"""
        if self.mistake is None:
            return
        points = self.mistake.get("knowledge_points") or []
        problem = points[0] if points else "未分类"
        self._followup_window = FollowupWindow(
            self.service, self.class_row, self.student, problem, [self.mistake["id"]], parent=self
        )
        self._followup_window.show()
        self._followup_window.raise_()

    def _load_existing_photo(self):
        mistake_id = self.mistake["id"]
        self.runner.run(
            lambda: self.service.mistake_photo(mistake_id),
            lambda result: self._show_photo(*result),
            lambda message: self.photo_info.setText("已有照片读不出来：%s" % message),
        )

    def _show_photo(self, blob, content_type=""):
        pixmap = QPixmap()
        pixmap.loadFromData(blob)
        if pixmap.isNull():
            self.photo_info.setText("已有照片读不出来。")
            return
        self.photo_preview.setPixmap(pixmap.scaled(220, 110, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ---------------------------------------------------------------- 照片
    def choose_photo(self):
        # 自动化测试用：设置 MISTAKE_PHOTO_PATH 时直接用它，跳过系统选图窗口。
        path = os.environ.get("MISTAKE_PHOTO_PATH") or ""
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "选择错题照片", "", PHOTO_FILTER)
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            self.photo_info.setText("这张图片打不开，请换一张 JPG / PNG / WebP 图片。")
            return
        if os.path.getsize(path) < 1024:
            self.photo_info.setText("这张图片太小，请换一张清楚的错题照片。")
            return
        self.photo_path = path
        self.photo_size = (image.width(), image.height())
        self.photo_info.setText("%s（%d × %d 像素）" % (os.path.basename(path), image.width(), image.height()))
        self.photo_preview.setPixmap(
            QPixmap.fromImage(image).scaled(220, 110, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    # ---------------------------------------------------------------- 保存
    def _collect(self):
        return {
            "question_text": self.question.toPlainText().strip(),
            "solution_text": self.solution.toPlainText().strip(),
            "observed_errors": [
                line.strip() for line in self.observed.toPlainText().splitlines() if line.strip()
            ],
            "knowledge_points": [
                part.strip() for part in re.split("[、,，;；]", self.knowledge.text()) if part.strip()
            ],
            "error_type": "" if self.error_type.currentIndex() <= 0 else self.error_type.currentText(),
            "possible_causes": [
                line.strip() for line in self.causes.toPlainText().splitlines() if line.strip()
            ],
            "advice_text": self.advice.toPlainText().strip(),
            "source_type": self.source_type.currentText(),
            "source_detail": self.source_detail.text().strip(),
            "occurred_on": self.occurred.date().toString("yyyy-MM-dd"),
            "teacher_note": self.teacher_note.toPlainText().strip(),
        }

    def _snapshot(self):
        data = self._collect()
        data["photo"] = self.photo_path
        return data

    def _dirty(self):
        return self._snapshot() != self._baseline

    def submit(self):
        if self._saving:
            return
        body = self._collect()
        if not body["question_text"]:
            self.status.setText("请先填写题目内容")
            return
        if not self.mistake and not self.photo_path:
            self.status.setText("请先选择一张错题照片")
            return
        if self.photo_path:
            image = QImage(self.photo_path)
            if image.isNull():
                self.status.setText("这张图片打不开，请换一张 JPG / PNG / WebP 图片")
                return
            if os.path.getsize(self.photo_path) < 1024:
                self.status.setText("这张图片太小，请换一张清楚的错题照片")
                return
            self.photo_size = (image.width(), image.height())
        self._saving = True
        self.buttons.setEnabled(False)
        self.status.setText("正在保存…")
        width, height = self.photo_size
        if self.mistake:
            action = lambda: self.service.update_mistake(
                self.mistake["id"],
                body,
                self.photo_path or None,
                width,
                height,
                base_fingerprint=self.mistake.get("base_fingerprint") or "",
            )
        else:
            action = lambda: self.service.create_mistake(
                self.class_row["id"], self.student["id"], body, self.photo_path, width, height
            )
        self.runner.run(action, self._done, self._fail)

    def _done(self, row):
        self._saving = False
        self.saved = row
        self.accept()

    def _fail(self, message):
        self._saving = False
        self.buttons.setEnabled(True)
        self.status.setText(message)

    # ---------------------------------------------------------------- 关闭
    def _confirm_discard(self):
        box = QMessageBox(self)
        box.setObjectName("discardPrompt")
        box.setWindowTitle("放弃这条错题的修改？")
        box.setText("当前填写的内容还没有保存，关闭后不会保留。")
        discard = box.addButton("放弃修改", QMessageBox.DestructiveRole)
        keep = box.addButton("继续编辑", QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is discard

    def reject(self):
        if self._saving:
            return
        if self._dirty() and not self._confirm_discard():
            return
        super().reject()

    def closeEvent(self, event):
        if self._saving:
            event.ignore()
            return
        if self._dirty() and not self._confirm_discard():
            event.ignore()
            return
        super().closeEvent(event)


def usage_field_text(value):
    """用量字段缺失时显示「尚未确认」；0 和正常数值照常显示。"""
    return "尚未确认" if value is None or value == "" else str(value)


def usage_has_values(usage):
    """判断服务是否真的返回了至少一个可核对的用量字段。"""
    if not isinstance(usage, dict):
        return False
    return any(
        usage.get(key) is not None
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "billing_source", "allowance")
    )


def analysis_failure_text(message, code="", mode=""):
    """把接口错误翻成老师能照着做的下一步，不显示原始异常。"""
    payment = {
        "go": "Go 订阅额度不足或本周期已用完。请到 OpenCode Go 页面查看剩余额度，"
              "本软件不会自动改用按量付费；恢复后再手动重试。",
        "real": "OpenCode Console 账户余额不足，请充值或换一个有额度的密钥后手动重试。",
        "custom": "该服务商返回额度不足或需要付费。请查看服务商的账户余额或套餐额度；"
                  "本软件不会改用模拟结果，也不会自动换服务。",
    }.get(mode or "", "所配置服务的额度不足，请查看对应服务的用量后手动重试。")
    auth = {
        "go": "请打开「AI 设置」确认密钥有效、账号已开通 Go 订阅，并确认该用途被允许；"
              "被服务拒绝时不会自动改用模拟。",
        "real": "请打开「AI 设置」确认本次填写的密钥有效，并确认账号已开通这个模型。",
        "custom": "请打开「AI 设置」确认密钥有效、模型名称正确，并确认该服务商允许图片输入；"
                  "被拒绝时不会自动改用模拟。",
    }.get(mode or "", "请打开「AI 设置」确认密钥有效，并确认账号已开通这个模型。")
    hints = {
        "auth": auth,
        "config": "请先在「AI 设置」填写密钥、接口地址和模型；配置不完整或地址冲突时分析会停止，不会改用模拟。",
        "quota": "请求被限流或额度不足，请稍后重试，或检查所配置服务的额度。",
        "payment": payment,
        "timeout": "网络响应超时，请检查网络后手动重试。",
        "network": "连接不上服务，请检查网络、服务地址后手动重试。",
        "server": "服务暂时异常，请稍后手动重试。",
        "format": "返回内容格式不对，本次没有采用。可以重试，或手动填写分析内容。",
        "request": "请检查服务地址是否为服务商提供的 OpenAI 兼容接口、模型名称和图片格式是否正确。",
        "billing": "服务返回的计费来源或费用不符合预期，软件已停止继续发送。"
                   "请先核对对应服务的账户或额度设置，改好后再手动重试。",
    }
    hint = hints.get(code or "")
    return "%s%s" % (message or "分析没有完成", ("\n\n下一步：" + hint) if hint else "")


class AISettingsDialog(QDialog):
    """AI 设置：模拟、OpenCode Go 或 OpenCode Console；密钥只用于本次登录。"""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.saved = False
        self._data = {}
        self.setObjectName("aiSettingsDialog")
        self.setWindowTitle("AI 设置")
        self.setMinimumWidth(700)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("aiModeCombo")
        self.mode_combo.addItem("模拟分析（默认，不联网）", "mock")
        self.mode_combo.addItem("OpenCode Go（订阅）", "go")
        self.mode_combo.addItem("OpenCode Console（按量付费）", "real")
        self.mode_combo.addItem("自定义 AI 服务（OpenAI 兼容）", "custom")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("aiGoModelCombo")
        self.model_combo.currentIndexChanged.connect(self._go_preview)
        self.go_base_label = QLabel("")
        self.go_base_label.setObjectName("aiGoBase")
        self.go_base_label.setWordWrap(True)
        self.go_policy = QLabel("")
        self.go_policy.setObjectName("aiGoPolicy")
        self.go_policy.setWordWrap(True)
        self.go_policy.setStyleSheet("color: #8a5a00;")
        go_form = QFormLayout()
        go_form.addRow("图片模型", self.model_combo)
        go_form.addRow("服务地址", self.go_base_label)
        go_form.addRow("", self.go_policy)
        self.go_page = QWidget()
        self.go_page.setLayout(go_form)

        self.base_edit = QLineEdit()
        self.base_edit.setObjectName("aiBaseUrlEdit")
        self.model_edit = QLineEdit()
        self.model_edit.setObjectName("aiModelEdit")
        console_form = QFormLayout()
        console_form.addRow("服务地址", self.base_edit)
        console_form.addRow("模型名称", self.model_edit)
        self.console_page = QWidget()
        self.console_page.setLayout(console_form)

        mock_note = QLabel("模拟分析只根据老师填写的文字给出示例，不联网、不读取照片内容，也不会产生费用。")
        mock_note.setWordWrap(True)
        mock_page = QWidget()
        mock_layout = QVBoxLayout(mock_page)
        mock_layout.addWidget(mock_note)

        self.custom_name_edit = QLineEdit()
        self.custom_name_edit.setObjectName("aiCustomNameEdit")
        self.custom_name_edit.setPlaceholderText("例如：公司网关、某云；只用于在本机识别")
        self.custom_base_edit = QLineEdit()
        self.custom_base_edit.setObjectName("aiCustomBaseEdit")
        self.custom_base_edit.setPlaceholderText("服务商给的基础地址，例如 https://api.example.com/v1")
        self.custom_base_edit.textChanged.connect(self._custom_preview)
        self.custom_model_edit = QLineEdit()
        self.custom_model_edit.setObjectName("aiCustomModelEdit")
        self.custom_model_edit.setPlaceholderText("必须选支持图片输入的模型，例如某视觉模型名称")
        self.custom_url_label = QLabel("")
        self.custom_url_label.setObjectName("aiCustomUrl")
        self.custom_url_label.setWordWrap(True)
        self.custom_hint = QLabel(
            "填服务商提供的 OpenAI 兼容基础地址即可，末尾斜杠或已经带有 /chat/completions 都不会重复拼接；"
            "本软件固定使用 Chat Completions 图片请求。不同服务商的兼容程度不同，必须使用支持图片输入的模型；"
            "保存成功只表示配置已记录，不代表已经连接成功。"
        )
        self.custom_hint.setWordWrap(True)
        self.custom_hint.setStyleSheet("color: #8a5a00;")
        custom_form = QFormLayout()
        custom_form.addRow("服务名称", self.custom_name_edit)
        custom_form.addRow("接口地址", self.custom_base_edit)
        custom_form.addRow("模型名称", self.custom_model_edit)
        custom_form.addRow("", self.custom_url_label)
        custom_form.addRow("", self.custom_hint)
        self.custom_page = QWidget()
        self.custom_page.setLayout(custom_form)

        self.pages = QStackedWidget()
        self.pages.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.pages.addWidget(mock_page)
        self.pages.addWidget(self.go_page)
        self.pages.addWidget(self.console_page)
        self.pages.addWidget(self.custom_page)

        self.key_edit = QLineEdit()
        self.key_edit.setObjectName("aiKeyEdit")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("留空则继续使用当前服务已填写的密钥（只在本机内存）")
        self.show_key = QCheckBox("显示密钥")
        self.show_key.setObjectName("aiShowKeyCheck")
        self.show_key.toggled.connect(
            lambda checked: self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_form = QFormLayout()
        key_form.addRow("API Key", self.key_edit)
        key_form.addRow("", self.show_key)
        self.key_box = QWidget()
        self.key_box.setLayout(key_form)

        self.sources = QLabel("")
        self.sources.setObjectName("aiSettingsSources")
        self.sources.setWordWrap(True)
        self.sources.setStyleSheet("color: #666;")

        # R1-14：个体数据外发开关（默认关闭）。关着的时候，作答原文/追问原话/错因判断
        # 一律不外发；开启后每次真实外发由服务端写一条审计。桌面端是唯一真能外发的客户端。
        self.individual_box = QWidget()
        individual_layout = QVBoxLayout(self.individual_box)
        individual_layout.setContentsMargins(0, 6, 0, 6)
        individual_layout.setSpacing(4)
        individual_title = QLabel("个体数据外发（默认关闭）")
        individual_title.setStyleSheet("font-weight: 600;")
        self.individual_check = QCheckBox("允许把学生作答原文、追问原话和错因判断发送给 AI 服务")
        self.individual_check.setObjectName("aiIndividualCheck")
        self.individual_state = QLabel("")
        self.individual_state.setObjectName("aiIndividualState")
        self.individual_state.setWordWrap(True)
        self.individual_state.setStyleSheet("color: #8a5a00;")
        self.individual_note = QLabel("")
        self.individual_note.setObjectName("aiIndividualNote")
        self.individual_note.setWordWrap(True)
        self.individual_note.setStyleSheet("color: #666;")
        self.individual_save = QPushButton("保存开关")
        self.individual_save.setObjectName("aiIndividualSave")
        self.individual_save.clicked.connect(self._save_individual)
        individual_row = QHBoxLayout()
        individual_row.addWidget(self.individual_save)
        individual_row.addStretch(1)
        individual_layout.addWidget(individual_title)
        individual_layout.addWidget(self.individual_check)
        individual_layout.addWidget(self.individual_state)
        individual_layout.addWidget(self.individual_note)
        individual_layout.addLayout(individual_row)
        self.note = QLabel("")
        self.note.setObjectName("aiSettingsNote")
        self.note.setWordWrap(True)

        self.status = QLabel("")
        self.status.setObjectName("aiSettingsStatus")
        self.status.setWordWrap(True)
        self.save_button = QPushButton("保存并启用")
        self.save_button.setObjectName("aiSettingsSave")
        theme.primary(self.save_button)
        self.save_button.clicked.connect(self._save)
        self.trial_button = QPushButton("试分析…")
        self.trial_button.setObjectName("aiSettingsTrial")
        self.trial_button.clicked.connect(self._trial)
        self.clear_button = QPushButton("清除本次登录密钥")
        self.clear_button.setObjectName("aiSettingsClearKey")
        self.clear_button.clicked.connect(self._clear_key)
        cancel = QPushButton("取消")
        cancel.setObjectName("aiSettingsCancel")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.trial_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        buttons.addWidget(cancel)

        top = QFormLayout()
        top.addRow("分析方式", self.mode_combo)

        # 表单与说明放进滚动区：1366×768 上也能看到底部的保存按钮。
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(self.pages)
        body_layout.addWidget(self.key_box)
        body_layout.addWidget(self.individual_box)
        body_layout.addWidget(self.sources)
        body_layout.addWidget(self.note)
        body_layout.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("aiSettingsScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumSize(320, 180)
        self.scroll.setWidget(body)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        fit_to_screen(self, 780, 700)
        self._load()

    def _load(self):
        self._data = self.service.ai_settings()
        data = self._data
        self.model_combo.clear()
        for item in data.get("go_models") or []:
            self.model_combo.addItem(item["id"] if not item.get("note") else "%s（%s）" % (item["id"], item["note"]), item["id"])
        if not self.model_combo.count():
            self.model_combo.addItem(data.get("go_default_model") or "qwen3.8-max", data.get("go_default_model") or "qwen3.8-max")
        mode = data.get("mode") or "mock"
        index = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(index if index >= 0 else 0)
        if mode == "go":
            target = self.model_combo.findData(data.get("model"))
            if target >= 0:
                self.model_combo.setCurrentIndex(target)
        self.base_edit.setText(data.get("account_base_url") or "")
        self.model_edit.setText(data.get("account_model") or "")
        self.custom_name_edit.setText(data.get("account_custom_name") or "")
        self.custom_base_edit.setText(data.get("account_custom_base") or "")
        self.custom_model_edit.setText(data.get("account_custom_model") or "")
        self.key_edit.clear()
        self._mode_changed()
        self._custom_preview()
        self._render()
        self._load_individual()

    def _load_individual(self):
        """读账号级「个体数据外发」开关；老版本 service 没有这个方法时如实说明，不假装已关。"""
        reader = getattr(self.service, "ai_individual_state", None)
        if reader is None:
            self.individual_check.setChecked(False)
            self.individual_check.setEnabled(False)
            self.individual_save.setEnabled(False)
            self.individual_state.setText("当前版本未提供「个体数据外发」开关。")
            self.individual_note.setText("")
            return
        try:
            data = reader() or {}
        except ApiError as exc:
            self.individual_state.setText(exc.message)
            return
        allowed = bool(data.get("ai_individual_allowed"))
        egress = bool(data.get("ai_egress_available"))
        self.individual_check.blockSignals(True)
        self.individual_check.setChecked(allowed)
        self.individual_check.blockSignals(False)
        self.individual_check.setEnabled(True)
        self.individual_save.setEnabled(True)
        self.individual_state.setText(
            ("当前：已允许个体数据外发；%s" if allowed else "当前：默认关闭，个体数据不会外发；%s")
            % ("本机可以外发（每次都会写审计）" if egress
               else "本机不能外发（网页端只记录偏好）"))
        self.individual_note.setText(data.get("note") or "")

    def _save_individual(self):
        writer = getattr(self.service, "set_ai_individual", None)
        if writer is None:
            self.individual_state.setText("当前版本未提供「个体数据外发」开关。")
            return False
        try:
            data = writer(bool(self.individual_check.isChecked())) or {}
        except ApiError as exc:
            self.individual_state.setText(exc.message)
            return False
        allowed = bool(data.get("ai_individual_allowed"))
        self.individual_check.blockSignals(True)
        self.individual_check.setChecked(allowed)
        self.individual_check.blockSignals(False)
        self.individual_state.setText(
            "已保存：%s；%s" % ("允许个体数据外发" if allowed else "关闭个体数据外发",
                              "本机可以外发（每次都会写审计）" if data.get("ai_egress_available")
                              else "本机不能外发（网页端只记录偏好）"))
        self.individual_note.setText(data.get("note") or "")
        return True

    def _mode_changed(self, *_):
        mode = self.mode_combo.currentData()
        self.pages.setCurrentIndex({"mock": 0, "go": 1, "real": 2, "custom": 3}.get(mode, 0))
        self.key_box.setVisible(mode != "mock")
        self.status.setText("")

    def _custom_preview(self, *_):
        base = self.custom_base_edit.text().strip()
        if not base:
            self.custom_url_label.setText("完整请求地址：填写接口地址后显示")
            return
        self.custom_url_label.setText("完整请求地址：%s" % vision.custom_request_url(base))

    def _render(self):
        data = self._data
        mode = data.get("mode") or "mock"
        key_state = "本次登录已填写" if data.get("session_key_configured") else "未配置"
        if mode == "mock":
            self.sources.setText("服务：模拟分析\n不联网，不读取照片，不发送任何内容。")
        else:
            test_line = "\n测试地址：%s（仅自动化测试）" % data.get("test_base") if data.get("test_base") else ""
            self.sources.setText(
                "服务：%s\n基底地址：%s\n完整请求地址：%s\n模型：%s\n密钥：%s%s"
                % (
                    data.get("service_name") or "",
                    data.get("base_url") or "",
                    data.get("request_url") or "",
                    data.get("model") or "",
                    key_state,
                    test_line,
                )
            )
            if mode == "custom":
                self.sources.setText(
                    self.sources.text()
                    + "\n说明：自定义服务由服务商决定是否兼容图片输入，保存配置不代表已经连接成功。"
                )
        self._go_preview()
        self.go_policy.setText(data.get("go_policy_note") or "")
        if mode == "mock":
            self.note.setText(
                "模拟分析：不联网、不读取照片，不会产生费用。升级到本版本不会自动把旧数据发到云端。"
            )
        elif mode == "custom":
            self.note.setText(
                "密钥只保存在这次登录的内存里，退出登录或换账号后清除；不会写入数据库、日志或交付包。"
                "地址或服务名称改动后，这个服务之前填写的密钥会被清除。正式分析前会再次确认发送内容。"
            )
        else:
            self.note.setText(
                "密钥只保存在这次登录的内存里，退出登录或换账号后清除；不会写入数据库、日志或交付包。"
                "保存后立即生效，正式分析前会再次确认发送。"
            )
        blocked = data.get("billing_block_message") or ""
        if blocked:
            state = "已停止发送：%s\n请先核对账户设置或服务额度，再保存或清除密钥。" % blocked
        elif mode == "mock":
            state = "当前：模拟分析（不发送）。"
        elif mode == "custom" and (not data.get("address_ok") or not data.get("base_url") or not data.get("model")):
            state = "当前：自定义 AI 服务，地址或模型未填完整；试分析和正式分析会停止并提示。"
        elif not data.get("key_configured"):
            state = "当前：%s，未配置密钥。试分析和正式分析会停止并提示，不会改用模拟。" % data.get("service_name")
        elif data.get("verify_state") == "ok":
            state = "当前：%s，已配置，试分析成功。" % data.get("service_name")
        elif data.get("verify_state") == "failed":
            state = "当前：%s，已配置，上次试分析失败：%s" % (
                data.get("service_name"), data.get("verify_message") or "原因未记录"
            )
        else:
            state = "当前：%s，已配置，尚未验证。" % data.get("service_name")
        self.status.setText(state)

    def _go_preview(self):
        """按所选 Go 图片模型显示基底地址和完整请求地址。"""
        data = self._data
        model_id = self.model_combo.currentData() or data.get("go_default_model")
        spec = None
        for item in data.get("go_models") or []:
            if item.get("id") == model_id:
                spec = item
                break
        base = (spec or {}).get("base_url") or data.get("go_base_url") or ""
        if data.get("test_base"):
            base = data["test_base"]
        api = (spec or {}).get("api") or "messages"
        path = "/messages" if api == "messages" else "/chat/completions"
        self.go_base_label.setText(
            "基底地址：%s\n完整请求地址：%s%s"
            % (base, base.rstrip("/") + path, "\n（本机测试地址，仅自动化测试）" if data.get("test_base") else "")
        )

    def _save(self):
        mode = self.mode_combo.currentData()
        name = ""
        if mode == "go":
            base = ""
            model = self.model_combo.currentData() or self.model_combo.currentText()
        elif mode == "real":
            base = self.base_edit.text()
            model = self.model_edit.text()
        elif mode == "custom":
            base = self.custom_base_edit.text()
            model = self.custom_model_edit.text()
            name = self.custom_name_edit.text()
        else:
            base = ""
            model = ""
        try:
            self._data = self.service.save_ai_settings(
                mode, base, model, self.key_edit.text() or None, service_name=name
            )
        except ApiError as exc:
            self.status.setText(exc.message)
            return False
        self.saved = True
        self.key_edit.clear()
        self._custom_preview()
        self._render()
        return True

    def _trial(self):
        if not self._save():
            return
        dialog = AITrialDialog(self.service, self)
        dialog.exec()
        self._data = self.service.ai_settings()
        self._render()

    def _clear_key(self):
        try:
            self._data = self.service.clear_ai_key(self.mode_combo.currentData())
        except ApiError as exc:
            self.status.setText(exc.message)
            return
        self.key_edit.clear()
        self._render()
        self.status.setText("已清除本次登录密钥。")

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)


class AITrialDialog(QDialog):
    """用一张不含学生身份的题图试分析；不创建错题、不写统计。"""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self._closed = False
        self._token = None
        self.setObjectName("aiTrialDialog")
        self.setWindowTitle("AI 试分析（不保存为错题）")
        fit_to_screen(self, 760, 680)

        self.config_label = QLabel("")
        self.config_label.setObjectName("aiTrialConfig")
        self.config_label.setWordWrap(True)

        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("aiTrialPath")
        self.path_edit.setReadOnly(True)
        # 自动化测试用：设置 MISTAKE_TRIAL_PHOTO_PATH 时直接带入题图，跳过系统选图窗口。
        trial_photo = os.environ.get("MISTAKE_TRIAL_PHOTO_PATH") or ""
        if trial_photo:
            self.path_edit.setText(trial_photo)
        self.choose_button = QPushButton("选择题图")
        self.choose_button.setObjectName("aiTrialChoosePhoto")
        self.choose_button.clicked.connect(self._choose_photo)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.choose_button)

        self.question = QPlainTextEdit()
        self.question.setObjectName("aiTrialQuestion")
        self.question.setPlaceholderText("可选：照片里的题目文字")
        self.question.setMaximumHeight(70)
        self.solution = QPlainTextEdit()
        self.solution.setObjectName("aiTrialSolution")
        self.solution.setPlaceholderText("可选：学生写的过程")
        self.solution.setMaximumHeight(70)

        self.consent = QCheckBox(
            "我已知晓：照片、题目和解题过程会发送给上面显示的云端服务，可能产生费用"
        )
        self.consent.setObjectName("aiTrialConsent")
        self.send_note = QLabel("")
        self.send_note.setObjectName("aiTrialSendNote")
        self.send_note.setWordWrap(True)

        self.start_button = QPushButton("开始试分析")
        self.start_button.setObjectName("aiTrialStart")
        self.start_button.clicked.connect(self._start)
        self.status = QLabel("")
        self.status.setObjectName("aiTrialStatus")
        self.status.setWordWrap(True)
        self.result = QPlainTextEdit()
        self.result.setObjectName("aiTrialResult")
        self.result.setReadOnly(True)

        form = QFormLayout()
        form.addRow("题图", path_row)
        form.addRow("题目", self.question)
        form.addRow("解题过程", self.solution)

        layout = QVBoxLayout(self)
        layout.addWidget(self.config_label)
        layout.addLayout(form)
        layout.addWidget(self.consent)
        layout.addWidget(self.send_note)
        layout.addWidget(self.start_button)
        layout.addWidget(self.status)
        layout.addWidget(self.result, 1)
        self._refresh_config()

    def _refresh_config(self):
        data = self.service.ai_settings()
        mode = data.get("mode") or "mock"
        self._mode = mode
        remote = mode in ("real", "go", "custom")
        if remote:
            test_line = "\n测试地址：%s（仅自动化测试）" % data.get("test_base") if data.get("test_base") else ""
            self.config_label.setText(
                "服务：%s\n基底地址：%s\n完整请求地址：%s\n模型：%s\n模式：%s%s"
                % (
                    data.get("service_name") or "未启用云端分析",
                    data.get("base_url") or "",
                    data.get("request_url") or "",
                    data.get("model") or "",
                    "真实云端调用",
                    test_line,
                )
            )
        else:
            self.config_label.setText("服务：模拟分析\n模式：模拟分析（不会发送，不读取照片）")
        self.consent.setVisible(remote)
        self.consent.setChecked(False)
        self.consent.setText(
            "我已知晓：照片、题目和解题过程会发送给%s，可能产生费用或消耗额度"
            % (data.get("service_name") or "云端服务")
        )
        if mode == "go":
            self.send_note.setText(
                "OpenCode Go 模式：照片和文字会发送到 Go 订阅接口，用量计入订阅额度；"
                "试分析结果不写入任何错题。若返回的计费来源不是 Go，或出现额外费用，"
                "软件会停止后续发送并提示核对账户设置。"
            )
        elif mode == "custom":
            self.send_note.setText(
                "自定义 AI 服务：照片和文字会按上面显示的地址发送给该服务商，"
                "可能按次计费；用量和费用以对方返回为准，没有返回就显示尚未确认。"
                "不同服务商的兼容程度不同，必须是支持图片输入的模型。"
            )
        elif mode == "real":
            self.send_note.setText(
                "真实模式：点开始后会先把照片和文字发送给 OpenCode Console，"
                "试分析结果不写入任何错题，可能产生费用。"
            )
        else:
            self.send_note.setText("当前是模拟模式：不联网、不读取照片内容，结果只用于看界面。")
        if remote:
            self.send_note.setText(
                self.send_note.text()
                + "\n不会单独发送学生姓名、班级或账号；照片和文字里若出现这些信息，对方仍可能看到。"
            )

    def _choose_photo(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择题图", "", PHOTO_FILTER)
        if path:
            self.path_edit.setText(path)
            self.status.setText("")

    def _start(self):
        if self.runner.busy:
            return
        path = self.path_edit.text().strip()
        if not path:
            self.status.setText("请先选择题图。")
            return
        if self._mode in ("real", "go", "custom") and not self.consent.isChecked():
            self.status.setText("请先勾选同意，才会发送给云端。")
            return
        self.start_button.setEnabled(False)
        self.status.setText("正在分析，请稍候…")
        self.result.clear()
        self._token = self.service.token
        self.runner.run(
            lambda: self.service.trial_analysis(
                path, self.question.toPlainText(), self.solution.toPlainText(),
                consent=self.consent.isChecked(),
            ),
            self._done,
            self._failed,
        )

    def _alive(self):
        return not self._closed and self.service.token == self._token

    def _failed(self, message):
        if not self._alive():
            return
        self.start_button.setEnabled(True)
        self.status.setText("试分析没有完成：%s" % message)

    def _done(self, payload):
        if not self._alive():
            return
        self.start_button.setEnabled(True)
        if not payload.get("ok"):
            self.status.setText(
                "试分析没有完成：%s" % analysis_failure_text(
                    payload.get("error") or "分析失败",
                    payload.get("error_code") or "",
                    payload.get("mode") or self._mode,
                )
            )
            return
        usage = payload.get("usage") or {}
        billing = usage.get("billing_source")
        mode = payload.get("mode") or self._mode
        if usage_has_values(usage):
            parts = [
                "prompt %s / completion %s / total %s"
                % (
                    usage_field_text(usage.get("prompt_tokens")),
                    usage_field_text(usage.get("completion_tokens")),
                    usage_field_text(usage.get("total_tokens")),
                ),
                "计费来源：%s" % usage_field_text(billing),
                "费用：%s" % ("$%s" % usage_field_text(usage.get("cost"))
                              if usage.get("cost") is not None else "尚未确认"),
            ]
            if mode == "go":
                parts.append(
                    "额度计入：%s"
                    % ("$%s" % usage_field_text(usage.get("allowance"))
                       if usage.get("allowance") is not None else "尚未确认")
                )
            usage_text = "；".join(parts)
        else:
            usage_text = "用量：尚未确认（服务没有返回用量、费用和计费来源）"
        if mode == "go":
            status = "Go 试分析成功（图片已发送）"
        elif mode == "real":
            status = "Console 试分析成功（图片已发送）"
        elif mode == "custom":
            status = "自定义服务试分析成功（图片已发送）"
        else:
            status = "模拟试分析完成（未发送，未读取图片内容）"
        warn = ""
        if mode == "go" and usage:
            if billing and billing not in ("go", "go-plus"):
                warn = (
                    "\n\n注意：服务把本次计费来源标为「%s」，不是 Go 订阅。"
                    "软件已停止后续发送，请核对账户设置。" % billing
                )
            elif billing in (None, "") and usage.get("cost") not in (None, 0, 0.0):
                warn = (
                    "\n\n注意：本次返回费用 $%s，但没有标明 Go 订阅来源。"
                    "软件已停止后续发送，请核对账户设置。" % usage.get("cost")
                )
            elif usage.get("cost") not in (None, 0, 0.0):
                warn = (
                    "\n\n注意：本次返回费用 $%s，不属于订阅额度内的零费用调用。"
                    "软件已停止后续发送，请核对账户设置。" % usage.get("cost")
                )
        self.status.setText(
            "%s；耗时 %d 毫秒；%s%s"
            % (status, int(payload.get("elapsed_ms") or 0), usage_text, warn)
        )
        self.result.setPlainText(
            "服务：%s\n请求地址：%s\n模型：%s\n\n%s"
            % (
                payload.get("service") or "",
                payload.get("request_url") or payload.get("base_url") or "",
                payload.get("model") or "",
                suggestion_text(payload.get("suggestion") or {}),
            )
        )

    def closeEvent(self, event):
        self._closed = True
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)


class ConsentDialog(QDialog):
    """真实云端分析前的确认框：默认不勾选，点取消就不发请求。"""

    def __init__(self, meta, parent=None):
        super().__init__(parent)
        self.setObjectName("aiConsentDialog")
        provider = meta.get("ai_provider") or "云端服务"
        self.setWindowTitle("确认把这条错题发给 %s？" % provider)
        self.setMinimumWidth(560)
        mode = meta.get("ai_mode") or ""
        if mode == "custom":
            billing_line = ("分析可能产生费用或消耗所选服务的额度，"
                            "关掉窗口也不能保证已经发出的请求撤回或免费。")
        else:
            billing_line = ("分析可能按次计费或计入订阅额度，"
                            "关掉窗口也不能保证已经发出的请求撤回或免费。")
        lines = [
            "服务：%s" % provider,
            "模型：%s" % (meta.get("ai_model") or "未配置"),
            "",
            "本次会发送：",
            "· 错题照片",
            "· 题目文字",
            "· 学生写的过程",
            "",
            "软件不会单独发送学生姓名、班级或账号字段。",
            meta.get("ai_price_note") or "",
            meta.get("ai_policy_summary") or "",
            "",
            "照片和文字可能包含学生姓名、班级等身份信息，发送后不能保证对方不留存；"
            + billing_line,
        ]
        text = QLabel("\n".join(line for line in lines if line is not None))
        text.setObjectName("aiConsentText")
        text.setWordWrap(True)
        self.check = QCheckBox("我已知晓发送内容和可能产生的费用，同意这一次发送")
        self.check.setObjectName("aiConsentCheck")
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("继续分析")
        self.ok_button.setObjectName("aiConsentOk")
        self.ok_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.Cancel).setObjectName("aiConsentCancel")
        self.check.toggled.connect(self.ok_button.setEnabled)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(self.check)
        layout.addWidget(self.buttons)

    @property
    def agreed(self):
        return self.check.isChecked()

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)


SUGGESTION_FIELDS = (
    ("question_text", "题目内容"),
    ("solution_text", "解题过程"),
    ("knowledge_points", "知识点"),
    ("error_type", "错误类型"),
    ("observed_errors", "学生错误表现"),
    ("possible_causes", "可能原因"),
    ("advice_text", "教学建议"),
)


def suggestion_text(suggestion):
    """把一份建议排成老师能直接看的几段文字。"""
    lines = []
    for field, title in SUGGESTION_FIELDS:
        value = suggestion.get(field)
        if isinstance(value, list):
            value = "、".join(str(item) for item in value)
        lines.extend(["【%s】" % title, (value or "（空）").strip(), ""])
    if suggestion.get("insufficient"):
        lines.extend(["【信息不足】", suggestion.get("insufficient_reason") or "（没有说明原因）", ""])
    if suggestion.get("likely_correct"):
        lines.extend(["【可能答对】这条错题看起来学生其实做对了。", ""])
    lines.extend(["【依据】", suggestion.get("basis") or "（没有说明）"])
    return "\n".join(lines)


class SuggestionDialog(QDialog):
    """建议预览：未采用前一个字都不会写进这条错题。"""

    def __init__(self, suggestion, parent=None):
        super().__init__(parent)
        self.setObjectName("aiSuggestionDialog")
        self.setWindowTitle("AI 建议（还没有保存）")
        fit_to_screen(self, 680, 560)
        self.body = QPlainTextEdit()
        self.body.setObjectName("aiSuggestionBody")
        self.body.setReadOnly(True)
        self.body.setPlainText(suggestion_text(suggestion))
        note = QLabel("预览不会保存。点「采用这条建议」才会写进这条错题，之后仍要老师核对确认。")
        note.setObjectName("aiSuggestionNote")
        note.setWordWrap(True)
        self.buttons = QDialogButtonBox()
        self.apply_button = self.buttons.addButton("采用这条建议", QDialogButtonBox.AcceptRole)
        self.apply_button.setObjectName("aiApplySuggestion")
        self.discard_button = self.buttons.addButton("放弃这条建议", QDialogButtonBox.RejectRole)
        self.discard_button.setObjectName("aiDiscardSuggestion")
        self.discarded = False
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self._reject_all)
        self.discard_button.clicked.connect(self._mark_discarded)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.buttons)

    def _mark_discarded(self):
        self.discarded = True

    def _reject_all(self):
        self.reject()

    def showEvent(self, event):
        super().showEvent(event)
        auto_shot(self)


class MistakesWindow(QMainWindow):
    """一个学生的错题：列表、详情、原图、录入与修改。"""

    closed = Signal()

    def __init__(self, service, class_row, student, parent=None, focus_id=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self.class_row = class_row
        self.student = student
        self.class_id = class_row["id"]
        self.student_id = student["id"]
        self._items = []
        self._focus_id = focus_id
        self._detail_id = None
        self._detail_seq = 0
        self.analysis_runner = TaskRunner()
        self._meta = {}
        self._preview = None
        self._analyzing = False
        # 每次发起分析就换一代；晚到的结果一律作废，不弹窗、不恢复旧建议。
        self._analysis_gen = 0
        self._closed = False
        self._analysis_token = None
        self._current = None
        self._flash = ""
        self.setObjectName("mistakesWindow")
        self.setWindowTitle("数智伴学 · 错题")
        fit_to_screen(self, 1000, 660)

        central = QWidget()
        self.setCentralWidget(central)

        heading = QLabel("%s · %s" % (class_row.get("name", ""), student.get("name", "")))
        heading.setObjectName("mistakeStudentName")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        active = bool(student.get("active", 1))
        state = QLabel("在读" if active else "已停用（历史错题仍可查看）")
        state.setObjectName("mistakeStudentState")
        self.back_button = QPushButton("返回学生名单")
        self.back_button.setObjectName("backToStudents")
        self.back_button.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(heading)
        top.addWidget(state)
        top.addStretch(1)
        top.addWidget(self.back_button)

        self.new_button = QPushButton("录入错题")
        self.new_button.setObjectName("newMistakeButton")
        self.new_button.clicked.connect(self.open_create_dialog)
        self.new_button.setEnabled(active)
        if not active:
            self.new_button.setToolTip("学生已停用，请先在学生名单里启用再录入错题")
        self.edit_button = QPushButton("修改")
        self.edit_button.setObjectName("editMistakeButton")
        self.edit_button.clicked.connect(self.open_edit)
        self.photo_button = QPushButton("查看原图")
        self.photo_button.setObjectName("viewPhotoButton")
        self.photo_button.clicked.connect(self.open_photo)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("refreshMistakesButton")
        self.refresh_button.clicked.connect(self.reload)
        self.settings_button = QPushButton("AI 设置")
        self.settings_button.setObjectName("openAiSettingsFromMistakes")
        self.settings_button.clicked.connect(self.open_ai_settings)
        self.analyze_button = QPushButton("AI 分析")
        self.analyze_button.setObjectName("analyzeButton")
        self.analyze_button.clicked.connect(self.start_analysis)
        self.suggestion_button = QPushButton("查看建议")
        self.suggestion_button.setObjectName("suggestionButton")
        self.suggestion_button.clicked.connect(self.show_suggestion)
        self.apply_button = QPushButton("采用建议")
        self.apply_button.setObjectName("applySuggestionButton")
        self.apply_button.clicked.connect(self.apply_suggestion)
        self.discard_button = QPushButton("放弃建议")
        self.discard_button.setObjectName("discardSuggestionButton")
        self.discard_button.clicked.connect(self.discard_suggestion)
        self.return_button = QPushButton("退回")
        self.return_button.setObjectName("returnMistakeButton")
        self.return_button.clicked.connect(self.return_current)
        self.confirm_button = QPushButton("确认核对")
        self.confirm_button.setObjectName("confirmMistakeButton")
        self.confirm_button.clicked.connect(self.confirm_current)
        self.status = QLabel("")
        self.status.setObjectName("mistakeStatus")

        tools = QHBoxLayout()
        for widget in (self.new_button, self.edit_button, self.photo_button,
                       self.refresh_button, self.settings_button):
            tools.addWidget(widget)
        tools.addWidget(self.status, 1)

        ai_tools = QHBoxLayout()
        for widget in (
            self.analyze_button,
            self.suggestion_button,
            self.apply_button,
            self.discard_button,
            self.return_button,
            self.confirm_button,
        ):
            ai_tools.addWidget(widget)
        self.ai_note = QLabel("")
        self.ai_note.setObjectName("aiConfigNote")
        self.ai_note.setWordWrap(True)
        ai_tools.addWidget(self.ai_note, 1)

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("mistakeTable")
        self.table.setHorizontalHeaderLabels(["错题日期", "题目摘要", "状态", "照片"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemSelectionChanged.connect(self.load_selected)
        self.table.itemDoubleClicked.connect(self.open_edit)

        self.empty = QLabel("这个学生还没有错题。点「录入错题」添加第一条。")
        self.empty.setObjectName("mistakeEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet("color: #777;")

        self.body = QStackedWidget()
        self.body.setObjectName("mistakesBody")
        self.body.addWidget(self.empty)
        self.body.addWidget(self.table)

        self.detail = QLabel("")
        self.detail.setObjectName("mistakeDetail")
        self.detail.setWordWrap(True)
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.detail.setStyleSheet("font-size: 12px;")
        self.detail_area = QScrollArea()
        self.detail_area.setObjectName("mistakeDetailArea")
        self.detail_area.setWidgetResizable(True)
        self.detail_area.setWidget(self.detail)

        self.photo_preview = QLabel("")
        self.photo_preview.setObjectName("mistakePhoto")
        self.photo_preview.setMinimumHeight(160)
        self.photo_preview.setAlignment(Qt.AlignCenter)
        self.photo_preview.setStyleSheet("background: #f3f3f3; color: #777;")

        detail_title = QLabel("错题详情")
        detail_title.setStyleSheet("font-weight: 600;")

        self.badge_source = QLabel("")
        self.badge_status = QLabel("")
        self.badge_run = QLabel("")
        self.badge_conclusion = QLabel("")
        badges = QHBoxLayout()
        for widget in (self.badge_source, self.badge_status, self.badge_run, self.badge_conclusion):
            widget.setObjectName("aiBadge")
            widget.setStyleSheet("border: 1px solid #d0d0d0; border-radius: 8px; padding: 2px 8px;")
            badges.addWidget(widget)
        badges.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(detail_title)
        right.addLayout(badges)
        right.addWidget(self.detail_area, 3)
        right.addWidget(self.photo_preview, 2)

        row = QHBoxLayout()
        row.addWidget(self.body, 3)
        row.addLayout(right, 2)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addLayout(tools)
        layout.addLayout(ai_tools)
        layout.addLayout(row, 1)

        self._load_meta()
        self._sync_buttons()

    # ---------------------------------------------------------------- 数据
    def _selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _sync_buttons(self):
        item = self._selected()
        self.edit_button.setEnabled(item is not None)
        self.photo_button.setEnabled(bool(item and item.get("has_image")))
        self._update_ai_buttons()

    def _update_ai_buttons(self):
        item = self._selected()
        has_preview = bool(self._preview)
        self.analyze_button.setEnabled(item is not None and not self._analyzing)
        self.suggestion_button.setEnabled(has_preview)
        self.apply_button.setEnabled(has_preview)
        self.discard_button.setEnabled(has_preview)
        status = (self._current or {}).get("status")
        self.confirm_button.setEnabled(item is not None and status != "confirmed")
        self.return_button.setEnabled(item is not None and status == "pending")
        mode = (self._current or {}).get("ai_mode") or ""
        self.analyze_button.setText("重新分析" if mode and mode != "local" else "AI 分析")

    def _load_meta(self):
        try:
            self._meta = self.service.meta() or {}
        except Exception as exc:
            self._meta = {}
            self.ai_note.setText("分析配置读不出来：%s" % exc)
            return
        mode = self._meta.get("ai_mode") or ""
        if mode in ("real", "go", "custom"):
            key_state = "已填写" if self._meta.get("ai_key_configured") else "未配置"
            self.ai_note.setText(
                "真实分析：%s\n请求地址 %s · 模型 %s · 密钥%s。"
                "已配置不等于调用成功；密钥或地址未配置完整时点击分析会停止并提示，不会改用模拟。每次发送前都会再确认。"
                % (
                    self._meta.get("ai_provider") or "云端视觉服务",
                    self._meta.get("ai_request_url") or self._meta.get("ai_base_url") or "未配置",
                    self._meta.get("ai_model") or "未配置",
                    key_state,
                )
            )
        else:
            self.ai_note.setText("模拟分析：不联网、不读取照片内容（当前模式：%s）" % AI_MODE_TEXT.get(mode, mode or "未知"))

    def _set_run_badge(self, text=""):
        data = self._current or {}
        if text:
            self.badge_run.setText("本次：%s" % text)
        elif self._analyzing:
            self.badge_run.setText("本次：分析中…")
        elif data.get("ai_error"):
            self.badge_run.setText("本次：分析失败")
        else:
            self.badge_run.setText("本次：未分析")

    def reload(self):
        if self.runner.busy:
            QTimer.singleShot(150, self.reload)
            return
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载错题…")
        keep = self._detail_id or self._focus_id
        self.runner.run(
            lambda: self.service.list_mistakes(self.class_id, self.student_id),
            lambda items: self._show_list(items, keep),
            self._load_failed,
        )

    def _load_failed(self, message):
        self.refresh_button.setEnabled(True)
        self.status.setText(message)

    def _show_list(self, items, keep):
        self.refresh_button.setEnabled(True)
        self._items = list(items)
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            text = (item.get("question_text") or "").replace("\n", " ").strip()
            values = [
                item.get("occurred_on") or "",
                text or "（没有填写题目内容）",
                STATUS_TEXT.get(item.get("status"), item.get("status") or ""),
                "有" if item.get("has_image") else "无",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 1:
                    cell.setToolTip(text)
                self.table.setItem(row, column, cell)
        self.body.setCurrentIndex(1 if items else 0)
        self.status.setText("共 %d 条错题" % len(items) if items else "")
        if not items:
            self._detail_id = None
            self._show_detail_empty("这个学生还没有错题。")
            self._sync_buttons()
            return
        target = 0
        if keep:
            for index, item in enumerate(items):
                if item["id"] == keep:
                    target = index
                    break
        self.table.selectRow(target)
        self._focus_id = None
        self._sync_buttons()
        # 选中行没变时不会触发选中信号，这里主动读一次，保证列表刷新后详情也是新的。
        self.load_selected()

    def load_selected(self):
        item = self._selected()
        if item is None:
            self._detail_id = None
            self._show_detail_empty("先在左边点一条错题，右边显示详情。")
            return
        self._detail_id = item["id"]
        if self._preview and self._preview.get("mistake_id") != item["id"]:
            self._drop_preview()
            self._update_ai_buttons()
        self._detail_seq += 1
        seq = self._detail_seq
        self.status.setText("正在打开错题…")
        self.runner.run(
            lambda: self._read_detail(item["id"]),
            lambda payload: self._show_detail(payload, seq),
            lambda message: self._detail_failed(message, seq),
        )

    def _read_detail(self, mistake_id):
        data = self.service.get_mistake(mistake_id)
        photo = None
        if data.get("has_image"):
            try:
                photo = self.service.mistake_photo(mistake_id)
            except ApiError as exc:
                photo = ("error", exc.message)
        return data, photo

    def _show_detail_empty(self, text):
        self.detail.setText(text)
        self.photo_preview.setText("")

    def _detail_failed(self, message, seq):
        if self._closed or seq != self._detail_seq:
            return
        self.status.setText(message)

    def _show_detail(self, payload, seq):
        if self._closed or seq != self._detail_seq:
            return  # 老师已经换了一条，旧请求的结果直接丢掉
        data, photo = payload
        self._current = data
        self.status.setText("")
        self.badge_source.setText("来源：" + AI_MODE_TEXT.get(data.get("ai_mode"), data.get("ai_mode") or "未知"))
        self.badge_status.setText("核对：" + STATUS_TEXT.get(data.get("status"), data.get("status") or ""))
        self._set_run_badge()
        if data.get("insufficient"):
            self.badge_conclusion.setText("结论：信息不足")
        elif data.get("likely_correct"):
            self.badge_conclusion.setText("结论：可能答对")
        else:
            self.badge_conclusion.setText("结论：—")
        lines = [
            "日期：%s" % (data.get("occurred_on") or "未填写"),
            "来源：%s%s" % (
                data.get("source_type") or "",
                (" · " + data["source_detail"]) if data.get("source_detail") else "",
            ),
            "状态：%s" % STATUS_TEXT.get(data.get("status"), data.get("status") or ""),
            "分析来源：%s" % AI_MODE_TEXT.get(data.get("ai_mode"), data.get("ai_mode") or "未知"),
        ]
        if data.get("ai_error"):
            lines.append("最近一次分析失败：%s" % data["ai_error"])
        if data.get("insufficient"):
            lines.append("信息不足：%s" % (data.get("insufficient_reason") or "（没有说明原因）"))
        if data.get("likely_correct"):
            lines.append("可能答对：学生可能其实做对了")
        points = data.get("knowledge_points") or []
        lines.extend(["", "知识点", "、".join(points) if points else "（空）"])
        lines.extend(["", "错误类型", data.get("error_type") or "（空）"])
        lines.extend(["", "题目内容", data.get("question_text") or "（空）"])
        lines.extend(["", "解题过程", data.get("solution_text") or "（空）"])
        lines.extend(["", "学生错误表现"])
        observed = data.get("observed_errors") or []
        lines.extend(("- " + item) for item in observed) if observed else lines.append("（空）")
        causes = data.get("possible_causes") or []
        lines.extend(["", "可能原因（推测）"])
        lines.extend(("- " + item) for item in causes) if causes else lines.append("（空）")
        lines.extend(["", "教学建议", data.get("advice_text") or "（空）"])
        if data.get("teacher_note"):
            lines.extend(["", "老师备注", data["teacher_note"]])
        edited = data.get("teacher_edited_fields") or []
        if edited:
            lines.extend(["", "老师已修改过：%s" % "、".join(edited)])
        self.detail.setText("\n".join(lines))
        if self._flash:
            self.status.setText(self._flash)
            self._flash = ""
        self._update_ai_buttons()
        if not data.get("has_image"):
            self.photo_preview.setText("这条错题没有照片。")
            return
        if isinstance(photo, tuple) and photo and photo[0] == "error":
            self.photo_preview.setText("照片文件缺失：%s" % photo[1])
            return
        if not photo:
            self.photo_preview.setText("照片加载失败。")
            return
        blob = photo[0]
        pixmap = QPixmap()
        pixmap.loadFromData(blob)
        if pixmap.isNull():
            self.photo_preview.setText("照片读不出来。")
            return
        self.photo_preview.setPixmap(
            pixmap.scaled(320, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        self.photo_preview.setToolTip("点「查看原图」可以缩放查看")

    # ---------------------------------------------------------------- 动作
    def open_create_dialog(self):
        dialog = MistakeDialog(self.service, self.class_row, self.student, parent=self)
        if dialog.exec() == QDialog.Accepted and dialog.saved:
            self._detail_id = dialog.saved["id"]
            self.reload()

    def open_edit(self, *_):
        item = self._selected()
        if item is None:
            self.status.setText("先在列表里点一条错题")
            return
        try:
            data = self.service.get_mistake(item["id"])
        except ApiError as exc:
            self.status.setText(exc.message)
            return
        dialog = MistakeDialog(self.service, self.class_row, self.student, mistake=data, parent=self)
        if dialog.exec() == QDialog.Accepted and dialog.saved:
            self._detail_id = dialog.saved["id"]
            self.reload()

    def open_ai_settings(self):
        dialog = AISettingsDialog(self.service, self)
        dialog.exec()
        self._load_meta()

    def open_photo(self):
        item = self._selected()
        if item is None:
            self.status.setText("先在列表里点一条错题")
            return
        try:
            blob, _content_type = self.service.mistake_photo(item["id"])
        except ApiError as exc:
            self.status.setText(exc.message)
            return
        dialog = ImageViewerDialog(blob, self)
        dialog.exec()

    # ------------------------------------------------------- AI 分析与核对
    def start_analysis(self):
        item = self._selected()
        if item is None or self._analyzing:
            return
        status = (self._current or {}).get("status")
        if status == "confirmed":
            box = QMessageBox(self)
            box.setObjectName("reanalyzeConfirmedPrompt")
            box.setWindowTitle("这条已经确认，重新分析？")
            box.setText(
                "重新分析会生成一份新建议，采用后状态会变回「待核对」，"
                "当前已确认的题目、分析内容和核对状态会先备份在记录里。"
            )
            box.setInformativeText("如果只是想改文字，请用「修改」。")
            ok = box.addButton("继续重新分析", QMessageBox.AcceptRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not ok:
                return
        consent = False
        if (self._meta.get("ai_mode") or "") in ("real", "go", "custom"):
            dialog = ConsentDialog(self._meta, self)
            if dialog.exec() != QDialog.Accepted or not dialog.agreed:
                self.status.setText("已取消，没有发送任何内容")
                return
            consent = True
        mistake_id = item["id"]
        # 新一轮分析一旦开始，上一条建议立即作废，避免「重新分析失败后还能采用旧建议」。
        self._drop_preview()
        self._analysis_gen += 1
        generation = self._analysis_gen
        self._analysis_token = self.service.token
        self._analyzing = True
        self._set_run_badge()
        self._update_ai_buttons()
        self.status.setText("正在分析…")
        self.analysis_runner.run(
            lambda: self.service.preview_analysis(mistake_id, consent=consent, task_id=uuid.uuid4().hex),
            lambda payload: self._analysis_done(payload, mistake_id, generation),
            lambda message: self._analysis_failed(message, mistake_id, generation),
        )

    def _stale(self, generation):
        """窗口已关或已经换了一轮分析，这次结果就不该再出现在界面上。"""
        return self._closed or generation != self._analysis_gen

    def _note_failure(self, mistake_id, message, code=""):
        """失败原因只在结果仍然有效时写库：迟到的失败一个字段都不改。"""
        if self._detail_id != mistake_id or not message:
            return
        # 换过账号或退出登录后再回来，旧会话的失败也不能记到记录上。
        if self.service.token != self._analysis_token:
            return
        try:
            self.service.record_analysis_failure(mistake_id, message, code)
        except ApiError:
            return
        self._flash = "分析失败：%s" % message
        self.load_selected()

    def _analysis_failed(self, message, mistake_id=0, generation=0):
        if self._stale(generation):
            return
        self._analyzing = False
        self._update_ai_buttons()
        self.status.setText(
            analysis_failure_text(
                "分析没有完成：%s" % message, mode=self._meta.get("ai_mode") or ""
            )
        )
        self._note_failure(mistake_id, message, "")

    def _analysis_done(self, payload, mistake_id, generation=0):
        if self._stale(generation):
            if payload.get("ok") and payload.get("preview_id"):
                # 结果晚到了：业务层那份建议也要一并作废。
                try:
                    self.service.discard_preview(mistake_id, payload["preview_id"])
                except ApiError:
                    pass
            return
        self._analyzing = False
        self._update_ai_buttons()
        if not payload.get("ok"):
            self.status.setText(
                analysis_failure_text(
                    payload.get("error") or "分析失败",
                    payload.get("error_code") or "",
                    self._meta.get("ai_mode") or "",
                )
            )
            if self._detail_id == mistake_id:
                self._note_failure(mistake_id, payload.get("error") or "分析失败", payload.get("error_code") or "")
            return
        if self._detail_id != mistake_id:
            # 老师已经换了别的错题，这份建议不再保留。
            self.service.discard_preview(mistake_id, payload["preview_id"])
            return
        self._preview = {
            "mistake_id": mistake_id,
            "preview_id": payload["preview_id"],
            "suggestion": payload["suggestion"],
        }
        self._set_run_badge("已出建议，未采用")
        self._update_ai_buttons()
        self.show_suggestion()

    def show_suggestion(self):
        if not self._preview:
            return
        dialog = SuggestionDialog(self._preview["suggestion"], self)
        accepted = dialog.exec() == QDialog.Accepted
        if accepted:
            self.apply_suggestion()
        elif dialog.discarded:
            self.discard_suggestion()

    def apply_suggestion(self):
        if not self._preview:
            return
        preview = self._preview
        self.status.setText("正在采用建议…")
        try:
            self.service.apply_analysis(preview["mistake_id"], preview["preview_id"])
        except ApiError as exc:
            self._preview = None
            self._update_ai_buttons()
            self._flash = "没有采用：%s" % exc.message
            self.load_selected()
            return
        self._preview = None
        self._detail_id = preview["mistake_id"]
        self._flash = "已采用建议，请核对内容后点「确认核对」。"
        self.reload()

    def discard_suggestion(self):
        if not self._preview:
            return
        preview = self._preview
        self._preview = None
        try:
            self.service.discard_preview(preview["mistake_id"], preview["preview_id"])
        except ApiError:
            pass
        self._flash = "已放弃这条建议，记录没有改动。"
        self._update_ai_buttons()
        self.load_selected()

    def confirm_current(self):
        item = self._selected()
        if item is None:
            return
        base = (self._current or {}).get("base_fingerprint") or ""
        box = QMessageBox(self)
        box.setObjectName("confirmPrompt")
        box.setWindowTitle("确认这条错题？")
        box.setText("确认表示老师已经核对过内容，状态会变成「已确认」。")
        ok = box.addButton("确认无误", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not ok:
            return
        try:
            self.service.confirm_mistake(item["id"], base)
        except ApiError as exc:
            self._flash = "没有确认：%s" % exc.message
            self.load_selected()
            return
        self._flash = "已确认。"
        self.reload()

    def return_current(self):
        item = self._selected()
        if item is None:
            return
        reason, ok = QInputDialog.getMultiLineText(
            self, "退回这条错题", "请填写退回原因（老师能看到）：", ""
        )
        if not ok:
            return
        reason = (reason or "").strip()
        if not reason:
            self.status.setText("退回时要写清原因。")
            return
        base = (self._current or {}).get("base_fingerprint") or ""
        try:
            self.service.return_mistake(item["id"], reason, base)
        except ApiError as exc:
            self._flash = "没有退回：%s" % exc.message
            self.load_selected()
            return
        self._flash = "已退回，原因已保存。"
        self.reload()

    def _drop_preview(self):
        preview = self._preview
        self._preview = None
        if not preview:
            return
        try:
            self.service.discard_preview(preview["mistake_id"], preview["preview_id"])
        except ApiError:
            pass

    def closeEvent(self, event):
        self._closed = True
        self._analysis_gen += 1
        self._drop_preview()
        super().closeEvent(event)
        self.closed.emit()


# ---------------------------------------------------------------------------
# 作答核对（R1：与网页端「课前总览 / 单份核对 / 核对记录」同一套口径）
# ---------------------------------------------------------------------------
CHECK_ALL_BATCHES = "*all*"
CHECK_ASK_UNAVAILABLE = "暂无适用追问"
CHECK_PRESET_NOTE = "预置演示 · 不代表已实现通用定位"
CHECK_QUEUE_NOTE = "保存后这份作答退出待核对队列，但仍可在「查看核对记录」里按「原因待判断」找回。"
CHECK_LONG_LINES = 10
CHECK_SCOPE_NOTE = ("「当前作业」只算最近一次导入的真实批次，「全部批次」把本班每个作业的应交/已核对/待核对"
                    "并列相加，两者分母不同；具体某一批请直接选批次名，或在下面作业表点那一行。")
CHECK_CHIP_ON = ("background: #2E6BD6; color: #FFFFFF; border: 1px solid #2E6BD6;"
                 " border-radius: 9px; padding: 2px 9px;")
CHECK_CHIP_OFF = ("background: #EDF2FB; color: #1F2328; border: 1px solid #D7DEE7;"
                  " border-radius: 9px; padding: 2px 9px;")
CHECK_CHIP_NA = ("background: #EFF0F2; color: #5B6673; border: 1px solid #D7DEE7;"
                 " border-radius: 9px; padding: 2px 9px;")
CAUSE_STATE_TEXT = {"undecided": "原因待判断", "decided": "教师已判断", "not_applicable": "无需判断"}
PROGRESS_TEXT = {"pending": "待核对", "checked": "已核对"}
CHECK_FILTERS = (("undecided", "原因待判断"), ("decided", "教师已判断"),
                 ("not_applicable", "无需判断"), ("all", "全部"))
# 400 的「下一步」提示：后端原文照旧逐字显示在前面，只在后面补一句老师该怎么改；
# 复用同一个错误提示标签，不新增视觉元素。四类业务码各自的文案见桌面套件 K 组与 C/J/E 组。
CHECK_ERROR_HINTS = {
    "basis_required": "要判原因就必须填「判断依据」；确认无需判断就直接点主按钮保存。",
    "cause_not_applicable": "正确 / 缺测 / 缺少步骤 的作答只能记「无需判断」；"
                            "要留人工核对证据请用「人工核对并记录错误」。",
}


def service_call(func):
    """把一次 service 调用包成「绝不抛出」的结果字典，status/code/message 原样带回界面。

    为什么必须有这一层：桌面端没有 HTTP 层兜异常，业务错误是带状态码的 `ApiError`
    （400 basis_required / 403 ai_isolation / 409 version_conflict …）。只让任务线程转成一句
    文字会把状态码和业务码丢掉；更糟的是让异常漏出任务线程会直接把窗口打断。
    所以：ApiError 取出 status/code/message，其它异常也必须变成一句人话。
    """
    def run():
        try:
            return {"ok": True, "value": func()}
        except ApiError as exc:
            return {"ok": False, "status": exc.status, "code": exc.code or "",
                    "message": exc.message}
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都要在界面上说出来
            return {"ok": False, "status": 0, "code": "",
                    "message": "%s: %s" % (type(exc).__name__, exc)}
    return run


def check_card(title=""):
    """白底卡片：沿用 theme 的 QFrame#card，与个人学情、教师短追问同一套样式。"""
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    if title:
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
    return frame, layout


def check_text(text="", name="", wrap=True):
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def check_kv(title, value="—"):
    """一个「小标题 + 大数字」：返回值标签，方便刷新时只改数字。"""
    box = QVBoxLayout()
    box.setSpacing(2)
    key = QLabel(title)
    key.setObjectName("pageMeta")
    key.setMinimumWidth(96)
    val = QLabel(str(value))
    val.setObjectName("bigValue")
    val.setTextInteractionFlags(Qt.TextSelectableByMouse)
    box.addWidget(key)
    box.addWidget(val)
    return box, key, val


def check_step_html(text, bad, locate):
    """错步行：下划线 + 文字标记同时出现（不能只靠颜色区分）。"""
    raw = "" if text is None else str(text)
    if not bad:
        return "<span>%s</span>" % html.escape(raw)
    hint = str((locate or {}).get("hint_text") or "")
    span = (locate or {}).get("underline") or {}
    start, end = span.get("start"), span.get("end")
    if hint and isinstance(start, int) and isinstance(end, int) and raw[start:end] == hint:
        body = "%s<u>%s</u>%s" % (html.escape(raw[:start]), html.escape(hint), html.escape(raw[end:]))
    elif hint and hint in raw:
        at = raw.index(hint)
        body = "%s<u>%s</u>%s" % (html.escape(raw[:at]), html.escape(hint),
                                   html.escape(raw[at + len(hint):]))
    else:
        body = "<u>%s</u>" % html.escape(raw)
    return '<span style="color: #C0392B;">%s</span>' % body


def check_locate_notice(locate, notice):
    """定位状态说人话；服务端给了 notice 就以它为准（单一事实来源）。"""
    locate = locate or {}
    status = locate.get("status") or ""
    if status == "first_bad":
        return "", ""
    server = str(notice or "").strip()
    if server:
        return server, ("info" if status == "correct" else "warn")
    if status == "correct":
        return "全程等价：从题干到学生最后一行都没有发现不等价，不标错步。", "info"
    if status == "missing_steps":
        steps = locate.get("steps") or {}
        completion = locate.get("completion") or {}
        kind = steps.get("kind") or "empty"
        if kind == "answer_only":
            sentence = "缺少步骤：学生只写了结论行，无法定位具体错在哪一行，系统不猜。"
        elif kind in ("unparsable", "unchecked"):
            sentence = "缺少步骤：写出的过程没能解析成可核对的式子，系统不猜错在哪一行。"
        elif completion.get("state") == "unfinished":
            sentence = "解题未完成：有过程但最后一行还不是结果，系统不猜错在哪一行。"
        elif kind == "empty":
            sentence = "缺少步骤：这份的步骤栏里没有可核对的过程，系统不猜错在哪一行。"
        else:
            sentence = "缺少步骤：这份没有可核对的过程，系统不猜错哪一行。"
        manual = locate.get("manual_note") or ""
        return (sentence + (" " + manual if manual else "")), "warn"
    if status == "missing":
        note = locate.get("reason_note") or ""
        return ("缺测：这份作答没有提交可核验的步骤，系统不判对错、也不猜错因。"
                + (" " + note if note else "")), "warn"
    if status == "out_of_scope":
        manual = locate.get("manual_note") or ""
        return ("超出本轮核验范围：已转人工，原步骤照常完整显示，不标错步。"
                + (" " + manual if manual else "")), "warn"
    return "定位结果没有返回，按人工核对处理：原步骤照常显示，不标错步。", "warn"


class CheckWindow(QMainWindow):
    """一个班级的作答核对：课前总览 → 单份核对 → 核对记录。

    与网页端同一套业务口径：原因轴三态、out_of_scope 改判必须有依据、撤回 = 版本置空
    （回到待核对）、补录/校正带 expected_version、批次三种范围、没有适用追问时明说
    「暂无适用追问」。界面只经 DesktopService 取数：不写 SQL、不 import 业务模块。
    """

    closed = Signal()

    def __init__(self, service, class_row, parent=None):
        super().__init__(parent)
        self.service = service
        self.class_row = class_row
        self.class_id = class_row["id"]
        self.runner = TaskRunner()
        self._overview = {}
        self._detail = {}
        self._records = {}
        self._answer_id = None
        self._batch = ""
        self._records_batch = ""
        self._records_state = "undecided"
        self._demo_notice = ""
        self._answer_flash = ""
        self._skipped = False
        self._cause_radios = []
        self._answer_lines = []
        self._answer_bad_index = None
        self._answer_gate_bad = False
        self._answer_show_all = True
        self._receipt = {}

        self.setObjectName("checkWindow")
        self.setWindowTitle("数智伴学 · 作答核对")
        fit_to_screen(self, 1200, 820)
        self.setMinimumSize(900, 560)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(self._build_top())
        self.stack = QStackedWidget()
        self.stack.setObjectName("checkStack")
        self.stack.addWidget(self._build_overview_page())
        self.stack.addWidget(self._build_answer_page())
        self.stack.addWidget(self._build_records_page())
        self.stack.addWidget(self._build_receipt_page())
        layout.addWidget(self.stack, 1)
        self.load_overview()

    # ---------------------------------------------------------------- 界面
    def _build_top(self):
        heading = QLabel("%s · 作答核对" % (self.class_row.get("name") or "班级"))
        heading.setObjectName("checkClassName")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.top_note = check_text("", "checkTopNote")
        self.top_note.setStyleSheet("color: #5B6673;")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("checkRefresh")
        self.refresh_button.clicked.connect(self.reload)
        back = QPushButton("返回班级列表")
        back.setObjectName("checkBack")
        back.clicked.connect(self.close)
        theme.ghost(back)
        row = QHBoxLayout()
        row.addWidget(heading)
        row.addWidget(self.top_note, 1)
        row.addWidget(self.refresh_button)
        row.addWidget(back)
        return row

    def _build_overview_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        self.overview_batch_combo = QComboBox()
        self.overview_batch_combo.setObjectName("checkBatchCombo")
        self.overview_batch_combo.setMinimumWidth(240)
        self.overview_batch_combo.currentIndexChanged.connect(self._batch_changed)
        self.overview_scope = check_text("", "checkBatchScope")
        self.overview_scope.setStyleSheet("color: #5B6673;")
        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("看哪个批次："))
        batch_row.addWidget(self.overview_batch_combo)
        batch_row.addWidget(self.overview_scope, 1)

        hero, hero_layout = check_card()
        self.overview_flag = check_text("", "checkStateFlag", wrap=False)
        self.overview_flag.setStyleSheet(CHECK_CHIP_OFF + " font-weight: 600;")
        self.overview_assignment = check_text("", "checkAssignment", wrap=False)
        self.overview_assignment.setStyleSheet("color: #5B6673;")
        flag_row = QHBoxLayout()
        flag_row.addWidget(self.overview_flag)
        flag_row.addStretch(1)
        flag_row.addWidget(self.overview_assignment)
        hero_layout.addLayout(flag_row)
        self.overview_headline = QLabel("")
        self.overview_headline.setObjectName("checkHeadline")
        self.overview_headline.setWordWrap(True)
        self.overview_headline.setStyleSheet("font-size: 17px; font-weight: 600;")
        hero_layout.addWidget(self.overview_headline)
        self.overview_detail = check_text("", "checkDetail")
        self.overview_detail.setStyleSheet("color: #5B6673;")
        hero_layout.addWidget(self.overview_detail)
        self.overview_counts_row = QHBoxLayout()
        self._count_pairs = []
        for _ in range(4):
            box, key, value = check_kv("", "0")
            self.overview_counts_row.addLayout(box)
            self.overview_counts_row.addStretch(1)
            self._count_pairs.append((key, value))
        hero_layout.addLayout(self.overview_counts_row)
        self.overview_primary = QPushButton("查看待核对作答")
        self.overview_primary.setObjectName("checkOverviewPrimary")
        self.overview_primary.clicked.connect(self._overview_primary_clicked)
        theme.primary(self.overview_primary)
        self.overview_records_button = QPushButton("查看核对记录")
        self.overview_records_button.setObjectName("checkOverviewRecords")
        self.overview_records_button.clicked.connect(self.show_records)
        actions = QHBoxLayout()
        actions.addWidget(self.overview_primary)
        actions.addWidget(self.overview_records_button)
        actions.addStretch(1)
        hero_layout.addLayout(actions)

        demo_card, demo_layout = check_card("合成演示数据")
        demo_note = check_text("演示不需要选文件、配模型或等网络：载入后到下面作业表点「合成演示」那一行即可开始"
                               "（演示批次单独成一个作业，不会顶替当前的真实作业）。", "checkDemoNote")
        demo_note.setStyleSheet("color: #5B6673;")
        demo_layout.addWidget(demo_note)
        self.demo_notice = check_text("", "checkDemoNotice")
        self.demo_notice.setStyleSheet("background: #EDF2FB; border: 1px solid #D7DEE7;"
                                       " border-radius: 8px; padding: 8px;")
        self.demo_notice.hide()
        demo_layout.addWidget(self.demo_notice)
        self.demo_seed_button = QPushButton("载入合成演示数据")
        self.demo_seed_button.setObjectName("checkDemoSeed")
        self.demo_seed_button.clicked.connect(lambda: self.run_demo("seed"))
        self.demo_reset_button = QPushButton("重置合成数据")
        self.demo_reset_button.setObjectName("checkDemoReset")
        self.demo_reset_button.clicked.connect(lambda: self.run_demo("reset"))
        self.demo_feedback = check_text("", "checkDemoFeedback")
        self.demo_feedback.setStyleSheet("color: #C0392B; font-weight: 600;")
        self.demo_feedback.hide()
        demo_actions = QHBoxLayout()
        demo_actions.addWidget(self.demo_seed_button)
        demo_actions.addWidget(self.demo_reset_button)
        demo_actions_hint = check_text("只影响合成数据，真实导入的作答不受影响", "checkDemoHint", wrap=False)
        demo_actions_hint.setStyleSheet("color: #5B6673; font-size: 11px;")
        demo_actions.addWidget(demo_actions_hint)
        demo_actions.addStretch(1)
        demo_layout.addLayout(demo_actions)
        demo_layout.addWidget(self.demo_feedback)

        assignment_card, assignment_layout = check_card("作业与待核对作答数量")
        self.assignment_table = QTableWidget(0, 5)
        self.assignment_table.setObjectName("checkAssignmentTable")
        self.assignment_table.setHorizontalHeaderLabels(["作业", "应交", "已核对", "待核对", "当前"])
        assignment_header = self.assignment_table.horizontalHeader()
        assignment_header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            assignment_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.assignment_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.assignment_table.setSelectionMode(QTableWidget.NoSelection)
        self.assignment_table.setTextElideMode(Qt.ElideRight)
        self.assignment_table.verticalHeader().setVisible(False)
        self.assignment_table.setMinimumHeight(110)
        self.assignment_table.cellClicked.connect(self._assignment_clicked)
        assignment_layout.addWidget(self.assignment_table)
        self._assignment_rows = []

        secondary, secondary_layout = check_card("其他入口")
        self.overview_import_button = QPushButton("导入作答")
        self.overview_import_button.setObjectName("checkImportAnswers")
        self.overview_import_button.clicked.connect(self.open_import)
        self.overview_records_secondary = QPushButton("查看核对记录")
        self.overview_records_secondary.setObjectName("checkOverviewRecordsSide")
        self.overview_records_secondary.clicked.connect(self.show_records)
        self.overview_refresh_secondary = QPushButton("刷新本页")
        self.overview_refresh_secondary.setObjectName("checkOverviewRefresh")
        self.overview_refresh_secondary.clicked.connect(self.load_overview)
        secondary_note = check_text("以上是次级入口；本页主操作只有一个，按当前状态给出"
                                    "（导入作答、查看待核对作答或查看核对记录）。", "checkSecondaryNote")
        secondary_note.setStyleSheet("color: #5B6673; font-size: 11px;")
        side_row = QHBoxLayout()
        side_row.addWidget(self.overview_import_button)
        side_row.addWidget(self.overview_records_secondary)
        side_row.addWidget(self.overview_refresh_secondary)
        side_row.addStretch(1)
        secondary_layout.addLayout(side_row)
        secondary_layout.addWidget(secondary_note)

        scope_note = check_text(CHECK_SCOPE_NOTE, "checkScopeNote")
        scope_note.setStyleSheet("color: #5B6673; font-size: 11px;")

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        body_layout.addLayout(batch_row)
        body_layout.addWidget(scope_note)
        body_layout.addWidget(hero)
        body_layout.addWidget(demo_card)
        body_layout.addWidget(assignment_card)
        body_layout.addWidget(secondary)
        body_layout.addStretch(1)
        self.overview_scroll = QScrollArea()
        self.overview_scroll.setObjectName("checkOverviewScroll")
        self.overview_scroll.setWidgetResizable(True)
        self.overview_scroll.setMinimumSize(320, 180)
        self.overview_scroll.setWidget(body)
        page_layout.addWidget(self.overview_scroll, 1)
        return page

    def _build_answer_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        self.answer_back = QPushButton("← 返回作答核对")
        self.answer_back.setObjectName("checkAnswerBack")
        self.answer_back.clicked.connect(self.show_overview)
        theme.ghost(self.answer_back)
        self.answer_title = QLabel("")
        self.answer_title.setObjectName("checkAnswerTitle")
        self.answer_title.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.answer_ident = check_text("", "checkAnswerIdent")
        self.answer_ident.setStyleSheet("color: #5B6673;")
        self.answer_queue = check_text("", "checkAnswerQueue", wrap=False)
        self.answer_queue.setStyleSheet("color: #5B6673;")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.answer_back)
        ident_box = QVBoxLayout()
        ident_box.setSpacing(2)
        ident_box.addWidget(self.answer_title)
        ident_box.addWidget(self.answer_ident)
        head.addLayout(ident_box, 1)
        head.addWidget(self.answer_queue)
        page_layout.addLayout(head)

        self.answer_notice = check_text("", "checkAnswerNotice")
        self.answer_notice.setStyleSheet("background: #EDF2FB; border: 1px solid #D7DEE7;"
                                         " border-radius: 8px; padding: 8px;")
        self.answer_notice.hide()
        page_layout.addWidget(self.answer_notice)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(10)
        right = QVBoxLayout()
        right.setSpacing(10)
        columns.addLayout(left, 3)
        columns.addLayout(right, 2)

        # ---- 左列：题目、逐行原文、定位提示、对照、标准解、人工核对入口、补录、追问
        question_card, question_layout = check_card("题目")
        self.question_text = check_text("", "checkQuestionText")
        self.question_meta = check_text("", "checkQuestionMeta")
        self.question_meta.setStyleSheet("color: #5B6673;")
        self.question_missing = check_text("", "checkQuestionMissing")
        self.question_missing.setStyleSheet("color: #A9630C; font-weight: 600;")
        self.question_missing.hide()
        question_layout.addWidget(self.question_text)
        question_layout.addWidget(self.question_meta)
        question_layout.addWidget(self.question_missing)
        left.addWidget(question_card)

        steps_card, steps_layout = check_card("学生原文（逐行）")
        self.steps_box = QWidget()
        self.steps_lines = QVBoxLayout(self.steps_box)
        self.steps_lines.setContentsMargins(0, 0, 0, 0)
        self.steps_lines.setSpacing(4)
        self.steps_empty = check_text("这份作答没有正文步骤。", "checkStepsEmpty")
        self.steps_empty.setStyleSheet("color: #5B6673;")
        self.steps_more_button = QPushButton("")
        self.steps_more_button.setObjectName("checkStepsMore")
        self.steps_more_button.clicked.connect(self._toggle_steps)
        self.steps_more_button.hide()
        self.locate_notice = check_text("", "checkLocateNotice")
        self.locate_notice.hide()
        self.contrast_box = QWidget()
        contrast_layout = QHBoxLayout(self.contrast_box)
        contrast_layout.setContentsMargins(0, 0, 0, 0)
        left_kv, _, self.contrast_expected = check_kv("按规则应为", "—")
        right_kv, _, self.contrast_actual = check_kv("学生这一行", "—")
        contrast_layout.addLayout(left_kv, 1)
        contrast_layout.addLayout(right_kv, 1)
        self.contrast_note = check_text("", "checkContrastNote")
        self.contrast_note.setStyleSheet("color: #5B6673;")
        self.contrast_box.hide()
        self.preset_note = check_text(CHECK_PRESET_NOTE, "checkPresetNote")
        self.preset_note.setStyleSheet("color: #A9630C; font-weight: 600;")
        self.preset_note.hide()
        self.std_toggle = QPushButton("标准解（默认折叠，点开查看）")
        self.std_toggle.setObjectName("checkStdToggle")
        self.std_toggle.setCheckable(True)
        self.std_toggle.toggled.connect(self._toggle_std)
        self.std_list = check_text("", "checkStdSolution")
        self.std_list.setTextFormat(Qt.RichText)
        self.std_list.hide()
        steps_layout.addWidget(self.steps_box)
        steps_layout.addWidget(self.steps_empty)
        steps_layout.addWidget(self.steps_more_button)
        steps_layout.addWidget(self.locate_notice)
        steps_layout.addWidget(self.contrast_box)
        steps_layout.addWidget(self.contrast_note)
        steps_layout.addWidget(self.preset_note)
        steps_layout.addWidget(self.std_toggle)
        steps_layout.addWidget(self.std_list)
        left.addWidget(steps_card)

        self.manual_card, manual_card_layout = check_card("规则判不了这份作答")
        manual_card_note = check_text("可以选择「人工核对并记录错误」：在右列写下你观察到的错误步骤、原因与判断依据，"
                                      "保存后这份不再挂在「原因待判断」上（依据为空会被服务端拒绝并提示）。")
        manual_card_note.setStyleSheet("color: #5B6673;")
        self.manual_focus_button = QPushButton("人工核对并记录错误")
        self.manual_focus_button.setObjectName("checkManualEntry")
        self.manual_focus_button.clicked.connect(self._focus_manual)
        manual_card_layout.addWidget(manual_card_note)
        manual_card_layout.addWidget(self.manual_focus_button)
        self.manual_card.hide()
        left.addWidget(self.manual_card)

        content_card, content_layout = check_card("补录 / 校正原始作答")
        content_note = check_text("缺步骤、步骤写错、学生后来又补了过程——都在这里改，不用重新导入整批。"
                                  "保存后生成新版本：旧结论作废、本份回到待核对，并按新内容重新分析。")
        content_note.setStyleSheet("color: #5B6673;")
        self.content_steps = QPlainTextEdit()
        self.content_steps.setObjectName("checkContentSteps")
        self.content_steps.setPlaceholderText("逐行照抄学生原文，一行一步；补上缺的中间步骤后保存即可")
        self.content_steps.setFixedHeight(96)
        self.content_missing = QCheckBox("标为缺测（这份没有学生步骤）")
        self.content_missing.setObjectName("checkContentMissing")
        self.content_missing.toggled.connect(self._content_missing_changed)
        self.content_reason = QLineEdit()
        self.content_reason.setObjectName("checkContentReason")
        self.content_reason.setPlaceholderText("缺测原因（标为缺测时填，例如「未提供学生步骤」）")
        self.content_button = QPushButton("保存补录并重新分析")
        self.content_button.setObjectName("checkContentSave")
        self.content_button.clicked.connect(self._submit_content)
        self.content_error = check_text("", "checkContentError")
        self.content_error.setStyleSheet("color: #C0392B; font-weight: 600;")
        self.content_error.hide()
        self.content_toggle = QPushButton("点开：改学生步骤或标为缺测")
        self.content_toggle.setObjectName("checkContentToggle")
        self.content_toggle.setCheckable(True)
        self.content_toggle.toggled.connect(self._toggle_content)
        self.content_body = QWidget()
        content_body_layout = QVBoxLayout(self.content_body)
        content_body_layout.setContentsMargins(0, 0, 0, 0)
        content_body_layout.addWidget(QLabel("学生步骤（逐行原文）"))
        content_body_layout.addWidget(self.content_steps)
        content_body_layout.addWidget(self.content_missing)
        content_body_layout.addWidget(QLabel("缺测原因"))
        content_body_layout.addWidget(self.content_reason)
        content_body_layout.addWidget(self.content_button)
        content_body_layout.addWidget(self.content_error)
        self.content_body.hide()
        content_layout.addWidget(content_note)
        content_layout.addWidget(self.content_toggle)
        content_layout.addWidget(self.content_body)
        left.addWidget(content_card)

        ask_card, ask_layout = check_card("当面追问")
        self.ask_line = check_text("", "checkAskLine")
        self.ask_line.setStyleSheet("font-weight: 600;")
        self.ask_note = check_text("", "checkAskNote")
        self.ask_note.setStyleSheet("color: #5B6673; font-size: 11px;")
        self.ask_input = QLineEdit()
        self.ask_input.setObjectName("checkAskInput")
        self.ask_input.setPlaceholderText("要问的那一句（可改；改了就算教师自拟）")
        self.followup_review = check_text("", "checkFollowupReview")
        self.followup_review.setStyleSheet("background: #FFF6E5; border: 1px solid #E8D2A8;"
                                           " border-radius: 8px; padding: 8px;")
        self.followup_review.hide()
        self.followup_recorded = check_text("", "checkFollowupRecorded")
        self.followup_recorded.setStyleSheet("background: #F7F9FC; border: 1px solid #D7DEE7;"
                                             " border-radius: 8px; padding: 8px;")
        self.followup_recorded.hide()
        self.followup_history = check_text("", "checkFollowupHistory")
        self.followup_history.setStyleSheet("color: #5B6673; font-size: 11px;")
        self.followup_history.hide()
        self.answer_input = QPlainTextEdit()
        self.answer_input.setObjectName("checkStudentAnswer")
        self.answer_input.setPlaceholderText("逐字记下学生原话，例如：2 只要乘 y 就好了。")
        self.answer_input.setFixedHeight(64)
        self.answer_record_button = QPushButton("记录学生回答")
        self.answer_record_button.setObjectName("checkRecordAnswer")
        self.answer_record_button.clicked.connect(self._record_answer)
        self.answer_skip_button = QPushButton("暂时跳过")
        self.answer_skip_button.setObjectName("checkSkipAnswer")
        self.answer_skip_button.clicked.connect(self._skip_answer)
        theme.ghost(self.answer_skip_button)
        self.skip_line = check_text("", "checkSkipLine")
        self.skip_line.setStyleSheet("color: #5B6673; font-size: 11px;")
        self.skip_line.hide()
        ask_hint = check_text("跳过不等于学生拒答；没有新增依据时，原因仍然待判断。", "checkAskHint")
        ask_hint.setStyleSheet("color: #5B6673; font-size: 11px;")
        ask_layout.addWidget(self.ask_line)
        ask_layout.addWidget(self.ask_note)
        ask_layout.addWidget(QLabel("要问的那一句"))
        ask_layout.addWidget(self.ask_input)
        ask_layout.addWidget(self.followup_review)
        ask_layout.addWidget(self.followup_recorded)
        ask_layout.addWidget(self.followup_history)
        ask_layout.addWidget(QLabel("学生原话（逐字记录，跟随本页保存）"))
        ask_layout.addWidget(self.answer_input)
        ask_row = QHBoxLayout()
        ask_row.addWidget(self.answer_record_button)
        ask_row.addWidget(self.answer_skip_button)
        ask_row.addStretch(1)
        ask_layout.addLayout(ask_row)
        ask_layout.addWidget(self.skip_line)
        ask_layout.addWidget(ask_hint)
        left.addWidget(ask_card)
        left.addStretch(1)

        # ---- 右列：观察到的错步、可能原因（含人工填写）、教师判断
        observed_card, observed_layout = check_card("观察到的错步")
        self.observed_input = QLineEdit()
        self.observed_input.setObjectName("checkObservedInput")
        self.observed_input.setPlaceholderText("学生原文里的这一处（老师可改）")
        self.observed_hint = check_text("", "checkObservedHint")
        self.observed_hint.setStyleSheet("color: #5B6673; font-size: 11px;")
        observed_layout.addWidget(QLabel("学生原文里的这一处（老师可改）"))
        observed_layout.addWidget(self.observed_input)
        observed_layout.addWidget(self.observed_hint)
        right.addWidget(observed_card)

        cause_card, cause_layout = check_card("可能原因")
        self.cause_box = QWidget()
        self.cause_rows = QVBoxLayout(self.cause_box)
        self.cause_rows.setContentsMargins(0, 0, 0, 0)
        self.cause_rows.setSpacing(6)
        self.causes_empty = check_text("", "checkCausesEmpty")
        self.causes_empty.setStyleSheet("color: #5B6673;")
        self.adopt_button = QPushButton("采纳选中原因并保存")
        self.adopt_button.setObjectName("checkAdopt")
        self.adopt_button.clicked.connect(lambda: self._save(adopt=True))
        adopt_hint = check_text("选中上面一条候选后，可直接采纳并保存（原因记为「教师已判断」）；"
                                "不采纳就点底部「保存错步，原因待判断」。", "checkAdoptHint")
        adopt_hint.setStyleSheet("color: #5B6673; font-size: 11px;")
        self.adopt_row = QWidget()
        adopt_layout = QVBoxLayout(self.adopt_row)
        adopt_layout.setContentsMargins(0, 0, 0, 0)
        adopt_layout.addWidget(self.adopt_button)
        adopt_layout.addWidget(adopt_hint)
        self.manual_code = QLineEdit()
        self.manual_code.setObjectName("checkManualCode")
        self.manual_code.setPlaceholderText("原因编码（可留空）")
        self.manual_label = QLineEdit()
        self.manual_label.setObjectName("checkManualLabel")
        self.manual_label.setPlaceholderText("原因说明（必填，例如「移项没变号」）")
        self.manual_label.textChanged.connect(self._sync_answer_buttons)
        self.manual_button = QPushButton("保存人工判断")
        self.manual_button.setObjectName("checkManualSave")
        self.manual_button.clicked.connect(lambda: self._save(action="manual"))
        self.manual_box = QWidget()
        manual_layout = QVBoxLayout(self.manual_box)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(check_text("没有合适候选？人工填写原因（来源记为「教师填写」）",
                                           "checkManualTitle"))
        manual_layout.addWidget(self.manual_code)
        manual_layout.addWidget(self.manual_label)
        manual_layout.addWidget(self.manual_button)
        self.causes_note = check_text("", "checkCausesNote")
        self.causes_note.setStyleSheet("color: #5B6673; font-size: 11px;")
        cause_layout.addWidget(self.cause_box)
        cause_layout.addWidget(self.causes_empty)
        cause_layout.addWidget(self.adopt_row)
        cause_layout.addWidget(self.manual_box)
        cause_layout.addWidget(self.causes_note)
        right.addWidget(cause_card)

        judge_card, judge_layout = check_card("教师判断")
        self.judge_stale = check_text("作答已更新，旧判断不再作为当前结论。", "checkJudgeStale")
        self.judge_stale.setStyleSheet("color: #A9630C; font-weight: 600;")
        self.judge_stale.hide()
        self.judge_retracted = check_text("", "checkJudgeRetracted")
        self.judge_retracted.setStyleSheet("color: #5B6673;")
        self.judge_retracted.hide()
        self.judge_current = check_text("", "checkJudgeCurrent")
        self.judge_current.hide()
        self.judge_source = check_text("", "checkJudgeSource")
        self.judge_source.setStyleSheet("color: #5B6673; font-size: 11px;")
        self.judge_source.hide()
        self.reset_cause_button = QPushButton("改为原因待判断")
        self.reset_cause_button.setObjectName("checkCauseUndecided")
        self.reset_cause_button.clicked.connect(lambda: self._save(action="undecided"))
        self.reset_cause_button.hide()
        self.basis_caption = QLabel("判断依据（可选，与备注分开记录）")
        self.basis_caption.setObjectName("checkBasisCaption")
        self.basis_input = QPlainTextEdit()
        self.basis_input.setObjectName("checkBasis")
        self.basis_input.setFixedHeight(70)
        self.basis_hint = check_text("", "checkBasisHint")
        self.basis_hint.setStyleSheet("color: #A9630C; font-size: 11px;")
        self.basis_hint.hide()
        self.note_input = QPlainTextEdit()
        self.note_input.setObjectName("checkNote")
        self.note_input.setFixedHeight(56)
        self.note_input.setPlaceholderText("可选：记一句备注，例如「让他重做一道 5(y-3)」")
        self.retract_button = QPushButton("撤回本次结论（回到待核对）")
        self.retract_button.setObjectName("checkRetract")
        self.retract_button.clicked.connect(lambda: self._save(action="retract"))
        self.retract_hint = check_text("撤回不会删除记录：旧结论进历史，本份回到「待核对」，可以重新核对。",
                                       "checkRetractHint")
        self.retract_hint.setStyleSheet("color: #5B6673; font-size: 11px;")
        queue_note = check_text(CHECK_QUEUE_NOTE, "checkQueueNote")
        queue_note.setStyleSheet("color: #5B6673; font-size: 11px;")
        judge_layout.addWidget(self.judge_stale)
        judge_layout.addWidget(self.judge_retracted)
        judge_layout.addWidget(self.judge_current)
        judge_layout.addWidget(self.judge_source)
        judge_layout.addWidget(self.reset_cause_button)
        judge_layout.addWidget(self.basis_caption)
        judge_layout.addWidget(self.basis_input)
        judge_layout.addWidget(self.basis_hint)
        judge_layout.addWidget(QLabel("教师备注（可选，随这次保存一起记录）"))
        judge_layout.addWidget(self.note_input)
        judge_layout.addWidget(self.retract_button)
        judge_layout.addWidget(self.retract_hint)
        judge_layout.addWidget(queue_note)
        right.addWidget(judge_card)
        right.addStretch(1)

        scroll_body = QWidget()
        scroll_body.setLayout(columns)
        self.answer_scroll = QScrollArea()
        self.answer_scroll.setObjectName("checkAnswerScroll")
        self.answer_scroll.setWidgetResizable(True)
        self.answer_scroll.setMinimumSize(320, 180)
        self.answer_scroll.setWidget(scroll_body)
        page_layout.addWidget(self.answer_scroll, 1)

        bar = QFrame()
        bar.setObjectName("checkBar")
        bar.setStyleSheet("QFrame#checkBar { background: #FFFFFF; border: 1px solid #D7DEE7;"
                          " border-radius: 10px; }")
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(12, 10, 12, 10)
        bar_layout.setSpacing(6)
        axis_row = QHBoxLayout()
        self.axis_progress = QLabel("待核对")
        self.axis_cause = QLabel("原因待判断")
        for chip in (self.axis_progress, self.axis_cause):
            chip.setStyleSheet(CHECK_CHIP_OFF)
        self.axis_hint = check_text("", "checkAxisHint")
        self.axis_hint.setStyleSheet("color: #5B6673; font-size: 11px;")
        axis_row.addWidget(self.axis_progress)
        axis_row.addWidget(QLabel(" · "))
        axis_row.addWidget(self.axis_cause)
        axis_row.addWidget(self.axis_hint, 1)
        bar_layout.addLayout(axis_row)
        self.answer_error = check_text("", "checkSaveError")
        self.answer_error.setStyleSheet("color: #C0392B; font-weight: 600;")
        self.answer_error.hide()
        bar_layout.addWidget(self.answer_error)
        self.save_button = QPushButton("保存错步，原因待判断")
        self.save_button.setObjectName("checkSave")
        self.save_button.clicked.connect(lambda: self._save())
        theme.primary(self.save_button)
        self.reload_answer_button = QPushButton("刷新这份作答（会清空本页输入）")
        self.reload_answer_button.setObjectName("checkReloadAnswer")
        self.reload_answer_button.clicked.connect(self._refresh_answer)
        self.reload_answer_button.hide()
        bar_actions = QHBoxLayout()
        bar_actions.addWidget(self.save_button)
        bar_actions.addWidget(self.reload_answer_button)
        bar_actions.addStretch(1)
        bar_layout.addLayout(bar_actions)
        page_layout.addWidget(bar)
        return page

    def _build_records_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)
        top = QHBoxLayout()
        back = QPushButton("← 返回作答核对")
        back.setObjectName("checkRecordsBack")
        back.clicked.connect(self.show_overview)
        theme.ghost(back)
        self.records_title = QLabel("核对记录")
        self.records_title.setObjectName("checkRecordsTitle")
        self.records_title.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.records_primary = QPushButton("查看待核对作答")
        self.records_primary.setObjectName("checkRecordsPrimary")
        self.records_primary.clicked.connect(self._open_next)
        theme.primary(self.records_primary)
        top.addWidget(back)
        top.addWidget(self.records_title, 1)
        top.addWidget(self.records_primary)

        self.records_batch_combo = QComboBox()
        self.records_batch_combo.setObjectName("checkRecordsBatch")
        self.records_batch_combo.setMinimumWidth(240)
        self.records_batch_combo.currentIndexChanged.connect(self._records_batch_changed)
        self.records_scope = check_text("", "checkRecordsScope")
        self.records_scope.setStyleSheet("color: #5B6673;")
        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("看哪个批次："))
        batch_row.addWidget(self.records_batch_combo)
        batch_row.addWidget(self.records_scope, 1)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("筛选："))
        self._filter_buttons = {}
        for key, label in CHECK_FILTERS:
            button = QPushButton(label)
            button.setObjectName("checkFilter-%s" % key)
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, name=key: self._records_filter_changed(name))
            filter_row.addWidget(button)
            self._filter_buttons[key] = button
        filter_row.addStretch(1)
        self.records_note = check_text("只列当前版本的有效记录，按保存时间倒序。改判后的旧原因不再作为当前结论，"
                                       "但仍保留在历史里。", "checkRecordsNote")
        self.records_note.setStyleSheet("color: #5B6673; font-size: 11px;")

        self.records_table = QTableWidget(0, 7)
        self.records_table.setObjectName("checkRecordsTable")
        self.records_table.setHorizontalHeaderLabels(
            ["学生", "题号", "作业批次", "观察到的错步", "处理进度", "原因判断", "保存时间"])
        records_header = self.records_table.horizontalHeader()
        for column in (0, 1, 2):
            records_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        records_header.setSectionResizeMode(3, QHeaderView.Stretch)
        for column in (4, 5, 6):
            records_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.records_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.records_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.records_table.setSelectionMode(QTableWidget.SingleSelection)
        self.records_table.setTextElideMode(Qt.ElideRight)
        self.records_table.verticalHeader().setVisible(False)
        self.records_table.itemDoubleClicked.connect(lambda _item: self._open_selected_record())
        self.records_open_button = QPushButton("打开选中的记录")
        self.records_open_button.setObjectName("checkRecordsOpen")
        self.records_open_button.clicked.connect(self._open_selected_record)
        self.records_status = check_text("", "checkRecordsStatus")
        self.records_status.setStyleSheet("color: #5B6673;")
        open_row = QHBoxLayout()
        open_row.addWidget(self.records_open_button)
        open_row.addWidget(self.records_status, 1)
        page_layout.addLayout(top)
        page_layout.addLayout(batch_row)
        page_layout.addWidget(self.records_note)
        page_layout.addLayout(filter_row)
        page_layout.addWidget(self.records_table, 1)
        page_layout.addLayout(open_row)
        return page

    def _build_receipt_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        hero, hero_layout = check_card()
        self.receipt_title = QLabel("已保存")
        self.receipt_title.setObjectName("checkReceiptTitle")
        self.receipt_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.receipt_dest = check_text("", "checkReceiptDest")
        self.receipt_ident = check_text("", "checkReceiptIdent")
        self.receipt_ident.setStyleSheet("color: #5B6673;")
        self.receipt_status = check_text("", "checkReceiptStatus")
        self.receipt_saved_at = check_text("", "checkReceiptSavedAt")
        self.receipt_saved_at.setStyleSheet("color: #5B6673;")
        receipt_axis = QHBoxLayout()
        self.receipt_progress_chip = QLabel("待核对")
        self.receipt_cause_chip = QLabel("原因待判断")
        for chip in (self.receipt_progress_chip, self.receipt_cause_chip):
            chip.setStyleSheet(CHECK_CHIP_OFF)
        receipt_axis.addWidget(self.receipt_progress_chip)
        receipt_axis.addWidget(QLabel(" · "))
        receipt_axis.addWidget(self.receipt_cause_chip)
        receipt_axis.addStretch(1)
        hero_layout.addWidget(self.receipt_title)
        hero_layout.addWidget(self.receipt_dest)
        hero_layout.addWidget(self.receipt_ident)
        hero_layout.addWidget(self.receipt_status)
        hero_layout.addLayout(receipt_axis)
        hero_layout.addWidget(self.receipt_saved_at)

        detail_card, detail_layout = check_card("这次判断根据什么")
        self.receipt_observed = check_text("", "checkReceiptObserved")
        self.receipt_verify = check_text("", "checkReceiptVerify")
        self.receipt_quote = check_text("", "checkReceiptQuote")
        self.receipt_review = check_text("", "checkReceiptReview")
        self.receipt_review.setStyleSheet("background: #FFF6E5; border: 1px solid #E8D2A8;"
                                          " border-radius: 8px; padding: 8px;")
        self.receipt_review.hide()
        self.receipt_cause_text = check_text("", "checkReceiptCause")
        self.receipt_source_text = check_text("", "checkReceiptSource")
        self.receipt_source_text.setStyleSheet("color: #5B6673; font-size: 11px;")
        self.receipt_basis_text = check_text("", "checkReceiptBasis")
        self.receipt_note_text = check_text("", "checkReceiptNote")
        detail_layout.addWidget(self.receipt_observed)
        detail_layout.addWidget(self.receipt_verify)
        detail_layout.addWidget(self.receipt_quote)
        detail_layout.addWidget(self.receipt_review)
        detail_layout.addWidget(self.receipt_cause_text)
        detail_layout.addWidget(self.receipt_source_text)
        detail_layout.addWidget(self.receipt_basis_text)
        detail_layout.addWidget(self.receipt_note_text)

        self.receipt_banner = check_text("", "checkReceiptBanner")
        self.receipt_banner.setStyleSheet("background: #EDF2FB; border: 1px solid #D7DEE7;"
                                          " border-radius: 8px; padding: 8px;")
        body_layout.addWidget(hero)
        body_layout.addWidget(detail_card)
        body_layout.addWidget(self.receipt_banner)
        body_layout.addStretch(1)
        self.receipt_scroll = QScrollArea()
        self.receipt_scroll.setObjectName("checkReceiptScroll")
        self.receipt_scroll.setWidgetResizable(True)
        self.receipt_scroll.setMinimumSize(320, 180)
        self.receipt_scroll.setWidget(body)
        page_layout.addWidget(self.receipt_scroll, 1)

        bar = QFrame()
        bar.setObjectName("checkReceiptBar")
        bar.setStyleSheet("QFrame#checkReceiptBar { background: #FFFFFF; border: 1px solid #D7DEE7;"
                          " border-radius: 10px; }")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 10, 12, 10)
        self.receipt_primary = QPushButton("查看下一份")
        self.receipt_primary.setObjectName("checkReceiptNext")
        self.receipt_primary.clicked.connect(self._receipt_primary_clicked)
        theme.primary(self.receipt_primary)
        self.receipt_records_button = QPushButton("查看核对记录")
        self.receipt_records_button.setObjectName("checkReceiptRecords")
        self.receipt_records_button.clicked.connect(self.show_records)
        bar_layout.addWidget(self.receipt_primary)
        bar_layout.addWidget(self.receipt_records_button)
        bar_layout.addStretch(1)
        page_layout.addWidget(bar)
        return page

    # ---------------------------------------------------------------- 数据
    @staticmethod
    def _call(func):
        """见模块级 service_call：把 ApiError 的 status/code/message 一并带回界面。"""
        return service_call(func)

    def reload(self):
        if self.runner.busy:
            QTimer.singleShot(150, self.reload)
            return
        index = self.stack.currentIndex()
        if index == 1 and self._answer_id:
            self.open_answer(self._answer_id)
        elif index == 2:
            self.load_records()
        else:
            self.load_overview()

    def load_overview(self):
        self.stack.setCurrentIndex(0)
        if self.runner.busy:
            QTimer.singleShot(150, self.load_overview)
            return
        self.overview_primary.setEnabled(False)
        # 「全部批次」不向后端要一个不存在的批次：照常取当前作业 + 各批次清单，
        # 由页面把各批次并列相加（每份作答仍只属于一个批次），与网页端同一做法。
        batch = "" if self._batch in ("", CHECK_ALL_BATCHES) else self._batch
        self.runner.run(CheckWindow._call(lambda: self.service.check_overview(self.class_id, batch)),
                        self._overview_done, self._overview_failed)

    def _overview_done(self, outcome):
        self.overview_primary.setEnabled(True)
        if not outcome.get("ok"):
            self._overview_failed(outcome)
            return
        self._overview = dict(outcome.get("value") or {})
        self._render_overview()

    def _overview_failed(self, outcome):
        if isinstance(outcome, dict):
            message = outcome.get("message") or "请稍后重试。"
        else:
            message = str(outcome)
        self.overview_primary.setEnabled(True)
        self.overview_headline.setText("没有读到作答核对总览")
        self.overview_detail.setText("%s" % message)
        self.overview_flag.hide()
        self.overview_assignment.setText("")
        self._fill_counts([("应交", "—"), ("已核对", "—"), ("待核对作答", "—"), ("缺测", "—")])
        self.assignment_table.setRowCount(0)
        self._assignment_rows = []

    def open_answer(self, answer_id):
        if not answer_id:
            return
        if self.runner.busy:
            QTimer.singleShot(150, lambda: self.open_answer(answer_id))
            return
        self._answer_id = int(answer_id)
        self.stack.setCurrentIndex(1)
        self.answer_error.hide()
        self.answer_error.setText("")
        self.runner.run(
            CheckWindow._call(lambda: self.service.check_answer(self.class_id, self._answer_id)),
            self._answer_done, self._answer_failed)

    def _answer_done(self, outcome):
        if not outcome.get("ok"):
            self._answer_failed(outcome)
            return
        self._show_answer(dict(outcome.get("value") or {}))

    def _answer_failed(self, outcome):
        message = (outcome.get("message") if isinstance(outcome, dict) else str(outcome)) or "请稍后重试。"
        self.answer_error.setText("没有读到这份作答：%s" % message)
        self.answer_error.show()

    def load_records(self):
        self.stack.setCurrentIndex(2)
        if self.runner.busy:
            QTimer.singleShot(150, self.load_records)
            return
        self.records_status.setText("正在加载核对记录…")
        self.runner.run(
            CheckWindow._call(lambda: self.service.check_records(
                self.class_id, self._records_batch, self._records_state, "")),
            self._records_done, self._records_failed)

    def _records_done(self, outcome):
        if not outcome.get("ok"):
            self._records_failed(outcome)
            return
        self._records = dict(outcome.get("value") or {})
        self._render_records()

    def _records_failed(self, outcome):
        message = (outcome.get("message") if isinstance(outcome, dict) else str(outcome)) or "请稍后重试。"
        self.records_status.setText("没有读到核对记录：%s" % message)
        self.records_table.setRowCount(0)

    # ---------------------------------------------------------------- 渲染
    def _refresh_layout(self):
        """文字是渲染时写入的：显式激活一遍布局（含嵌套卡片），

        否则标签会按「写入文字之前」的空尺寸被压成一条（实测总览的计数与状态标签被压成 6px 宽）。
        """
        for _pass in range(2):
            for layout in self.findChildren(QLayout):
                layout.activate()

    def _fill_counts(self, pairs, visible=True):
        """刷新「小标题 + 大数字」四格：范围不同（当前作业 / 全部批次）标题也不同。"""
        for index, (key, value) in enumerate(self._count_pairs):
            if visible and index < len(pairs):
                title, text = pairs[index]
                key.setText(title)
                value.setText(str(text))
                key.setVisible(True)
                value.setVisible(True)
            else:
                key.setVisible(False)
                value.setVisible(False)

    def _render_overview(self):
        data = self._overview
        assignments = list(data.get("assignments") or [])
        state = data.get("state") or "no_answers"
        assignment = dict(data.get("assignment") or {})
        next_item = dict(data.get("next") or {})
        view_all = self._batch == CHECK_ALL_BATCHES
        assignment_name = assignment.get("name") or assignment.get("batch") or ""
        if not assignment_name and self._batch and not view_all:
            assignment_name = self._batch
        show_assignment = state != "no_answers" and assignment_name and assignment_name != "未命名批次"
        total = int(assignment.get("total") or 0)
        pending = int(assignment.get("pending") or 0)
        checked = int(assignment.get("checked") or 0)
        missing = int(assignment.get("missing") or 0)
        all_total = sum(int(item.get("total") or 0) for item in assignments)
        all_checked = sum(int(item.get("checked") or 0) for item in assignments)
        all_pending = sum(int(item.get("pending") or 0) for item in assignments)
        next_id = next_item.get("answer_id")

        self._fill_overview_batches(assignments)
        if view_all:
            self.overview_scope.setText("当前范围：全部批次（%d 个作业，各批次并列相加；"
                                        "每份作答仍属于某个批次）" % len(assignments))
        elif self._batch:
            self.overview_scope.setText("当前范围：只看「%s」这一批" % assignment_name)
        else:
            self.overview_scope.setText("当前范围：当前作业（最近一次导入的真实批次）；"
                                        "要并看所有批次请选「全部批次」")

        if state == "has_pending":
            self.overview_flag.setText("待核对作答 %d 份" % pending)
            self.overview_flag.setStyleSheet(CHECK_CHIP_ON + " font-weight: 600;")
            self.overview_flag.show()
        elif state == "all_checked":
            self.overview_flag.setText("全部已核对")
            self.overview_flag.setStyleSheet(CHECK_CHIP_NA + " font-weight: 600;")
            self.overview_flag.show()
        else:
            self.overview_flag.hide()
        self.overview_assignment.setText(
            ("当前作业：%s" % assignment_name) if show_assignment else "")

        if view_all:
            headline = "全部批次：%d 个作业、应交 %d 份" % (len(assignments), all_total)
        elif state == "no_answers":
            headline = "尚未导入作答"
        elif state == "all_checked":
            headline = "本次作业已核对完"
        else:
            headline = next_item.get("headline") or "这批作答里，哪些可以判、哪些还需要问一句？"
        self.overview_headline.setText(headline)

        if view_all:
            detail = ("这是把各批次并列相加的结果：已核对 %d 份、待核对 %d 份。%s"
                      "要看某一批的点下面作业表那一行；「当前作业」范围请在下拉里切回去。"
                      % (all_checked, all_pending,
                         "当前没有待核对作答。" if all_pending == 0 else ""))
        elif state == "no_answers":
            detail = "可以导入本班作答，或用下面的合成演示数据先走一遍。"
        elif state == "all_checked":
            detail = ("本次作业共 %d 份，已核对 %d 份；本批次暂无待核对作答；"
                      "原因待判断的记录可以在「查看核对记录」里找回。" % (total, checked))
        else:
            detail = ("下一份待核对作答：%s%s。待核对数与「查看待核对作答」用同一个队列。"
                      % (next_item.get("student_label") or "—",
                         (" · 第 %s 题" % next_item.get("question_no"))
                         if next_item.get("question_no") else ""))
        self.overview_detail.setText(detail)

        if view_all:
            self._fill_counts([("作业数", len(assignments)), ("应交（各批合计）", all_total),
                               ("已核对（合计）", all_checked), ("待核对作答（合计）", all_pending)])
        elif state == "no_answers":
            self._fill_counts([], visible=False)
        else:
            self._fill_counts([("应交", total), ("已核对", checked),
                               ("待核对作答", pending), ("缺测", missing)])

        if view_all:
            self.overview_primary.setText("查看核对记录")
        elif state == "no_answers":
            self.overview_primary.setText("导入作答")
        elif next_id:
            self.overview_primary.setText("查看待核对作答")
        else:
            self.overview_primary.setText("查看核对记录")
        self.overview_records_button.setVisible(not (view_all or state == "all_checked"))
        self._render_assignments(assignments, assignment, view_all)
        if self._demo_notice:
            self.demo_notice.setText(self._demo_notice)
            self.demo_notice.show()
            self._demo_notice = ""
        else:
            self.demo_notice.hide()
        self._refresh_layout()

    def _fill_overview_batches(self, assignments):
        current = self._batch
        self.overview_batch_combo.blockSignals(True)
        self.overview_batch_combo.clear()
        self.overview_batch_combo.addItem("当前作业（最近一次导入的真实批次）", "")
        self.overview_batch_combo.addItem("全部批次（各批次并列相加）", CHECK_ALL_BATCHES)
        for item in assignments:
            label = item.get("name") or item.get("batch") or "（未命名作业）"
            if item.get("synthetic"):
                label += "（演示）"
            self.overview_batch_combo.addItem(label, item.get("batch") or "")
        index = self.overview_batch_combo.findData(current)
        self.overview_batch_combo.setCurrentIndex(index if index >= 0 else 0)
        self.overview_batch_combo.blockSignals(False)

    def _render_assignments(self, assignments, assignment, view_all):
        self._assignment_rows = assignments
        self.assignment_table.setRowCount(len(assignments))
        current_batch = "" if view_all else (assignment.get("batch") or self._batch or "")
        for row, item in enumerate(assignments):
            name = item.get("name") or item.get("batch") or "（未命名作业）"
            if item.get("synthetic"):
                name += "（演示）"
            values = [name, str(int(item.get("total") or 0)), str(int(item.get("checked") or 0)),
                      str(int(item.get("pending") or 0)),
                      "当前" if (item.get("batch") or "") == current_batch else ""]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.assignment_table.setItem(row, column, cell)

    def _show_answer(self, data):
        self._detail = data
        answer = dict(data.get("answer") or {})
        locate = dict(data.get("locate") or {})
        judgement = dict(data.get("judgement") or {})
        followup = dict(data.get("followup") or {})
        queue = dict(data.get("queue") or {})
        candidates = list(data.get("candidates") or [])
        applicability = data.get("applicability") or ""
        batch = answer.get("batch") or queue.get("batch") or ""
        self._answer_gate_bad = (locate.get("status") or "") == "first_bad"
        try:
            bad_index = int(locate.get("first_bad_index"))
        except (TypeError, ValueError):
            bad_index = None
        self._answer_bad_index = bad_index

        self.answer_title.setText("%s · 第 %s 题" % (answer.get("student_label") or "学生",
                                                    answer.get("question_no") or "—"))
        ident = ["记录编号 %s" % (answer.get("record_key") or ("#%s" % (answer.get("id") or self._answer_id)))]
        ident.append(answer.get("data_source_label") or "")
        ident.append("作业批次：%s" % (batch or "—"))
        if answer.get("occurred_on"):
            ident.append("作答日期 %s" % answer["occurred_on"])
        self.answer_ident.setText(" · ".join([part for part in ident if part]))
        if queue.get("position") or queue.get("total"):
            self.answer_queue.setText("队列第 %d / %d 份%s" % (
                int(queue.get("position") or 0), int(queue.get("total") or 0),
                "（最后一份）" if queue.get("is_last") else ""))
        else:
            self.answer_queue.setText("")

        if self._answer_flash:
            self.answer_notice.setText(self._answer_flash)
            self.answer_notice.show()
            self._answer_flash = ""
        else:
            self.answer_notice.hide()

        self.question_text.setText(answer.get("question_text") or "（这份作答没有题目文本）")
        self.question_meta.setText(" · ".join([part for part in (
            answer.get("unit") or "", batch or "",
            ("作答日期 %s" % answer["occurred_on"]) if answer.get("occurred_on") else "") if part]))
        if int(answer.get("missing") or 0):
            self.question_missing.setText("缺测：%s" % (answer.get("missing_reason")
                                                       or "这份作答没有可核验的步骤。"))
            self.question_missing.show()
        else:
            self.question_missing.hide()

        lines = list(locate.get("lines") or [])
        if not lines:
            raw = [text.strip() for text in str(answer.get("student_steps") or "").splitlines()
                   if text.strip()]
            lines = [{"text": text, "role": "given" if index == 0 else "student", "bad": False}
                     for index, text in enumerate(raw)]
        self._answer_lines = lines
        self._answer_show_all = len(lines) <= CHECK_LONG_LINES
        self._render_steps()

        text, kind = check_locate_notice(locate, data.get("notice"))
        self.locate_notice.setText(text)
        self.locate_notice.setVisible(bool(text))
        self.locate_notice.setStyleSheet(
            "background: #EDF2FB; border: 1px solid #D7DEE7; border-radius: 8px; padding: 8px;"
            if kind == "info" else
            "background: #FFF6E5; border: 1px solid #E8D2A8; border-radius: 8px; padding: 8px;")

        contrast = dict(locate.get("contrast") or {})
        self.contrast_box.setVisible(self._answer_gate_bad)
        self.contrast_expected.setText(contrast.get("expected") or "—")
        self.contrast_actual.setText(contrast.get("actual") or "—")
        note = ""
        if self._answer_gate_bad:
            if contrast.get("note"):
                note = "对照：%s" % contrast["note"]
            if locate.get("reason_note"):
                note = (note + " " + locate["reason_note"]).strip()
        self.contrast_note.setText(note)
        self.contrast_note.setVisible(bool(note))
        self.preset_note.setVisible(locate.get("source") == "preset")

        raw = str(answer.get("standard_solution") or "").strip()
        steps = [part.strip() for part in re.split(r"[；;\n]+", raw) if part.strip()]
        if steps:
            self.std_list.setText("<ol style='margin-left: 18px;'>%s</ol>"
                                  % "".join("<li>%s</li>" % html.escape(part) for part in steps))
        else:
            self.std_list.setText("<p>这份作答没有附带标准解。</p>")
        self.std_toggle.setChecked(False)
        self.std_list.hide()

        self.manual_card.setVisible((locate.get("status") or "") == "out_of_scope")
        self.content_steps.setPlainText(answer.get("student_steps") or "")
        self.content_missing.setChecked(bool(int(answer.get("missing") or 0)))
        self.content_reason.setText(answer.get("missing_reason") or "")
        self.content_reason.setEnabled(self.content_missing.isChecked())
        self.content_error.hide()

        self._render_causes(candidates, judgement, data)
        self._render_judgement(judgement, applicability)
        self._render_followup(followup)
        self._render_axis(judgement)
        self.observed_input.setText(judgement.get("observed_step") or locate.get("first_bad_text") or "")
        self.observed_hint.setText(
            "只记录观察到的不等价，不推断学生为什么这样写。" if self._answer_gate_bad else
            "本次定位没有给出错步行：可以留空，也可以手动写下你观察到的错误步骤；"
            "手写错步算「人工核验」，需要同时在「教师判断」卡里填判断依据。")
        self._skipped = False
        self.skip_line.hide()
        self.reload_answer_button.hide()
        self.answer_error.hide()
        self._sync_answer_buttons()
        self._refresh_layout()

    def _render_steps(self):
        while self.steps_lines.count():
            item = self.steps_lines.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        lines = self._answer_lines
        locate = dict((self._detail or {}).get("locate") or {})
        limit = len(lines) if self._answer_show_all else CHECK_LONG_LINES
        for index, line in enumerate(lines[:limit]):
            bad = self._answer_gate_bad and (
                line.get("bad") is True or (self._answer_bad_index is not None
                                            and index == self._answer_bad_index))
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            role = QLabel("原题" if line.get("role") == "given" else "第 %d 行" % index)
            role.setObjectName("checkLineRole-%d" % index)
            role.setStyleSheet("color: #5B6673; font-size: 11px;")
            role.setMinimumWidth(44)
            text = QLabel(check_step_html(line.get("text"), bad, locate))
            text.setObjectName("checkLineText-%d" % index)
            text.setWordWrap(True)
            text.setTextFormat(Qt.RichText)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row_layout.addWidget(role)
            row_layout.addWidget(text, 1)
            if bad:
                mark = QLabel("首处不等价")
                mark.setObjectName("checkLineMark-%d" % index)
                mark.setStyleSheet("background: #FDECEA; border: 1px solid #E4B7B2; border-radius: 8px;"
                                   " padding: 1px 6px; color: #C0392B; font-weight: 600;")
                row_layout.addWidget(mark)
            self.steps_lines.addWidget(row)
        self.steps_box.setVisible(bool(lines))
        self.steps_empty.setVisible(not lines)
        rest = len(lines) - CHECK_LONG_LINES
        if rest > 0:
            self.steps_more_button.setText(
                ("收起其余 %d 行（长作答不横向滚动）" % rest) if self._answer_show_all
                else ("展开其余 %d 行（长作答不横向滚动）" % rest))
            self.steps_more_button.show()
        else:
            self.steps_more_button.hide()

    def _render_causes(self, candidates, judgement, data):
        while self.cause_rows.count():
            item = self.cause_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cause_radios = []
        selected = judgement.get("cause_code") or ""
        for index, candidate in enumerate(candidates):
            card = QFrame()
            card.setObjectName("checkCandidate-%d" % index)
            card.setStyleSheet("QFrame#checkCandidate-%d { border: 1px solid #E7ECF2;"
                               " border-radius: 8px; }" % index)
            row = QVBoxLayout(card)
            row.setContentsMargins(8, 6, 8, 6)
            row.setSpacing(2)
            head = QHBoxLayout()
            radio = QRadioButton(candidate.get("label") or candidate.get("code") or "（未命名候选）")
            radio.setObjectName("checkCandidateRadio-%d" % index)
            radio.setChecked(bool(candidate.get("code")) and candidate.get("code") == selected)
            radio.toggled.connect(self._sync_answer_buttons)
            head.addWidget(radio)
            if candidate.get("code"):
                code_label = QLabel(str(candidate["code"]))
                code_label.setStyleSheet("color: #5B6673; font-size: 11px;")
                head.addWidget(code_label)
            head.addStretch(1)
            row.addLayout(head)
            for key, title in (("support", "支持"), ("rebut", "削弱"), ("pending", "待补")):
                items = [str(part) for part in (candidate.get(key) or []) if str(part).strip()]
                if items:
                    label = QLabel("%s：%s" % (title, "；".join(items)))
                    label.setWordWrap(True)
                    label.setStyleSheet("color: #5B6673; font-size: 11px;")
                    row.addWidget(label)
            self.cause_rows.addWidget(card)
            self._cause_radios.append(radio)
        has_candidates = bool(candidates)
        self.cause_box.setVisible(has_candidates)
        empty_note = data.get("causes_note") or "尚无已审错因库可用，保留待判断。"
        self.causes_empty.setText("" if has_candidates else ("暂无候选错因：%s" % empty_note))
        self.adopt_row.setVisible(has_candidates)
        note = data.get("causes_note") or ""
        # 没有候选时同一句话已经显示在空位里，别再重复一遍。
        self.causes_note.setText("" if (not has_candidates and note == empty_note) else note)
        self.causes_note.setVisible(bool(self.causes_note.text()))

    def _render_judgement(self, judgement, applicability):
        state = judgement.get("cause_state") or "undecided"
        decided = state == "decided"
        self.judge_stale.setVisible(bool(judgement.get("stale")))
        retracted = dict(judgement.get("retracted") or {})
        if retracted.get("at"):
            line = "上一次结论已于 %s 撤回" % str(retracted["at"]).replace("T", " ")[:16]
            if retracted.get("reason"):
                line += "：%s" % retracted["reason"]
            self.judge_retracted.setText(line + "。结论已作废，本份回到待核对队列。")
            self.judge_retracted.show()
        else:
            self.judge_retracted.hide()
        if decided:
            self.judge_current.setText("当前原因：%s" % (judgement.get("cause_label_text")
                                                      or judgement.get("cause_code") or ""))
            self.judge_current.show()
            self.reset_cause_button.show()
        else:
            self.judge_current.hide()
            self.reset_cause_button.hide()
        source = ""
        if (judgement.get("cause_code") or judgement.get("cause_label_text")) and \
                judgement.get("cause_source_label"):
            source = "错因来源：%s" % judgement["cause_source_label"]
        self.judge_source.setText(source)
        self.judge_source.setVisible(bool(source))
        basis_required = applicability == "optional" and state != "not_applicable"
        self.basis_caption.setText(
            "判断依据（本题规则判不了：人工改判原因时必填）" if basis_required
            else "判断依据（可选，与备注分开记录）")
        self.basis_hint.setText(
            "本题超出规则核验范围：要判原因就必须写判断依据，否则保存会被拒绝（basis_required）；"
            "确认无需判断就直接点主按钮，系统会记成「无需判断」，不要求依据。"
            if basis_required else "")
        self.basis_hint.setVisible(basis_required)
        self.basis_input.setPlainText(judgement.get("basis_note") or "")
        self.note_input.setPlainText(judgement.get("teacher_note") or "")
        checked = (judgement.get("progress") or "pending") == "checked"
        self.retract_button.setVisible(checked)
        self.retract_hint.setVisible(checked)

    def _render_followup(self, followup):
        suggestion = dict(followup.get("suggestion") or {})
        available = followup.get("available")
        if available is None:
            available = bool(followup.get("question"))
        question = (followup.get("question") or suggestion.get("question") or "") if available else ""
        if available:
            self.ask_line.setText("可以这样问学生：%s" % question)
            self.ask_line.setStyleSheet("font-weight: 600;")
        else:
            self.ask_line.setText(CHECK_ASK_UNAVAILABLE)
            self.ask_line.setStyleSheet("color: #C0392B; font-weight: 600;")
        note = suggestion.get("note") or followup.get("note") or ""
        self.ask_note.setText(note)
        self.ask_note.setVisible(bool(note))
        if question and not self.ask_input.text().strip():
            self.ask_input.setText(question)
        recorded = bool(followup.get("recorded"))
        self.followup_recorded.setVisible(recorded)
        if recorded:
            source = followup.get("source_label") or ""
            self.followup_recorded.setText("上次记录的学生原话%s：「%s」" % (
                ("（问句来源：%s）" % source) if source else "",
                followup.get("answer_text") or "（原话为空）"))
            if followup.get("answer_text") and not self.answer_input.toPlainText().strip():
                self.answer_input.setPlainText(followup.get("answer_text") or "")
        if followup.get("needs_review"):
            self.followup_review.setText(
                "上一次记录的学生回答已不再是当前证据：%s"
                % (followup.get("review_note") or "这条追问依据的错步或原因已经变了，请重新确认。"))
            self.followup_review.show()
        else:
            self.followup_review.hide()
        history = [item for item in (followup.get("history") or [])
                   if item.get("answer_text") and not item.get("current_version")][:3]
        if history:
            lines = []
            for item in history:
                lines.append("旧版本记录（%s）：%s学生原话：「%s」" % (
                    item.get("invalidated_reason") or "已失效",
                    ("问：%s；" % item["question"]) if item.get("question") else "",
                    item.get("answer_text")))
            self.followup_history.setText("\n".join(lines))
            self.followup_history.show()
        else:
            self.followup_history.hide()

    def _render_axis(self, judgement):
        progress = judgement.get("progress") or "pending"
        state = judgement.get("cause_state") or "undecided"
        self.axis_progress.setText(judgement.get("progress_label")
                                   or PROGRESS_TEXT.get(progress, "待核对"))
        self.axis_cause.setText(judgement.get("cause_label")
                                or CAUSE_STATE_TEXT.get(state, "原因待判断"))
        self.axis_progress.setStyleSheet(CHECK_CHIP_ON if progress == "checked" else CHECK_CHIP_OFF)
        if state == "decided":
            self.axis_cause.setStyleSheet(CHECK_CHIP_ON)
        elif state == "not_applicable":
            self.axis_cause.setStyleSheet(CHECK_CHIP_NA)
        else:
            self.axis_cause.setStyleSheet(CHECK_CHIP_OFF)
        self.axis_hint.setText(
            "当前是「教师已判断」：主按钮沿用这条原因；改判请在候选区点「采纳选中原因并保存」。"
            if state == "decided" else
            "原因留空即「原因待判断」，课堂上可以再补记；保存后退出待核对队列。")

    def _render_records(self):
        records = self._records
        items = list(records.get("items") or [])
        self.records_title.setText("核对记录（%d 条）" % int(records.get("total") or len(items)))
        for key, button in self._filter_buttons.items():
            button.setChecked(key == self._records_state)
        self._fill_records_batches()
        self.records_scope.setText(
            ("当前范围：只看「%s」这一批" % self._records_batch) if self._records_batch
            else "当前范围：全部批次（本班所有批次，每行都标着所属批次）")
        self.records_table.setRowCount(len(items))
        for row, item in enumerate(items):
            progress = item.get("progress") or "pending"
            state = item.get("cause_state") or "undecided"
            cause_text = (item.get("cause_label_text") or item.get("cause_state_label")
                          or CAUSE_STATE_TEXT.get(state, "原因待判断"))
            values = [
                item.get("student_label") or "—",
                item.get("question_no") or "—",
                item.get("batch") or "—",
                item.get("observed_step") or "—",
                PROGRESS_TEXT.get(progress, "待核对"),
                cause_text,
                str(item.get("saved_at") or "").replace("T", " ")[:16] or "—",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                self.records_table.setItem(row, column, cell)
        self.records_status.setText(
            "" if items else "这个筛选下还没有记录。本批次暂无满足条件的核对记录；"
                             "保存过的作答会出现在这里，按「原因待判断」能找回还没定原因的那几份。")
        self._next_answer_id = ((self._overview or {}).get("next") or {}).get("answer_id")
        self.records_primary.setEnabled(bool(self._next_answer_id))
        self.records_primary.setToolTip("" if self._next_answer_id else "当前没有待核对的作答")
        if items and self.records_table.currentRow() < 0:
            self.records_table.selectRow(0)
        self._refresh_layout()

    def _fill_records_batches(self):
        assignments = list((self._overview or {}).get("assignments") or [])
        current = self._records_batch
        self.records_batch_combo.blockSignals(True)
        self.records_batch_combo.clear()
        self.records_batch_combo.addItem("全部批次（本班所有批次）", "")
        for item in assignments:
            label = item.get("name") or item.get("batch") or "（未命名作业）"
            if item.get("synthetic"):
                label += "（演示）"
            self.records_batch_combo.addItem(label, item.get("batch") or "")
        index = self.records_batch_combo.findData(current)
        self.records_batch_combo.setCurrentIndex(index if index >= 0 else 0)
        self.records_batch_combo.blockSignals(False)

    # ---------------------------------------------------------------- 动作
    def show_overview(self):
        self.load_overview()

    def show_records(self):
        self.stack.setCurrentIndex(2)
        if not (self._overview or {}).get("assignments"):
            self.load_overview()
        self.load_records()

    def _batch_changed(self, _index=0):
        self._batch = self.overview_batch_combo.currentData() or ""
        self.load_overview()

    def _assignment_clicked(self, row, _column=0):
        if 0 <= row < len(self._assignment_rows):
            batch = self._assignment_rows[row].get("batch") or ""
            index = self.overview_batch_combo.findData(batch)
            if index >= 0:
                self.overview_batch_combo.setCurrentIndex(index)

    def _overview_primary_clicked(self):
        text = self.overview_primary.text()
        if text == "导入作答":
            self.open_import()
        elif text == "查看核对记录":
            self.show_records()
        else:
            next_item = dict((self._overview or {}).get("next") or {})
            if next_item.get("answer_id"):
                self.open_answer(next_item["answer_id"])
            else:
                self.show_records()

    def _open_next(self):
        next_item = dict((self._overview or {}).get("next") or {})
        next_id = next_item.get("answer_id")
        if not next_id:
            self.records_status.setText("当前没有待核对的作答。")
            return
        self.open_answer(next_id)

    def _records_filter_changed(self, name):
        self._records_state = name
        self.load_records()

    def _records_batch_changed(self, _index=0):
        self._records_batch = self.records_batch_combo.currentData() or ""
        self.load_records()

    def _open_selected_record(self):
        row = self.records_table.currentRow()
        items = list((self._records or {}).get("items") or [])
        if row < 0 or row >= len(items):
            self.records_status.setText("先在表里点一条记录。")
            return
        self.open_answer(items[row].get("answer_id"))

    def open_import(self):
        self._import_window = AnswerImportWindow(self.service, self.class_row, parent=self)
        self._import_window.closed.connect(self.load_overview)
        self._import_window.show()
        self._import_window.raise_()

    def _toggle_steps(self):
        self._answer_show_all = not self._answer_show_all
        self._render_steps()

    def _toggle_std(self, checked):
        self.std_list.setVisible(bool(checked))
        self.std_toggle.setText("标准解（点一下收起）" if checked else "标准解（默认折叠，点开查看）")

    def _toggle_content(self, checked):
        self.content_body.setVisible(bool(checked))
        self.content_toggle.setText("收起补录 / 校正" if checked else "点开：改学生步骤或标为缺测")

    def _focus_manual(self):
        self.basis_input.setFocus()
        self.answer_error.hide()
        self.top_note.setText("已展开人工核对：请填观察到的错误步骤、原因与判断依据。")
        self.top_note.show()

    def _content_missing_changed(self, checked):
        self.content_reason.setEnabled(bool(checked))

    def _record_answer(self):
        self._skipped = False
        self.skip_line.hide()
        self.answer_input.setFocus()

    def _skip_answer(self):
        self._skipped = True
        self.answer_input.clear()
        self.skip_line.setText("已跳过提问，未记录回答")
        self.skip_line.show()

    def _refresh_answer(self):
        self.answer_input.clear()
        self.ask_input.clear()
        self.basis_input.clear()
        self.note_input.clear()
        self.content_steps.clear()
        self.observed_input.clear()
        self.open_answer(self._answer_id)

    def _selected_candidate(self):
        candidates = list((self._detail or {}).get("candidates") or [])
        for index, radio in enumerate(self._cause_radios):
            if radio.isChecked() and 0 <= index < len(candidates):
                return candidates[index]
        return None

    def _sync_answer_buttons(self):
        busy = self.runner.busy
        judgement = dict((self._detail or {}).get("judgement") or {})
        decided = (judgement.get("cause_state") or "") == "decided"
        self.save_button.setText("保存中…" if busy else
                                 ("保存错步（沿用已判断原因）" if decided else "保存错步，原因待判断"))
        self.save_button.setEnabled(not busy)
        self.adopt_button.setEnabled(not busy and self._selected_candidate() is not None)
        self.manual_button.setEnabled(not busy and bool(self.manual_label.text().strip()))
        self.reset_cause_button.setEnabled(not busy)
        self.retract_button.setEnabled(not busy)
        self.content_button.setEnabled(not busy)
        self.answer_record_button.setEnabled(not busy)
        self.answer_skip_button.setEnabled(not busy)
        self.reload_answer_button.setEnabled(not busy)

    def _show_error(self, text):
        self.answer_error.setText(text)
        self.answer_error.show()

    def run_demo(self, action):
        if self.runner.busy:
            self.demo_feedback.setText("上一个操作还在处理，请稍后再点。")
            self.demo_feedback.show()
            return
        self.demo_seed_button.setEnabled(False)
        self.demo_reset_button.setEnabled(False)
        self.demo_feedback.hide()
        func = (lambda: self.service.check_demo_seed(self.class_id)) if action == "seed" \
            else (lambda: self.service.check_demo_reset(self.class_id))
        self.runner.run(CheckWindow._call(func),
                        lambda outcome: self._demo_done(action, outcome), self._demo_failed)

    def _demo_done(self, action, outcome):
        self.demo_seed_button.setEnabled(True)
        self.demo_reset_button.setEnabled(True)
        if not outcome.get("ok"):
            self._demo_failed(outcome)
            return
        result = dict(outcome.get("value") or {})
        if action == "seed":
            seeded = dict(result.get("seeded") or {})
            text = "已载入合成演示数据：新增 %d 份" % int(seeded.get("answers") or 0)
            if int(seeded.get("skipped_existing") or 0):
                text += "，跳过已存在的 %d 份" % int(seeded.get("skipped_existing") or 0)
            text += "。只影响合成数据。"
        else:
            deleted = dict(result.get("deleted") or {})
            kept = dict(result.get("kept") or {})
            text = ("已重置合成数据：删除 %d 份作答、%d 条追问；真实导入的作答保留 %d 份。"
                    % (int(deleted.get("answers") or 0), int(deleted.get("followups") or 0),
                       int(kept.get("imported_answers") or 0)))
        self._demo_notice = text
        self.top_note.setText(text)
        self.top_note.show()
        self._batch = ""
        self.load_overview()

    def _demo_failed(self, outcome):
        self.demo_seed_button.setEnabled(True)
        self.demo_reset_button.setEnabled(True)
        message = (outcome.get("message") if isinstance(outcome, dict) else str(outcome)) or "请稍后重试"
        self.demo_feedback.setText("合成演示数据没有写入：%s" % message)
        self.demo_feedback.show()

    def _save(self, adopt=False, action="main"):
        if self.runner.busy:
            self._show_error("上一个操作还在处理，请稍后再点。")
            return
        detail = self._detail or {}
        answer = dict(detail.get("answer") or {})
        locate = dict(detail.get("locate") or {})
        judgement = dict(detail.get("judgement") or {})
        followup = dict(detail.get("followup") or {})
        decided = (judgement.get("cause_state") or "") == "decided"
        cause_state, cause_code, cause_label, cause_source = "undecided", "", "", ""
        if action == "retract":
            pass
        elif action == "undecided":
            cause_state = "undecided"
        elif action == "manual":
            cause_label = self.manual_label.text().strip()
            if not cause_label:
                self._show_error("人工判断要填「原因说明」；本次没有保存。")
                return
            cause_state = "decided"
            cause_code = self.manual_code.text().strip()
            cause_source = "teacher"
        elif adopt:
            chosen = self._selected_candidate()
            if chosen is None:
                self._show_error("先在候选里选一条，再点「采纳选中原因并保存」；本次没有保存。")
                return
            cause_state = "decided"
            cause_code = chosen.get("code") or ""
            cause_label = chosen.get("label") or ""
            cause_source = "preset"
        elif decided:
            cause_state = "decided"
            cause_code = judgement.get("cause_code") or ""
            cause_label = judgement.get("cause_label_text") or ""
        asked = self.ask_input.text().strip()
        followed = "" if self._skipped else self.answer_input.toPlainText().strip()
        if action != "retract" and followed and not asked:
            self._show_error(
                "要记录学生回答，请先在「当面追问」里填上你实际问的那一句（这份作答%s线下提问也可以）。"
                "本次没有保存。" % ("" if followup.get("available") else "没有适用追问，"))
            return
        bad_index = locate.get("first_bad_index")
        if (locate.get("status") or "") != "first_bad" or bad_index is None:
            bad_index = None
        payload = {
            "expected_version": answer.get("version") or "",
            "observed_step": self.observed_input.text().strip(),
            "first_bad_index": bad_index,
            "cause_code": cause_code,
            "cause_label": cause_label,
            "teacher_note": self.note_input.toPlainText().strip(),
            "basis_note": self.basis_input.toPlainText().strip(),
            "cause_state": cause_state,
            "followup": {"question": asked, "answer_text": followed},
        }
        if cause_source:
            payload["cause_source"] = cause_source
        if action == "retract":
            payload.pop("cause_code", None)
            payload.pop("cause_label", None)
            payload.pop("cause_state", None)
            payload.pop("cause_source", None)
            payload["progress"] = "pending"
            payload["change_reason"] = "老师在页面上撤回本次结论"
        self.answer_error.hide()
        self.answer_error.setText("")
        self.save_button.setEnabled(False)
        self.runner.run(
            CheckWindow._call(lambda: self.service.check_save(self.class_id, self._answer_id, payload)),
            self._save_done, self._save_failed)

    def _save_done(self, outcome):
        if not outcome.get("ok"):
            self._save_failed(outcome)
            return
        result = dict(outcome.get("value") or {})
        self._show_receipt(result)

    def _save_failed(self, outcome):
        if isinstance(outcome, dict) and outcome.get("status") is not None:
            status = outcome.get("status")
            code = outcome.get("code") or ""
            message = outcome.get("message") or "服务器返回错误"
            if status == 409:
                self._show_error(
                    "这份作答已被更新（%s%s）：%s 本次没有保存。你填写的内容仍在本页；可以先复制下来，"
                    "再点「刷新这份作答（会清空本页输入）」重新核对。"
                    % (status, (" " + code) if code else "", message))
                self.reload_answer_button.show()
            elif status == 400:
                hint = CHECK_ERROR_HINTS.get(code) or ""
                self._show_error("保存被拒绝（400%s）：%s%s"
                                 % ((" " + code) if code else "", message,
                                    ("（提示：%s）" % hint) if hint else ""))
            else:
                self._show_error("保存失败（%s%s）：%s"
                                 % (status, (" " + code) if code else "", message))
        else:
            self._show_error("保存失败：%s" % (outcome if not isinstance(outcome, dict)
                                              else outcome.get("message") or "请稍后重试"))
        self._sync_answer_buttons()

    def _submit_content(self):
        if self.runner.busy:
            self.content_error.setText("上一个操作还在处理，请稍后再点。")
            self.content_error.show()
            return
        answer = dict((self._detail or {}).get("answer") or {})
        missing = self.content_missing.isChecked()
        payload = {
            "expected_version": answer.get("version") or "",
            "student_steps": self.content_steps.toPlainText(),
            "missing": "是" if missing else "否",
        }
        if missing:
            payload["missing_reason"] = self.content_reason.text().strip()
        self.content_error.hide()
        self.content_button.setEnabled(False)
        self.runner.run(
            CheckWindow._call(lambda: self.service.check_update_content(
                self.class_id, self._answer_id, payload)),
            self._content_done, self._content_failed)

    def _content_done(self, outcome):
        self.content_button.setEnabled(True)
        if not outcome.get("ok"):
            self._content_failed(outcome)
            return
        result = dict(outcome.get("value") or {})
        previous = str(result.get("previous_version") or "")[:8]
        version = str(result.get("version") or "")[:8]
        note = result.get("note") or ""
        # 演示作答被校正：预置的五项匹配随旧内容失效，这份改为按当前内容规则定位（R1-01 隔离不变）。
        preset_line = "预置已失效、按当前内容重新定位。" if result.get("demo_preset_invalidated") else ""
        self._answer_flash = ("已按新内容重新分析：旧版本 %s 作废、新版本 %s；这份作答已回到待核对。%s%s"
                              % (previous or "—", version or "—", preset_line, note)).strip()
        self.content_steps.clear()
        self.content_missing.setChecked(False)
        self.content_reason.clear()
        self.open_answer(self._answer_id)

    def _content_failed(self, outcome):
        self.content_button.setEnabled(True)
        if isinstance(outcome, dict) and outcome.get("status") is not None:
            status = outcome.get("status")
            code = outcome.get("code") or ""
            message = outcome.get("message") or "请稍后重试"
            if status == 409:
                self.content_error.setText(
                    "这份作答的内容已经更新（%s%s）：%s 本次没有改动这份作答。"
                    % (status, (" " + code) if code else "", message))
            else:
                self.content_error.setText(
                    "补录没有保存（%s%s）：%s（本次没有改动这份作答）"
                    % (status, (" " + code) if code else "", message))
        else:
            self.content_error.setText("补录没有保存：%s（本次没有改动这份作答）"
                                       % (outcome if not isinstance(outcome, dict)
                                          else outcome.get("message") or "请稍后重试"))
        self.content_error.show()

    def _show_receipt(self, result):
        self._receipt = dict(result or {})
        receipt = dict(result.get("receipt") or {})
        after = dict(result.get("judgement") or {})
        after_queue = dict(result.get("queue") or {})
        after_answer = dict(result.get("answer") or {})
        after_followup = dict(result.get("followup") or {})
        after_locate = dict(result.get("locate") or {})
        retracted = result.get("action") == "retracted"
        class_name = receipt.get("class_name") or self.class_row.get("name") or "本班级"
        assignment_name = receipt.get("assignment_name") or after_answer.get("batch") or "本次作业"
        next_after = after_queue.get("next_answer_id")
        is_last = after_queue.get("is_last") is True or not next_after
        remaining = after_queue.get("remaining")
        state = after.get("cause_state") or "undecided"
        if state == "decided":
            cause_text = "教师已判断：%s" % (after.get("cause_label_text") or after.get("cause_code")
                                            or "（未命名原因）")
        elif state == "not_applicable":
            cause_text = "无需判断：本题没有可归因的错步（不计入已确认错因）"
        else:
            cause_text = "原因待判断（不计入已确认错因）"
        observed = after.get("observed_step") or after_locate.get("first_bad_text") or ""
        followed = after_followup.get("answer_text") if after_followup.get("recorded") else ""
        candidates = list(result.get("candidates") or [])
        adopted = after.get("cause_source") == "preset"
        evidence = None
        for item in candidates:
            if (item.get("code") or "") == (after.get("cause_code") or ""):
                evidence = item
                break
        if evidence is None and candidates:
            evidence = candidates[0]

        self.receipt_title.setText("已撤回" if retracted else "已保存")
        self.receipt_dest.setText(("撤回后回到待核对：" if retracted else "已保存到：")
                                  + "%s · %s" % (class_name, assignment_name))
        self.receipt_ident.setText("%s · 第 %s 题 · 记录编号 %s" % (
            after_answer.get("student_label") or "学生", after_answer.get("question_no") or "—",
            after_answer.get("record_key") or "—"))
        self.receipt_status.setText(("两轴状态：%s" % receipt["status_line"])
                                    if receipt.get("status_line") else "")
        self.receipt_saved_at.setText(("保存时间：%s" % str(receipt["saved_at"]).replace("T", " ")[:19])
                                      if receipt.get("saved_at") else "")
        progress = after.get("progress") or "pending"
        self.receipt_progress_chip.setText(after.get("progress_label")
                                           or PROGRESS_TEXT.get(progress, "待核对"))
        self.receipt_cause_chip.setText(after.get("cause_label")
                                        or CAUSE_STATE_TEXT.get(state, "原因待判断"))
        self.receipt_progress_chip.setStyleSheet(CHECK_CHIP_ON if progress == "checked" else CHECK_CHIP_OFF)
        if state == "decided":
            self.receipt_cause_chip.setStyleSheet(CHECK_CHIP_ON)
        elif state == "not_applicable":
            self.receipt_cause_chip.setStyleSheet(CHECK_CHIP_NA)
        else:
            self.receipt_cause_chip.setStyleSheet(CHECK_CHIP_OFF)

        self.receipt_observed.setText("观察到的错步：%s" % (observed or "（未填写）"))
        if after.get("verify_source") == "manual":
            self.receipt_verify.setText(
                "核验来源：教师人工核验%s" % (("（人工记录的错步：「%s」）" % after["teacher_error_text"])
                                        if after.get("teacher_error_text") else ""))
        elif after.get("verify_source_label"):
            self.receipt_verify.setText("核验来源：%s" % after["verify_source_label"])
        else:
            self.receipt_verify.setText("")
        if followed:
            self.receipt_quote.setText("问：%s\n学生原话：「%s」" % (
                after_followup.get("question") or self.ask_input.text().strip() or CHECK_ASK_UNAVAILABLE,
                followed))
            self.receipt_quote.show()
        else:
            self.receipt_quote.setText("本次未记录学生回答（已跳过提问或未填写原话）。")
            self.receipt_quote.show()
        if after_followup.get("needs_review"):
            self.receipt_review.setText(
                "依据已更新，需复核：%s"
                % (after_followup.get("review_note")
                   or "这条追问依据的错步或原因已经变了，请重新确认后再当作当前证据。"))
            self.receipt_review.show()
        else:
            self.receipt_review.hide()
        self.receipt_cause_text.setText("教师判断：%s" % cause_text)
        source = ""
        if (after.get("cause_code") or after.get("cause_label_text")) and after.get("cause_source_label"):
            source = "错因来源：%s" % after["cause_source_label"]
        if evidence is not None:
            source = (source + "；" if source else "") + "%s：%s" % (
                "已采纳的候选" if adopted else "未采纳的候选依据",
                evidence.get("label") or evidence.get("code") or "")
        self.receipt_source_text.setText(source)
        self.receipt_source_text.setVisible(bool(source))
        self.receipt_basis_text.setText(("判断依据：%s" % after["basis_note"]) if after.get("basis_note") else "")
        self.receipt_note_text.setText(("教师备注：%s" % after["teacher_note"]) if after.get("teacher_note") else "")

        if retracted:
            self.receipt_banner.setText(
                "%s：本次结论已作废，本份回到「待核对」队列；旧结论与撤回理由留在历史里，可以重新核对。"
                % (receipt.get("action_label") or "已撤回，回到待核对"))
            self.receipt_primary.setText("返回课前总览")
        elif is_last:
            self.receipt_banner.setText("本次作业的待核对作答已全部处理完。可以从「查看核对记录」"
                                        "找回原因待判断的记录，课堂上再补记学生原话。")
            self.receipt_primary.setText("返回课前总览")
        else:
            self.receipt_banner.setText(
                "这份已经保存，队列里还剩 %d 份待核对（仍是本次作业）。" % int(remaining or 0)
                if remaining is not None else "同一个队列里还有待核对的作答。")
            self.receipt_primary.setText("查看下一份")
        self._receipt_next_id = next_after if not is_last else None
        self.receipt_primary.show()
        self.receipt_banner.show()
        self.stack.setCurrentIndex(3)
        self.receipt_scroll.verticalScrollBar().setValue(0)
        self._refresh_layout()

    def _receipt_primary_clicked(self):
        next_id = getattr(self, "_receipt_next_id", None)
        if next_id:
            self.open_answer(next_id)
        else:
            self.load_overview()

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()


class ClassesWindow(QMainWindow):
    logged_out = Signal()
    open_students = Signal(dict)
    show_class_stats = Signal(dict)
    closed = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner()
        self._items = []
        self.setObjectName("classesWindow")
        self.setWindowTitle("数智伴学 · 桌面版")
        self.resize(900, 520)

        central = QWidget()
        self.setCentralWidget(central)

        self.who = QLabel("")
        self.who.setObjectName("currentUser")
        self.logout_button = QPushButton("退出登录")
        self.logout_button.setObjectName("logoutButton")
        self.logout_button.clicked.connect(self.logout)

        top = QHBoxLayout()
        heading = QLabel("我的班级")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        top.addWidget(heading)
        top.addStretch(1)
        top.addWidget(self.who)
        top.addWidget(self.logout_button)

        self.new_button = QPushButton("新建班级")
        self.new_button.setObjectName("newClassButton")
        self.new_button.clicked.connect(self.open_create_dialog)
        self.students_button = QPushButton("学生名单")
        self.students_button.setObjectName("openStudentsButton")
        self.students_button.setEnabled(False)
        self.students_button.clicked.connect(self.open_selected_class)
        self.class_stats_button = QPushButton("班级学情")
        self.class_stats_button.setObjectName("openClassStatsButton")
        self.class_stats_button.setEnabled(False)
        self.class_stats_button.setToolTip("按日期查看这个班级的错题分布")
        self.class_stats_button.clicked.connect(self.open_selected_class_stats)
        self.check_button = QPushButton("作答核对")
        self.check_button.setObjectName("openAnswerCheckButton")
        self.check_button.setEnabled(False)
        self.check_button.setToolTip("按批次核对本班作答：先看哪个能判、哪个要问一句，再逐份核对")
        self.check_button.clicked.connect(self.open_check)
        self.grading_button = QPushButton("批量核验与评分")
        self.grading_button.setObjectName("openGradingButton")
        self.grading_button.setEnabled(False)
        self.grading_button.setToolTip("按批次批量核验、逐条确认错因、录入与确认教师评分")
        self.grading_button.clicked.connect(self.open_grading_window)
        self.dashboard_button = QPushButton("学情看板")
        self.dashboard_button.setObjectName("openDashboardButton")
        self.dashboard_button.setEnabled(False)
        self.dashboard_button.setToolTip("交互式看板：指标卡片、知识点/错因分布、热力图、完整成绩分布")
        self.dashboard_button.clicked.connect(self.open_dashboard)
        self.teaching_button = QPushButton("教学建议与练习")
        self.teaching_button.setObjectName("openTeachingButton")
        self.teaching_button.setEnabled(False)
        self.teaching_button.setToolTip("按已确认错因检索已发布的教学资源：教学建议、三级提示与举一反三练习")
        self.teaching_button.clicked.connect(self.open_teaching)
        self.ai_settings_button = QPushButton("AI 设置")
        self.ai_settings_button.setObjectName("openAiSettingsButton")
        self.ai_settings_button.setToolTip("配置云端视觉服务地址、模型和本次登录的密钥；默认不启用云端分析")
        self.ai_settings_button.clicked.connect(self.open_ai_settings)
        self.ai_trial_button = QPushButton("AI 试分析")
        self.ai_trial_button.setObjectName("openAiTrialButton")
        self.ai_trial_button.setToolTip("用一张不含学生身份的题图试一次，不生成错题")
        self.ai_trial_button.clicked.connect(self.open_ai_trial)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.clicked.connect(self.reload)
        self.status = QLabel("")
        self.status.setObjectName("classStatus")

        tools = QHBoxLayout()
        tools.addWidget(self.new_button)
        tools.addWidget(self.students_button)
        tools.addWidget(self.class_stats_button)
        tools.addWidget(self.check_button)
        tools.addWidget(self.grading_button)
        tools.addWidget(self.dashboard_button)
        tools.addWidget(self.teaching_button)
        tools.addWidget(self.ai_settings_button)
        tools.addWidget(self.ai_trial_button)
        tools.addWidget(self.refresh_button)
        tools.addWidget(self.status, 1)

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("classTable")
        self.table.setHorizontalHeaderLabels(["班级名称", "年级", "学生数", "创建时间"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemDoubleClicked.connect(self.open_selected_class)

        self.empty = QLabel("还没有班级。点「新建班级」创建第一个班级。")
        self.empty.setObjectName("emptyLabel")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet("color: #777;")

        self.body = QStackedWidget()
        self.body.setObjectName("classesBody")
        self.body.addWidget(self.empty)
        self.body.addWidget(self.table)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addLayout(tools)
        layout.addWidget(self.body, 1)

        self.data_dir_label = QLabel("")
        self.data_dir_label.setObjectName("dataDirLabel")
        self.data_dir_label.setStyleSheet("color: #777; font-size: 11px;")
        layout.addWidget(self.data_dir_label)

    def set_data_dir(self, path):
        self.data_dir_label.setText("数据目录：%s" % data_dir_text(path))
        self.data_dir_label.setToolTip(path)

    # ---------------------------------------------------------------- 数据
    def show_user(self, user):
        self.who.setText("当前账号：%s" % user["username"])

    def reload(self):
        if self.runner.busy:
            # 刚保存完就刷新时，上一个任务还没释放；等它释放再刷，不能悄悄跳过。
            QTimer.singleShot(150, self.reload)
            return
        self.new_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载班级…")
        self.runner.run(self.service.list_classes, self._show_classes, self._load_failed)

    def _load_failed(self, message):
        self.new_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.status.setText(message)

    def _show_classes(self, items):
        self.new_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        current = self._selected_class()
        self._items = list(items)
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = [
                item.get("name", ""),
                item.get("grade", ""),
                str(item.get("student_count", 0)),
                (item.get("created_at", "") or "").replace("T", " ")[:16],
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 3:
                    cell.setToolTip(item.get("created_at", ""))
                self.table.setItem(row, column, cell)
            if current is not None and item["id"] == current["id"]:
                self.table.selectRow(row)
        if items and self.table.currentRow() < 0:
            # 刷新后默认高亮第一个班级，老师不必先点一下才能打开学生名单。
            self.table.selectRow(0)
        self.body.setCurrentIndex(1 if items else 0)
        self.status.setText("共 %d 个班级" % len(items) if items else "")
        self._sync_buttons()

    # ---------------------------------------------------------------- 动作
    def _selected_class(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _sync_buttons(self):
        enabled = self._selected_class() is not None
        self.students_button.setEnabled(enabled)
        self.class_stats_button.setEnabled(enabled)
        self.check_button.setEnabled(enabled)
        self.grading_button.setEnabled(enabled)
        self.dashboard_button.setEnabled(enabled)
        self.teaching_button.setEnabled(enabled)

    def open_selected_class(self, *_):
        row = self._selected_class()
        if row is None:
            self.status.setText("先在列表里点一个班级")
            return
        self.open_students.emit(row)

    def open_selected_class_stats(self):
        row = self._selected_class()
        if row is None:
            self.status.setText("先在列表里点一个班级")
            return
        self.show_class_stats.emit(row)

    def open_check(self):
        row = self._selected_class()
        if row is None:
            self.status.setText("先在列表里点一个班级")
            return
        self._check_window = CheckWindow(self.service, row, parent=self)
        self._check_window.show()
        self._check_window.raise_()

    def open_grading_window(self):
        """R2-02：批量核验与评分（原版「作答核对」逐份核对的并列入口，两者并存）。"""
        from desktop.ui_grading import GradingWindow

        row = self._selected_class()
        if row is None:
            self.status.setText("先在列表里点一个班级")
            return
        window = GradingWindow(self.service, row, parent=self)
        window.setAttribute(Qt.WA_DeleteOnClose, True)
        self._grading_window = window
        window.show()
        window.raise_()
        return window

    def open_dashboard(self):
        """R2-03：班级学情看板（与「班级学情」并存，那个是原版按日期的错题分布）。"""
        from desktop.ui_dashboard import ClassDashboardWindow

        row = self._selected_class()
        if row is None:
            self.status.setText("先在列表里点一个班级")
            return
        # 归属闸门：越权时 404，不让看板用别人的班级 id 打开。
        try:
            self.service.owned_class_row(row["id"])
        except Exception as exc:
            self.status.setText(getattr(exc, "message", "") or "打开失败")
            return
        window = ClassDashboardWindow(
            self.service.db, row["id"], class_name=row.get("name", ""),
            today=QDate.currentDate().toString("yyyy-MM-dd"), parent=self)
        window.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dashboard_window = window
        window.show()
        window.raise_()
        return window

    def open_teaching(self):
        """R2-05：教学建议 / 三级提示 / 举一反三 / 辅导任务 / 纸笔 PDF。"""
        from desktop.ui_teaching import TeachingWindow

        row = self._selected_class()
        if row is None:
            self.status.setText("先在列表里点一个班级")
            return
        window = TeachingWindow(self.service, row, parent=self)
        window.setAttribute(Qt.WA_DeleteOnClose, True)
        self._teaching_window = window
        window.show()
        window.raise_()
        return window

    def open_ai_settings(self):
        dialog = AISettingsDialog(self.service, self)
        if dialog.exec() == QDialog.Accepted and dialog.saved:
            self.status.setText("AI 设置已保存；密钥下次登录需要重新填写。")

    def open_ai_trial(self):
        AITrialDialog(self.service, self).exec()

    def open_create_dialog(self):
        dialog = CreateClassDialog(self.service, self)
        if dialog.exec() == QDialog.Accepted and dialog.created:
            self.reload()
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0) and self.table.item(row, 0).text() == dialog.created["name"]:
                    self.table.selectRow(row)
                    break

    def logout(self):
        self.runner.run(self.service.logout, lambda _: self._logged_out(), self.status.setText)

    def _logged_out(self):
        self.logged_out.emit()

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()


class DesktopApp:
    """把登录窗口、班级窗口与学生名单串起来，退出登录时回到登录窗口。"""

    def __init__(self, service, data_dir):
        self.service = service
        self.data_dir = data_dir
        theme.apply()
        self.login_window = LoginWindow(service)
        self.login_window.set_data_dir(data_dir)
        self.login_window.logged_in.connect(self.enter_classes)
        self.login_window.closed.connect(self.quit)
        self.classes_window = None
        self.students_window = None
        self.mistakes_window = None
        self.stats_window = None
        self.class_stats_window = None
        self._switching = False

    def start(self):
        self.login_window.show()

    def quit(self):
        """关掉登录窗或班级窗就结束程序；切窗口过程中的关闭不算。"""
        if self._switching:
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def enter_classes(self, user):
        window = ClassesWindow(self.service)
        window.set_data_dir(self.data_dir)
        window.show_user(user)
        window.logged_out.connect(self.back_to_login)
        window.open_students.connect(self.open_students)
        window.show_class_stats.connect(self.open_class_stats)
        window.closed.connect(self.quit)
        window.show()
        window.reload()
        self.classes_window = window
        # 新窗口先出来，再收掉登录窗，避免中途没有任何窗口。
        self.login_window.hide()

    def open_students(self, class_row):
        if self.classes_window is None or self.students_window is not None:
            return
        window = StudentsWindow(self.service, class_row)
        window.closed.connect(self.close_students)
        window.show_mistakes.connect(self.open_mistakes)
        window.show_stats.connect(self.open_stats)
        window.show()
        window.reload()
        self.students_window = window
        self.classes_window.hide()

    def open_class_stats(self, class_row):
        if self.classes_window is None or self.class_stats_window is not None:
            return
        window = ClassStatsWindow(self.service, class_row)
        window.closed.connect(self.close_class_stats)
        window.open_mistake.connect(self.open_mistake_from_class_stats)
        window.show()
        self.class_stats_window = window
        self.classes_window.hide()

    def close_class_stats(self):
        # 和学情页一样：窗口一关就放手，等错题详情关掉后再回班级列表。
        window, self.class_stats_window = self.class_stats_window, None
        if window is not None:
            window.deleteLater()
        if self.mistakes_window is not None:
            return
        if self.classes_window is not None:
            self.classes_window.show()
            self.classes_window.reload()

    def open_mistake_from_class_stats(self, student, mistake_id):
        self._open_mistakes_window(student, mistake_id)

    def close_students(self):
        if self.mistakes_window is not None or self.stats_window is not None or self.class_stats_window is not None:
            return  # 错题或统计窗口还开着时不允许把名单收掉
        window, self.students_window = self.students_window, None
        if window is not None:
            window.deleteLater()
        if self.classes_window is not None:
            self.classes_window.show()
            self.classes_window.reload()

    def open_stats(self, student):
        if self.students_window is None or self.stats_window is not None:
            return
        window = StudentStatsWindow(self.service, self.students_window.class_row, student)
        window.closed.connect(self.close_stats)
        window.open_mistake.connect(self.open_mistake_from_stats)
        window.show()
        self.stats_window = window
        self.students_window.hide()

    def close_stats(self):
        # 统计页被关掉就立刻放手：即使错题详情还开着，也不能再用这个已经关掉的窗口。
        window, self.stats_window = self.stats_window, None
        if window is not None:
            window.deleteLater()
        if self.mistakes_window is not None:
            # 错题详情是从学情页打开的，等它关掉后统一回到学生名单。
            return
        if self.students_window is not None:
            self.students_window.show()
            self.students_window.reload()

    def open_mistake_from_stats(self, student, mistake_id):
        self._open_mistakes_window(student, mistake_id)

    def open_mistakes(self, student):
        self._open_mistakes_window(student, None)

    def _open_mistakes_window(self, student, focus_id):
        if self.mistakes_window is not None:
            return
        if self.class_stats_window is not None:
            class_row = self.class_stats_window.class_row
        elif self.students_window is not None:
            class_row = self.students_window.class_row
        else:
            return
        window = MistakesWindow(self.service, class_row, student, focus_id=focus_id)
        window.closed.connect(self.close_mistakes)
        window.show()
        window.reload()
        self.mistakes_window = window
        if self.class_stats_window is not None:
            self.class_stats_window.hide()
        elif self.students_window is not None:
            self.students_window.hide()

    def close_mistakes(self):
        window, self.mistakes_window = self.mistakes_window, None
        if window is not None:
            window.deleteLater()
        if self.class_stats_window is not None:
            # 从班级统计打开的错题，关掉后回到统计页，并刷新数字与依据列表。
            self.class_stats_window.show()
            self.class_stats_window.reload(keep_basis=True)
            return
        if self.stats_window is not None:
            # 从学情页打开的错题，关掉后回到学情页，并刷新数字与依据列表。
            self.stats_window.show()
            self.stats_window.reload()
            return
        if self.students_window is not None:
            self.students_window.show()
            self.students_window.reload()
            return
        if self.classes_window is not None:
            # 班级统计页在详情打开期间被关掉：详情关掉后直接回班级列表。
            self.classes_window.show()
            self.classes_window.reload()

    def back_to_login(self):
        window, self.classes_window = self.classes_window, None
        students, self.students_window = self.students_window, None
        mistakes, self.mistakes_window = self.mistakes_window, None
        stats, self.stats_window = self.stats_window, None
        class_stats, self.class_stats_window = self.class_stats_window, None
        self._switching = True
        try:
            for extra in (mistakes, stats, class_stats, students, window):
                if extra is not None:
                    extra.close()
                    extra.deleteLater()
        finally:
            self._switching = False
        self.login_window.show_login()
        self.login_window.show()
