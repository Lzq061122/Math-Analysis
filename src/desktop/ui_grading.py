"""R2-02 桌面界面：作答批量核验 · 单条批改 · 教师评分（PySide6）。

与 R1 的 `CheckWindow`（`src/desktop/ui.py`）**并存**：那边是逐份核对，这边是「整批跑核验 + 批改 + 评分」。
窗口本身不做业务判断，一律调用 `app.grading`（四维写入的唯一入口），所以「四维状态 / 失效规则 /
评分口径」只有一处实现，界面不会与后端漂移。

三条交互约定（与 R1 核对页同一套习惯）：
1. **先看进度再逐条改**：批量核验逐条回调刷新进度与明细，取消只停「未处理」的记录；
2. **失败留在原页**：单条失败不弹窗打断整批，结果写进明细并进「重试失败」；
3. **错误提示逐字给**：后端中文消息原样显示，再按 `status/code` 补一句「下一步」提示
   （`GRADING_ERROR_HINTS`，缺的码回落到 `CheckWindow` 的 `CHECK_ERROR_HINTS`）。

接线（由 captain 在 `src/desktop/ui.py` 里注册；本模块不改 ui.py / service.py / theme.py）：
    from desktop.ui_grading import GradingWindow
    self._grading_window = GradingWindow(self.service, self.class_row, parent=self)
    self._grading_window.show()
`GradingWindow` 也接受直接构造好的 `GradingBackend`（测试与其它入口用）：
    GradingWindow(GradingBackend(db, class_row, user_id, uploads_dir))
"""

import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from app import db as db_mod
from app import grading, r2_contract
from . import theme

# ---------------------------------------------------------------------------
# 文案与口径（一律从 r2_contract 取，不在界面里另写状态名）
# ---------------------------------------------------------------------------

SCOPE_OPTIONS = (
    (grading.SCOPE_PENDING, "全部待核验"),
    (grading.SCOPE_ALL, "批次内全部记录"),
    (grading.SCOPE_IDS, "指定记录（填作答编号）"),
)

LOCATE_STATUS_TEXT = {
    "correct": "正确",
    "first_bad": "首处不等价",
    "missing_steps": "缺少步骤",
    "out_of_scope": "超出核验范围",
    "missing": "缺测（没有可核对的过程）",
    "": "—",
}

VERIFY_STATE_TEXT = r2_contract.VERIFY_STATES
CAUSE_STATE_TEXT = r2_contract.CAUSE_STATES
SCORE_STATE_TEXT = r2_contract.SCORE_STATES
ANSWER_STATE_TEXT = r2_contract.ANSWER_STATES

# 400/403/409 的「下一步」提示：后端原文照旧逐字显示在前面，只在后面补一句老师该怎么改。
GRADING_ERROR_HINTS = {
    "full_score_missing": "先给这道题配满分（满分一律以批次配置为准，界面不另传）。",
    "above_full": "分数不能超过该题满分（同批次同题同一个满分）。",
    "out_of_range": "分数不能小于 0；要扣分请用步骤扣分项。",
    "too_many_decimals": "分数最多两位小数，例如 7.5 或 7.25。",
    "not_number": "分数要填数字；没有依据时留空存草稿。",
    "confirm_required": "要计入正式统计，请勾选「确认成绩」后再保存。",
    "no_evidence": "没有分数只能存草稿；等有依据再确认。",
    "revision_reason_required": "改已确认的分数必须写修改理由（谁改、为什么改）。",
    "deduction_mismatch": "步骤扣分合计必须等于「满分 − 分数」；改扣分或改分数后再存。",
    "rubric_version_unchanged": "改标准正文必须同时改该题的版本号，否则旧成绩不会失效。",
    "rubric_version_missing": "请填该题的新版本号（例如 q3-2026-11）。",
    "verify_required": "先核验（或人工核对）这一条，再判断错因。",
    "cause_required": "选「教师已判断」要写原因说明。",
    "basis_required": "人工核对要留下判断依据（作业本原图或原文）。",
    "cause_not_applicable": "正确 / 缺测 / 缺少步骤的作答只能记「无需判断」。",
    "version_conflict": "这份作答内容已更新，请刷新后重新评分。",
    "nothing_to_retract": "当前没有可撤回的判断。",
    "ai_isolation": "R2 的批改与错因一律本地规则，不会外发学生作答。",
    "answer_not_found": "找不到这份作答（可能已删除或不属于本班）。",
    "mistake_missing": "先确认错因（会同步错题），再补知识点。",
    "mistake_not_confirmed": "这条错题已退回，补分类前请先重新确认错因。",
    "knowledge_point_required": "知识点不能为空；确实无法归类就保持「未分类」。",
    "bad_steps": "扣分项要写清说明与扣分值（扣分值不能为负）。",
    "bad_score_state": "评分状态只能是草稿或已确认。",
    "bad_transition": "当前状态不允许这样改，请刷新后再试。",
}


def error_hint(code):
    """错误码 → 一句「下一步」提示；缺的码回落到 `CheckWindow` 的 `CHECK_ERROR_HINTS`。"""
    hint = GRADING_ERROR_HINTS.get(code or "") or ""
    if hint:
        return hint
    try:  # 延迟导入：单独 import 本模块时不必拉起整个 ui.py
        from desktop.ui import CHECK_ERROR_HINTS
    except Exception:  # noqa: BLE001 — 拿不到提示不影响功能
        return ""
    return CHECK_ERROR_HINTS.get(code or "") or ""


def error_text(exc):
    """把 `GradingError`（或普通异常）拼成一行可读文字：原文 + （提示：下一步）。"""
    message = getattr(exc, "message", None) or str(exc)
    code = getattr(exc, "code", "") or ""
    status = getattr(exc, "status", 0) or 0
    prefix = "操作被拒绝" if 400 <= status < 500 else "操作失败"
    if status:
        prefix = "%s（%s%s）" % (prefix, status, (" " + code) if code else "")
    hint = error_hint(code)
    return "%s：%s%s" % (prefix, message, ("（提示：%s）" % hint) if hint else "")


# ---------------------------------------------------------------------------
# 后端适配层：界面只认这一层，业务全部落到 app.grading
# ---------------------------------------------------------------------------


class GradingBackend:
    """把 `grading` 的读写接口收成界面好用的形状（不做任何业务判断）。"""

    def __init__(self, db, class_row, user_id, uploads_dir=""):
        self.db = db
        self.class_row = class_row or {}
        self.user_id = user_id
        self.uploads_dir = uploads_dir or ""

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def from_service(cls, service, class_row):
        user = getattr(service, "user", None) or {}
        user_id = user.get("id") if isinstance(user, dict) else user
        uploads = getattr(service, "uploads", None)
        uploads_dir = getattr(uploads, "root", "") or getattr(uploads, "path", "") or ""
        return cls(getattr(service, "db", None), class_row, user_id, uploads_dir)

    # -- 只读 ---------------------------------------------------------------
    def batches(self):
        rows = db_mod.list_batch_configs(self.db, self.class_row.get("id")) \
            if self.class_row.get("id") is not None else []
        names = [row.get("batch") for row in rows if row.get("batch")]
        for raw in self.db.query(
                "SELECT DISTINCT batch FROM answers WHERE class_id=? AND batch<>''"
                " ORDER BY batch", (self.class_row.get("id"),)):
            if raw["batch"] not in names:
                names.append(raw["batch"])
        configs = {row.get("batch"): row for row in rows}
        return [{"batch": name,
                 "name": (configs.get(name) or {}).get("name") or name,
                 "config_state": (configs.get(name) or {}).get("config_state") or "",
                 "occurred_on": (configs.get(name) or {}).get("occurred_on") or ""}
                for name in names]

    def queue(self, batch="", scope=grading.SCOPE_PENDING, answer_ids=None):
        return grading.batch_rows(self.db, self.class_row, batch, scope=scope,
                                 answer_ids=answer_ids)

    def progress(self, batch=""):
        return grading.review_progress(self.db, self.class_row, batch)

    def detail(self, answer_id):
        answer = self.db.query_one(
            "SELECT a.*, s.name AS roster_name FROM answers a"
            " LEFT JOIN students s ON s.id = a.student_id"
            " WHERE a.id=? AND a.class_id=?",
            (answer_id, self.class_row.get("id")))
        if answer is None:
            raise grading.GradingError("找不到这份作答（可能已被删除），或者它不属于当前班级。",
                                       404, "answer_not_found")
        answer = dict(answer)
        answer["student_name"] = answer.get("student_name") or answer.get("roster_name") or ""
        dims = grading.answer_dimensions(self.db, self.class_row, answer_id)
        config = db_mod.load_batch_config(self.db, self.class_row.get("id"),
                                          answer["batch"] or "")
        question = dims["scored_question_no"] or answer["question_no"] or ""
        entry = grading.question_rubric(config, question)
        return {
            "answer": answer,
            "dimensions": dims["dimensions"],
            "images": dims["image_rows"],
            "score": grading.score_row(self.db, self.class_row, answer_id, question),
            "candidates": grading.cause_candidates(self.db, self.class_row, answer_id),
            "mistake": grading.mistake_of(self.db, self.class_row, answer_id),
            "rubric": entry,
            "question_no": question,
            "batch": answer["batch"] or "",
        }

    def score_sheet(self, batch):
        return grading.score_sheet(self.db, self.class_row, batch)

    def scoring_progress(self, batch):
        return grading.scoring_progress(self.db, self.class_row, batch)

    # -- 写 -----------------------------------------------------------------
    def verify(self, batch="", scope=grading.SCOPE_PENDING, answer_ids=None,
               on_result=None, cancel_check=None):
        return grading.batch_verify(self.db, self.class_row, self.user_id, batch,
                                    scope=scope, answer_ids=answer_ids,
                                    on_result=on_result, cancel_check=cancel_check)

    def retry(self, answer_ids, on_result=None, cancel_check=None):
        return grading.retry_failed(self.db, self.class_row, self.user_id, answer_ids,
                                    on_result=on_result, cancel_check=cancel_check)

    def confirm_cause(self, answer_id, cause_label, cause_code="", basis_note="",
                      knowledge_point=""):
        return grading.confirm_cause(self.db, self.class_row, self.user_id, answer_id,
                                     cause_code=cause_code, cause_label=cause_label,
                                     basis_note=basis_note, knowledge_point=knowledge_point)

    def retract_cause(self, answer_id, reason=""):
        return grading.retract_cause(self.db, self.class_row, self.user_id, answer_id, reason)

    def set_knowledge(self, answer_id, knowledge_point, basis=""):
        return grading.set_knowledge_point(self.db, self.class_row, self.user_id, answer_id,
                                           knowledge_point, basis)

    def save_score(self, answer_id, score, state=grading.SCORE_DRAFT, steps=None,
                   revision_reason="", confirm=False, question_no=None):
        return grading.save_score(self.db, self.class_row, self.user_id, answer_id, score,
                                  state=state, steps=steps, revision_reason=revision_reason,
                                  confirm=confirm, question_no=question_no)

    def retract_score(self, answer_id, reason="", question_no=None):
        return grading.retract_score(self.db, self.class_row, self.user_id, answer_id, reason,
                                     question_no)

    def update_rubric(self, batch, question_no, text, rubric_version, full_score=None):
        return grading.update_rubric(self.db, self.class_row, batch, self.user_id, question_no,
                                     {"text": text}, rubric_version, full_score)

    # -- 图片 ---------------------------------------------------------------
    def image_path(self, row):
        """图片路径：绝对路径或 uploads 目录下的相对路径；文件缺失时返回原路径（查看器会说读不出来）。"""
        path = str((row or {}).get("file_path") or "")
        if not path:
            return ""
        if os.path.isabs(path) and os.path.exists(path):
            return path
        if self.uploads_dir:
            candidate = os.path.join(self.uploads_dir, path)
            if os.path.exists(candidate):
                return candidate
        return path


def _is_backend(obj):
    return all(hasattr(obj, name) for name in ("queue", "detail", "verify", "save_score"))


class ServiceGradingBackend(GradingBackend):
    """走 `DesktopService` 的 R2-02 方法（`class_id` 作第一参数，归属闸门在 service 入口）。

    captain 在 `service.py` 里登记的真实签名（b45c611）：
        owned_class_row(class_id) / grading_queue(class_id, batch, scope, answer_ids)
        grading_progress / grading_scoring_progress / grading_batch_verify / grading_retry
        grading_detail(class_id, answer_id, question_no=None)
        grading_confirm_cause / grading_retract_cause / grading_save_score / grading_retract_score
        grading_update_rubric(class_id, batch, question_no, text, rubric_version, full_score=None)
        grading_score_sheet(class_id, batch, question_no="")
    写操作一律走 service（不绕过归属校验）；读作答正文/图片/评分标准这几样 service 的
    `grading_detail` 没带，就用同一个 db 按 `class_id` 收口读（与 grading 内部同一判据）。
    """

    def __init__(self, service, class_row):
        super().__init__(getattr(service, "db", None), class_row,
                         (getattr(service, "user", None) or {}).get("id")
                         if isinstance(getattr(service, "user", None), dict) else None,
                         _uploads_dir(service))
        self.service = service
        self.class_id = (class_row or {}).get("id")

    # -- 只读 ---------------------------------------------------------------
    def queue(self, batch="", scope=grading.SCOPE_PENDING, answer_ids=None):
        return self.service.grading_queue(self.class_id, batch, scope, answer_ids)

    def progress(self, batch=""):
        return self.service.grading_progress(self.class_id, batch)

    def scoring_progress(self, batch):
        return self.service.grading_scoring_progress(self.class_id, batch)

    def score_sheet(self, batch):
        return self.service.grading_score_sheet(self.class_id, batch)

    def detail(self, answer_id, question_no=None):
        payload = self.service.grading_detail(self.class_id, answer_id, question_no) or {}
        answer = self.db.query_one(
            "SELECT a.*, s.name AS roster_name FROM answers a"
            " LEFT JOIN students s ON s.id = a.student_id"
            " WHERE a.id=? AND a.class_id=?", (answer_id, self.class_id))
        if answer is None:
            raise grading.GradingError("找不到这份作答（可能已被删除），或者它不属于当前班级。",
                                       404, "answer_not_found")
        answer = dict(answer)
        answer["student_name"] = answer.get("student_name") or answer.get("roster_name") or ""
        dims = payload.get("dimensions") or grading.answer_dimensions(
            self.db, self.class_row, answer_id)["dimensions"]
        config = db_mod.load_batch_config(self.db, self.class_id, answer["batch"] or "")
        question = str(question_no or answer.get("question_no") or "")
        entry = grading.question_rubric(config, question)
        images = [dict(row) for row in self.db.query(
            "SELECT * FROM answer_images WHERE answer_id=? AND class_id=?"
            " ORDER BY seq ASC, id ASC", (answer_id, self.class_id))]
        return {
            "answer": answer,
            "dimensions": dims,
            "images": images,
            "score": payload.get("score") or grading.score_row(
                self.db, self.class_row, answer_id, question),
            "candidates": payload.get("candidates") or grading.cause_candidates(
                self.db, self.class_row, answer_id),
            "mistake": payload.get("mistake"),
            "rubric": entry,
            "question_no": question,
            "batch": answer["batch"] or "",
        }

    # -- 写（全部经 service，带归属闸门）------------------------------------
    def verify(self, batch="", scope=grading.SCOPE_PENDING, answer_ids=None,
               on_result=None, cancel_check=None):
        return self.service.grading_batch_verify(self.class_id, batch, scope, answer_ids,
                                                 on_result=on_result,
                                                 cancel_check=cancel_check)

    def retry(self, answer_ids, on_result=None, cancel_check=None):
        return self.service.grading_retry(self.class_id, answer_ids,
                                          on_result=on_result, cancel_check=cancel_check)

    def confirm_cause(self, answer_id, cause_label, cause_code="", basis_note="",
                      knowledge_point=""):
        return self.service.grading_confirm_cause(
            self.class_id, answer_id, cause_label, cause_code=cause_code,
            basis_note=basis_note, knowledge_point=knowledge_point)

    def retract_cause(self, answer_id, reason=""):
        return self.service.grading_retract_cause(self.class_id, answer_id, reason)

    def save_score(self, answer_id, score, state=grading.SCORE_DRAFT, steps=None,
                   revision_reason="", confirm=False, question_no=None):
        return self.service.grading_save_score(
            self.class_id, answer_id, score, state=state, steps=steps,
            revision_reason=revision_reason, confirm=confirm, question_no=question_no)

    def retract_score(self, answer_id, reason="", question_no=None):
        return self.service.grading_retract_score(self.class_id, answer_id, reason, question_no)

    def update_rubric(self, batch, question_no, text, rubric_version, full_score=None):
        return self.service.grading_update_rubric(self.class_id, batch, question_no, text,
                                                  rubric_version, full_score)


def _uploads_dir(service):
    uploads = getattr(service, "uploads", None)
    for attr in ("root", "path", "folder", "directory"):
        value = getattr(uploads, attr, "")
        if value:
            return value
    return ""



# ---------------------------------------------------------------------------
# 多图查看
# ---------------------------------------------------------------------------


class ImagePreviewDialog(QDialog):
    """一条作答的多张图片查看：上一张/下一张 + 第几张（不看图，只显示原图）。"""

    def __init__(self, paths, parent=None, index=0, title="作答图片"):
        super().__init__(parent)
        self.paths = [path for path in (paths or []) if path]
        self.index = max(0, min(int(index or 0), max(len(self.paths) - 1, 0)))
        self.setObjectName("gradingImageDialog")
        self.setWindowTitle(title)
        self.resize(720, 640)

        self.image_label = QLabel()
        self.image_label.setObjectName("gradingImageCanvas")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(480, 420)
        self.position_label = QLabel("")
        self.position_label.setObjectName("gradingImagePosition")
        self.prev_button = QPushButton("上一张")
        self.prev_button.setObjectName("gradingImagePrev")
        self.prev_button.clicked.connect(self.prev)
        self.next_button = QPushButton("下一张")
        self.next_button.setObjectName("gradingImageNext")
        self.next_button.clicked.connect(self.next)
        self.close_button = QPushButton("关闭")
        self.close_button.setObjectName("gradingImageClose")
        self.close_button.clicked.connect(self.accept)

        tools = QHBoxLayout()
        tools.addWidget(self.prev_button)
        tools.addWidget(self.next_button)
        tools.addWidget(self.position_label, 1)
        tools.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.image_label)
        layout.addWidget(area, 1)
        layout.addLayout(tools)
        self.render()

    def count(self):
        return len(self.paths)

    def render(self):
        if not self.paths:
            self.image_label.setText("这条作答没有关联图片。")
            self.position_label.setText("")
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        path = self.paths[self.index]
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.image_label.setText("图片文件读不出来：%s" % os.path.basename(path))
        else:
            self.image_label.setPixmap(pixmap.scaled(680, 560, Qt.KeepAspectRatio,
                                                     Qt.SmoothTransformation))
        self.position_label.setText("第 %d / %d 张" % (self.index + 1, len(self.paths)))
        self.prev_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < len(self.paths) - 1)

    def next(self):
        if self.index < len(self.paths) - 1:
            self.index += 1
            self.render()

    def prev(self):
        if self.index > 0:
            self.index -= 1
            self.render()


# ---------------------------------------------------------------------------
# 批量核验：后台线程
# ---------------------------------------------------------------------------


class VerifyWorker(QThread):
    """整批核验放到后台：逐条结果回主线程刷新进度，取消只停未处理的记录。"""

    item_done = Signal(object)
    job_done = Signal(object)

    def __init__(self, backend, batch="", scope=grading.SCOPE_PENDING, answer_ids=None,
                 parent=None):
        super().__init__(parent)
        self.backend = backend
        self.batch = batch
        self.scope = scope
        self.answer_ids = answer_ids
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def cancelled(self):
        return self._cancelled

    def run(self):
        try:
            summary = self.backend.verify(
                self.batch, scope=self.scope, answer_ids=self.answer_ids,
                on_result=self.item_done.emit, cancel_check=self.cancelled)
        except Exception as exc:  # noqa: BLE001 — 整批入口不许把异常漏出线程
            summary = {"total": 0, "processed": 0, "verified": 0, "skipped": 0, "failed": 1,
                       "cancelled": False, "remaining": [], "failed_ids": [], "results": [],
                       "error": error_text(exc)}
        self.job_done.emit(summary)


# ---------------------------------------------------------------------------
# 批量核验面板
# ---------------------------------------------------------------------------


class BatchVerifyPanel(QWidget):
    """选批次/范围 → 看进度 → 取消未处理 / 重试失败 / 打开单条核对。"""

    answer_selected = Signal(int)

    COLUMNS = ["#", "学生", "题号", "作答", "核验", "错因", "评分", "说明"]

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.backend = window.backend
        self.rows = []
        self.last_summary = None
        self.worker = None
        self._build()
        self.refresh()

    # -- 构建 ---------------------------------------------------------------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        frame, card = self._card("① 选择范围并开始核验")
        row = QHBoxLayout()
        self.batch_combo = QComboBox()
        self.batch_combo.setObjectName("gradingBatchCombo")
        self.batch_combo.currentIndexChanged.connect(lambda _=0: self.refresh())
        self.scope_combo = QComboBox()
        self.scope_combo.setObjectName("gradingScopeCombo")
        for key, label in SCOPE_OPTIONS:
            self.scope_combo.addItem(label, key)
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        self.ids_edit = QLineEdit()
        self.ids_edit.setObjectName("gradingIdsEdit")
        self.ids_edit.setPlaceholderText("例如：12,13,14（只核验这几份）")
        self.ids_edit.setVisible(False)
        self.refresh_button = QPushButton("刷新队列")
        self.refresh_button.setObjectName("gradingRefresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.start_button = QPushButton("开始核验")
        self.start_button.setObjectName("gradingStart")
        theme.primary(self.start_button)
        self.start_button.clicked.connect(self.start)
        self.cancel_button = QPushButton("取消未处理")
        self.cancel_button.setObjectName("gradingCancel")
        theme.danger(self.cancel_button)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.retry_button = QPushButton("重试失败")
        self.retry_button.setObjectName("gradingRetry")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.retry)
        for widget in (QLabel("作业批次"), self.batch_combo, QLabel("核验范围"),
                       self.scope_combo, self.ids_edit, self.refresh_button):
            row.addWidget(widget)
        row.addStretch(1)
        for widget in (self.start_button, self.cancel_button, self.retry_button):
            row.addWidget(widget)
        card.addLayout(row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("gradingProgressBar")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.summary_label = QLabel("还没有开始核验。")
        self.summary_label.setObjectName("gradingSummary")
        self.summary_label.setWordWrap(True)
        card.addWidget(self.progress_bar)
        card.addWidget(self.summary_label)
        layout.addWidget(frame)

        frame2, card2 = self._card("② 逐条结果（双击任一打开单条批改）")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setObjectName("gradingQueueTable")
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.doubleClicked.connect(self._open_selected)
        self.open_button = QPushButton("打开选中这条做人工核对")
        self.open_button.setObjectName("gradingOpenDetail")
        self.open_button.clicked.connect(self._open_selected)
        card2.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.open_button)
        card2.addLayout(buttons)
        layout.addWidget(frame2, 1)

        frame3, card3 = self._card("③ 运行日志")
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("gradingLog")
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("核验开始后，这里逐条显示结果。")
        self.log_view.setMaximumHeight(150)
        card3.addWidget(self.log_view)
        layout.addWidget(frame3)

    @staticmethod
    def _card(title):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
        return frame, layout

    def _scope_changed(self, *_args):
        scope = self.scope_combo.currentData()
        self.ids_edit.setVisible(scope == grading.SCOPE_IDS)
        self.refresh()

    # -- 数据 ---------------------------------------------------------------
    def current_batch(self):
        data = self.batch_combo.currentData()
        return data or ""

    def reload_batches(self):
        current = self.current_batch()
        self.batch_combo.blockSignals(True)
        self.batch_combo.clear()
        self.batch_combo.addItem("全部批次", "")
        for item in self.backend.batches():
            label = item["name"]
            if item.get("config_state") != "configured":
                label += "（未配置满分）"
            self.batch_combo.addItem(label, item["batch"])
        index = max(0, self.batch_combo.findData(current))
        self.batch_combo.setCurrentIndex(index)
        self.batch_combo.blockSignals(False)

    def scope(self):
        return self.scope_combo.currentData() or grading.SCOPE_PENDING

    def answer_ids(self):
        if self.scope() != grading.SCOPE_IDS:
            return None
        values = []
        for chunk in (self.ids_edit.text() or "").replace("，", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                values.append(int(chunk))
            except ValueError:
                continue
        return values

    def refresh(self):
        self.reload_batches()
        try:
            self.rows = self.backend.queue(self.current_batch(), self.scope(),
                                           self.answer_ids())
        except Exception as exc:  # noqa: BLE001 — 读取失败也要在页面上说清
            self.rows = []
            self.window.show_error(exc)
        self.fill_table()
        self.update_summary_preview()
        return self.rows

    def fill_table(self):
        self.table.setRowCount(0)
        for index, row in enumerate(self.rows, 1):
            self.table.insertRow(self.table.rowCount())
            dims = row["dimensions"]
            cells = [
                str(index),
                row.get("student_name") or row.get("student_code") or "—",
                row.get("question_no") or "—",
                ANSWER_STATE_TEXT.get(dims["answer"]["state"], dims["answer"]["state"]),
                VERIFY_STATE_TEXT.get(dims["verify"]["state"], dims["verify"]["state"]),
                CAUSE_STATE_TEXT.get(dims["cause"]["state"], dims["cause"]["state"]),
                SCORE_STATE_TEXT.get(dims["score"]["state"], dims["score"]["state"]),
                self._note(row),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, row["answer_id"])
                self.table.setItem(self.table.rowCount() - 1, column, item)

    @staticmethod
    def _note(row):
        reasons = row.get("protected") or []
        if reasons:
            labels = {"manual_verification": "人工核验过", "cause_decided": "已判错因",
                      "score_confirmed": "已有确认成绩"}
            return "批量核验会跳过：" + "、".join(labels.get(item, item) for item in reasons)
        dims = row["dimensions"]
        if dims["verify"]["state"] == "unverified":
            return "待核验"
        if dims["verify"]["state"] == "stale":
            return "作答已更新，旧结论失效，需重核验"
        return "已核验，可打开单条批改"

    def update_summary_preview(self):
        total = len(self.rows)
        pending = sum(1 for row in self.rows
                      if row["dimensions"]["verify"]["state"] in ("unverified", "stale"))
        self.summary_label.setText(
            "本次待处理 %d 条（待核验 %d 条）。点「开始核验」逐条跑本地规则核验；"
            "已有人工核验 / 已判错因 / 已确认成绩的记录会自动跳过。" % (total, pending))
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m")

    # -- 执行 ---------------------------------------------------------------
    def start(self):
        if self.worker is not None and self.worker.isRunning():
            return None
        batch = self.current_batch()
        scope = self.scope()
        ids = self.answer_ids()
        if scope == grading.SCOPE_IDS and not ids:
            self.window.show_error_text("指定记录模式要先填作答编号（例如 12,13）。")
            return None
        rows = self.backend.queue(batch, scope, ids)
        if not rows:
            self.window.show_error_text("这条范围里没有需要核验的记录。")
            return None
        self.log_view.clear()
        self._log("开始核验：范围「%s」，共 %d 条。" % (dict(SCOPE_OPTIONS).get(scope, scope),
                                                     len(rows)))
        self.progress_bar.setRange(0, len(rows))
        self.progress_bar.setValue(0)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.retry_button.setEnabled(False)
        self.worker = VerifyWorker(self.backend, batch, scope, ids, parent=self)
        self.worker.item_done.connect(self._on_item)
        self.worker.job_done.connect(self._on_done)
        self.worker.finished.connect(self._on_thread_finished)
        self.worker.start()
        return self.worker

    def run_verify_sync(self):
        """同步跑一遍（测试与「没有事件循环」的入口用）：返回汇总。"""
        batch, scope, ids = self.current_batch(), self.scope(), self.answer_ids()
        self.progress_bar.setRange(0, max(len(self.backend.queue(batch, scope, ids)), 1))
        self.progress_bar.setValue(0)
        summary = self.backend.verify(batch, scope=scope, answer_ids=ids,
                                      on_result=self._on_item)
        self._on_done(summary)
        return summary

    def cancel(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self._log("已请求取消：正在处理的这一条会保存，剩下的留作未处理。")
            self.cancel_button.setEnabled(False)
            return True
        self.window.show_error_text("当前没有正在运行的核验。")
        return False

    def retry(self):
        failed = (self.last_summary or {}).get("failed_ids") or []
        if not failed:
            self.window.show_error_text("没有失败的记录可重试。")
            return None
        self._log("重试 %d 条失败记录…" % len(failed))
        self.progress_bar.setRange(0, len(failed))
        self.progress_bar.setValue(0)
        summary = self.backend.retry(failed, on_result=self._on_item)
        self._on_done(summary)
        return summary

    def _on_item(self, result):
        index = result.get("index") or 0
        self.progress_bar.setValue(min(index, self.progress_bar.maximum()))
        status_text = {"verified": "已核验", "skipped": "已跳过", "failed": "失败"}.get(
            result.get("status"), result.get("status"))
        detail = result.get("message") or ""
        if result.get("status") == "verified":
            locate = result.get("locate_status") or "—"
            detail = "定位结论：%s" % LOCATE_STATUS_TEXT.get(locate, locate)
        elif result.get("status") == "skipped":
            labels = {"manual_verification": "人工核验过", "cause_decided": "已判错因",
                      "score_confirmed": "已有确认成绩"}
            detail = "、".join(labels.get(item, item) for item in (result.get("reasons") or []))
        elif detail:
            detail = "%s%s" % (("[%s] " % result.get("code")) if result.get("code") else "",
                               detail)
        self._log("第 %d 条（作答 %s）：%s%s" % (
            index, result.get("answer_id"), status_text, (" · " + detail) if detail else ""))
        self.refresh_rows(keep_log=True)

    def _on_done(self, summary):
        self.last_summary = summary
        total = summary.get("total", 0)
        verified = summary.get("verified", 0)
        skipped = summary.get("skipped", 0)
        failed = summary.get("failed", 0)
        cancelled = summary.get("cancelled")
        remaining = len(summary.get("remaining") or [])
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(summary.get("processed", 0))
        self.summary_label.setText(
            "本次共 %d 条：已核验 %d、跳过 %d、失败 %d%s。%s" % (
                total, verified, skipped, failed,
                "、取消后剩余 %d 条未处理" % remaining if cancelled else "",
                ("整批出错：%s" % summary.get("error")) if summary.get("error") else
                ("失败的可点「重试失败」。" if failed else "全部处理完毕。")))
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.retry_button.setEnabled(bool(summary.get("failed_ids")))
        if cancelled:
            self._log("已取消：未处理的 %d 条保持原状。" % remaining)
        self.refresh_rows(keep_log=True)

    def _on_thread_finished(self):
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def refresh_rows(self, keep_log=False):
        try:
            self.rows = self.backend.queue(self.current_batch(), self.scope(),
                                           self.answer_ids())
        except Exception as exc:  # noqa: BLE001
            self.window.show_error(exc)
            return
        self.fill_table()

    def _log(self, text):
        self.log_view.appendPlainText(text)

    # -- 交互 ---------------------------------------------------------------
    def _open_selected(self, *_args):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            self.window.show_error_text("请先在列表里选中一条作答。")
            return
        answer_id = self.rows[row]["answer_id"]
        self.answer_selected.emit(answer_id)


# ---------------------------------------------------------------------------
# 单条批改面板
# ---------------------------------------------------------------------------


class AnswerReviewPanel(QWidget):
    """题目/标准解法 → 学生步骤与图片 → 核验结论与错因 → 教师评分（三区分栏）。"""

    COLUMNS = ["步骤", "扣分说明", "扣分值", "备注"]

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.backend = window.backend
        self.answer_id = None
        self.detail = None
        self.image_buttons = []
        self.step_rows = []
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setSpacing(10)

        left = QVBoxLayout()
        self.answer_list = QListWidget()
        self.answer_list.setObjectName("gradingAnswerList")
        self.answer_list.setMinimumWidth(220)
        self.answer_list.currentRowChanged.connect(self._list_changed)
        left.addWidget(QLabel("作答列表"))
        left.addWidget(self.answer_list, 1)
        self.reload_button = QPushButton("刷新列表")
        self.reload_button.setObjectName("gradingReloadAnswers")
        self.reload_button.clicked.connect(lambda: self.reload(self.answer_id))
        left.addWidget(self.reload_button)
        layout.addLayout(left)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        right = QWidget()
        self.right_layout = QVBoxLayout(right)
        self.right_layout.setSpacing(10)
        scroll.setWidget(right)
        layout.addWidget(scroll, 1)

        # ① 题目与标准解法
        frame, card = self._card("① 题目与标准解法")
        self.question_label = QLabel("—")
        self.question_label.setObjectName("gradingQuestionText")
        self.question_label.setWordWrap(True)
        self.question_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.solution_view = QPlainTextEdit()
        self.solution_view.setObjectName("gradingSolution")
        self.solution_view.setReadOnly(True)
        self.solution_view.setMaximumHeight(80)
        self.rubric_label = QLabel("—")
        self.rubric_label.setObjectName("gradingRubricText")
        self.rubric_label.setWordWrap(True)
        card.addWidget(QLabel("题目"))
        card.addWidget(self.question_label)
        card.addWidget(QLabel("标准解法"))
        card.addWidget(self.solution_view)
        card.addWidget(QLabel("本题评分标准"))
        card.addWidget(self.rubric_label)
        # 题级标准编辑（按题显示该题版本；改正文必须同时改版本号）
        rubric_row = QHBoxLayout()
        self.rubric_edit = QPlainTextEdit()
        self.rubric_edit.setObjectName("gradingRubricEdit")
        self.rubric_edit.setMaximumHeight(60)
        self.rubric_edit.setPlaceholderText("这一题的评分标准正文（可改）")
        self.rubric_version_edit = QLineEdit()
        self.rubric_version_edit.setObjectName("gradingRubricVersion")
        self.rubric_version_edit.setPlaceholderText("新版本号，例如 q3-2026-11")
        self.rubric_full_edit = QLineEdit()
        self.rubric_full_edit.setObjectName("gradingRubricFullScore")
        self.rubric_full_edit.setPlaceholderText("满分（可留空不改）")
        self.rubric_full_edit.setMaximumWidth(110)
        self.save_rubric_button = QPushButton("保存本题标准")
        self.save_rubric_button.setObjectName("gradingSaveRubric")
        self.save_rubric_button.clicked.connect(self.save_rubric)
        rubric_row.addWidget(self.rubric_edit, 1)
        rubric_row.addWidget(self.rubric_version_edit)
        rubric_row.addWidget(self.rubric_full_edit)
        rubric_row.addWidget(self.save_rubric_button)
        card.addLayout(rubric_row)
        self.rubric_message = QLabel("")
        self.rubric_message.setObjectName("gradingRubricMessage")
        self.rubric_message.setWordWrap(True)
        card.addWidget(self.rubric_message)
        self.right_layout.addWidget(frame)

        # ② 学生原始步骤与图片
        frame, card = self._card("② 学生原始步骤与关联图片")
        self.steps_view = QPlainTextEdit()
        self.steps_view.setObjectName("gradingSteps")
        self.steps_view.setReadOnly(True)
        self.steps_view.setMinimumHeight(140)
        card.addWidget(self.steps_view)
        self.images_label = QLabel("—")
        self.images_label.setObjectName("gradingImagesCount")
        self.images_row = QHBoxLayout()
        self.images_row.addWidget(self.images_label)
        self.images_row.addStretch(1)
        self.view_images_button = QPushButton("查看全部图片")
        self.view_images_button.setObjectName("gradingViewImages")
        self.view_images_button.clicked.connect(lambda: self.open_images(0))
        self.images_row.addWidget(self.view_images_button)
        card.addLayout(self.images_row)
        self.right_layout.addWidget(frame)

        # ③ 核验结论 / 错因 / 知识点
        frame, card = self._card("③ 数学核验结论 · 错因判断 · 知识点")
        self.locate_label = QLabel("—")
        self.locate_label.setObjectName("gradingLocateStatus")
        self.locate_label.setWordWrap(True)
        self.locate_evidence_label = QLabel("—")
        self.locate_evidence_label.setObjectName("gradingLocateEvidence")
        self.locate_evidence_label.setWordWrap(True)
        self.locate_evidence_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card.addWidget(QLabel("核验结论"))
        card.addWidget(self.locate_label)
        card.addWidget(QLabel("首个可确定错误步骤（原文证据）"))
        card.addWidget(self.locate_evidence_label)
        card.addWidget(QLabel("候选错因（本地规则，不外发）"))
        self.candidate_list = QListWidget()
        self.candidate_list.setObjectName("gradingCandidates")
        self.candidate_list.setMaximumHeight(90)
        self.candidate_list.itemClicked.connect(self._pick_candidate)
        card.addWidget(self.candidate_list)
        cause_row = QGridLayout()
        self.cause_label_edit = QLineEdit()
        self.cause_label_edit.setObjectName("gradingCauseLabel")
        self.cause_label_edit.setPlaceholderText("原因说明（必填），例如：移项时符号写错")
        self.cause_code_edit = QLineEdit()
        self.cause_code_edit.setObjectName("gradingCauseCode")
        self.cause_code_edit.setPlaceholderText("原因编码（可留空）")
        self.basis_edit = QPlainTextEdit()
        self.basis_edit.setObjectName("gradingBasis")
        self.basis_edit.setPlaceholderText("判断依据（人工核验必填），例如：作业本原图第 2 行是 −2")
        self.basis_edit.setMaximumHeight(60)
        self.confirm_cause_button = QPushButton("确认错因并同步错题")
        self.confirm_cause_button.setObjectName("gradingConfirmCause")
        theme.primary(self.confirm_cause_button)
        self.confirm_cause_button.clicked.connect(self.confirm_cause)
        self.retract_cause_button = QPushButton("撤回错因判断")
        self.retract_cause_button.setObjectName("gradingRetractCause")
        theme.danger(self.retract_cause_button)
        self.retract_cause_button.clicked.connect(self.retract_cause)
        cause_row.addWidget(QLabel("原因说明"), 0, 0)
        cause_row.addWidget(self.cause_label_edit, 0, 1)
        cause_row.addWidget(QLabel("原因编码"), 1, 0)
        cause_row.addWidget(self.cause_code_edit, 1, 1)
        card.addLayout(cause_row)
        card.addWidget(self.basis_edit)
        cause_buttons = QHBoxLayout()
        cause_buttons.addStretch(1)
        cause_buttons.addWidget(self.retract_cause_button)
        cause_buttons.addWidget(self.confirm_cause_button)
        card.addLayout(cause_buttons)
        knowledge_row = QHBoxLayout()
        self.knowledge_edit = QLineEdit()
        self.knowledge_edit.setObjectName("gradingKnowledge")
        self.knowledge_edit.setPlaceholderText("知识点，例如：一元一次方程·移项")
        self.knowledge_basis_edit = QLineEdit()
        self.knowledge_basis_edit.setObjectName("gradingKnowledgeBasis")
        self.knowledge_basis_edit.setPlaceholderText("分类依据（可留空）")
        self.knowledge_button = QPushButton("保存知识点")
        self.knowledge_button.setObjectName("gradingSaveKnowledge")
        self.knowledge_button.clicked.connect(self.save_knowledge)
        knowledge_row.addWidget(QLabel("知识点"))
        knowledge_row.addWidget(self.knowledge_edit, 1)
        knowledge_row.addWidget(self.knowledge_basis_edit, 1)
        knowledge_row.addWidget(self.knowledge_button)
        card.addLayout(knowledge_row)
        self.mistake_label = QLabel("—")
        self.mistake_label.setObjectName("gradingMistakeState")
        self.mistake_label.setWordWrap(True)
        card.addWidget(self.mistake_label)
        self.cause_message = QLabel("")
        self.cause_message.setObjectName("gradingCauseMessage")
        self.cause_message.setWordWrap(True)
        card.addWidget(self.cause_message)
        self.right_layout.addWidget(frame)

        # ④ 教师评分
        frame, card = self._card("④ 教师评分（满分以批次配置为准）")
        head = QHBoxLayout()
        self.full_score_label = QLabel("满分：—")
        self.full_score_label.setObjectName("gradingFullScore")
        self.score_status_label = QLabel("评分状态：—")
        self.score_status_label.setObjectName("gradingScoreStatus")
        head.addWidget(self.full_score_label)
        head.addWidget(self.score_status_label)
        head.addStretch(1)
        card.addLayout(head)
        score_row = QHBoxLayout()
        self.score_edit = QLineEdit()
        self.score_edit.setObjectName("gradingScore")
        self.score_edit.setPlaceholderText("分数，例如 7.5（0–满分，最多两位小数）")
        self.score_edit.setMaximumWidth(200)
        self.confirm_checkbox_label = QLabel("勾选「确认成绩」才计入正式统计")
        self.confirm_checkbox_label.setObjectName("gradingConfirmHint")
        self.save_draft_button = QPushButton("存草稿")
        self.save_draft_button.setObjectName("gradingSaveDraft")
        self.save_draft_button.clicked.connect(lambda: self.save_score("draft"))
        self.confirm_button = QPushButton("确认成绩")
        self.confirm_button.setObjectName("gradingConfirmScore")
        theme.primary(self.confirm_button)
        self.confirm_button.clicked.connect(lambda: self.save_score("confirmed"))
        self.retract_score_button = QPushButton("撤回成绩")
        self.retract_score_button.setObjectName("gradingRetractScore")
        theme.danger(self.retract_score_button)
        self.retract_score_button.clicked.connect(self.retract_score)
        score_row.addWidget(QLabel("分数"))
        score_row.addWidget(self.score_edit)
        score_row.addWidget(self.confirm_checkbox_label)
        score_row.addStretch(1)
        for widget in (self.save_draft_button, self.confirm_button, self.retract_score_button):
            score_row.addWidget(widget)
        card.addLayout(score_row)

        card.addWidget(QLabel("步骤扣分项（满分 − Σ扣分 必须等于分数）"))
        self.steps_table = QTableWidget(0, len(self.COLUMNS))
        self.steps_table.setObjectName("gradingStepsTable")
        self.steps_table.setHorizontalHeaderLabels(self.COLUMNS)
        self.steps_table.verticalHeader().setVisible(False)
        self.steps_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.steps_table.setMinimumHeight(120)
        card.addWidget(self.steps_table)
        step_buttons = QHBoxLayout()
        self.add_step_button = QPushButton("添加扣分项")
        self.add_step_button.setObjectName("gradingAddStep")
        self.add_step_button.clicked.connect(lambda: self.add_step_row())
        self.remove_step_button = QPushButton("删除选中扣分项")
        self.remove_step_button.setObjectName("gradingRemoveStep")
        self.remove_step_button.clicked.connect(self.remove_step_row)
        self.steps_hint_label = QLabel("")
        self.steps_hint_label.setObjectName("gradingStepsHint")
        self.steps_hint_label.setWordWrap(True)
        step_buttons.addWidget(self.add_step_button)
        step_buttons.addWidget(self.remove_step_button)
        step_buttons.addWidget(self.steps_hint_label, 1)
        card.addLayout(step_buttons)
        reason_row = QHBoxLayout()
        self.reason_edit = QLineEdit()
        self.reason_edit.setObjectName("gradingRevisionReason")
        self.reason_edit.setPlaceholderText("修改理由（改已确认的分数时必填）")
        reason_row.addWidget(QLabel("修改理由"))
        reason_row.addWidget(self.reason_edit, 1)
        card.addLayout(reason_row)
        self.score_message = QLabel("")
        self.score_message.setObjectName("gradingScoreMessage")
        self.score_message.setWordWrap(True)
        card.addWidget(self.score_message)
        self.right_layout.addWidget(frame)
        self.right_layout.addStretch(1)
        self.score_edit.textChanged.connect(self.update_steps_hint)
        self.current_label = QLabel("还没有选择作答。")
        self.current_label.setObjectName("gradingCurrentAnswer")
        self.current_label.setWordWrap(True)
        self.right_layout.insertWidget(0, self.current_label)

    @staticmethod
    def _card(title):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
        return frame, layout

    # -- 列表 ---------------------------------------------------------------
    def reload(self, answer_id=None, batch=None):
        """按批次重载左侧作答列表（默认沿用当前批次/当前选中）。"""
        batch = batch if batch is not None else (self.detail or {}).get("batch") or ""
        if not batch:
            batch = self.window.verify_panel.current_batch()
        if not batch:
            batch = self.backend.batches()[0]["batch"] if self.backend.batches() else ""
        self.answer_list.blockSignals(True)
        self.answer_list.clear()
        try:
            rows = self.backend.queue(batch, grading.SCOPE_ALL)
        except Exception as exc:  # noqa: BLE001
            rows = []
            self.window.show_error(exc)
        for row in rows:
            item = QListWidgetItem("%s · 题%s · %s" % (
                row.get("student_name") or row.get("student_code") or "—",
                row.get("question_no") or "-",
                SCORE_STATE_TEXT.get(row["dimensions"]["score"]["state"], "")))
            item.setData(Qt.UserRole, row["answer_id"])
            self.answer_list.addItem(item)
        self.answer_list.blockSignals(False)
        if answer_id is not None:
            self.load(answer_id)
        elif rows:
            self.load(rows[0]["answer_id"])

    def _list_changed(self, index):
        if index < 0:
            return
        item = self.answer_list.item(index)
        if item is not None:
            self.load(item.data(Qt.UserRole))

    # -- 载入 ---------------------------------------------------------------
    def load(self, answer_id):
        try:
            detail = self.backend.detail(answer_id)
        except Exception as exc:  # noqa: BLE001
            self.window.show_error(exc)
            return None
        self.answer_id = answer_id
        self.detail = detail
        self._sync_list_selection(answer_id)
        answer = detail["answer"]
        dims = detail["dimensions"]
        self.current_label.setText("%s · 题%s · 批次「%s」｜作答：%s ｜核验：%s ｜错因：%s ｜评分：%s" % (
            answer.get("student_name") or answer.get("student_code") or "—",
            answer.get("question_no") or "-", answer.get("batch") or "-",
            ANSWER_STATE_TEXT.get(dims["answer"]["state"], dims["answer"]["state"]),
            VERIFY_STATE_TEXT.get(dims["verify"]["state"], dims["verify"]["state"]),
            CAUSE_STATE_TEXT.get(dims["cause"]["state"], dims["cause"]["state"]),
            SCORE_STATE_TEXT.get(dims["score"]["state"], dims["score"]["state"])))
        self.question_label.setText(answer.get("question_text") or "（没有题干）")
        self.solution_view.setPlainText(answer.get("standard_solution") or "（没有标准解法）")
        rubric = detail["rubric"]
        self.rubric_label.setText("%s（版本 %s）" % (
            rubric.get("text") or "（这一题还没有配评分标准）",
            rubric.get("rubric_version") or "未配置"))
        self.rubric_edit.setPlainText(rubric.get("text") or "")
        self.rubric_version_edit.clear()
        self.rubric_full_edit.setText(
            "" if rubric.get("full_score") is None else str(rubric.get("full_score")))
        self.steps_view.setPlainText(answer.get("student_steps") or "（学生步骤为空）")
        self._render_images(detail.get("images") or [])
        self._render_locate(dims, detail)
        self._render_candidates(detail.get("candidates") or {})
        self._render_mistake(detail.get("mistake"))
        self._render_score(detail.get("score") or {}, dims, detail)
        self.cause_message.setText("")
        self.score_message.setText("")
        self.rubric_message.setText("")
        return detail

    def _sync_list_selection(self, answer_id):
        for index in range(self.answer_list.count()):
            item = self.answer_list.item(index)
            if item is not None and item.data(Qt.UserRole) == answer_id:
                self.answer_list.blockSignals(True)
                self.answer_list.setCurrentRow(index)
                self.answer_list.blockSignals(False)
                return

    def _render_images(self, images):
        self.images_label.setText(
            "关联图片 %d 张" % len(images) if images else "没有关联图片（仅文字作答）")
        self.view_images_button.setEnabled(bool(images))
        for button in self.image_buttons:
            self.images_row.removeWidget(button)
            button.deleteLater()
        self.image_buttons = []
        for index, row in enumerate(images):
            button = QPushButton("图片 %d" % (row.get("seq") or index + 1))
            button.setObjectName("gradingImageButton")
            button.clicked.connect(lambda _checked=False, at=index: self.open_images(at))
            self.images_row.insertWidget(index + 2, button)
            self.image_buttons.append(button)

    def _render_locate(self, dims, detail):
        verify = dims["verify"]
        locate_status = verify.get("locate_status") or ""
        self.locate_label.setText("核验状态：%s ｜ 定位结论：%s" % (
            VERIFY_STATE_TEXT.get(verify["state"], verify["state"]),
            LOCATE_STATUS_TEXT.get(locate_status, locate_status or "—")))
        candidates = detail.get("candidates") or {}
        locate = candidates.get("locate") or {}
        evidence = locate.get("first_bad_text") or ""
        note = candidates.get("note") or ""
        applicability = candidates.get("applicability") or ""
        parts = []
        if evidence:
            parts.append("原文证据：%s" % evidence)
        if note:
            parts.append(note)
        if applicability:
            parts.append("适用范围：%s" % {"required": "必须查明原因", "optional": "可判可不判",
                                            "forbidden": "没有可归因的错步"}.get(
                applicability, applicability))
        self.locate_evidence_label.setText("\n".join(parts) or "—")

    def _render_candidates(self, candidates):
        self.candidate_list.clear()
        items = candidates.get("candidates") or []
        if not items:
            item = QListWidgetItem("（本地规则没有给出候选错因；可直接写原因说明）")
            item.setFlags(Qt.NoItemFlags)
            self.candidate_list.addItem(item)
            return
        for candidate in items:
            text = "%s｜%s" % (candidate.get("label") or candidate.get("code") or "候选",
                               candidate.get("evidence") or "")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, candidate)
            self.candidate_list.addItem(item)

    def _pick_candidate(self, item):
        candidate = item.data(Qt.UserRole) or {}
        self.cause_label_edit.setText(candidate.get("label") or "")
        self.cause_code_edit.setText(candidate.get("code") or "")

    def _render_mistake(self, mistake):
        if not mistake:
            self.mistake_label.setText("错题：还没有（确认错因后自动同步）")
            self.knowledge_edit.clear()
            return
        points = mistake.get("knowledge_points") or []
        self.mistake_label.setText("错题：%s ｜知识点：%s ｜状态：%s" % (
            mistake.get("source_type") or "—",
            "、".join(points) if points else "未分类（缺依据，待补充）",
            "已确认" if mistake.get("status") == "confirmed" else "已退回"))
        self.knowledge_edit.setText(points[0] if points else "")

    def _render_score(self, score, dims, detail):
        full = score.get("target_total")
        self.full_score_label.setText(
            "满分：%s" % ("—" if full is None else self._fmt(full)))
        state = dims["score"]["state"]
        self.score_status_label.setText("评分状态：%s" % SCORE_STATE_TEXT.get(state, state))
        value = score.get("score")
        self.score_edit.setText("" if value is None else self._fmt(value))
        self.steps_table.setRowCount(0)
        for step in score.get("steps") or []:
            self.add_step_row(step)
        if state == "invalidated":
            self.score_message.setText("这条成绩已失效（需重评）：旧分数与扣分项都留在历史里。")
        self.update_steps_hint()

    @staticmethod
    def _fmt(value):
        if value is None:
            return ""
        number = float(value)
        if abs(number - round(number)) < 1e-9:
            return "%d" % round(number)
        return ("%.2f" % number).rstrip("0").rstrip(".")

    # -- 图片 ---------------------------------------------------------------
    def image_paths(self):
        if not self.detail:
            return []
        return [self.backend.image_path(row) for row in (self.detail.get("images") or [])]

    def open_images(self, index=0):
        paths = self.image_paths()
        if not paths:
            self.window.show_error_text("这条作答没有关联图片。")
            return None
        dialog = ImagePreviewDialog(paths, parent=self.window, index=index,
                                    title="作答图片 · %s" % (
                                        (self.detail or {}).get("answer", {}).get("student_name")
                                        or "学生"))
        self.window.last_image_dialog = dialog
        dialog.show()
        return dialog

    # -- 提示与刷新 ---------------------------------------------------------
    def _refresh_after(self, label, text):
        """先刷新（刷新会重载整条作答并清空提示），再把这次的结果提示写回标签。"""
        self.window.refresh_all(self.answer_id)
        label.setText(text)

    # -- 错因 ---------------------------------------------------------------
    def confirm_cause(self):
        if self.answer_id is None:
            self.window.show_error_text("先选一条作答。")
            return None
        try:
            result = self.backend.confirm_cause(
                self.answer_id,
                cause_label=self.cause_label_edit.text().strip(),
                cause_code=self.cause_code_edit.text().strip(),
                basis_note=self.basis_edit.toPlainText().strip(),
                knowledge_point=self.knowledge_edit.text().strip())
        except Exception as exc:  # noqa: BLE001
            self.cause_message.setText(error_text(exc))
            self.window.show_error(exc)
            return None
        self._refresh_after(self.cause_message,
                            "已确认错因并同步错题；核验与评分保持不变。")
        return result

    def retract_cause(self):
        if self.answer_id is None:
            self.window.show_error_text("先选一条作答。")
            return None
        try:
            result = self.backend.retract_cause(self.answer_id, reason="界面撤回")
        except Exception as exc:  # noqa: BLE001
            self.cause_message.setText(error_text(exc))
            self.window.show_error(exc)
            return None
        self._refresh_after(self.cause_message,
                            "已撤回错因：核验结论与已确认成绩都保留。")
        return result

    def save_knowledge(self):
        if self.answer_id is None:
            self.window.show_error_text("先选一条作答。")
            return None
        try:
            result = self.backend.set_knowledge(self.answer_id,
                                                self.knowledge_edit.text().strip(),
                                                self.knowledge_basis_edit.text().strip())
        except Exception as exc:  # noqa: BLE001
            self.cause_message.setText(error_text(exc))
            self.window.show_error(exc)
            return None
        self._refresh_after(self.cause_message, "知识点已保存（原版学情页面可查询）。")
        return result

    # -- 评分 ---------------------------------------------------------------
    def add_step_row(self, step=None):
        step = step or {}
        row = self.steps_table.rowCount()
        self.steps_table.insertRow(row)
        values = [
            "" if step.get("step_index") is None else str(step.get("step_index")),
            str(step.get("label") or ""),
            "" if step.get("deduction") is None else self._fmt(step.get("deduction")),
            str(step.get("note") or ""),
        ]
        for column, text in enumerate(values):
            self.steps_table.setItem(row, column, QTableWidgetItem(text))
        return row

    def remove_step_row(self):
        row = self.steps_table.currentRow()
        if row < 0:
            self.steps_table.setRowCount(max(self.steps_table.rowCount() - 1, 0))
            return
        self.steps_table.removeRow(row)

    def collect_steps(self):
        steps = []
        for row in range(self.steps_table.rowCount()):
            def text(column):
                item = self.steps_table.item(row, column)
                return (item.text() if item is not None else "").strip()
            index_text = text(0)
            label = text(1)
            deduction_text = text(2)
            if not (index_text or label or deduction_text):
                continue
            step = {"label": label,
                    "deduction": deduction_text or 0,
                    "note": text(3),
                    "step_index": None}
            if index_text:
                try:
                    step["step_index"] = int(index_text)
                except ValueError:
                    step["step_index"] = index_text
            steps.append(step)
        return steps

    def update_steps_hint(self, *_args):
        full = None
        if self.detail:
            full = (self.detail.get("score") or {}).get("target_total")
        steps = self.collect_steps()
        if not steps:
            self.steps_hint_label.setText("没有扣分项时，分数不受「满分−扣分」约束。")
            return
        total = 0.0
        for step in steps:
            try:
                total += float(step.get("deduction") or 0)
            except (TypeError, ValueError):
                self.steps_hint_label.setText("扣分值要填数字。")
                return
        score_text = self.score_edit.text().strip()
        try:
            score = float(score_text) if score_text else None
        except ValueError:
            score = None
        if full is None:
            self.steps_hint_label.setText("这道题还没有配满分，无法核对扣分。")
            return
        expected = round(float(full) - total, 2)
        if score is None:
            self.steps_hint_label.setText("扣分合计 %s，按「满分−扣分」应为 %s。"
                                          % (self._fmt(total), self._fmt(expected)))
        elif abs(score - expected) > 1e-9:
            self.steps_hint_label.setText("不一致：扣分合计 %s，应为 %s（现在填 %s）。"
                                          % (self._fmt(total), self._fmt(expected),
                                             self._fmt(score)))
        else:
            self.steps_hint_label.setText("一致：满分 %s − 扣分 %s = %s。"
                                          % (self._fmt(full), self._fmt(total),
                                             self._fmt(expected)))

    def save_score(self, state):
        if self.answer_id is None:
            self.window.show_error_text("先选一条作答。")
            return None
        score_text = self.score_edit.text().strip()
        score = None if score_text == "" else score_text
        try:
            result = self.backend.save_score(
                self.answer_id, score=score, state=state, steps=self.collect_steps(),
                revision_reason=self.reason_edit.text().strip(),
                confirm=(state == grading.SCORE_CONFIRMED))
        except Exception as exc:  # noqa: BLE001
            self.score_message.setText(error_text(exc))
            self.window.show_error(exc)
            return None
        if state == grading.SCORE_CONFIRMED:
            message = "已确认成绩，计入正式统计（平均分/得分率/覆盖率）。"
        else:
            message = "已存草稿：不计入任何正式统计。"
        self._refresh_after(self.score_message, message)
        return result

    def retract_score(self):
        if self.answer_id is None:
            self.window.show_error_text("先选一条作答。")
            return None
        try:
            result = self.backend.retract_score(self.answer_id,
                                                reason=self.reason_edit.text().strip())
        except Exception as exc:  # noqa: BLE001
            self.score_message.setText(error_text(exc))
            self.window.show_error(exc)
            return None
        self._refresh_after(self.score_message,
                            "已撤回成绩：回到未评分，退出正式统计，历史保留。")
        return result

    def save_rubric(self):
        if not self.detail:
            self.window.show_error_text("先选一条作答（批次与题号从这条作答取）。")
            return None
        question = self.detail.get("question_no") or ""
        batch = self.detail.get("batch") or ""
        full_text = self.rubric_full_edit.text().strip()
        try:
            result = self.backend.update_rubric(
                batch, question, self.rubric_edit.toPlainText().strip(),
                self.rubric_version_edit.text().strip(),
                None if full_text == "" else full_text)
        except Exception as exc:  # noqa: BLE001
            self.rubric_message.setText(error_text(exc))
            self.window.show_error(exc)
            return None
        self._refresh_after(
            self.rubric_message,
            "本题标准已更新为版本 %s；只有本题旧成绩失效（%d 条），其它题不受影响。"
            % (result.get("rubric_version"), result.get("invalidated", 0)))
        return result


# ---------------------------------------------------------------------------
# 评分进度面板
# ---------------------------------------------------------------------------


class ScoreProgressPanel(QWidget):
    """按批次看评分进度与覆盖率（分母=应参与评分学生数，与统计同口径）。"""

    COLUMNS = ["题号", "满分", "应评份数", "已确认", "草稿", "未评", "已失效", "平均分", "得分率"]

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.backend = window.backend
        self._build()
        self.refresh()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        head = QHBoxLayout()
        self.batch_combo = QComboBox()
        self.batch_combo.setObjectName("gradingScoreBatchCombo")
        self.batch_combo.currentIndexChanged.connect(lambda _=0: self.refresh())
        self.refresh_button = QPushButton("刷新进度")
        self.refresh_button.setObjectName("gradingScoreRefresh")
        self.refresh_button.clicked.connect(self.refresh)
        head.addWidget(QLabel("作业批次"))
        head.addWidget(self.batch_combo)
        head.addStretch(1)
        head.addWidget(self.refresh_button)
        layout.addLayout(head)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setObjectName("gradingScoreTable")
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.coverage_label = QLabel("—")
        self.coverage_label.setObjectName("gradingCoverage")
        self.average_label = QLabel("—")
        self.average_label.setObjectName("gradingAverage")
        self.complete_label = QLabel("—")
        self.complete_label.setObjectName("gradingCompleteStudents")
        for label in (self.coverage_label, self.average_label, self.complete_label):
            label.setWordWrap(True)
            layout.addWidget(label)

    def current_batch(self):
        return self.batch_combo.currentData() or ""

    def reload_batches(self):
        current = self.current_batch()
        self.batch_combo.blockSignals(True)
        self.batch_combo.clear()
        self.batch_combo.addItem("全部批次", "")
        for item in self.backend.batches():
            self.batch_combo.addItem(item["name"], item["batch"])
        self.batch_combo.setCurrentIndex(max(0, self.batch_combo.findData(current)))
        self.batch_combo.blockSignals(False)

    def refresh(self):
        self.reload_batches()
        batch = self.current_batch()
        rows = []
        if batch:
            try:
                sheet = self.backend.score_sheet(batch)
                rows = sheet.get("questions") or []
                self.window_score_sheet = sheet
            except Exception as exc:  # noqa: BLE001
                self.window.show_error(exc)
        self.table.setRowCount(0)
        for row in rows:
            self.table.insertRow(self.table.rowCount())
            values = [
                row.get("question_no") or "—",
                self._fmt(row.get("full_score")),
                str(row.get("answers") or 0),
                str(row.get("confirmed") or 0),
                str(row.get("draft") or 0),
                str(row.get("unscored") or 0),
                str(row.get("invalidated") or 0),
                "暂无" if row.get("average") is None else self._fmt(row.get("average")),
                "暂无" if row.get("rate") is None else "%.1f%%" % (100.0 * float(row["rate"])),
            ]
            for column, text in enumerate(values):
                self.table.setItem(self.table.rowCount() - 1, column, QTableWidgetItem(text))
        if batch:
            try:
                progress = self.backend.scoring_progress(batch)
            except Exception as exc:  # noqa: BLE001
                progress = {}
                self.window.show_error(exc)
            self.coverage_label.setText(
                "评分覆盖率：%s（完整成绩 %d / 应参与评分 %d 人，分母来源：%s）" % (
                    progress.get("coverage_text") or "无法计算",
                    progress.get("confirmed_students") or 0,
                    progress.get("eligible_students") or 0,
                    "名单" if progress.get("denominator_source") == "roster" else "实际作答"))
            self.average_label.setText("完整成绩平均分：%s" % (
                progress.get("average_text") or "暂无"))
            self.complete_label.setText("作业满分合计：%s（只作展示，不参与平均分/得分率）" % (
                "暂无" if progress.get("full_total") is None else self._fmt(
                    progress.get("full_total"))))
        else:
            self.coverage_label.setText("选择具体批次后显示评分覆盖率。")
            self.average_label.setText("")
            self.complete_label.setText("")
        return rows

    @staticmethod
    def _fmt(value):
        if value is None:
            return "—"
        number = float(value)
        if abs(number - round(number)) < 1e-9:
            return "%d" % round(number)
        return ("%.2f" % number).rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class GradingWindow(QMainWindow):
    """R2-02 主窗口：批量核验 / 单条批改 / 评分进度。"""

    closed = Signal()

    def __init__(self, backend_or_service, class_row=None, parent=None):
        super().__init__(parent)
        if _is_backend(backend_or_service):
            self.backend = backend_or_service
            self.class_row = backend_or_service.class_row
        elif hasattr(backend_or_service, "grading_queue"):
            # captain 在 service.py 登记的 R2-02 方法（第一参数 class_id，归属闸门在 service 内）
            self.class_row = class_row or {}
            self.backend = ServiceGradingBackend(backend_or_service, self.class_row)
        else:
            self.backend = GradingBackend.from_service(backend_or_service, class_row or {})
            self.class_row = class_row or {}
        self.last_error = ""
        self.last_image_dialog = None
        self.setObjectName("gradingWindow")
        self.setWindowTitle("数智伴学 · 作答批量核验与评分")
        self.resize(1220, 820)
        self._build()
        self.verify_panel.refresh()
        self.score_panel.refresh()

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        heading = QLabel("%s · 作答批量核验与教师评分" % (self.class_row.get("name") or ""))
        heading.setObjectName("gradingTitle")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        hint = QLabel(
            "批量核验逐条调用本地数学核验（不调用任何大模型，学生作答不外发）；核验结论、错因、"
            "评分是三条独立的轴：撤回错因不会撤掉核验与成绩，改评分标准只让该题旧成绩失效。")
        hint.setObjectName("gradingHint")
        hint.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(hint)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("gradingTabs")
        self.verify_panel = BatchVerifyPanel(self)
        self.review_panel = AnswerReviewPanel(self)
        self.score_panel = ScoreProgressPanel(self)
        self.tabs.addTab(self.verify_panel, "批量核验")
        self.tabs.addTab(self.review_panel, "单条批改与评分")
        self.tabs.addTab(self.score_panel, "评分进度")
        self.verify_panel.answer_selected.connect(self.open_answer)
        layout.addWidget(self.tabs, 1)

        self.status_label = QLabel("就绪。")
        self.status_label.setObjectName("gradingStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # -- 错误与状态 ---------------------------------------------------------
    def show_error(self, exc):
        text = error_text(exc)
        self.last_error = text
        self.status_label.setText(text)
        return text

    def show_error_text(self, text):
        self.last_error = text
        self.status_label.setText(text)
        return text

    def show_info(self, text):
        self.last_error = ""
        self.status_label.setText(text)
        return text

    # -- 刷新 ---------------------------------------------------------------
    def refresh_all(self, answer_id=None):
        self.verify_panel.refresh_rows(keep_log=True)
        self.score_panel.refresh()
        if answer_id is not None:
            self.review_panel.reload(answer_id)
        else:
            self.review_panel.reload(self.review_panel.answer_id)

    def open_answer(self, answer_id):
        """从批量核验表双击/按钮进来：切到单条批改并载入这条。"""
        self.tabs.setCurrentWidget(self.review_panel)
        self.review_panel.reload(answer_id)
        return answer_id

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        if self.verify_panel.worker is not None and self.verify_panel.worker.isRunning():
            self.verify_panel.worker.cancel()
            self.verify_panel.worker.wait(3000)
        self.closed.emit()
        super().closeEvent(event)


__all__ = [
    "GradingBackend", "ServiceGradingBackend", "GradingWindow", "BatchVerifyPanel",
    "AnswerReviewPanel", "ScoreProgressPanel", "ImagePreviewDialog", "VerifyWorker",
    "GRADING_ERROR_HINTS", "error_hint", "error_text", "SCOPE_OPTIONS",
]
