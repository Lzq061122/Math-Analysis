"""桌面版的账号、班级、学生与错题逻辑入口。

桌面版不开 HTTP 服务：它构造与 app.api 相同的上下文对象，直接调用同一批处理器，
因此密码校验、登录会话、班级、学生与错题归属校验和数据库结构只有一份实现，
桌面版写入的数据网页版可以直接读取，反之亦然。
"""
import base64
import os
import time
import urllib.parse
import uuid

from app import analysis, backup, vision
from app.api import (
    ApiError,
    api_answer_analyse,
    api_answer_review_confirm,
    api_answer_review_get,
    api_answer_review_reject,
    api_answer_review_update,
    api_answer_reviews,
    api_apply_analysis,
    api_answers_import,
    api_answers_list,
    api_answers_preview,
    api_answers_template,
    api_confirm_mistake,
    api_class_stats,
    api_create_class,
    api_create_mistake,
    api_create_student,
    api_discard_preview,
    api_get_class,
    api_get_mistake,
    api_list_classes,
    api_list_mistakes,
    api_login,
    api_logout,
    api_mistake_image,
    api_mistake_list_for_ids,
    api_preview_analysis,
    api_record_analysis_failure,
    api_register,
    api_return_mistake,
    api_student_stats,
    api_student_advice_generate,
    api_student_advice_save,
    api_student_insight,
    api_followup_answer,
    api_followup_create,
    api_followup_decide,
    api_followup_get,
    api_followup_list,
    api_followup_context,
    api_followup_suggest,
    api_followup_update,
    api_update_mistake,
    api_update_student,
    describe_ai,
)
from app.db import (
    Database, add_student_active, ensure_answers_schema, ensure_followup_schema, ensure_tutor_schema,
)
from app.preview import PreviewStore
from app.uploads import UploadStore

# 只看文件头判断图片类型，不信任扩展名。
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _sniff_mime(blob):
    for head, mime in IMAGE_SIGNATURES:
        if blob.startswith(head):
            return mime
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


class Context:
    """与 app.api 处理器约定的上下文字段保持一致。"""

    def __init__(self, db, user=None, token=None, body=None, params=None, query=None, uploads=None,
                 previews=None, session_current=None, ai_config=None, ai_provider=None):
        self.db = db
        self.user = user
        self.token = token
        self.body = dict(body or {})
        self.params = dict(params or {})
        self.query = dict(query or {})
        self.uploads = uploads
        self.previews = previews
        # 长耗时操作结束时用它确认期间没有退出登录或换账号。
        self.session_current = session_current
        self.ai_config = dict(ai_config or {})
        self.ai_provider = ai_provider
        self.client_ip = "127.0.0.1"


class RestoreBlockedError(Exception):
    """数据目录存在恢复异常或待处理任务，拒绝进入。"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _BlockedProvider:
    """上一次请求出现非订阅计费后，后续发送一律在这里停下。"""

    def __init__(self, mode, message):
        self.mode = mode
        self.last_payload = None
        self.message = message

    def analyze(self, job):
        raise vision.AnalysisError(self.message, "billing", False)


class DesktopService:
    def __init__(self, data_dir, test_ai_base=""):
        # 业务入口的恢复保护：发现恢复异常或未处理任务时拒绝打开数据库，
        # 与网页版 server.py 的保护共用同一套状态文件，不能靠界面按钮绕过。
        problem = backup.restore_problem(data_dir)
        if problem:
            raise RestoreBlockedError(problem)
        self.data_dir = data_dir
        # 自动化测试只能显式注入本机回环地址；正式启动永远为空。
        test_base = (test_ai_base or "").strip().rstrip("/")
        if test_base and not vision.is_loopback_base(test_base):
            raise ValueError("测试地址只允许本机回环地址")
        self._test_ai_base = test_base
        os.makedirs(data_dir, exist_ok=True)
        db_file = os.path.join(data_dir, "app.db")
        # 1.3.0 的库没有 students.active：显式迁移（先备份再在事务里加列），
        # 失败时抛 MigrationError，原库不动。
        self.migration_backup = add_student_active(db_file) if os.path.exists(db_file) else None
        # 作答表是后加的结构：同样先备份再显式建表/补列，失败时原库不动。
        self.answers_migration = ensure_answers_schema(db_file) if os.path.exists(db_file) else None
        # 个人辅导建议表是 1.7.0 新增的结构：同样先备份再建表，失败时原库不动。
        self.tutor_migration = ensure_tutor_schema(db_file) if os.path.exists(db_file) else None
        # 追问记录与候选更新表是 1.8.0 新增的结构。
        self.followup_migration = ensure_followup_schema(db_file) if os.path.exists(db_file) else None
        # 打开已有库：create=False，绝不执行建表/补列/补索引/改结构版本。
        self.db = Database(db_file, create=False)
        # 照片与网页版存在同一个 uploads 目录，两边看到的是同一批文件。
        self.uploads = UploadStore(os.path.join(data_dir, "uploads"))
        self.previews = PreviewStore()
        self.user = None
        self.token = None
        # 界面输入的密钥只在本次登录内存里，按服务分开保存，绝不写数据库或配置文件。
        self._ai_keys = {}
        self._ai_key_user_id = None
        # Go 的会话标识：每次登录生成一次，同一次分析与重试复用，退出或换账号后清空。
        self._ai_session_id = ""
        self._ai_verify = {}
        # 出现非订阅计费后记录账号与原因；改设置或重新登录才解除。
        self._ai_billing_block = {}

    @property
    def logged_in(self):
        return self.user is not None

    def _ctx(self, **kwargs):
        token = self.token
        config = self._ai_config()
        block = self._ai_billing_block or {}
        if block and self.user and block.get("user_id") == int(self.user["id"]):
            provider = _BlockedProvider(block.get("mode") or config.get("mode"), block["message"])
        else:
            provider = analysis.get_provider(config)
        return Context(
            self.db,
            user=self.user,
            token=self.token,
            uploads=self.uploads,
            previews=self.previews,
            session_current=lambda: self.token == token,
            ai_config=config,
            ai_provider=provider,
            **kwargs
        )

    def _adopt(self, result):
        self._clear_ai_key()
        self._ai_session_id = "math-mistakes-desktop-%s-%s" % (vision.VERSION, uuid.uuid4().hex)
        self._ai_verify = {}
        self._ai_billing_block = {}
        self.token = result["token"]
        self.user = dict(result["user"])
        return self.user

    def register(self, username, password):
        return self._adopt(api_register(self._ctx(body={"username": username, "password": password})))

    def login(self, username, password):
        return self._adopt(api_login(self._ctx(body={"username": username, "password": password})))

    def logout(self):
        if self.token:
            api_logout(self._ctx())
        # 建议与登录绑定：退出登录后旧建议与界面密钥一律作废。
        self.previews.clear()
        self._clear_ai_key()
        self._ai_session_id = ""
        self._ai_verify = {}
        self._ai_billing_block = {}
        self.token = None
        self.user = None

    # ------------------------------------------------------------ AI 设置
    def _clear_ai_key(self):
        self._ai_keys = {}
        self._ai_key_user_id = None

    def _session_key(self, mode):
        if not self.user or self._ai_key_user_id != int(self.user["id"]):
            return ""
        return self._ai_keys.get(mode, "")

    def _set_ai_key(self, mode, value):
        if not self.user:
            return
        user_id = int(self.user["id"])
        if self._ai_key_user_id not in (None, user_id):
            self._ai_keys = {}
        self._ai_key_user_id = user_id
        if value:
            self._ai_keys[mode] = value
        else:
            self._ai_keys.pop(mode, None)

    def _meta_value(self, key):
        row = self.db.query_one("SELECT value FROM app_meta WHERE key=?", (key,))
        return str(row["value"] or "") if row else ""

    def _set_meta_value(self, key, value):
        with self.db.tx() as conn:
            if value:
                conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)", (key, value))
            else:
                conn.execute("DELETE FROM app_meta WHERE key=?", (key,))

    def _ai_config(self):
        if not self.user:
            return {}
        user_id = int(self.user["id"])
        stored = self._meta_value("ai_mode:%d" % user_id)
        modes = (vision.MODE_MOCK, vision.MODE_REAL, vision.MODE_GO, vision.MODE_CUSTOM)
        # 账号没有设置过就是模拟；旧环境变量不能改变默认模式。
        mode = stored if stored in modes else vision.MODE_MOCK
        base = ""
        model = ""
        service = ""
        if mode == vision.MODE_GO:
            # Go 模型单独存；1.4.5 升级上来的账号回退读旧的 ai_model。
            model = self._meta_value("ai_go_model:%d" % user_id)
            if not model:
                legacy = self._meta_value("ai_model:%d" % user_id)
                model = legacy if legacy in vision.GO_MODELS else vision.GO_DEFAULT_MODEL
        elif mode == vision.MODE_CUSTOM:
            service = self._meta_value("ai_custom_name:%d" % user_id)
            base = self._meta_value("ai_custom_base:%d" % user_id)
            model = self._meta_value("ai_custom_model:%d" % user_id)
        else:
            base = self._meta_value("ai_base_url:%d" % user_id)
            model = self._meta_value("ai_model:%d" % user_id)
        return {
            "mode": mode,
            "api_key": self._session_key(mode),
            "base_url": base,
            "model": model,
            "service_name": service,
            "session_id": self._ai_session_id,
            "allow_env": False,
            "test_base": self._test_ai_base,
        }

    def _verify_state(self, user_id, mode=""):
        data = self._ai_verify or {}
        if data.get("user_id") != user_id or (data.get("mode") or "") != mode:
            return {"state": "", "message": ""}
        return {"state": data.get("state") or "", "message": data.get("message") or ""}

    def _billing_block_message(self, user_id):
        block = self._ai_billing_block or {}
        return block.get("message", "") if block.get("user_id") == user_id else ""

    def _block_billing(self, message, mode):
        if not self.user:
            return
        self._ai_billing_block = {"user_id": int(self.user["id"]), "mode": mode, "message": message}

    def _note_billing(self, result, mode):
        """试分析成功后记录结论；只有 Go 需要核对订阅来源，缺失来源只标尚未确认。"""
        if not self.user:
            return
        user_id = int(self.user["id"])
        usage = (result or {}).get("usage") or {}
        if mode != vision.MODE_GO:
            message = "试分析成功"
            if not usage:
                message += "；服务没有返回用量，记为尚未确认。"
            self._ai_verify = {"user_id": user_id, "mode": mode, "state": "ok", "message": message}
            return
        source = usage.get("billing_source")
        if source in ("go", "go-plus"):
            self._ai_verify = {"user_id": user_id, "mode": mode, "state": "ok", "message": "试分析成功"}
        elif source:
            self._ai_verify = {
                "user_id": user_id,
                "mode": mode,
                "state": "failed",
                "message": "计费来源是 %s，不是 Go 订阅。" % source,
            }
        else:
            self._ai_verify = {
                "user_id": user_id,
                "mode": mode,
                "state": "ok",
                "message": "试分析成功；服务没有提供订阅来源，记为尚未确认。",
            }

    def ai_settings(self):
        """返回可展示的 AI 配置，不含任何密钥内容。"""
        cfg = vision.resolve_config(self._ai_config())
        user_id = int(self.user["id"]) if self.user else 0
        mode = cfg["mode"]
        session_key = bool(self._session_key(mode))
        verify = self._verify_state(user_id, mode)
        custom_base = self._meta_value("ai_custom_base:%d" % user_id) if user_id else ""
        return {
            "service_name": cfg["service"],
            "custom_name": cfg["custom_name"],
            "price_note": vision.price_note(mode),
            "policy_summary": vision.policy_summary(mode),
            "policy_url": vision.POLICY_URL,
            "go_policy_note": vision.GO_POLICY_NOTE,
            "base_url": cfg["base_url"],
            "request_url": cfg["request_url"],
            "model": cfg["model"],
            "api": cfg["api"],
            "base_url_source": cfg["base_url_source"],
            "model_source": cfg["model_source"],
            "key_configured": bool(cfg["key_configured"]),
            "key_source": cfg["key_source"],
            "session_key_configured": session_key,
            "env_key_configured": False,
            "test_base": self._test_ai_base,
            "billing_block_message": self._billing_block_message(user_id),
            "address_ok": cfg["address_ok"],
            "address_error": cfg["address_error"],
            "account_mode": self._meta_value("ai_mode:%d" % user_id) or vision.MODE_MOCK,
            "account_base_url": self._meta_value("ai_base_url:%d" % user_id) if user_id else "",
            "account_model": self._meta_value("ai_model:%d" % user_id) if user_id else "",
            "account_go_model": self._meta_value("ai_go_model:%d" % user_id) if user_id else "",
            "account_custom_name": self._meta_value("ai_custom_name:%d" % user_id) if user_id else "",
            "account_custom_base": custom_base,
            "account_custom_model": self._meta_value("ai_custom_model:%d" % user_id) if user_id else "",
            "custom_request_url": vision.custom_request_url(custom_base) if custom_base else "",
            "default_base_url": vision.DEFAULT_BASE_URL,
            "default_model": vision.DEFAULT_MODEL,
            "go_base_url": vision.GO_DEFAULT_BASE_URL,
            "go_default_model": vision.GO_DEFAULT_MODEL,
            "go_models": [
                {
                    "id": model_id,
                    "note": spec["note"],
                    "api": spec["api"],
                    "base_url": spec["base_url"],
                }
                for model_id, spec in vision.GO_MODELS.items()
            ],
            "mode": mode,
            "reason": cfg["reason"],
            "verify_state": verify["state"],
            "verify_message": verify["message"],
        }

    def save_ai_settings(self, mode, base_url="", model="", api_key=None, service_name=""):
        """保存当前账号的分析方式与对应服务的地址/模型；密钥只放内存，api_key=None 表示不改。"""
        if not self.user:
            raise ApiError("请先登录")
        user_id = int(self.user["id"])
        mode = (mode or "").strip().lower()
        if mode not in (vision.MODE_MOCK, vision.MODE_REAL, vision.MODE_GO, vision.MODE_CUSTOM):
            raise ApiError("请选择分析方式：模拟、OpenCode Go、OpenCode Console 或自定义 AI 服务")
        base = (base_url or "").strip().rstrip("/")
        model = (model or "").strip()
        name = (service_name or "").strip()
        if len(model) > 120:
            raise ApiError("模型名称过长")
        if len(name) > 40:
            raise ApiError("服务名称过长")
        if mode == vision.MODE_GO:
            model = model or vision.GO_DEFAULT_MODEL
            if model not in vision.GO_MODELS:
                raise ApiError("这个模型不在 Go 的图片模型清单里，请从下拉列表选择")
            # Go 地址由模型决定，不保存历史 Console 地址。
            base = ""
            self._set_meta_value("ai_go_model:%d" % user_id, model)
        elif mode == vision.MODE_CUSTOM:
            if base:
                error = vision.custom_address_error(base)
                if error:
                    raise ApiError(error)
            old_name = self._meta_value("ai_custom_name:%d" % user_id)
            old_base = self._meta_value("ai_custom_base:%d" % user_id)
            self._set_meta_value("ai_custom_name:%d" % user_id, name)
            self._set_meta_value("ai_custom_base:%d" % user_id, base)
            self._set_meta_value("ai_custom_model:%d" % user_id, model)
            if name != old_name or base != old_base:
                # 换了服务或地址，之前填的密钥不再适用于新目标。
                self._set_ai_key(vision.MODE_CUSTOM, "")
        elif mode == vision.MODE_REAL:
            if base:
                parsed = urllib.parse.urlparse(base)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise ApiError("服务地址要以 http:// 或 https:// 开头，并包含主机名")
            if vision.is_go_base(base):
                raise ApiError("OpenCode Console 模式不能填 Go 订阅地址，请重新选择或修改地址")
            self._set_meta_value("ai_base_url:%d" % user_id, base)
            self._set_meta_value("ai_model:%d" % user_id, model)
        self._set_meta_value("ai_mode:%d" % user_id, mode)
        if api_key is not None:
            self._set_ai_key(mode, str(api_key).strip())
        # 设置一改，之前的试分析结论和计费阻断都不再代表当前配置。
        self._ai_verify = {}
        self._ai_billing_block = {}
        return self.ai_settings()

    def clear_ai_key(self, mode=""):
        mode = (mode or "").strip().lower()
        if mode:
            self._set_ai_key(mode, "")
        else:
            self._clear_ai_key()
        self._ai_billing_block = {}
        return self.ai_settings()

    def _trial_result(self, provider, config, started, result=None, exc=None):
        data = {
            "ok": exc is None,
            "mode": provider.mode,
            "service": config["service"],
            "model": config["model"],
            "base_url": config["base_url"],
            "request_url": config["request_url"],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if exc is not None:
            data.update({"error": exc.message, "error_code": exc.code})
        else:
            usage = result.get("usage") or None
            data["suggestion"] = result
            data["usage"] = usage
            data["billing_source"] = (usage or {}).get("billing_source")
        raw = getattr(provider, "last_payload", None)
        if raw:
            data["raw_response"] = raw
        return data

    def trial_analysis(self, photo_path, question="", solution="", consent=False,
                       width=0, height=0, blurry=False):
        """不写数据库的试分析：复用正式分析的 provider 与解析。"""
        if not photo_path or not os.path.exists(photo_path):
            raise ApiError("请选择一张题图")
        with open(photo_path, "rb") as fh:
            blob = fh.read()
        if len(blob) > 10 * 1024 * 1024:
            raise ApiError("图片超过 10MB，请压缩后重试")
        mime = _sniff_mime(blob)
        if not mime:
            raise ApiError("图片格式不支持，仅接受 JPG / PNG / WebP")
        config = vision.resolve_config(self._ai_config())
        provider = analysis.get_provider(config)
        block = self._ai_billing_block or {}
        if block and self.user and block.get("user_id") == int(self.user["id"]):
            provider = _BlockedProvider(block.get("mode") or provider.mode, block["message"])
        if provider.mode in vision.REMOTE_MODES and not consent:
            raise ApiError("需要先勾选同意，才能把照片与文字发送给%s" % config["service"])
        job = {
            "question_text": (question or "").strip(),
            "solution_text": (solution or "").strip(),
            "image_file": os.path.basename(photo_path),
            "image_bytes": blob,
            "image_mime": mime,
            "image_width": int(width or 0),
            "image_height": int(height or 0),
            "blurry": bool(blurry),
            "trial": True,
        }
        token = self.token
        user_id = int(self.user["id"]) if self.user else 0
        remote = provider.mode in vision.REMOTE_MODES
        started = time.monotonic()
        try:
            result = provider.analyze(job)
        except vision.AnalysisError as exc:
            data = self._trial_result(provider, config, started, exc=exc)
            if remote and self.token == token and user_id:
                if exc.code == "billing":
                    self._block_billing(exc.message, provider.mode)
                self._ai_verify = {
                    "user_id": user_id,
                    "mode": provider.mode,
                    "state": "failed",
                    "message": exc.message,
                }
            return data
        data = self._trial_result(provider, config, started, result=result)
        if remote and self.token == token and user_id:
            self._note_billing(result, provider.mode)
        return data

    def list_classes(self):
        return api_list_classes(self._ctx())["items"]

    def create_class(self, name, grade="", note=""):
        return api_create_class(self._ctx(body={"name": name, "grade": grade, "note": note}))

    # ------------------------------------------------------------ 学生名单
    def list_students(self, class_id):
        return api_get_class(self._ctx(params={"id": class_id}))["students"]

    def create_student(self, class_id, name, code=""):
        return api_create_student(
            self._ctx(params={"id": class_id}, body={"name": name, "code": code})
        )

    def rename_student(self, student_id, name, code=None):
        body = {"name": name}
        if code is not None:
            body["code"] = code
        return api_update_student(self._ctx(params={"id": student_id}, body=body))

    def set_student_active(self, student_id, active):
        return api_update_student(self._ctx(params={"id": student_id}, body={"active": bool(active)}))

    # ------------------------------------------------------------ 学情统计
    def class_stats(self, class_id, date_from="", date_to=""):
        query = {"from": date_from or "", "to": date_to or ""}
        return api_class_stats(self._ctx(params={"id": class_id}, query=query))

    def student_stats(self, student_id, date_from="", date_to=""):
        query = {"from": date_from or "", "to": date_to or ""}
        return api_student_stats(self._ctx(params={"id": student_id}, query=query))

    def mistakes_by_ids(self, ids, full=False):
        if not ids:
            return []
        query = {"ids": ",".join(str(int(i)) for i in ids)}
        if full:
            query["full"] = "1"
        return api_mistake_list_for_ids(self._ctx(query=query))["items"]

    # ------------------------------------------------------------ 作答 CSV 导入
    def answer_batches(self, class_id, batch=""):
        return api_answers_list(self._ctx(params={"id": class_id}, query={"batch": batch or ""}))

    def preview_answers(self, class_id, text, batch="", selections=None):
        return api_answers_preview(
            self._ctx(params={"id": class_id},
                      body={"text": text, "batch": batch, "selections": selections or {}})
        )

    def import_answers(self, class_id, text, batch="", selections=None, overwrite=False):
        return api_answers_import(
            self._ctx(params={"id": class_id},
                      body={"text": text, "batch": batch, "selections": selections or {},
                            "overwrite": bool(overwrite), "confirm": True})
        )

    def answer_template(self):
        return api_answers_template(self._ctx())

    # ------------------------------------------------------------ 作答审核
    def answer_reviews(self, class_id, batch=""):
        return api_answer_reviews(self._ctx(params={"id": class_id}, query={"batch": batch or ""}))

    def answer_review(self, class_id, answer_id):
        return api_answer_review_get(self._ctx(params={"id": class_id, "answer_id": answer_id}))

    def analyse_answer(self, class_id, answer_id, consent=False):
        """老师主动发起的一次分析；失败原样返回，不写任何内容。"""
        params = {"id": class_id, "answer_id": answer_id}
        result = api_answer_analyse(self._ctx(params=params, body={"consent": bool(consent)}))
        if not result.get("ok") and result.get("error_code") == "billing":
            mode = vision.resolve_config(self._ai_config()).get("mode") or vision.MODE_GO
            self._block_billing(result.get("error") or "", mode)
        return result

    def update_answer_review(self, class_id, review_id, causes, note=""):
        body = {"causes": causes or [], "note": note or ""}
        return api_answer_review_update(self._ctx(params={"id": class_id, "review_id": review_id}, body=body))

    def confirm_answer_review(self, class_id, review_id):
        return api_answer_review_confirm(self._ctx(params={"id": class_id, "review_id": review_id}))

    def reject_answer_review(self, class_id, review_id):
        return api_answer_review_reject(self._ctx(params={"id": class_id, "review_id": review_id}))

    # ------------------------------------------------------------ 个人学情与辅导建议
    def student_insight(self, class_id, student_id):
        return api_student_insight(self._ctx(params={"id": class_id, "student_id": student_id}))

    def generate_student_advice(self, class_id, student_id, consent=False):
        """老师主动发起的一次生成；失败原样返回，不改已有建议。"""
        params = {"id": class_id, "student_id": student_id}
        result = api_student_advice_generate(self._ctx(params=params, body={"consent": bool(consent)}))
        if not result.get("ok") and result.get("error_code") == "billing":
            mode = vision.resolve_config(self._ai_config()).get("mode") or vision.MODE_GO
            self._block_billing(result.get("error") or "", mode)
        return result

    def save_student_advice(self, class_id, student_id, content):
        params = {"id": class_id, "student_id": student_id}
        return api_student_advice_save(self._ctx(params=params, body={"content": content}))

    # ------------------------------------------------------------ 教师短追问
    def list_followups(self, class_id, student_id):
        params = {"id": class_id, "student_id": student_id}
        return api_followup_list(self._ctx(params=params))["items"]

    def followup_context(self, class_id, student_id, problem, mistake_id=None):
        """追问要用的原文依据：只读本地数据，不调用 AI。"""
        params = {"id": class_id, "student_id": student_id}
        query = {"problem": problem or "", "mistake_id": mistake_id}
        return api_followup_context(self._ctx(params=params, query=query))["context"]

    def suggest_followup(self, class_id, student_id, problem, mistake_id=None, consent=False):
        """生成候选追问；模拟模式下返回错误，不编造。"""
        params = {"id": class_id, "student_id": student_id}
        body = {"problem": problem, "mistake_id": mistake_id, "consent": bool(consent)}
        result = api_followup_suggest(self._ctx(params=params, body=body))
        if not result.get("ok") and result.get("error_code") == "billing":
            mode = vision.resolve_config(self._ai_config()).get("mode") or vision.MODE_GO
            self._block_billing(result.get("error") or "", mode)
        return result

    def create_followup(self, class_id, student_id, problem, question, source="teacher",
                        mistake_id=None, suggestion=None):
        params = {"id": class_id, "student_id": student_id}
        body = {
            "problem": problem,
            "question": question,
            "source": source,
            "mistake_id": mistake_id,
            "suggestion": suggestion or {},
        }
        return api_followup_create(self._ctx(params=params, body=body))["followup"]

    def get_followup(self, class_id, student_id, followup_id):
        params = {"id": class_id, "student_id": student_id, "fid": followup_id}
        return api_followup_get(self._ctx(params=params))["followup"]

    def save_followup_answer(self, class_id, student_id, followup_id, answer):
        params = {"id": class_id, "student_id": student_id, "fid": followup_id}
        return api_followup_answer(self._ctx(params=params, body={"answer": answer}))["followup"]

    def update_followup_causes(self, class_id, student_id, followup_id, consent=False):
        """按学生回答生成待核对的错因候选；失败原样返回。"""
        params = {"id": class_id, "student_id": student_id, "fid": followup_id}
        body = {"consent": bool(consent)}
        result = api_followup_update(self._ctx(params=params, body=body))
        if not result.get("ok") and result.get("error_code") == "billing":
            mode = vision.resolve_config(self._ai_config()).get("mode") or vision.MODE_GO
            self._block_billing(result.get("error") or "", mode)
        return result

    def decide_followup_update(self, class_id, student_id, followup_id, update_id, action,
                               causes=None, note=None):
        params = {
            "id": class_id,
            "student_id": student_id,
            "fid": followup_id,
            "uid": update_id,
        }
        body = {"action": action, "causes": causes, "note": note}
        return api_followup_decide(self._ctx(params=params, body=body))["followup"]

    # ------------------------------------------------------------ 错题
    def list_mistakes(self, class_id, student_id):
        result = api_list_mistakes(
            self._ctx(query={"class_id": str(class_id), "student_id": str(student_id)})
        )
        return result["items"]

    def get_mistake(self, mistake_id):
        return api_get_mistake(self._ctx(params={"id": mistake_id}))

    def create_mistake(self, class_id, student_id, fields, photo_path, width=0, height=0):
        body = dict(fields)
        body["class_id"] = class_id
        body["student_id"] = student_id
        # 桌面版手动录入：不把照片发给云端，也不生成 AI 内容。
        body["local_only"] = True
        body["image"] = self._photo_payload(photo_path, width, height)
        return api_create_mistake(self._ctx(body=body))

    def update_mistake(self, mistake_id, fields, photo_path=None, width=0, height=0, base_fingerprint=""):
        body = dict(fields)
        if base_fingerprint:
            body["base_fingerprint"] = base_fingerprint
        if photo_path:
            body["image"] = self._photo_payload(photo_path, width, height)
        return api_update_mistake(self._ctx(params={"id": mistake_id}, body=body))

    # ------------------------------------------------------------ AI 分析
    def meta(self):
        """分析配置说明：模式、模型、密钥是否已配置，由界面原样显示。"""
        return describe_ai(self._ctx())

    def preview_analysis(self, mistake_id, consent=False, task_id=""):
        """算一份建议交给内存保管，正式记录不变；失败也先不写，交界面判定。"""
        body = {"consent": bool(consent), "task_id": task_id}
        result = api_preview_analysis(self._ctx(params={"id": mistake_id}, body=body))
        if not result.get("ok") and result.get("error_code") == "billing":
            mode = vision.resolve_config(self._ai_config()).get("mode") or vision.MODE_GO
            self._block_billing(result.get("error") or "", mode)
        return result

    def record_analysis_failure(self, mistake_id, message, code=""):
        """界面确认这次失败仍属于当前窗口和会话之后，才记下最近一次失败原因。"""
        body = {"message": message, "code": code}
        return api_record_analysis_failure(self._ctx(params={"id": mistake_id}, body=body))

    def apply_analysis(self, mistake_id, preview_id):
        """采用建议：写库内容取自业务层保管的原件，界面只提供一个标识。"""
        body = {"preview_id": preview_id}
        return api_apply_analysis(self._ctx(params={"id": mistake_id}, body=body))

    def discard_preview(self, mistake_id, preview_id):
        return api_discard_preview(self._ctx(params={"id": mistake_id}, body={"preview_id": preview_id}))

    def confirm_mistake(self, mistake_id, base_fingerprint=""):
        return api_confirm_mistake(self._ctx(params={"id": mistake_id}, body={"base_fingerprint": base_fingerprint}))

    def return_mistake(self, mistake_id, reason, base_fingerprint=""):
        body = {"reason": reason, "base_fingerprint": base_fingerprint}
        return api_return_mistake(self._ctx(params={"id": mistake_id}, body=body))

    def mistake_photo(self, mistake_id):
        """返回 (照片字节, 类型)；记录没有照片或文件缺失时抛 ApiError。"""
        data = api_mistake_image(self._ctx(params={"id": mistake_id}))
        return data["body"], data["content_type"]

    def _photo_payload(self, path, width, height):
        if not path:
            raise ApiError("缺少图片")
        with open(path, "rb") as fh:
            blob = fh.read()
        mime = _sniff_mime(blob)
        if not mime:
            raise ApiError("图片格式不支持，仅接受 JPG / PNG / WebP")
        return {
            "data_url": "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii")),
            "width": int(width or 0),
            "height": int(height or 0),
        }


__all__ = ["ApiError", "DesktopService"]
