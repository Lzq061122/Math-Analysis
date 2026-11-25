"""1.5.0 作答核对与追问：源码级验收（本地临时库 + 环回 HTTP，零外部网络）。

依据 docs/作答核对_接口契约.md（§2 定位、§3 接口、§3.6 统计、§3.7 口径补充、§2.1 lead 裁定）
与 数智伴学_修改任务清单.md A1–A7 的「完成标准」。

为什么要连本地服务而不用桩：本轮 A1–A5 的完成标准里写着 409/500 两种失败路径、保存回执、
队列跳转与「关闭重开」，这些只有走真实 HTTP 栈才验得出来。服务只监听 127.0.0.1 的随机端口，
数据目录在 tests/.tmp-answer-check/，全程不访问任何外部主机。

反自欺约定：每条断言都必须是「可以假」的。第 12 组把 src/app 复制一份、按预期反向改错，
用子进程跑同一套断言，证明实现一改错断言就会失败（而不是恒真）。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import http.cookiejar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from app import answer_check as C  # noqa: E402
from app import demo_answers as D  # noqa: E402
from app import equation_steps as E  # noqa: E402
from app.answers import fingerprint as answer_fingerprint  # noqa: E402
from app.db import Database, now_iso  # noqa: E402
from app.stats import answer_check_stats  # noqa: E402
from logmeta import environment_lines, exit_line  # noqa: E402

import server as server_mod  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-answer-check")
LOG_PATH = os.path.join(ROOT, "docs", "verify-answer-check-150.txt")
PYTHON = sys.executable

RESULTS = {"pass": 0, "fail": 0, "hold": 0}
LOG_LINES = []

# 进行中的环境，退出时统一关掉，避免端口与线程泄漏。
ENVS = []

# 契约 §2 的固定句与文案
REASON_FIRST_BAD = "这里只记录观察到的不等价，不推断学生为什么这样写。"
STATUS_LINE_UNDECIDED = "已核对 · 原因待判断"

# 案例 A / B / C / 对照，全部取自 数智伴学_Agent开发规格.md §7 的数学内容
CASE_A = {
    "question": "3(x-2)=12",
    "standard": "3(x-2)=12；3x-6=12；3x=18；x=6",
    "steps": "3(x-2)=12\n3x-2=12\nx=14/3",
}
CASE_B = {
    "question": "5x-3=2x+6",
    "standard": "5x-3=2x+6；5x-2x=6+3；3x=9；x=3",
    "steps": "5x-3=2x+6\n5x-2x=6-3\nx=1",
}
CASE_C = {
    "question": "4(x-1)=8",
    "standard": "4(x-1)=8；x-1=2；x=3",
    "steps": "x=1",
}
CASE_OK = {
    "question": "2(x+3)=10",
    "standard": "2(x+3)=10；2x+6=10；2x=4；x=2",
    "steps": "2(x+3)=10\n2x+6=10\n2x=4\nx=2",
}

# 「为什么」式推断的禁用词：定位结果、判断与回执里都不许出现。
INFERENCE_WORDS = ("可能", "因为", "大概", "也许", "似乎", "估计", "应该是", "判断为", "推测", "学生想")


# --------------------------------------------------------------------------
# 日志与断言
# --------------------------------------------------------------------------
def log(text=""):
    print(text)
    LOG_LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s %s" % (name, detail))
    return bool(ok)


def hold(name, reason):
    """拿不到结论时用「未验证」，绝不记成通过：本项由别的成员并行修复中或本机无法取证。"""
    RESULTS["hold"] += 1
    log("未验证 %s —— %s" % (name, reason))


def section(title):
    log("")
    log("== %s ==" % title)


def jdump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def inference_hits(payload):
    """在一段返回里找「为什么」式推断用词；返回命中的词表，空表才算干净。"""
    text = jdump(payload)
    return [word for word in INFERENCE_WORDS if word in text]


# --------------------------------------------------------------------------
# 环回 HTTP 客户端
# --------------------------------------------------------------------------
class Client:
    """环回接口客户端。

    会话令牌显式放在 Cookie 头里，不靠 http.cookiejar：服务重启后端口会变，
    显式令牌才能保证「关掉重开」这条断言测的是服务端会话，而不是客户端 cookie 匹配。
    """

    def __init__(self, base, token=None):
        self.base = base
        self.token = token
        self.opener = urllib.request.build_opener()

    def request(self, method, path, body=None):
        data = None
        headers = {}
        if self.token:
            headers["Cookie"] = "teacher_session=" + self.token
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as resp:
                raw = resp.read()
                # 登录/注册接口把令牌只放进 Set-Cookie（响应体里已 pop 掉），这里顺手取出来。
                cookie = resp.headers.get("Set-Cookie") or ""
                if "teacher_session=" in cookie:
                    self.token = cookie.split("teacher_session=", 1)[1].split(";", 1)[0]
                return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                payload = {"raw": raw.decode("utf-8", "replace")}
            return exc.code, payload

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})


class Env:
    """一个数据目录 + 一个只监听 127.0.0.1 的服务 + 一个已登录的客户端。"""

    def __init__(self, name, register=True):
        self.name = name
        self.dir = os.path.join(WORK, name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        self.server = None
        self.client = None
        self.token = None
        self.start()
        self.db = Database(os.path.join(self.dir, "app.db"), create=False)
        if register:
            status, payload = self.client.post(
                "/api/auth/register", {"username": "t4", "password": "pw123456"}
            )
            if status != 200:
                raise RuntimeError("注册失败：%s %s" % (status, payload))
            self.token = self.client.token
            self.user_id = self.db.scalar("SELECT id FROM users WHERE username='t4'")
        else:
            self.user_id = None
        ENVS.append(self)

    # -- 服务生命周期 ----------------------------------------------------
    def start(self):
        self.server = server_mod.make_server("127.0.0.1", 0, self.dir, use_demo=False)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.base, token=self.token)

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=10)
            self.server = None

    def restart(self):
        """模拟「关掉软件、重新打开同一个数据目录」；会话 token 仍在库里，所以 cookie 还能用。"""
        self.stop()
        self.start()
        self.db.conn.close()
        self.db = Database(os.path.join(self.dir, "app.db"), create=False)

    def close(self):
        self.stop()
        try:
            self.db.conn.close()
        except Exception:
            pass

    # -- 造数 ------------------------------------------------------------
    def add_class(self, name):
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO classes(user_id, name, grade, note, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (self.user_id, name, "七年级", "", stamp, stamp),
            )
            return cur.lastrowid

    def add_student(self, class_id, name, code=""):
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO students(class_id, name, code, note, active, created_at, updated_at)"
                " VALUES (?,?,?,?,1,?,?)",
                (class_id, name, code, "", stamp, stamp),
            )
            return cur.lastrowid

    def add_answer(self, class_id, student_id, record_key, case, *, batch="9月第2次作业",
                   question_no="1", occurred_on="2026-11-24", missing=0, missing_reason="",
                   source="真实导入", imported_at=None, student_name="", unit="一元一次方程"):
        row = {
            "question_no": question_no,
            "question_text": case["question"],
            "unit": unit,
            "standard_solution": case["standard"],
            "student_steps": case["steps"],
            "occurred_on": occurred_on,
            "missing": int(missing),
            "missing_reason": missing_reason,
        }
        fp = answer_fingerprint(row, student_id)
        stamp = imported_at or now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, student_id, record_key, batch, student_name, "",
                 row["question_no"], row["question_text"], row["unit"], row["standard_solution"],
                 row["student_steps"], source, row["occurred_on"], row["missing"],
                 row["missing_reason"], fp, stamp, stamp, stamp),
            )
            return cur.lastrowid

    def checks_count(self):
        return int(self.db.scalar("SELECT COUNT(*) FROM answer_checks") or 0)

    def followups_count(self):
        return int(self.db.scalar("SELECT COUNT(*) FROM answer_followups") or 0)

    def mistakes_count(self):
        return int(self.db.scalar("SELECT COUNT(*) FROM mistakes") or 0)


def detail(env, class_id, answer_id):
    status, payload = env.client.get("/api/classes/%d/check/answers/%d" % (class_id, answer_id))
    if status != 200:
        raise RuntimeError("详情接口失败：%s %s" % (status, payload))
    return payload


def overview(env, class_id, batch=""):
    path = "/api/classes/%d/check/overview" % class_id
    if batch:
        path += "?batch=" + urllib.parse.quote(batch)
    status, payload = env.client.get(path)
    if status != 200:
        raise RuntimeError("总览接口失败：%s %s" % (status, payload))
    return payload


def save(env, class_id, answer_id, body):
    return env.client.post("/api/classes/%d/check/answers/%d/save" % (class_id, answer_id), body)


def save_body(data, *, cause_state="undecided", cause_code="", cause_label="",
              followup=None, observed=None, note="", index=None):
    """照契约 §3.3 组请求体；没给的字段按「老师什么也不改直接点主按钮」取默认值。"""
    locate = data.get("locate") or {}
    body = {
        "expected_version": (data.get("answer") or {}).get("version", ""),
        "observed_step": locate.get("first_bad_text") or "" if observed is None else observed,
        "first_bad_index": locate.get("first_bad_index") if index is None else index,
        "cause_code": cause_code,
        "cause_label": cause_label,
        "teacher_note": note,
        "cause_state": cause_state,
    }
    if followup is not None:
        body["followup"] = followup
    return body


# --------------------------------------------------------------------------
# 1. 定位六分支（纯函数）
# --------------------------------------------------------------------------
def group_1_locate():
    section("1. 确定位：5 种 status（correct / first_bad / missing_steps / missing / out_of_scope）+ 正确作答不标错 + 只有答案不猜行")

    a = E.locate(CASE_A["question"], CASE_A["standard"], CASE_A["steps"])
    check("1.1 案例A status=first_bad", a["status"] == "first_bad", a["status"])
    check("1.2 案例A first_bad_index=1（指向 3x-2=12）",
          a["first_bad_index"] == 1 and a["first_bad_text"] == "3x-2=12",
          "%s / %s" % (a["first_bad_index"], a["first_bad_text"]))
    check("1.3 案例A underline={start:2,end:4}，hint_text='-2' 逐字取自该行",
          a["underline"] == {"start": 2, "end": 4}
          and a["hint_text"] == "-2"
          and a["first_bad_text"][a["underline"]["start"]:a["underline"]["end"]] == a["hint_text"],
          "%s %s" % (a["underline"], a["hint_text"]))
    check("1.4 案例A contrast.expected 含 3x-6", "3x-6" in (a["contrast"]["expected"] or ""),
          a["contrast"]["expected"])
    check("1.5 案例A 对照句=单括号模板（原文用语不冒充学生写法）",
          a["contrast"]["note"] == "展开后应为 3x-6；括号外的 3 也要乘以 -2。",
          a["contrast"]["note"])
    check("1.6 案例A reason_note 固定句", a["reason_note"] == REASON_FIRST_BAD, a["reason_note"])
    check("1.7 案例A 只有 bad 行被标记",
          [line["bad"] for line in a["lines"]] == [False, True, False],
          [line["bad"] for line in a["lines"]])
    check("1.8 案例A lines 逐行保留原文",
          [line["text"] for line in a["lines"]] == ["3(x-2)=12", "3x-2=12", "x=14/3"],
          [line["text"] for line in a["lines"]])
    check("1.9 案例A source=rule", a["source"] == "rule", a["source"])

    ok = E.locate(CASE_OK["question"], CASE_OK["standard"], CASE_OK["steps"])
    check("1.10 正确作答 status=correct 且 first_bad_index=None",
          ok["status"] == "correct" and ok["first_bad_index"] is None, ok["status"])
    check("1.11 正确作答不标错：没有任何 bad 行、没有 hint_text",
          not any(line["bad"] for line in ok["lines"]) and ok["hint_text"] is None,
          jdump(ok["lines"]))

    c = E.locate(CASE_C["question"], CASE_C["standard"], CASE_C["steps"])
    check("1.12 只有答案 status=missing_steps 且 first_bad_index=None（不猜行）",
          c["status"] == "missing_steps" and c["first_bad_index"] is None, c["status"])
    check("1.13 只有答案 不标错、manual_note 提示缺少步骤",
          not any(line["bad"] for line in c["lines"]) and "缺少步骤" in c["manual_note"],
          c["manual_note"])

    m = E.locate(CASE_A["question"], CASE_A["standard"], CASE_A["steps"], missing=True,
                 missing_reason="未提供学生步骤")
    check("1.14 缺测 status=missing、source=none、不标错",
          m["status"] == "missing" and m["source"] == "none"
          and not any(line["bad"] for line in m["lines"]), m["status"])
    check("1.15 缺测 reason_note 说明缺测且不猜原因", "不标错步" in m["reason_note"], m["reason_note"])

    oos = E.locate("2x+1>3", "x>1", "2x+1>3\n2x>2\nx>1")
    check("1.16 超范围 status=out_of_scope、manual_note=已转人工",
          oos["status"] == "out_of_scope" and oos["manual_note"] == "已转人工",
          "%s / %s" % (oos["status"], oos["manual_note"]))
    check("1.17 超范围 原步骤照常完整显示、不标错",
          [line["text"] for line in oos["lines"]] == ["2x+1>3", "2x+1>3", "2x>2", "x>1"]
          and not any(line["bad"] for line in oos["lines"]),
          jdump(oos["lines"]))

    # 全角、数学减号、省略乘号：契约 §2 判定规则第 5 条
    full = E.locate("3（x－2）＝12", "3(x-2)=12；x=6", "3（x－2）＝12\n3x－2＝12\nx=14/3")
    # hint_text 必须逐字取自学生原文，所以这里保留全角减号「－2」；规范化之后才是 -2。
    check("1.18 全角/数学减号能吃下：仍定位到第 2 行，hint 逐字=「－2」（规范化 -2）",
          full["status"] == "first_bad" and full["first_bad_index"] == 1
          and full["hint_text"] == full["first_bad_text"][full["underline"]["start"]:full["underline"]["end"]]
          and full["hint_text"].replace("－", "-") == "-2",
          "%s %r %s" % (full["status"], full["hint_text"], full["underline"]))
    star = E.locate("3*(x-2)=12", "3x-6=12；x=6", "3*(x-2)=12\n3x-6=12\nx=6")
    check("1.19 3*(x-2) 与 3(x-2) 同解：status=correct", star["status"] == "correct", star["status"])

    # 案例 B 走 §2.1 新裁定：两端逐字取自原文，actual 必须等于 first_bad_text
    b = E.locate(CASE_B["question"], CASE_B["standard"], CASE_B["steps"])
    check("1.20 案例B first_bad_text 逐字出现在对照句里（§2.1 第 1 条）",
          b["status"] == "first_bad" and b["first_bad_text"] in b["contrast"]["note"],
          b["contrast"]["note"])
    check("1.21 案例B contrast.actual == first_bad_text（不拿化简式冒充学生原文，§2.1 第 2 条）",
          b["contrast"]["actual"] == b["first_bad_text"], b["contrast"]["actual"])
    check("1.22 案例B 对照句两端都是原文行",
          b["contrast"]["note"] == "这两行不等价：上一行是 5x-3=2x+6，学生这一行是 5x-2x=6-3。",
          b["contrast"]["note"])

    check("1.23 定位返回不含「为什么」式推断用词（案例A/B/正确/缺测）",
          not inference_hits({"a": {k: a[k] for k in ("status", "reason_note", "contrast", "hint_text")},
                              "b": {k: b[k] for k in ("status", "reason_note", "contrast", "hint_text")},
                              "ok": ok["reason_note"], "m": m["reason_note"]}),
          inference_hits({"a": a["contrast"], "b": b["contrast"]}))

    # 案例 D（负系数）：lead 指出过旧实现会写成「括号外的 2 也要乘以 -3」，负号丢了
    d = E.locate("-2(x-3)=-2x+6", "-2(x-3)=-2x+6；-2x+6=-2x+6；x=0",
                 "-2(x-3)=-2x+6\n-2x-6=-2x+6")
    check("1.24 案例D 负系数：first_bad_index=1、hint='-6' 逐字取自该行",
          d["status"] == "first_bad" and d["first_bad_index"] == 1 and d["hint_text"] == "-6"
          and d["first_bad_text"][d["underline"]["start"]:d["underline"]["end"]] == "-6",
          "%s %s %s" % (d["status"], d["hint_text"], d["underline"]))
    check("1.25 案例D 对照句保留括号外系数的负号（括号外的 -2 也要乘以 -3）",
          d["contrast"]["note"] == "展开后应为 -2x+6；括号外的 -2 也要乘以 -3。",
          d["contrast"]["note"])
    check("1.26 案例D contrast.actual 与 hint 同形态（都是 -6）",
          d["contrast"]["actual"] == "-6" and d["contrast"]["expected"] == "-2x+6",
          jdump(d["contrast"]))

    # 契约 §2 的 status 只有 5 个取值、source 只有 rule/none；以后悄悄多一个没测过的分支要当场红。
    statuses = {a["status"], b["status"], ok["status"], c["status"], m["status"], oos["status"]}
    check("1.27 status 只能取契约 §2 的 5 个值之一",
          statuses <= {"correct", "first_bad", "missing_steps", "missing", "out_of_scope"},
          statuses)
    sources = {a["source"], b["source"], ok["source"], c["source"], m["source"], oos["source"]}
    check("1.28 source 只有 rule / none 两个取值", sources <= {"rule", "none"}, sources)
    pairs_seen = [(x["status"], x["source"]) for x in (a, b, ok, c, m, oos)]
    check("1.29 status→source 映射符合契约（first_bad/correct/missing_steps→rule，"
          "missing/out_of_scope→none）",
          pairs_seen == [("first_bad", "rule"), ("first_bad", "rule"), ("correct", "rule"),
                         ("missing_steps", "rule"), ("missing", "none"),
                         ("out_of_scope", "none")], pairs_seen)

    # R-07（T12）：四种减号（全角－、小写﹣、数学−、ASCII-）下 actual 与 hint 必须逐字符相同，
    # 而且都逐字取自 first_bad_text 的划线区间——不能只比「值相等」，要比字符。
    minus_results = []
    for minus in ("－", "﹣", "−", "-"):
        step = "3x%s2=12" % minus
        item = E.locate(CASE_A["question"], CASE_A["standard"],
                        "3(x-2)=12\n" + step + "\nx=14/3")
        span = item["underline"] or {}
        raw = (item["first_bad_text"] or "")[span.get("start", 0):span.get("end", 0)]
        minus_results.append({
            "minus": minus,
            "hint": item["hint_text"],
            "actual": item["contrast"]["actual"],
            "raw": raw,
            "ok": item["hint_text"] == item["contrast"]["actual"] == raw == ("%s2" % minus),
        })
    check("1.30 R-07：四种减号 hint_text == contrast.actual == 原文切片（逐字符相同）",
          all(row["ok"] for row in minus_results), jdump(minus_results))


# --------------------------------------------------------------------------
# 2. 案例 A 的具体断言 + 预置来源分离
# --------------------------------------------------------------------------
def group_2_case_a_and_preset(envs):
    section("2. 案例A 的三项具体断言（走接口）+ 预置候选单一来源 + 真实导入必须走 rule")

    env = Env("case-a")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    for name in ("学生甲", "学生乙", "学生丙", "学生丁"):
        env.add_student(class_id, name)

    status, seeded = env.client.post("/api/classes/%d/check/demo/seed" % class_id)
    check("2.1 合成演示 seed 成功且写入 4 份作答",
          status == 200 and seeded.get("seeded", {}).get("answers") == 4, jdump(seeded))

    rows = {row["record_key"]: row for row in env.db.query(
        "SELECT * FROM answers WHERE class_id=? ORDER BY id", (class_id,))}
    answer_a = rows["DEMO-A-01"]["id"]
    data = detail(env, class_id, answer_a)
    locate = data["locate"]

    check("2.2 详情 locate.status=first_bad 且 source=preset（合成演示只给预置定位）",
          locate["status"] == "first_bad" and locate["source"] == "preset",
          "%s/%s" % (locate["status"], locate["source"]))
    check("2.3 first_bad_index 指向 3x-2=12 那一行",
          locate["first_bad_index"] == 1 and locate["first_bad_text"] == "3x-2=12",
          "%s/%s" % (locate["first_bad_index"], locate["first_bad_text"]))
    check("2.4 hint_text 逐字出现在该行原文里", locate["hint_text"] in locate["first_bad_text"],
          "%s in %s" % (locate["hint_text"], locate["first_bad_text"]))
    check("2.5 underline 精确覆盖 -2", locate["underline"] == {"start": 2, "end": 4},
          jdump(locate["underline"]))
    check("2.6 contrast.expected 含 3x-6", "3x-6" in (locate["contrast"]["expected"] or ""),
          locate["contrast"]["expected"])
    check("2.7 详情返回不含任何「为什么」式推断（locate+judgement）",
          not inference_hits({"locate": locate, "judgement": data["judgement"]}),
          inference_hits({"locate": locate}))
    check("2.8 详情标合成演示数据", data["answer"]["data_source"] == "synthetic"
          and data["answer"]["data_source_label"] == "合成演示数据",
          data["answer"]["data_source_label"])

    preset = D.preset_causes(CASE_A["question"])
    api_causes = data["candidates"]
    check("2.9 候选唯一来源：接口 candidates == demo_answers.preset_causes(同题)",
          api_causes == preset, "%d vs %d" % (len(api_causes), len(preset)))
    check("2.10 候选不超过 3 条且每条带 支持/削弱/待补",
          len(api_causes) <= 3
          and all(c.get("support") and c.get("rebut") and c.get("pending") for c in api_causes),
          [len(c.get("support") or []) for c in api_causes])
    check("2.11 causes_source=preset 且 causes_note 说明本轮未接已审错因库",
          data["causes_source"] == "preset" and "B2" in (data["causes_note"] or ""),
          "%s / %s" % (data["causes_source"], data["causes_note"]))
    check("2.12 案例A 默认问句用预置那条",
          data["followup"]["question"] == "你当时是怎么展开这个括号的？",
          data["followup"]["question"])

    # 同一题的「真实导入」作答：预置数据绝不能套上去，必须走规则定位
    real_class = env.add_class("七年级 2 班")
    real_student = env.add_student(real_class, "学生甲")
    real_answer = env.add_answer(real_class, real_student, "REAL-A-01", CASE_A,
                                 source="真实导入", student_name="学生甲")
    real = detail(env, real_class, real_answer)
    check("2.13 真实导入作答 locate.source=rule（不得出现 preset）",
          real["locate"]["source"] == "rule" and real["locate"]["status"] == "first_bad",
          "%s/%s" % (real["locate"]["source"], real["locate"]["status"]))
    check("2.14 真实导入与预置同题同步骤：定位四项一致（说明预置没被冒充）",
          (real["locate"]["first_bad_index"], real["locate"]["hint_text"],
           real["locate"]["contrast"]["expected"], real["locate"]["underline"])
          == (locate["first_bad_index"], locate["hint_text"],
              locate["contrast"]["expected"], locate["underline"]),
          jdump([real["locate"]["first_bad_index"], real["locate"]["hint_text"],
                 real["locate"]["contrast"]["expected"], real["locate"]["underline"]]))
    check("2.15 真实导入 data_source=imported / 真实导入",
          real["answer"]["data_source"] == "imported"
          and real["answer"]["data_source_label"] == "真实导入",
          real["answer"]["data_source_label"])
    check("2.16 真实导入仍拿同一份预置候选（候选是预置库、定位是规则，二者分离）",
          real["candidates"] == preset and real["causes_source"] == "preset",
          len(real["candidates"]))

    # 契约 §3.2：demo_answers 被裁掉时必须安静退化（[]+兜底说明），不许拖垮详情接口。
    # 用 sys.modules 里放 None 让 `from . import demo_answers` 真的抛 ImportError，走的是同一条 except 分支。
    import sys as _sys
    import app as _app_pkg
    saved_module = _sys.modules.get("app.demo_answers")
    saved_attr = getattr(_app_pkg, "demo_answers", None)
    # 只把 sys.modules 置 None 不够：包属性还在时 `from . import demo_answers` 会直接用缓存。
    # 两个都撤掉，导入才真的失败，才走的是 answer_check 里那条 except ImportError。
    _sys.modules["app.demo_answers"] = None
    if saved_attr is not None:
        delattr(_app_pkg, "demo_answers")
    raised = None
    broken_causes = broken_note = None
    try:
        broken_causes = C.candidates_for({"question_text": CASE_A["question"]})
        broken_note = C.causes_note()
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        if saved_module is not None:
            _sys.modules["app.demo_answers"] = saved_module
        else:
            _sys.modules.pop("app.demo_answers", None)
        if saved_attr is not None:
            setattr(_app_pkg, "demo_answers", saved_attr)
    check("2.17 demo_answers 不可用时 candidates_for 退化为 []、不抛异常、说明用兜底句",
          raised is None and broken_causes == [] and broken_note == C.FALLBACK_CAUSES_NOTE,
          "%r / %r / %r" % (broken_causes, broken_note, raised))
    return env, class_id


# --------------------------------------------------------------------------
# 3. 确定性
# --------------------------------------------------------------------------
def group_3_determinism():
    section("3. 确定性：同进程两次一致 + 跨进程一致")

    cases = [
        [CASE_A["question"], CASE_A["standard"], CASE_A["steps"]],
        [CASE_B["question"], CASE_B["standard"], CASE_B["steps"]],
        [CASE_C["question"], CASE_C["standard"], CASE_C["steps"]],
        [CASE_OK["question"], CASE_OK["standard"], CASE_OK["steps"]],
    ]
    first = [E.locate(*case) for case in cases]
    second = [E.locate(*case) for case in cases]
    check("3.1 同进程两次调用完全一致", jdump(first) == jdump(second))

    code = (
        "import json,sys;sys.path.insert(0,{src!r});"
        "from app import equation_steps as E;"
        "cases=json.loads({cases!r});"
        "print(json.dumps([E.locate(*c) for c in cases],ensure_ascii=True,sort_keys=True))"
    ).format(src=os.path.join(ROOT, "src"), cases=json.dumps(cases, ensure_ascii=False))
    proc = subprocess.run([PYTHON, "-c", code], capture_output=True, timeout=120)
    check("3.2 子进程运行成功（退出码 0）", proc.returncode == 0,
          proc.stderr.decode("utf-8", "replace")[-400:])
    try:
        remote = json.loads(proc.stdout.decode("utf-8"))
    except ValueError:
        remote = None
    check("3.3 跨进程结果与本地一致（含 status / first_bad_index / hint / contrast）",
          remote is not None and jdump(remote) == jdump(json.loads(jdump(first))),
          (proc.stdout.decode("utf-8", "replace")[:200] if remote is None else "mismatch"))
    return first


# --------------------------------------------------------------------------
# 4. 两轴独立 + 关库重开
# --------------------------------------------------------------------------
def group_4_two_axes(envs):
    section("4. 两轴独立：progress=checked 与 cause_state=undecided 同时成立；关掉重开库后仍然成立")

    env = Env("two-axes")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    answer_id = env.add_answer(class_id, student, "A1", CASE_A, student_name="学生甲")

    data = detail(env, class_id, answer_id)
    check("4.1 保存前两轴都是待：pending / undecided",
          data["judgement"]["progress"] == "pending"
          and data["judgement"]["cause_state"] == "undecided", jdump(data["judgement"]))

    status, result = save(env, class_id, answer_id, save_body(data))
    check("4.2 主按钮保存成功（200 ok）", status == 200 and result.get("ok") is True,
          "%s %s" % (status, jdump(result)[:200]))
    judgement = result.get("judgement") or {}
    check("4.3 progress=checked 与 cause_state=undecided 同时成立",
          judgement.get("progress") == "checked"
          and judgement.get("cause_state") == "undecided", jdump(judgement))
    check("4.4 两个标签分开给：progress_label=已核对 / cause_label=原因待判断",
          judgement.get("progress_label") == "已核对"
          and judgement.get("cause_label") == "原因待判断", jdump(judgement))
    check("4.5 回执 status_line=「%s」" % STATUS_LINE_UNDECIDED,
          (result.get("receipt") or {}).get("status_line") == STATUS_LINE_UNDECIDED,
          jdump(result.get("receipt")))

    row = env.db.query_one("SELECT * FROM answer_checks WHERE answer_id=?", (answer_id,))
    check("4.6 库里两条轴是两列、各自独立取值",
          row and row["progress"] == "checked" and row["cause_state"] == "undecided",
          jdump(row and {"progress": row["progress"], "cause_state": row["cause_state"]}))

    env.restart()
    # 另开一条独立连接直接读库，绕开应用层缓存
    conn = sqlite3.connect(os.path.join(env.dir, "app.db"))
    conn.row_factory = sqlite3.Row
    direct = conn.execute("SELECT progress, cause_state FROM answer_checks WHERE answer_id=?",
                          (answer_id,)).fetchone()
    conn.close()
    check("4.7 关闭并重开数据目录后，直接读库仍是 checked / undecided",
          direct is not None and direct["progress"] == "checked"
          and direct["cause_state"] == "undecided", jdump(dict(direct) if direct else None))

    again = detail(env, class_id, answer_id)
    check("4.8 重开后详情仍显示「已核对 · 原因待判断」",
          again["judgement"]["progress_label"] == "已核对"
          and again["judgement"]["cause_label"] == "原因待判断"
          and not again["judgement"]["stale"], jdump(again["judgement"]))
    return env, class_id, answer_id


# --------------------------------------------------------------------------
# 5. 队列（含乱序核对）+ 保存后找回 + 最后一份收尾
# --------------------------------------------------------------------------
def group_5_queue(envs):
    section("5. 队列：待核对数与「查看下一份」同一队列；乱序核对；保存后仍能找回；最后一份收尾")

    env = Env("queue")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    a1 = env.add_answer(class_id, student, "Q1", CASE_A, question_no="1", student_name="学生甲")
    a2 = env.add_answer(class_id, student, "Q2", CASE_B, question_no="2", student_name="学生甲")
    a3 = env.add_answer(class_id, student, "Q3", CASE_C, question_no="3", student_name="学生甲")

    ov = overview(env, class_id)
    check("5.1 三份待核对：待核对数=3，与队列 total 一致",
          ov["state"] == "has_pending" and ov["assignment"]["pending"] == 3
          and (ov["next"] or {}).get("answer_id") == a1, jdump(ov["assignment"]))
    d1 = detail(env, class_id, a1)
    check("5.2 「查看下一份」与总览 next 同一队列（第 1 份 = 队列第 1/3）",
          d1["queue"]["position"] == 1 and d1["queue"]["total"] == 3
          and d1["queue"]["is_last"] is False, jdump(d1["queue"]))

    # 乱序核对：先保存中间那份（契约 §3.7 第 3 条）
    status, r2 = save(env, class_id, a2, save_body(detail(env, class_id, a2)))
    q2 = r2.get("queue") or {}
    check("5.3 先保存中间那份：next 指向 id 最小的待核对项（第 1 份）",
          status == 200 and q2.get("next_answer_id") == a1,
          "%s %s" % (q2.get("next_answer_id"), a1))
    check("5.4 中间那份保存后 remaining=2、is_last=false、total 把自己算回来=3",
          q2.get("remaining") == 2 and q2.get("is_last") is False and q2.get("total") == 3,
          jdump(q2))
    ov2 = overview(env, class_id)
    check("5.5 总览待核对数同步变 2，且 next 与「查看下一份」仍是同一队列",
          ov2["assignment"]["pending"] == 2 and (ov2["next"] or {}).get("answer_id") == a1,
          jdump(ov2["assignment"]))
    d2 = detail(env, class_id, a2)
    check("5.6 已保存那份退出队列但仍按原位置显示第 2/3（不是第 1/2）",
          d2["queue"]["position"] == 2 and d2["queue"]["total"] == 3
          and d2["judgement"]["progress"] == "checked", jdump(d2["queue"]))

    status, r1 = save(env, class_id, a1, save_body(detail(env, class_id, a1)))
    q1 = r1.get("queue") or {}
    check("5.7 再保存第 1 份：next 指向第 3 份", status == 200 and q1.get("next_answer_id") == a3,
          "%s %s" % (q1.get("next_answer_id"), a3))

    status, r3 = save(env, class_id, a3, save_body(detail(env, class_id, a3)))
    q3 = r3.get("queue") or {}
    check("5.8 保存最后一份：next_answer_id=null 且 is_last=true、remaining=0",
          status == 200 and q3.get("next_answer_id") is None and q3.get("is_last") is True
          and q3.get("remaining") == 0, jdump(q3))
    ov3 = overview(env, class_id)
    check("5.9 全部保存后 state=all_checked、pending=0、next=null",
          ov3["state"] == "all_checked" and ov3["assignment"]["pending"] == 0
          and ov3["next"] is None, jdump(ov3))

    status, records = env.client.get("/api/classes/%d/check/records" % class_id)
    ids = sorted(item["answer_id"] for item in records.get("items") or [])
    check("5.10 保存后退出队列，但 check/records 仍能找回三条",
          status == 200 and records.get("total") == 3 and ids == sorted([a1, a2, a3]),
          jdump(ids))
    check("5.11 records 里每条 progress=checked、cause_state=undecided",
          all(item["progress"] == "checked" and item["cause_state"] == "undecided"
              for item in records.get("items") or []),
          jdump([(i["progress"], i["cause_state"]) for i in records.get("items") or []]))
    status, all_records = env.client.get("/api/classes/%d/check/records?cause_state=all" % class_id)
    check("5.12 cause_state=all 也给出同样三条",
          status == 200 and all_records.get("total") == 3, all_records.get("total"))
    return env, class_id, [a1, a2, a3]


# --------------------------------------------------------------------------
# 6. 改判 + cause_history
# --------------------------------------------------------------------------
def group_6_rejudge(envs):
    section("6. 改判：旧原因进 cause_history，当前结论只显示新原因；decided 必须带编码与说明")

    env = Env("rejudge")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    answer_id = env.add_answer(class_id, student, "A1", CASE_A, student_name="学生甲")
    data = detail(env, class_id, answer_id)
    causes = data["candidates"]
    first, second = causes[0], causes[1]

    status, r1 = save(env, class_id, answer_id, save_body(
        data, cause_state="decided", cause_code=first["code"], cause_label=first["label"],
        followup={"question": data["followup"]["question"],
                  "answer_text": "2 只要乘 y 就好了。"}))
    j1 = r1.get("judgement") or {}
    check("6.1 采纳第一条：cause_state=decided 且 cause_code 有值",
          status == 200 and j1.get("cause_state") == "decided"
          and j1.get("cause_code") == first["code"], jdump(j1))
    check("6.2 当前结论显示新原因说明",
          j1.get("cause_label_text") == first["label"], j1.get("cause_label_text"))

    status, r2 = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id), cause_state="decided",
        cause_code=second["code"], cause_label=second["label"]))
    j2 = r2.get("judgement") or {}
    row = env.db.query_one("SELECT * FROM answer_checks WHERE answer_id=?", (answer_id,))
    history = json.loads(row["cause_history"] or "[]")
    check("6.3 改判后 cause_history 长度 +1，装的是旧原因",
          status == 200 and len(history) == 1 and history[0]["code"] == first["code"]
          and history[0]["label"] == first["label"], jdump(history))
    check("6.4 当前结论只显示新原因（cause_code / cause_label_text 都是新的）",
          j2.get("cause_code") == second["code"]
          and j2.get("cause_label_text") == second["label"]
          and first["label"] not in (j2.get("cause_label_text") or ""), jdump(j2))
    check("6.5 改判不新增重复行：这一份仍然只有 1 条 check",
          env.checks_count() == 1, env.checks_count())

    status, records = env.client.get(
        "/api/classes/%d/check/records?cause_state=decided" % class_id)
    item = (records.get("items") or [{}])[0]
    check("6.6 记录列表里也只显示新原因",
          status == 200 and records.get("total") == 1
          and item.get("cause_label_text") == second["label"], jdump(item))

    before = env.checks_count()
    status, bad = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id), cause_state="decided",
        cause_code="", cause_label=""))
    check("6.7 decided 但缺 cause_code/label → 400 cause_required 且不写库",
          status == 400 and bad.get("code") == "cause_required"
          and env.checks_count() == before, "%s %s" % (status, bad.get("code")))
    return env, class_id, answer_id


# --------------------------------------------------------------------------
# 7. 缺测 / 正确 / 只有答案：保存不产生错题，原因强制回 undecided
# --------------------------------------------------------------------------
def group_7_no_mistake(envs):
    section("7. 缺测/正确/只有答案：点保存不产生错题、不计 original_errors、cause_state 强制回 undecided")

    env = Env("no-mistake")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    correct_id = env.add_answer(class_id, student, "OK-1", CASE_OK, question_no="1",
                                student_name="学生甲")
    steps_id = env.add_answer(class_id, student, "ONLY-1", CASE_C, question_no="2",
                              student_name="学生甲")
    missing_id = env.add_answer(class_id, student, "MISS-1", CASE_A, question_no="3",
                                missing=1, missing_reason="未提供学生步骤", student_name="学生甲")
    error_id = env.add_answer(class_id, student, "ERR-1", CASE_A, question_no="4",
                              student_name="学生甲")

    forced = []
    for answer_id, label in ((correct_id, "正确作答"), (steps_id, "只有答案"),
                             (missing_id, "缺测")):
        status, result = save(env, class_id, answer_id, save_body(
            detail(env, class_id, answer_id), cause_state="decided",
            cause_code="E-DIST-01", cause_label="只把系数乘以括号里的第一项"))
        judgement = result.get("judgement") or {}
        row = env.db.query_one("SELECT * FROM answer_checks WHERE answer_id=?", (answer_id,))
        ok = (status == 200 and judgement.get("progress") == "checked"
              and judgement.get("cause_state") == "undecided"
              and row["cause_state"] == "undecided" and row["cause_code"] == "")
        forced.append((label, ok, judgement.get("cause_state"), row["cause_code"]))
    check("7.1 正确/只有答案/缺测 三份都能保存处理进度，但原因被强制回 undecided",
          all(item[1] for item in forced), jdump(forced))

    status, result = save(env, class_id, error_id, save_body(detail(env, class_id, error_id)))
    check("7.2 真的错步那份保存成功", status == 200, status)

    # R-10：这条是**不变量**，不是可证伪的断言——本轮核对链路没有写 mistakes 的分支，
    # 任何实现都会绿。同类证据由 7.7（locate_status 逐行如实）与 7.4（统计口径）承担。
    log("说明（R-10）：7.3 属不变量（核对链路不写 mistakes）；该口径的证据由 7.4 / 7.7 承担。")
    check("7.3 点保存没有生成任何错题（mistakes 表仍为 0；不变量，证据见 7.4/7.7）",
          env.mistakes_count() == 0, env.mistakes_count())
    stats = answer_check_stats(env.db, env.db.query_one("SELECT * FROM classes WHERE id=?", (class_id,)))
    check("7.4 original_errors 只数错步那一份（=1），缺测与正确不计入",
          stats["original_errors"] == 1, stats["original_errors"])
    check("7.5 cause_decided=0：三份被强制待判断，只有错步那份手动保存也是 undecided",
          stats["cause_decided"] == 0, stats["cause_decided"])
    check("7.6 checked=4 / pending=0 / missing=1",
          stats["checked"] == 4 and stats["pending"] == 0 and stats["missing"] == 1,
          jdump(stats))
    checks = env.db.query("SELECT cause_state, cause_code, locate_status FROM answer_checks"
                          " ORDER BY id")
    check("7.7 四行 check 的 cause_state 全是 undecided，locate_status 各自如实",
          all(row["cause_state"] == "undecided" and row["cause_code"] == "" for row in checks)
          and {row["locate_status"] for row in checks}
          == {"correct", "missing_steps", "missing", "first_bad"},
          jdump(checks))


# --------------------------------------------------------------------------
# 8. 版本失效（模拟重导）
# --------------------------------------------------------------------------
def group_8_stale(envs):
    section("8. 版本失效：改 student_steps + 更新 fingerprint 模拟重导 → 旧 check 退出队列与统计、stale=true、回待核对")

    env = Env("stale")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    target = env.add_answer(class_id, student, "S1", CASE_A, question_no="1", student_name="学生甲")
    other = env.add_answer(class_id, student, "S2", CASE_B, question_no="2", student_name="学生甲")

    status, _ = save(env, class_id, target, save_body(detail(env, class_id, target)))
    before = overview(env, class_id)
    check("8.1 重导前：checked=1 / pending=1",
          status == 200 and before["assignment"]["checked"] == 1
          and before["assignment"]["pending"] == 1, jdump(before["assignment"]))

    # 模拟重导：学生改了步骤，fingerprint 随之变化（契约 §1.2）
    new_steps = "3(x-2)=12\n3x-6=12\n3x=18\nx=6"
    row = env.db.query_one("SELECT * FROM answers WHERE id=?", (target,))
    payload = {
        "student_id": int(row["student_id"]),
        "question_no": row["question_no"],
        "question_text": row["question_text"],
        "unit": row["unit"],
        "standard_solution": row["standard_solution"],
        "student_steps": new_steps,
        "occurred_on": row["occurred_on"],
        "missing": int(row["missing"]),
        "missing_reason": row["missing_reason"],
    }
    import hashlib
    new_fp = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()
    with env.db.tx() as conn:
        conn.execute("UPDATE answers SET student_steps=?, fingerprint=?, updated_at=? WHERE id=?",
                     (new_steps, new_fp, now_iso(), target))
    check("8.2 造数自检：新 fingerprint 与旧的不同", new_fp != row["fingerprint"],
          "%s -> %s" % (row["fingerprint"][:8], new_fp[:8]))

    data = detail(env, class_id, target)
    check("8.3 stale=true 且旧结论不再作为当前结论（progress=pending、saved_at 空）",
          data["judgement"]["stale"] is True
          and data["judgement"]["progress"] == "pending"
          and data["judgement"]["saved_at"] == "", jdump(data["judgement"]))
    after = overview(env, class_id)
    check("8.4 旧 check 退出队列：pending 回到 2、next 回到这一份",
          after["assignment"]["pending"] == 2 and after["assignment"]["checked"] == 0
          and (after["next"] or {}).get("answer_id") == target, jdump(after["assignment"]))
    stats = answer_check_stats(env.db, env.db.query_one("SELECT * FROM classes WHERE id=?", (class_id,)))
    check("8.5 旧 check 退出统计：checked=0 / pending=2 / original_errors=0",
          stats["checked"] == 0 and stats["pending"] == 2 and stats["original_errors"] == 0,
          jdump(stats))
    status, records = env.client.get("/api/classes/%d/check/records?cause_state=all" % class_id)
    check("8.6 记录找回里也不列旧版本 check", status == 200 and records.get("total") == 0,
          records.get("total"))
    check("8.7 历史仍保留：库里那条旧 check 没被删",
          env.checks_count() == 1
          and env.db.scalar("SELECT answer_version FROM answer_checks WHERE answer_id=?",
                            (target,)) == row["fingerprint"],
          env.checks_count())
    # 反向自检：同一断言在重导前后给出相反结果，说明它不是恒真
    check("8.8 断言非恒真自检：pending 重导前=1、重导后=2（同一表达式变了）",
          before["assignment"]["pending"] == 1 and after["assignment"]["pending"] == 2,
          "%s -> %s" % (before["assignment"]["pending"], after["assignment"]["pending"]))


# --------------------------------------------------------------------------
# 9. 保存失败：409 / 500
# --------------------------------------------------------------------------
def group_9_failures(envs):
    section("9. 保存失败：expected_version 不匹配→409 version_conflict；注入异常→500 save_failed；两者都不写库")

    env = Env("failures")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    answer_id = env.add_answer(class_id, student, "F1", CASE_A, student_name="学生甲")
    data = detail(env, class_id, answer_id)

    before = env.checks_count()
    body = save_body(data)
    body["expected_version"] = "0" * 64
    status, payload = save(env, class_id, answer_id, body)
    check("9.1 版本不一致 → 409 version_conflict",
          status == 409 and payload.get("code") == "version_conflict",
          "%s %s" % (status, payload.get("code")))
    check("9.2 409 时库无新增行", env.checks_count() == before, env.checks_count())

    os.environ["MISTAKE_FAULT"] = "answer_check_save"
    try:
        status, payload = save(env, class_id, answer_id, save_body(data))
    finally:
        os.environ.pop("MISTAKE_FAULT", None)
    check("9.3 注入异常 → 500 save_failed",
          status == 500 and payload.get("code") == "save_failed",
          "%s %s" % (status, payload.get("code")))
    check("9.4 500 时库无新增行（半条记录都不留）", env.checks_count() == before,
          env.checks_count())
    check("9.5 500 的错误说明面向老师（提到「没有写入」「重试」）",
          "没有写入" in (payload.get("error") or "") and "重试" in (payload.get("error") or ""),
          payload.get("error"))

    status, payload = save(env, class_id, answer_id, save_body(data))
    check("9.6 故障移除后同一请求保存成功（说明是注入而不是永久坏）",
          status == 200 and env.checks_count() == before + 1, "%s %s" % (status, env.checks_count()))

    other_class = env.add_class("七年级 2 班")
    status, payload = env.client.get(
        "/api/classes/%d/check/answers/%d" % (other_class, answer_id))
    check("9.7 跨班级读他人作答 → 404 not_found（班级归属校验）",
          status == 404 and payload.get("code") == "not_found",
          "%s %s" % (status, payload.get("code")))


# --------------------------------------------------------------------------
# 10. 统计口径：10 条造数人工复算
# --------------------------------------------------------------------------
def group_10_stats(envs):
    section("10. 统计口径：10 条造数（6 对 / 2 错 / 1 缺测 / 1 待核对）逐项复算")

    env = Env("stats")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    ids = {}
    for index in range(6):
        ids["ok%d" % index] = env.add_answer(
            class_id, student, "OK-%d" % index, CASE_OK, question_no=str(index + 1),
            student_name="学生甲")
    for index in range(2):
        ids["bad%d" % index] = env.add_answer(
            class_id, student, "BAD-%d" % index, CASE_A, question_no=str(index + 7),
            student_name="学生甲")
    ids["missing"] = env.add_answer(class_id, student, "MISS-1", CASE_C, question_no="9",
                                    missing=1, missing_reason="未提供学生步骤", student_name="学生甲")
    ids["pending"] = env.add_answer(class_id, student, "PEND-1", CASE_B, question_no="10",
                                    student_name="学生甲")

    for key in ["ok%d" % i for i in range(6)] + ["missing"]:
        save(env, class_id, ids[key], save_body(detail(env, class_id, ids[key])))
    data_bad0 = detail(env, class_id, ids["bad0"])
    first = data_bad0["candidates"][0]
    save(env, class_id, ids["bad0"], save_body(
        data_bad0, cause_state="decided", cause_code=first["code"], cause_label=first["label"]))
    save(env, class_id, ids["bad1"], save_body(detail(env, class_id, ids["bad1"])))

    # 人工复算：应交 10；缺测 1 → 有效 9；已保存 9 → 待核对 1；原题错误只数 first_bad=2；原因已判断=1
    expect = {"expected": 10, "valid": 9, "missing": 1, "pending": 1, "checked": 9,
              "original_errors": 2, "cause_decided": 1}
    log("人工复算：应交10 = 6对+2错+1缺测+1未核对；有效9 = 10-1缺测；已核对9；待核对1；"
        "原题错误2（只有 first_bad 计数）；原因已判断1（仅 bad0 采纳）")

    status, class_payload = env.client.get("/api/classes/%d" % class_id)
    block = (class_payload or {}).get("answer_check") or {}
    got = {key: block.get(key) for key in expect}
    check("10.1 GET /api/classes/{id} 的 answer_check 块七项与人工复算一致",
          status == 200 and got == expect, jdump(got))
    status, stats_payload = env.client.get("/api/classes/%d/stats" % class_id)
    block2 = (stats_payload or {}).get("answer_check") or {}
    got2 = {key: block2.get(key) for key in expect}
    check("10.2 GET /api/classes/{id}/stats 同一口径同一数字", status == 200 and got2 == expect,
          jdump(got2))
    check("10.3 basis 说明两套口径不同",
          block.get("basis") == "原题错误数按已保存错步计；原因判断数单独统计，两者口径不同",
          block.get("basis"))
    check("10.4 统计批次数=10", block.get("batch") == "9月第2次作业", block.get("batch"))

    # 反向自检：把「待核对那 1 份」也算进去，同一表达式就会变成 10，说明口径真的在数状态
    direct = env.db.scalar(
        "SELECT COUNT(*) FROM answers WHERE class_id=? AND batch='9月第2次作业'", (class_id,))
    check("10.5 断言非恒真自检：直接数作答=10，而 checked=9（两个数字不同，说明不是同一个计数）",
          direct == 10 and block.get("checked") == 9, "%s vs %s" % (direct, block.get("checked")))


# --------------------------------------------------------------------------
# 11. 追问写入条件
# --------------------------------------------------------------------------
def group_11_followup(envs):
    section("11. 追问：answer_text 为空不写记录；有回答则写入并关联 answer_id + answer_version")

    env = Env("followup")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    answer_id = env.add_answer(class_id, student, "FU-1", CASE_A, student_name="学生甲")
    data = detail(env, class_id, answer_id)
    question = data["followup"]["question"]

    status, _ = save(env, class_id, answer_id, save_body(
        data, followup={"question": question, "answer_text": "   "}))
    check("11.1 answer_text 全是空白 → 不写追问记录",
          status == 200 and env.followups_count() == 0, env.followups_count())

    status, _ = save(env, class_id, answer_id, save_body(data))
    check("11.2 请求里没有 followup 字段（相当于「暂时跳过」）→ 不写追问记录",
          status == 200 and env.followups_count() == 0, env.followups_count())

    quote = "2 只要乘 y 就好了。"
    status, _ = save(env, class_id, answer_id, save_body(
        data, followup={"question": question, "answer_text": quote}))
    rows = env.db.query("SELECT * FROM answer_followups")
    fp = env.db.scalar("SELECT fingerprint FROM answers WHERE id=?", (answer_id,))
    check("11.3 有回答 → 写入 1 条追问记录", status == 200 and len(rows) == 1, len(rows))
    check("11.4 追问挂到 answer_id + answer_version（当前版本）",
          rows and rows[0]["answer_id"] == answer_id and rows[0]["answer_version"] == fp,
          jdump(rows[0] if rows else None))
    check("11.5 追问关联本次 check，原话逐字保存",
          rows and rows[0]["check_id"] == env.db.scalar(
              "SELECT id FROM answer_checks WHERE answer_id=?", (answer_id,))
          and rows[0]["answer_text"] == quote, jdump(rows[0] if rows else None))
    check("11.6 预置问句记 source=preset",
          rows and rows[0]["question"] == question and rows[0]["source"] == "preset",
          rows[0]["source"] if rows else None)

    status, _ = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id),
        followup={"question": question, "answer_text": quote}))
    check("11.7 同一问句同一原话重复保存不重复插入（§3.7 第 5 条）",
          env.followups_count() == 1, env.followups_count())

    teacher_question = "你当时先算的是哪一步？"
    status, _ = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id),
        followup={"question": teacher_question, "answer_text": "先算括号外面的。"}))
    latest = env.db.query_one("SELECT * FROM answer_followups ORDER BY id DESC LIMIT 1")
    check("11.8 老师自己写的问句 → source=teacher",
          env.followups_count() == 2 and latest["source"] == "teacher"
          and latest["question"] == teacher_question, jdump(latest))
    detail_after = detail(env, class_id, answer_id)
    check("11.9 详情 followup.recorded=true 且回显最新原话",
          detail_after["followup"]["recorded"] is True
          and detail_after["followup"]["answer_text"] == "先算括号外面的。"
          and detail_after["followup"]["asked_at"], jdump(detail_after["followup"]))


# --------------------------------------------------------------------------
# 12. 断言非恒真：把 src/app 复制一份反向改错，同一断言必须失败
# --------------------------------------------------------------------------
def _copy_app(dest):
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "src", "app"), os.path.join(dest, "app"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def _patch(file_path, old, new):
    with open(file_path, encoding="utf-8") as fh:
        text = fh.read()
    count = text.count(old)
    if count != 1:
        return False
    with open(file_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.replace(old, new))
    return True


def _child_env(extra=None):
    """子进程统一用 UTF-8 输出：本机管道编码是 cp936，不指定会把中文日志读成乱码。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    env.pop("MISTAKE_FAULT", None)
    env.update(extra or {})
    return env


def _run_mutant(src_root, code):
    proc = subprocess.run([PYTHON, "-c", code], capture_output=True, timeout=180,
                          env=_child_env())
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip(), \
        proc.stderr.decode("utf-8", "replace")[-300:]


def group_12_mutants(envs, queue_env, queue_class_id):
    section("12. 断言非恒真：把实现反向改错后，同一断言必须失败（4 个变异体）")

    base = os.path.join(WORK, "mutants")

    # 变异体 1：hint_text 直接不给（案例A 的「逐字划线」断言应从 PASS 变 FAIL）
    root = _copy_app(os.path.join(base, "m1"))
    patched = _patch(os.path.join(root, "app", "equation_steps.py"),
                     '        "hint_text": hint,', '        "hint_text": None,')
    check("12.1 变异体1 补丁命中唯一位置", patched)
    code = ("import json,sys;sys.path.insert(0,%r);from app import equation_steps as E;"
            "r=E.locate('3(x-2)=12','3(x-2)=12;3x-6=12;3x=18;x=6','3(x-2)=12\\n3x-2=12\\nx=14/3');"
            "print(json.dumps([r['status'],r['hint_text'],r['underline']]))" % root)
    rc, out, err = _run_mutant(root, code)
    check("12.2 变异体1 让 hint_text 变空 → 第 1.3/2.4 条断言不再成立",
          rc == 0 and out == '["first_bad", null, {"start": 2, "end": 4}]',
          "%s %s" % (out, err))
    real = E.locate(CASE_A["question"], CASE_A["standard"], CASE_A["steps"])
    check("12.3 对照：真实实现 hint_text 非空（同一断言在原实现上成立）",
          real["hint_text"] == "-2" and real["hint_text"] in real["first_bad_text"])

    # 变异体 2：不再比较相邻两行 → status 应从 first_bad 变 correct
    root = _copy_app(os.path.join(base, "m2"))
    patched = _patch(os.path.join(root, "app", "equation_steps.py"),
                     "        if not _same_solution(chain[index - 1], chain[index]):",
                     "        if False:")
    check("12.4 变异体2 补丁命中唯一位置", patched)
    code = ("import json,sys;sys.path.insert(0,%r);from app import equation_steps as E;"
            "r=E.locate('3(x-2)=12','3(x-2)=12;3x-6=12;3x=18;x=6','3(x-2)=12\\n3x-2=12\\nx=14/3');"
            "print(json.dumps([r['status'],r['first_bad_index']]))" % root)
    rc, out, err = _run_mutant(root, code)
    check("12.5 变异体2 让案例A 变成 correct → 第 1.1/1.2/2.2 条断言不再成立",
          rc == 0 and out == '["correct", null]', "%s %s" % (out, err))

    # 变异体 3：缺测/正确/只有答案不再强制待判断
    root = _copy_app(os.path.join(base, "m3"))
    patched = _patch(os.path.join(root, "app", "answer_check.py"),
                     '    return _missing(answer) or status in ("correct", "missing_steps", "out_of_scope")',
                     "    return False")
    check("12.6 变异体3 补丁命中唯一位置", patched)
    code = ("import json,sys;sys.path.insert(0,%r);from app import answer_check as C;"
            "print(json.dumps([C._force_undecided({'missing':0},'correct'),"
            "C._force_undecided({'missing':0},'missing_steps'),"
            "C._force_undecided({'missing':1},'first_bad')]))" % root)
    rc, out, err = _run_mutant(root, code)
    check("12.7 变异体3 让「正确/只有答案/缺测强制待判断」失效 → 第 7.1 条断言不再成立",
          rc == 0 and out == "[false, false, false]", "%s %s" % (out, err))
    check("12.8 对照：真实实现三种情形都强制待判断",
          C._force_undecided({"missing": 0}, "correct") is True
          and C._force_undecided({"missing": 0}, "missing_steps") is True
          and C._force_undecided({"missing": 1}, "first_bad") is True)

    # 变异体 4：已核对数恒为 0（带真实库，证明队列/统计断言不是恒真）
    root = _copy_app(os.path.join(base, "m4"))
    patched = _patch(os.path.join(root, "app", "answer_check.py"),
                     '    checked = int(row["checked"] or 0)', "    checked = 0")
    check("12.9 变异体4 补丁命中唯一位置", patched)
    data_dir = os.path.join(base, "m4data")
    shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir)
    src_conn = sqlite3.connect(os.path.join(queue_env.dir, "app.db"))
    dst_conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    with dst_conn:
        src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()
    code = (
        "import json,sys;sys.path.insert(0,%r);"
        "from app.db import Database;from app import answer_check as C;"
        "db=Database(%r,create=False);"
        "cls=db.query_one('SELECT * FROM classes WHERE id=?',(%d,));"
        "ov=C.overview(db,cls);"
        "print(json.dumps([ov['assignment']['pending'],ov['assignment']['checked'],"
        "ov['state']]))"
    ) % (root, os.path.join(data_dir, "app.db"), queue_class_id)
    rc, out, err = _run_mutant(root, code)
    check("12.10 变异体4 让「已核对数」恒 0 → 队列待核对数/状态断言不再成立",
          rc == 0 and out == "[3, 0, \"has_pending\"]", "%s %s" % (out, err))
    real_ov = overview(queue_env, queue_class_id)
    check("12.11 对照：真实实现在同一份库上是 [0, 3, all_checked]（已全部核对）",
          real_ov["assignment"]["pending"] == 0 and real_ov["assignment"]["checked"] == 3
          and real_ov["state"] == "all_checked", jdump(real_ov["assignment"]))


# --------------------------------------------------------------------------
# 13. 防漂移不变式：预置定位必须与规则引擎逐字段一致
# --------------------------------------------------------------------------
LOCATE_FIELDS = ("status", "first_bad_index", "first_bad_text", "underline", "hint_text")


def group_13_demo_rule_invariant():
    section("13. 防漂移不变式：demo_answers 预置 locate 与 equation_steps.locate 逐字段相等")

    drift = []
    checked = 0
    for record in D.build()["answers"]:
        preset = record.get("locate")
        if not isinstance(preset, dict):
            continue
        checked += 1
        rule = E.locate(record["question_text"], record["standard_solution"],
                        record["student_steps"], missing=bool(record.get("missing")))
        diff = {key: [preset.get(key), rule.get(key)] for key in LOCATE_FIELDS
                if preset.get(key) != rule.get(key)}
        if preset.get("contrast") != rule.get("contrast"):
            diff["contrast"] = [preset.get("contrast"), rule.get("contrast")]
        if diff:
            drift.append((record["record_key"], diff))

    check("13.1 demo build() 里带 locate 的作答都被复核到（至少 4 份）", checked >= 4, checked)
    if drift:
        # 这里绝不放宽成「note 非空」：同一题两套说法就是 A2 不成立。
        for key, diff in drift:
            log("  漂移 %s：%s" % (key, jdump(diff)))
        hold("13.2 预置 locate 与规则引擎逐字段一致（%d 份漂移：%s）"
             % (len(drift), ", ".join(item[0] for item in drift)),
             "等 T9 让 demo_answers 的预置 locate 与规则引擎一致")
    else:
        check("13.2 预置 locate 与规则引擎逐字段一致（%d 份）" % checked, True)

    # 演示批次同题的真实导入作答走 rule，两边在界面上说法一致才不算「演示一套、真实一套」
    preset_a = next(item for item in D.build()["answers"] if item["record_key"] == "DEMO-A-01")
    rule_a = E.locate(preset_a["question_text"], preset_a["standard_solution"],
                      preset_a["student_steps"])
    check("13.3 案例A：预置与规则除了 source 之外完全一致",
          preset_a["locate"]["status"] == rule_a["status"]
          and preset_a["locate"]["first_bad_index"] == rule_a["first_bad_index"]
          and preset_a["locate"]["hint_text"] == rule_a["hint_text"]
          and preset_a["locate"]["underline"] == rule_a["underline"]
          and preset_a["locate"]["contrast"] == rule_a["contrast"],
          jdump([preset_a["locate"]["contrast"], rule_a["contrast"]]))


# --------------------------------------------------------------------------
# 14. 回归三条线
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 15. 合成演示数据：seed / 幂等 / reset（R-05，lead 追加）
# --------------------------------------------------------------------------
def group_15_demo_seed_reset(envs):
    section("15. 合成演示数据：seed → 再 seed（幂等）→ reset；真实导入一律不动（R-05）")

    env = Env("demo-reset")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    real_id = env.add_answer(class_id, student, "REAL-001", CASE_A, student_name="学生甲")
    status, _ = save(env, class_id, real_id, save_body(detail(env, class_id, real_id)))
    check("15.1 先造一条真实导入作答并保存判断（用于验证 reset 不动它）",
          status == 200 and env.checks_count() == 1, status)

    status, first = env.client.post("/api/classes/%d/check/demo/seed" % class_id)
    seeded = first.get("seeded") or {}
    check("15.2 首次 seed 写入 4 份合成作答",
          status == 200 and seeded.get("answers") == 4, jdump(first))

    status, second = env.client.post("/api/classes/%d/check/demo/seed" % class_id)
    seeded2 = second.get("seeded") or {}
    total = env.db.scalar("SELECT COUNT(*) FROM answers WHERE class_id=?", (class_id,))
    check("15.3 再次 seed 幂等：新增 0、跳过已存在 4、总数仍 5",
          status == 200 and seeded2.get("answers") == 0
          and seeded2.get("skipped_existing") == 4 and total == 5,
          jdump([seeded2, total]))

    status, reset = env.client.post("/api/classes/%d/check/demo/reset" % class_id)
    deleted = reset.get("deleted") or {}
    kept = reset.get("kept") or {}
    left = env.db.query("SELECT record_key, source FROM answers WHERE class_id=?", (class_id,))
    synthetic = env.db.scalar(
        "SELECT COUNT(*) FROM answers WHERE class_id=? AND source='合成演示'", (class_id,))
    check("15.4 reset 删除 4 份合成作答、kept.imported_answers=1",
          status == 200 and deleted.get("answers") == 4
          and kept.get("imported_answers") == 1, jdump(reset))
    check("15.5 reset 后 source='合成演示' 归零，只剩真实导入那一条",
          synthetic == 0 and len(left) == 1 and left[0]["record_key"] == "REAL-001"
          and left[0]["source"] == "真实导入", jdump(left))
    check("15.6 reset 不动真实作答的判断（check 仍在、详情仍显示已核对）",
          env.checks_count() == 1
          and detail(env, class_id, real_id)["judgement"]["progress"] == "checked",
          env.checks_count())


def group_14_regression():
    section("14. 三条既有回归线（如实记录，含已知失败）")

    lines = [
        ("verify_answers_150.py", 60, 60, 0),
        ("verify_followup_180.py", 126, 126, 0),
        ("verify_review_160.py", 135, 134, 1),
    ]
    for index, (name, total, passed, failed) in enumerate(lines, start=1):
        started = time.time()
        proc = subprocess.run([PYTHON, os.path.join(ROOT, "tests", name)],
                              capture_output=True, cwd=ROOT, timeout=1200,
                              env=_child_env())
        text = proc.stdout.decode("utf-8", "replace")
        summary = ""
        for line in text.splitlines():
            if line.startswith("合计"):
                summary = line.strip()
        ok = ("合计 %d 项，通过 %d，失败 %d" % (total, passed, failed)) in summary
        log("回归 %s：退出码 %s，用时 %.1fs，%s" % (name, proc.returncode, time.time() - started,
                                              summary or "（没有解析到合计行）"))
        if not summary:
            log("  子进程 stderr（末 600 字）：%s"
                % proc.stderr.decode("utf-8", "replace")[-600:].replace("\n", " / "))
        check("14.%d %s 合计 %d 项、通过 %d、失败 %d（%s）"
              % (index, name, total, passed, failed,
                 "已知失败：FAIL 1.8.0 版本号 1.8.1" if failed else "无失败"),
              ok, summary)
        if name == "verify_review_160.py":
            check("14.4 verify_review_160.py 的唯一失败是已知的历史版本号断言",
                  "FAIL 1.8.0 版本号 1.8.1" in text
                  and text.count("FAIL ") == 1, text.count("FAIL "))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    log("数智伴学｜作答核对与追问（第一轮 A1–A7）源码级验收")
    for line in environment_lines():
        log(line)
    log("临时库：%s" % WORK)
    log("网络：只用 127.0.0.1 随机端口环回，无任何外部请求")
    try:
        import hashlib
        for name in ("src/web/app.js", "src/web/styles.css", "src/app/equation_steps.py",
                     "src/app/demo_answers.py", "src/app/answer_check.py", "src/app/stats.py",
                     "src/app/api.py"):
            path = os.path.join(ROOT, name)
            with open(path, "rb") as fh:
                log("被测文件 %s sha256=%s mtime=%s size=%d" % (
                    name, hashlib.sha256(fh.read()).hexdigest()[:12].upper(),
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
                    os.path.getsize(path)))
    except OSError as exc:
        log("（读取被测文件指纹失败：%s）" % exc)

    envs = []
    try:
        group_1_locate()
        case_env, case_class = group_2_case_a_and_preset(envs)
        group_3_determinism()
        group_4_two_axes(envs)
        queue_env, queue_class, queue_ids = group_5_queue(envs)
        group_6_rejudge(envs)
        group_7_no_mistake(envs)
        group_8_stale(envs)
        group_9_failures(envs)
        group_10_stats(envs)
        group_11_followup(envs)
        group_12_mutants(envs, queue_env, queue_class)
        group_13_demo_rule_invariant()
        group_15_demo_seed_reset(envs)
        group_14_regression()
    finally:
        for env in ENVS:
            try:
                env.close()
            except Exception:
                pass

    log("")
    log("合计 %d 项，通过 %d，失败 %d（另：未验证 %d 项）"
        % (RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"], RESULTS["fail"], RESULTS["hold"]))
    if RESULTS["hold"]:
        log("未验证项见上文「未验证」行：没有拿到结论的项不记通过，等对应成员修复后重跑复核。")
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
