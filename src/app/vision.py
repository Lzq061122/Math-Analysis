"""模拟、OpenCode Console、OpenCode Go 与自定义 AI 服务的视觉分析接入。

默认是模拟分析，不向任何外部服务发送照片或文字。只有老师在「AI 设置」里显式选择
「OpenCode Go（订阅）」「OpenCode Console（按量付费）」或「自定义 AI 服务」并填写
本次登录的密钥后，试分析、首次分析和重新分析才会真的发送；配置不完整时分析直接停止
并提示，不会退回模拟。

三种真实接口的请求格式：
  OpenCode Console：OpenAI Chat Completions，基底地址 https://opencode.ai/inference/openai/v1
  OpenCode Go：按模型的原生接口，qwen3.8-max 走 Anthropic Messages，
               基底地址 https://opencode.ai/inference/go/anthropic/v1
  自定义 AI 服务：OpenAI 兼容 Chat Completions，地址、模型由老师填写；
               服务商是否兼容、模型是否支持图片输入由老师自行确认。
Go 请求按官方要求带稳定会话标识 x-opencode-session，并使用本软件自己的名称与版本作为
User-Agent；不模拟其他客户端。自定义服务只发送本软件自己的 User-Agent 与 Bearer 密钥。

配置优先级：界面输入的密钥 > MISTAKE_AI_API_KEY > DASHSCOPE_API_KEY；账号保存的地址/模型 >
MISTAKE_AI_BASE_URL / DASHSCOPE_BASE_URL / MISTAKE_AI_MODEL > 默认值。桌面版总是显式传入
模式，环境变量 MISTAKE_AI_MODE 只作为非桌面场景的兜底。

环境变量：
  MISTAKE_AI_API_KEY  可选，本次启用云端时使用
  DASHSCOPE_API_KEY   可选，兼容旧配置；仅在 MISTAKE_AI_API_KEY 未设置时使用
  MISTAKE_AI_MODEL    可选，覆盖默认 qwen3.8-max
  MISTAKE_AI_BASE_URL 可选，覆盖默认 Console 地址
  MISTAKE_AI_TIMEOUT  可选，单次调用超时秒数，默认 180
  MISTAKE_AI_MODE     可选，mock / real / go；桌面版不使用
"""

import base64
import json
import os
import socket
import urllib.error
import urllib.request

from .analysis import ERROR_TYPES, KNOWLEDGE_POINTS
from .db import now_iso
from .version import VERSION

MODE_MOCK = "mock"
MODE_REAL = "real"
MODE_GO = "go"
MODE_CUSTOM = "custom"
REMOTE_MODES = (MODE_REAL, MODE_GO, MODE_CUSTOM)

SERVICE_NAME = "OpenCode Console"
SERVICE_GO = "OpenCode Go（订阅）"
SERVICE_CUSTOM = "自定义 AI 服务"
DEFAULT_MODEL = "qwen3.8-max"
DEFAULT_BASE_URL = "https://opencode.ai/inference/openai/v1"
DEFAULT_TIMEOUT = 180.0

GO_DEFAULT_MODEL = "qwen3.8-max"
GO_DEFAULT_BASE_URL = "https://opencode.ai/inference/go/anthropic/v1"
GO_CHAT_BASE_URL = "https://opencode.ai/inference/go/openai/v1"

# 订阅内明确支持图片输入的模型；api 是模型在 Go 里的原生接口，base_url 是对应的基底地址。
GO_MODELS = {
    "qwen3.8-max": {
        "api": "messages",
        "base_url": GO_DEFAULT_BASE_URL,
        "note": "中文手写与几何图优先，输入 $2.00 / 输出 $6.00 每百万 tokens",
    },
    "qwen3.8-flash": {
        "api": "messages",
        "base_url": GO_DEFAULT_BASE_URL,
        "note": "更快更省，输入 $0.15 / 输出 $0.47 每百万 tokens",
    },
    "claude-haiku-5-5": {
        "api": "messages",
        "base_url": GO_DEFAULT_BASE_URL,
        "note": "英文与公式清晰时较快，输入 $0.10 / 输出 $0.50 每百万 tokens",
    },
    "kimi-k3": {
        "api": "chat",
        "base_url": GO_CHAT_BASE_URL,
        "note": "输入 $3.00 / 输出 $15.00 每百万 tokens",
    },
    "mimo-v2.6-pro": {
        "api": "chat",
        "base_url": GO_CHAT_BASE_URL,
        "note": "输入 $0.435 / 输出 $0.87 每百万 tokens",
    },
    "deepseek-v4.1-flash": {
        "api": "chat",
        "base_url": GO_CHAT_BASE_URL,
        "note": "峰谷计价，输入 $0.15~0.30 / 输出 $0.60~1.20 每百万 tokens",
    },
    "minimax-m3.1-flash": {
        "api": "chat",
        "base_url": GO_CHAT_BASE_URL,
        "note": "输入 $0.15 / 输出 $0.60 每百万 tokens",
    },
}

PRICE_NOTE = (
    "OpenCode Console 按量计费；qwen3.8-max 官方目录价：输入 $2.00 / 百万 tokens，"
    "输出 $6.00 / 百万 tokens，缓存读取 $0.25 / 百万 tokens。"
    "实际扣费以接口返回的 usage.cost 与 OpenCode Console 账单为准；本软件不估算费用。"
)
GO_PRICE_NOTE = (
    "OpenCode Go 订阅内按月额度计费，订阅额度不额外扣余额；qwen3.8-max 目录价："
    "输入 $2.00 / 百万 tokens，输出 $6.00 / 百万 tokens。"
    "每次请求的订阅来源、用量和额度计入以接口返回的 usage.opencode 为准；"
    "服务没有返回时界面显示「尚未确认」，本软件不按零费用推断订阅来源。"
)
GO_POLICY_NOTE = (
    "OpenCode 官方文档写明 Go 面向编程代理（coding agents）的请求；本软件不是编程代理，"
    "请先向 OpenCode 确认这种用途被允许，再启用 Go 分析。"
)
POLICY_URL = "https://opencode.ai/legal/terms-of-service"
POLICY_SUMMARY = (
    "OpenCode Console 的提示词会转发给上游模型提供方；OpenCode 表示提示词不存储，"
    "第三方模型仍适用其自身的数据保留政策。qwen3.8-max 在 Console 模型页列为"
    "零保留、不用于训练。发送前请确认学校/机构具备处理学生作答内容的合法依据，"
    "并取得必要同意。"
)
GO_POLICY_SUMMARY = (
    "OpenCode Go 的提示词会转发给上游模型提供方；qwen3.8-max 在 Go 模型目录列为"
    "不用于训练、保留 0 天。OpenCode 文档写明 Go 面向编程代理的请求，本软件不是编程代理，"
    "发送前请确认官方允许这种用途。"
)
SERVICE_MOCK = "模拟分析"
MOCK_PRICE_NOTE = "模拟分析不联网、不读取照片，不产生费用。"
MOCK_POLICY_SUMMARY = "模拟分析不读取照片像素，也不会把任何内容发送到云端服务。"
CUSTOM_PRICE_NOTE = (
    "自定义 AI 服务的费用、用量和计费来源以服务商实际返回为准；服务没有返回时，"
    "界面显示「尚未确认」，本软件不估算费用，也不推断免费或属于某种订阅。"
)
CUSTOM_POLICY_SUMMARY = (
    "照片、题目和解题过程会发送给老师选择的服务商，由该服务商按其条款与隐私政策处理；"
    "本软件无法控制对方的留存与使用。发送前请确认学校/机构具备处理学生作答内容的合法依据。"
)

SYSTEM_PROMPT = """你是一名谨慎的初中数学老师助理，只根据照片内容分析学生的错题。
必须遵守：
1. 只写照片里能看清的内容，看不清、残缺或模糊就如实说信息不足，不要补全题目或编造步骤。
2. 把「观察到的错误」和「推测的原因」严格分开：观察要指向具体步骤或式子；原因用「可能/疑似」，不得下结论说学生粗心、不认真或没掌握。
3. 如果学生的解答看起来正确，或你无法判断有错，把 likely_correct 设为 true，不要强行判错。
4. 只输出一个 JSON 对象，不要输出解释文字、Markdown 代码块或多余字段。
5. 不要输出学生姓名、班级、学校等身份信息。"""

OUTPUT_SPEC = """请输出以下 JSON（字段名固定，列表最多 6 条）：
{
  "question_text": "识别出的题目，保留关键数学式",
  "solution_text": "识别出的学生解题过程，按行保留；看不清就写已看清的部分",
  "knowledge_points": ["从下列知识点中选 1-3 个"],
  "error_type": "从下列错误类型中选 1 个；判断不出就留空字符串",
  "observed_errors": ["照片上能直接看到的具体错误及位置，例如：第 2 行移项后 -3 写成 3"],
  "possible_causes": "可能原因，用推测语气；多条用分号隔开；判断不出写「暂不确定」",
  "advice_text": "针对这道题或这位学生的教学/学习建议",
  "insufficient": false,
  "insufficient_reason": "信息不足时写明缺什么；否则空字符串",
  "likely_correct": false
}
知识点：%s
错误类型：%s"""


class AnalysisError(Exception):
    """真实分析失败。code 用于前端区分原因，message 直接展示给老师。"""

    def __init__(self, message, code="error", retryable=True):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


def service_name(mode, custom_name=""):
    if mode == MODE_GO:
        return SERVICE_GO
    if mode == MODE_MOCK:
        return SERVICE_MOCK
    if mode == MODE_CUSTOM:
        return (custom_name or "").strip() or SERVICE_CUSTOM
    return SERVICE_NAME


def price_note(mode):
    if mode == MODE_GO:
        return GO_PRICE_NOTE
    if mode == MODE_MOCK:
        return MOCK_PRICE_NOTE
    if mode == MODE_CUSTOM:
        return CUSTOM_PRICE_NOTE
    return PRICE_NOTE


def policy_summary(mode):
    if mode == MODE_GO:
        return GO_POLICY_SUMMARY
    if mode == MODE_MOCK:
        return MOCK_POLICY_SUMMARY
    if mode == MODE_CUSTOM:
        return CUSTOM_POLICY_SUMMARY
    return POLICY_SUMMARY


def _clean_base(value):
    """去掉末尾斜杠和用户误贴进来的完整请求路径，只保留基底地址。"""
    base = str(value or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/messages", "/responses"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def is_go_base(value):
    """只认 https://opencode.ai/inference/go/ 下的 Go 订阅地址。"""
    parsed = urllib.parse.urlparse(_clean_base(value))
    return (
        parsed.scheme == "https"
        and parsed.netloc == "opencode.ai"
        and parsed.path.startswith("/inference/go/")
    )


def is_loopback_base(value):
    """测试地址只允许本机回环，避免正式版本把请求发到外部测试服务器。"""
    parsed = urllib.parse.urlparse(_clean_base(value))
    return parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "localhost", "::1")


def custom_address_error(value):
    """检查老师填写的自定义服务地址；返回空字符串表示可用。"""
    base = _clean_base(value)
    if not base:
        return "请填写自定义服务的接口地址。"
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "自定义服务地址要以 http:// 或 https:// 开头，并包含主机名。"
    if parsed.query or parsed.fragment:
        return "自定义服务地址不能带查询参数或 # 片段，请只填服务商给的基础地址。"
    if is_go_base(base):
        return "自定义服务不能使用 OpenCode Go 订阅地址，请改用服务商提供的接口地址。"
    return ""


def custom_request_url(value):
    """显示自定义服务最终会请求的完整地址，末尾斜杠和完整路径都不会重复拼接。"""
    return request_url(value, "chat")


def request_url(base_url, api):
    """把基底地址和完整请求地址拼好；已经带完整路径时不再重复拼接。"""
    base = _clean_base(base_url)
    path = "/messages" if api == "messages" else "/chat/completions"
    return base if base.endswith(path) else base + path


def _go_spec(model, base_url=""):
    spec = GO_MODELS.get(model)
    if spec:
        return spec
    # 账号里保存了未知模型名时，按地址判断接口类型，不静默改成别的模型。
    api = "messages" if "/anthropic/" in (base_url or "") else "chat"
    return {"api": api, "base_url": GO_DEFAULT_BASE_URL, "note": ""}


def resolve_config(config=None):
    """合并界面、账号与显式测试配置；返回内部使用的完整配置（含密钥，不对外展示）。

    allow_env=False 时环境变量完全不参与：默认模式、地址、模型和密钥都只认界面输入。
    桌面端固定这样，旧版 Console 环境变量不会影响默认模拟、Go 地址或自定义服务。
    """
    supplied = dict(config or {})
    allow_env = bool(supplied.get("allow_env", True))
    explicit_key = str(supplied.get("api_key") or "").strip()
    explicit_base = str(supplied.get("base_url") or "").strip()
    explicit_model = str(supplied.get("model") or "").strip()
    explicit_mode = str(supplied.get("mode") or "").strip().lower()
    custom_name = str(supplied.get("service_name") or "").strip()
    test_base = _clean_base(supplied.get("test_base") or "")

    env_key = (os.environ.get("MISTAKE_AI_API_KEY") or "").strip() if allow_env else ""
    env_key_fallback = (os.environ.get("DASHSCOPE_API_KEY") or "").strip() if allow_env else ""
    env_base = (
        (os.environ.get("MISTAKE_AI_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL") or "").strip()
        if allow_env
        else ""
    )
    env_model = (os.environ.get("MISTAKE_AI_MODEL") or "").strip() if allow_env else ""
    env_mode = (os.environ.get("MISTAKE_AI_MODE") or "").strip().lower() if allow_env else ""

    key = explicit_key or env_key or env_key_fallback
    key_source = "本次登录输入" if explicit_key else ("环境变量" if key else "未配置")

    requested = explicit_mode or env_mode
    if requested not in (MODE_MOCK,) + REMOTE_MODES:
        requested = MODE_MOCK

    if requested == MODE_GO:
        model = explicit_model or env_model or GO_DEFAULT_MODEL
        spec = _go_spec(model)
        api = spec["api"]
        if test_base:
            base_url = test_base
            base_source = "测试配置"
        else:
            # Go 地址只由所选模型决定：不继承旧 Console 地址，也不读环境变量。
            base_url = _clean_base(spec["base_url"])
            base_source = "按所选模型固定"
    elif requested == MODE_CUSTOM:
        # 自定义服务只认老师填写的地址与模型，没有默认值，也不继承 Console 配置。
        model = explicit_model or env_model
        base_url = _clean_base(explicit_base or env_base)
        api = "chat"
        base_source = "账号设置" if explicit_base else ("环境变量" if env_base else "未设置")
    else:
        model = explicit_model or env_model or DEFAULT_MODEL
        base_url = _clean_base(explicit_base or env_base or DEFAULT_BASE_URL)
        api = "chat"
        base_source = "账号设置" if explicit_base else ("环境变量" if env_base else "默认值")
    model_source = "账号设置" if explicit_model else ("环境变量" if env_model else "默认值")

    address_ok = True
    address_error = ""
    if requested == MODE_GO:
        if test_base and not is_loopback_base(test_base):
            address_ok = False
            address_error = "测试地址只允许本机回环地址（127.0.0.1 或 localhost），已停止发送。"
        elif not test_base and not is_go_base(base_url):
            address_ok = False
            address_error = "Go 模式的目标地址不是 Go 订阅接口：%s。已停止发送。" % base_url
    elif requested == MODE_CUSTOM:
        address_error = custom_address_error(base_url)
        address_ok = not address_error
    elif requested == MODE_REAL and not test_base and is_go_base(base_url):
        address_ok = False
        address_error = "Console 模式不能使用 Go 订阅地址：%s。已停止发送。" % base_url

    try:
        timeout = float(supplied.get("timeout") or os.environ.get("MISTAKE_AI_TIMEOUT") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    label = service_name(requested, custom_name)
    reason = ""
    if requested == MODE_MOCK:
        mode = MODE_MOCK
        reason = "当前是模拟分析，不联网、不读取图片像素，也不会向外部发送任何数据。"
    elif not key:
        mode = requested
        reason = "已选择%s，但本次登录还没有填写密钥；分析会停止并提示，不会自动改用模拟。" % label
    elif not address_ok:
        mode = requested
        reason = address_error
    elif not base_url or not model:
        mode = requested
        reason = "已选择%s，但服务地址或模型为空；分析会停止并提示，不会自动改用模拟。" % label
    else:
        mode = requested
        reason = "已配置%s，尚未验证；先做一次试分析再用于正式错题。" % label

    return {
        "mode": mode,
        "service": service_name(mode, custom_name),
        "custom_name": custom_name,
        "api": api,
        "api_key": key,
        "key_configured": bool(key),
        "key_source": key_source,
        "model": model,
        "model_source": model_source,
        "base_url": base_url,
        "base_url_source": base_source,
        "request_url": request_url(base_url, api),
        "session_id": str(supplied.get("session_id") or "").strip(),
        "timeout": timeout,
        "allow_env": allow_env,
        "test_base": test_base,
        "address_ok": address_ok,
        "address_error": address_error,
        "reason": reason,
    }


def describe_config(config=None):
    """返回可安全展示给前端的模式信息，绝不包含密钥。"""
    cfg = resolve_config(config)
    cfg.pop("api_key", None)
    return cfg


def _match_known(value, options):
    text = str(value or "").strip()
    if not text:
        return ""
    if text in options:
        return text
    for option in options:
        if option and (option in text or text in option):
            return option
    return ""


def _clean_list(value, limit=6):
    if isinstance(value, str):
        value = [part for part in (p.strip(" -•\t") for p in value.replace("\n", "；").split("；")) if part]
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text[:300])
        if len(cleaned) >= limit:
            break
    return cleaned


def _extract_json(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    if start < 0:
        raise AnalysisError("AI 返回的内容里没有 JSON，未能得到结构化分析，可重试或手动填写。", "format")
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        raise AnalysisError("AI 返回的 JSON 无法解析，可重试或手动填写。", "format")
    if not isinstance(data, dict):
        raise AnalysisError("AI 返回的结构不是对象，可重试或手动填写。", "format")
    return data


def _normalise(data, model, usage=None, mode=MODE_REAL):
    if not isinstance(data, dict):
        raise AnalysisError("AI 返回的结构不是对象，可重试或手动填写。", "format")
    meaningful = {
        "question_text",
        "solution_text",
        "knowledge_points",
        "error_type",
        "observed_errors",
        "possible_causes",
        "advice_text",
        "insufficient",
        "likely_correct",
    }
    if not meaningful.intersection(data):
        raise AnalysisError("AI 返回的内容缺少题目、解答或错误说明等必要字段，可重试或手动填写。", "format")
    question = str(data.get("question_text") or "").strip()[:4000]
    solution = str(data.get("solution_text") or "").strip()[:6000]
    knowledge = []
    for item in _clean_list(data.get("knowledge_points"), 3):
        point = _match_known(item, KNOWLEDGE_POINTS)
        if point and point not in knowledge:
            knowledge.append(point)
    observed = _clean_list(data.get("observed_errors"))
    causes = _clean_list(data.get("possible_causes"), 4)
    error_type = _match_known(data.get("error_type"), ERROR_TYPES)
    likely_correct = bool(data.get("likely_correct"))
    insufficient = bool(data.get("insufficient"))
    reason = str(data.get("insufficient_reason") or "").strip()[:500]

    if not insufficient and not observed and not error_type and not likely_correct:
        insufficient = True
        reason = reason or "模型没有指出具体错误，请老师对照原图核对或手动补充。"
    if insufficient and not reason:
        reason = "模型表示照片信息不足，未能给出确定结论。"

    advice = str(data.get("advice_text") or "").strip()[:1500]
    if insufficient and not advice:
        advice = "信息不足：请重新拍摄清晰、完整的题目与解题过程，或在核对页手动填写后重新分析。"

    result = {
        "mode": mode,
        "question_text": question,
        "solution_text": solution,
        "knowledge_points": knowledge,
        "error_type": "" if insufficient else error_type,
        "observed_errors": [] if insufficient else observed,
        "possible_causes": [] if insufficient else causes,
        "advice_text": advice,
        "insufficient": insufficient,
        "insufficient_reason": reason,
        "likely_correct": bool(likely_correct and not insufficient),
        "basis": "真实 AI 分析（%s）：模型读取了照片并给出建议，所有内容都需要老师核对后才能确认。" % model,
        "model": model,
        "analyzed_at": now_iso(),
    }
    if usage:
        result["usage"] = usage
    return result


def normalise_usage(raw):
    """把两种接口的 usage 统一成界面能直接显示的结构；缺失的字段保留 None。"""
    if not isinstance(raw, dict):
        return None
    opencode = raw.get("opencode") if isinstance(raw.get("opencode"), dict) else {}
    prompt = raw.get("prompt_tokens")
    if prompt is None:
        prompt = raw.get("input_tokens")
    completion = raw.get("completion_tokens")
    if completion is None:
        completion = raw.get("output_tokens")
    total = raw.get("total_tokens")
    if total is None and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost": raw.get("cost"),
        "billing_source": opencode.get("billing_source"),
        "list_cost": opencode.get("list_cost"),
        "allowance": opencode.get("allowance"),
        "opencode": opencode or None,
    }


class CloudVisionProvider:
    """调用显式配置的云端接口：Console 用 Chat Completions，Go 用模型原生接口。"""

    def __init__(self, config):
        self.config = config
        self.mode = config.get("mode") or MODE_REAL
        self.api = config.get("api") or "chat"
        self.service = config.get("service") or service_name(self.mode, config.get("custom_name") or "")
        self.last_payload = None

    def _prompt(self, job):
        question = (job.get("question_text") or "").strip()
        solution = (job.get("solution_text") or "").strip()
        note = "老师已标注：图片不清晰或解题过程不完整。" if job.get("blurry") else ""
        lead = "这是一张不含学生身份信息的试分析题图。" if job.get("trial") else "以下是学生一道数学错题的照片。"
        return "%s%s\n%s\n\n老师已填写的题目（可能为空）：\n%s\n老师已填写的解题过程（可能为空）：\n%s" % (
            lead,
            note,
            OUTPUT_SPEC % ("、".join(KNOWLEDGE_POINTS), "、".join(ERROR_TYPES)),
            question or "（空）",
            solution or "（空）",
        )

    def build_request(self, job):
        """构造请求体。单独成函数，便于测试时检查实际发送的内容。"""
        prompt = self._prompt(job)
        mime = job.get("image_mime") or "image/jpeg"
        data = base64.b64encode(job["image_bytes"]).decode("ascii")
        if self.api == "messages":
            return {
                "model": self.config["model"],
                "max_tokens": 32000,
                "temperature": 0.1,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            }
        return {
            "model": self.config["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, data)}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
            "temperature": 0.1,
            "max_tokens": 32000,
        }

    def _headers(self, api_key):
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "math-mistakes-desktop/%s" % VERSION,
        }
        if self.mode == MODE_GO:
            session = str(self.config.get("session_id") or "").strip()
            if not session:
                raise AnalysisError("缺少本次分析的会话标识，未发出请求。请退出后重新登录再试。", "config", False)
            headers["X-OpenCode-Session"] = session
        return headers

    def _target_error(self, base, test_base, what="照片"):
        """发送前核对目标地址与模式是否匹配；返回错误文案，没问题返回空串。"""
        if self.mode == MODE_GO:
            if test_base and not is_loopback_base(base):
                return "测试地址只允许本机回环地址，已停止发送。请重新选择模型或检查测试配置。"
            if not test_base and not is_go_base(base):
                return (
                    "Go 模式的目标地址不是 Go 订阅接口：%s。已停止发送，没有把%s发出去。"
                    "请打开「AI 设置」重新选择模型。" % (base, what)
                )
        elif self.mode == MODE_CUSTOM:
            error = custom_address_error(base)
            if error:
                return error + "已停止发送，没有把%s发出去。" % what
        elif not test_base and is_go_base(base):
            return (
                "Console 模式不能使用 Go 订阅地址：%s。已停止发送，请打开「AI 设置」重新选择模式。" % base
            )
        return ""

    def _post(self, body, saved_note):
        """真正发出请求并读回响应；只做传输与 JSON 层，业务解析交给调用方。"""
        api_key = str(self.config.get("api_key") or "").strip()
        if not api_key:
            raise AnalysisError(
                "未配置密钥，当前不会向外部发送任何数据。请先在「AI 设置」里填写本次登录的密钥。",
                "config",
                False,
            )
        base = _clean_base(self.config.get("base_url") or "")
        error = self._target_error(base, bool(self.config.get("test_base")), "作答内容")
        if error:
            raise AnalysisError(error, "config", False)
        request = urllib.request.Request(
            request_url(self.config["base_url"], self.api),
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(api_key),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config["timeout"]) as response:
                raw = response.read()
        except socket.timeout:
            raise AnalysisError(
                "调用超时（%.0f 秒），%s，可以重试。" % (self.config["timeout"], saved_note), "timeout"
            )
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc)
        except urllib.error.URLError:
            raise AnalysisError(
                "无法连接 AI 服务。请检查网络，或打开「AI 设置」确认服务地址；%s，可以重试。" % saved_note,
                "network",
            )
        except OSError:
            raise AnalysisError(
                "读取服务响应失败。请检查网络或「AI 设置」里的服务地址，稍后重试；%s。" % saved_note,
                "network",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise AnalysisError(
                "服务返回的内容不是有效的 JSON，可能不是兼容接口。请确认接口地址是服务商给的地址；%s。"
                % saved_note,
                "format",
            )
        self.last_payload = payload
        return payload

    def chat_text(self, prompt, system):
        """纯文字请求：只发题目与步骤，不带图片、不带学生身份。返回 (正文, 用量)。"""
        if self.api == "messages":
            body = {
                "model": self.config["model"],
                "max_tokens": 32000,
                "temperature": 0.1,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        else:
            body = {
                "model": self.config["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 32000,
            }
        payload = self._post(body, "作答内容和审核意见已保存")
        if self.api == "messages":
            content, truncated = self._messages_content(payload)
        else:
            content, truncated = self._chat_content(payload)
        if truncated:
            raise AnalysisError(
                "模型输出被截断，这次没有生成完整的候选错因。可以重试；作答内容没有被改动。", "format"
            )
        usage = normalise_usage(payload.get("usage"))
        if self.mode == MODE_CUSTOM and usage:
            for key in ("billing_source", "list_cost", "allowance"):
                usage.pop(key, None)
        billing_error = self._go_billing_error(usage)
        if billing_error:
            raise AnalysisError(billing_error, "billing", False)
        return content or "", usage

    def analyze(self, job):
        blob = job.get("image_bytes") or b""
        if not blob:
            raise AnalysisError("没有可发送的图片数据，未调用真实 AI。", "config", False)
        api_key = str(self.config.get("api_key") or "").strip()
        if not api_key:
            raise AnalysisError(
                "未配置密钥，当前不会向外部发送任何数据。请先在「AI 设置」里填写本次登录的密钥。",
                "config",
                False,
            )
        base = _clean_base(self.config.get("base_url") or "")
        test_base = bool(self.config.get("test_base"))
        error = self._target_error(base, test_base, "照片")
        if error:
            raise AnalysisError(error, "config", False)

        body = json.dumps(self.build_request(job)).encode("utf-8")
        request = urllib.request.Request(
            request_url(self.config["base_url"], self.api),
            data=body,
            headers=self._headers(api_key),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config["timeout"]) as response:
                raw = response.read()
        except socket.timeout:
            raise AnalysisError(
                "调用超时（%.0f 秒），照片和录入内容已保存，可以重试。" % self.config["timeout"], "timeout"
            )
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc)
        except urllib.error.URLError:
            raise AnalysisError(
                "无法连接 AI 服务。请检查网络，或打开「AI 设置」确认服务地址；"
                "照片和录入内容已保存，可以重试。",
                "network",
            )
        except OSError:
            raise AnalysisError(
                "读取服务响应失败。请检查网络或「AI 设置」里的服务地址，稍后重试；"
                "照片和录入内容已保存。",
                "network",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise AnalysisError(
                "服务返回的内容不是有效的 JSON，可能不是兼容的图片接口。"
                "请确认接口地址是服务商给的地址，模型支持图片输入；照片和录入内容已保存。",
                "format",
            )
        self.last_payload = payload

        if self.api == "messages":
            content, truncated = self._messages_content(payload)
        else:
            content, truncated = self._chat_content(payload)
        usage = normalise_usage(payload.get("usage"))
        if self.mode == MODE_CUSTOM and usage:
            for key in ("billing_source", "list_cost", "allowance"):
                usage.pop(key, None)
        billing_error = self._go_billing_error(usage)
        if billing_error:
            raise AnalysisError(billing_error, "billing", False)
        if truncated:
            raise AnalysisError(
                "模型输出被截断，这次没有生成可用的分析结果。可以重试，或手动填写分析内容；"
                "照片和录入内容已保存。",
                "format",
            )
        return _normalise(_extract_json(content), self.config["model"], usage, self.mode)

    def _go_billing_error(self, usage):
        """Go 请求返回非订阅计费或额外费用时，阻止继续发送。缺失来源不算确认，也不阻断。"""
        if self.mode != MODE_GO:
            return ""
        usage = usage or {}
        source = usage.get("billing_source")
        cost = usage.get("cost")
        unexpected_source = bool(source) and source not in ("go", "go-plus")
        extra_cost = cost is not None and cost > 0
        if not unexpected_source and not extra_cost:
            return ""
        parts = []
        if unexpected_source:
            parts.append("计费来源显示为 %s，不是 Go 订阅" % source)
        if extra_cost:
            parts.append("出现额外费用 $%.4f" % cost)
        return (
            "；".join(parts)
            + "。软件已停止继续发送，请先核对 OpenCode 账户设置后再试；本条没有保存为分析结果。"
        )

    def _chat_content(self, payload):
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AnalysisError("服务返回的结构不符合预期，可重试或手动填写。", "format")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content or "", choice.get("finish_reason") == "length"

    def _messages_content(self, payload):
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise AnalysisError("服务返回的结构不符合预期，可重试或手动填写。", "format")
        text = "".join(
            block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        )
        return text, payload.get("stop_reason") == "max_tokens"

    def _http_error(self, exc):
        detail = ""
        try:
            raw = exc.read(4096).decode("utf-8", "ignore")
            data = json.loads(raw)
            error = data.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "").strip()
            detail = detail or str(data.get("message") or "").strip()
        except (ValueError, OSError):
            detail = ""
        status = exc.code
        go = self.mode == MODE_GO
        custom = self.mode == MODE_CUSTOM
        if status in (401, 403):
            if go:
                return AnalysisError(
                    "密钥无效、没有该模型的权限，或该用途未被允许（HTTP %d）。请打开「AI 设置」"
                    "检查本次填写的 Go 密钥和模型；本次没有生成分析结果。" % status,
                    "auth",
                )
            if custom:
                return AnalysisError(
                    "密钥无效、没有该模型的权限，或服务商不允许这种请求（HTTP %d）。"
                    "请打开「AI 设置」检查本次填写的密钥和模型名称，并确认该服务商允许图片输入；"
                    "本次没有生成分析结果。" % status,
                    "auth",
                )
            return AnalysisError(
                "密钥无效或没有该模型的权限（HTTP %d）。请打开「AI 设置」检查本次填写的密钥，"
                "或确认环境变量中的密钥有效、且已开通这个模型。" % status,
                "auth",
            )
        if status == 402:
            if go:
                return AnalysisError(
                    "Go 订阅额度不足（HTTP 402）。本次没有生成分析结果，照片和录入内容已保存。"
                    "可以等到额度重置，或到 OpenCode Go 页面确认剩余额度；本软件不会自动改用按量付费。",
                    "payment",
                )
            if custom:
                return AnalysisError(
                    "服务商返回额度不足或需要付费（HTTP 402）。请检查该服务商的账户余额或套餐额度；"
                    "本次没有生成分析结果，照片和录入内容已保存。本软件不会改用模拟结果，也不会自动换服务。",
                    "payment",
                )
            return AnalysisError(
                "账户余额不足（HTTP 402）。请到 OpenCode Console 充值，或换一个有额度的密钥；"
                "本次没有生成分析结果，照片和录入内容已保存。",
                "payment",
            )
        if status == 429:
            return AnalysisError("调用被限流或额度不足（HTTP 429），请稍后再试或查看所配置服务的额度。", "quota")
        if status in (404, 405):
            if custom:
                return AnalysisError(
                    "接口地址或模型名称不可用（HTTP %d）。请打开「AI 设置」确认地址是服务商提供的"
                    " OpenAI 兼容接口（通常以 /v1 结尾），并核对模型名称。" % status,
                    "request",
                )
            return AnalysisError(
                "接口地址或模型名称不可用（HTTP %d）。请打开「AI 设置」检查服务地址和模型名称。"
                % status,
                "request",
            )
        if status in (400, 413, 415):
            return AnalysisError(
                "请求被服务拒绝（HTTP %d）%s本次没有生成分析结果，照片和录入内容已保存。"
                % (status, "：" + detail + "。" if detail else "。"),
                "request",
            )
        return AnalysisError(
            "云端视觉服务出错（HTTP %d）%s，可稍后重试。" % (status, "：" + detail if detail else ""), "server"
        )


# 兼容旧测试与旧调用名；新代码统一使用 CloudVisionProvider。
OpenCodeProvider = CloudVisionProvider
DashScopeProvider = CloudVisionProvider
