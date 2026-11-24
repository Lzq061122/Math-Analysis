"""1.4.6 自定义 AI 服务的源码级验证。

覆盖：地址与拼接、Go 认证与会话标识、Anthropic/OpenAI 两种请求体、
订阅来源与用量、额度/权限/限流/超时/格式/截断、失败保留、账号隔离与保存流程。
真实 Go 云端调用不在本脚本内，脚本只连本地桩服务。
"""
import io
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from app.version import VERSION as VERSION_EXPECT
WORK = os.path.join(ROOT, "tests", ".tmp-ai-146")
LOG_PATH = os.path.join(ROOT, "docs", "verify-ai-146.txt")
SECRET = "sk-go-145-never-write-7f3a"
CHECKS = []
STUB = {"mode": "ok", "calls": [], "lock": threading.Lock()}

SUCCESS = {
    "question_text": "解方程：3(x-2)=2x+5",
    "solution_text": "3x-6=2x+5\n3x+2x=5+6\n5x=11\nx=11/5",
    "knowledge_points": ["一元一次方程"],
    "error_type": "符号错误",
    "observed_errors": ["第 2 行移项时 2x 与 -6 都没有变号"],
    "possible_causes": ["可能把移项要变号的规则记混了"],
    "advice_text": "先单独练 3 道只做移项的题，每步标出新符号。",
    "insufficient": False,
    "insufficient_reason": "",
    "likely_correct": False,
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
    for candidate in list(range(18860, 18920)) + [8768, 8769]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", candidate))
            return candidate
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError("找不到可用的本地测试端口")


def png_bytes(width=640, height=800):
    from PIL import Image

    image = Image.effect_noise((width, height), 64).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def make_photo(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(png_bytes())
    return path


def scan_secret(root, secret):
    for name in ("app.db", "app.db-wal", "app.db-shm"):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            if secret.encode("utf-8") in fh.read():
                return path
    return ""


def suggestion_json():
    return "```json\n" + json.dumps(SUCCESS, ensure_ascii=False) + "\n```"


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8") or "{}")
        with STUB["lock"]:
            STUB["calls"].append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
                "raw": raw.decode("utf-8", "ignore"),
            })
        mode = STUB["mode"]
        if mode == "slow":
            time.sleep(3)
        if mode == "payment":
            return self._send(402, {"error": {"message": "go allowance exhausted"}})
        if mode == "auth":
            return self._send(401, {"error": {"message": "invalid key or usage not allowed"}})
        if mode == "quota":
            return self._send(429, {"error": {"message": "rate limited"}})
        if mode == "request":
            return self._send(400, {"error": {"message": "bad image payload"}})
        if mode == "server":
            return self._send(500, {"error": {"message": "upstream error"}})
        if mode == "malformed":
            return self._raw_send(200, b"not-a-json-body")
        if self.path.endswith("/messages"):
            return self._send(200, self._messages_payload(mode))
        return self._send(200, self._chat_payload(mode))

    def _messages_payload(self, mode):
        truncated = mode == "truncated"
        payload = {
            "id": "msg_stub_145",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": suggestion_json() if not truncated else "{\"question_text\":\"解方程"}],
            "stop_reason": "max_tokens" if truncated else "end_turn",
        }
        if mode != "nousage":
            source, cost = self._billing(mode)
            payload["usage"] = {
                "input_tokens": 2600,
                "output_tokens": 320,
                "cost": cost,
                "opencode": {"billing_source": source, "list_cost": 0.00712, "allowance": 0.00712},
            }
        return payload

    def _chat_payload(self, mode):
        truncated = mode == "truncated"
        payload = {
            "id": "chatcmpl_stub_145",
            "choices": [{
                "message": {"content": suggestion_json() if not truncated else "{\"question_text\":\"解方程"},
                "finish_reason": "length" if truncated else "stop",
            }],
        }
        if mode != "nousage":
            source, cost = self._billing(mode)
            payload["usage"] = {
                "prompt_tokens": 2400,
                "completion_tokens": 300,
                "total_tokens": 2700,
                "cost": cost,
                "opencode": {"billing_source": source, "list_cost": 0.0066, "allowance": 0.0066},
            }
        return payload

    @staticmethod
    def _billing(mode):
        if mode == "billing_balance":
            return "balance", 0
        if mode == "billing_cost":
            return "go", 0.004
        return "go", 0

    def _send(self, status, payload):
        self._raw_send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _raw_send(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


def stub_calls():
    with STUB["lock"]:
        return list(STUB["calls"])


def last_call():
    calls = stub_calls()
    return calls[-1] if calls else {}


def main():
    from logmeta import environment_lines

    for line in environment_lines():
        LOG.write(line)
    LOG.write("")
    os.environ.pop("MISTAKE_AI_MODE", None)
    os.environ.pop("MISTAKE_AI_API_KEY", None)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    os.environ.pop("MISTAKE_AI_BASE_URL", None)
    os.environ.pop("MISTAKE_AI_MODEL", None)
    os.environ.pop("MISTAKE_AI_TIMEOUT", None)
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", port), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_root = "http://127.0.0.1:%d" % port

    from app import vision
    from desktop import data_dir
    from desktop.service import ApiError, DesktopService

    LOG.write("== 一、地址、模式与请求地址拼接")
    check("版本号为当前版本", vision.VERSION == VERSION_EXPECT, vision.VERSION)
    default = vision.resolve_config({})
    check("没有配置时默认模拟", default["mode"] == "mock", default["mode"])
    check("默认不读取任何密钥", not default["key_configured"], default)
    go = vision.resolve_config({"mode": "go", "api_key": "k", "model": "qwen3.8-max", "session_id": "s"})
    check("Go 的 qwen3.8-max 走 Anthropic Messages 基底",
          go["base_url"] == "https://opencode.ai/inference/go/anthropic/v1", go["base_url"])
    check("Go 的 qwen3.8-max 完整地址是 /messages",
          go["request_url"] == "https://opencode.ai/inference/go/anthropic/v1/messages", go["request_url"])
    check("Go 的 qwen3.8-max 使用 messages 请求体", go["api"] == "messages", go["api"])
    chat = vision.resolve_config({"mode": "go", "api_key": "k", "model": "kimi-k3", "session_id": "s"})
    check("Go 的 kimi-k3 走 OpenAI 兼容基底",
          chat["base_url"] == "https://opencode.ai/inference/go/openai/v1", chat["base_url"])
    check("Go 的 kimi-k3 完整地址是 /chat/completions",
          chat["request_url"] == "https://opencode.ai/inference/go/openai/v1/chat/completions", chat["request_url"])
    check("基底已带 /messages 时不重复拼接",
          vision.request_url("https://x.test/v1/messages", "messages") == "https://x.test/v1/messages")
    check("基底已带 /chat/completions 时不重复拼接",
          vision.request_url("https://x.test/v1/chat/completions", "chat") == "https://x.test/v1/chat/completions")
    check("基底带尾斜杠也能正确拼接",
          vision.request_url("https://x.test/v1/", "chat") == "https://x.test/v1/chat/completions")
    nokey = vision.resolve_config({"mode": "go", "model": "qwen3.8-max"})
    check("选了 Go 但没密钥时仍是 go，不回退模拟", nokey["mode"] == "go", nokey["mode"])
    check("没密钥时说明会停止并提示", "停止" in nokey["reason"], nokey["reason"])
    console = vision.resolve_config({"mode": "real"})
    check("Console 模式仍保持原有 chat 地址",
          console["request_url"] == "https://opencode.ai/inference/openai/v1/chat/completions", console["request_url"])
    check("Console 模式不带 Go 会话标识", console["mode"] == "real", console["mode"])

    LOG.write("== 二、请求体、认证头与会话标识")
    job = {
        "image_bytes": png_bytes(), "image_mime": "image/png",
        "question_text": "解方程：3(x-2)=2x+5", "solution_text": "3x-6=2x+5",
        "trial": True,
    }
    provider = vision.CloudVisionProvider(go)
    body = provider.build_request(job)
    check("Anthropic 请求体带 system", isinstance(body.get("system"), str) and body["system"], body.get("system"))
    parts = body["messages"][0]["content"]
    check("Anthropic 图片块放在 text 之前", parts[0]["type"] == "image" and parts[1]["type"] == "text", parts)
    check("Anthropic 图片用 base64 + media_type",
          parts[0]["source"]["type"] == "base64" and parts[0]["source"]["media_type"] == "image/png"
          and parts[0]["source"]["data"], parts[0]["source"].keys())
    check("Anthropic 请求带 max_tokens", body.get("max_tokens") == 32000, body.get("max_tokens"))
    check("试分析提示写明不含学生身份", "不含学生身份" in parts[1]["text"], parts[1]["text"][:40])
    headers = provider._headers("sk-test-145")
    check("Go 请求带 X-OpenCode-Session", headers.get("X-OpenCode-Session") == "s", headers)
    check("User-Agent 是真实软件名与当前版本", headers.get("User-Agent") == "math-mistakes-desktop/" + VERSION_EXPECT, headers.get("User-Agent"))
    check("认证使用 Bearer", headers.get("Authorization") == "Bearer sk-test-145", headers.get("Authorization"))
    real_provider = vision.CloudVisionProvider(vision.resolve_config({"mode": "real", "api_key": "k"}))
    real_headers = real_provider._headers("k")
    check("Console 请求不带 Go 会话标识", "X-OpenCode-Session" not in real_headers, real_headers)
    chat_body = vision.CloudVisionProvider(chat).build_request(job)
    check("OpenAI 兼容请求把图片包成 data URL",
          chat_body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"),
          chat_body["messages"][1]["content"][0])
    no_session = dict(go, session_id="")
    try:
        vision.CloudVisionProvider(no_session)._headers("k")
        check("Go 缺少会话标识时拒绝发送", False, "没有抛错")
    except vision.AnalysisError as exc:
        check("Go 缺少会话标识时拒绝发送", exc.code == "config" and "会话标识" in exc.message, exc.message)

    LOG.write("== 三、用量与订阅来源归一化")
    usage = vision.normalise_usage({
        "input_tokens": 10, "output_tokens": 5, "cost": 0,
        "opencode": {"billing_source": "go", "list_cost": 0.1, "allowance": 0.08},
    })
    check("Anthropic 用量映射到 prompt/completion", usage["prompt_tokens"] == 10 and usage["completion_tokens"] == 5, usage)
    check("缺少 total 时按两项相加", usage["total_tokens"] == 15, usage)
    check("订阅来源取 opencode.billing_source", usage["billing_source"] == "go", usage)
    check("额度计入取 opencode.allowance", usage["allowance"] == 0.08, usage)
    check("服务未提供用量时保持 None", vision.normalise_usage(None) is None, vision.normalise_usage(None))
    zero_usage = vision.normalise_usage({
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0,
    })
    check("用量为 0 时保留 0 而不是缺失",
          zero_usage["prompt_tokens"] == 0 and zero_usage["completion_tokens"] == 0
          and zero_usage["total_tokens"] == 0 and zero_usage["cost"] == 0, zero_usage)

    LOG.write("== 四、桌面服务：默认模拟、保存 Go、试分析与发送内容")
    data_root = os.path.join(WORK, "data")
    data_dir.create_new(data_root)
    service = DesktopService(data_root, test_ai_base=stub_root)
    service.register("验证老师甲", "pw-go-145")
    cls = service.create_class("Go 验证班")
    student = service.create_student(cls["id"], "虚构学生甲")
    photo = make_photo(os.path.join(WORK, "题图-分数.png"))
    mistake = service.create_mistake(
        cls["id"], student["id"],
        {"question_text": "解方程：3(x-2)=2x+5", "solution_text": "3x-6=2x+5",
         "occurred_on": "2026-11-12", "source_type": "作业"},
        photo,
    )
    initial = service.ai_settings()
    check("新建数据后默认模拟", initial["mode"] == "mock", initial["mode"])
    check("默认没有发过任何请求", not stub_calls(), len(stub_calls()))

    saved = service.save_ai_settings("go", stub_root + "/go/anthropic/v1", "qwen3.8-max", SECRET)
    check("保存后模式为 go", saved["mode"] == "go", saved["mode"])
    check("服务名显示 OpenCode Go（订阅）", saved["service_name"] == "OpenCode Go（订阅）", saved.get("service_name"))
    check("保存后算出的完整地址是 Anthropic Messages",
          saved["request_url"].endswith("/messages"), saved.get("request_url"))
    check("密钥配置状态可见但不回显", saved["key_configured"] and SECRET not in json.dumps(saved, ensure_ascii=False), saved)
    meta = service.meta()
    check("meta 报告 go 模式", meta["ai_mode"] == "go", meta["ai_mode"])
    check("meta 报告 Go 服务名", meta["ai_provider"] == "OpenCode Go（订阅）", meta["ai_provider"])
    check("meta 给出真实请求地址", meta["ai_request_url"].endswith("/messages"), meta["ai_request_url"])
    check("meta 不包含密钥", SECRET not in json.dumps(meta, ensure_ascii=False), "密钥泄漏")
    check("保存设置本身不发送请求", not stub_calls(), len(stub_calls()))

    hits = len(stub_calls())
    try:
        service.trial_analysis(photo, "解方程：3(x-2)=2x+5", "3x-6=2x+5", consent=False)
        check("Go 未同意时拒绝发送", False, "没有抛错")
    except ApiError as exc:
        check("Go 未同意时拒绝发送", "同意" in exc.message and "OpenCode Go" in exc.message, exc.message)
    check("未同意时没有请求", len(stub_calls()) == hits, len(stub_calls()))

    trial = service.trial_analysis(photo, "解方程：3(x-2)=2x+5", "3x-6=2x+5", consent=True)
    check("Go 试分析成功", trial["ok"] and trial.get("suggestion"), trial.get("error"))
    check("试分析报告 go 模式", trial.get("mode") == "go", trial.get("mode"))
    check("试分析报告服务名与请求地址",
          trial.get("service") == "OpenCode Go（订阅）" and trial.get("request_url", "").endswith("/messages"), trial)
    trial_usage = trial.get("usage") or {}
    check("试分析回传订阅来源 go", trial_usage.get("billing_source") == "go", trial_usage)
    check("试分析回传零费用", trial_usage.get("cost") == 0, trial_usage)
    check("试分析回传额度计入", trial_usage.get("allowance") == 0.00712, trial_usage)
    check("试分析原始响应用于核对", (trial.get("raw_response") or {}).get("id") == "msg_stub_145", trial.get("raw_response"))
    call = last_call()
    check("请求走 Anthropic Messages 路径", call["path"] == "/messages", call.get("path"))
    check("请求带本次会话标识", call["headers"].get("x-opencode-session") == service._ai_session_id, call["headers"].get("x-opencode-session"))
    check("会话标识含软件名和版本", service._ai_session_id.startswith("math-mistakes-desktop-" + VERSION_EXPECT + "-"), service._ai_session_id)
    check("请求带真实 User-Agent", call["headers"].get("user-agent") == "math-mistakes-desktop/" + VERSION_EXPECT, call["headers"].get("user-agent"))
    check("请求用 Bearer 认证", call["headers"].get("authorization") == "Bearer " + SECRET, call["headers"].get("authorization"))
    check("请求体不含学生姓名", "虚构学生甲" not in call["raw"] and "Go 验证班" not in call["raw"], "泄漏身份")
    check("请求体实际带题图 base64", '"type": "image"' in call["raw"] and "base64" in call["raw"], call["raw"][:80])
    check("试分析成功后状态为已配置并验证", service.ai_settings()["verify_state"] == "ok", service.ai_settings()["verify_state"])
    check("试分析不创建新错题", len(service.list_mistakes(cls["id"], student["id"])) == 1, len(service.list_mistakes(cls["id"], student["id"])))

    LOG.write("== 五、失败原因、计费异常与不自动切换 Console")
    for mode, code in (("payment", "payment"), ("auth", "auth"), ("quota", "quota"),
                       ("request", "request"), ("server", "server"), ("malformed", "format"),
                       ("truncated", "format")):
        STUB["mode"] = mode
        payload = service.trial_analysis(photo, consent=True)
        check("Go %s 映射为 %s" % (mode, code), not payload["ok"] and payload.get("error_code") == code, payload)
        if mode == "payment":
            text = payload.get("error") or ""
            check("Go 额度不足不再要求 Console 充值", "Go 订阅额度不足" in text and "Console 充值" not in text, text)
        if mode == "auth":
            check("Go 权限错误提到用途限制", "用途" in (payload.get("error") or ""), payload.get("error"))
        if mode == "truncated":
            check("截断明确提示没有可用结果", "截断" in (payload.get("error") or ""), payload.get("error"))
        if mode == "malformed":
            text = payload.get("error") or ""
            check("非 JSON 响应说明不是兼容接口", "不是有效的 JSON" in text and "兼容" in text, text)
        check("Go %s 失败后仍走 Go 路径" % mode, last_call()["path"] in ("/messages", "/chat/completions"), last_call()["path"])
    STUB["mode"] = "nousage"
    payload = service.trial_analysis(photo, consent=True)
    check("服务不返回用量时仍成功", payload["ok"], payload.get("error"))
    check("服务不返回用量时界面可判为尚未确认", payload.get("usage") is None, payload.get("usage"))
    STUB["mode"] = "slow"
    slow_cfg = vision.resolve_config({
        "mode": "go", "api_key": "k", "model": "qwen3.8-max", "session_id": "s",
        "timeout": 1, "test_base": stub_root, "allow_env": False,
    })
    try:
        vision.CloudVisionProvider(slow_cfg).analyze(job)
        check("Go 超时映射为 timeout", False, "没有抛错")
    except vision.AnalysisError as exc:
        check("Go 超时映射为 timeout", exc.code == "timeout", exc.message)
    STUB["mode"] = "ok"

    LOG.write("== 五之二、计费来源异常时停止后续发送")
    STUB["mode"] = "billing_balance"
    payload = service.trial_analysis(photo, consent=True)
    check("非订阅计费来源时试分析失败", not payload["ok"] and payload.get("error_code") == "billing", payload)
    check("非订阅来源提示核对账户设置", "核对" in (payload.get("error") or ""), payload.get("error"))
    blocked = service.ai_settings().get("billing_block_message") or ""
    check("出现异常后设置页记录已停止发送", "停止" in blocked, blocked)
    sent = len(stub_calls())
    second = service.trial_analysis(photo, consent=True)
    check("阻断期间不再发出请求", len(stub_calls()) == sent, len(stub_calls()) - sent)
    check("阻断期间仍提示先核对设置", not second["ok"] and "停止" in (second.get("error") or ""), second)
    service.save_ai_settings("go", "", "qwen3.8-max", None)
    check("重新保存设置后解除阻断", not service.ai_settings().get("billing_block_message"), service.ai_settings().get("billing_block_message"))
    STUB["mode"] = "billing_cost"
    payload = service.trial_analysis(photo, consent=True)
    check("Go 订阅也出现额外费用时失败", not payload["ok"] and payload.get("error_code") == "billing", payload)
    service.save_ai_settings("go", "", "qwen3.8-max", None)

    STUB["mode"] = "billing_balance"
    before = len(stub_calls())
    formal = service.preview_analysis(mistake["id"], consent=True)
    check("正式分析同样拒绝非订阅计费来源", not formal.get("ok") and formal.get("error_code") == "billing", formal)
    check("正式分析确实发出了这次请求", len(stub_calls()) == before + 1, len(stub_calls()) - before)
    check("正式分析异常后记录已停止发送", "停止" in (service.ai_settings().get("billing_block_message") or ""))
    after_first = len(stub_calls())
    blocked_formal = service.preview_analysis(mistake["id"], consent=True)
    check("阻断后正式分析不再发出请求", len(stub_calls()) == after_first, len(stub_calls()) - after_first)
    check("阻断后正式分析仍提示先核对设置",
          not blocked_formal.get("ok") and "停止" in (blocked_formal.get("error") or ""), blocked_formal)
    STUB["mode"] = "ok"

    LOG.write("== 六、OpenAI 兼容模型的图片分析")
    service.save_ai_settings("go", stub_root + "/go/openai/v1", "kimi-k3", None)
    chat_trial = service.trial_analysis(photo, consent=True)
    check("chat 模型试分析成功", chat_trial["ok"], chat_trial.get("error"))
    check("chat 模型走 /chat/completions", last_call()["path"] == "/chat/completions", last_call()["path"])
    chat_body = last_call()["body"]
    check("chat 模型仍读取题图", chat_body["messages"][1]["content"][0]["type"] == "image_url", chat_body["messages"][1]["content"][0])
    check("chat 模型用量仍标 go", ((chat_trial.get("usage") or {}).get("billing_source")) == "go", chat_trial.get("usage"))

    LOG.write("== 七、正式分析失败保留内容、老师确认与重开")
    service.save_ai_settings("go", stub_root + "/go/anthropic/v1", "qwen3.8-max", None)
    STUB["mode"] = "payment"
    failed = service.preview_analysis(mistake["id"], consent=True)
    check("正式分析失败不产生建议", not failed.get("ok") and failed.get("error_code") == "payment", failed)
    after = service.get_mistake(mistake["id"])
    check("失败后题目文字保留", after["question_text"] == "解方程：3(x-2)=2x+5", after["question_text"])
    check("失败后解题过程保留", after["solution_text"] == "3x-6=2x+5", after["solution_text"])
    check("失败后照片保留", bool(after.get("has_image")), after.get("has_image"))
    check("失败后记录仍待核对", after["status"] == "pending", after["status"])
    service.record_analysis_failure(mistake["id"], failed.get("error") or "分析失败", failed.get("error_code") or "")
    after = service.get_mistake(mistake["id"])
    check("失败原因写入记录但不改内容", "Go" in (after.get("ai_error") or "") and after["question_text"], after.get("ai_error"))
    STUB["mode"] = "ok"
    preview = service.preview_analysis(mistake["id"], consent=True)
    check("重试成功生成待核对建议", preview.get("ok") and preview.get("preview_id"), preview)
    check("预览阶段不直接改记录", service.get_mistake(mistake["id"])["status"] == "pending")
    applied = service.apply_analysis(mistake["id"], preview["preview_id"])
    check("采用建议后进入待核对", applied["status"] == "pending", applied["status"])
    check("采用建议后知识点写入", applied["knowledge_points"] == ["一元一次方程"], applied["knowledge_points"])
    confirmed = service.confirm_mistake(mistake["id"], applied.get("base_fingerprint") or "")
    check("老师确认后状态为已确认", confirmed["status"] == "confirmed", confirmed["status"])
    reopened = DesktopService(data_root, test_ai_base=stub_root)
    reopened.login("验证老师甲", "pw-go-145")
    persisted = reopened.get_mistake(mistake["id"])
    check("重开程序后状态仍为已确认", persisted["status"] == "confirmed", persisted["status"])
    check("重开程序后分析内容仍在", (persisted.get("ai") or {}).get("advice_text"), persisted.get("ai"))

    LOG.write("== 八、账号隔离、退出清密钥与迟到结果")
    sid_one = service._ai_session_id
    stale = service.preview_analysis(mistake["id"], consent=True)
    service.logout()
    service.register("验证老师乙", "pw-go-145-b")
    try:
        service.apply_analysis(mistake["id"], stale.get("preview_id") or "")
        check("换账号后旧建议不能采用", False, "没有抛错")
    except ApiError as exc:
        check("换账号后旧建议不能采用", True, exc.message)
    other = service.ai_settings()
    check("另一个账号默认模拟", other["mode"] == "mock", other["mode"])
    check("另一个账号没有沿用前一个账号的密钥", not other["key_configured"], other)
    service.save_ai_settings("go", stub_root + "/go/anthropic/v1", "qwen3.8-max", "sk-go-145-second")
    other_headers = service.trial_analysis(photo, consent=True)
    check("另一个账号可以独立试分析", other_headers["ok"], other_headers.get("error"))
    check("另一个账号的会话标识不同", service._ai_session_id != sid_one, service._ai_session_id)
    service.logout()
    service.login("验证老师甲", "pw-go-145")
    back = service.ai_settings()
    check("回到原账号模式仍是 go", back["mode"] == "go", back["mode"])
    check("回到原账号本次登录密钥已清", not back["key_configured"], back)
    check("回到原账号会话标识重新生成", service._ai_session_id != sid_one, service._ai_session_id)
    check("回到原账号提示未配置密钥会停止", "停止" in back["reason"], back["reason"])

    LOG.write("== 九、密钥不落库")
    leaked = scan_secret(data_root, SECRET)
    check("数据库没有保存密钥", not leaked, leaked)
    leaked2 = scan_secret(data_root, "sk-go-145-second")
    check("数据库没有保存第二个账号的密钥", not leaked2, leaked2)

    LOG.write("== 十、从 1.4.4 升级不自动上云")
    upgrade_root = os.path.join(WORK, "data-upgrade")
    data_dir.create_new(upgrade_root)
    legacy = DesktopService(upgrade_root, test_ai_base=stub_root)
    legacy.register("升级老师丙", "pw-upgrade-145")
    legacy_id = int(legacy.user["id"])
    legacy._set_meta_value("ai_base_url:%d" % legacy_id, stub_root + "/go/anthropic/v1")
    legacy._set_meta_value("ai_model:%d" % legacy_id, "qwen3.8-max")
    upgraded = DesktopService(upgrade_root, test_ai_base=stub_root)
    upgraded.login("升级老师丙", "pw-upgrade-145")
    up = upgraded.ai_settings()
    check("旧版只留地址和模型时升级后默认模拟", up["mode"] == "mock", up["mode"])
    check("升级后模式显示为模拟分析", up["service_name"] == "模拟分析", up["service_name"])
    check("升级后没有密钥", not up["key_configured"], up)
    before = len(stub_calls())
    upgraded.trial_analysis(photo, consent=True)
    check("升级后试分析不发送请求", len(stub_calls()) == before, len(stub_calls()) - before)
    check("升级后 meta 报告模拟", upgraded.meta()["ai_mode"] == "mock", upgraded.meta()["ai_mode"])

    LOG.write("== 十一、保留旧 Console 环境变量时选择 Go")
    os.environ["MISTAKE_AI_MODE"] = "real"
    os.environ["MISTAKE_AI_BASE_URL"] = "https://opencode.ai/inference/openai/v1"
    os.environ["MISTAKE_AI_MODEL"] = "qwen3.8-max"
    os.environ["MISTAKE_AI_API_KEY"] = "sk-env-should-be-ignored"
    env_root = os.path.join(WORK, "data-env")
    data_dir.create_new(env_root)
    env_service = DesktopService(env_root, test_ai_base=stub_root)
    env_service.register("环境老师丁", "pw-env-145")
    st = env_service.ai_settings()
    check("有旧 Console 环境变量时首次启动仍是模拟", st["mode"] == "mock", st["mode"])
    check("旧环境变量密钥不被采用", not st["key_configured"], st)
    prod_root = os.path.join(WORK, "data-env-prod")
    data_dir.create_new(prod_root)
    prod = DesktopService(prod_root)
    prod.register("环境老师戊", "pw-env-145-p")
    check("无账号设置时旧 MISTAKE_AI_MODE=real 不生效", prod.ai_settings()["mode"] == "mock", prod.ai_settings()["mode"])
    prod.save_ai_settings("go", "https://opencode.ai/inference/openai/v1", "qwen3.8-max", SECRET)
    ps = prod.ai_settings()
    check("Go 不采用传入的 Console 基底", ps["base_url"] == "https://opencode.ai/inference/go/anthropic/v1", ps["base_url"])
    check("Go 完整地址没有继承 Console", ps["request_url"] == "https://opencode.ai/inference/go/anthropic/v1/messages", ps["request_url"])
    check("保存 Go 不把旧 Console 地址写进账号", not ps["account_base_url"], ps["account_base_url"])
    env_service.save_ai_settings("go", "", "qwen3.8-max", SECRET)
    env_trial = env_service.trial_analysis(photo, consent=True)
    check("旧环境变量存在时 Go 试分析仍走测试 Go 路径", env_trial["ok"] and last_call()["path"] == "/messages", last_call()["path"])
    check("请求密钥用本次登录输入而不是环境变量", last_call()["headers"].get("authorization") == "Bearer " + SECRET, last_call()["headers"].get("authorization"))
    sent = len(stub_calls())
    bad_go = vision.resolve_config({"mode": "go", "api_key": "k", "model": "qwen3.8-max", "session_id": "s", "base_url": "https://opencode.ai/inference/openai/v1"})
    check("Go 解析结果仍固定为 Go 地址", vision.is_go_base(bad_go["base_url"]), bad_go["base_url"])
    guard = {"mode": "go", "api": "chat", "api_key": "k", "model": "qwen3.8-max",
             "base_url": "https://opencode.ai/inference/openai/v1", "session_id": "s", "timeout": 5}
    try:
        vision.CloudVisionProvider(guard).analyze(job)
        check("Go 指向 Console 地址时停止发送", False, "没有抛错")
    except vision.AnalysisError as exc:
        check("Go 指向 Console 地址时停止发送", exc.code == "config" and "停止" in exc.message, exc.message)
    check("地址冲突时没有发出请求", len(stub_calls()) == sent, len(stub_calls()) - sent)
    env_service.logout()
    env_service.register("环境老师己", "pw-env-145-c")
    st = env_service.ai_settings()
    check("换账号后模式回到模拟", st["mode"] == "mock", st["mode"])
    check("换账号后环境变量密钥仍不被采用", not st["key_configured"], st)
    env_service.logout()
    env_service.login("环境老师丁", "pw-env-145")
    st = env_service.ai_settings()
    check("换回原账号仍是 go", st["mode"] == "go", st["mode"])
    check("换回原账号密钥已清除", not st["key_configured"], st)
    for name in ("MISTAKE_AI_MODE", "MISTAKE_AI_BASE_URL", "MISTAKE_AI_MODEL", "MISTAKE_AI_API_KEY"):
        os.environ.pop(name, None)

    LOG.write("== 十二、自定义 AI 服务（OpenAI 兼容 Chat Completions）")
    STUB["mode"] = "ok"
    custom_base = stub_root + "/custom/v1"
    custom = vision.resolve_config({
        "mode": "custom", "api_key": "k", "base_url": custom_base + "/",
        "model": "vision-x", "service_name": "测试云", "allow_env": False,
    })
    check("自定义服务末尾斜杠不会重复拼接",
          custom["request_url"] == custom_base + "/chat/completions", custom["request_url"])
    full_path = vision.resolve_config({
        "mode": "custom", "api_key": "k", "base_url": custom_base + "/chat/completions",
        "model": "vision-x", "allow_env": False,
    })
    check("自定义服务填完整请求路径也不重复拼接",
          full_path["request_url"] == custom_base + "/chat/completions", full_path["request_url"])
    bad_query = vision.resolve_config({
        "mode": "custom", "api_key": "k", "base_url": custom_base + "?key=1", "model": "vision-x",
    })
    check("自定义服务地址带查询参数时拒绝", not bad_query["address_ok"], bad_query["address_error"])
    bad_go = vision.resolve_config({
        "mode": "custom", "api_key": "k", "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "vision-x",
    })
    check("自定义服务不能使用 Go 订阅地址", not bad_go["address_ok"] and "Go" in bad_go["address_error"], bad_go["address_error"])
    no_key = vision.resolve_config({"mode": "custom", "base_url": custom_base, "model": "vision-x"})
    check("自定义服务缺密钥时仍是 custom，不回退模拟", no_key["mode"] == "custom", no_key["mode"])

    custom_root = os.path.join(WORK, "data-custom")
    data_dir.create_new(custom_root)
    svc = DesktopService(custom_root, test_ai_base=stub_root)
    svc.register("自定义老师庚", "pw-custom-146")
    cls2 = svc.create_class("自定义验证班")
    stu2 = svc.create_student(cls2["id"], "虚构学生乙")
    saved = svc.save_ai_settings("custom", custom_base + "/", "vision-x", "sk-custom-146", service_name="测试云")
    check("保存自定义服务后模式为 custom", saved["mode"] == "custom", saved["mode"])
    check("界面显示自定义服务名称", saved["service_name"] == "测试云", saved["service_name"])
    check("界面显示最终请求地址", saved["request_url"] == custom_base + "/chat/completions", saved["request_url"])
    check("自定义模式固定 chat 接口", saved["api"] == "chat", saved["api"])
    meta_custom = svc.meta()
    check("meta 报告自定义服务名", meta_custom["ai_provider"] == "测试云", meta_custom["ai_provider"])
    check("meta 给出实际请求地址", meta_custom["ai_request_url"] == custom_base + "/chat/completions", meta_custom["ai_request_url"])
    check("meta 不含密钥", "sk-custom-146" not in str(meta_custom), "leak")

    before = len(stub_calls())
    try:
        svc.trial_analysis(photo)
        check("自定义服务未同意时拒绝发送", False, "没有抛错")
    except ApiError as exc:
        check("自定义服务未同意时拒绝发送", "同意" in exc.message and "测试云" in exc.message, exc.message)
    check("未同意时没有请求", len(stub_calls()) == before, len(stub_calls()) - before)

    trial = svc.trial_analysis(photo, "解方程：2(x-3)=5x+6", "2x-3=5x+6", consent=True)
    check("自定义服务试分析成功", trial["ok"], trial)
    check("试分析走自定义 chat 路径", last_call()["path"] == "/custom/v1/chat/completions", last_call()["path"])
    call = last_call()
    check("请求用 Bearer 认证", call["headers"].get("authorization") == "Bearer sk-custom-146", call["headers"].get("authorization"))
    check("自定义请求不带 Go 会话标识", "x-opencode-session" not in call["headers"], call["headers"])
    check("自定义请求带真实 User-Agent", call["headers"].get("user-agent") == "math-mistakes-desktop/" + VERSION_EXPECT, call["headers"].get("user-agent"))
    image_part = call["body"]["messages"][1]["content"][0]
    check("请求体实际带题图", image_part["image_url"]["url"].startswith("data:image/png;base64,"), image_part["type"])
    check("试分析回传用量", (trial.get("usage") or {}).get("prompt_tokens"), trial.get("usage"))

    STUB["mode"] = "nousage"
    no_usage = svc.trial_analysis(photo, consent=True)
    check("服务不返回用量时仍能成功", no_usage["ok"], no_usage)
    check("服务不返回用量时不推断费用", no_usage.get("usage") is None, no_usage.get("usage"))
    check("服务不返回用量时状态标尚未确认", "尚未确认" in (svc.ai_settings().get("verify_message") or ""),
          svc.ai_settings().get("verify_message"))
    STUB["mode"] = "ok"

    mistake2 = svc.create_mistake(
        cls2["id"], stu2["id"],
        {"question_text": "解方程：2(x-3)=5x+6", "solution_text": "2x-3=5x+6",
         "occurred_on": "2026-11-13", "source_type": "作业"},
        photo,
    )
    preview = svc.preview_analysis(mistake2["id"], consent=True, task_id="custom-preview")
    check("自定义服务正式分析成功", preview.get("ok"), preview)
    applied = svc.apply_analysis(mistake2["id"], preview["preview_id"])
    check("自定义建议可以进入待核对", applied.get("status") == "pending", applied.get("status"))
    svc.confirm_mistake(mistake2["id"], applied.get("base_fingerprint") or "")
    reopened = DesktopService(custom_root, test_ai_base=stub_root)
    reopened.login("自定义老师庚", "pw-custom-146")
    detail = reopened.get_mistake(mistake2["id"])
    check("自定义分析确认后重开仍已确认", detail.get("status") == "confirmed", detail.get("status"))
    check("自定义分析内容已保存", detail.get("ai_mode") == "custom" and bool(detail.get("advice_text")), detail.get("ai_mode"))
    reopened.logout()

    for mode, code in (("auth", "auth"), ("payment", "payment"), ("request", "request"),
                       ("quota", "quota")):
        STUB["mode"] = mode
        payload = svc.trial_analysis(photo, consent=True)
        check("自定义 %s 映射为 %s" % (mode, code), not payload["ok"] and payload.get("error_code") == code, payload)
        check("自定义 %s 失败后仍走自定义路径" % mode, last_call()["path"] == "/custom/v1/chat/completions", last_call()["path"])
        if mode == "payment":
            check("自定义额度不足不再提示 Console 充值", "Console" not in payload.get("error", ""), payload.get("error"))
    STUB["mode"] = "slow"
    slow_cfg = vision.resolve_config({
        "mode": "custom", "api_key": "sk-custom-146", "base_url": custom_base + "/",
        "model": "vision-x", "service_name": "测试云", "timeout": 1,
    })
    try:
        vision.CloudVisionProvider(slow_cfg).analyze(job)
        check("自定义 slow 映射为 timeout", False, "没有抛错")
    except vision.AnalysisError as exc:
        check("自定义 slow 映射为 timeout", exc.code == "timeout", exc.message)
    check("自定义 slow 失败后仍走自定义路径", last_call()["path"] == "/custom/v1/chat/completions", last_call()["path"])
    STUB["mode"] = "ok"

    STUB["mode"] = "ok"
    before = len(stub_calls())
    guard = vision.resolve_config({
        "mode": "custom", "api_key": "k", "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "vision-x", "timeout": 5,
    })
    try:
        vision.CloudVisionProvider(guard).analyze(job)
        check("自定义指向 Go 地址时停止发送", False, "没有抛错")
    except vision.AnalysisError as exc:
        check("自定义指向 Go 地址时停止发送", exc.code == "config" and "停止" in exc.message, exc.message)
    check("自定义地址冲突时没有发出请求", len(stub_calls()) == before, len(stub_calls()) - before)

    svc.save_ai_settings("real", "https://console.example/v1", "console-model", None)
    mixed = svc.ai_settings()
    check("保存 Console 不影响自定义配置", mixed["account_custom_name"] == "测试云" and mixed["account_custom_base"] == custom_base,
          (mixed["account_custom_name"], mixed["account_custom_base"]))
    check("保存 Console 不影响自定义模型", mixed["account_custom_model"] == "vision-x", mixed["account_custom_model"])
    svc.save_ai_settings("go", "", "qwen3.8-max", None)
    mixed = svc.ai_settings()
    check("保存 Go 不覆盖 Console 地址", mixed["account_base_url"] == "https://console.example/v1", mixed["account_base_url"])
    check("保存 Go 不覆盖 Console 模型", mixed["account_model"] == "console-model", mixed["account_model"])
    check("保存 Go 单独记录 Go 模型", mixed["account_go_model"] == "qwen3.8-max", mixed["account_go_model"])

    svc.save_ai_settings("custom", custom_base + "/", "vision-x", None, service_name="测试云")
    back_custom = svc.ai_settings()
    check("切回自定义仍保留本次登录密钥", back_custom["session_key_configured"], back_custom)
    trial = svc.trial_analysis(photo, consent=True)
    check("切回自定义后仍用自己的密钥", last_call()["headers"].get("authorization") == "Bearer sk-custom-146",
          last_call()["headers"].get("authorization"))
    svc.save_ai_settings("custom", custom_base + "/v2", "vision-x", None, service_name="测试云")
    changed = svc.ai_settings()
    check("自定义地址变化后清除该服务密钥", not changed["session_key_configured"], changed)

    svc.logout()
    svc.register("自定义老师辛", "pw-custom-146-b")
    other = svc.ai_settings()
    check("换账号后自定义配置为空", not other["account_custom_base"], other["account_custom_base"])
    check("换账号后自定义密钥为空", not other["key_configured"], other)
    svc.logout()
    svc.login("自定义老师庚", "pw-custom-146")
    back = svc.ai_settings()
    check("换回原账号自定义配置仍在", back["account_custom_base"] == custom_base + "/v2", back["account_custom_base"])
    check("换回原账号自定义密钥已清除", not back["session_key_configured"], back)

    failed_count = sum(1 for _, ok, _ in CHECKS if not ok)
    LOG.write("")
    LOG.write("合计 %d 项，通过 %d 项，失败 %d 项" % (len(CHECKS), len(CHECKS) - failed_count, failed_count))
    code = 1 if failed_count else 0
    LOG.write("退出码：%d" % code)
    stub.shutdown()
    LOG.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
