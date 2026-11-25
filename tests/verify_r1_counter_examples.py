# -*- coding: utf-8 -*-
"""T7 R1 反例测试：22 条必覆盖反例，全部走真实业务调用路径（环回 HTTP + 真实库）。

为什么要这个套件（与既有套件的关系）：
- `verify_answer_check_150.py` / `verify_answer_check_ui.py` / `verify_r1_05_06_manual_followup.py`
  等已经按功能分组覆盖了大部分行为，但它们是「按实现分组」写的；
- 本套件按**任务书列出的 22 条必覆盖反例**逐条独立重推：自己起服务、自己造数、自己断言，
  不复用那些套件的断言代码。这样一处实现被改坏时，两套不同来路的断言不会一起变哑巴。

反自欺约定：每条断言都必须「可以假」。第 23 组用变异体（把实现按预期改错后跑同一批断言）
证明关键几条不是恒真。

跑法（源码根目录）：C:\\Python314\\python.exe tests\\verify_r1_counter_examples.py
日志：docs/verify-r1-counter-examples.txt
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
from app import demo_answers as D  # noqa: E402
from app.answers import fingerprint  # noqa: E402
from app.db import Database, now_iso  # noqa: E402
from app.stats import answer_check_stats  # noqa: E402

import server as server_mod  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r1-counter")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-counter-examples.txt")
FIXTURE_160 = os.path.join(ROOT, "tests", "fixtures", "legacy160", "app.db")

PASSED = [0]
FAILED = [0]
LINES = []
ENVS = []

DEMO_BATCH = D.DEMO_BATCH
BATCH = "9月第2次作业"

# 案例（取自 数智伴学_Agent开发规格.md §7 与 R1 各任务的实测案例）
CASE_MOVE = {"q": "2x+7=15", "std": "2x+7=15；2x=8；x=4", "steps": "2x+7=15\n2x=15+7\nx=11"}
CASE_BRACKET = {"q": "3(x-2)=12", "std": "3(x-2)=12；3x-6=12；3x=18；x=6",
                "steps": "3(x-2)=12\n3x-2=12\nx=14/3"}
CASE_OK = {"q": "3(x-2)=12", "std": "3(x-2)=12；3x-6=12；3x=18；x=6",
           "steps": "3(x-2)=12\n3x-6=12\n3x=18\nx=6"}
CASE_UNFIN = {"q": "2x+4=10", "std": "2x+4=10；2x=6；x=3", "steps": "2x+4=10\n2x=6"}
CASE_OOS = {"q": "2x+3>7", "std": "2x+3>7；2x>4；x>2", "steps": "2x+3>7\n2x>4\nx>2"}
CASE_MISS = {"q": "2x+4=10", "std": "2x+4=10；2x=6；x=3", "steps": "", "missing": 1,
             "missing_reason": "请假"}


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        PASSED[0] += 1
        log("PASS %s" % name)
    else:
        FAILED[0] += 1
        log("FAIL %s   -> %s" % (name, detail))


def section(title):
    log("")
    log("== %s ==" % title)


def jd(obj, n=300):
    return json.dumps(obj, ensure_ascii=False)[:n]


# ---------------------------------------------------------------- HTTP 客户端
class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, body=None):
        data = None
        if method != "GET":
            data = json.dumps(body if body is not None else {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, (json.loads(raw.decode("utf-8")) if raw else {})
            except ValueError:
                return exc.code, {"raw": raw.decode("utf-8", "replace")}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})

    def patch(self, path, body=None):
        return self.request("PATCH", path, body if body is not None else {})


class Env:
    """一个数据目录 + 一个只监听 127.0.0.1 的真实服务 + 一个已登录客户端。"""

    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(WORK, name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        self.server = server_mod.make_server("127.0.0.1", 0, self.dir, use_demo=False)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.base)
        status, payload = self.client.post("/api/auth/register",
                                          {"username": "t7", "password": "pw123456"})
        if status != 200:
            raise RuntimeError("注册失败：%s %s" % (status, payload))
        self.db = Database(os.path.join(self.dir, "app.db"), create=False)
        self.user_id = self.db.scalar("SELECT id FROM users WHERE username='t7'")
        ENVS.append(self)

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=10)
        except Exception:
            pass
        try:
            self.db.conn.close()
        except Exception:
            pass

    # -- 造数 ---------------------------------------------------------
    def add_class(self, name):
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO classes(user_id,name,grade,note,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?)", (self.user_id, name, "七年级", "", stamp, stamp))
            return cur.lastrowid

    def add_student(self, class_id, name, code=""):
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO students(class_id,name,code,note,active,created_at,updated_at)"
                " VALUES (?,?,?,?,1,?,?)", (class_id, name, code, "", stamp, stamp))
            return cur.lastrowid

    def add_answer(self, class_id, student_id, record_key, case, *, batch=BATCH,
                   source="真实导入", question=None, steps=None, missing=None,
                   student_name=""):
        row = {
            "question_no": "1",
            "question_text": case["q"] if question is None else question,
            "unit": "一元一次方程",
            "standard_solution": case["std"],
            "student_steps": case["steps"] if steps is None else steps,
            "occurred_on": "2026-11-24",
            "missing": int(case.get("missing", 0) if missing is None else missing),
            "missing_reason": case.get("missing_reason", ""),
        }
        fp = fingerprint(row, student_id)
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO answers(class_id,student_id,record_key,batch,student_name,student_code,"
                "question_no,question_text,unit,standard_solution,student_steps,source,occurred_on,"
                "missing,missing_reason,fingerprint,imported_at,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, student_id, record_key, batch, student_name, "",
                 row["question_no"], row["question_text"], row["unit"], row["standard_solution"],
                 row["student_steps"], source, row["occurred_on"], row["missing"],
                 row["missing_reason"], fp, stamp, stamp, stamp))
            return cur.lastrowid


# ---------------------------------------------------------------- 接口小工具
def detail(env, class_id, answer_id):
    status, payload = env.client.get("/api/classes/%d/check/answers/%d" % (class_id, answer_id))
    if status != 200:
        raise RuntimeError("详情失败：%s %s" % (status, payload))
    return payload


def overview(env, class_id, batch=""):
    path = "/api/classes/%d/check/overview" % class_id
    if batch:
        path += "?batch=" + urllib.parse.quote(batch)
    status, payload = env.client.get(path)
    if status != 200:
        raise RuntimeError("总览失败：%s %s" % (status, payload))
    return payload


def save(env, class_id, answer_id, body):
    return env.client.post("/api/classes/%d/check/answers/%d/save" % (class_id, answer_id), body)


def patch_content(env, class_id, answer_id, body):
    return env.client.patch("/api/classes/%d/check/answers/%d/content" % (class_id, answer_id), body)


def records(env, class_id, query=""):
    status, payload = env.client.get("/api/classes/%d/check/records%s" % (class_id, query))
    return status, payload


def body_for(data, **over):
    """按详情拼「老师点了主按钮」的请求体；over 里给什么就覆盖什么。"""
    locate = data.get("locate") or {}
    body = {
        "expected_version": (data.get("answer") or {}).get("version", ""),
        "observed_step": over.pop("observed_step", locate.get("first_bad_text") or ""),
        "first_bad_index": over.pop("first_bad_index", locate.get("first_bad_index")),
        "cause_state": over.pop("cause_state", "undecided"),
        "cause_code": over.pop("cause_code", ""),
        "cause_label": over.pop("cause_label", ""),
        "teacher_note": over.pop("teacher_note", ""),
    }
    body.update(over)
    return body


def db_checks(env, answer_id):
    return env.db.query(
        "SELECT * FROM answer_checks WHERE answer_id=? ORDER BY id", (answer_id,))


def db_current(env, answer_id):
    return env.db.query_one(
        "SELECT * FROM answer_checks WHERE answer_id=? AND answer_version<>''"
        " ORDER BY id DESC LIMIT 1", (answer_id,))


def db_followups(env, answer_id):
    return env.db.query(
        "SELECT * FROM answer_followups WHERE answer_id=? ORDER BY id", (answer_id,))


def wait_for(predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- 22 条反例
def group_evidence_pollution(env, cls, ans):
    section("反例 1：合成演示证据不得污染真实作答（T7-01）")
    d = detail(env, cls, ans["move"])
    text = json.dumps(d, ensure_ascii=False)
    check("T7-01.1 真实作答没有预置候选", d["candidates"] == [], d["candidates"])
    check("T7-01.2 错因来源不是 preset", d["causes_source"] != "preset", d["causes_source"])
    check("T7-01.3 判据范围是真实作答", d["causes_scope"] != C.SCOPE_SYNTHETIC, d["causes_scope"])
    demo_codes = [c.get("code") for c in D.build()["answers"][0].get("candidates") or []]
    leaked = [code for code in demo_codes if code and code in text]
    check("T7-01.4 演示候选编码一个字都没出现在真实作答详情里", leaked == [], leaked)
    check("T7-01.5 说明里没有「候选来自预置演示条目」",
          "预置演示" not in (d.get("causes_note") or ""), d.get("causes_note"))


def _demo_preset_alive(env, cls):
    """对照探针：载入演示后，逐字相同的演示记录必须仍有预置候选（否则 T7-02.1 没有说服力）。"""
    env.client.post("/api/classes/%d/check/demo/seed" % cls, {})
    row = env.db.query_one(
        "SELECT id FROM answers WHERE class_id=? AND record_key='DEMO-A-01'", (cls,))
    alive = False
    if row:
        d = detail(env, cls, row["id"])
        alive = bool(d["candidates"]) and d["causes_source"] == "preset"
    env.client.post("/api/classes/%d/check/demo/reset" % cls, {})
    return alive


def group_normalized_question(env, cls, ans):
    section("反例 2：题干只是「归一化后相同」不得命中演示预置（T7-02）")
    demo = D.build()["answers"][0]
    # 演示记录的题干与步骤原文（五项全匹配的基线）
    full_width = demo["question_text"].replace("(", "（").replace(")", "）").replace("=", "＝")
    row = {"question_no": "1", "question_text": full_width, "unit": demo.get("unit", ""),
           "standard_solution": demo["standard_solution"],
           "student_steps": demo["student_steps"], "occurred_on": "2026-11-24",
           "missing": 0, "missing_reason": ""}
    fp = fingerprint(row, ans["student"])
    stamp = now_iso()
    with env.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO answers(class_id,student_id,record_key,batch,student_name,student_code,"
            "question_no,question_text,unit,standard_solution,student_steps,source,occurred_on,"
            "missing,missing_reason,fingerprint,imported_at,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cls, ans["student"], "R-FULLWIDTH", BATCH, "学生甲", "", "1", full_width,
             row["unit"], row["standard_solution"], row["student_steps"], "真实导入",
             "2026-11-24", 0, "", fp, stamp, stamp, stamp))
        full_id = cur.lastrowid
    d = detail(env, cls, full_id)
    check("T7-02.1 全角题干 + 与演示相同的步骤，仍然没有预置候选",
          d["candidates"] == [], d["candidates"])
    check("T7-02.2 来源不是 preset（归一化只用于说明，不用于匹配）",
          d["causes_source"] != "preset", d["causes_source"])
    check("T7-02.3 对照：演示记录本身（逐字相同）的预置仍然有效 —— 证明 2.1 不是环境坏",
          _demo_preset_alive(env, cls), "载入演示后 DEMO-A-01 的候选")


def group_batch_collision(env, cls, ans):
    section("反例 3：与演示批次撞名的真实批次不得被 reset 删（T7-03）")
    collide = env.add_answer(cls, ans["student"], "REAL-COLLIDE-1", CASE_OK, batch=DEMO_BATCH)
    both = env.add_answer(cls, ans["student"], "REAL-BOTH-1", CASE_OK, batch=DEMO_BATCH,
                          source=D.DEMO_SOURCE)
    status, seeded = env.client.post("/api/classes/%d/check/demo/seed" % cls, {})
    check("T7-03.1 演示数据载入成功", status == 200 and seeded.get("ok") is True, jd(seeded))
    status, out = env.client.post("/api/classes/%d/check/demo/reset" % cls, {})
    left = {r["record_key"] for r in env.db.query(
        "SELECT record_key FROM answers WHERE class_id=?", (cls,))}
    check("T7-03.2 撞批名的真实作答没被删", "REAL-COLLIDE-1" in left, sorted(left))
    check("T7-03.3 批名+来源都撞、但没有 DEMO- 前缀的真实作答没被删",
          "REAL-BOTH-1" in left, sorted(left))
    check("T7-03.4 reset 只删了演示记录", out.get("deleted", {}).get("answers")
          == len(D.build()["answers"]), jd(out))
    check("T7-03.5 演示作答全清空", not ({"DEMO-A-01", "DEMO-MV-01"} & left), sorted(left))


def group_seed_students(env, cls, ans):
    section("反例 4：seed 不得挂到真实学生（T7-04）")
    status, seeded = env.client.post("/api/classes/%d/check/demo/seed" % cls, {})
    real_ids = {r["id"] for r in env.db.query(
        "SELECT id FROM students WHERE class_id=? AND (note IS NULL OR note<>?)",
        (cls, D.STUDENT_NOTE))}
    demo_rows = env.db.query(
        "SELECT student_id, id, record_key FROM answers WHERE class_id=? AND source=?"
        " AND record_key LIKE 'DEMO-%'", (cls, D.DEMO_SOURCE))
    check("T7-04.1 演示作答一条都没挂真实学生",
          bool(real_ids) and demo_rows
          and all(r["student_id"] not in real_ids for r in demo_rows),
          (sorted(real_ids), [r["student_id"] for r in demo_rows]))
    notes = {r["note"] for r in env.db.query(
        "SELECT note FROM students WHERE class_id=?", (cls,))}
    check("T7-04.2 演示作答挂的是带标记的合成学生", D.STUDENT_NOTE in notes, sorted(notes))
    env.client.post("/api/classes/%d/check/demo/reset" % cls, {})
    check("T7-04.3 reset 后合成学生没有残留空壳", env.db.scalar(
        "SELECT COUNT(*) FROM students WHERE class_id=? AND note=?", (cls, D.STUDENT_NOTE)) == 0)


def group_demo_not_counted(env, cls, ans):
    section("反例 5：演示数据不进队列与统计（T7-05）")
    ov_before = overview(env, cls, BATCH)
    st_before = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (cls,)))
    env.client.post("/api/classes/%d/check/demo/seed" % cls, {})
    ov_after = overview(env, cls, BATCH)
    st_after = answer_check_stats(env.db, env.db.query_one(
        "SELECT * FROM classes WHERE id=?", (cls,)))
    check("T7-05.1 载入演示后「当前作业」仍是真实批次",
          ov_after["assignment"]["batch"] == BATCH, ov_after["assignment"]["batch"])
    check("T7-05.2 应交数不变",
          ov_before["assignment"]["total"] == ov_after["assignment"]["total"],
          (ov_before["assignment"]["total"], ov_after["assignment"]["total"]))
    check("T7-05.3 待核对数不含演示作答",
          ov_after["assignment"]["pending"] == ov_before["assignment"]["pending"],
          (ov_before["assignment"], ov_after["assignment"]))
    check("T7-05.4 统计块 expected 不被演示虚增",
          st_before["expected"] == st_after["expected"], (st_before["expected"], st_after["expected"]))
    demo_ids = {r["id"] for r in env.db.query(
        "SELECT id FROM answers WHERE class_id=? AND source=?", (cls, D.DEMO_SOURCE))}
    cls_row5 = env.db.query_one("SELECT * FROM classes WHERE id=?", (cls,))
    demo_stats = answer_check_stats(env.db, cls_row5, DEMO_BATCH)
    real_in_demo_batch = env.db.scalar(
        "SELECT COUNT(*) FROM answers WHERE class_id=? AND batch=? AND source<>?",
        (cls, DEMO_BATCH, D.DEMO_SOURCE))
    check("T7-05.5 演示批次的应交数只数真实行、不数演示行（真实 %d / 统计 %d）"
          % (real_in_demo_batch, demo_stats["expected"]),
          demo_stats["expected"] == real_in_demo_batch, jd(demo_stats))
    env.client.post("/api/classes/%d/check/demo/reset" % cls, {})
    check("T7-05.6 reset 后真实统计回到原值",
          answer_check_stats(env.db, env.db.query_one("SELECT * FROM classes WHERE id=?", (cls,)))["expected"]
          == st_before["expected"])


def group_no_triggers(env, cls, ans):
    section("反例 6：库级触发器删除后统计仍然干净（T7-06）")
    triggers = [r["name"] for r in env.db.query(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_answers_demo%'")]
    check("T7-06.1 库里没有演示批次归位触发器", triggers == [], triggers)
    cls_row = env.db.query_one("SELECT * FROM classes WHERE id=?", (cls,))
    st_before = answer_check_stats(env.db, cls_row)
    ov_before = overview(env, cls, BATCH)
    legacy = env.add_answer(cls, ans["student"], "DEMO-LEGACY-1", CASE_OK, batch=BATCH,
                            source=D.DEMO_SOURCE)
    moved = env.db.query_one("SELECT batch FROM answers WHERE id=?", (legacy,))
    check("T7-06.2 演示行留在真实批次里（不再被悄悄归位）", moved["batch"] == BATCH, moved)
    st_after = answer_check_stats(env.db, cls_row)
    ov_after = overview(env, cls, BATCH)
    check("T7-06.3 统计仍不数它：expected 不变",
          st_before["expected"] == st_after["expected"], (st_before["expected"], st_after["expected"]))
    check("T7-06.4 总览也不数它（与插入演示行之前逐项相等）",
          ov_after["assignment"] == ov_before["assignment"]
          and ov_after["counts"]["answers"] == ov_before["counts"]["answers"],
          (ov_before["assignment"], ov_after["assignment"]))
    env.db.tx().__enter__().execute("DELETE FROM answers WHERE id=?", (legacy,))
    with env.db.tx() as conn:
        conn.execute("DELETE FROM answers WHERE id=?", (legacy,))


def group_cause_source(env, cls, ans):
    section("反例 7：无候选时的人工判断与 cause_source 可区分（T7-07）")
    d = detail(env, cls, ans["move"])
    st, saved = save(env, cls, ans["move"], body_for(
        d, cause_state="decided", cause_label="移项没有变号", cause_code="E-MOVE-01"))
    check("T7-07.1 无候选时老师能形成判断", st == 200 and saved.get("ok") is True, jd(saved))
    check("T7-07.2 cause_source=teacher（与规则/预置可区分）",
          saved["judgement"]["cause_source"] == "teacher", saved["judgement"]["cause_source"])
    check("T7-07.3 verify_source=rule（规则定位与人工判断分列）",
          saved["judgement"]["verify_source"] == "rule", saved["judgement"]["verify_source"])
    row = db_current(env, ans["move"])
    check("T7-07.4 库里的来源列与响应一致",
          row["cause_source"] == "teacher" and row["verify_source"] == "rule",
          (row["cause_source"], row["verify_source"]))
    # 同一条答复里 teacher_error_text 与规则 observed_step 分列（人工核验轴）
    d2 = detail(env, cls, ans["move"])
    st2, saved2 = save(env, cls, ans["move"], body_for(
        d2, cause_state="decided", cause_label="移项没有变号", cause_code="E-MOVE-01",
        observed_step="2x=15+7（老师写的错步）", basis_note="当面核对，学生承认没变号"))
    check("T7-07.5 老师写的错步与规则错步分列存",
          st2 == 200 and saved2["judgement"]["verify_source"] == "manual"
          and "老师写的错步" in (saved2["judgement"]["teacher_error_text"] or ""),
          (saved2["judgement"]["verify_source"], saved2["judgement"]["teacher_error_text"]))
    check("T7-07.6 规则的错步仍然保留（没被覆盖）",
          saved2["locate"]["first_bad_text"] == "2x=15+7", saved2["locate"]["first_bad_text"])


def group_forbidden(env, cls, ans):
    section("反例 8：forbidden（correct/缺测）提交 decided 必 400（T7-08）")
    for label, answer_id in (("正确作答", ans["ok"]), ("缺测", ans["miss"])):
        d = detail(env, cls, answer_id)
        before = len(db_checks(env, answer_id))
        st, payload = save(env, cls, answer_id, body_for(
            d, cause_state="decided", cause_label="移项没有变号", cause_code="E-MOVE-01"))
        check("T7-08.%s %s + decided → 400 cause_not_applicable"
              % ("1" if label == "正确作答" else "2", label),
              st == 400 and payload.get("code") == "cause_not_applicable",
              (st, jd(payload)))
        check("T7-08.%s %s 被拒后一条都没写库"
              % ("3" if label == "正确作答" else "4", label),
              len(db_checks(env, answer_id)) == before, len(db_checks(env, answer_id)))


def group_oos_basis(env, cls, ans):
    section("反例 9：out_of_scope 无依据提交 decided 必 400（T7-09）")
    d = detail(env, cls, ans["oos"])
    check("T7-09.0 前提：这份确实是 out_of_scope", d["locate"]["status"] == "out_of_scope",
          d["locate"]["status"])
    before = len(db_checks(env, ans["oos"]))
    st, payload = save(env, cls, ans["oos"], body_for(
        d, cause_state="decided", cause_label="不等式没学", cause_code="E-OOS-01"))
    check("T7-09.1 没填依据 → 400 basis_required",
          st == 400 and payload.get("code") == "basis_required", (st, jd(payload)))
    check("T7-09.2 被拒后一条都没写库", len(db_checks(env, ans["oos"])) == before)
    st2, saved = save(env, cls, ans["oos"], body_for(
        d, cause_state="decided", cause_label="不等式没学", cause_code="E-OOS-01",
        basis_note="问过学生：还没学不等式，按课上例题写的"))
    check("T7-09.3 填了依据 → 200 且记成人工核验",
          st2 == 200 and saved["judgement"]["verify_source"] == "manual",
          (st2, saved.get("judgement", {}).get("verify_source")))
    check("T7-09.4 依据落库（不是只回显）",
          "还没学不等式" in (db_current(env, ans["oos"])["basis_note"] or ""),
          db_current(env, ans["oos"])["basis_note"])
    # 反向对照：first_bad（required）不强制依据，别把闸门开到不该开的地方
    d2 = detail(env, cls, ans["bracket"])
    st3, saved3 = save(env, cls, ans["bracket"], body_for(
        d2, cause_state="decided", cause_label="去括号漏乘", cause_code="E-BRACKET-01"))
    check("T7-09.5 first_bad 提交 decided 不强制依据（闸门没有过宽）",
          st3 == 200 and saved3["judgement"]["verify_source"] == "rule", (st3, jd(saved3)[:160]))


def group_no_silent_fix(env, cls, ans):
    section("反例 10：静默改写零容忍，非法请求必须 4xx 且不写库（T7-10）")
    d = detail(env, cls, ans["unfin"])
    d_move = detail(env, cls, ans["move"])
    cases = [
        ("T7-10.1 版本不匹配 → 409 version_conflict", ans["unfin"], d,
         dict(expected_version="0" * 64), {"code": "version_conflict"}, 409),
        ("T7-10.2 progress 取值非法 → 400 bad_progress", ans["unfin"], d,
         dict(progress="done"), {"code": "bad_progress"}, 400),
        ("T7-10.3 decided 但没有原因说明 → 400 cause_required", ans["unfin"], d,
         dict(cause_state="decided", cause_label="", cause_code=""),
         {"code": "cause_required"}, 400),
        # 撤回只能作废整条结论，不能夹带新判断；用 required 档（移项题）验，才不会被
        # 「forbidden 档先报 cause_not_applicable」抢在前面。
        ("T7-10.4 撤回却同时提交原因 → 400 cause_state_mismatch", ans["move"], d_move,
         dict(progress="pending", cause_state="decided", cause_label="x", cause_code="E-X"),
         {"code": "cause_state_mismatch"}, 400),
    ]
    for name, answer_id, data, over, expect, status_code in cases:
        before = len(db_checks(env, answer_id))
        st, payload = save(env, cls, answer_id, body_for(data, **over))
        check(name, st == status_code and payload.get("code") == expect["code"], (st, jd(payload)))
        check("%s ── 被拒后没有写库" % name.split(" ")[0], len(db_checks(env, answer_id)) == before,
              (before, len(db_checks(env, answer_id))))


def _eleven(view):
    """把「权威 11 项」从一份响应里取出来，用于逐项比较。"""
    locate = view.get("locate") or {}
    judgement = view.get("judgement") or {}
    answer = view.get("answer") or {}
    queue = view.get("queue") or {}
    followup = view.get("followup") or {}
    return {
        "1.answer_identity": [answer.get("id"), answer.get("version"), answer.get("record_key"),
                              answer.get("batch")],
        "2.locate_conclusion": [locate.get("status"), locate.get("source"),
                                locate.get("first_bad_index"), locate.get("first_bad_text")],
        "3.steps": locate.get("steps"),
        "4.completion": locate.get("completion"),
        "5.manual_verification": [locate.get("manual"), locate.get("verification")],
        "6.two_axes": [judgement.get("progress"), judgement.get("cause_state"),
                       judgement.get("progress_label"), judgement.get("cause_label")],
        "7.cause_source": [judgement.get("cause_source"), judgement.get("cause_source_label")],
        "8.notes": [judgement.get("basis_note"), judgement.get("teacher_note"),
                    judgement.get("observed_step")],
        "9.followup": [followup.get("question"), followup.get("recorded"),
                       followup.get("answer_text"), followup.get("source")],
        "10.queue": [queue.get("next"), queue.get("next_answer_id"), queue.get("remaining"),
                     queue.get("is_last")],
        "11.adjustments_receipt": [view.get("adjustments"), view.get("receipt")],
    }


def group_eleven_items(env, cls, ans):
    section("反例 11：保存响应的权威 11 项与再 GET / 库里一致（T7-11）")
    d = detail(env, cls, ans["bracket"])
    st, saved = save(env, cls, ans["bracket"], body_for(
        d, cause_state="decided", cause_label="去括号漏乘", cause_code="E-BRACKET-01",
        teacher_note="课堂讲评过", followup={"question": "括号外的 3 乘到 -2 了吗？",
                                            "answer_text": "我以为只乘 x"}))
    check("T7-11.0 保存成功", st == 200 and saved.get("ok") is True, jd(saved))
    again = detail(env, cls, ans["bracket"])
    eleven_saved = _eleven(saved)
    eleven_again = _eleven(again)
    missing = [k for k, v in eleven_saved.items() if v in (None, [], "")]
    check("T7-11.1 11 项在保存响应里都齐备", missing == [], missing)
    # 第 11 项（adjustments + receipt）按契约只在保存响应里出现，再 GET 详情没有它；
    # 所以逐项相等只比 1–10，第 11 项单独断言「保存响应里有、且与 judgement 不矛盾」。
    diff = [k for k in eleven_saved if k != "11.adjustments_receipt"
            and eleven_saved[k] != eleven_again[k]]
    check("T7-11.2 再 GET 详情逐项相等（第 1–10 项）", diff == [],
          [(k, jd(eleven_saved[k], 80), jd(eleven_again[k], 80)) for k in diff][:3])
    adjustments, receipt = eleven_saved["11.adjustments_receipt"]
    check("T7-11.2b 第 11 项（adjustments + receipt）确实在保存响应里",
          isinstance(adjustments, list) and isinstance(receipt, dict) and bool(receipt),
          jd(receipt)[:160])
    check("T7-11.2c 回执的状态行与 judgement 两条轴一致",
          (receipt.get("status_line") or "").startswith(saved["judgement"]["progress_label"])
          or saved["judgement"]["progress_label"] in (receipt.get("status_line") or ""),
          (receipt.get("status_line"), saved["judgement"]["progress_label"]))
    row = db_current(env, ans["bracket"])
    check("T7-11.3 库里的两条轴与响应一致",
          row["progress"] == saved["judgement"]["progress"]
          and row["cause_state"] == saved["judgement"]["cause_state"],
          (row["progress"], row["cause_state"]))
    check("T7-11.4 库里的说明/依据与响应一致",
          (row["teacher_note"] or "") == (saved["judgement"]["teacher_note"] or ""),
          (row["teacher_note"], saved["judgement"]["teacher_note"]))
    check("T7-11.5 追问原话落库且详情里 recorded=true",
          saved["followup"]["recorded"] is True
          and any(f["answer_text"] == "我以为只乘 x" for f in db_followups(env, ans["bracket"])),
          jd(saved["followup"]))


def group_progress_rollback(env, cls, ans):
    section("反例 12：progress 可回退，撤回留 invalidated_*（T7-12）")
    d = detail(env, cls, ans["bracket"])
    before = db_current(env, ans["bracket"])
    st, saved = save(env, cls, ans["bracket"], body_for(
        d, progress="pending", change_reason="核错了，撤回"))
    check("T7-12.1 撤回成功", st == 200 and saved.get("ok") is True, jd(saved))
    row = env.db.query_one("SELECT * FROM answer_checks WHERE id=?", (before["id"],))
    check("T7-12.2 撤回行 answer_version 置空", row["answer_version"] == "", row["answer_version"])
    check("T7-12.3 撤回行 progress=pending", row["progress"] == "pending", row["progress"])
    check("T7-12.4 invalidated_* 三列都写了",
          bool(row["invalidated_at"]) and bool(row["invalidated_reason"]),
          (row["invalidated_at"], row["invalidated_reason"]))
    check("T7-12.5 行本身与历史都保留（没删行）",
          env.db.scalar("SELECT COUNT(*) FROM answer_checks WHERE answer_id=?", (ans["bracket"],)) >= 1)
    again = detail(env, cls, ans["bracket"])
    check("T7-12.6 详情回到待核对", again["judgement"]["progress"] == "pending",
          again["judgement"]["progress"])
    ov = overview(env, cls, BATCH)
    check("T7-12.7 撤回后这份回到待核对队列",
          ov["assignment"]["pending"] >= 1, jd(ov["assignment"]))
    check("T7-12.8 撤回后再撤回 → 400 nothing_to_retract",
          save(env, cls, ans["bracket"], body_for(again, progress="pending"))[0] == 400,
          "重复撤回")


def group_aba(env, cls, ans):
    section("反例 13：A→B→A 不堆重复结论（T7-13）")
    d = detail(env, cls, ans["move"])
    for label in ("移项没有变号", "去括号漏乘", "移项没有变号"):
        st, saved = save(env, cls, ans["move"], body_for(
            d, cause_state="decided", cause_label=label, cause_code="E-" + label))
        if st != 200:
            check("T7-13.0 三次改判都能保存（%s）" % label, False, (st, jd(saved)))
            return
    rows = env.db.query(
        "SELECT id, answer_version, cause_label, cause_history FROM answer_checks"
        " WHERE answer_id=? AND answer_version<>''", (ans["move"],))
    check("T7-13.1 同一 (answer_id, answer_version) 只有一条当前结论",
          len(rows) == 1, [(r["id"], r["answer_version"][:8]) for r in rows])
    history = json.loads(rows[0]["cause_history"] or "[]") if rows else []
    check("T7-13.2 改判历史留在 cause_history 里（旧结论没丢）",
          len(history) >= 2, [h.get("action") for h in history])
    check("T7-13.3 当前结论是最后一次提交的那个",
          rows and rows[0]["cause_label"] == "移项没有变号", rows[0]["cause_label"] if rows else None)


def group_unfinished(env, cls, ans):
    section("反例 14：未完成但各行等价，不得判 correct（T7-14）")
    d = detail(env, cls, ans["unfin"])
    locate = d["locate"]
    check("T7-14.1 status=missing_steps", locate["status"] == "missing_steps", locate["status"])
    check("T7-14.2 completion.state=unfinished",
          (locate.get("completion") or {}).get("state") == "unfinished",
          locate.get("completion"))
    check("T7-14.3 步骤完整性认定「有过程」",
          (locate.get("steps") or {}).get("has_process") is True, locate.get("steps"))
    check("T7-14.4 人工处理状态是「需要补充步骤」",
          (locate.get("manual") or {}).get("state") == "need_steps", locate.get("manual"))
    ok = detail(env, cls, ans["ok"])
    check("T7-14.5 对照：真正解完的才判 correct",
          ok["locate"]["status"] == "correct", ok["locate"]["status"])


def group_followup_kind(env, cls, ans):
    section("反例 15：追问按错步类型生成，移项题不得问括号（T7-15）")
    move = detail(env, cls, ans["move"])["followup"]
    check("T7-15.1 移项题 kind=rule:term_move", move["kind"] == "rule:term_move", move["kind"])
    check("T7-15.2 移项题的问句问的是「符号/移项」，不是括号",
          "括号" not in move["question"] and ("符号" in move["question"] or "移" in move["question"]),
          move["question"])
    check("T7-15.3 问句逐字引用了本条原文两行",
          "2x+7=15" in move["question"] and "2x=15+7" in move["question"], move["question"])
    bracket = detail(env, cls, ans["bracket2"])["followup"]
    check("T7-15.4 展开括号题 kind=rule:bracket_expand",
          bracket["kind"] == "rule:bracket_expand", bracket["kind"])
    check("T7-15.5 括号题才问括号", "括号" in bracket["question"], bracket["question"])


def group_followup_unavailable(env, cls, ans):
    section("反例 16：无错步/缺测时追问 available=false（T7-16）")
    for label, answer_id in (("正确作答", ans["ok"]), ("缺测", ans["miss"])):
        fu = detail(env, cls, answer_id)["followup"]
        check("T7-16.%s %s 的追问不可用且问句为空" % ("1" if label == "正确作答" else "2", label),
              fu["available"] is False and fu["question"] == "", jd(fu))
        check("T7-16.%s %s 的说明写清「暂无」" % ("3" if label == "正确作答" else "4", label),
              "暂无" in (fu.get("note") or ""), fu.get("note"))


def group_followup_invalidation(env, cls, ans):
    section("反例 17：追问证据更新后旧确认失效（T7-17）")
    # ① answer_updated：补录内容（新版本）
    d = detail(env, cls, ans["move"])
    st, saved = save(env, cls, ans["move"], body_for(
        d, cause_state="decided", cause_label="移项没有变号", cause_code="E-MOVE-01",
        followup={"question": "移项要变号吗？", "answer_text": "要变号"}))
    check("T7-17.1 追问记录成功", st == 200 and saved["followup"]["recorded"] is True,
          jd(saved.get("followup")))
    st2, patched = patch_content(env, ans["cls"], ans["move"], {
        "expected_version": d["answer"]["version"],
        "student_steps": "2x+7=15\n2x=8\nx=11"})
    fu_rows = db_followups(env, ans["move"])
    check("T7-17.2 补录后旧追问标 answer_updated",
          st2 == 200 and fu_rows and fu_rows[-1]["invalidated_reason"].startswith("answer_updated"),
          (st2, [r["invalidated_reason"] for r in fu_rows]))
    check("T7-17.3 补录回执里 invalidated_followups>=1",
          (patched.get("invalidated_followups") or 0) >= 1, jd(patched.get("invalidated_followups")))
    check("T7-17.4 原话没被删（只是失效）", fu_rows and fu_rows[-1]["answer_text"] == "要变号",
          [r["answer_text"] for r in fu_rows])

    # ② conclusion_retracted：撤回结论
    d2 = detail(env, cls, ans["move"])
    st3, saved3 = save(env, cls, ans["move"], body_for(
        d2, cause_state="decided", cause_label="移项没有变号", cause_code="E-MOVE-01",
        followup={"question": "移项要变号吗？", "answer_text": "这次懂了"}))
    st4, retracted = save(env, cls, ans["move"], body_for(d2, progress="pending"))
    reasons = [r["invalidated_reason"] for r in db_followups(env, ans["move"])]
    check("T7-17.5 撤回结论后旧追问标 conclusion_retracted",
          st4 == 200 and any(r.startswith("conclusion_retracted") for r in reasons), reasons)

    # ③ evidence_changed：证据（结论）变化
    d3 = detail(env, cls, ans["bracket2"])
    st5, s5 = save(env, cls, ans["bracket2"], body_for(
        d3, cause_state="decided", cause_label="去括号漏乘", cause_code="E-BRACKET-01",
        followup={"question": "括号外的 3 乘 -2 了吗？", "answer_text": "没乘"}))
    st6, s6 = save(env, cls, ans["bracket2"], body_for(
        detail(env, cls, ans["bracket2"]), cause_state="decided", cause_label="移项没有变号",
        cause_code="E-MOVE-01"))
    reasons2 = [r["invalidated_reason"] for r in db_followups(env, ans["bracket2"])]
    check("T7-17.6 结论变了之后旧追问标 evidence_changed",
          st5 == 200 and st6 == 200 and any(r.startswith("evidence_changed") for r in reasons2),
          (st5, st6, reasons2))
    check("T7-17.7 三种失效原因都出现过（answer_updated / conclusion_retracted / evidence_changed）",
          any(r.startswith("answer_updated") for r in reasons)
          and any(r.startswith("conclusion_retracted") for r in reasons),
          (reasons, reasons2))


def group_content_patch(env, cls, ans):
    section("反例 18：单条补录校验（T7-18）")
    d = detail(env, cls, ans["unfin"])
    st, payload = patch_content(env, cls, ans["unfin"],
                                {"expected_version": "0" * 64, "student_steps": "x=1"})
    check("T7-18.1 版本冲突 → 409 version_conflict",
          st == 409 and payload.get("code") == "version_conflict", (st, jd(payload)))
    st, payload = patch_content(env, cls, ans["unfin"],
                                {"expected_version": d["answer"]["version"], "student_id": 999})
    check("T7-18.2 身份字段不可改 → 400", st == 400, (st, jd(payload)))
    # 先落一条当前结论，补录之后才谈得上「旧结论 stale」
    st0, saved0 = save(env, cls, ans["unfin"], body_for(d))
    check("T7-18.2b 前提：先存一条当前结论（待判断）", st0 == 200 and saved0.get("ok") is True,
          jd(saved0)[:120])
    st, payload = patch_content(env, cls, ans["unfin"],
                                {"expected_version": d["answer"]["version"],
                                 "question_text": "2x+4=10", "student_steps": "2x+4=10\n2x=6\nx=3"})
    check("T7-18.3 合法补录 200 且标 reanalysed",
          st == 200 and payload.get("ok") is True and payload.get("reanalysed") is not False,
          (st, jd(payload)[:200]))
    after = detail(env, cls, ans["unfin"])
    check("T7-18.4 补录后旧结论 stale（回到待核对）",
          after["judgement"]["progress"] == "pending"
          and after["judgement"].get("stale") is True,
          (after["judgement"]["progress"], after["judgement"].get("stale")))
    check("T7-18.5 补录后版本变了", after["answer"]["version"] != d["answer"]["version"],
          (d["answer"]["version"][:8], after["answer"]["version"][:8]))
    ov = overview(env, cls, BATCH)
    check("T7-18.6 补录的这份回到待核对队列", ov["assignment"]["pending"] >= 1, jd(ov["assignment"]))
    # 合成演示作答：T12/c060f65 之后**允许**校正，回执标「预置已失效」，reset 可恢复。
    # 原断言（本轮 T7 第一版）：合成演示作答 → 409 demo_readonly。
    # 原依据：契约 §3.3 第 6 条「合成演示作答是五项全匹配基线，改了预置就失效，请用重置恢复」。
    # 新断言：PATCH 返回 200，且响应里写明预置候选已失效；随后 demo/reset 能恢复到预置有效。
    # 改写依据：commit c060f65「允许校正合成演示作答（返回体标注预置失效）」+ T12 §1 演示数据可重复走完
    #           五条流程（含补录校正）。任务书里「演示记录 409 demo_readonly」这一条已随实现变更作废，
    #           已作为「任务书与实现不一致」上报 captain。
    env.client.post("/api/classes/%d/check/demo/seed" % cls, {})
    demo = env.db.query_one(
        "SELECT id FROM answers WHERE class_id=? AND source=? AND record_key='DEMO-A-01'",
        (cls, D.DEMO_SOURCE))
    dd = detail(env, cls, demo["id"])
    st, payload = patch_content(env, cls, demo["id"],
                                {"expected_version": dd["answer"]["version"],
                                 "student_steps": "3(x-2)=12\n3x-6=12\nx=6"})
    text = json.dumps(payload, ensure_ascii=False)
    check("T7-18.7 合成演示作答现在允许校正，回执写明预置已失效",
          st == 200 and ("预置" in text and ("失效" in text or "重置" in text)), (st, text[:200]))
    broken = detail(env, cls, demo["id"])
    check("T7-18.8 校正后预置候选确实失效（退回规则定位）",
          broken["candidates"] == [] and broken["locate"]["source"] == "rule",
          (broken["candidates"], broken["locate"]["source"]))
    env.client.post("/api/classes/%d/check/demo/reset" % cls, {})
    env.client.post("/api/classes/%d/check/demo/seed" % cls, {})
    restored = env.db.query_one(
        "SELECT id FROM answers WHERE class_id=? AND record_key='DEMO-A-01'", (cls,))
    check("T7-18.9 reset + 重新载入后预置恢复有效（可重复演示）",
          bool(restored) and bool(detail(env, cls, restored["id"])["candidates"]),
          "恢复后候选")
    env.client.post("/api/classes/%d/check/demo/reset" % cls, {})


def group_multi_class(env, cls, ans):
    section("反例 19：多班级多批次隔离（T7-19）")
    cls2 = env.add_class("初二 2 班")
    sid2 = env.add_student(cls2, "学生乙")
    same_key = env.add_answer(cls2, sid2, "R-MOVE", CASE_MOVE, batch=BATCH,
                              student_name="学生乙")
    st_row1 = answer_check_stats(env.db, env.db.query_one("SELECT * FROM classes WHERE id=?", (cls,)))
    st_row2 = answer_check_stats(env.db, env.db.query_one("SELECT * FROM classes WHERE id=?", (cls2,)))
    d = detail(env, cls2, same_key)
    st, saved = save(env, cls2, same_key, body_for(
        d, cause_state="decided", cause_label="移项没有变号", cause_code="E-MOVE-01"))
    check("T7-19.1 另一个班的同名记录能独立保存", st == 200 and saved.get("ok") is True, jd(saved))
    st_after1 = answer_check_stats(env.db, env.db.query_one("SELECT * FROM classes WHERE id=?", (cls,)))
    check("T7-19.2 保存另一个班不影响本班统计",
          st_after1["checked"] == st_row1["checked"], (st_row1["checked"], st_after1["checked"]))
    ov2 = overview(env, cls2, BATCH)
    check("T7-19.3 另一个班的总览只数自己",
          ov2["assignment"]["total"] == 1, jd(ov2["assignment"]))
    status, payload = records(env, cls2, "?batch=%s&cause_state=decided"
                              % urllib.parse.quote(BATCH))
    items = payload.get("items") or []
    check("T7-19.4 记录页按班级隔离",
          bool(items) and all(i.get("answer_id") == same_key for i in items),
          (status, payload.get("total"), [i.get("answer_id") for i in items], same_key))
    status, payload = records(env, cls, "?batch=" + urllib.parse.quote("不存在的批次"))
    check("T7-19.5 批次筛选没有串班", (payload.get("items") or []) == [], jd(payload)[:120])


def group_ai_gate(env, cls, ans):
    section("反例 20：个体作答不得触发外部模型请求（T7-20）")
    status, payload = env.client.get("/api/account/ai-individual")
    check("T7-20.1 网页端不提供外发能力（ai_egress_available=false）",
          status == 200 and payload.get("ai_egress_available") is False, jd(payload))
    check("T7-20.2 个体 AI 默认关闭", payload.get("ai_individual_allowed") is False, jd(payload))
    audits_before = env.db.scalar("SELECT COUNT(*) FROM ai_audit") if env.db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_audit'") else 0
    status, result = env.client.post(
        "/api/classes/%d/answers/%d/review/analyse" % (cls, ans["move"]), {"consent": True})
    provider = ((result or {}).get("review") or {}).get("provider", "")
    check("T7-20.3 分析入口不组装真实外发（provider 是模拟）",
          status in (200, 403) and "模拟" in provider, (status, provider))
    audits_after = env.db.scalar("SELECT COUNT(*) FROM ai_audit") if env.db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_audit'") else 0
    check("T7-20.4 没有产生任何外发审计记录", audits_after == audits_before,
          (audits_before, audits_after))


def group_migration():
    section("反例 21：1.7.x 旧库迁移不再抛 MigrationError（T7-21）")
    work = os.path.join(WORK, "migrate")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    target = os.path.join(work, "app.db")
    shutil.copy2(FIXTURE_160, target)
    conn = sqlite3.connect(target)
    try:
        before = {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "classes": conn.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
            "students": conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
            "answers": conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0],
        }
    finally:
        conn.close()
    from app.db import ensure_answer_check_schema
    error = None
    backup = None
    try:
        backup = ensure_answer_check_schema(target)
    except Exception as exc:  # noqa: BLE001
        error = "%s: %s" % (type(exc).__name__, exc)
    check("T7-21.1 升级不再抛异常（基线是 MigrationError: no such table: answer_followups）",
          error is None, error)
    check("T7-21.2 升级前留了一致性备份",
          bool(backup) and os.path.exists(str(backup)), backup)
    conn = sqlite3.connect(target)
    try:
        after = {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "classes": conn.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
            "students": conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
            "answers": conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0],
        }
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    check("T7-21.3 旧数据一条不丢", before == after, (before, after))
    check("T7-21.4 两张新表都建好了",
          {"answer_checks", "answer_followups"} <= tables, sorted(tables))


def group_receipt_consistency(env, cls, ans):
    section("反例 22：回执与响应/库不得出现「请求值 vs 响应值」矛盾（T7-22）")
    d = detail(env, cls, ans["oos"])
    want_label = "不等式没学"
    st, saved = save(env, cls, ans["oos"], body_for(
        d, cause_state="decided", cause_label=want_label, cause_code="E-OOS-01",
        basis_note="学生说还没学不等式"))
    judgement = saved.get("judgement") or {}
    receipt = saved.get("receipt") or {}
    check("T7-22.1 响应回执里的原因文字与请求一致（不是旧值）",
          judgement.get("cause_label_text") == want_label, judgement.get("cause_label_text"))
    check("T7-22.2 回执三行不出现占位空值",
          all((receipt.get(k) or "").strip() for k in ("class_name", "assignment_name")),
          jd(receipt))
    check("T7-22.3 回执里没有把请求值原样当结果显示（来源是服务端判定）",
          judgement.get("cause_source") == "teacher"
          and judgement.get("verify_source") == "manual",
          (judgement.get("cause_source"), judgement.get("verify_source")))
    status, payload = records(env, cls, "?batch=%s&cause_state=decided"
                              % urllib.parse.quote(BATCH))
    row = next((i for i in payload.get("items") or [] if i.get("answer_id") == ans["oos"]), None)
    check("T7-22.4 记录页那一行的原因文字与保存响应一致",
          row is not None and row.get("cause_label_text") == want_label,
          (status, payload.get("total"), jd(row)[:200]))
    check("T7-22.5 记录页那一行的来源标签与响应一致",
          row is not None and row.get("cause_source") == "teacher", jd(row)[:200])


def group_mutant_selfcheck():
    section("第 23 组：反向自证 —— 变异体必须让本套件的关键断言变假（T7-M）")
    src = os.path.join(ROOT, "src")
    work = os.path.join(WORK, "mutant")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    shutil.copytree(src, os.path.join(work, "src"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    targets = [
        # stats.py 从 T6 起用参数化写法：`demo_source = check_state.DEMO_SOURCE` 再 ` AND a.source<>?`。
        # 变异体把「被排除的来源」换成一个不存在的值 —— 等价于「没人被排除」，参数个数不变，
        # 于是演示行会被数进真实统计（T7-06.3 必须因此变红）。
        ("统计过滤", os.path.join(work, "src", "app", "stats.py"),
         "demo_source = check_state.DEMO_SOURCE", 'demo_source = "__t7_mutant_none__"',
         "T7-06.3"),
        ("依据闸门", os.path.join(work, "src", "app", "answer_check.py"),
         "    if not basis:", "    if False and not basis:", "T7-09.1"),
    ]
    for name, path, old, new, ref in targets:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        hits = text.count(old)
        check("T7-M.%s 变异锚点在 %s 里恰好出现 1 次（改一处只影响一处）" % (name, os.path.basename(path)),
              hits == 1, hits)
        if hits != 1:
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(old, new, 1))
        script = os.path.join(work, "run_mutant.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(MUTANT_BODY)
        env_vars = dict(os.environ)
        env_vars["T7_MUTANT_ROOT"] = work
        out_file = os.path.join(work, "mutant-output.txt")
        env_vars["PYTHONIOENCODING"] = "utf-8"
        with open(out_file, "w", encoding="utf-8") as sink:
            proc = subprocess.run([sys.executable, script], stdout=sink, stderr=subprocess.STDOUT,
                                  env=env_vars, timeout=180)
        with open(out_file, "r", encoding="utf-8", errors="replace") as fh:
            output = fh.read()
        check("T7-M.%s 摘掉「%s」后同一批断言确实变红（断言不恒真）" % (name, old.strip()),
              proc.returncode != 0 and "MUTANT_RED" in output,
              output.strip().splitlines()[-4:] if output.strip() else "无输出")


# 变异体里跑的极小子集：只验「被摘掉的那道闸门」对应的两条断言
MUTANT_BODY = r'''# -*- coding: utf-8 -*-
import json, os, shutil, sys, threading, tempfile, urllib.request, urllib.error, http.cookiejar, traceback
ROOT = os.environ["T7_MUTANT_ROOT"]

def say(*parts):
    print(*parts)
    sys.stdout.flush()

try:
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import server as S
    from app.answers import fingerprint
    from app.db import Database, now_iso
    from app.stats import answer_check_stats

    work = os.path.join(tempfile.gettempdir(), "t7-mutant-run")
    shutil.rmtree(work, ignore_errors=True); os.makedirs(work)
    srv = S.make_server("127.0.0.1", 0, work, use_demo=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def req(method, path, body=None):
        data = json.dumps(body or {}).encode() if method != "GET" else None
        r = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with op.open(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try: return exc.code, json.loads(raw or "{}")
            except ValueError: return exc.code, {"raw": raw}

    req("POST", "/api/auth/register", {"username": "m", "password": "pw123456"})
    db = Database(os.path.join(work, "app.db"), create=False)
    uid = db.scalar("SELECT id FROM users WHERE username='m'")
    stamp = now_iso()
    with db.tx() as conn:
        cur = conn.execute("INSERT INTO classes(user_id,name,grade,note,created_at,updated_at) VALUES (?,?,?,?,?,?)", (uid, "初一 1 班", "七年级", "", stamp, stamp)); cls = cur.lastrowid
        cur = conn.execute("INSERT INTO students(class_id,name,code,note,active,created_at,updated_at) VALUES (?,?,?,?,1,?,?)", (cls, "学生甲", "", "", stamp, stamp)); sid = cur.lastrowid

    def add(key, q, std, steps, batch="9月第2次作业", source="真实导入"):
        row = {"question_no": "1", "question_text": q, "unit": "一元一次方程", "standard_solution": std, "student_steps": steps, "occurred_on": "2026-11-24", "missing": 0, "missing_reason": ""}
        fp = fingerprint(row, sid)
        with db.tx() as conn:
            cur = conn.execute("INSERT INTO answers(class_id,student_id,record_key,batch,student_name,student_code,question_no,question_text,unit,standard_solution,student_steps,source,occurred_on,missing,missing_reason,fingerprint,imported_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (cls, sid, key, batch, "学生甲", "", "1", q, "一元一次方程", std, steps, source, "2026-11-24", 0, "", fp, stamp, stamp, stamp))
        return cur.lastrowid

    oos = add("R-OOS", "2x+3>7", "2x+3>7；2x>4；x>2", "2x+3>7\n2x>4\nx>2")
    st, d = req("GET", "/api/classes/%d/check/answers/%d" % (cls, oos))
    body = {"expected_version": d["answer"]["version"], "observed_step": "", "first_bad_index": None,
            "cause_state": "decided", "cause_code": "E-OOS-01", "cause_label": "不等式没学", "teacher_note": ""}
    st_save, payload = req("POST", "/api/classes/%d/check/answers/%d/save" % (cls, oos), body)
    cls_row = db.query_one("SELECT * FROM classes WHERE id=?", (cls,))
    st_before = answer_check_stats(db, cls_row)
    add("DEMO-LEGACY-1", "3(x-2)=12", "3(x-2)=12；3x-6=12；3x=18；x=6", "3(x-2)=12\n3x-6=12\n3x=18\nx=6", source="合成演示")
    st_after = answer_check_stats(db, cls_row)
    say(json.dumps({"save_status": st_save, "save_code": (payload or {}).get("code"),
                    "expected_before": st_before["expected"], "expected_after": st_after["expected"]},
                   ensure_ascii=False))
    basis_still_ok = (st_save == 400)
    stats_still_ok = (st_before["expected"] == st_after["expected"])
    if basis_still_ok and stats_still_ok:
        say("MUTANT_BLIND 两条都没变红，变异体没能证明断言可假")
        sys.exit(0)
    say("MUTANT_RED 至少一条变红：basis_still_ok=%s stats_still_ok=%s" % (basis_still_ok, stats_still_ok))
    sys.exit(1)
except Exception as exc:
    traceback.print_exc()
    say("MUTANT_RED_HARNESS 变异体运行出错（也算「不是恒真」，但要看清原因）：%s" % exc)
    sys.exit(2)
'''


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("T7 R1 反例测试（22 条必覆盖反例 + 反向自证）")
    log("时间：%s" % time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    log("目录：%s" % ROOT)
    log("Python：%s" % sys.version.replace("\n", " "))
    log("")

    env = Env("main")
    cls = env.add_class("初一 1 班")
    stu = env.add_student(cls, "学生甲", "S001")
    ans = {
        "cls": cls,
        "student": stu,
        "move": env.add_answer(cls, stu, "R-MOVE", CASE_MOVE),
        "bracket": env.add_answer(cls, stu, "R-BRACKET", CASE_BRACKET),
        "bracket2": env.add_answer(cls, stu, "R-BRACKET-2", CASE_BRACKET),
        "ok": env.add_answer(cls, stu, "R-OK", CASE_OK),
        "unfin": env.add_answer(cls, stu, "R-UNFIN", CASE_UNFIN),
        "oos": env.add_answer(cls, stu, "R-OOS", CASE_OOS),
        "miss": env.add_answer(cls, stu, "R-MISS", CASE_MISS),
    }

    try:
        group_evidence_pollution(env, cls, ans)
        group_normalized_question(env, cls, ans)
        group_batch_collision(env, cls, ans)
        group_seed_students(env, cls, ans)
        group_demo_not_counted(env, cls, ans)
        group_no_triggers(env, cls, ans)
        group_cause_source(env, cls, ans)
        group_forbidden(env, cls, ans)
        group_oos_basis(env, cls, ans)
        group_no_silent_fix(env, cls, ans)
        group_eleven_items(env, cls, ans)
        group_progress_rollback(env, cls, ans)
        group_aba(env, cls, ans)
        group_unfinished(env, cls, ans)
        group_followup_kind(env, cls, ans)
        group_followup_unavailable(env, cls, ans)
        group_followup_invalidation(env, cls, ans)
        group_content_patch(env, cls, ans)
        group_multi_class(env, cls, ans)
        group_ai_gate(env, cls, ans)
        group_migration()
        group_receipt_consistency(env, cls, ans)
        group_mutant_selfcheck()
    finally:
        for item in ENVS:
            item.close()

    total = PASSED[0] + FAILED[0]
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (total, PASSED[0], FAILED[0]))
    code = 0 if FAILED[0] == 0 else 1
    log("退出码：%d" % code)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
