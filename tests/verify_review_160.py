"""1.6 作答步骤证据与老师审核：源码级测试（本地桩，零真实云端）。

覆盖：数据结构与迁移、提示词不含身份字段、证据必须来自学生原文、
模拟模式不联网、真实通道走本地桩、老师修改/确认/拒绝、重复确认拦截、
统计口径、账号与班级隔离、备份恢复、迁移失败回滚、
五类作答场景、服务失败与主动重试、关闭重开后的状态与关联、
输出上限与截断处理、1.6.0 数据的打开与备份恢复。
"""
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from app import answer_review as R
from app import backup as B
from app import vision
from app.db import Database, SCHEMA_VERSION, answers_schema_ready, ensure_answers_schema
from desktop.service import DesktopService
from logmeta import environment_lines, exit_line

import ai_stub_server as stub

WORK = os.path.join(ROOT, "tests", ".tmp-review-160")
LOG_PATH = os.path.join(ROOT, "docs", "verify-review-160.txt")
CSV_HEAD = ["记录编号", "学生姓名", "学校编号", "题号", "题目内容", "单元", "标准解法",
            "学生步骤", "来源批次", "作答日期", "缺测", "缺测原因"]

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


def row(**kw):
    base = {"记录编号": "A1", "学生姓名": "张三", "学校编号": "XY001", "题号": "1",
            "题目内容": "3(x-2)=2x+5", "单元": "一元一次方程",
            "标准解法": "3x-6=2x+5；3x-2x=5+6；x=11",
            "学生步骤": "3(x-2)=2x+5\n3x-2=2x+5\nx=7", "来源批次": "9月作业",
            "作答日期": "2026-09-02", "缺测": "否", "缺测原因": ""}
    base.update(kw)
    return base


def csv_text(rows):
    import csv
    import io
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_HEAD)
    writer.writeheader()
    for item in rows:
        writer.writerow({key: item.get(key, "") for key in CSV_HEAD})
    return out.getvalue()


def make_service(name):
    path = os.path.join(WORK, name)
    os.makedirs(path)
    Database(os.path.join(path, "app.db"))
    service = DesktopService(path)
    service.register("teacher", "pw123456")
    service.login("teacher", "pw123456")
    cls = service.create_class("七年级 1 班")
    service.create_student(cls["id"], "张三", "XY001")
    service.create_student(cls["id"], "李四", "")
    return path, service, cls


def review_count(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    total = conn.execute("SELECT COUNT(*) FROM answer_reviews").fetchone()[0]
    conn.close()
    return total


def evidence_ok(review, steps):
    """每条证据都要能在原始步骤里逐字找到（忽略空白）。"""
    flat = "".join(steps.split())
    return all("".join(item["evidence"].split()) in flat for item in review["causes"])


class Stub:
    """在进程内起本地桩，日志文件用来看请求内容。"""

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

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    for line in environment_lines():
        log(line)
    log("")

    data_dir, svc, cls = make_service("main")
    cid = cls["id"]

    section("一、数据结构与迁移")
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("新库有 answer_reviews 表", "answer_reviews" in tables, tables)
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check("有按作答查审核记录的索引", "idx_reviews_answer" in indexes, indexes)
    check("同一条作答只允许一条已确认记录（部分唯一索引）",
          "idx_reviews_answer_confirmed" in indexes, indexes)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(answer_reviews)")]
    for name in ("answer_id", "status", "causes", "note", "uncertain", "answer_fingerprint",
                 "mistake_id", "decided_at"):
        check("answer_reviews 有 %s 列" % name, name in columns, columns)
    conn.close()

    legacy = os.path.join(WORK, "legacy-150")
    shutil.copytree(data_dir, legacy)
    conn = sqlite3.connect(os.path.join(legacy, "app.db"))
    conn.execute("DROP TABLE answer_reviews")
    conn.commit()
    conn.close()
    check("模拟 1.5.0 库：迁移前没有审核表", not answers_schema_ready(os.path.join(legacy, "app.db")))
    created = ensure_answers_schema(os.path.join(legacy, "app.db"))
    check("旧库自动补建审核表", answers_schema_ready(os.path.join(legacy, "app.db")), created)
    check("迁移前留下备份文件",
          any(name.startswith("app.db.bak-") for name in os.listdir(legacy)), os.listdir(legacy))
    again = ensure_answers_schema(os.path.join(legacy, "app.db"))
    check("重复迁移是空操作", again is None, again)

    bad = os.path.join(WORK, "legacy-bad")
    shutil.copytree(data_dir, bad)
    conn = sqlite3.connect(os.path.join(bad, "app.db"))
    conn.execute("DROP TABLE answer_reviews")
    conn.commit()
    conn.close()
    os.environ["MISTAKE_FAULT"] = "answers_commit"
    try:
        ensure_answers_schema(os.path.join(bad, "app.db"))
        failed = False
    except Exception:
        failed = True
    finally:
        os.environ.pop("MISTAKE_FAULT", None)
    check("迁移中途失败会抛错", failed)
    conn = sqlite3.connect(os.path.join(bad, "app.db"))
    left = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    check("失败后没有留下半张审核表", "answer_reviews" not in left, left)
    check("失败后旧数据仍在", users == 1, users)
    check("失败后备份文件保留",
          any(name.startswith("app.db.bak-") for name in os.listdir(bad)), os.listdir(bad))

    section("二、提示词与解析")
    sample = {"question_no": "1", "unit": "一元一次方程", "question_text": "3(x-2)=2x+5",
              "standard_solution": "x=11", "student_steps": "3x-2=2x+5", "missing": 0,
              "student_name": "张三", "student_code": "XY001", "batch": "9月作业"}
    prompt = R.build_prompt(sample)
    for forbidden in ("张三", "XY001", "七年级 1 班", "9月作业"):
        check("提示词不含 %s" % forbidden, forbidden not in prompt, prompt[:200])
    check("提示词带分析标记", R.PROMPT_MARKER in prompt)
    check("提示词包含学生步骤", "3x-2=2x+5" in prompt)

    good = json.dumps({"causes": [
        {"cause": "去括号漏乘", "evidence": "3x-2=2x+5", "reason": "应为 3x-6"},
        {"cause": "移项不变号", "evidence": "题里没有这一句", "reason": "编造证据"},
    ], "note": "第一条确定", "uncertain": False}, ensure_ascii=False)
    parsed = R.parse_suggestion(good, "3(x-2)=2x+5\n3x-2=2x+5")
    check("丢弃原文里找不到的证据", [c["cause"] for c in parsed["causes"]] == ["去括号漏乘"], parsed)
    check("保留可核对的证据原文", parsed["causes"][0]["evidence"] == "3x-2=2x+5", parsed)
    check("证据匹配忽略空格与换行",
          R.parse_suggestion(json.dumps({"causes": [{"cause": "x", "evidence": "3x - 2 = 2x + 5",
                                                     "reason": "r"}], "note": "", "uncertain": False}),
                             "3x-2=2x+5")["causes"] != [])
    fake_only = json.dumps({"causes": [{"cause": "编的", "evidence": "原文没有", "reason": "x"}],
                            "note": "看不清", "uncertain": False}, ensure_ascii=False)
    parsed = R.parse_suggestion(fake_only, "3x-2=2x+5")
    check("证据全部不成立时标为不确定", parsed["uncertain"] is True and not parsed["causes"], parsed)
    check("不确定时保留模型说明", "看不清" in parsed["note"], parsed)
    four = json.dumps({"causes": [{"cause": "c%d" % i, "evidence": "3x-2=2x+5", "reason": "r"}
                                  for i in range(4)], "note": "", "uncertain": False})
    check("最多保留 3 条错因", len(R.parse_suggestion(four, "3x-2=2x+5")["causes"]) == 3)
    try:
        R.parse_suggestion("这不是 JSON", "3x-2=2x+5")
        bad_json = False
    except R.AnswerReviewError:
        bad_json = True
    check("返回内容不是 JSON 时报错", bad_json)

    section("三、导入不联网，模拟模式不发请求")
    st = Stub()
    svc.save_ai_settings("real", st.base, "stub-vision", "sk-test")
    before = len(st.records())
    imported = svc.import_answers(cid, csv_text([row(), row(记录编号="A2", 学生步骤="")]), "9月作业")
    check("导入成功两条", imported["counts"]["new"] == 2, imported["counts"])
    check("CSV 导入没有发任何网络请求", len(st.records()) == before, len(st.records()) - before)

    items = svc.answer_reviews(cid)["items"]
    first = [item for item in items if item["record_key"] == "A1"][0]
    missing = [item for item in items if item["record_key"] == "A2"][0]

    svc.save_ai_settings("mock", "", "")
    before = len(st.records())
    mock = svc.analyse_answer(cid, first["id"], consent=True)["review"]
    check("模拟模式不发请求", len(st.records()) == before, len(st.records()) - before)
    check("模拟模式给出待核对记录", mock["status"] == "pending", mock["status"])
    check("模拟模式明确说不确定", mock["uncertain"] is True, mock["causes"])
    check("模拟模式不编造错因", mock["causes"] == [], mock["causes"])
    missing_review = svc.analyse_answer(cid, missing["id"], consent=True)["review"]
    check("缺测作答不发请求也不给错因",
          missing_review["uncertain"] is True and not missing_review["causes"], missing_review)

    section("四、真实通道（本地桩）")
    # 【R1-07 改写点】verify_review_160.py 原第 257 行附近
    #   原断言内容：真实模式下 analyse 成功、桩收到 1 次带「作答分析」标记的请求、请求体不含
    #     身份字段，并校验模型给的两条候选错因与证据；随后校验「未确认就不发送」。
    #   改写依据：R1-07；PRD §4.2 / §4.5；规格 §5「业务环境只运行固定规则与人工流程，
    #     不发起个体化生成式请求」（题目、标准解法、学生步骤都是可关联个人的内容）。
    #   新断言内容：真实通道一律 ai_isolation、桩 0 次请求、没有写入建议记录；
    #     候选解析的三条语义（接受 2 条、每条带证据、信息不足标不确定）改用保留在代码里的
    #     parse_suggestion 组件直接验证，覆盖不缩水。
    svc.save_ai_settings("real", st.base, "stub-vision", "sk-test")
    before = len(st.records())
    reviews_before = review_count(data_dir)
    try:
        asked = svc.analyse_answer(cid, first["id"], consent=True)
    except Exception as exc:
        asked = {"ok": False, "error_code": getattr(exc, "code", ""),
                 "error": getattr(exc, "message", str(exc))}
    check("真实模式返回被拦断（原：真实模式返回成功）", asked.get("error_code") == "ai_isolation", asked)
    records = st.records()[before:]
    check("一次请求都没发（原：只发了一次请求）", len(records) == 0, len(records))
    forbidden_text = asked.get("error") or ""
    check("拦断文案写明没有把任何内容发出去（原：请求带作答分析标记）",
          "没有把任何内容发出去" in forbidden_text, forbidden_text[:80])
    for forbidden in ("张三", "XY001", "七年级 1 班", "teacher"):
        check("请求不含 %s" % forbidden, forbidden not in forbidden_text)
    check("拦断没有写入建议记录（原：建议标为待核对）", review_count(data_dir) == reviews_before,
          (review_count(data_dir), reviews_before))
    ok_parsed = R.parse_suggestion(json.dumps(stub.REVIEW_RESULT, ensure_ascii=False),
                                  first["student_steps"])
    check("桩返回两条错因都被接受", len(ok_parsed["causes"]) == 2, ok_parsed["causes"])
    check("建议带证据", all(c["evidence"] for c in ok_parsed["causes"]), ok_parsed["causes"])

    uncertain_parsed = R.parse_suggestion(json.dumps(stub.REVIEW_UNCERTAIN, ensure_ascii=False),
                                          first["student_steps"])
    check("模型说信息不足时如实展示",
          uncertain_parsed["uncertain"] is True and not uncertain_parsed["causes"], uncertain_parsed)

    try:
        svc.analyse_answer(cid, first["id"], consent=False)
        no_consent = ""
    except Exception as exc:
        no_consent = getattr(exc, "code", "")
    check("真实模式没有确认就不发送", no_consent == "ai_isolation", no_consent)

    section("五、老师修改、确认、拒绝")
    # 【同上改写点】原第 284 行用「真实通道刚生成的那条建议」做后续流程的夹具。
    #   真实通道已被 R1-07 拦断，待核对建议只能来自本机模拟（第三节已建）或历史数据；
    #   老师改错因 → 确认成错题 → 拒绝 这条人工流程本身一字未改。
    pending = mock
    edited = [{"cause": "去括号漏乘 6", "evidence": "3x-2=2x+5", "reasoning": "老师改过"}]
    saved = svc.update_answer_review(cid, pending["id"], edited, "老师备注")
    check("修改保存到这条建议", saved["causes"] == edited, saved["causes"])
    check("修改后仍是待核对", saved["status"] == "pending", saved["status"])

    done = svc.confirm_answer_review(cid, pending["id"])
    check("确认后状态为已确认", done["status"] == "confirmed", done["status"])
    check("确认后记录错题编号", bool(done["mistake_id"]), done)
    mistake = svc.get_mistake(done["mistake_id"])
    check("生成的是已确认错题", mistake["status"] == "confirmed", mistake["status"])
    check("错题保留老师的错因", mistake["possible_causes"] == ["去括号漏乘 6"], mistake["possible_causes"])
    check("错题保留老师备注", "老师备注" in (mistake.get("advice_text") or ""), mistake.get("advice_text"))
    after_answer = svc.answer_reviews(cid)["items"]
    original = [item for item in after_answer if item["record_key"] == "A1"][0]
    check("原始作答没有被改写", original["student_steps"].startswith("3(x-2)"), original["student_steps"])
    check("列表显示已确认", original["review_status"] == "confirmed", original["review_status"])

    try:
        svc.confirm_answer_review(cid, pending["id"])
        duplicate = False
    except Exception as exc:
        duplicate = "已经确认" in str(exc)
    check("重复确认被拒绝", duplicate)
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    count = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    conn.close()
    check("重复确认没有多建错题", count == 1, count)

    second = missing_review   # 【同上改写点】原为真实通道新生成的一条；改用第三节模拟模式已建的那条
    rejected = svc.reject_answer_review(cid, second["id"])
    check("拒绝后状态为已拒绝", rejected["status"] == "rejected", rejected["status"])
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    count = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    conn.close()
    check("拒绝不生成错题", count == 1, count)

    section("六、统计口径")
    stats = svc.class_stats(cid)
    check("已确认错题进入统计", stats["summary"]["confirmed"] == 1, stats["summary"])
    check("未确认建议不计入统计", stats["summary"]["pending"] == 0, stats["summary"])

    section("七、账号与班级隔离")
    other = os.path.join(WORK, "other")
    os.makedirs(other)
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    src = sqlite3.connect(os.path.join(other, "app.db"))
    src.close()
    conn.backup(sqlite3.connect(os.path.join(other, "app.db")))
    conn.close()
    svc2 = DesktopService(other)
    svc2.register("teacher2", "pw123456")
    svc2.login("teacher2", "pw123456")
    try:
        svc2.answer_reviews(cid)
        crossed = False
    except Exception:
        crossed = True
    check("另一个账号看不到这个班的作答", crossed)

    svc2.login("teacher", "pw123456")
    other_cls = svc2.create_class("七年级 2 班")
    try:
        svc2.answer_reviews(other_cls["id"])
        own = True
    except Exception:
        own = False
    check("同一账号可以看自己的新班级", own)

    section("八、备份与恢复")
    user_id = sqlite3.connect(os.path.join(data_dir, "app.db")).execute(
        "SELECT id FROM users WHERE username='teacher'").fetchone()[0]
    zip_path = os.path.join(WORK, "backup.zip")
    manifest = B.create_backup(data_dir, zip_path, user_id)
    check("备份清单包含审核记录", manifest["counts"].get("answer_reviews") == 2, manifest["counts"])

    restore_dir = os.path.join(WORK, "restore")
    os.makedirs(restore_dir)
    holder = sqlite3.connect(os.path.join(data_dir, "app.db"))
    holder.backup(sqlite3.connect(os.path.join(restore_dir, "app.db")))
    holder.close()
    staging = os.path.join(restore_dir, "staging")
    checked, _relation = B.inspect_backup(zip_path, staging)
    result = B.apply_staged(staging, restore_dir, user_id, "1.6.0", mode="restore")
    check("恢复成功", result["counts"]["answer_reviews"] == 2, result["counts"])
    conn = sqlite3.connect(os.path.join(restore_dir, "app.db"))
    joined = conn.execute(
        "SELECT r.status, m.id, a.record_key FROM answer_reviews r"
        " JOIN mistakes m ON m.id = r.mistake_id JOIN answers a ON a.id = r.answer_id").fetchall()
    conn.close()
    check("恢复后审核记录仍指向正确的错题与作答",
          len(joined) == 1 and joined[0][0] == "confirmed" and joined[0][2] == "A1", joined)

    section("九、五类作答场景（本地桩按场景返回）")
    # 【同上改写点】原流程：把作答发出去，由桩按场景返回，再看产品解析结果。
    #   现在个体作答不允许外发，场景响应改为直接喂给保留的解析组件（parse_suggestion），
    #   下面 17 条断言的名字与语义全部不变：只看解析后的错因、证据、不确定标记。
    svc.save_ai_settings("real", st.base, "stub-vision", "sk-test")
    scenes = [
        ("S1", "2(x+3)=10", "2x+6=10；2x=4；x=2", "2(x+3)=10\n2x+6=10\n2x=4\nx=2"),
        ("S2", "2(x+3)=10", "2x+6=10；2x=4；x=2", "2(x+3)=10\n2x+6=10\n2x=10+6\n2x=16\nx=8"),
        ("S3", "2(x+3)=10", "2x+6=10；2x=4；x=2", "2(x+3)=10\nx=2"),
        ("S4", "3x-2=2x+5", "3x-2x=5+2；x=7", "3x-2=2x+5\n3x-2x=5+2\nx=7\n答：x=9"),
        ("S5", "2(x+3)=10", "2x+6=10；2x=4；x=2", "x=2"),
        ("S6", "2(x+3)=10", "2x+6=10；2x=4；x=2", "2(x+3)=10\n2x=6\nx=3"),
    ]
    scene_steps = {key: steps for key, _q, _sol, steps in scenes}
    svc.import_answers(cid, csv_text([
        row(记录编号=key, 题目内容=question, 标准解法=solution, 学生步骤=steps, 来源批次="场景")
        for key, question, solution, steps in scenes
    ]), "场景")
    scene_items = {item["record_key"]: item for item in svc.answer_reviews(cid, "场景")["items"]}
    check("场景作答已导入", len(scene_items) == 6, sorted(scene_items))

    # 【R1-07 改写用夹具】真实通道已停用，待核对建议改由本机模拟建立（不联网）。
    #   后面的失败、截断、重开三段仍验证「这些人工记录不会被 AI 路径改动」，语义与原来一致。
    svc.save_ai_settings("mock", "", "")
    for _key in ("S2", "S3"):
        svc.analyse_answer(cid, scene_items[_key]["id"], consent=True)
    svc.save_ai_settings("real", st.base, "stub-vision", "sk-test")

    def analyse_scene(key, case):
        """组件级：把桩的场景响应直接交给解析器，不发起任何网络请求。"""
        stub.REVIEW["mode"] = "case"
        stub.REVIEW["case"] = case
        return R.parse_suggestion(json.dumps(stub.review_payload(), ensure_ascii=False),
                                 scene_steps[key])

    right = analyse_scene("S1", "correct")
    check("正确步骤：不给错因", right["causes"] == [], right["causes"])
    check("正确步骤：写明未发现明确错误", "未发现明确错误" in right["note"], right["note"])

    sign = analyse_scene("S2", "sign")
    check("符号错误：给出一条错因", [c["cause"] for c in sign["causes"]] == ["移项不变号"], sign["causes"])
    check("符号错误：证据取自原始步骤", evidence_ok(sign, scene_steps["S2"]), sign["causes"])
    check("符号错误：不确定标为否", sign["uncertain"] is False, sign["uncertain"])

    skipped = analyse_scene("S3", "skipped")
    check("步骤缺失：给出一条错因", len(skipped["causes"]) == 1, skipped["causes"])
    check("步骤缺失：证据取自原始步骤", evidence_ok(skipped, scene_steps["S3"]), skipped["causes"])
    check("步骤缺失：保留不确定标记", skipped["uncertain"] is True, skipped["uncertain"])

    conflict = analyse_scene("S4", "conflict")
    check("答案与步骤矛盾：指出不一致", [c["cause"] for c in conflict["causes"]] == ["答案与步骤不一致"],
          conflict["causes"])
    check("答案与步骤矛盾：证据是最后那行", evidence_ok(conflict, scene_steps["S4"]), conflict["causes"])

    weak = analyse_scene("S5", "insufficient")
    check("信息不足：不强行给错因", weak["causes"] == [], weak["causes"])
    check("信息不足：标为不确定", weak["uncertain"] is True, weak["uncertain"])
    check("信息不足：保留模型说明", "看不出" in weak["note"], weak["note"])

    faked = analyse_scene("S6", "fabricated")
    check("编造的证据被丢弃", faked["causes"] == [], faked["causes"])
    check("丢弃证据时写明已忽略", "已忽略" in faked["note"], faked["note"])
    check("丢弃证据后标为不确定", faked["uncertain"] is True, faked["uncertain"])

    section("十、服务失败与主动重试")
    # 【同上改写点】原断言：服务返回 400 / 截断 / 超时时 analyse 返回失败并保留可读原因，
    #   重试需要老师再确认，确认后重试成功且结果仍待核对。
    #   现在个体作答不会被发出去，失败形态统一是 ai_isolation；
    #   「不新增记录、原建议与原作答不变、不静默改用模拟」这几条原样保留（并改成更强的前后对比）。
    target = scene_items["S2"]["id"]
    before = {item["record_key"]: item for item in svc.answer_reviews(cid, "场景")["items"]}
    before_count = review_count(data_dir)
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    providers_before = [item[0] for item in conn.execute(
        "SELECT provider FROM answer_reviews WHERE answer_id=?", (target,))]
    conn.close()
    try:
        stub.FAIL["mode"] = "400"
        try:
            failed = svc.analyse_answer(cid, target, consent=True)
        except Exception as exc:  # 失败也可能以异常形式抛出，两种都算失败
            failed = {"ok": False, "error": getattr(exc, "message", str(exc)),
                      "error_code": getattr(exc, "code", "")}
    finally:
        stub.FAIL["mode"] = "ok"
    check("服务失败时返回失败",
          failed.get("ok") is False and failed.get("error_code") == "ai_isolation", failed)
    check("失败时给出可读原因", bool(failed.get("error")), failed)
    check("失败后没有新增建议记录", review_count(data_dir) == before_count,
          (review_count(data_dir), before_count))
    kept = {item["record_key"]: item for item in svc.answer_reviews(cid, "场景")["items"]}["S2"]
    check("失败后原来的建议还在", kept["review_id"] == before["S2"]["review_id"], kept["review_id"])
    check("失败后原始作答没有变化", kept["student_steps"] == before["S2"]["student_steps"])
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    providers = [item[0] for item in conn.execute(
        "SELECT provider FROM answer_reviews WHERE answer_id=?", (target,))]
    conn.close()
    check("失败没有静默改用模拟分析", providers == providers_before, providers)

    try:
        svc.analyse_answer(cid, target, consent=False)
        retry_no_consent = ""
    except Exception as exc:
        retry_no_consent = getattr(exc, "code", "")
    check("重试仍然被拦断（原：重试仍需老师确认发送范围）", retry_no_consent == "ai_isolation", retry_no_consent)

    stub.REVIEW["mode"] = "case"
    stub.REVIEW["case"] = "sign"
    try:
        retried = svc.analyse_answer(cid, target, consent=True)
        retried_error = ""
    except Exception as exc:
        retried = {}
        retried_error = getattr(exc, "code", "")
    check("老师确认后重试仍被拦断（原：老师确认后重试成功）", retried_error == "ai_isolation", retried_error)
    after_retry = {item["record_key"]: item for item in svc.answer_reviews(cid, "场景")["items"]}["S2"]
    check("重试不改动原有待核对建议（原：重试结果仍是待核对）", after_retry["review_id"] == before["S2"]["review_id"]
          and after_retry["review_status"] == "pending",
          (after_retry["review_id"], after_retry["review_status"]))

    section("十一、关闭重开后状态与关联")
    reopened_service = DesktopService(data_dir)
    reopened_service.login("teacher", "pw123456")
    reopened = {item["record_key"]: item for item in reopened_service.answer_reviews(cid)["items"]}
    check("重开后已确认状态仍在", reopened["A1"]["review_status"] == "confirmed",
          reopened["A1"]["review_status"])
    check("重开后仍指向同一错题", reopened["A1"]["mistake_id"] == done["mistake_id"],
          reopened["A1"]["mistake_id"])
    check("重开后待核对仍是待核对", reopened["S2"]["review_status"] == "pending",
          reopened["S2"]["review_status"])
    again_stats = reopened_service.class_stats(cid)
    check("新增待核对建议不计入已审核统计",
          again_stats["summary"]["confirmed"] == 1, again_stats["summary"])

    section("十二、输出上限、截断与失败处理")
    job = {"image_bytes": b"\xff\xd8\xff\xe0stub-jpeg", "image_mime": "image/jpeg",
           "question_text": "3(x-2)=2x+5", "solution_text": "x=11"}

    def provider(api, timeout=5, base=None):
        return vision.CloudVisionProvider({
            "mode": "custom", "api": api, "model": "stub-vision",
            "base_url": base or st.base, "api_key": "sk-test",
            "timeout": timeout, "service": "本地模拟服务"})

    check("图片请求（Chat Completions）带 max_tokens 32000",
          provider("chat").build_request(job).get("max_tokens") == 32000,
          provider("chat").build_request(job).get("max_tokens"))
    check("图片请求（Messages）带 max_tokens 32000",
          provider("messages").build_request(job).get("max_tokens") == 32000,
          provider("messages").build_request(job).get("max_tokens"))
    # 【同上改写点】下面这些是「通道组件」的测试：R1-07 之后业务路径不再发请求，
    #   组件本身仍保留给离线工具用途（本机回环），所以必须显式带上 purpose=offline_tool，
    #   并且目标仍是本机桩。业务侧没有任何调用点（入口层已拦断，见第四节）。
    provider("chat").chat_text("【作答分析】文字请求", "系统提示",
                               purpose=vision.PURPOSE_OFFLINE_TOOL)
    check("文字请求（Chat Completions）带 max_tokens 32000",
          st.records()[-1].get("max_tokens") == 32000, st.records()[-1].get("max_tokens"))
    provider("messages").chat_text("【作答分析】文字请求", "系统提示",
                                   purpose=vision.PURPOSE_OFFLINE_TOOL)
    check("文字请求（Messages）带 max_tokens 32000",
          st.records()[-1].get("max_tokens") == 32000, st.records()[-1].get("max_tokens"))

    trunc_target = scene_items["S3"]["id"]
    snap = {item["record_key"]: item for item in svc.answer_reviews(cid, "场景")["items"]}
    trunc_before = review_count(data_dir)
    try:
        stub.FAIL["mode"] = "length"
        # 【同上改写点】原为 svc.analyse_answer（业务外发）；改用离线工具通道拿同一份截断错误，
        #   下面 6 条断言（截断文案、作答未被改动、建议条数不变）全部保留。
        try:
            provider("chat").chat_text("【作答分析】文字请求", "系统提示",
                                       purpose=vision.PURPOSE_OFFLINE_TOOL)
            truncated = {"ok": True}
        except vision.AnalysisError as exc:
            truncated = {"ok": False, "error": exc.message}
    finally:
        stub.FAIL["mode"] = "ok"
    check("finish_reason=length 时按失败返回", truncated.get("ok") is False, truncated)
    check("截断时说明没有完整结果", "截断" in (truncated.get("error") or ""), truncated.get("error"))
    check("截断时说明作答未被改动", "作答内容没有被改动" in (truncated.get("error") or ""),
          truncated.get("error"))
    check("截断没有新增建议记录", review_count(data_dir) == trunc_before,
          (review_count(data_dir), trunc_before))
    kept_s3 = {item["record_key"]: item for item in svc.answer_reviews(cid, "场景")["items"]}["S3"]
    check("截断后原始作答不变", kept_s3["student_steps"] == snap["S3"]["student_steps"])
    check("截断后原建议与错因不变",
          kept_s3["review_id"] == snap["S3"]["review_id"] and
          [c["cause"] for c in kept_s3.get("causes") or []] ==
          [c["cause"] for c in snap["S3"].get("causes") or []], kept_s3.get("causes"))

    try:
        stub.FAIL["mode"] = "maxtokens"
        provider("messages").chat_text("【作答分析】文字请求", "系统提示",
                                       purpose=vision.PURPOSE_OFFLINE_TOOL)
        message_truncated = ""
    except vision.AnalysisError as exc:
        message_truncated = exc.message
    finally:
        stub.FAIL["mode"] = "ok"
    check("stop_reason=max_tokens 时也按截断处理", "截断" in message_truncated, message_truncated)

    try:
        stub.FAIL["mode"] = "400"
        # 【同上改写点】原为 svc.analyse_answer；改用离线工具通道验证同一套 HTTP 错误映射。
        try:
            provider("chat").chat_text("【作答分析】文字请求", "系统提示",
                                       purpose=vision.PURPOSE_OFFLINE_TOOL)
            rejected = {"ok": True}
        except vision.AnalysisError as exc:
            rejected = {"ok": False, "error": exc.message}
    finally:
        stub.FAIL["mode"] = "ok"
    check("服务拒绝时返回失败", rejected.get("ok") is False, rejected)
    check("服务拒绝时说明是请求被拒", "HTTP 400" in (rejected.get("error") or ""), rejected.get("error"))
    check("服务拒绝时不写模拟结果", "没有生成分析结果" in (rejected.get("error") or ""),
          rejected.get("error"))

    try:
        stub.FAIL["mode"] = "slow"
        try:
            provider("chat", timeout=0.3).chat_text("【作答分析】x", "sys",
                                                    purpose=vision.PURPOSE_OFFLINE_TOOL)
            timeout_error = ""
        except vision.AnalysisError as exc:
            timeout_error = exc.message
    finally:
        stub.FAIL["mode"] = "ok"
    check("超时提示可以重试", "超时" in timeout_error and "重试" in timeout_error, timeout_error)

    try:
        stub.FAIL["mode"] = "garbage"
        provider("chat").chat_text("【作答分析】x", "sys", purpose=vision.PURPOSE_OFFLINE_TOOL)
        garbage_error = ""
    except vision.AnalysisError as exc:
        garbage_error = exc.message
    finally:
        stub.FAIL["mode"] = "ok"
    check("返回不是 JSON 时明确提示", "不是有效的 JSON" in garbage_error, garbage_error)

    try:
        provider("chat", timeout=2, base="http://127.0.0.1:1/v1").chat_text(
            "【作答分析】x", "sys", purpose=vision.PURPOSE_OFFLINE_TOOL)
        net_error = ""
    except vision.AnalysisError as exc:
        net_error = exc.message
    check("连不上服务时提示检查网络或地址", "无法连接" in net_error, net_error)

    stub.REVIEW["mode"] = "case"
    stub.REVIEW["case"] = "skipped"
    # 【同上改写点】原为 svc.analyse_answer 重试成功；现在重试只发生在离线工具通道上，
    #   「拿到的结果仍待核对、带证据、同一条作答只留一条待核对建议」三条语义保持不变。
    scene_row = {item["record_key"]: item for item in svc.answer_reviews(cid, "场景")["items"]}["S3"]
    retry_text, _retry_usage = provider("chat").chat_text(
        R.build_prompt(scene_row), "系统提示", purpose=vision.PURPOSE_OFFLINE_TOOL)
    recovered = R.parse_suggestion(retry_text, scene_row["student_steps"])
    check("失败后主动重试可以拿到结果", bool(recovered.get("causes") or recovered.get("note")),
          recovered)
    after_retry_s3 = {item["record_key"]: item
                      for item in svc.answer_reviews(cid, "场景")["items"]}["S3"]
    check("重试结果仍是待核对",
          after_retry_s3["review_id"] == snap["S3"]["review_id"]
          and after_retry_s3["review_status"] == "pending",
          (after_retry_s3["review_id"], after_retry_s3["review_status"]))
    check("重试结果带证据", bool(recovered.get("causes")), recovered.get("causes"))
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    rows = conn.execute("SELECT COUNT(*) FROM answer_reviews WHERE answer_id=? AND status='pending'",
                        (trunc_target,)).fetchone()[0]
    conn.close()
    check("同一条作答只留一条待核对建议", rows == 1, rows)

    section("十三、1.6.0 数据在 1.6.2 上打开、备份与恢复")
    fixture = os.path.join(ROOT, "tests", "fixtures", "legacy160", "app.db")
    upgrade = os.path.join(WORK, "upgrade-160")
    shutil.copytree(os.path.dirname(fixture), upgrade)
    up = DesktopService(upgrade)
    up.login("exe_teacher", "pw123456")
    up_classes = up.list_classes()
    check("1.6.0 数据能登录并读到班级", len(up_classes) == 1, up_classes)
    up_cid = up_classes[0]["id"]
    up_items = up.answer_reviews(up_cid)["items"]
    check("1.6.0 作答记录仍在", len(up_items) == 2, len(up_items))
    up_confirmed = [item for item in up_items if item["review_status"] == "confirmed"]
    up_conn = sqlite3.connect(os.path.join(upgrade, "app.db"))
    up_rows = up_conn.execute("SELECT status, mistake_id FROM answer_reviews ORDER BY id").fetchall()
    up_join_exists = up_conn.execute(
        "SELECT COUNT(*) FROM answer_reviews r JOIN mistakes m ON m.id = r.mistake_id"
        " WHERE r.status='confirmed'").fetchone()[0]
    up_steps = up_conn.execute("SELECT student_steps FROM answers WHERE record_key='EX1'").fetchone()[0]
    up_conn.close()
    check("1.6.0 已确认记录仍指向错题",
          up_rows == [("confirmed", 1), ("rejected", None)] and up_join_exists == 1, up_rows)
    check("1.6.0 原始作答仍可读", up_steps == "3x-2=2x+5;3x-2x=5-6;x=-1", up_steps)
    check("1.6.0 列表按最新一条建议展示", up_confirmed == [], [i["review_status"] for i in up_items])
    up_stats = up.class_stats(up_cid)
    check("1.6.0 已确认错题仍计入统计", up_stats["summary"]["confirmed"] == 1, up_stats["summary"])
    check("1.8.0 版本号", vision.VERSION == "1.8.0", vision.VERSION)
    check("数据库结构版本为 4", SCHEMA_VERSION == "4", SCHEMA_VERSION)
    check("备份格式为 6", B.FORMAT == 6, B.FORMAT)
    check("旧格式仍在可读列表", tuple(B.READABLE_FORMATS) == (6, 5, 4, 3, 2), B.READABLE_FORMATS)

    up_uid = sqlite3.connect(os.path.join(upgrade, "app.db")).execute(
        "SELECT id FROM users LIMIT 1").fetchone()[0]
    up_zip = os.path.join(WORK, "upgrade-backup.zip")
    up_manifest = B.create_backup(upgrade, up_zip, up_uid)
    check("1.6.0 数据导出的是 format 6 备份", up_manifest.get("format") == 6, up_manifest.get("format"))
    up_target = os.path.join(WORK, "upgrade-restore")
    os.makedirs(up_target)
    holder = sqlite3.connect(os.path.join(upgrade, "app.db"))
    holder.backup(sqlite3.connect(os.path.join(up_target, "app.db")))
    holder.close()
    up_staging = os.path.join(up_target, "staging")
    B.inspect_backup(up_zip, up_staging)
    up_result = B.apply_staged(up_staging, up_target, up_uid, "1.6.2", mode="restore")
    check("恢复后审核记录数与导出时一致",
          up_result["counts"]["answer_reviews"] == 2, up_result["counts"])
    up_conn = sqlite3.connect(os.path.join(up_target, "app.db"))
    up_join = up_conn.execute(
        "SELECT r.status, m.id FROM answer_reviews r JOIN mistakes m ON m.id = r.mistake_id").fetchall()
    up_conn.close()
    check("恢复后已确认记录仍指向同一条错题", up_join == [("confirmed", 1)], up_join)

    st.close()
    code = 0 if RESULTS["fail"] == 0 else 1
    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"], RESULTS["pass"], RESULTS["fail"]))
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
