"""本地 OpenAI / Anthropic 兼容桩：只供自动化测试使用，不连接真实云端。

默认行为与早期版本一致：POST 任意路径返回 chat/completions 结构和一条
billing_source=balance 的用量。加 --billing go 时返回 Go 订阅的零费用用量；
请求路径以 /messages 结尾时返回 Anthropic Messages 结构。
"""
import argparse
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RESULT = {
    "question_text": "解方程：3(x-2)=2x+5",
    "solution_text": "3x-6=2x+5\n3x+2x=5+6\n5x=11\nx=11/5",
    "knowledge_points": ["一元一次方程"],
    "error_type": "符号错误",
    "observed_errors": ["第 2 行移项时 2x 与 -6 都没有变号"],
    "possible_causes": ["可能对移项要变号的规则记混了"],
    "advice_text": "先单独练 3 道只做移项的题，每步标出新符号。",
    "insufficient": False,
    "insufficient_reason": "",
    "likely_correct": False,
}

BILLING = {"source": "balance"}
USAGE = {"mode": "full"}
FAIL_MODES = ("ok", "400", "length", "maxtokens", "slow", "garbage")
FAIL = {"mode": "ok"}
REVIEW = {"mode": "ok", "case": ""}
REVIEW_MARKER = "【作答分析】"
REVIEW_RESULT = {
    "causes": [
        {"cause": "去括号时漏乘", "evidence": "3x-2=2x+5",
         "reasoning": "3(x-2) 应展开成 3x-6，第二步把 -6 丢掉了"},
        {"cause": "移项不变号", "evidence": "x=7",
         "reasoning": "最后一步没有写移项过程，无法确认符号是否正确"},
    ],
    "note": "第二条依据较弱，请老师核对",
    "uncertain": False,
}
REVIEW_UNCERTAIN = {
    "causes": [],
    "note": "学生只写了一行，信息不足，不能确定错因",
    "uncertain": True,
}

# 五类作答场景的模型返回，配上固定的合成作答使用。
REVIEW_CASES = {
    "correct": {
        "causes": [],
        "note": "学生步骤与标准解法一致，未发现明确错误。",
        "uncertain": True,
    },
    "sign": {
        "causes": [
            {"cause": "移项不变号", "evidence": "2x=10+6",
             "reasoning": "2x+6=10 移项后右边应是 10-6，写成 10+6 使结果变成 x=8"},
        ],
        "note": "",
        "uncertain": False,
    },
    "skipped": {
        "causes": [
            {"cause": "跳过去括号与移项", "evidence": "x=2",
             "reasoning": "从原式直接写到结果，中间步骤缺失"},
        ],
        "note": "步骤不完整，只能判断存在跳步。",
        "uncertain": True,
    },
    "conflict": {
        "causes": [
            {"cause": "答案与步骤不一致", "evidence": "答：x=9",
             "reasoning": "上一行已经算出 x=7，最后写的答案却是 x=9"},
        ],
        "note": "",
        "uncertain": False,
    },
    "insufficient": {
        "causes": [],
        "note": "只有一行结果，看不出用了什么方法。",
        "uncertain": True,
    },
    "fabricated": {
        "causes": [
            {"cause": "去括号漏乘", "evidence": "2x+6=10",
             "reasoning": "这一行学生并没有写"},
        ],
        "note": "",
        "uncertain": False,
    },
}


def review_payload():
    mode = REVIEW["mode"]
    if mode == "case":
        return REVIEW_CASES.get(REVIEW["case"]) or REVIEW_RESULT
    return REVIEW_UNCERTAIN if mode == "uncertain" else REVIEW_RESULT


def wants_review(parsed):
    for message in parsed.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str) and REVIEW_MARKER in content:
            return True
        for block in content or []:
            if isinstance(block, dict) and REVIEW_MARKER in str(block.get("text") or ""):
                return True
    return False


TUTOR = {"mode": "ok"}
TUTOR_MARKER = "【个人学情辅导建议】"
TUTOR_MODES = ("ok", "fabricate", "uncertain", "empty")


def _texts(parsed):
    parts = []
    for message in parsed.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def wants_tutor(parsed):
    return TUTOR_MARKER in _texts(parsed)


FOLLOWUP = {"suggest": "ok", "update": "ok"}
FOLLOWUP_MODES = ("ok", "fabricate", "uncertain", "empty")
SUGGEST_MARKER = "【追问候选】"
UPDATE_MARKER = "【追问后的错因候选】"


def wants_followup_suggest(parsed):
    return SUGGEST_MARKER in _texts(parsed)


def wants_followup_update(parsed):
    return UPDATE_MARKER in _texts(parsed)


def _prompt_fields(parsed):
    """把提示词里的「字段：值」摘出来，桩要引用原文，不能自己编。"""
    fields = {}
    for line in _texts(parsed).splitlines():
        line = line.strip()
        for key in ("题目原文：", "学生过程原文：", "老师已确认过的错因：", "学生回答：", "老师追问："):
            if line.startswith(key):
                fields[key[:-1]] = line[len(key):]
    return fields


def followup_suggest_payload(parsed):
    fields = _prompt_fields(parsed)
    evidence = fields.get("学生过程原文") or fields.get("题目原文") or ""
    mode = FOLLOWUP["suggest"]
    questions = []
    if mode != "empty" and evidence:
        questions.append({
            "question": "这一步把 6 移到右边时，你写成了 +6 还是 -6？",
            "why": "学生过程里这一步和标准解法不一样",
            "evidence": evidence,
        })
    if mode == "fabricate":
        questions.append({"question": "编造的追问", "why": "编造", "evidence": "这段话原文里没有"})
        questions.append({"question": "编造的追问二", "why": "编造", "evidence": "心里默念口诀"})
    return {
        "questions": questions,
        "note": "证据不足，先问一句确认学生怎么想的。" if mode == "uncertain" else "",
        "uncertain": mode == "uncertain",
    }


def followup_update_payload(parsed):
    fields = _prompt_fields(parsed)
    answer = fields.get("学生回答") or ""
    steps = fields.get("学生过程原文") or ""
    mode = FOLLOWUP["update"]
    causes = []
    if mode != "empty":
        causes.append({
            "cause": "移项时没有变号",
            "evidence": answer or steps,
            "reason": "学生回答里说把 +6 写成 +6，说明没意识到移项要变号",
        })
    if mode == "fabricate":
        causes.append({"cause": "编造的错因", "evidence": "学生说心里默念口诀", "reason": "编造"})
    return {
        "causes": causes,
        "note": "只是一次回答，不能据此判断学生是否掌握。" if mode == "uncertain" else "",
        "uncertain": mode == "uncertain",
    }


def tutor_payload(parsed):
    """从提示词里挑出问题名与原文，拼成一条形状正确的候选建议。"""
    problems = []
    current = None
    for line in _texts(parsed).splitlines():
        line = line.strip()
        if "问题「" in line and "」" in line:
            current = {"name": line.split("问题「", 1)[1].split("」", 1)[0], "parts": []}
            problems.append(current)
        elif line.startswith("原文：") and current is not None:
            current["parts"].append(line[3:])
    valid = next((p for p in problems if p["parts"]), None)
    focus = []
    if valid is not None:
        focus.append({
            "problem": valid["name"],
            "why": "最近 30 天内出现过，加权分最高",
            "evidence": valid["parts"][0],
            "how": "先用同类题示范一次移项，再让学生口述每一步为什么要变号。",
        })
    mode = TUTOR["mode"]
    if mode == "fabricate" and valid is not None:
        focus.append({"problem": "灵魂出窍", "why": "编造的问题", "evidence": "",
                      "how": "编造的做法"})
        focus.append({"problem": valid["name"], "why": "编造的证据",
                      "evidence": "做题时心里默念口诀", "how": "编造的做法"})
    if mode in ("uncertain", "empty"):
        focus = []
    return {
        "focus": focus,
        "note": "证据不足，只看到一两次错误，不能判断是否已经掌握。" if mode == "uncertain" else "",
        "uncertain": mode == "uncertain",
    }



def usage_payload():
    mode = USAGE["mode"]
    if mode == "none":
        return None
    if mode == "partial":
        usage = {
            "prompt_tokens": 1200,
            "completion_tokens": None,
            "total_tokens": None,
            "cost": None,
            "opencode": {"billing_source": BILLING["source"]},
        }
        return usage
    if mode == "zeros":
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0,
            "opencode": {"billing_source": BILLING["source"]},
        }
        if BILLING["source"] == "go":
            usage["opencode"]["allowance"] = 0
        return usage
    if mode == "nulls":
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "cost": None,
            "opencode": {},
        }
    usage = {
        "prompt_tokens": 1200,
        "completion_tokens": 180,
        "total_tokens": 1380,
        "input_tokens": 1200,
        "output_tokens": 180,
        "cost": 0.00348 if BILLING["source"] == "balance" else 0,
        "opencode": {
            "billing_source": BILLING["source"],
            "list_cost": 0.00348,
        },
    }
    if BILLING["source"] == "go":
        usage["opencode"]["allowance"] = 0.00348
        usage["opencode"]["tokens"] = {"input": 1200, "output": 180, "reasoning": 0,
                                       "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0}
    return usage


def summarise(parsed):
    """只留核对用途所需的形状，不把 base64 图片写进日志。"""
    summary = {"model": parsed.get("model"), "has_image": False, "max_tokens": parsed.get("max_tokens"),
               "text": "", "followup": "suggest" if wants_followup_suggest(parsed)
               else "update" if wants_followup_update(parsed) else ""}
    texts = []
    for message in parsed.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in ("image", "image_url"):
                summary["has_image"] = True
            elif kind == "text":
                texts.append(str(block.get("text") or ""))
    summary["text"] = "\n".join(texts)[:2000]
    return summary


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_path = ""
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = {}
        if self.path.rstrip("/") == "/__usage":
            mode = parsed.get("mode") or "full"
            USAGE["mode"] = mode if mode in ("full", "partial", "zeros", "nulls", "none") else "full"
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.rstrip("/") == "/__fail":
            mode = parsed.get("mode") or "ok"
            FAIL["mode"] = mode if mode in FAIL_MODES else "ok"
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.rstrip("/") == "/__review":
            mode = parsed.get("mode") or "ok"
            REVIEW["mode"] = mode if mode in ("ok", "uncertain", "case") else "ok"
            case = parsed.get("case") or ""
            REVIEW["case"] = case if case in REVIEW_CASES else ""
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.rstrip("/") == "/__tutor":
            mode = parsed.get("mode") or "ok"
            TUTOR["mode"] = mode if mode in TUTOR_MODES else "ok"
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.rstrip("/") == "/__followup":
            for key in ("suggest", "update"):
                mode = parsed.get(key)
                if mode:
                    FOLLOWUP[key] = mode if mode in FOLLOWUP_MODES else "ok"
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.log_path:
            record = summarise(parsed)
            usage = usage_payload() or {}
            opencode = usage.get("opencode") or {}
            record.update({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "path": self.path,
                "status": 400 if FAIL["mode"] == "400" else 200,
                "session": self.headers.get("x-opencode-session") or "",
                "user_agent": self.headers.get("User-Agent") or "",
                "authorization": bool(self.headers.get("Authorization")),
                "bytes": len(raw),
                "billing_source": opencode.get("billing_source"),
                "cost": usage.get("cost"),
                "allowance": opencode.get("allowance"),
                "kind": ("tutor" if wants_tutor(parsed)
                         else "followup" if (wants_followup_suggest(parsed) or wants_followup_update(parsed))
                         else "review" if wants_review(parsed) else "vision"),
            })
            with self.lock:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        if FAIL["mode"] == "400":
            data = json.dumps(
                {"error": {"message": "stub injected failure", "type": "invalid_request_error"}},
                ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return

        if FAIL["mode"] == "slow":
            time.sleep(6)

        if FAIL["mode"] == "garbage":
            data = b"not-a-json-body"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if wants_tutor(parsed):
            body = tutor_payload(parsed)
        elif wants_followup_suggest(parsed):
            body = followup_suggest_payload(parsed)
        elif wants_followup_update(parsed):
            body = followup_update_payload(parsed)
        else:
            body = review_payload() if wants_review(parsed) else RESULT
        content = "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"
        usage = usage_payload()
        if self.path.rstrip("/").endswith("/messages"):
            payload = {
                "id": "msg_stub_145",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": content}],
                "stop_reason": "max_tokens" if FAIL["mode"] == "maxtokens" else "end_turn",
            }
        else:
            payload = {
                "id": "chatcmpl-stub-145",
                "choices": [{"message": {"content": content},
                             "finish_reason": "length" if FAIL["mode"] == "length" else "stop"}],
            }
        if usage:
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


def make_server(handler, port=0, attempts=6):
    """绑定本地桩端口。

    Windows 偶尔会把某些端口排除在外（Hyper-V 保留区间），这时 bind 会报
    WinError 10013；换一个端口重试，避免整套测试因端口问题失败。
    """
    last = None
    for _ in range(attempts):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            last = exc
            port = 0
            time.sleep(0.05)
    raise last


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log", default="")
    parser.add_argument("--billing", default="balance", choices=("balance", "go"))
    args = parser.parse_args()
    Handler.log_path = args.log
    BILLING["source"] = args.billing
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("READY %d" % server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
