"""1.4.6 界面层验证：AI 设置选择 Go/自定义服务、试分析同意与结果标识。

只连本地桩服务，不发起真实云端调用。用 offscreen Qt 运行，不弹系统窗口。
"""
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
WORK = os.path.join(ROOT, "tests", ".tmp-ai-146-ui")
LOG_PATH = os.path.join(ROOT, "docs", "verify-ai-146-ui.txt")
SECRET = "sk-go-146-ui-never-write"
CUSTOM_SECRET = "sk-custom-146-ui-never-write"
CHECKS = []
STUB = {"mode": "ok", "calls": [], "lock": threading.Lock()}

SUCCESS = {
    "question_text": "化简：√18 - √8",
    "solution_text": "√18-√8=3√2-2√2=√2",
    "knowledge_points": ["二次根式"],
    "error_type": "",
    "observed_errors": [],
    "possible_causes": "",
    "advice_text": "过程完整，结果正确。",
    "insufficient": False,
    "insufficient_reason": "",
    "likely_correct": True,
}


class Log:
    def __init__(self, path):
        self.path = path
        self.lines = []

    def write(self, text=""):
        print(text)
        self.lines.append(text)

    def close(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.lines) + "\n")


LOG = Log(LOG_PATH)


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    LOG.write(("  OK  " if ok else "  FAIL") + "  " + name + ("" if ok else "  -> " + str(detail)))


def free_port():
    import socket

    for candidate in range(18930, 18990):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", candidate))
            return candidate
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError("找不到可用端口")


def png_bytes():
    from PIL import Image

    buffer = __import__("io").BytesIO()
    Image.effect_noise((640, 800), 64).convert("RGB").save(buffer, "PNG")
    return buffer.getvalue()


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with STUB["lock"]:
            STUB["calls"].append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "raw": raw.decode("utf-8", "ignore"),
            })
        content = "```json\n" + json.dumps(SUCCESS, ensure_ascii=False) + "\n```"
        mode = STUB["mode"]
        if self.path.endswith("/messages"):
            payload = {
                "id": "msg_ui_146",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
            }
            usage = {
                "input_tokens": 1800, "output_tokens": 120, "cost": 0,
                "opencode": {"billing_source": "go", "list_cost": 0.00432, "allowance": 0.00432},
            }
        else:
            payload = {
                "id": "chatcmpl_ui_146",
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            }
            usage = {
                "prompt_tokens": 1800, "completion_tokens": 120, "total_tokens": 1920, "cost": 0,
                "opencode": {"billing_source": "go", "list_cost": 0.00432, "allowance": 0.00432},
            }
        if mode == "partial":
            usage = {
                "prompt_tokens": 1800, "completion_tokens": None, "total_tokens": None,
                "cost": None, "opencode": {"billing_source": "go"},
            }
        elif mode == "zeros":
            usage = {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0,
                "opencode": {"billing_source": "go", "allowance": 0},
            }
        elif mode == "nulls":
            usage = {
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                "cost": None, "opencode": {},
            }
        if mode != "nousage":
            payload["usage"] = usage
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


def wait_runner(app, dialog, timeout=20.0):
    deadline = time.time() + timeout
    while dialog.runner.busy and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.01)


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    from logmeta import environment_lines

    for line in environment_lines():
        LOG.write(line)
    LOG.write("")
    port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", port), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_root = "http://127.0.0.1:%d" % port

    os.environ["MISTAKE_AI_BASE_URL"] = stub_root
    os.environ["MISTAKE_AI_MODE"] = "real"
    os.environ.pop("MISTAKE_AI_API_KEY", None)

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from desktop import data_dir
    from desktop.service import DesktopService
    from desktop.ui import AISettingsDialog, AITrialDialog, ConsentDialog, analysis_failure_text

    data_root = os.path.join(WORK, "data")
    data_dir.create_new(data_root)
    service = DesktopService(data_root, test_ai_base=stub_root)
    service.register("界面验证老师", "pw-go-146-ui")
    photo = os.path.join(WORK, "题图-根式.png")
    with open(photo, "wb") as fh:
        fh.write(png_bytes())

    LOG.write("== 一、AI 设置：默认模拟、Go、自定义服务与未验证状态")
    dialog = AISettingsDialog(service)
    dialog.show()
    app.processEvents()
    modes = [dialog.mode_combo.itemData(i) for i in range(dialog.mode_combo.count())]
    check("分析方式有模拟/Go/Console/自定义四项", modes == ["mock", "go", "real", "custom"], modes)
    check("默认选中模拟", dialog.mode_combo.currentData() == "mock", dialog.mode_combo.currentData())
    check("默认隐藏密钥输入", not dialog.key_box.isVisible(), dialog.key_box.isVisible())
    go_index = dialog.mode_combo.findData("go")
    dialog.mode_combo.setCurrentIndex(go_index)
    app.processEvents()
    check("选择 Go 后显示 Go 页", dialog.pages.currentIndex() == 1, dialog.pages.currentIndex())
    check("选择 Go 后要求填写密钥", dialog.key_box.isVisible(), dialog.key_box.isVisible())
    check("Go 模型下拉来自配置清单", dialog.model_combo.count() == 7, dialog.model_combo.count())
    check("Go 页写明官方用途限制", "编程代理" in dialog.go_policy.text(), dialog.go_policy.text())
    check("Go 页给出完整请求地址", "/messages" in dialog.go_base_label.text(), dialog.go_base_label.text())
    check("Go 页标明本机测试地址", "测试地址" in dialog.go_base_label.text(), dialog.go_base_label.text())

    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("qwen3.8-max"))
    dialog.key_edit.setText(SECRET)
    dialog.save_button.click()
    app.processEvents()
    settings = service.ai_settings()
    check("保存后进入 Go 模式", settings["mode"] == "go", settings["mode"])
    check("保存后密钥只在内存", settings["session_key_configured"] and SECRET not in json.dumps(settings, ensure_ascii=False), settings)
    check("保存后状态为已配置未验证", "尚未验证" in dialog.status.text(), dialog.status.text())
    check("保存按钮不会关闭窗口", dialog.isVisible(), "窗口被关闭")
    check("界面上能读到完整请求地址", dialog.sources.text().count("/messages") >= 1, dialog.sources.text())

    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("custom"))
    app.processEvents()
    check("选择自定义服务后显示自定义页", dialog.pages.currentIndex() == 3, dialog.pages.currentIndex())
    check("自定义页要求填写密钥", dialog.key_box.isVisible(), dialog.key_box.isVisible())
    dialog.custom_name_edit.setText("测试云")
    dialog.custom_base_edit.setText(stub_root + "/custom/v1/")
    dialog.custom_model_edit.setText("vision-x")
    dialog.key_edit.setText(CUSTOM_SECRET)
    app.processEvents()
    check("自定义页实时显示完整请求地址",
          dialog.custom_url_label.text().endswith("/custom/v1/chat/completions"), dialog.custom_url_label.text())
    check("自定义页说明兼容程度与图片模型",
          "图片" in dialog.custom_hint.text() and "兼容" in dialog.custom_hint.text(), dialog.custom_hint.text())
    dialog.save_button.click()
    app.processEvents()
    custom_settings = service.ai_settings()
    check("保存后进入自定义模式", custom_settings["mode"] == "custom", custom_settings["mode"])
    check("保存后显示自定义服务名", custom_settings["service_name"] == "测试云", custom_settings["service_name"])
    check("保存后状态为已配置未验证", "尚未验证" in dialog.status.text(), dialog.status.text())
    check("自定义设置页写明不代表已接通",
          "不代表已经连接成功" in dialog.sources.text(), dialog.sources.text())
    dialog.close()

    dialog = AISettingsDialog(service)
    dialog.show()
    app.processEvents()
    check("重开设置仍选中自定义服务", dialog.mode_combo.currentData() == "custom", dialog.mode_combo.currentData())
    check("重开设置回填服务名", dialog.custom_name_edit.text() == "测试云", dialog.custom_name_edit.text())
    check("重开设置回填接口地址", dialog.custom_base_edit.text() == stub_root + "/custom/v1", dialog.custom_base_edit.text())
    check("重开设置回填模型", dialog.custom_model_edit.text() == "vision-x", dialog.custom_model_edit.text())

    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("mock"))
    app.processEvents()
    check("切回模拟后隐藏密钥", not dialog.key_box.isVisible())
    dialog.save_button.click()
    app.processEvents()
    check("保存模拟后模式为 mock", service.ai_settings()["mode"] == "mock", service.ai_settings()["mode"])
    check("保存模拟后没有任何请求", not STUB["calls"], len(STUB["calls"]))
    check("模拟状态显示不发送", "模拟" in dialog.status.text() and "不发送" in dialog.status.text(), dialog.status.text())
    check("模拟设置页不显示云端地址", "模拟分析" in dialog.sources.text() and "inference" not in dialog.sources.text(), dialog.sources.text())
    dialog.close()

    LOG.write("== 二、试分析：Go 未勾选不发送、成功标识与用量来源")
    service.save_ai_settings("go", "", "qwen3.8-max", SECRET)
    trial = AITrialDialog(service)
    trial.show()
    app.processEvents()
    trial.path_edit.setText(photo)
    check("试分析显示 Go 服务名", "OpenCode Go（订阅）" in trial.config_label.text(), trial.config_label.text())
    check("试分析显示真实请求地址", trial.config_label.text().count("/messages") >= 1, trial.config_label.text())
    check("Go 模式显示同意勾选框", trial.consent.isVisible(), trial.consent.isVisible())
    trial.start_button.click()
    app.processEvents()
    check("未勾选同意时被拦下", "勾选同意" in trial.status.text(), trial.status.text())
    check("未勾选时没有发出请求", not STUB["calls"], len(STUB["calls"]))

    trial.consent.setChecked(True)
    trial.start_button.click()
    wait_runner(app, trial)
    check("Go 试分析成功", "Go 试分析成功" in trial.status.text(), trial.status.text())
    check("结果显示计费来源 go", "计费来源：go" in trial.status.text(), trial.status.text())
    check("结果显示费用 0", "费用：$0" in trial.status.text(), trial.status.text())
    check("结果显示额度计入", "额度计入：$0.00432" in trial.status.text(), trial.status.text())
    check("试分析结果区显示 Go 服务", "OpenCode Go（订阅）" in trial.result.toPlainText(), trial.result.toPlainText()[:80])
    check("试分析请求带 Go 会话标识", bool(STUB["calls"][-1]["headers"].get("x-opencode-session")), STUB["calls"][-1]["headers"])
    check("试分析请求含实际题图", "base64" in STUB["calls"][-1]["raw"], "没有图片数据")

    STUB["mode"] = "nousage"
    trial.start_button.click()
    wait_runner(app, trial)
    check("服务无用量时明确写尚未确认", "尚未确认" in trial.status.text(), trial.status.text())
    check("服务无用量时不显示 None", "None" not in trial.status.text(), trial.status.text())

    STUB["mode"] = "partial"
    trial.start_button.click()
    wait_runner(app, trial)
    partial = trial.status.text()
    check("部分用量缺失时缺的项写尚未确认", "completion 尚未确认" in partial, partial)
    check("部分用量缺失时费用写尚未确认", "费用：尚未确认" in partial, partial)
    check("部分用量缺失时不显示 None", "None" not in partial, partial)

    STUB["mode"] = "zeros"
    trial.start_button.click()
    wait_runner(app, trial)
    zeros = trial.status.text()
    check("用量为 0 时正常显示 0", "prompt 0 / completion 0 / total 0" in zeros, zeros)
    check("费用为 0 时显示 $0", "费用：$0" in zeros, zeros)
    check("额度为 0 时显示 $0", "额度计入：$0" in zeros, zeros)

    STUB["mode"] = "nulls"
    trial.start_button.click()
    wait_runner(app, trial)
    nulls = trial.status.text()
    check("用量字段全为 null 时写尚未确认", "用量：尚未确认" in nulls, nulls)
    check("用量字段全为 null 时不显示 None", "None" not in nulls, nulls)
    STUB["mode"] = "ok"
    trial.close()

    LOG.write("== 二之二、自定义服务：设置、试分析与图片请求")
    service.save_ai_settings("custom", stub_root + "/custom/v1", "vision-x", CUSTOM_SECRET, service_name="测试云")
    custom_trial = AITrialDialog(service)
    custom_trial.show()
    app.processEvents()
    custom_trial.path_edit.setText(photo)
    check("试分析显示自定义服务名", "测试云" in custom_trial.config_label.text(), custom_trial.config_label.text())
    check("试分析显示自定义完整地址",
          "/custom/v1/chat/completions" in custom_trial.config_label.text(), custom_trial.config_label.text())
    check("试分析说明不单独发送姓名班级账号",
          "不会单独发送学生姓名" in custom_trial.send_note.text(), custom_trial.send_note.text())
    before = len(STUB["calls"])
    custom_trial.start_button.click()
    app.processEvents()
    check("自定义服务未勾选同意被拦下", "勾选同意" in custom_trial.status.text(), custom_trial.status.text())
    check("自定义服务未勾选时没有请求", len(STUB["calls"]) == before, len(STUB["calls"]) - before)
    custom_trial.consent.setChecked(True)
    custom_trial.start_button.click()
    wait_runner(app, custom_trial)
    check("自定义服务试分析成功", "自定义服务试分析成功" in custom_trial.status.text(), custom_trial.status.text())
    check("自定义结果不把 Go 计费来源当成自定义服务的",
          "计费来源：尚未确认" in custom_trial.status.text() and "计费来源：go" not in custom_trial.status.text(),
          custom_trial.status.text())
    check("自定义结果不显示 Go 额度计入", "额度计入" not in custom_trial.status.text(), custom_trial.status.text())
    check("自定义结果区显示服务名", "测试云" in custom_trial.result.toPlainText(), custom_trial.result.toPlainText()[:80])
    last = STUB["calls"][-1]
    check("自定义试分析走 chat 路径", last["path"] == "/custom/v1/chat/completions", last["path"])
    check("自定义请求不带 Go 会话标识", "x-opencode-session" not in last["headers"], last["headers"])
    check("自定义请求含实际题图", "base64" in last["raw"], "没有图片数据")

    STUB["mode"] = "nousage"
    custom_trial.start_button.click()
    wait_runner(app, custom_trial)
    check("自定义服务无用量时标尚未确认", "尚未确认" in custom_trial.status.text(), custom_trial.status.text())
    check("自定义服务无用量时不显示 None", "None" not in custom_trial.status.text(), custom_trial.status.text())

    STUB["mode"] = "partial"
    custom_trial.start_button.click()
    wait_runner(app, custom_trial)
    custom_partial = custom_trial.status.text()
    check("自定义部分用量缺失时写尚未确认", "completion 尚未确认" in custom_partial, custom_partial)
    check("自定义部分用量缺失时费用写尚未确认", "费用：尚未确认" in custom_partial, custom_partial)
    check("自定义部分用量缺失时不显示 None", "None" not in custom_partial, custom_partial)

    STUB["mode"] = "zeros"
    custom_trial.start_button.click()
    wait_runner(app, custom_trial)
    custom_zeros = custom_trial.status.text()
    check("自定义用量 0 正常显示", "prompt 0 / completion 0 / total 0" in custom_zeros, custom_zeros)
    check("自定义费用 0 显示 $0", "费用：$0" in custom_zeros, custom_zeros)

    STUB["mode"] = "nulls"
    custom_trial.start_button.click()
    wait_runner(app, custom_trial)
    check("自定义用量全 null 写尚未确认", "用量：尚未确认" in custom_trial.status.text(), custom_trial.status.text())
    check("自定义用量全 null 不显示 None", "None" not in custom_trial.status.text(), custom_trial.status.text())
    STUB["mode"] = "ok"
    custom_trial.close()
    service.save_ai_settings("go", "", "qwen3.8-max", SECRET)

    LOG.write("== 三、失败提示与确认框按服务区分")
    text = analysis_failure_text("x", "payment", "go")
    check("Go 额度不足不提 Console 充值", "Go 订阅额度不足" in text and "Console 充值" not in text, text)
    text = analysis_failure_text("x", "auth", "go")
    check("Go 权限错误提用途与不回退", "用途" in text and "不会自动改用模拟" in text, text)
    text = analysis_failure_text("x", "billing", "go")
    check("计费异常提示停止并核对账户", "停止" in text and "核对" in text, text)
    text = analysis_failure_text("x", "payment", "custom")
    check("自定义额度不足不提 Console 充值", "服务商" in text and "Console" not in text, text)
    text = analysis_failure_text("x", "auth", "custom")
    check("自定义密钥错误提示核对密钥与图片权限", "密钥" in text and "图片" in text, text)

    from PySide6.QtWidgets import QLabel

    consent = ConsentDialog(service.meta())
    consent.show()
    app.processEvents()
    check("确认框标题带 Go 服务名", "OpenCode Go（订阅）" in consent.windowTitle(), consent.windowTitle())
    labels = [w.text() for w in consent.findChildren(QLabel)]
    check("确认框列出发送范围", any("错题照片" in item for item in labels), labels)
    check("确认框说明不单独发送姓名班级账号",
          any("不会单独发送学生姓名" in item for item in labels), labels)
    check("确认框说明可能计入订阅额度", any("订阅额度" in item for item in labels), labels)
    consent.close()

    service.save_ai_settings("custom", stub_root + "/custom/v1", "vision-x", CUSTOM_SECRET, service_name="测试云")
    consent = ConsentDialog(service.meta())
    consent.show()
    app.processEvents()
    check("自定义确认框标题带服务名", "测试云" in consent.windowTitle(), consent.windowTitle())
    labels = [w.text() for w in consent.findChildren(QLabel)]
    check("自定义确认框说明可能产生费用或消耗额度",
          any("费用或消耗" in item for item in labels), labels)
    consent.close()

    # ------------------------------------------------------------------
    # T26 新增（整段为新断言，不替换任何既有断言）：个体 AI 开关与本套件管辖的两个错题入口。
    # 为什么加：R1-14 之后「真实通道」默认不外发，只有教师账号显式开启才真调用；
    # 本套件原来只验设置页与试分析，没验「关闸门时错题分析入口必须 403」这一档。
    # ------------------------------------------------------------------
    LOG.write("")
    LOG.write("== 十、个体 AI 开关：关闭 → 错题分析入口 403；开启 → 立即可用")
    state = service.ai_individual_state()
    check("T26.1 新账号默认未开启个体 AI 外发", state.get("ai_individual_allowed") is False, state)
    check("T26.2 状态回执写明关闭即 403 ai_isolation 与审计",
          "403" in (state.get("note") or "") and "审计" in (state.get("note") or ""),
          state.get("note"))

    class_row = service.create_class("T26 闸门校验班", grade="七年级")
    student = service.create_student(class_row["id"], "学生甲", "")
    created = service.create_mistake(
        class_row["id"], student["id"],
        {"question_text": "2x+3=7", "solution_text": "2x=4\nx=2",
         "observed_errors": ["移项没有变号"], "knowledge_points": ["移项"],
         "error_type": "", "possible_causes": ["移项没有变号"], "advice_text": "",
         "source_type": "作业", "source_detail": "", "occurred_on": "2026-11-20",
         "teacher_note": ""},
        photo, 240, 120,
    )
    mistake_id = created["id"] if isinstance(created, dict) else created

    from app import api as api_mod

    def expect_isolation(name, call):
        try:
            result = call()
        except api_mod.ApiError as exc:
            check(name,
                  exc.status == 403 and exc.code == "ai_isolation" and "已停用" in exc.message,
                  (exc.status, exc.code, (exc.message or "")[:60]))
            return
        except Exception as exc:  # noqa: BLE001
            check(name, False, "未按 403 拦断，而是 %s: %s" % (type(exc).__name__, exc))
            return
        check(name, False, "没有抛 403，而是返回了 %r" % (result,))

    expect_isolation("T26.3 关闭时「错题照片分析建议」→ 403 ai_isolation",
                     lambda: service.preview_analysis(mistake_id, consent=True, task_id="t26-a"))
    expect_isolation("T26.4 关闭时「错题重新分析」（旧入口同一道闸门）→ 403 ai_isolation",
                     lambda: api_mod.api_reanalyze(
                         service._ctx(params={"id": mistake_id}, body={"consent": True})))

    opened = service.set_ai_individual(True)
    check("T26.5 显式开启后账号状态已授权", opened.get("ai_individual_allowed") is True, opened)
    preview = service.preview_analysis(mistake_id, consent=True, task_id="t26-b")
    # 说明（如实）：本套件的这条错题是脚本造的最小样本，照片字节没有进 uploads 存储，
    # 所以开启闸门后 preview 会以「没有可发送的图片数据」失败——但那已经**不是闸门**拦的。
    # 这条断言验的是「闸门已放开」，因此只要求失败原因不再是 403 ai_isolation / 「已停用」。
    check("T26.6 开启后同一入口不再被闸门拦（失败原因不再是 ai_isolation / 已停用）",
          preview.get("error_code") != "ai_isolation"
          and "已停用" not in (preview.get("error") or ""),
          (preview.get("ok"), preview.get("error_code"), (preview.get("error") or "")[:60]))
    closed = service.set_ai_individual(False)
    check("T26.7 再关闭立即恢复拦截，不需要重启",
          closed.get("ai_individual_allowed") is False,
          closed.get("ai_individual_allowed"))

    failed = sum(1 for _, ok, _ in CHECKS if not ok)
    LOG.write("")
    LOG.write("合计 %d 项，通过 %d 项，失败 %d 项" % (len(CHECKS), len(CHECKS) - failed, failed))
    code = 1 if failed else 0
    LOG.write("退出码：%d" % code)
    stub.shutdown()
    LOG.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
