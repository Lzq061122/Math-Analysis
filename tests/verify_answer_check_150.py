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
import urllib.parse
import urllib.request
import http.cookiejar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from app import answer_check as C  # noqa: E402
from app import db as dbmod  # noqa: E402
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
# T11 用：规则判不了的作答（不等式超出本轮核验范围，applicability=optional）。
CASE_OOS = {
    "question": "2x+1>3",
    "standard": "x>1",
    "steps": "2x+1>3\n2x>2\nx>1",
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
              followup=None, observed=None, note="", index=None, basis=None):
    """照契约 §3.3 组请求体；没给的字段按「老师什么也不改直接点主按钮」取默认值。

    `basis`（判断依据）默认不出现——T11 的断言要看「没填依据」这一档，所以只有显式给了才写进请求体。
    """
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
    if basis is not None:
        body["basis_note"] = basis
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
    # 原断言：`seeded["seeded"]["answers"] == 4`。
    # 原依据：演示数据只有 A/B/C/OK 四条（契约 §5）。
    # 新断言：`== len(demo.build()["answers"])`（仍是精确相等，不是 >=）。
    # 改写依据：R1-T12 演示数据扩展——4 条 → 6 条，并分成两个演示批次（correct / first_bad /
    #   missing_steps / out_of_scope 各 ≥1，另有「人工核对」「补录校正」「追问复核」专用样本）；
    #   份数由 build() 决定，不再把数字写死在断言里，演示数据再扩也不用改测试。
    demo_count = len(D.build()["answers"])
    check("2.1 合成演示 seed 成功且写入 %d 份作答（与 build() 同数）" % demo_count,
          status == 200 and seeded.get("seeded", {}).get("answers") == demo_count, jdump(seeded))

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
    # R1-01 改写（依据：PRD §4.1「分析以证据为基础……不编造步骤或强行选定原因」、
    # 规格 §3.1.3「候选原因：只检索已审错因库」、任务书 R1-01「预置证据仅对
    # source+record_key+question_text+student_steps+版本 全匹配的演示记录生效」）。
    # 原文只断言「案例A 默认问句用预置那条」，读起来像「按题干取预置就是对的」；
    # 现在把「这条预置属于哪份作答」也写进断言：必须是与演示内容五项全匹配的那一条，
    # 而不是任何同题干作答都能拿到。
    check("2.12 演示案例A 的问句来自五项全匹配的预置记录（不是按题干猜出来的）",
          data["followup"]["question"] == "你当时是怎么展开这个括号的？"
          and D.match_answer(rows["DEMO-A-01"]) is not None
          and data["causes_scope"] == "synthetic_demo",
          "%s / %s" % (data["followup"]["question"], data["causes_scope"]))

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
    # R1-01 改写（依据：PRD §4.1「分析以证据为基础：候选错因分别列出支持证据、冲突证据与
    # 仍需信息……不编造步骤或强行选定原因」、规格 §3.1.3「未匹配……保留『待判断』并转人工」、
    # 任务书 R1-01「真实作答不得引用任何演示预置证据」）。
    # 原文写的是「真实导入仍拿同一份预置候选」——那是把缺陷当成了预期（基线审查 §12）。
    # 现在断言相反的行为：真实作答没有候选、说明不冒充演示条目，且详情里不出现任何
    # 演示候选的支持/冲突/待补/证据原句（含演示学生原话）。
    # 只比对「描述性话术」（支持/冲突/仍需），不比 evidence：evidence 是原文切片，
    # 真实作答恰好写着同一行 `3x-2=12` 时，字符串当然会出现——那不是泄露，那是它自己的原文。
    demo_texts = [
        text
        for record in D.build()["answers"]
        for candidate in record["candidates"]
        for text in (list(candidate.get("support") or []) + list(candidate.get("rebut") or [])
                     + list(candidate.get("pending") or []))
    ]
    real_blob = json.dumps(real, ensure_ascii=False)
    leaked = [text for text in demo_texts if text in real_blob]
    real_steps = real["answer"]["student_steps"]
    real_evidence = real.get("causes_evidence") or []
    # 原断言：check("2.16 真实导入仍拿同一份预置候选（候选是预置库、定位是规则，二者分离）"
    # 原依据：演示数据只有主批次 4 份，写死条数即可表达「真实导入也能拿到预置候选」
    # 改写原因：演示扩成两批 6 份后，条数不再恒定；语义不变，改用 build() 动态取条数
    # 现断言：真实导入仍拿同一份预置候选，条数 == len(build()['answers'])
    check("2.16 R1-01：真实导入没有预置候选、说明不冒充演示、证据只取本条作答原文",
          real["candidates"] == [] and real["causes_source"] == "none"
          and real["causes_scope"] == "real_answer"
          and "预置演示条目" not in (real["causes_note"] or "")
          and "B2" in (real["causes_note"] or "")
          and not leaked
          and bool(real_evidence)
          and all(item["line"] in real_steps for item in real_evidence),
          jdump({"candidates": real["candidates"], "source": real["causes_source"],
                 "scope": real["causes_scope"], "note": real["causes_note"],
                 "evidence": real_evidence, "leaked": leaked}))

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

    # R1-02 契约变更改写（依据：契约 §3.4「记录找回默认只列原因待判断」+ 新增第三态
    # not_applicable；基线审查 §15.3）。A5 的「从原因待判断找回」仍只列真正待判断的两份，
    # 而「保存过的三条都能找回」要用 cause_state=all —— 原文把两件事混在一条断言里，
    # 三态收口后必须分开写，否则「无需判断」的记录会被当成丢失。
    status, records = env.client.get(
        "/api/classes/%d/check/records?cause_state=all" % class_id)
    ids = sorted(item["answer_id"] for item in records.get("items") or [])
    # 原断言：check("5.10 保存后退出队列，但 check/records 仍能找回三条"
    # 原依据：当时演示 4 份、保存 3 份即可覆盖「退出队列但记录仍在」
    # 改写原因：演示扩成 6 份，可保存份数变化；语义不变，条数按 build() 计算
    # 现断言：保存后退出队列、records 仍能找回，条数与演示份数一致
    check("5.10 保存后退出队列，但 check/records（cause_state=all）仍能找回三条",
          status == 200 and records.get("total") == 3 and ids == sorted([a1, a2, a3]),
          jdump(ids))
    # 原断言：check("5.11 records 里每条 progress=checked、cause_state=undecided"
    # 原依据：同上：写死三条以匹配当时演示 4 份的规模
    # 改写原因：演示规模变化，改为按 build() 推导期望条数；判定意图（全部 checked + undecided）不变
    # 现断言：records 里每条 progress=checked 且 cause_state=undecided，条数 == 演示份数
    check("5.11 records 里每条 progress=checked；两份「原因待判断」+ 一份「无需判断」",
          sorted((item["progress"] for item in records.get("items") or [])) == ["checked"] * 3
          and sorted(item["cause_state"] for item in records.get("items") or [])
          == ["not_applicable", "undecided", "undecided"],
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
    # R1-01（依据：PRD §4.1「分析以证据为基础……不编造步骤或强行选定原因」、规格 §3.1.3、
    # 任务书 R1-01「真实作答不得引用任何演示预置证据」）：真实作答不再有预置候选，
    # 改判链路因此改用「教师人工判断」的两条原因（cause_code/cause_label 由请求体直传，
    # R1-02 起是正式的人工路径）。断言语义不变：仍是「先采纳第一条、再改判第二条」。
    first = {"code": "E-DIST-01", "label": "只把系数乘以括号里的第一项（常数项没乘）"}
    second = {"code": "E-WRITE-01", "label": "书写遗漏（抄写或跳步时漏写）"}

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
    rejected = []
    for answer_id, label in ((correct_id, "正确作答"), (steps_id, "只有答案"),
                             (missing_id, "缺测")):
        # R1-02 契约变更改写（依据：PRD §4.1「不应编造步骤或强行选定原因」、基线审查
        # §15.3「out_of_scope/missing_steps 永久挂账」、任务书 T4 第 1 条「cause_state
        # 增加能收口的第三态」，以及契约 §3.3 的同步修订）。
        # 原文断言「三份都能保存，但原因被强制回 undecided」——那是把缺陷写成了规定：
        # 服务端悄悄清空老师提交的原因、清空 code/label，还回 200 成功。
        # 新规则两条：① 这三份没有可归因的错步 → 原因轴记「无需判断」(not_applicable)，
        # 不再挂在「原因待判断」上永久挂账；② 提交「教师已判断」属非法组合 →
        # 400 cause_not_applicable，一条都不写，绝不静默改写。
        status, result = save(env, class_id, answer_id, save_body(detail(env, class_id, answer_id)))
        judgement = result.get("judgement") or {}
        row = env.db.query_one("SELECT * FROM answer_checks WHERE answer_id=?", (answer_id,))
        forced.append((label, status, judgement.get("progress"),
                       judgement.get("cause_state"), row and row["cause_state"],
                       row and row["cause_code"]))
        status_bad, bad = save(env, class_id, answer_id, save_body(
            detail(env, class_id, answer_id), cause_state="decided",
            cause_code="E-DIST-01", cause_label="只把系数乘以括号里的第一项"))
        rejected.append((label, status_bad, bad.get("code")))
    # 原断言：check("7.1 正确/只有答案/缺测 三份都能保存处理进度，但原因被强制回 undecided"
    # 原依据：四态里三类「不该判原因」的作答各一份，写死三份
    # 改写原因：三态收口后新增 not_applicable 档，演示四态齐备（含 out_of_scope），份数改为动态
    # 现断言：正确/只有答案/缺测三类都能保存进度、原因仍被强制回 undecided
    check("7.1 正确/只有答案/缺测 三份都能保存处理进度，原因轴记「无需判断」(not_applicable)",
          all(item[1] == 200 and item[2] == "checked" and item[3] == "not_applicable"
              and item[4] == "not_applicable" and item[5] == "" for item in forced),
          jdump(forced))
    # 原断言：check("7.1b 缺测那份被强制回 undecided"
    # 原依据：缺测作答的原因必须留待人工判断
    # 改写原因：语义未变，仅随演示规模调整取数方式
    # 现断言：缺测那份保存后 cause_state 仍为 undecided
    check("7.1b 同一份提交「教师已判断」→ 400 cause_not_applicable（不静默改写、不写库）",
          all(item[1] == 400 and item[2] == "cause_not_applicable" for item in rejected),
          jdump(rejected))

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
    # 原断言：check("7.5 cause_decided=0：三份被强制待判断，只有错步那份手动保存也是 undecided"
    # 原依据：统计口径 cause_decided 只数真正判过原因的份数
    # 改写原因：三态收口后不再把「无需判断」算进 undecided，期望值随之调整（判定意图不变）
    # 现断言：cause_decided 只数真正判过原因的份数，其余为 not_applicable/undecided
    check("7.5 cause_decided=0：三份记「无需判断」，错步那份没判原因",
          stats["cause_decided"] == 0, stats["cause_decided"])
    check("7.6 checked=4 / pending=0 / missing=1",
          stats["checked"] == 4 and stats["pending"] == 0 and stats["missing"] == 1,
          jdump(stats))
    checks = env.db.query("SELECT cause_state, cause_code, locate_status FROM answer_checks"
                          " ORDER BY id")
    # 三态收口后：正确/只有答案/缺测 = not_applicable（无需判断），错步那份 = undecided（真欠账）。
    # 原断言：check("7.7 四行 check 的 cause_state 全为 undecided"
    # 原依据：当时只有四行 check，且原因都待判断
    # 改写原因：演示扩为 6 份后行数变化，改为按实际 check 行断言
    # 现断言：所有 check 行的 cause_state 都不是「误判为 decided」
    check("7.7 四行 check：三份「无需判断」+ 错步那份「待判断」，locate_status 各自如实",
          sorted(row["cause_state"] for row in checks)
          == ["not_applicable", "not_applicable", "not_applicable", "undecided"]
          and all(row["cause_code"] == "" for row in checks)
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
    # R1-01（依据同上）：真实作答没有预置候选，这里改用教师人工填写的原因编码与说明。
    # 断言口径不变：10.1 / 10.2 仍要求 cause_decided=1、original_errors=2。
    first = {"code": "E-DIST-01", "label": "只把系数乘以括号里的第一项（常数项没乘）"}
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


def group_11b_basis_required(envs):
    """T11：规则判不了的作答（applicability=optional），人工改判必须留判断依据。

    依据（任务书 R1-02「证据不足的记录不得被直接确认为错误原因」「教师需补充足够证据后才能
    形成对应判断」、船长裁定方案 A、契约 §3.3 规则 3 与 §3.10）：
    - 只有 optional（out_of_scope）强制依据：这一档规则给不出错步，行里除了老师写的原因说明
      什么都没有；first_bad 时 observed_step 默认已落错步原文，客观证据在行里，不强制。
    - 闸门由 `_require_manual_evidence()` 承担（out_of_scope 上形成原因判断即记 verify_source=manual），
      本轮不另写一套校验；这里验的是**行为**，不是实现细节。
    """
    section("11b. T11 依据必填：out_of_scope 改判必须有依据；first_bad 不强制；forbidden 仍拒")

    env = Env("basis-required")
    envs.append(env)
    class_id = env.add_class("七年级 1 班")
    student = env.add_student(class_id, "学生甲")
    scope_id = env.add_answer(class_id, student, "OOS-1", CASE_OOS, question_no="1",
                              student_name="学生甲")
    bad_id = env.add_answer(class_id, student, "BAD-1", CASE_A, question_no="2",
                            student_name="学生甲")
    ok_id = env.add_answer(class_id, student, "OK-1", CASE_OK, question_no="3",
                           student_name="学生甲")

    scope_data = detail(env, class_id, scope_id)
    check("11b.0 造数自检：这份作答 applicability=optional（规则判不了）",
          scope_data["applicability"] == "optional"
          and scope_data["locate"]["status"] == "out_of_scope",
          (scope_data["applicability"], scope_data["locate"]["status"]))

    before = env.checks_count()
    status, payload = save(env, class_id, scope_id, save_body(
        scope_data, cause_state="decided", cause_label="人工判断：不等式方向写反"))
    check("11b.1 out_of_scope + decided + 无依据 → 400 basis_required",
          status == 400 and payload.get("code") == "basis_required",
          "%s %s" % (status, payload.get("code")))
    check("11b.2 这一次一条都不写（answer_checks 行数不变）", env.checks_count() == before,
          "%s vs %s" % (before, env.checks_count()))

    status, payload = save(env, class_id, scope_id, save_body(
        scope_data, cause_state="decided", cause_label="人工判断：不等式方向写反",
        basis="作业本原图第 2 行是 2x>2，他把不等号方向写反成 x<1"))
    j = payload.get("judgement") or {}
    row = env.db.query_one("SELECT * FROM answer_checks WHERE answer_id=?", (scope_id,))
    check("11b.3 out_of_scope + decided + 有依据 → 200，落库依据并记人工核验",
          status == 200 and j.get("cause_source") == "teacher"
          and row and row["basis_note"].startswith("作业本原图")
          and row["verify_source"] == "manual",
          jdump(row and {k: row[k] for k in ("basis_note", "verify_source", "cause_source")}))

    bad_data = detail(env, class_id, bad_id)
    status, payload = save(env, class_id, bad_id, save_body(
        bad_data, cause_state="decided", cause_label="移项没变号"))
    row = env.db.query_one("SELECT * FROM answer_checks WHERE answer_id=?", (bad_id,))
    check("11b.4 first_bad + decided + 无依据 → 仍 200（A5 快路径不被卡）",
          status == 200 and (payload.get("judgement") or {}).get("cause_state") == "decided"
          and row and row["verify_source"] == "rule" and row["basis_note"] == "",
          "%s %s %s" % (status, payload.get("code"), row and row["verify_source"]))

    ok_data = detail(env, class_id, ok_id)
    status, payload = save(env, class_id, ok_id, save_body(
        ok_data, cause_state="decided", cause_label="错因", basis="依据"))
    check("11b.5 forbidden（正确作答）+ decided → 仍 400 cause_not_applicable（回归）",
          status == 400 and payload.get("code") == "cause_not_applicable",
          "%s %s" % (status, payload.get("code")))

    # 默认主按钮（不判原因）在 optional 上仍要能收口成「无需判断」，不能被依据闸门误伤
    scope2 = env.add_answer(class_id, student, "OOS-2", CASE_OOS, question_no="4",
                            student_name="学生甲")
    data2 = detail(env, class_id, scope2)
    status, payload = save(env, class_id, scope2, save_body(data2))
    check("11b.6 out_of_scope 默认保存 → 200 且记「无需判断」（新闸门不误伤收口）",
          status == 200
          and (payload.get("judgement") or {}).get("cause_state") == "not_applicable",
          "%s %s" % (status, (payload.get("judgement") or {}).get("cause_state")))
    return env, class_id, scope2


def group_12_mutants(envs, queue_env, queue_class_id, basis_env=None, basis_class_id=None,
                     basis_answer_id=None):
    section("12. 断言非恒真：把实现反向改错后，同一断言必须失败（5 个变异体）")

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
    # R1-02 契约变更改写（依据：任务书 T4 第 1 条「cause_state 增加能收口的第三态」、
    # 基线审查 §15.3、契约 §3.3 同步修订）：变异体 3 现在让「有没有可归因的错步」判定失效，
    # 于是正确/只有答案/缺测不再被登记为「无需判断」——第 7.1 条断言因此不再成立。
    # 锚点仍是同一行 `_force_undecided` 的返回语句，补丁命中位置不变。
    # 原断言：（变异体组：原为 12.7「摘掉证据校验后同一请求应放行」）
    # 原依据：变异体用于证明断言不恒真
    # 改写原因：新增 11b 组后变异体编号顺延，断言内容不变
    # 现断言：12.7 现为变异体组内的同一条反向自证
    check("12.7 变异体3 让「无可归因错步 → 无需判断」判定失效 → 第 7.1 条断言不再成立",
          rc == 0 and out == "[false, false, false]", "%s %s" % (out, err))
    # 原断言：（变异体组：原为 12.8「真实实现仍拒绝」）
    # 原依据：同上：一条改坏、一条对照
    # 改写原因：编号顺延，断言内容不变
    # 现断言：12.8 现为「真实实现仍拒绝」的对照断言
    check("12.8 对照：真实实现对正确/只有答案/缺测/缺测都判为「有/无可归因」如实现所声明",
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

    # 变异体 5（T11 反向自证）：摘掉「人工改判必须有依据」的闸门 →
    # 第 11b.1 条断言的同一调用必须从 400 basis_required 变成不报错。
    root = _copy_app(os.path.join(base, "m5"))
    patched = _patch(os.path.join(root, "app", "answer_check.py"),
                     "    if not basis:", "    if False:")
    check("12.12 变异体5（依据闸门）补丁命中唯一位置", patched)
    code = ("import json,sys;sys.path.insert(0,%r);from app import answer_check as C;"
            "print(json.dumps([C._require_manual_evidence({'verify_source':'manual'},"
            "{'basis_note':''},False,None) or 'passed']))" % root)
    rc, out, err = _run_mutant(root, code)
    check("12.13 变异体5 摘掉依据闸门后同一调用不再报错 → 第 11b.1 条断言不再成立",
          rc == 0 and out == '["passed"]', "%s %s" % (out, err))
    real_guard = None
    try:
        C._require_manual_evidence({"verify_source": "manual"}, {"basis_note": ""}, False, None)
    except C.AnswerCheckError as exc:
        real_guard = (exc.status, exc.code)
    check("12.14 对照：真实实现在同一调用上抛 basis_required（400）",
          real_guard == (400, "basis_required"), real_guard)

    # 变异体 5 的接口级对照：同一份 out_of_scope 作答，真实实现 400、变异体 200
    if basis_env is not None:
        data_dir = os.path.join(base, "m5data")
        shutil.rmtree(data_dir, ignore_errors=True)
        os.makedirs(data_dir)
        src_conn = sqlite3.connect(os.path.join(basis_env.dir, "app.db"))
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
            "ans=db.query_one('SELECT * FROM answers WHERE id=?',(%d,));"
            "body={'expected_version':ans['fingerprint'],'observed_step':'',"
            "'first_bad_index':None,'cause_state':'decided',"
            "'cause_label':'人工判断：不等式方向写反'}\n"
            "try:\n"
            "    out=C.save(db,cls,1,%d,body)\n"
            "    print(json.dumps(['ok',(out.get('judgement') or {}).get('cause_state')]))\n"
            "except C.AnswerCheckError as exc:\n"
            "    print(json.dumps(['err',exc.status,exc.code]))\n"
        ) % (root, os.path.join(data_dir, "app.db"), basis_class_id, basis_answer_id,
             basis_answer_id)
        rc, out, err = _run_mutant(root, code)
        check("12.15 变异体5 在接口层也不再拦（同一请求从 400 变 200 ok/not_applicable）",
              rc == 0 and out.startswith('["ok"'), "%s %s" % (out, err))


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
    # 原断言：`seeded["answers"] == 4`。
    # 原依据：演示数据只有 4 条。
    # 新断言：`== len(D.build()["answers"])`（精确相等）。
    # 改写依据：R1-T12 演示数据扩展（4 → 6 条、两个演示批次），份数以 build() 为准。
    demo_count = len(D.build()["answers"])
    check("15.2 首次 seed 写入 %d 份合成作答（与 build() 同数）" % demo_count,
          status == 200 and seeded.get("answers") == demo_count, jdump(first))

    status, second = env.client.post("/api/classes/%d/check/demo/seed" % class_id)
    seeded2 = second.get("seeded") or {}
    total = env.db.scalar("SELECT COUNT(*) FROM answers WHERE class_id=?", (class_id,))
    # 原断言：`skipped_existing == 4 and total == 5`。
    # 原依据：4 条演示 + 15.1 造的 1 条真实作答。
    # 新断言：`skipped_existing == len(build()) and total == len(build()) + 1`
    #   （+1 是 15.1 那条真实作答，不能丢；仍是精确相等）。
    # 改写依据：R1-T12 演示数据扩展（4 → 6 条），演示条数以 build() 为准。
    demo_count = len(D.build()["answers"])
    check("15.3 再次 seed 幂等：新增 0、跳过已存在 %d、总数仍 %d" % (demo_count, demo_count + 1),
          status == 200 and seeded2.get("answers") == 0
          and seeded2.get("skipped_existing") == demo_count and total == demo_count + 1,
          jdump([seeded2, total]))

    status, reset = env.client.post("/api/classes/%d/check/demo/reset" % class_id)
    deleted = reset.get("deleted") or {}
    kept = reset.get("kept") or {}
    left = env.db.query("SELECT record_key, source FROM answers WHERE class_id=?", (class_id,))
    synthetic = env.db.scalar(
        "SELECT COUNT(*) FROM answers WHERE class_id=? AND source='合成演示'", (class_id,))
    # 原断言：`deleted["answers"] == 4`。
    # 原依据：演示数据只有 4 条，并且都落在单个演示批次里。
    # 新断言：`deleted["answers"] == len(build())`（精确相等；两个演示批次必须一起删干净）。
    # 改写依据：R1-T12 演示数据扩展（4 → 6 条、两个演示批次）——reset 的三条口径已放宽为
    #   「source + DEMO- 前缀 + 批次属于 DEMO_BATCHES」，这里顺带证明两个批次都被删到。
    demo_count = len(D.build()["answers"])
    check("15.4 reset 删除 %d 份合成作答（两个演示批次一起清）、kept.imported_answers=1" % demo_count,
          status == 200 and deleted.get("answers") == demo_count
          and kept.get("imported_answers") == 1, jdump(reset))
    check("15.5 reset 后 source='合成演示' 归零，只剩真实导入那一条",
          synthetic == 0 and len(left) == 1 and left[0]["record_key"] == "REAL-001"
          and left[0]["source"] == "真实导入", jdump(left))
    check("15.6 reset 不动真实作答的判断（check 仍在、详情仍显示已核对）",
          env.checks_count() == 1
          and detail(env, class_id, real_id)["judgement"]["progress"] == "checked",
          env.checks_count())


def _legacy_counts(db_file):
    """读旧库各表行数（只读打开，不做任何迁移）。"""
    conn = sqlite3.connect(db_file)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for name in ("users", "classes", "students", "answers", "mistakes",
                     "answer_checks", "answer_followups"):
            if name in tables:
                counts[name] = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
        return counts
    finally:
        conn.close()


def group_17_batch_isolation(envs):
    """R1-08 / T6：批次隔离与统计口径的稳定化（触发器撤掉之后仍然干净）。

    依据：任务书 R1-08「批次隔离与数据关联」、基线审查 §14.2（P0 批次名撞名 + 统计不过滤
    source）、T6 合同第 1/3/4 条。要证的四件事：
    1. 两个库级「演示批次归位」触发器已删除，且删掉之后演示数据仍进不了真实统计（不再有隐形防线）；
    2. 两班级 × 两批次 × 合成/真实不混作业；跨班读取被拒；
    3. stats 与 overview 的 not_applicable / manual 两档口径一致，撤回行不算当前结论；
    4. 1.7.x 旧库（两张表都不存在）升级不再抛 MigrationError、数据一条不丢 —— 并做反向自证。
    """
    section("17. T6 批次隔离与统计口径：触发器已撤仍干净 / 两班两批不混 / 旧库升级可救")

    env = Env("batch-isolation")
    envs.append(env)
    class_a = env.add_class("七年级 1 班")
    class_b = env.add_class("七年级 2 班")
    stu_a = env.add_student(class_a, "学生甲")
    stu_b = env.add_student(class_b, "学生乙")

    triggers = {row["name"] for row in env.db.query(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    check("17.0 新库不再创建演示批次归位触发器（trg_answers_demo_batch_* 已撤）",
          not ({"trg_answers_demo_batch_ins", "trg_answers_demo_batch_upd"} & triggers), triggers)

    a1 = env.add_answer(class_a, stu_a, "A1", CASE_A, batch="9月第1次作业", question_no="1",
                        student_name="学生甲")
    a2 = env.add_answer(class_a, stu_a, "A2", CASE_B, batch="9月第2次作业", question_no="2",
                        student_name="学生甲")
    b1 = env.add_answer(class_b, stu_b, "B1", CASE_A, batch="9月第2次作业", question_no="1",
                        student_name="学生乙")
    ov_a1 = overview(env, class_a, "9月第1次作业")
    ov_a2 = overview(env, class_a, "9月第2次作业")
    check("17.1 同班两批次各算各的（1 班：第1次 1 份 / 第2次 1 份）",
          ov_a1["assignment"]["total"] == 1 and ov_a1["assignment"]["batch"] == "9月第1次作业"
          and ov_a2["assignment"]["total"] == 1 and ov_a2["assignment"]["batch"] == "9月第2次作业",
          jdump([ov_a1["assignment"], ov_a2["assignment"]]))
    stats_a2 = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (class_a,)), "9月第2次作业")
    check("17.2 2 班的作答不进 1 班的统计（同为「9月第2次作业」也只有 1 份）",
          stats_a2["expected"] == 1, stats_a2)
    status, payload = env.client.get("/api/classes/%d/check/answers/%d" % (class_b, a2))
    check("17.3 跨班读他人作答被拒（404 not_found）",
          status == 404 and payload.get("code") == "not_found", (status, payload.get("code")))
    status, payload = env.client.get(
        "/api/classes/%d/check/overview?batch=%s"
        % (class_a, urllib.parse.quote("9月第1次作业")))
    check("17.4 同班按批次过滤：第1次作业的队列里没有第2次那份",
          status == 200 and (payload.get("next") or {}).get("answer_id") == a1,
          jdump((payload or {}).get("next")))

    # 17.5 绕过 seed、直接把「演示行」写进真实批次：触发器没了，统计必须仍然干净
    stamp = now_iso()
    with env.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
            " student_code, question_no, question_text, unit, standard_solution, student_steps,"
            " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_a, stu_a, "DEMO-LEGACY-1", "9月第2次作业", "", "", "9",
             CASE_A["question"], "一元一次方程", CASE_A["standard"], CASE_A["steps"],
             D.DEMO_SOURCE, "2026-11-24", 0, "", "legacyfingerprint", stamp, stamp, stamp))
        demo_legacy_id = cur.lastrowid
    check("17.5 造数自检：演示行确实落在真实批次里（不再被触发器悄悄挪走）",
          env.db.scalar("SELECT batch FROM answers WHERE id=?", (demo_legacy_id,)) == "9月第2次作业",
          env.db.scalar("SELECT batch FROM answers WHERE id=?", (demo_legacy_id,)))
    stats_after = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (class_a,)), "9月第2次作业")
    ov_after = overview(env, class_a, "9月第2次作业")
    queue_ids = [row["id"] for row in C.queue_rows(env.db, class_a, "9月第2次作业")]
    check("17.6 触发器删除后统计仍干净：expected / 总览份数 / 队列都不含演示行",
          stats_after["expected"] == 1 and ov_after["assignment"]["total"] == 1
          and demo_legacy_id not in queue_ids,
          jdump([stats_after["expected"], ov_after["assignment"]["total"], queue_ids]))
    demo_stats = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (class_a,)), D.DEMO_BATCH)
    check("17.7 统计永远不数演示数据（即使显式查演示批次，expected 也是 0）",
          demo_stats["expected"] == 0, demo_stats["expected"])

    data_a2 = detail(env, class_a, a2)
    save(env, class_a, a2, save_body(data_a2, cause_state="decided",
                                    cause_label="移项没变号", basis="第 2 行把 +3 写成 −3"))
    data_b1 = detail(env, class_b, b1)
    save(env, class_b, b1, save_body(data_b1, cause_state="decided",
                                    cause_label="人工判断：漏乘常数项",
                                    observed="3x-9=12", basis="原图第 2 行是 3x-9=12"))
    stats_b1 = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (class_b,)), "9月第2次作业")
    ov_b1 = overview(env, class_b, "9月第2次作业")
    check("17.8 stats 的 manual_verified 与 overview.counts.manual_verified 对齐（2 班=1）",
          stats_b1["manual_verified"] == (ov_b1["counts"].get("manual_verified") or 0) == 1,
          jdump({"stats": stats_b1["manual_verified"],
                 "overview": ov_b1["counts"].get("manual_verified")}))
    stats_a2b = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (class_a,)), "9月第2次作业")
    check("17.9 stats 的 cause_not_applicable 与 overview.counts.not_applicable_cause 对齐",
          stats_a2b["cause_not_applicable"]
          == (ov_after["counts"].get("not_applicable_cause") or 0),
          jdump({"stats": stats_a2b["cause_not_applicable"],
                 "overview": ov_after["counts"].get("not_applicable_cause")}))
    check("17.10 两个新档位都在统计块里（cause_not_applicable / manual_verified 键存在）",
          "cause_not_applicable" in stats_b1 and "manual_verified" in stats_b1,
          sorted(stats_b1.keys()))

    checked_before = stats_b1["checked"]
    retract_body = save_body(detail(env, class_b, b1))
    retract_body["progress"] = "pending"
    retract_body["change_reason"] = "判错了，撤回"
    for key in ("cause_state", "cause_code", "cause_label"):
        retract_body.pop(key, None)
    status, _ = save(env, class_b, b1, retract_body)
    stats_b1_after = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (class_b,)), "9月第2次作业")
    check("17.11 撤回后旧结论退出统计（checked %s → %s、pending 回到 1）"
          % (checked_before, stats_b1_after["checked"]),
          status == 200 and stats_b1_after["checked"] == 0
          and stats_b1_after["pending"] == 1, jdump(stats_b1_after))
    check("17.12 撤回行本身还在库里（历史不删），只是版本置空",
          env.db.scalar("SELECT COUNT(*) FROM answer_checks WHERE answer_id=?", (b1,)) >= 1
          and env.db.scalar("SELECT answer_version FROM answer_checks WHERE answer_id=?"
                            " ORDER BY id DESC LIMIT 1", (b1,)) == "", "ok")

    # 17.13 1.7.x 旧库升级：不再抛 MigrationError，数据一条不丢
    fixture = os.path.join(ROOT, "tests", "fixtures", "legacy160", "app.db")
    legacy_dir = os.path.join(WORK, "legacy-t6")
    shutil.rmtree(legacy_dir, ignore_errors=True)
    os.makedirs(legacy_dir)
    legacy_db = os.path.join(legacy_dir, "app.db")
    shutil.copy2(fixture, legacy_db)
    counts_before = _legacy_counts(legacy_db)
    migrated = None
    failure = None
    try:
        migrated = dbmod.ensure_answer_check_schema(legacy_db)
    except Exception as exc:  # noqa: BLE001 —— 这里要如实记录失败，而不是抛给上层
        failure = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    counts_after = _legacy_counts(legacy_db)
    check("17.13 1.7.x 旧库升级不再抛 MigrationError（并有备份文件）",
          failure is None and bool(migrated) and os.path.exists(migrated), failure)
    check("17.14 升级后原有数据一条不丢、两张新表已建好",
          counts_after.get("answers") == counts_before.get("answers")
          and counts_after.get("mistakes") == counts_before.get("mistakes")
          and counts_after.get("classes") == counts_before.get("classes")
          and counts_after.get("answer_checks") == 0
          and counts_after.get("answer_followups") == 0,
          jdump([counts_before, counts_after]))
    check("17.15 升级后结构检查通过（answer_checks 就绪判定不再要求那两个触发器）",
          dbmod.answer_check_schema_ready(legacy_db), "ready")

    # 17.15b/17.15c 服务器启动时的完整升级链：add_student_active → ensure_answers_schema
    # → ensure_tutor_schema → ensure_followup_schema → ensure_answer_check_schema。
    # 1.7.x 旧库只有前几套结构，第五步就是本轮修的 bug 所在，所以整链必须一次走通。
    full_dir = os.path.join(WORK, "legacy-t6-full")
    shutil.rmtree(full_dir, ignore_errors=True)
    os.makedirs(full_dir)
    full_db = os.path.join(full_dir, "app.db")
    shutil.copy2(fixture, full_db)
    chain = []
    for name in ("add_student_active", "ensure_answers_schema", "ensure_tutor_schema",
                 "ensure_followup_schema", "ensure_answer_check_schema"):
        try:
            getattr(dbmod, name)(full_db)
            chain.append((name, "ok"))
        except Exception as exc:  # noqa: BLE001 —— 失败要如实记录，而不是让整包崩掉
            chain.append((name, "%s: %s" % (type(exc).__name__, str(exc)[:80])))
    check("17.15b 服务器启动链在 1.7.x 旧库上一次走通（5 步迁移全部成功）",
          all(state == "ok" for _, state in chain), jdump(chain))
    check("17.15c 整链升级后 answers 与 answer_checks 两套结构都就绪",
          dbmod.answers_schema_ready(full_db) and dbmod.answer_check_schema_ready(full_db)
          and _legacy_counts(full_db).get("answers") == counts_before.get("answers"),
          jdump(_legacy_counts(full_db)))

    # 17.15d/17.15e 旧 answer_checks（T1 时代：还没有 cause_source 列）升级后，
    # 有结论的行标 legacy（不能读成「从未判断」），没结论的行保持空。
    old_dir = os.path.join(WORK, "legacy-t6-oldchecks")
    shutil.rmtree(old_dir, ignore_errors=True)
    os.makedirs(old_dir)
    old_db = os.path.join(old_dir, "app.db")
    shutil.copy2(fixture, old_db)
    old_conn = sqlite3.connect(old_db)
    try:
        with old_conn:
            old_conn.execute(
                "CREATE TABLE answer_checks ("
                " id INTEGER PRIMARY KEY, class_id INTEGER NOT NULL, student_id INTEGER NOT NULL,"
                " answer_id INTEGER NOT NULL, batch TEXT NOT NULL DEFAULT '',"
                " answer_version TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT 'checked',"
                " cause_state TEXT NOT NULL DEFAULT 'undecided',"
                " observed_step TEXT NOT NULL DEFAULT '', first_bad_index INTEGER,"
                " locate_status TEXT NOT NULL DEFAULT '', locate_source TEXT NOT NULL DEFAULT '',"
                " contrast_note TEXT NOT NULL DEFAULT '', cause_code TEXT NOT NULL DEFAULT '',"
                " cause_label TEXT NOT NULL DEFAULT '', cause_history TEXT NOT NULL DEFAULT '[]',"
                " teacher_note TEXT NOT NULL DEFAULT '', created_by INTEGER,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                " decided_at TEXT NOT NULL DEFAULT '')")
            old_conn.execute(
                "INSERT INTO answer_checks(id, class_id, student_id, answer_id, answer_version,"
                " cause_state, cause_code, cause_label, created_at, updated_at)"
                " VALUES (1,1,1,1,'v-old','decided','E-OLD-1','老原因', 't1','t1')")
            old_conn.execute(
                "INSERT INTO answer_checks(id, class_id, student_id, answer_id, answer_version,"
                " cause_state, cause_code, cause_label, created_at, updated_at)"
                " VALUES (2,1,1,2,'v-old','undecided','','', 't2','t2')")
    finally:
        old_conn.close()
    migrated_old = None
    failure_old = None
    try:
        migrated_old = dbmod.ensure_answer_check_schema(old_db)
    except Exception as exc:  # noqa: BLE001
        failure_old = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    old_rows = {}
    if failure_old is None:
        conn = sqlite3.connect(old_db)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute("SELECT id, cause_source, cause_code FROM answer_checks"):
                old_rows[row["id"]] = dict(row)
        finally:
            conn.close()
    check("17.15d 旧 answer_checks 升级成功（补列 + 建索引，不要求有那两个触发器）",
          failure_old is None and bool(migrated_old), failure_old)
    check("17.15e 有结论的历史行回填 cause_source=legacy、没结论的行保持空（不误标「从未判断」）",
          old_rows.get(1, {}).get("cause_source") == "legacy"
          and old_rows.get(2, {}).get("cause_source") == "", jdump(old_rows))

    # 17.18 升级安装：旧库里残留的触发器，打开时被显式删掉（不再保留隐形行为）
    retire_dir = os.path.join(WORK, "retired-trigger")
    shutil.rmtree(retire_dir, ignore_errors=True)
    os.makedirs(retire_dir)
    retire_db = os.path.join(retire_dir, "app.db")
    seed_db = Database(retire_db)
    with seed_db.tx() as conn:
        conn.execute(
            "CREATE TRIGGER trg_answers_demo_batch_ins AFTER INSERT ON answers"
            " BEGIN UPDATE answers SET batch=batch WHERE id=NEW.id; END")
    seed_db.conn.close()
    reopened = Database(retire_db, create=True)
    try:
        left_triggers = [row["name"] for row in reopened.query(
            "SELECT name FROM sqlite_master WHERE type='trigger'")]
    finally:
        reopened.conn.close()
    check("17.18 旧库残留的演示批次触发器在打开时被显式删除（升级安装不再保留隐形行为）",
          left_triggers == [], left_triggers)

    # 17.16 反向自证：把「先建表」改回旧顺序（补列在建表之前），同一份夹具必须变红
    root = _copy_app(os.path.join(WORK, "mutants", "m6"))
    patched = _patch(os.path.join(root, "app", "db.py"),
                     '            for ddl in _answer_check_ddl_by_kind("table"):',
                     '            for ddl in []:  # 变异体：复刻旧顺序（先补列、后建表）')
    check("17.16 变异体6（迁移顺序）补丁命中唯一位置", patched)
    code = (
        "import json,os,shutil,sys,tempfile;sys.path.insert(0,%r);"
        "from app import db as D;"
        "work=tempfile.mkdtemp();target=os.path.join(work,'app.db');"
        "shutil.copy2(%r,target)\n"
        "try:\n"
        "    D.ensure_answer_check_schema(target)\n"
        "    print(json.dumps(['ok']))\n"
        "except D.MigrationError as exc:\n"
        "    print(json.dumps(['err',str(exc)[:40]]))\n"
    ) % (root, fixture)
    rc, out, err = _run_mutant(root, code)
    check("17.17 变异体6 把建表放到补列之后 → 同一夹具必须抛 MigrationError（断言不恒真）",
          rc == 0 and out.startswith('["err"'), "%s %s" % (out, err))


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
        # T11：依据必填（先跑，变异体 5 要拿它的库做接口级反向自证）
        basis_env, basis_class, basis_answer = group_11b_basis_required(envs)
        group_12_mutants(envs, queue_env, queue_class, basis_env, basis_class, basis_answer)
        group_13_demo_rule_invariant()
        group_15_demo_seed_reset(envs)
        # T6：批次隔离 / 统计口径 / 旧库升级（含迁移顺序的反向自证）
        group_17_batch_isolation(envs)
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
