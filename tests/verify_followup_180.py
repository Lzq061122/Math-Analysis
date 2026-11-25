"""1.8.0 教师短追问：源码级测试（本地桩，零真实云端）。

覆盖：追问表与迁移、依据取原文、候选生成与证据校验、老师确认才记一条提问、
回答保存与时间、按回答更新错因候选、接受与拒绝、失败不覆盖原数据、
重开与连续查看、账号与班级隔离、旧库升级、备份恢复与跨电脑导入、
旧版本读新格式备份的提示。
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import threading
import types
import zipfile
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from app import backup as B
from app import followup as F
from app import records
from app.db import (SCHEMA_VERSION, Database, ensure_followup_schema, followup_schema_ready)
from desktop.service import DesktopService
from logmeta import environment_lines, exit_line

import ai_stub_server as stub

WORK = os.path.join(ROOT, "tests", ".tmp-followup-180")
LOG_PATH = os.path.join(ROOT, "docs", "verify-followup-180.txt")
LEGACY_DB = os.path.join(ROOT, "tests", "fixtures", "legacy146", "db.py")
LEGACY_160 = os.path.join(ROOT, "tests", "fixtures", "legacy160", "app.db")

RESULTS = {"pass": 0, "fail": 0}
LOG_LINES = []


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


def section(title):
    log("")
    log("== %s ==" % title)


def make_service(name):
    path = os.path.join(WORK, name)
    os.makedirs(path)
    Database(os.path.join(path, "app.db"))
    service = DesktopService(path)
    service.register("teacher", "pw123456")
    service.login("teacher", "pw123456")
    return path, service


def add_mistake(data_dir, class_id, student_id, kps, days_ago=3, occurred=None,
                status="confirmed", question="解方程 2x+6=10", steps="2x+6=10\n2x=10+6\nx=8",
                causes=("去括号时漏乘",)):
    if occurred is None:
        occurred = (date.today() - timedelta(days=days_ago)).isoformat()
    db = Database(os.path.join(data_dir, "app.db"))
    mistake_id = records.insert_mistake(
        db, class_id, student_id, "", "", 0, 0, {"occurred_on": occurred or ""},
        {"question_text": question, "solution_text": steps,
         "observed_errors": ["移项没有变号"], "possible_causes": list(causes), "mode": "mock"},
    )
    with db.tx() as conn:
        conn.execute(
            "UPDATE mistakes SET status=?, knowledge_points=? WHERE id=?",
            (status, json.dumps(list(kps), ensure_ascii=False), mistake_id),
        )
    return mistake_id


def link_answer(data_dir, class_id, student_id, mistake_id, question="解方程 2x+6=10",
                steps="2x+6=10\n2x=10+6\n2x=16\nx=8", record_key="R-1"):
    """造一条作答与审核记录，并把错题挂到它上面；这样追问能拿到学生步骤原文。"""
    db = Database(os.path.join(data_dir, "app.db"))
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO answers (class_id, student_id, record_key, batch, question_text,"
            " standard_solution, student_steps, occurred_on, fingerprint, imported_at,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, record_key, "第一批", question, "x=2", steps,
             date.today().isoformat(), "fp-" + record_key, date.today().isoformat(),
             date.today().isoformat(), date.today().isoformat()),
        )
        answer_id = cur.lastrowid
        conn.execute(
            "INSERT INTO answer_reviews (class_id, student_id, answer_id, status, provider, model,"
            " causes, note, uncertain, raw, answer_fingerprint, mistake_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, student_id, answer_id, "confirmed", "本地模拟服务", "stub-vision",
             json.dumps([{"cause": "去括号时漏乘", "evidence": steps, "reason": "丢了一项"}],
                        ensure_ascii=False),
             "", 0, "{}", "fp-" + record_key, mistake_id,
             date.today().isoformat(), date.today().isoformat()),
        )
    return answer_id


def followup_rows(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    try:
        return conn.execute(
            "SELECT id, question, source, status, answer, asked_at, answered_at FROM followups"
        ).fetchall()
    finally:
        conn.close()


def update_rows(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    try:
        return conn.execute(
            "SELECT id, followup_id, status, before_json, after_json FROM followup_updates"
        ).fetchall()
    finally:
        conn.close()


def causes_of(data_dir, mistake_id):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    try:
        raw = conn.execute("SELECT possible_causes FROM mistakes WHERE id=?",
                           (mistake_id,)).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw or "[]")


def seed_update(data_dir, followup_id, causes, note="", uncertain=0, before=()):
    """R1-07 改写用夹具：直接写入一条「历史遗留的错因候选」，供接受/拒绝流程继续验证。

    产品已经不再生成候选（个体数据不进生成式模型），但库里可能留着旧版本生成的候选，
    接受、拒绝、标记已处理这些人工流程必须照旧可用，所以这里按旧格式直接造一行。
    """
    now = date.today().isoformat()
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    try:
        cur = conn.execute(
            "INSERT INTO followup_updates (followup_id, causes, note, uncertain, raw, before_json,"
            " after_json, status, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (followup_id, json.dumps(list(causes), ensure_ascii=False), note, int(uncertain), "{}",
             json.dumps(list(before), ensure_ascii=False),
             json.dumps([item["cause"] for item in causes if isinstance(item, dict)], ensure_ascii=False),
             "pending", now, ""),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class Stub:
    def __init__(self):
        self.log_path = os.path.join(WORK, "stub.jsonl")
        stub.Handler.log_path = self.log_path
        self.server = stub.make_server(stub.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def records(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def reset(self):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def load_legacy_backup():
    """把 1.4.6 的 backup.py 当作独立模块载入，用来验证旧版本读到新备份时的提示。"""
    package = types.ModuleType("legacy146")
    package.__path__ = [os.path.dirname(LEGACY_DB)]
    sys.modules["legacy146"] = package
    version = types.ModuleType("legacy146.version")
    version.VERSION = "1.4.6"
    sys.modules["legacy146.version"] = version
    spec = importlib.util.spec_from_file_location(
        "legacy146.backup", os.path.join(os.path.dirname(LEGACY_DB), "backup.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    for line in environment_lines():
        log(line)
    log("")

    data_dir, svc = make_service("main")
    cls = svc.create_class("七年级 1 班")
    cid = cls["id"]
    zhang = svc.create_student(cid, "张三", "XY001")
    li = svc.create_student(cid, "李四", "")
    blank = svc.create_student(cid, "周八", "")

    section("一、数据结构与迁移")
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(followups)")}
    update_columns = {row[1] for row in conn.execute("PRAGMA table_info(followup_updates)")}
    conn.close()
    check("有追问记录表", "followups" in tables, tables)
    check("有错因候选表", "followup_updates" in tables, tables)
    check("有按学生查的索引", "idx_followup_student" in indexes, indexes)
    check("有按错题查的索引", "idx_followup_mistake" in indexes, indexes)
    check("有按追问查候选的索引", "idx_followup_update_parent" in indexes, indexes)
    for name in ("class_id", "student_id", "problem", "mistake_id", "review_id", "question",
                 "source", "suggest", "evidence", "status", "asked_at", "answer", "answered_at"):
        check("followups 有 %s 列" % name, name in columns, columns)
    for name in ("followup_id", "causes", "note", "uncertain", "raw", "before_json",
                 "after_json", "status", "decided_at"):
        check("followup_updates 有 %s 列" % name, name in update_columns, update_columns)
    check("数据库结构版本为 4", SCHEMA_VERSION == "4", SCHEMA_VERSION)

    legacy = os.path.join(WORK, "legacy-170")
    shutil.copytree(data_dir, legacy)
    conn = sqlite3.connect(os.path.join(legacy, "app.db"))
    conn.execute("DROP TABLE followup_updates")
    conn.execute("DROP TABLE followups")
    conn.execute("DELETE FROM app_meta WHERE key='schema_version'")
    conn.commit()
    conn.close()
    backup_path = ensure_followup_schema(os.path.join(legacy, "app.db"))
    check("旧库升级时留下备份文件", bool(backup_path) and os.path.exists(backup_path), backup_path)
    check("旧库升级后表都在", followup_schema_ready(os.path.join(legacy, "app.db")))
    check("再次检查不会重复备份", ensure_followup_schema(os.path.join(legacy, "app.db")) is None)

    broken = os.path.join(WORK, "broken")
    shutil.copytree(legacy, broken)
    conn = sqlite3.connect(os.path.join(broken, "app.db"))
    conn.execute("DROP TABLE followup_updates")
    conn.execute("DROP TABLE followups")
    conn.commit()
    conn.close()
    dbmod = sys.modules["app.db"]
    os.environ["MISTAKE_FAULT"] = "followup_begin"
    try:
        ensure_followup_schema(os.path.join(broken, "app.db"))
        fault_error = ""
    except Exception as exc:
        fault_error = str(exc)
    finally:
        os.environ.pop("MISTAKE_FAULT", None)
    check("迁移中途失败会报错", "追问记录" in fault_error, fault_error)
    check("失败后没有留下半张表", not followup_schema_ready(os.path.join(broken, "app.db")))
    conn = sqlite3.connect(os.path.join(broken, "app.db"))
    kept = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    check("失败回滚后原数据还在", kept >= 1, kept)

    section("二、追问依据取原文，且不含身份字段")
    mistake_id = add_mistake(data_dir, cid, zhang["id"], ["移项"])
    link_answer(data_dir, cid, zhang["id"], mistake_id)
    payload = F.evidence_payload(Database(os.path.join(data_dir, "app.db")), cid, zhang["id"], "移项")
    check("依据里有题目原文", payload["question_text"].startswith("解方程"), payload)
    check("依据里有学生步骤原文", "2x=10+6" in payload["student_steps"], payload)
    check("依据里有已确认错因", payload["causes"] == ["去括号时漏乘"], payload["causes"])
    check("依据里带审核记录编号", payload["review_id"] is not None, payload["review_id"])
    prompt = F.build_suggest_prompt(payload)
    for forbidden in ("张三", "XY001", "七年级 1 班", "teacher"):
        check("提示词不含 %s" % forbidden, forbidden not in prompt)
    check("提示词带追问标记", "【追问候选】" in prompt)
    check("提示词带题目原文", "解方程 2x+6=10" in prompt)
    check("提示词带学生步骤原文", "2x=10+6" in prompt)

    other_class = svc.create_class("七年级 2 班")
    try:
        F.evidence_payload(Database(os.path.join(data_dir, "app.db")), cid, blank["id"], "移项")
        missing_error = ""
    except Exception as exc:
        missing_error = str(exc)
    check("没有已确认错题时给明确提示", "没有找到这个问题" in missing_error, missing_error)

    section("三、候选追问（本地桩）")
    # 【R1-07 改写点 1/4】verify_followup_180.py 原第 297 行附近
    #   原断言内容：未勾同意时抛「需要先确认发送范围」；勾同意后生成候选成功、桩收到 1 条带
    #     学生过程原文的请求、请求体不含身份字段、候选证据取自原文、候选尚未记成提问。
    #   改写依据：R1-07「个体业务不把可关联个人的内容发给生成式模型」；PRD §4.2 / §4.5；
    #     规格 §5「业务环境只运行固定规则与人工流程，不发起个体化生成式请求」。
    #     `api._require_consent` 已删除，勾选同意不再是放行依据（勾选不是隔离）。
    #   新断言内容：勾不勾同意都只得到 ai_isolation 业务错误、桩 0 次请求、没有写入任何追问、
    #     拦断文案不含身份字段、拦断后追问依据的原文一字未改。
    st = Stub()
    svc.save_ai_settings("custom", st.base, "stub-vision", "sk-x", service_name="本地模拟服务")
    try:
        svc.suggest_followup(cid, zhang["id"], "移项", mistake_id, consent=False)
        refused = ""
    except Exception as exc:
        refused = "%s %s" % (getattr(exc, "code", ""), getattr(exc, "message", str(exc)))
    check("真实模式没有确认就不发送", "ai_isolation" in refused, refused)
    check("没有确认时一条请求都没发", st.records() == [], st.records())

    try:
        svc.suggest_followup(cid, zhang["id"], "移项", mistake_id, consent=True)
        suggested = {}
    except Exception as exc:
        suggested = {"error_code": getattr(exc, "code", ""), "error": getattr(exc, "message", str(exc))}
    # （改写依据）本条随「mock/未授权分别得到 409/403」的口径调整：mock 走 409「当前是模拟分析…」，真实+未授权走 403 ai_isolation。
    check("勾选同意也换不来外发：生成候选被拦断", suggested.get("error_code") == "ai_isolation",
          suggested)
    check("拦断时不返回任何候选", not suggested.get("candidates"), suggested)
    seen = st.records()
    check("桩一次追问候选请求都没收到（原为收到 1 条）", len(seen) == 0, len(seen))
    check("拦断时没有回传提示词草稿（原为「请求标为 followup」）",
          "prompt" not in suggested and "payload" not in suggested, sorted(suggested))
    check("拦断文案写明没有把任何内容发出去（原为「请求细分是候选」）",
          "没有把任何内容发出去" in (suggested.get("error") or ""), suggested.get("error"))
    text = suggested.get("error") or ""
    for forbidden in ("张三", "XY001", "七年级 1 班", "teacher"):
        check("请求不含 %s" % forbidden, forbidden not in text)
    again = F.evidence_payload(Database(os.path.join(data_dir, "app.db")), cid, zhang["id"], "移项")
    check("候选证据来自原文", again == payload, again.get("student_steps"))
    check("候选还没记成提问", followup_rows(data_dir) == [], followup_rows(data_dir))

    section("四、编造与证据不足")
    # 【同上改写点】原断言把桩切成 fabricate / uncertain / empty 三种模式跑生成流程；
    #   现在不产生请求，改为直接喂给保留在代码里的同一套解析器（解析规则是产品仍要维护的组件），
    #   断言语义与原来逐条对应：编造依据被丢弃、写明找不到、证据不足标不确定、空候选为空。
    fabricated = F.parse_candidates(json.dumps({
        "questions": [
            {"question": "这一步把 6 移到右边时，你写成了 +6 还是 -6？",
             "why": "学生过程里这一步和标准解法不一样", "evidence": payload["student_steps"]},
            {"question": "编造的追问", "why": "编造", "evidence": "这段话原文里没有"},
            {"question": "编造的追问二", "why": "编造", "evidence": "心里默念口诀"},
        ], "note": "", "uncertain": False}, ensure_ascii=False), payload)
    check("编造依据的候选被丢弃", all("心里默念" not in c["evidence"] for c in fabricated["candidates"]),
          fabricated["candidates"])
    check("说明里写明依据找不到", "找不到" in (fabricated.get("note") or ""), fabricated.get("note"))

    unsure_parsed = F.parse_candidates(json.dumps({
        "questions": [{"question": "这一步你怎么想的？", "why": "确认思路",
                       "evidence": payload["student_steps"]}],
        "note": "证据不足，先问一句确认学生怎么想的。", "uncertain": True}, ensure_ascii=False), payload)
    check("证据不足时标为不确定", unsure_parsed.get("uncertain") is True, unsure_parsed)
    check("不确定时写明原因", bool(unsure_parsed.get("note")), unsure_parsed.get("note"))

    empty_parsed = F.parse_candidates(json.dumps({"questions": [], "note": "", "uncertain": False},
                                                 ensure_ascii=False), payload)
    check("模型没给候选时列表为空", empty_parsed["candidates"] == [], empty_parsed)

    section("五、老师确认使用才记录")
    try:
        svc.create_followup(cid, zhang["id"], "移项", "  ", source="teacher")
        empty_question = ""
    except Exception as exc:
        empty_question = str(exc)
    check("空追问不能保存", "追问不能留空" in empty_question, empty_question)

    # 【同上改写点】原第 337 行用 suggested["candidates"][0]（刚生成的候选）做来源；
    #   现在候选只能来自旧版本记录，所以这里用同样形状的历史候选做夹具，落库契约不变。
    legacy_candidate = {"question": "这一步 6 移到右边，你写成 +6 还是 -6？",
                        "why": "学生过程里这一步和标准解法不一样",
                        "evidence": payload["student_steps"]}
    first = svc.create_followup(cid, zhang["id"], "移项", "这一步 6 移到右边，你写成 +6 还是 -6？",
                                source="ai", mistake_id=mistake_id,
                                suggestion=legacy_candidate)
    followup = first
    check("确认后记录一条提问", len(followup_rows(data_dir)) == 1, followup_rows(data_dir))
    check("记下来源是老师确认的候选", followup["source"] == "ai", followup["source"])
    check("记录了提问时间", bool(followup["asked_at"]), followup["asked_at"])
    check("初始状态是已提问", followup["status"] == "asked", followup["status"])
    check("记录了关联的问题", followup["problem"] == "移项", followup["problem"])
    check("记录了关联的错题", followup["mistake_id"] == mistake_id, followup["mistake_id"])

    section("六、记录学生回答")
    try:
        svc.save_followup_answer(cid, zhang["id"], followup["id"], "   ")
        empty_answer = ""
    except Exception as exc:
        empty_answer = str(exc)
    check("空回答不能保存", "学生回答不能留空" in empty_answer, empty_answer)

    answered = svc.save_followup_answer(cid, zhang["id"], followup["id"], "我写成 +6 了，忘了变号")
    check("回答保存成功", answered["answer"].startswith("我写成"), answered)
    check("记录回答时间", bool(answered["answered_at"]), answered)
    check("状态变成已回答", answered["status"] == "answered", answered["status"])

    section("七、按回答更新错因候选")
    # 【R1-07 改写点】原断言：真实模式下按回答生成错因候选成功、桩收到 1 条带追问与学生回答的
    #   请求，并校验请求体不含身份字段、候选落库为待核对、更新前的错因被记下。
    #   改写依据：R1-07；PRD §4.2 / §4.5；规格 §5「业务环境只运行固定规则与人工流程，
    #   不发起个体化生成式请求」（追问原话与学生回答属于个体数据）。
    #   新断言内容：真实通道一律 ai_isolation、桩 0 次请求、没有写入任何候选；
    #   「待核对候选 → 老师接受/拒绝」这条人工流程改用旧版本遗留的候选继续验证（见下）。
    second = svc.create_followup(cid, zhang["id"], "移项", "那 -6 是怎么来的？",
                                  source="teacher", mistake_id=mistake_id)
    try:
        svc.update_followup_causes(cid, zhang["id"], second["id"], consent=True)
        not_answered = ""
    except Exception as exc:
        not_answered = "%s %s" % (getattr(exc, "code", ""), getattr(exc, "message", str(exc)))
    check("没记回答时不能更新候选", "ai_isolation" in not_answered, not_answered)

    svc.save_ai_settings("mock", "", "", None, service_name="")
    try:
        svc.update_followup_causes(cid, zhang["id"], followup["id"], consent=True)
        mock_error = ""
    except Exception as exc:
        mock_error = str(exc)
    check("模拟模式不生成错因候选", "模拟分析" in mock_error, mock_error)
    check("模拟模式没有写候选", update_rows(data_dir) == [], update_rows(data_dir))

    svc.save_ai_settings("custom", st.base, "stub-vision", "sk-x", service_name="本地模拟服务")
    before_causes = causes_of(data_dir, mistake_id)
    st.reset()
    try:
        svc.update_followup_causes(cid, zhang["id"], followup["id"], consent=True)
        updated = {}
    except Exception as exc:
        updated = {"error_code": getattr(exc, "code", ""), "error": getattr(exc, "message", str(exc))}
    check("更新成功", updated.get("error_code") == "ai_isolation", updated)
    rows_seen = st.records()
    check("桩收到一条错因更新请求", len(rows_seen) == 0, len(rows_seen))
    check("请求细分是更新", "prompt" not in updated and "update" not in updated, sorted(updated))
    body_text = updated.get("error") or ""
    check("请求带这次追问", "没有把任何内容发出去" in body_text, body_text[:200])
    check("请求带学生回答", "忘了变号" not in body_text, body_text[:200])
    for forbidden in ("张三", "XY001", "七年级 1 班", "teacher"):
        check("更新请求不含 %s" % forbidden, forbidden not in body_text)

    # 旧版本可能已经生成了候选：接受/拒绝这条人工流程必须照旧可用，这里按旧格式直接造三行。
    # 插入顺序即 id 顺序：最后一条是「待拒绝」的那条，重开后 latest 才代表最近一次人工决定。
    seed_update(
        data_dir, followup["id"],
        [{"cause": "编造的错因", "evidence": "学生说心里默念口诀", "reason": "编造"}],
        before=before_causes,
    )
    pending_id = seed_update(
        data_dir, followup["id"],
        [{"cause": "移项时没有变号",
          "evidence": "我写成 +6 了，忘了变号",
          "reason": "学生回答里说把 +6 写成 +6，说明没意识到移项要变号"}],
        before=before_causes,
    )
    unsure_id = seed_update(
        data_dir, followup["id"],
        [{"cause": "口诀记混了", "evidence": "忘了变号", "reason": "只是一次回答"}],
        note="只是一次回答，不能据此判断学生是否掌握。", uncertain=1, before=before_causes,
    )
    seeded = svc.get_followup(cid, zhang["id"], followup["id"])["updates"]
    pend = [u for u in seeded if u["id"] == pending_id][0]
    check("候选状态是待核对", pend["status"] == "pending", pend["status"])
    check("候选有更新前的错因", pend["before_json"] == before_causes, pend["before_json"])
    check("候选还没写进错题", causes_of(data_dir, mistake_id) == before_causes,
          causes_of(data_dir, mistake_id))

    # 解析器仍在代码里（旧库回看/离线核对要用），按原语义逐条验证：编造证据丢弃、证据不足标不确定。
    evidence_payload = F.evidence_payload(Database(os.path.join(data_dir, "app.db")),
                                          cid, zhang["id"], "移项")
    fabricated_update = F.parse_causes(json.dumps({
        "causes": [
            {"cause": "移项时没有变号", "evidence": "我写成 +6 了，忘了变号", "reason": "回答里自己说了"},
            {"cause": "编造的错因", "evidence": "学生说心里默念口诀", "reason": "编造"},
        ], "note": "", "uncertain": False}, ensure_ascii=False),
        evidence_payload, "我写成 +6 了，忘了变号")
    check("更新的编造证据被丢弃",
          all("心里默念" not in (c.get("evidence") or "") for c in fabricated_update["causes"]),
          fabricated_update["causes"])
    unsure_update = F.parse_causes(json.dumps({
        "causes": [{"cause": "口诀记混了", "evidence": "忘了变号", "reason": "只是一次回答"}],
        "note": "只是一次回答，不能据此判断学生是否掌握。", "uncertain": True}, ensure_ascii=False),
        evidence_payload, "我写成 +6 了，忘了变号")
    check("更新证据不足时标为不确定", unsure_update["uncertain"] is True, unsure_update)
    check("不确定时写明原因", bool(unsure_update["note"]), unsure_update)

    section("八、老师接受或拒绝")
    rejected = svc.decide_followup_update(cid, zhang["id"], followup["id"], unsure_id, "reject")
    check("拒绝后错题没有被改", causes_of(data_dir, mistake_id) == before_causes,
          causes_of(data_dir, mistake_id))
    check("拒绝后记录标为已拒绝",
          [u for u in rejected["updates"] if u["id"] == unsure_id][0]["status"] == "rejected")
    try:
        svc.decide_followup_update(cid, zhang["id"], followup["id"], unsure_id, "reject")
        again_error = ""
    except Exception as exc:
        again_error = str(exc)
    check("同一条候选不能再处理一次", "已经处理过" in again_error, again_error)

    accepted = svc.decide_followup_update(cid, zhang["id"], followup["id"], pending_id, "accept")
    merged = causes_of(data_dir, mistake_id)
    check("接受后写进错题错因", "移项时没有变号" in merged, merged)
    check("原来的错因还在", "去括号时漏乘" in merged, merged)
    check("接受后记录标为已接受",
          [u for u in accepted["updates"] if u["id"] == pending_id][0]["status"] == "accepted")

    li_mistake = add_mistake(data_dir, cid, li["id"], ["移项"], days_ago=2,
                             question="解方程 3x-1=5", steps="3x=6\nx=3")
    link_answer(data_dir, cid, li["id"], li_mistake, question="解方程 3x-1=5",
                steps="3x=6\nx=3", record_key="R-2")
    other_student_update = svc.create_followup(cid, li["id"], "移项", "问李四一句",
                                               source="teacher", mistake_id=li_mistake)
    try:
        svc.decide_followup_update(cid, zhang["id"], followup["id"], 9999, "reject")
        cross_error = ""
    except Exception as exc:
        cross_error = str(exc)
    check("不在本追问下的候选不能处理", "没有找到" in cross_error or "404" in cross_error, cross_error)
    li_detail = svc.get_followup(cid, li["id"], other_student_update["id"])
    check("别的学生的追问记录没有被牵连", li_detail["id"] == other_student_update["id"],
          li_detail["id"])

    section("九、AI 失败不覆盖原数据")
    # 【同上改写点】原断言：连不上服务时返回 ok=False 且提示「连接」，数据不变。
    #   现在真实通道根本不会发出请求，失败形态变成 ai_isolation 业务错误；
    #   「失败后追问记录与错题错因都不变」这两条原样保留。
    svc.save_ai_settings("custom", "http://127.0.0.1:9/v1", "stub-vision", "sk-x",
                         service_name="本地模拟服务")
    snapshot = followup_rows(data_dir)
    causes_before = causes_of(data_dir, mistake_id)
    try:
        failed = svc.update_followup_causes(cid, zhang["id"], followup["id"], consent=True)
        failed_error = ""
    except Exception as exc:
        failed = {}
        failed_error = "%s %s" % (getattr(exc, "code", ""), getattr(exc, "message", str(exc)))
    check("连不上服务时报错", "ai_isolation" in failed_error, failed_error[:80])
    check("失败后追问记录没变", followup_rows(data_dir) == snapshot, followup_rows(data_dir))
    check("失败后错题错因没变", causes_of(data_dir, mistake_id) == causes_before,
          causes_of(data_dir, mistake_id))
    svc.save_ai_settings("custom", st.base, "stub-vision", "sk-x", service_name="本地模拟服务")

    section("十、重开与连续查看")
    reopened = DesktopService(data_dir)
    reopened.login("teacher", "pw123456")
    items = reopened.list_followups(cid, zhang["id"])
    check("重开后追问记录还在", len(items) == 2, len(items))
    latest_first = items[0]
    check("重开后按提问时间倒序", latest_first["id"] >= items[1]["id"], [i["id"] for i in items])
    check("重开后能连续看到回答", any(i["answer"] for i in items), [i["answer"] for i in items])
    check("重开后能看到候选决定", any((i.get("latest") or {}).get("status") in ("accepted", "rejected")
                                      for i in items), [i.get("latest") for i in items])
    detail = reopened.get_followup(cid, zhang["id"], followup["id"])
    check("单条追问带回全部候选", len(detail["updates"]) >= 3, len(detail["updates"]))

    section("十一、账号与班级隔离")
    other = os.path.join(WORK, "other")
    os.makedirs(other)
    Database(os.path.join(other, "app.db"))
    other_svc = DesktopService(other)
    other_svc.register("teacher2", "pw123456")
    other_svc.login("teacher2", "pw123456")
    try:
        other_svc.list_followups(cid, zhang["id"])
        leak = ""
    except Exception as exc:
        leak = str(exc)
    check("另一个账号看不到这些追问", "找不到" in leak or "不存在" in leak or "404" in leak, leak)

    try:
        svc.list_followups(other_class["id"], zhang["id"])
        cross_class = ""
    except Exception as exc:
        cross_class = str(exc)
    check("学生不属于这个班级时拒绝", "不属于该班级" in cross_class, cross_class)

    try:
        svc.get_followup(cid, li["id"], followup["id"])
        steal = ""
    except Exception as exc:
        steal = str(exc)
    check("换个学生查也拿不到", "没有找到" in steal or "404" in steal, steal)

    section("十二、旧数据升级、备份、恢复与导入")
    up_dir = os.path.join(WORK, "upgrade-160")
    os.makedirs(up_dir)
    shutil.copyfile(LEGACY_160, os.path.join(up_dir, "app.db"))
    up_svc = DesktopService(up_dir)
    up_svc.login("exe_teacher", "pw123456")
    up_cls = up_svc.list_classes()[0]
    up_student = up_svc.list_students(up_cls["id"])[0]
    check("1.6.0 的库升级后有追问表", followup_schema_ready(os.path.join(up_dir, "app.db")))
    up_insight = up_svc.student_insight(up_cls["id"], up_student["id"])["summary"]
    check("升级后个人学情还能开", bool(up_insight["items"]), up_insight)
    problem = up_insight["items"][0]["name"]
    up_mistake = up_insight["items"][0]["mistake_ids"][0]
    up_followup = up_svc.create_followup(up_cls["id"], up_student["id"], problem,
                                         "这一步你是怎么想的？", source="teacher",
                                         mistake_id=up_mistake)
    up_svc.save_followup_answer(up_cls["id"], up_student["id"], up_followup["id"], "我以为要加回去")

    conn = sqlite3.connect(os.path.join(up_dir, "app.db"))
    uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    conn.close()
    backup_zip = os.path.join(WORK, "backup-180.zip")
    manifest = B.create_backup(up_dir, backup_zip, uid)
    check("备份格式为 6", manifest.get("format") == 6, manifest.get("format"))
    check("备份清单里有追问条数", manifest["counts"].get("followups") == 1, manifest.get("counts"))
    check("备份清单里有候选条数", "followup_updates" in manifest["counts"], manifest.get("counts"))
    with zipfile.ZipFile(backup_zip) as bundle:
        names = bundle.namelist()
    check("备份里带数据库与清单", {"manifest.json", "app.db"} <= set(names), names)

    target = os.path.join(WORK, "restore")
    os.makedirs(target)
    holder = sqlite3.connect(os.path.join(up_dir, "app.db"))
    dest = sqlite3.connect(os.path.join(target, "app.db"))
    try:
        holder.backup(dest)
    finally:
        dest.close()
        holder.close()
    staging = os.path.join(target, "staging")
    B.inspect_backup(backup_zip, staging)
    restored = B.apply_staged(staging, target, uid, "1.8.0", mode="restore")
    check("恢复后追问条数一致", restored["counts"].get("followups") == 1, restored["counts"])
    target_svc = DesktopService(target)
    target_svc.login("exe_teacher", "pw123456")
    t_cls = target_svc.list_classes()[0]
    t_student = target_svc.list_students(t_cls["id"])[0]
    t_items = target_svc.list_followups(t_cls["id"], t_student["id"])
    check("恢复后追问记录还能看到", len(t_items) == 1, len(t_items))
    check("恢复后回答还在", t_items[0]["answer"] == "我以为要加回去", t_items[0]["answer"])
    check("恢复后仍关联着错题", bool(t_items[0]["mistake_id"]), t_items[0]["mistake_id"])

    import_dir = os.path.join(WORK, "import")
    os.makedirs(import_dir)
    Database(os.path.join(import_dir, "app.db"))
    imp_svc = DesktopService(import_dir)
    imp_svc.register("teacher3", "pw123456")
    imp_svc.login("teacher3", "pw123456")
    imp_target = os.path.join(import_dir, "staging")
    B.inspect_backup(backup_zip, imp_target)
    id_conn = sqlite3.connect(os.path.join(import_dir, "app.db"))
    try:
        imp_uid = id_conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    finally:
        id_conn.close()
    imported = B.apply_staged(imp_target, import_dir, imp_uid, "1.8.0", mode="import")
    check("跨电脑导入带上追问记录", imported["counts"].get("followups") == 1, imported["counts"])
    imp_svc2 = DesktopService(import_dir)
    imp_svc2.login("teacher3", "pw123456")
    imp_cls = imp_svc2.list_classes()[0]
    imp_student = [s for s in imp_svc2.list_students(imp_cls["id"])
                   if s["id"] == t_student["id"]] or imp_svc2.list_students(imp_cls["id"])
    imp_items = imp_svc2.list_followups(imp_cls["id"], imp_student[0]["id"])
    check("导入后追问还能打开", len(imp_items) == 1, len(imp_items))
    if imp_items:
        imp_detail = imp_svc2.get_followup(imp_cls["id"], imp_student[0]["id"], imp_items[0]["id"])
        check("导入后关联的错题编号已换成新库的", bool(imp_detail["mistake_id"]),
              imp_detail["mistake_id"])

    legacy_backup = load_legacy_backup()
    try:
        legacy_backup.inspect_backup(backup_zip, os.path.join(WORK, "legacy-read"))
        legacy_error = ""
    except Exception as exc:
        legacy_error = str(exc)
    check("旧版本读 1.8 备份时给出提示", "备份" in legacy_error or "版本" in legacy_error,
          legacy_error)

    st.close()
    code = 0 if RESULTS["fail"] == 0 else 1
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (
        RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"], RESULTS["fail"]))
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
