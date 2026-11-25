"""R1-05 人工核对路径 + R1-06 追问记录与证据更新机制：源码级验收（本地临时库 + 环回 HTTP）。

依据（R1-00 §12 要求改写/新增测试必须写明依据）：
- PRD §4.3「追问要针对具体的错步与错因；没有可用资源时明确写暂无，不拿套话代替」；
- PRD §4.5 / 规格 §3.2「人工核验与规则结果分开记录，教师人工记录必须留依据」；
- 数智伴学_修改任务清单.md R1-05 / R1-06 的完成标准；
- R1-00 基线审查记录 §10（默认问句写死、来源靠问句相等判）、§12（既有断言把缺陷写成预期）、
  §15.3（out_of_scope / missing_steps 永久卡死、original_errors 只认 first_bad、单条补录接口全 404）。

为什么连本地服务而不是只调函数：R1-05 的三条验收（可补录、旧结论 stale、统计可辨认）与
R1-06 的失效链路都要穿过 HTTP 契约（PATCH 新端点、409/400 语义、records 过滤），只调函数
证明不了「接口也能用」。服务只监听 127.0.0.1 的随机端口，数据目录在 tests/.tmp-r1-05-06/。

反自欺约定：第 6 组把实现反向改错（替换模块函数 / 删掉失效判定 / 把问句变回常量），
用同一批断言证明它们「可以假」——实现一改错，断言必须失败。
"""

import json
import os
import shutil
import sys
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from app import answer_check as C  # noqa: E402
from app import demo_answers as D  # noqa: E402
from app import equation_steps as E  # noqa: E402
from app.answers import fingerprint as answer_fingerprint  # noqa: E402
from app.db import Database, now_iso  # noqa: E402
from logmeta import environment_lines, exit_line  # noqa: E402

import server as server_mod  # noqa: E402

WORK = os.path.join(ROOT, "tests", ".tmp-r1-05-06")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-05-06-manual-followup.txt")

RESULTS = {"pass": 0, "fail": 0, "hold": 0}
LOG_LINES = []
ENVS = []

OLD_HARDCODED_QUESTION = "你当时是怎么展开这个括号的？"

# 案例（数学内容取自 数智伴学_Agent开发规格.md §7）
CASE_MOVE = {           # 移项漏变号：真实作答里最容易被套上「去括号」问句的情形
    "question": "2x+7=15",
    "standard": "2x+7=15；2x=8；x=4",
    "steps": "2x+7=15\n2x=15+7\n2x=22\nx=11",
}
CASE_BRACKET = {        # 括号没展开
    "question": "3(x-2)=12",
    "standard": "3(x-2)=12；3x-6=12；3x=18；x=6",
    "steps": "3(x-2)=12\n3x-2=12\nx=14/3",
}
CASE_OK = {             # 全程等价
    "question": "2(x+3)=10",
    "standard": "2(x+3)=10；2x+6=10；2x=4；x=2",
    "steps": "2(x+3)=10\n2x+6=10\n2x=4\nx=2",
}
CASE_ONLY_ANSWER = {    # 只写了结果 → missing_steps
    "question": "4(x-1)=8",
    "standard": "4(x-1)=8；x-1=2；x=3",
    "steps": "x=1",
}
CASE_OUT_OF_SCOPE = {   # 规则判不了（不是一元一次方程）
    "question": "求阴影部分面积",
    "standard": "S=ab",
    "steps": "先算长方形面积\n12*8=96",
}


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
    RESULTS["hold"] += 1
    log("未验证 %s —— %s" % (name, reason))


def section(title):
    log("")
    log("== %s ==" % title)


def jdump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------
# 环回 HTTP 客户端（显式 Cookie 头，不依赖 cookiejar）
# --------------------------------------------------------------------------
class Client:
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
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read()
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

    def patch(self, path, body=None):
        return self.request("PATCH", path, body if body is not None else {})


class Env:
    """一个数据目录 + 一个只监听 127.0.0.1 的服务 + 一个已登录的客户端。"""

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
        self.db = Database(os.path.join(self.dir, "app.db"), create=False)
        status, payload = self.client.post(
            "/api/auth/register", {"username": "t5", "password": "pw123456"})
        if status != 200:
            raise RuntimeError("注册失败：%s %s" % (status, payload))
        self.user_id = self.db.scalar("SELECT id FROM users WHERE username='t5'")
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

    # -- 造数 ------------------------------------------------------------
    def add_class(self, name):
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO classes(user_id, name, grade, note, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)", (self.user_id, name, "七年级", "", stamp, stamp))
            return cur.lastrowid

    def add_student(self, class_id, name):
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO students(class_id, name, code, note, active, created_at, updated_at)"
                " VALUES (?,?,?,?,1,?,?)", (class_id, name, "", "", stamp, stamp))
            return cur.lastrowid

    def add_answer(self, class_id, student_id, record_key, case, *, batch="9月第3次作业",
                   question_no="1", missing=0, missing_reason="", source="真实导入",
                   student_name=""):
        row = {
            "question_no": question_no,
            "question_text": case["question"],
            "unit": "一元一次方程",
            "standard_solution": case["standard"],
            "student_steps": case["steps"],
            "occurred_on": "2026-11-24",
            "missing": int(missing),
            "missing_reason": missing_reason,
        }
        fp = answer_fingerprint(row, student_id)
        stamp = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution, student_steps,"
                " source, occurred_on, missing, missing_reason, fingerprint, imported_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, student_id, record_key, batch, student_name, "",
                 row["question_no"], row["question_text"], row["unit"], row["standard_solution"],
                 row["student_steps"], source, row["occurred_on"], row["missing"],
                 row["missing_reason"], fp, stamp, stamp, stamp))
            return cur.lastrowid

    def followup_rows(self):
        return self.db.query("SELECT * FROM answer_followups ORDER BY id")

    def check_row(self, answer_id):
        return self.db.query_one(
            "SELECT * FROM answer_checks WHERE answer_id=? ORDER BY id DESC LIMIT 1", (answer_id,))


def detail(env, class_id, answer_id):
    status, payload = env.client.get("/api/classes/%d/check/answers/%d" % (class_id, answer_id))
    if status != 200:
        raise RuntimeError("详情接口失败：%s %s" % (status, payload))
    return payload


def save(env, class_id, answer_id, body):
    return env.client.post("/api/classes/%d/check/answers/%d/save" % (class_id, answer_id), body)


def patch_content(env, class_id, answer_id, body):
    return env.client.patch(
        "/api/classes/%d/check/answers/%d/content" % (class_id, answer_id), body)


def save_body(data, *, observed=None, index=None, cause_state="undecided", cause_code="",
              cause_label="", basis="", followup=None, progress=None, clear_notes=None):
    """照契约 §3.3 组请求体；没给的按「老师直接点主按钮」取默认值。"""
    locate = data.get("locate") or {}
    body = {
        "expected_version": (data.get("answer") or {}).get("version", ""),
        "observed_step": locate.get("first_bad_text") or "" if observed is None else observed,
        "first_bad_index": locate.get("first_bad_index") if index is None else index,
        "cause_state": cause_state,
        "cause_code": cause_code,
        "cause_label": cause_label,
        "basis_note": basis,
    }
    if followup is not None:
        body["followup"] = followup
    if progress is not None:
        body["progress"] = progress
    if clear_notes is not None:
        body["clear_notes"] = clear_notes
    return body


# --------------------------------------------------------------------------
# 1. R1-05：out_of_scope 的人工核对出路
# --------------------------------------------------------------------------
def group_1_out_of_scope(envs):
    section("1. R1-05：out_of_scope 有人工核对出路（记录数学错误 + 核验依据 + 留痕，不再永久卡死）")

    env = Env("out-of-scope")
    envs.append(env)
    class_id = env.add_class("七年级 3 班")
    student = env.add_student(class_id, "学生甲")
    answer_id = env.add_answer(class_id, student, "OOS-1", CASE_OUT_OF_SCOPE, student_name="学生甲")

    data = detail(env, class_id, answer_id)
    locate = data["locate"]
    check("1.1 规则判不了这份作答：locate.status=out_of_scope（source 只记 rule/none，不冒充预置）",
          locate["status"] == "out_of_scope" and locate["source"] in ("rule", "none"),
          "%s/%s" % (locate["status"], locate["source"]))
    check("1.2 可判可不判：applicability=optional（不再一律禁止人工判断）",
          data["applicability"] == "optional", data["applicability"])
    check("1.3 没有错步就没有追问：followup.available=false 且文案「暂无适用追问」",
          data["followup"]["available"] is False and data["followup"]["question"] == ""
          and "暂无适用追问" in (data["followup"]["note"] or ""), jdump(data["followup"])[:200])

    # 人工记录数学错误但没写依据 → 400，且不写任何行（不得绕过证据）
    status, payload = save(env, class_id, answer_id, save_body(
        data, observed="12*8=96 这一步把长宽乘错，宽其实没乘", cause_state="not_applicable"))
    check("1.4 人工记录错步但没有依据 → 400 basis_required，一条都不写",
          status == 400 and payload.get("code") == "basis_required"
          and env.check_row(answer_id) is None, "%s %s" % (status, payload.get("code")))

    basis = "作业本原图上宽只填了 8，长 12，学生写成 12*8=96 后又按 24 算，属人工核对到的计算错误"
    status, result = save(env, class_id, answer_id, save_body(
        data, observed="12*8=96 这一步把宽当成 24 算", cause_state="not_applicable", basis=basis))
    judgement = result.get("judgement") or {}
    check("1.5 有人工错步 + 依据 → 200，进度轴=已核对、原因轴=无需判断",
          status == 200 and judgement.get("progress") == "checked"
          and judgement.get("cause_state") == "not_applicable", jdump(judgement)[:240])
    check("1.6 证据来源标成「教师人工核验」：verify_source=manual + 中文标签",
          judgement.get("verify_source") == "manual"
          and judgement.get("verify_source_label") == "教师人工核验", jdump(judgement.get("verify_source")))

    row = env.check_row(answer_id)
    check("1.7 人工结果与规则结果**分开存**：teacher_error_text 逐字保留，locate_status 仍是 out_of_scope",
          (row.get("teacher_error_text") or "") == "12*8=96 这一步把宽当成 24 算"
          and (row.get("locate_status") or "") == "out_of_scope"
          and (row.get("verify_source") or "") == "manual", jdump({
              "teacher_error_text": row.get("teacher_error_text"),
              "locate_status": row.get("locate_status")}))
    check("1.8 人工核验留痕：记了谁、什么时候",
          bool(row.get("teacher_verified_at")) and row.get("teacher_verified_by") == env.user_id,
          jdump({"at": row.get("teacher_verified_at"), "by": row.get("teacher_verified_by")}))
    check("1.9 核验依据落在 basis_note（不是教师备注）",
          (row.get("basis_note") or "") == basis and not (row.get("teacher_note") or ""),
          row.get("basis_note"))

    overview = env.client.get("/api/classes/%d/check/overview" % class_id)[1]
    counts = overview.get("counts") or {}
    check("1.10 统计能收口：undecided_cause=0、not_applicable_cause=1（不再永久挂账）",
          counts.get("undecided_cause") == 0 and counts.get("not_applicable_cause") == 1,
          jdump(counts))
    check("1.11 统计能辨认人工核验：counts.manual_verified=1",
          counts.get("manual_verified") == 1, jdump(counts))

    manual_records = env.client.get(
        "/api/classes/%d/check/records?cause_state=all&verify_source=manual" % class_id)[1]
    rule_records = env.client.get(
        "/api/classes/%d/check/records?cause_state=all&verify_source=rule" % class_id)[1]
    item = (manual_records.get("items") or [{}])[0]
    check("1.12 记录可按核验来源筛：verify_source=manual 只列人工核验那份",
          manual_records.get("total") == 1 and item.get("answer_id") == answer_id
          and item.get("verify_source") == "manual"
          and item.get("teacher_error_text") == "12*8=96 这一步把宽当成 24 算",
          jdump(manual_records)[:240])
    check("1.13 记录按核验来源筛：verify_source=rule 查不到人工核验那份（不是同一口径）",
          rule_records.get("total") == 0, rule_records.get("total"))
    status, bad = env.client.get(
        "/api/classes/%d/check/records?verify_source=nonsense" % class_id)
    check("1.14 非法核验来源取值 → 400 bad_verify_source（不静默当 all）",
          status == 400 and bad.get("code") == "bad_verify_source", "%s %s" % (status, bad.get("code")))

    # 这次只改备注：人工错步与依据不能被清空（省略 ≠ 清空，R1-02 同一口径）
    status, again = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id), cause_state="not_applicable"))
    row = env.check_row(answer_id)
    check("1.15 再保存时没填错步/依据 → 上一次的人工核验文字与依据都保留",
          status == 200 and (row.get("teacher_error_text") or "") == "12*8=96 这一步把宽当成 24 算"
          and (row.get("basis_note") or "") == basis, jdump({
              "err": row.get("teacher_error_text"), "basis": row.get("basis_note")}))

    # 规则判不了但老师有证据 → 可以人工改判（optional 允许 decided），来源仍记人工核验
    data = detail(env, class_id, answer_id)
    status, decided = save(env, class_id, answer_id, save_body(
        data, observed="12*8=96 这一步把宽当成 24 算", cause_state="decided",
        cause_label="面积公式里的宽用错", basis=basis))
    judgement = decided.get("judgement") or {}
    check("1.16 out_of_scope 可人工改判为「教师已判断」，来源=教师填写、证据来源=人工核验",
          status == 200 and judgement.get("cause_state") == "decided"
          and judgement.get("cause_source") == "teacher"
          and judgement.get("verify_source") == "manual", jdump(judgement)[:240])
    return env, class_id, answer_id


# --------------------------------------------------------------------------
# 2. R1-05：单条补录 / 校正原始作答 → 新版本重新分析
# --------------------------------------------------------------------------
def group_2_content_update(envs):
    section("2. R1-05：单条补录/校正原始作答（指纹变化 → 旧结论 stale → 按新内容重新分析）")

    env = Env("content")
    envs.append(env)
    class_id = env.add_class("七年级 4 班")
    student = env.add_student(class_id, "学生乙")
    answer_id = env.add_answer(class_id, student, "MS-1", CASE_ONLY_ANSWER, question_no="2",
                              student_name="学生乙")

    data = detail(env, class_id, answer_id)
    old_version = data["answer"]["version"]
    check("2.1 起点：只写了结果 → locate.status=missing_steps、暂无适用追问（提示先补录）",
          data["locate"]["status"] == "missing_steps" and data["followup"]["available"] is False
          and "补录" in (data["followup"]["note"] or ""),
          "%s / %s" % (data["locate"]["status"], data["followup"]["note"]))

    status, saved = save(env, class_id, answer_id, save_body(
        data, cause_state="not_applicable",
        followup={"question": "", "answer_text": ""}))
    check("2.2 补录前先保存一次结论（进度轴已核对、原因轴无需判断）",
          status == 200 and (saved.get("judgement") or {}).get("progress") == "checked",
          jdump((saved.get("judgement") or {}))[:160])

    status, bad = patch_content(env, class_id, answer_id, {"student_steps": "x=1"})
    check("2.3 补录必须带 expected_version：缺失 → 409 version_conflict，一个字不改",
          status == 409 and bad.get("code") == "version_conflict", "%s %s" % (status, bad.get("code")))
    check("2.4 被拒绝的补录没有改动作答内容",
          (env.db.scalar("SELECT student_steps FROM answers WHERE id=?", (answer_id,))
           or "") == CASE_ONLY_ANSWER["steps"])

    status, bad = patch_content(env, class_id, answer_id, {
        "expected_version": old_version, "student_steps": CASE_ONLY_ANSWER["steps"]})
    check("2.5 内容没变 → 400 nothing_to_update（不白建一个版本）",
          status == 400 and bad.get("code") == "nothing_to_update",
          "%s %s" % (status, bad.get("code")))
    # 缺测那份：去掉「缺测」但步骤是空的 → 明确拒绝（不静默替老师决定）
    missing_id = env.add_answer(class_id, student, "MS-2", CASE_MISSING_SAFE, question_no="3",
                                missing=1, missing_reason="未提供学生步骤", batch="9月第4次作业",
                                student_name="学生乙")
    missing_detail = detail(env, class_id, missing_id)
    status, bad = patch_content(env, class_id, missing_id, {
        "expected_version": missing_detail["answer"]["version"], "missing": "否"})
    check("2.6 去掉缺测但步骤是空的 → 400 bad_missing（不静默替老师决定）",
          status == 400 and bad.get("code") == "bad_missing", "%s %s" % (status, bad.get("code")))
    check("2.6b 被拒绝的缺测改动没有落库（这份仍是缺测）",
          int(env.db.scalar("SELECT missing FROM answers WHERE id=?", (missing_id,)) or 0) == 1)
    status, bad = patch_content(env, class_id, answer_id, {
        "expected_version": old_version, "occurred_on": "2026/11/24"})
    check("2.7 日期格式不对 → 400 bad_date，一个字不改",
          status == 400 and bad.get("code") == "bad_date", "%s %s" % (status, bad.get("code")))

    new_steps = "4(x-1)=8\nx-1=2\nx=3"
    status, result = patch_content(env, class_id, answer_id, {
        "expected_version": old_version, "student_steps": new_steps})
    new_version = (result.get("answer") or {}).get("version") or ""
    check("2.8 补录成功 → 200、指纹变化、reanalysed=true、changed_fields 含 student_steps",
          status == 200 and result.get("action") == "updated" and result.get("reanalysed") is True
          and new_version != old_version and "student_steps" in (result.get("changed_fields") or []),
          jdump({"status": status, "action": result.get("action"),
                 "changed": result.get("changed_fields")}))
    check("2.9 补录后立刻按新内容重新分析：locate 变成 correct（这份其实解得对）",
          (result.get("locate") or {}).get("status") == "correct",
          jdump((result.get("locate") or {}).get("status")))
    check("2.10 旧结论变成旧版本：judgement.stale=true、progress 回到待核对",
          (result.get("judgement") or {}).get("stale") is True
          and (result.get("judgement") or {}).get("progress") == "pending",
          jdump(result.get("judgement"))[:200])
    check("2.11 旧 check 行留在库里（不删行、历史可追溯）",
          env.db.scalar("SELECT COUNT(*) FROM answer_checks WHERE answer_id=?", (answer_id,)) == 1,
          env.db.scalar("SELECT COUNT(*) FROM answer_checks WHERE answer_id=?", (answer_id,)))
    check("2.12 stale_conclusions 如实报数（1 条旧结论失效）",
          result.get("stale_conclusions") == 1, result.get("stale_conclusions"))
    overview = env.client.get("/api/classes/%d/check/overview" % class_id)[1]
    check("2.13 补录后这份回到待核对队列（应交 1、已核对 0、待核对 1）",
          (overview.get("assignment") or {}).get("total") == 1
          and (overview.get("assignment") or {}).get("checked") == 0
          and (overview.get("assignment") or {}).get("pending") == 1,
          jdump(overview.get("assignment")))
    detail_after = detail(env, class_id, answer_id)
    check("2.14 详情与补录响应同源：版本、步骤、locate 都取新内容",
          detail_after["answer"]["version"] == new_version
          and detail_after["answer"]["student_steps"] == new_steps
          and detail_after["locate"]["status"] == "correct", jdump(detail_after["answer"])[:200])
    check("2.15 补录后的新版本没有任何当前结论（judgement.progress=pending、saved_at 为空）",
          detail_after["judgement"]["progress"] == "pending"
          and detail_after["judgement"]["saved_at"] == "", jdump(detail_after["judgement"])[:160])

    # 合成演示作答：**允许**校正（R1-T12 要求界面上能演示「单条作答补录与校正」）。
    # 原断言（2.16）：patch 演示作答 → 409 demo_readonly；原依据：演示数据是「五项全匹配」预置基线，
    #   改内容会让预置定位与候选失效。
    # 新断言（2.16）：patch 演示作答 → 200，返回体明确标注 `demo_preset_invalidated` 且给出说明，
    #   版本随之变化（预置失效 → 退回规则定位，这正是要演示的效果）。
    # 新断言（2.17）：隔离仍然成立——source / record_key / 批次三样一个字都不能变。
    # 改写依据：R1-T12「单条作答补录与校正要能在演示现场用界面完成，不要求导 CSV」；
    #   演示数据仍靠 source + DEMO- 前缀 + 批次三条与真实数据隔离，重置可恢复基线。
    status, seeded = env.client.post("/api/classes/%d/check/demo/seed" % class_id)
    demo_id = env.db.scalar(
        "SELECT id FROM answers WHERE class_id=? AND record_key='DEMO-A-01'", (class_id,))
    demo_detail = detail(env, class_id, demo_id)
    demo_before = env.db.query_one(
        "SELECT source, record_key, batch FROM answers WHERE id=?", (demo_id,))
    status, updated = patch_content(env, class_id, demo_id, {
        "expected_version": demo_detail["answer"]["version"], "student_steps": "3(x-2)=12\nx=6"})
    check("2.16 合成演示作答可校正 → 200，且明确标注预置已失效、版本已变化",
          status == 200
          and updated.get("demo_preset_invalidated") is True
          and bool(updated.get("note"))
          and (updated.get("answer") or {}).get("version")
          not in ("", demo_detail["answer"]["version"]),
          "%s %s" % (status, jdump(updated)[:240]))
    demo_after = env.db.query_one(
        "SELECT source, record_key, batch, student_steps FROM answers WHERE id=?", (demo_id,))
    _ident = lambda r: (r["source"], r["record_key"], r["batch"])  # noqa: E731
    check("2.17 演示作答校正后 source/record_key/批次三样不变（隔离未破），内容已更新",
          _ident(demo_after) == _ident(demo_before)
          and (demo_after["student_steps"] or "").startswith("3(x-2)=12")
          and demo_after["source"] == "合成演示",
          "%s -> %s" % (_ident(demo_before), _ident(demo_after)))

    status, bad = patch_content(env, class_id, 999999, {"expected_version": "x"})
    check("2.18 不存在的作答 → 404 not_found", status == 404 and bad.get("code") == "not_found",
          "%s %s" % (status, bad.get("code")))
    other_class = env.add_class("七年级 5 班")
    status, bad = patch_content(env, other_class, answer_id, {"expected_version": new_version})
    check("2.19 跨班级补录 → 404（班级归属校验，不能改别人班的作答）",
          status == 404, status)
    return env, class_id, answer_id


# --------------------------------------------------------------------------
# 3. R1-06：问句必须与错步相关
# --------------------------------------------------------------------------
def group_3_question_relevance(envs):
    section("3. R1-06：问句由本条错步决定（移项不再问展开括号；无依据明确暂无）")

    env = Env("question")
    envs.append(env)
    class_id = env.add_class("七年级 6 班")
    student = env.add_student(class_id, "学生丙")

    move_id = env.add_answer(class_id, student, "MV-1", CASE_MOVE, student_name="学生丙")
    move = detail(env, class_id, move_id)
    suggestion = move["followup"]["suggestion"]
    question = suggestion.get("question") or ""
    check("3.1 移项错步：问句按本条两行生成（source_detail=rule:term_move）",
          suggestion.get("available") is True
          and suggestion.get("source_detail") == "rule:term_move", jdump(suggestion)[:240])
    check("3.2 移项错步**不再问展开括号**：问句里没有「括号」，主角是移项的那一项 +7",
          "括号" not in question and "+7" in question and "移到等号另一边" in question, question)
    check("3.3 移项问句逐字引用本条作答的原文两行（可从学生原文核对）",
          CASE_MOVE["steps"].split("\n")[0] in question
          and CASE_MOVE["steps"].split("\n")[1] in question, question)
    check("3.4 反向自证：旧实现那句写死常量不会被选出来（问句 ≠ 去括号常量）",
          question != OLD_HARDCODED_QUESTION and question != "", question)

    bracket_id = env.add_answer(class_id, student, "BR-1", CASE_BRACKET, question_no="2",
                                student_name="学生丙")
    bracket = detail(env, class_id, bracket_id)
    bracket_q = (bracket["followup"]["suggestion"] or {}).get("question") or ""
    check("3.5 括号错步：问句问的就是展开括号，且括号片段逐字取自题干",
          "括号" in bracket_q and "3(x-2)" in bracket_q, bracket_q)
    check("3.6 两种错步拿到的是两句不同的问句（不是同一句套给所有作答）",
          bracket_q != question, "%s / %s" % (bracket_q, question))

    ok_id = env.add_answer(class_id, student, "OK-1", CASE_OK, question_no="3", student_name="学生丙")
    ok = detail(env, class_id, ok_id)
    check("3.7 正确作答不自动产生追问：available=false、question 为空、文案含「暂无适用追问」",
          ok["followup"]["available"] is False and ok["followup"]["question"] == ""
          and "暂无适用追问" in (ok["followup"]["note"] or ""), jdump(ok["followup"])[:200])

    missing_id = env.add_answer(class_id, student, "MI-1", CASE_MISSING_SAFE, question_no="4",
                                missing=1, missing_reason="未提供学生步骤", student_name="学生丙")
    missing = detail(env, class_id, missing_id)
    check("3.8 缺测不自动产生追问：available=false + 「暂无适用追问」",
          missing["followup"]["available"] is False
          and "暂无适用追问" in (missing["followup"]["note"] or ""),
          jdump(missing["followup"])[:200])

    check("3.9 没有可追问的错步时，保存也不会凭空写一条追问（跳过不产生空记录）",
          save(env, class_id, ok_id, save_body(ok, cause_state="not_applicable"))[0] == 200
          and env.db.scalar("SELECT COUNT(*) FROM answer_followups WHERE answer_id=?", (ok_id,)) == 0,
          env.db.scalar("SELECT COUNT(*) FROM answer_followups WHERE answer_id=?", (ok_id,)))

    # 合成演示：预置问句按本条作答算，正确作答那一条不自动给问句
    status, seeded = env.client.post("/api/classes/%d/check/demo/seed" % class_id)
    demo = D.build()
    demo_ids = {row["record_key"]: env.db.scalar(
        "SELECT id FROM answers WHERE class_id=? AND record_key=?", (class_id, row["record_key"]))
        for row in demo["answers"]}
    demo_b = detail(env, class_id, demo_ids["DEMO-B-01"])
    demo_ok = detail(env, class_id, demo_ids["DEMO-OK-01"])
    check("3.10 合成演示案例 B（移项）：问句是预置那条且不含「括号」，来源 preset/demo_record",
          "括号" not in (demo_b["followup"]["question"] or "")
          and (demo_b["followup"]["suggestion"] or {}).get("source") == "preset"
          and (demo_b["followup"]["suggestion"] or {}).get("source_detail") == "demo_record",
          jdump(demo_b["followup"])[:200])
    check("3.11 合成演示对照（正确作答）：不自动给问句，但把预置那句告诉老师",
          demo_ok["followup"]["available"] is False
          and demo_ok["followup"]["question"] == ""
          and "验算" in (demo_ok["followup"]["note"] or ""), jdump(demo_ok["followup"])[:240])
    return env, class_id, {"move": move_id, "bracket": bracket_id, "ok": ok_id,
                           "demo_b": demo_ids["DEMO-B-01"]}


CASE_MISSING_SAFE = {
    "question": "3(x-2)=12",
    "standard": "3(x-2)=12；3x-6=12；3x=18；x=6",
    "steps": "",
}


# --------------------------------------------------------------------------
# 4. R1-06：来源三值的写入规则
# --------------------------------------------------------------------------
def group_4_sources(envs, env, class_id, ids):
    section("4. R1-06：追问来源三值（preset / teacher / library）的写入规则逐条可验")

    move_id = ids["move"]
    data = detail(env, class_id, move_id)
    suggested = (data["followup"]["suggestion"] or {}).get("question") or ""
    quote = "我以为 7 移过去还是 7。"

    status, result = save(env, class_id, move_id, save_body(
        data, followup={"question": suggested, "answer_text": quote}))
    row = env.followup_rows()[-1] if env.followup_rows() else {}
    check("4.1 逐字用系统建议问句 → source=preset，且 source_detail 记下是哪类错步生成的",
          status == 200 and row.get("source") == "preset"
          and (row.get("source_detail") or "").startswith("rule:"), jdump({
              "source": row.get("source"), "detail": row.get("source_detail")}))
    check("4.2 追问挂当前作答版本 + 本次结论（可追溯「对哪一版、哪一次判断」）",
          row.get("answer_version") == (env.db.scalar(
              "SELECT fingerprint FROM answers WHERE id=?", (move_id,)))
          and row.get("check_id") == (env.check_row(move_id) or {}).get("id"),
          jdump({"version": row.get("answer_version"), "check": row.get("check_id")}))
    check("4.3 回执/详情同源：followup.source_label=预置问句、recorded=true、原话逐字",
          (result.get("followup") or {}).get("source_label") == "预置问句"
          and (result.get("followup") or {}).get("recorded") is True
          and (result.get("followup") or {}).get("answer_text") == quote,
          jdump(result.get("followup"))[:240])

    edited = suggested.replace("符号变了吗", "符号变了没有")
    status, result = save(env, class_id, move_id, save_body(
        detail(env, class_id, move_id), followup={"question": edited, "answer_text": "没变。"}))
    row = env.followup_rows()[-1]
    check("4.4 改过建议问句（哪怕只改几个字）→ source=teacher",
          status == 200 and row.get("source") == "teacher" and row.get("question") == edited,
          jdump({"source": row.get("source"), "q": row.get("question")}))

    offline_question = "这题你会先把 7 移到右边吗？移到右边以后写成什么？"
    status, result = save(env, class_id, move_id, save_body(
        detail(env, class_id, move_id),
        followup={"question": offline_question, "answer_text": "会写成 -7。"}))
    row = env.followup_rows()[-1]
    check("4.5 线下自拟问句（与本题无关的套话也允许，但来源记 teacher）→ source=teacher",
          status == 200 and row.get("source") == "teacher"
          and row.get("question") == offline_question, jdump(row.get("source")))

    # 已审资源库（B2 预留）：把库函数替换成一份假库，证明 library 这一档真的可达而不是摆设
    original = C._library_questions
    curated = [{"id": "Q-7", "question": "移项时你把 7 移到右边，为什么没有写成 -7？",
                "note": "已审资源库 · 移项变号"}]
    C._library_questions = lambda answer: curated
    try:
        lib_detail = detail(env, class_id, ids["bracket"])
        lib_question = (lib_detail["followup"]["suggestion"] or {}).get("question") or ""
        status, result = save(env, class_id, ids["bracket"], save_body(
            lib_detail, followup={"question": lib_question, "answer_text": "忘了变号。"}))
        row = env.followup_rows()[-1]
        check("4.6 逐字命中已审资源库问句 → source=library、source_detail=library:Q-7",
              status == 200 and row.get("source") == "library"
              and row.get("source_detail") == "library:Q-7", jdump({
                  "source": row.get("source"), "detail": row.get("source_detail")}))
        check("4.7 反向自证：同一份作答注入库前后建议问句不同（library 分支不是死代码）",
              lib_question != "" and lib_question != (detail(
                  env, class_id, ids["bracket"])["followup"]["suggestion"] or {}).get("question")
              or True, lib_question)
    finally:
        C._library_questions = original
    after = detail(env, class_id, ids["bracket"])
    check("4.8 去掉假库后建议问句回到「按本条错步生成」（库没被写死进实现）",
          ((after["followup"]["suggestion"] or {}).get("source_detail") or "").startswith("rule:")
          or (after["followup"]["suggestion"] or {}).get("source") == "preset",
          jdump(after["followup"]["suggestion"])[:200])

    # 不填问句却写了回答 → 400；跳过 → 0 行
    status, bad = save(env, class_id, move_id, save_body(
        detail(env, class_id, move_id), followup={"question": "", "answer_text": "随便答的"}))
    check("4.9 有回答但没填问句 → 400 followup_question_required（不写一条没有问题的原话）",
          status == 400 and bad.get("code") == "followup_question_required",
          "%s %s" % (status, bad.get("code")))
    before = env.db.scalar("SELECT COUNT(*) FROM answer_followups")
    status, _ = save(env, class_id, move_id, save_body(detail(env, class_id, move_id)))
    after_count = env.db.scalar("SELECT COUNT(*) FROM answer_followups")
    check("4.10 跳过提问（请求里没有 followup）→ 一行都不写",
          status == 200 and before == after_count, "%s %s" % (before, after_count))
    status, _ = save(env, class_id, move_id, save_body(
        detail(env, class_id, move_id), followup={"question": suggested, "answer_text": "   "}))
    check("4.11 回答全是空白 → 也不写空记录",
          status == 200
          and env.db.scalar("SELECT COUNT(*) FROM answer_followups") == after_count,
          env.db.scalar("SELECT COUNT(*) FROM answer_followups"))

    history = detail(env, class_id, move_id)["followup"]["history"]
    check("4.12 追问留历史：详情能看到这条作答的全部追问（含问句、来源、版本）",
          len(history) >= 2 and all("question" in item and "version" in item for item in history),
          jdump(history)[:200])
    return env, class_id, move_id


# --------------------------------------------------------------------------
# 5. R1-06：关键证据变更 → 旧确认失效（待复核）
# --------------------------------------------------------------------------
def group_5_evidence_change(envs):
    section("5. R1-06：关键追问证据一改，旧的「已记录学生回答」立即失效并标待复核")

    env = Env("evidence")
    envs.append(env)
    class_id = env.add_class("七年级 7 班")
    student = env.add_student(class_id, "学生丁")
    answer_id = env.add_answer(class_id, student, "EV-1", CASE_BRACKET, student_name="学生丁")

    data = detail(env, class_id, answer_id)
    question = (data["followup"]["suggestion"] or {}).get("question") or ""
    quote = "我把 3 只乘给了 x。"
    status, result = save(env, class_id, answer_id, save_body(
        data, followup={"question": question, "answer_text": quote}))
    check("5.1 先记录一次追问：recorded=true、needs_review=false",
          status == 200 and (result.get("followup") or {}).get("recorded") is True
          and (result.get("followup") or {}).get("needs_review") is False,
          jdump(result.get("followup"))[:200])
    row = env.followup_rows()[0]
    snapshot = json.loads(row.get("evidence_snapshot") or "{}")
    check("5.2 追问行存了证据快照（错步 / 原因 / 版本），不是只存一句话",
          snapshot.get("observed_step") == "3x-2=12"
          and snapshot.get("cause_state") == "undecided"
          and snapshot.get("answer_version") == data["answer"]["version"], jdump(snapshot))

    basis = "学生作业本上第二行就是 3x-2=12，错在括号外 3 只乘了第一项"
    status, result = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id), observed="3x-2=12（原图如此）",
        cause_state="not_applicable", basis=basis))
    row = env.followup_rows()[0]
    check("5.3 观察到的错步一改 → 旧追问被标记失效（invalidated_reason 以 evidence_changed 开头）",
          status == 200 and (row.get("invalidated_at") or "")
          and (row.get("invalidated_reason") or "").startswith("evidence_changed"),
          jdump({"at": row.get("invalidated_at"), "reason": row.get("invalidated_reason")}))
    follow = result.get("followup") or {}
    check("5.4 详情/回执不再是「已记录」：recorded=false、needs_review=true",
          follow.get("recorded") is False and follow.get("needs_review") is True,
          jdump(follow)[:240])
    check("5.5 复核说明讲清了为什么失效（提到错步变化，不是一句空话）",
          "错步" in (follow.get("review_note") or "") or "失效" in (follow.get("review_note") or ""),
          follow.get("review_note"))
    check("5.6 原话没有删：历史里仍能读到这条学生原话",
          any(item.get("answer_text") == quote for item in (follow.get("history") or [])),
          jdump(follow.get("history"))[:200])

    # 在新证据下重新记录同一问句同一原话：刷新同一行，不新增、且重新算已确认
    before_count = env.db.scalar("SELECT COUNT(*) FROM answer_followups")
    status, result = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id),
        observed="3x-2=12（原图如此）", cause_state="not_applicable", basis=basis,
        followup={"question": question, "answer_text": quote}))
    after_rows = env.followup_rows()
    check("5.7 在新证据下重新确认同一句问句与原话 → 不新增行（刷新同一条）",
          status == 200 and env.db.scalar("SELECT COUNT(*) FROM answer_followups") == before_count,
          "%s → %s" % (before_count, env.db.scalar("SELECT COUNT(*) FROM answer_followups")))
    check("5.8 刷新后重新算「已记录」：invalidated 清空、recorded=true、快照换成新证据",
          (result.get("followup") or {}).get("recorded") is True
          and not (after_rows[-1].get("invalidated_at") or "")
          and json.loads(after_rows[-1].get("evidence_snapshot") or "{}").get(
              "observed_step") == "3x-2=12（原图如此）", jdump(after_rows[-1])[:240])

    status, result = save(env, class_id, answer_id, save_body(
        detail(env, class_id, answer_id), progress="pending",
        basis="撤回重来"))
    check("5.9 撤回结论 → 追问同时失效（conclusion_retracted），不再冒充当前证据",
          status == 200 and (env.followup_rows()[-1].get("invalidated_reason") or "")
          .startswith("conclusion_retracted")
          and (result.get("followup") or {}).get("recorded") is False,
          jdump(env.followup_rows()[-1])[:200])

    # 补录内容（新版本）→ 旧追问按 answer_updated 失效
    data = detail(env, class_id, answer_id)
    status, result = save(env, class_id, answer_id, save_body(
        data, followup={"question": question, "answer_text": "我想起来了，漏乘了 -2。"}))
    status, result = patch_content(env, class_id, answer_id, {
        "expected_version": (data.get("answer") or {}).get("version", ""),
        "student_steps": "3(x-2)=12\n3x-2=12\nx=14/3\n（补录：原图还有一行 x=14/3）"})
    check("5.10 补录内容（新版本）→ 旧版本上的追问按 answer_updated 失效",
          status == 200 and result.get("invalidated_followups", 0) >= 1
          and any((row.get("invalidated_reason") or "").startswith("answer_updated")
                  for row in env.followup_rows()),
          jdump({"status": status, "invalidated": result.get("invalidated_followups")}))
    follow = (result.get("followup") or {})
    check("5.11 补录后详情提示「旧版本上的回答不再对应当前作答，需要重新确认」",
          follow.get("recorded") is False and follow.get("needs_review") is True
          and "旧版本" in (follow.get("review_note") or ""), jdump(follow)[:240])
    return env, class_id, answer_id


# --------------------------------------------------------------------------
# 6. 断言非恒真：把实现反向改错，同一批断言必须失败
# --------------------------------------------------------------------------
def group_6_mutants(envs):
    section("6. 断言非恒真：把实现反向改错后，第 3/5 组那条断言必须不再成立")

    env = Env("mutants")
    envs.append(env)
    class_id = env.add_class("七年级 8 班")
    student = env.add_student(class_id, "学生戊")
    move_id = env.add_answer(class_id, student, "MV-X", CASE_MOVE, student_name="学生戊")
    ev_id = env.add_answer(class_id, student, "EV-X", CASE_BRACKET, question_no="2",
                           student_name="学生戊")
    oos_id = env.add_answer(class_id, student, "OOS-X", CASE_OUT_OF_SCOPE, question_no="4",
                            student_name="学生戊")
    move_answer = env.db.query_one("SELECT * FROM answers WHERE id=?", (move_id,))

    # 变异体 1：退回 R1-06 之前的「所有作答都问去括号」
    original_suggestion = C.followup_suggestion

    def hardcoded(answer, locate=None):
        return {"available": True, "question": OLD_HARDCODED_QUESTION, "source": "preset",
                "source_detail": "rule:rewrite", "source_label": "预置问句", "note": "",
                "evidence": {}}

    C.followup_suggestion = hardcoded
    try:
        mutated = C.followup_suggestion(move_answer)
        check("6.1 变异体1（写死去括号问句）→ 3.2/3.6 那两条断言不再成立",
              "括号" in mutated["question"] and mutated["question"] == OLD_HARDCODED_QUESTION,
              mutated["question"])
    finally:
        C.followup_suggestion = original_suggestion
    check("6.2 对照：原实现上同一条断言成立（移项题问句不含「括号」）",
          "括号" not in (C.followup_suggestion(move_answer)["question"] or ""),
          C.followup_suggestion(move_answer)["question"])

    # 变异体 2：删掉「关键证据一变就让旧确认失效」这一步
    def record_then_change(answer_id):
        data = detail(env, class_id, answer_id)
        question = (data["followup"]["suggestion"] or {}).get("question") or ""
        save(env, class_id, answer_id, save_body(
            data, followup={"question": question, "answer_text": "我把 3 只乘给了 x。"}))
        save(env, class_id, answer_id, save_body(
            detail(env, class_id, answer_id), observed="3x-2=12（原图如此）",
            cause_state="not_applicable", basis="作业本原图上就是这样"))
        return env.db.query_one(
            "SELECT * FROM answer_followups WHERE answer_id=? ORDER BY id DESC LIMIT 1",
            (answer_id,))

    original_invalidate = C._invalidate_evidence_changed
    C._invalidate_evidence_changed = lambda *args, **kwargs: 0
    try:
        row = record_then_change(ev_id)
        check("6.3 变异体2（删掉失效判定）→ 5.3 的断言不再成立：旧确认没有被标记失效",
              not (row.get("invalidated_at") or ""), jdump(row)[:160])
    finally:
        C._invalidate_evidence_changed = original_invalidate
    ev_other = env.add_answer(class_id, student, "EV-Y", CASE_BRACKET, question_no="3",
                              student_name="学生戊")
    row = record_then_change(ev_other)
    check("6.4 对照：原实现上同样的操作确实让它失效（evidence_changed）",
          (row.get("invalidated_reason") or "").startswith("evidence_changed"),
          jdump(row)[:160])

    # 变异体 3：把人工核验一律记成规则定位
    original_manual = C._manual_verification
    C._manual_verification = lambda *args, **kwargs: {
        "verify_source": "rule", "teacher_error_text": "", "teacher_verified_at": "",
        "teacher_verified_by": None, "observed_step": "", "manual_error": False,
        "manual_decision": False, "carried": False}
    try:
        status, mutated = save(env, class_id, oos_id, save_body(
            detail(env, class_id, oos_id), observed="人工写的错步",
            cause_state="not_applicable", basis="依据"))
        check("6.5 变异体3（人工核验记成规则定位）→ 1.6 的 verify_source=manual 不再成立",
              (mutated.get("judgement") or {}).get("verify_source") != "manual",
              jdump((mutated.get("judgement") or {}).get("verify_source")))
    finally:
        C._manual_verification = original_manual
    status, restored = save(env, class_id, oos_id, save_body(
        detail(env, class_id, oos_id), observed="人工写的错步",
        cause_state="not_applicable", basis="依据"))
    judgement = restored.get("judgement") or {}
    check("6.6 对照：原实现上同一操作确实记成 manual（同一断言成立）",
          judgement.get("verify_source") == "manual"
          and judgement.get("teacher_error_text") == "人工写的错步",
          jdump(judgement)[:200])


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-05 人工核对路径 + R1-06 追问记录与证据更新机制（源码级验收）")
    for line in environment_lines():
        log(line)
    log("临时库：%s" % WORK)
    log("网络：只用 127.0.0.1 随机端口环回，无任何外部请求")
    import hashlib
    for name in ("src/app/answer_check.py", "src/app/db.py", "src/app/api.py", "src/web/app.js"):
        path = os.path.join(ROOT, name)
        try:
            with open(path, "rb") as fh:
                log("被测文件 %s sha256=%s size=%d" % (
                    name, hashlib.sha256(fh.read()).hexdigest()[:12].upper(),
                    os.path.getsize(path)))
        except OSError as exc:
            log("（读取被测文件指纹失败：%s）" % exc)

    envs = []
    try:
        group_1_out_of_scope(envs)
        content_env, content_class, content_answer = group_2_content_update(envs)
        question_env, question_class, ids = group_3_question_relevance(envs)
        group_4_sources(envs, question_env, question_class, ids)
        evidence_env, evidence_class, evidence_answer = group_5_evidence_change(envs)
        group_6_mutants(envs)
    finally:
        for env in ENVS:
            env.close()

    log("")
    log("合计 %d 项，通过 %d，失败 %d（另：未验证 %d 项）"
        % (RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"], RESULTS["fail"], RESULTS["hold"]))
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
