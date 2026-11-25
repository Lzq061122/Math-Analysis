"""R1-07 旧版 AI 个体分析路径隔离：外发拦截验证（请求拦截 + 本地模拟模型服务）。

证明三件事：
1. 正常操作：网页端与桌面端的个体业务流程（错题、作答、追问、辅导建议）不产生任何外部请求；
2. 旧接口直调：八个入口与五条「只传 id、服务端自己捞」的绕过大体，只要配置指向真实通道，
   就在组装请求体之前返回 ai_isolation 业务错误；旧环境变量 MISTAKE_AI_MODE /
   MISTAKE_AI_API_KEY / MISTAKE_AI_BASE_URL / DASHSCOPE_* 放不开业务环境；
3. 异常路径：模型未配置或不可用时，作答导入、规则定位、人工判断、统计、规则建议照常可用。

同时保留一条正向对照（positive control）：**离线工具用途 + 本机回环桩**仍然能真的发出请求，
用来证明拦截记录器本身有效 —— 业务路径 0 次外发不是因为记录器没装上。

运行：python tests\\verify_r1_07_isolation.py
输出：docs/verify-r1-07-isolation.txt（同时打印到标准输出）
退出码：0 全部通过，1 有失败
"""

import base64
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image  # noqa: E402

from app import advice as advice_mod  # noqa: E402
from app import analysis, answer_check as answer_check_mod, api, equation_steps  # noqa: E402
from app import followup as followup_mod  # noqa: E402
from app import stats as stats_mod  # noqa: E402
from app import vision  # noqa: E402
from app.db import Database, ensure_answer_check_schema  # noqa: E402
from app.preview import PreviewStore  # noqa: E402
from app.uploads import UploadStore  # noqa: E402
from desktop.service import ApiError, DesktopService  # noqa: E402

LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-07-isolation.txt")
TOTAL = [0]
FAILED = [0]
LINES = []

# 外部模型服务：不可达的保留域名，确保「想发也发不出去」。
EXTERNAL_BASE = "https://ai.invalid.example/v1"


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if not ok:
        FAILED[0] += 1
    log(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  ← %r" % (detail,)))


def section(title):
    log("")
    log("== %s ==" % title)


# ---------------------------------------------------------------------------
# 本地模拟模型服务：真的收请求、真的回 JSON；它收了几次就是「外发了几次」
# ---------------------------------------------------------------------------
STUB = {"calls": [], "lock": threading.Lock()}


def _stub_reply(path):
    suggestion = {
        "question_text": "（桩服务回的题目）",
        "solution_text": "（桩服务回的步骤）",
        "knowledge_points": ["一元一次方程"],
        "error_type": "符号错误",
        "observed_errors": ["第 2 行移项后没有变号"],
        "possible_causes": ["计算习惯"],
        "advice_text": "（桩服务回的建议）",
        "insufficient": False,
        "insufficient_reason": "",
        "likely_correct": False,
    }
    text = json.dumps(suggestion, ensure_ascii=False)
    if path.endswith("/messages"):
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 11, "output_tokens": 7}}
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with STUB["lock"]:
            STUB["calls"].append({
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": raw.decode("utf-8", "ignore"),
            })
        data = json.dumps(_stub_reply(self.path), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def stub_calls():
    with STUB["lock"]:
        return list(STUB["calls"])


# ---------------------------------------------------------------------------
# 外发拦截器：urllib 的每一次尝试都记下来；非回环地址一个字节都不放过
# ---------------------------------------------------------------------------
EGRESS = {"attempts": [], "lock": threading.Lock()}
REAL_URLOPEN = urllib.request.urlopen


def _loopback(url):
    return urllib.parse.urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")


def _recording_urlopen(request, *args, **kwargs):
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    data = getattr(request, "data", b"") or b""
    with EGRESS["lock"]:
        EGRESS["attempts"].append({
            "url": url,
            "loopback": _loopback(url),
            "bytes": len(data),
            "body": bytes(data).decode("utf-8", "ignore")[:4000],
        })
    if not _loopback(url):
        # 隔离的最后一层：外部地址在客户端就被打断，连 TCP 连接都不建立。
        raise urllib.error.URLError("R1-07 验证脚本：拦截一切外部请求")
    return REAL_URLOPEN(request, *args, **kwargs)


def egress():
    with EGRESS["lock"]:
        return list(EGRESS["attempts"])


def external_egress():
    return [item for item in egress() if not item["loopback"]]


def png_bytes(width=640, height=800):
    """带噪点的 PNG：确保超过接口要求的最小体积（1024 字节）。"""
    image = Image.new("RGB", (width, height), "white")
    pixels = image.load()
    rng = random.Random(20261125)
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            value = rng.randint(0, 255)
            pixels[x, y] = (value, (value * 7) % 256, (value * 13) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_photo(path):
    with open(path, "wb") as fh:
        fh.write(png_bytes(900, 600))


def data_url(blob):
    return "data:image/png;base64," + base64.b64encode(blob).decode("ascii")


def answer_csv():
    return (
        "记录编号,学生姓名,学校编号,题号,题目内容,单元,标准解法,学生步骤,来源批次,作答日期,缺测,缺测原因\r\n"
        "R1-07-001,学生甲,XY001,1,解方程 2x+4=10,一元一次方程,2x=6；x=3,2x+4=10；2x=3；x=1.5,"
        "9月第1次作业,2026-09-02,否,\r\n"
        "R1-07-002,学生乙,,2,解方程 3(x-2)=2x+5,一元一次方程,3x-6=2x+5；x=11,3x-6=2x+5；x=11,"
        "9月第1次作业,2026-09-02,否,\r\n"
    )


# ---------------------------------------------------------------------------
# 与 api 处理器约定的最小上下文（桌面 Context 的同形替身，只为直调旧接口）
# ---------------------------------------------------------------------------
class FakeCtx:
    def __init__(self, db, user, uploads, body=None, params=None, query=None,
                 ai_provider=None, ai_config=None, previews=None):
        self.db = db
        self.user = user
        self.uploads = uploads
        self.body = dict(body or {})
        self.params = dict(params or {})
        self.query = dict(query or {})
        self.previews = previews
        self.ai_config = dict(ai_config or {})
        self.ai_provider = ai_provider
        self.token = "harness-token"
        self.session_current = lambda: True
        self.server = None
        self.client_ip = "127.0.0.1"


def expect_error(kind, call, expected_code):
    """调用并断言抛出指定 code 的业务错误；返回 (是否通过, 说明)。"""
    try:
        result = call()
    except kind as exc:
        return getattr(exc, "code", "") == expected_code, getattr(exc, "message", str(exc))
    return False, "没有抛错，返回 %r" % (result,)


# ---------------------------------------------------------------------------
# 真正跑起来的网页服务（server.py）：验证旧变量 + 旧接口的端到端行为
# ---------------------------------------------------------------------------
def start_web_server(data_dir):
    import server as server_mod

    instance = server_mod.make_server("127.0.0.1", 0, data_dir, use_demo=False)
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % instance.server_address[1]
    state = {"cookie": ""}

    def call(method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if state["cookie"]:
            headers["Cookie"] = "teacher_session=" + state["cookie"]
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with REAL_URLOPEN(request, timeout=30) as response:
                raw = response.read().decode("utf-8", "ignore")
                payload = json.loads(raw) if raw else {}
                status = response.status
                set_cookie = response.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "ignore")
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"error": raw}
            status = exc.code
            set_cookie = exc.headers.get("Set-Cookie") or ""
        for part in set_cookie.split(";"):
            if part.strip().startswith("teacher_session="):
                state["cookie"] = part.split("=", 1)[1].strip()
        return {"status": status, "body": payload}

    def stop():
        instance.shutdown()
        instance.server_close()

    return {
        "post": lambda path, body: call("POST", path, body),
        "get": lambda path: call("GET", path),
        "stop": stop,
        "base": base,
    }


def main():
    from logmeta import environment_lines

    for line in environment_lines():
        log(line)
    log("")

    work = tempfile.mkdtemp(prefix="r1-07-isolation-")
    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_root = "http://127.0.0.1:%d" % stub.server_address[1]
    log("本地模拟模型服务（回环桩）：%s" % stub_root)
    log("外部模型服务地址（不可达）：%s" % EXTERNAL_BASE)

    for name in ("MISTAKE_AI_MODE", "MISTAKE_AI_API_KEY", "DASHSCOPE_API_KEY",
                 "MISTAKE_AI_BASE_URL", "DASHSCOPE_BASE_URL", "MISTAKE_AI_MODEL"):
        os.environ.pop(name, None)
    urllib.request.urlopen = _recording_urlopen

    data_root = os.path.join(work, "data")
    os.makedirs(data_root)
    db_file = os.path.join(data_root, "app.db")
    Database(db_file)
    ensure_answer_check_schema(db_file)
    svc = DesktopService(data_root)
    svc.register("隔离验证老师", "pw-iso-107")
    cid = svc.create_class("隔离验证班")["id"]
    sid = svc.create_student(cid, "学生甲", "XY001")["id"]
    svc.create_student(cid, "学生乙", "")
    photo = os.path.join(work, "题图.png")
    make_photo(photo)

    csv_text = answer_csv()
    preview = svc.preview_answers(cid, csv_text, "9月第1次作业")
    check("准备数据：作答导入预览可用", preview["counts"]["ok"] == 2, preview["counts"])
    committed = svc.import_answers(cid, csv_text, "9月第1次作业")
    check("准备数据：作答导入落库可用", committed["counts"]["new"] == 2, committed["counts"])

    db = svc.db
    user = svc.user
    uploads = UploadStore(os.path.join(data_root, "uploads"))
    class_row = db.query_one("SELECT * FROM classes WHERE id=?", (cid,))
    answers = db.query("SELECT * FROM answers WHERE class_id=? ORDER BY id", (cid,))

    # ------------------------------------------------------------------
    section("一、正向对照：离线工具用途 + 本机回环桩确实能发出去（证明记录器有效）")
    # ------------------------------------------------------------------
    control = vision.CloudVisionProvider(vision.resolve_config({
        "mode": "custom", "api_key": "sk-control", "base_url": stub_root + "/v1",
        "model": "stub-vision", "allow_env": False,
    }))
    before_calls = len(stub_calls())
    control_result = control.analyze({
        "purpose": analysis.PURPOSE_OFFLINE_TOOL,
        "image_bytes": png_bytes(), "image_mime": "image/png",
    })
    check("正向对照：离线工具用途 + 本机回环地址真的发出了 1 次请求",
          len(stub_calls()) == before_calls + 1, len(stub_calls()) - before_calls)
    check("正向对照：请求落在本机回环桩上",
          stub_calls()[-1]["path"] == "/v1/chat/completions", stub_calls()[-1]["path"])
    check("正向对照：桩返回被解析成结构化结果", control_result.get("mode") == "custom", control_result)
    check("正向对照：拦截器记录了这次回环请求（不是 0，说明记录器有效）",
          any(item["loopback"] for item in egress()), len(egress()))
    log("拦截日志（正向对照）：" + json.dumps(
        [{"url": item["url"], "loopback": item["loopback"], "bytes": item["bytes"]} for item in egress()],
        ensure_ascii=False))

    # ------------------------------------------------------------------
    section("二、通道层：同一个 provider 拿个体数据也发不出去（用途判定）")
    # ------------------------------------------------------------------
    mark = len(egress())
    okay, detail = expect_error(
        vision.AnalysisError,
        lambda: control.analyze({"image_bytes": png_bytes(), "image_mime": "image/png"}),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("个体作业（无离线工具用途）在本机回环桩上也被拒绝", okay, detail)
    okay, detail = expect_error(
        vision.AnalysisError,
        lambda: control.chat_text("【作答分析】题目：解方程 3(x-2)=2x+5；学生步骤：x=11", "系统提示"),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("个体文字请求（作答分析）在本机回环桩上也被拒绝", okay, detail)
    check("这两次拒绝都没有产生任何请求", len(egress()) == mark, len(egress()) - mark)
    check("拒绝文案写明没有把任何内容发出去", "没有把任何内容发出去" in analysis.INDIVIDUAL_FLOW_MESSAGE,
          analysis.INDIVIDUAL_FLOW_MESSAGE)

    external_provider = vision.CloudVisionProvider(vision.resolve_config({
        "mode": "custom", "api_key": "sk-leak", "base_url": EXTERNAL_BASE,
        "model": "leak-model", "allow_env": False,
    }))
    mark = len(egress())
    okay, detail = expect_error(
        vision.AnalysisError,
        lambda: external_provider.analyze({
            "purpose": analysis.PURPOSE_OFFLINE_TOOL,
            "image_bytes": png_bytes(), "image_mime": "image/png",
        }),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("离线工具用途指向外部地址也被拒绝", okay, detail)
    check("拒绝时没有发生任何网络尝试", len(egress()) == mark, len(egress()) - mark)
    check("拦截器从未看到任何外部地址尝试", external_egress() == [], external_egress())

    # ------------------------------------------------------------------
    section("三、字段白名单：请求体只能出现白名单里的字段")
    # ------------------------------------------------------------------
    good_body = {
        "model": "m", "max_tokens": 10, "temperature": 0.1,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "题目"}]}],
    }
    check("白名单接受正常请求体", vision.payload_field_error(good_body) == "",
          vision.payload_field_error(good_body))
    bad_bodies = [
        (dict(good_body, student_name="张三"), "多出身份字段 student_name"),
        (dict(good_body, messages=[{"role": "user", "content": "x", "student_id": 3}]), "消息里多出 student_id"),
        (dict(good_body, messages=[{"role": "tool", "content": "x"}]), "角色不在白名单"),
        (dict(good_body, messages=[{"role": "user", "content": [{"type": "audio", "text": "x"}]}]),
         "内容块类型不在白名单"),
        (dict(good_body, messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]), "图片不是受限 data URL"),
        (dict(good_body, messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "AAA", "student_code": "XY1"}}]}]), "图片块里多出学校编号"),
    ]
    for bad_body, label in bad_bodies:
        check("白名单拒绝：%s" % label, vision.payload_field_error(bad_body) != "",
              vision.payload_field_error(bad_body))
    dirty_job = {"image_bytes": b"x", "image_mime": "image/png", "student_id": 3}
    clean_job = {"image_bytes": b"x", "image_mime": "image/png", "trial": True}
    check("作业字段白名单拒绝 student_id", vision.job_field_error(dirty_job) != "",
          vision.job_field_error(dirty_job))
    check("作业字段白名单接受正常作业", vision.job_field_error(clean_job) == "",
          vision.job_field_error(clean_job))

    # ------------------------------------------------------------------
    section("四、旧环境变量放不开业务环境")
    # ------------------------------------------------------------------
    os.environ["MISTAKE_AI_MODE"] = "real"
    os.environ["MISTAKE_AI_BASE_URL"] = EXTERNAL_BASE
    os.environ["MISTAKE_AI_MODEL"] = "leak-model"
    os.environ["MISTAKE_AI_API_KEY"] = "sk-env-leak"
    os.environ["DASHSCOPE_API_KEY"] = "sk-env-leak-2"
    default_cfg = vision.resolve_config({})
    check("resolve_config({}) 默认仍是模拟（allow_env 默认 False）", default_cfg["mode"] == "mock", default_cfg["mode"])
    check("默认配置不读任何环境变量密钥", not default_cfg["key_configured"], default_cfg["key_source"])
    check("默认配置的地址不含外部模型地址", EXTERNAL_BASE not in default_cfg["base_url"], default_cfg["base_url"])
    check("get_provider() 在旧变量存在时仍给 MockProvider",
          isinstance(analysis.get_provider(), analysis.MockProvider), type(analysis.get_provider()).__name__)
    check("api._analysis_provider(None) 不再回落环境变量",
          isinstance(api._analysis_provider(None), analysis.MockProvider),
          type(api._analysis_provider(None)).__name__)
    check("MockProvider 仍然没有 chat_text（不可能误走文字通道）",
          not hasattr(analysis.MockProvider(), "chat_text"), "chat_text 存在")

    # ------------------------------------------------------------------
    section("五、旧接口直调：配置真实通道时八个入口一律 ai_isolation")
    # ------------------------------------------------------------------
    svc.save_ai_settings("custom", EXTERNAL_BASE, "leak-model", "sk-desktop-leak", service_name="外部测试云")
    settings = svc.ai_settings()
    check("桌面端账号设置指向真实通道（用于本次验证）", settings["mode"] == "custom", settings["mode"])
    remote_provider = svc._ctx().ai_provider
    check("桌面端 provider 是真实通道而不是 mock",
          getattr(remote_provider, "mode", "") == "custom", type(remote_provider).__name__)

    mark_attempts, mark_calls = len(egress()), len(stub_calls())

    created = api.api_create_mistake(FakeCtx(
        db, user, uploads,
        body={
            "class_id": cid, "student_id": sid,
            "question_text": "解方程 3(x-2)=2x+5",
            "solution_text": "3x-6=2x+5；x=11",
            "consent": True,
            "image": {"data_url": data_url(png_bytes()), "width": 640, "height": 800},
        },
        ai_provider=remote_provider, ai_config=settings,
    ))
    check("入口 /api/mistakes（新建错题）不外发，落库为本地录入",
          created.get("ai_mode") == "local", created.get("ai_mode"))
    mid = created["id"]
    fresh = api.api_get_mistake(FakeCtx(db, user, uploads, params={"id": mid}))
    check("新建错题记录里写明 ai_isolation 与原因",
          fresh.get("ai_error_code") == analysis.INDIVIDUAL_FLOW_CODE and bool(fresh.get("ai_error")),
          (fresh.get("ai_error_code"), (fresh.get("ai_error") or "")[:20]))

    okay, detail = expect_error(
        api.ApiError,
        lambda: api.api_reanalyze(FakeCtx(db, user, uploads, params={"id": mid}, body={"consent": True},
                                          ai_provider=remote_provider)),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("入口 POST /api/mistakes/{id}/analyze（mistake_id 只传 id）返回 ai_isolation", okay, detail)

    okay, detail = expect_error(
        api.ApiError,
        lambda: api.api_preview_analysis(FakeCtx(db, user, uploads, params={"id": mid}, body={"consent": True},
                                                ai_provider=remote_provider, previews=PreviewStore())),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("入口 桌面预览分析（api_preview_analysis，未注册进 ROUTES）返回 ai_isolation", okay, detail)

    okay, detail = expect_error(
        api.ApiError,
        lambda: api.api_answer_analyse(FakeCtx(db, user, uploads,
                                               params={"id": cid, "answer_id": answers[0]["id"]},
                                               body={"consent": True}, ai_provider=remote_provider)),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("入口 review/analyse（answer_id 只传 id）返回 ai_isolation", okay, detail)

    okay, detail = expect_error(
        api.ApiError,
        lambda: api.api_student_advice_generate(FakeCtx(db, user, uploads,
                                                       params={"id": cid, "student_id": sid},
                                                       body={"consent": True}, ai_provider=remote_provider)),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("入口 insight/advice/generate（student_id 只传 id）返回 ai_isolation", okay, detail)

    okay, detail = expect_error(
        api.ApiError,
        lambda: api.api_followup_suggest(FakeCtx(db, user, uploads, params={"id": cid, "student_id": sid},
                                                 body={"problem": "一元一次方程", "mistake_id": mid,
                                                       "consent": True}, ai_provider=remote_provider)),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("入口 followups/suggest（mistake_id 只传 id）返回 ai_isolation", okay, detail)

    # 追问本身是老师手写的、不经过模型；建好后再测「按回答更新错因」
    api.api_confirm_mistake(FakeCtx(db, user, uploads, params={"id": mid},
                                    body={"base_fingerprint": fresh["base_fingerprint"]}))
    followup = followup_mod.create(
        db, cid, sid, "一元一次方程", "你这一步为什么把 -3 变成 3？", "teacher", {},
        followup_mod.evidence_payload(db, cid, sid, "一元一次方程", mid),
    )
    followup_mod.save_answer(db, cid, sid, followup["id"], "我以为移项不用变号")
    okay, detail = expect_error(
        api.ApiError,
        lambda: api.api_followup_update(FakeCtx(db, user, uploads,
                                                params={"id": cid, "student_id": sid, "fid": followup["id"]},
                                                body={"consent": True}, ai_provider=remote_provider)),
        analysis.INDIVIDUAL_FLOW_CODE,
    )
    check("入口 followups/{fid}/updates（fid 只传 id）返回 ai_isolation", okay, detail)

    okay, detail = expect_error(ApiError, lambda: svc.preview_analysis(mid, consent=True),
                                analysis.INDIVIDUAL_FLOW_CODE)
    check("桌面 service.preview_analysis（ui.py:5350 旧路径）返回 ai_isolation", okay, detail)
    okay, detail = expect_error(ApiError, lambda: svc.analyse_answer(cid, answers[0]["id"], consent=True),
                                analysis.INDIVIDUAL_FLOW_CODE)
    check("桌面 service.analyse_answer 返回 ai_isolation", okay, detail)
    okay, detail = expect_error(ApiError, lambda: svc.generate_student_advice(cid, sid, consent=True),
                                analysis.INDIVIDUAL_FLOW_CODE)
    check("桌面 service.generate_student_advice 返回 ai_isolation", okay, detail)
    okay, detail = expect_error(ApiError, lambda: svc.suggest_followup(cid, sid, "一元一次方程", mid, consent=True),
                                analysis.INDIVIDUAL_FLOW_CODE)
    check("桌面 service.suggest_followup 返回 ai_isolation", okay, detail)
    okay, detail = expect_error(ApiError,
                                lambda: svc.update_followup_causes(cid, sid, followup["id"], consent=True),
                                analysis.INDIVIDUAL_FLOW_CODE)
    check("桌面 service.update_followup_causes 返回 ai_isolation", okay, detail)

    trial = svc.trial_analysis(photo, "解方程 3(x-2)=2x+5", "3x-6=2x+5", consent=True)
    check("桌面 ui.py:4632 试分析（外部地址）不发送并给出业务文案",
          trial.get("ok") is False and analysis.INDIVIDUAL_FLOW_MESSAGE in (trial.get("error") or ""),
          (trial.get("error") or "")[:60])
    check("本轮旧接口直调产生 0 次外部请求", external_egress() == [], external_egress())
    check("本轮旧接口直调连本地桩都没有多一次请求", len(stub_calls()) == mark_calls,
          len(stub_calls()) - mark_calls)
    check("本轮旧接口直调没有发生任何网络尝试", len(egress()) == mark_attempts,
          len(egress()) - mark_attempts)
    log("拦截日志（旧接口直调）：0 次外发；错误码统一为 %s" % analysis.INDIVIDUAL_FLOW_CODE)

    # ------------------------------------------------------------------
    section("六、网页业务环境（server.py）：旧变量 + 旧接口端到端")
    # ------------------------------------------------------------------
    web = start_web_server(os.path.join(work, "web-data"))
    try:
        registered = web["post"]("/api/auth/register", {"username": "网页老师", "password": "pw-web-107"})
        check("网页端登录/注册可用", registered["status"] == 200, registered)
        meta = web["get"]("/api/meta")["body"]
        check("网页端口 /api/meta 报告 mock（旧 MISTAKE_AI_MODE=real 无效）",
              meta.get("ai_mode") == "mock", meta.get("ai_mode"))
        check("网页端口 /api/meta 声明业务隔离", meta.get("ai_business_isolated") is True,
              meta.get("ai_business_isolated"))
        check("网页端发送范围写明「不向生成式模型发送」",
              all("不向生成式模型发送" in item for item in meta.get("ai_send_fields") or []),
              meta.get("ai_send_fields"))

        class_id = web["post"]("/api/classes", {"name": "网页班"})["body"]["id"]
        student_id = web["post"]("/api/classes/%d/students" % class_id,
                                 {"name": "学生甲", "code": "XY001"})["body"]["id"]
        imported = web["post"]("/api/classes/%d/answers/import" % class_id,
                               {"text": csv_text, "batch": "9月第1次作业", "confirm": True})
        check("网页端作答导入照常可用", imported["status"] == 200, imported)
        listing = web["get"]("/api/classes/%d/answer-reviews" % class_id)
        answer_id = listing["body"]["items"][0]["id"]
        mistake = web["post"]("/api/mistakes", {
            "class_id": class_id, "student_id": student_id,
            "question_text": "解方程 3(x-2)=2x+5", "solution_text": "3x-6=2x+5；x=11",
            "consent": True,
            "image": {"data_url": data_url(png_bytes()), "width": 640, "height": 800},
        })
        check("网页端新建错题照常可用", mistake["status"] == 200, mistake)
        mistake_id = mistake["body"]["id"]

        mark_attempts, mark_calls = len(egress()), len(stub_calls())
        endpoints = [
            ("POST /api/mistakes/%d/analyze" % mistake_id,
             "/api/mistakes/%d/analyze" % mistake_id, {}),
            ("POST .../answers/%d/review/analyse" % answer_id,
             "/api/classes/%d/answers/%d/review/analyse" % (class_id, answer_id), {}),
            ("POST .../insight/advice/generate",
             "/api/classes/%d/students/%d/insight/advice/generate" % (class_id, student_id), {}),
            ("POST .../followups/suggest",
             "/api/classes/%d/students/%d/followups/suggest" % (class_id, student_id),
             {"problem": "一元一次方程", "mistake_id": mistake_id}),
        ]
        statuses = []
        for label, path, extra in endpoints:
            result = web["post"](path, dict(extra, consent=True))
            statuses.append((label, result["status"]))
            check("网页端旧接口 %s 不外发（HTTP %d）" % (label, result["status"]),
                  result["status"] in (200, 400, 403, 409), result)
        check("网页端全部旧接口调用产生 0 次外部请求", external_egress() == [], external_egress())
        check("网页端调用连本地桩都没有多一次请求", len(stub_calls()) == mark_calls,
              len(stub_calls()) - mark_calls)
        check("网页端调用没有发生任何网络尝试", len(egress()) == mark_attempts, len(egress()) - mark_attempts)
        log("拦截日志（网页端旧接口）：0 次外发；返回状态 " +
            json.dumps(["%s → %d" % item for item in statuses], ensure_ascii=False))
    finally:
        web["stop"]()

    # ------------------------------------------------------------------
    section("七、异常路径：模型未配置 / 不可用时业务照常可用")
    # ------------------------------------------------------------------
    plain_root = os.path.join(work, "plain-data")
    os.makedirs(plain_root)
    plain_db_file = os.path.join(plain_root, "app.db")
    Database(plain_db_file)
    ensure_answer_check_schema(plain_db_file)
    plain = DesktopService(plain_root)
    plain.register("无机型老师", "pw-plain-107")
    pcid = plain.create_class("异常路径班")["id"]
    plain.create_student(pcid, "学生甲", "XY001")
    plain.create_student(pcid, "学生乙", "")
    p_preview = plain.preview_answers(pcid, csv_text, "9月第1次作业")
    check("模型未配置：作答导入预览可用", p_preview["counts"]["ok"] == 2, p_preview["counts"])
    p_commit = plain.import_answers(pcid, csv_text, "9月第1次作业")
    check("模型未配置：作答导入落库可用", p_commit["counts"]["new"] == 2, p_commit["counts"])
    p_answers = plain.db.query("SELECT * FROM answers WHERE class_id=? ORDER BY id", (pcid,))
    p_class_row = plain.db.query_one("SELECT * FROM classes WHERE id=?", (pcid,))

    located = equation_steps.locate("解方程 2x+4=10", "2x=6；x=3", "2x+4=10；2x=3；x=1.5")
    check("规则定位可用：定位到首个错步", located["status"] == "first_bad", located.get("status"))
    check("规则定位给出错步原文", bool(located.get("first_bad_text")), located.get("first_bad_text"))
    check("规则定位标注来源为固定规则", located.get("source") == "rule", located.get("source"))

    target = p_answers[0]
    saved = answer_check_mod.save(
        plain.db, p_class_row, int(plain.user["id"]), int(target["id"]),
        {
            "expected_version": target["fingerprint"],
            "cause_state": "decided", "cause_code": "calc", "cause_label": "计算错误",
            "teacher_note": "人工判断：移项没有变号",
            "followup": {"question": "你这一步为什么把 -3 变成 3？", "answer_text": "我以为不用变号"},
        },
    )
    check("人工判断可用：保存后返回教师判断", saved["judgement"]["cause_state"] == "decided",
          saved.get("judgement"))
    check("人工判断可用：判断依据落库",
          "移项没有变号" in json.dumps(saved, ensure_ascii=False), "依据丢失")

    block = stats_mod.answer_check_stats(plain.db, p_class_row, "9月第1次作业")
    check("统计可用：核对统计读到 2 份作答", block["expected"] == 2, block)
    check("统计可用：已核对 1 份、原因已判断 1 份",
          block["checked"] == 1 and block["cause_decided"] == 1, block)
    build = stats_mod.build_stats(plain.db, p_class_row)
    check("统计可用：正式学情统计仍可计算", "summary" in build and "knowledge_points" in build,
          list(build)[:6])

    students = plain.db.query("SELECT * FROM students WHERE class_id=?", (pcid,))
    advice_mod.refresh(plain.db, p_class_row, students)
    advice_row = plain.db.query_one("SELECT * FROM advice WHERE class_id=? AND scope='class'", (pcid,))
    check("规则建议可用：生成了班级参考意见", bool(advice_row and advice_row["content"]),
          (advice_row["content"][:30] if advice_row else None))

    overview = answer_check_mod.overview(plain.db, p_class_row, "9月第1次作业")
    check("核对总览可用（不依赖模型）",
          overview.get("state") in ("has_pending", "all_checked", "no_answers")
          and overview.get("assignment", {}).get("total") == 2,
          (overview.get("state"), overview.get("assignment")))
    check("异常路径全程没有发生任何外部请求", external_egress() == [], external_egress())

    # ------------------------------------------------------------------
    log("")
    log("拦截汇总：")
    log("  正向对照（离线工具用途 + 本机回环桩）：%d 次请求（证明记录器有效）" % len(stub_calls()))
    log("  外部地址尝试次数：%d" % len(external_egress()))
    log("  业务路径（八入口 + 五条 id 绕过 + 网页端 + 桌面端）：0 次请求")
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    code = 1 if FAILED[0] else 0
    log("退出码：%d" % code)

    stub.shutdown()
    urllib.request.urlopen = REAL_URLOPEN
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
