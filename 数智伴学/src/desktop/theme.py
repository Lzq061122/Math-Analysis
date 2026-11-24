"""统一的界面外观：一套颜色、字体与控件样式，所有窗口共用。

做法只有两件事：
1. 一份调色板（主色、成功、提醒、错误、文字、边框、底色）；
2. 一份 QSS，把标题、分区标题、提示文字、按钮层级、表格、输入框的样式定下来。

窗口只需要给控件起对 objectName 或设置 role 属性，就会自动套上对应样式；
不改任何业务逻辑，也不依赖操作系统主题。
"""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

INK = "#1F2328"
MUTED = "#5B6673"
LINE = "#D7DEE7"
BG = "#F4F6F9"
CARD = "#FFFFFF"
PRIMARY = "#2E6BD6"
PRIMARY_DARK = "#2559B4"
SUCCESS = "#1E8E5A"
WARNING = "#A9630C"
DANGER = "#C0392B"
SOFT = "#EDF2FB"
HEAD_BG = "#F1F4F8"

FONT_FAMILY = "Microsoft YaHei UI"

STYLE = """
QMainWindow, QDialog { background: %(bg)s; }
QWidget { color: %(ink)s; }
QLabel { background: transparent; }

QLabel#pageTitle {
    font-size: 20px; font-weight: 600; padding: 2px 0 2px 0;
}
QLabel#pageHint { color: %(muted)s; }
QLabel#pageMeta { color: %(muted)s; font-size: 12px; }
QLabel#sectionTitle {
    font-size: 14px; font-weight: 600; padding-top: 6px;
}
QLabel#statusLine { color: %(muted)s; }
QLabel#warnLine { color: %(warning)s; font-weight: 600; }
QLabel#dangerLine { color: %(danger)s; }
QLabel#successLine { color: %(success)s; font-weight: 600; }
QLabel#bigValue { font-size: 18px; font-weight: 600; }

QFrame#card {
    background: %(card)s; border: 1px solid %(line)s; border-radius: 10px;
}
QFrame#cardTitle { background: transparent; }

QPushButton {
    background: %(card)s; border: 1px solid %(line)s; border-radius: 6px;
    padding: 5px 14px; min-height: 24px;
}
QPushButton:hover { border-color: %(primary)s; color: %(primary_dark)s; }
QPushButton:pressed { background: %(soft)s; }
QPushButton:disabled { color: #9AA4B1; border-color: #E3E8EF; background: #F7F8FA; }
QPushButton[role="primary"] {
    background: %(primary)s; border-color: %(primary)s; color: #FFFFFF; font-weight: 600;
}
QPushButton[role="primary"]:hover { background: %(primary_dark)s; border-color: %(primary_dark)s; color: #FFFFFF; }
QPushButton[role="primary"]:disabled { background: #B9CBEA; border-color: #B9CBEA; color: #F2F5FA; }
QPushButton[role="danger"] { color: %(danger)s; border-color: #E4B7B2; }
QPushButton[role="danger"]:hover { border-color: %(danger)s; color: %(danger)s; }
QPushButton[role="ghost"] { border-color: transparent; background: transparent; color: %(primary_dark)s; }
QPushButton[role="ghost"]:hover { background: %(soft)s; }

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDateEdit {
    background: %(card)s; border: 1px solid %(line)s; border-radius: 6px; padding: 5px 8px;
    selection-background-color: %(primary)s; selection-color: #FFFFFF;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {
    border-color: %(primary)s;
}
QPlainTextEdit[readOnly="true"] { background: #FAFBFD; }

QTableWidget, QTableView {
    background: %(card)s; alternate-background-color: #FAFBFD;
    border: 1px solid %(line)s; border-radius: 8px; gridline-color: #E7ECF2;
    selection-background-color: %(soft)s; selection-color: %(ink)s;
}
QTableWidget::item, QTableView::item { padding: 7px 8px; }
QHeaderView::section {
    background: %(head)s; color: %(muted)s; border: none;
    border-bottom: 1px solid %(line)s; padding: 9px 8px; font-weight: 600;
}
QTableCornerButton::section { background: %(head)s; border: none; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #C9D2DD; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #AEB9C7; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #C9D2DD; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }

QGroupBox {
    background: %(card)s; border: 1px solid %(line)s; border-radius: 10px;
    margin-top: 14px; padding: 12px 12px 10px 12px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 4px; color: %(muted)s; font-weight: 600;
}

QTabWidget::pane { border: 1px solid %(line)s; border-radius: 8px; background: %(card)s; }
QTabBar::tab {
    background: transparent; padding: 7px 16px; margin-right: 4px;
    border: 1px solid transparent; border-top-left-radius: 7px; border-top-right-radius: 7px;
}
QTabBar::tab:selected { background: %(card)s; border-color: %(line)s; border-bottom-color: %(card)s; }
QTabBar::tab:hover { color: %(primary_dark)s; }

QProgressBar { border: 1px solid %(line)s; border-radius: 6px; background: %(card)s; text-align: center; }
QProgressBar::chunk { background: %(primary)s; border-radius: 5px; }

QCheckBox { spacing: 8px; }
QToolTip { background: #2B3138; color: #FFFFFF; border: none; padding: 4px 6px; }
""" % {
    "ink": INK,
    "muted": MUTED,
    "line": LINE,
    "bg": BG,
    "card": CARD,
    "primary": PRIMARY,
    "primary_dark": PRIMARY_DARK,
    "success": SUCCESS,
    "warning": WARNING,
    "danger": DANGER,
    "soft": SOFT,
    "head": HEAD_BG,
}


def apply(app=None):
    """给整个程序套上统一外观；重复调用没有副作用。"""
    app = app or QApplication.instance()
    if app is None:
        return
    font = QFont(FONT_FAMILY, 10)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)
    app.setStyleSheet(STYLE)


def primary(button):
    """把按钮标成主要操作（蓝色实心）。"""
    button.setProperty("role", "primary")
    return button


def danger(button):
    """把按钮标成危险操作（红字描边）。"""
    button.setProperty("role", "danger")
    return button


def ghost(button):
    """把按钮标成次要操作（无边框）。"""
    button.setProperty("role", "ghost")
    return button


def title(label):
    label.setObjectName("pageTitle")
    return label


def section(label):
    label.setObjectName("sectionTitle")
    return label
