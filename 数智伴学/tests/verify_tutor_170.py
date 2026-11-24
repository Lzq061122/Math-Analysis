"""1.7.0 个人学情与辅导建议：源码级测试（本地桩，零真实云端）。

覆盖：辅导建议表与迁移、汇总口径（只含已确认）、近期权重与边界日期、
缺失日期与未来日期、知识点重复、提示词不含身份字段、需要老师确认才发送、
证据必须来自原文、编造内容被丢弃、模拟模式不编造、编辑保存与重开、
数据变化后的过期提示、空状态、账号与班级隔离、旧版本数据升级、
备份恢复与跨电脑导入、旧版本读新备份的提示。
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
from app import records
from app import stats as S
from app import tutor as T
from app.db import (SCHEMA_VERSION, Database, ensure_tutor_schema, tutor_schema_ready)
from desktop.service import DesktopService
from logmeta import environment_lines, exit_line

import ai_stub_server as stub

WORK = os.path.join(ROOT, "tests", ".tmp-tutor-170")
LOG_PATH = os.path.join(ROOT, "docs", "verify-tutor-170.txt")
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


def add_mistake(data_dir, class_id, student_id, kps, days_ago=None, occurred=None,
                status="confirmed", question="2x+3=11", steps="2x=11-3\nx=8",
                errors=(), causes=()):
    if occurred is None and days_ago is not None:
        occurred = (date.today() - timedelta(days=days_ago)).isoformat()
    db = Database(os.path.join(data_dir, "app.db"))
    mistake_id = records.insert_mistake(
        db, class_id, student_id, "", "", 0, 0, {"occurred_on": occurred or ""},
        {"question_text": question, "solution_text": steps,
         "observed_errors": list(errors), "possible_causes": list(causes), "mode": "mock"},
    )
    with db.tx() as conn:
        conn.execute(
            "UPDATE mistakes SET status=?, knowledge_points=? WHERE id=?",
            (status, json.dumps(list(kps), ensure_ascii=False), mistake_id),
        )
    return mistake_id


def advice_row(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    row = conn.execute("SELECT student_id, content, teacher_edited, fingerprint FROM tutor_advice"
                       ).fetchall()
    conn.close()
    return row


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
    wang = svc.create_student(cid, "王五", "")
    sun = svc.create_student(cid, "孙七", "")
    zhao = svc.create_student(cid, "赵六", "")

    section("一、数据结构与迁移")
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    columns = [row[1] for row in conn.execute("PRAGMA table_info(tutor_advice)")]
    conn.close()
    check("新库有 tutor_advice 表", "tutor_advice" in tables, tables)
    check("按学生唯一（一人一份建议）", "idx_tutor_student" in indexes, indexes)
    check("有按班级查的索引", "idx_tutor_class" in indexes, indexes)
    for name in ("class_id", "student_id", "content", "ai_content", "evidence", "fingerprint",
                 "teacher_edited", "mode", "generated_at", "updated_at"):
        check("tutor_advice 有 %s 列" % name, name in columns, columns)
    check("数据库结构版本为 4", SCHEMA_VERSION == "4", SCHEMA_VERSION)

    legacy = os.path.join(WORK, "legacy-160")
    shutil.copytree(data_dir, legacy)
    conn = sqlite3.connect(os.path.join(legacy, "app.db"))
    conn.execute("DROP TABLE tutor_advice")
    conn.commit()
    conn.close()
    check("模拟旧库：迁移前没有辅导建议表", not tutor_schema_ready(os.path.join(legacy, "app.db")))
    created = ensure_tutor_schema(os.path.join(legacy, "app.db"))
    check("旧库自动补建辅导建议表", tutor_schema_ready(os.path.join(legacy, "app.db")), created)
    check("迁移前留下备份文件",
          any(name.startswith("app.db.bak-") for name in os.listdir(legacy)), os.listdir(legacy))
    check("重复迁移是空操作", ensure_tutor_schema(os.path.join(legacy, "app.db")) is None)

    broken = os.path.join(WORK, "legacy-broken")
    shutil.copytree(data_dir, broken)
    conn = sqlite3.connect(os.path.join(broken, "app.db"))
    conn.execute("DROP TABLE tutor_advice")
    conn.execute("INSERT INTO classes(user_id, name, grade, note, created_at, updated_at)"
                 " VALUES (1,'迁移实验班','七年级','','2026-11-18','2026-11-18')")
    conn.commit()
    conn.close()
    os.environ["MISTAKE_FAULT"] = "tutor_begin"
    try:
        ensure_tutor_schema(os.path.join(broken, "app.db"))
        raised = False
    except Exception as exc:
        raised = "个人辅导建议" in str(exc)
    finally:
        os.environ.pop("MISTAKE_FAULT", None)
    check("开始事务失败时明确报错", raised)
    check("失败后没有留下半张表", not tutor_schema_ready(os.path.join(broken, "app.db")))
    conn = sqlite3.connect(os.path.join(broken, "app.db"))
    kept = conn.execute("SELECT COUNT(*) FROM classes WHERE name='迁移实验班'").fetchone()[0]
    conn.close()
    check("失败回滚后原有数据还在", kept == 1, kept)

    section("二、汇总只算这名学生的已确认错题")
    a1 = add_mistake(data_dir, cid, zhang["id"], ["移项"], days_ago=5, question="2x+3=11",
                     steps="2x=11+3\nx=7", errors=["移项没有变号"])
    a2 = add_mistake(data_dir, cid, zhang["id"], ["移项"], days_ago=60, question="3x-1=5")
    a3 = add_mistake(data_dir, cid, zhang["id"], ["移项"], days_ago=200, question="4x=8")
    add_mistake(data_dir, cid, zhang["id"], ["去括号"], days_ago=10, question="2(x+3)=10")
    add_mistake(data_dir, cid, li["id"], ["去括号"], days_ago=2, question="3(x-1)=6")
    data = svc.student_insight(cid, zhang["id"])
    summary = data["summary"]
    names = {item["name"]: item for item in summary["items"]}
    check("只出现这名学生的问题", set(names) == {"移项", "去括号"}, list(names))
    check("移项出现 3 次", names["移项"]["count"] == 3, names["移项"])
    check("移项加权分 3+2+1=6", names["移项"]["weight"] == 6, names["移项"])
    check("最近发生日期取最近一条", names["移项"]["last_on"] == (date.today() - timedelta(days=5)).isoformat(),
          names["移项"]["last_on"])
    check("可以追溯到错题编号", sorted(names["移项"]["mistake_ids"]) == sorted([a1, a2, a3]),
          names["移项"]["mistake_ids"])
    check("页面显示当前学生", data["student"]["id"] == zhang["id"] and data["student"]["name"] == "张三",
          data["student"])
    check("空的学生没有问题", svc.student_insight(cid, zhao["id"])["summary"]["items"] == [])

    add_mistake(data_dir, cid, zhang["id"], ["分数"], days_ago=1, status="pending", question="x/2=3")
    add_mistake(data_dir, cid, zhang["id"], ["分数"], days_ago=1, status="returned", question="x/2=4")
    after = svc.student_insight(cid, zhang["id"])["summary"]
    check("待核对与已退回不进入汇总", "分数" not in [item["name"] for item in after["items"]],
          [item["name"] for item in after["items"]])
    check("已确认条数不含待核对", after["confirmed"] == 4, after["confirmed"])
    class_stats = svc.class_stats(cid)
    check("班级学情统计仍然可用", class_stats["summary"]["confirmed"] == 5, class_stats["summary"])

    section("三、权重与日期边界")
    for days, expected in ((0, 3), (30, 3), (31, 2), (90, 2), (91, 1)):
        add_mistake(data_dir, cid, li["id"], ["去括号边界"], days_ago=days, question="边界题")
    li_items = {item["name"]: item for item in svc.student_insight(cid, li["id"])["summary"]["items"]}
    check("边界日期权重 3/3/2/2/1", li_items["去括号边界"]["weight"] == 11, li_items["去括号边界"])
    check("边界日期次数 5 次", li_items["去括号边界"]["count"] == 5, li_items["去括号边界"])
    check("权重函数单独可用", [T.weight_for(d) for d in (0, 30, 31, 90, 91)] == [3, 3, 2, 2, 1])

    add_mistake(data_dir, cid, wang["id"], [], occurred="", question="没有日期的题")
    add_mistake(data_dir, cid, wang["id"], ["未来题"],
                occurred=(date.today() + timedelta(days=3)).isoformat(), question="未来题")
    add_mistake(data_dir, cid, wang["id"], ["坏日期"], occurred="二〇二六年", question="坏日期题")
    wang_summary = svc.student_insight(cid, wang["id"])["summary"]
    check("没有日期的单独列出", len(wang_summary["undated"]) == 1, wang_summary["undated"])
    reasons = sorted(item["reason"] for item in wang_summary["anomalies"])
    check("未来日期提示数据异常", "日期在今天之后" in reasons, reasons)
    check("无法识别的日期也提示", "日期无法识别" in reasons, reasons)
    check("异常日期不参与加权", wang_summary["counted"] == 0, wang_summary["counted"])
    check("没有任何知识点的归入未分类", wang_summary["items"] == [], wang_summary["items"])

    add_mistake(data_dir, cid, sun["id"], ["移项", "移项", "去括号"], days_ago=3, question="重复知识点")
    sun_items = {item["name"]: item for item in svc.student_insight(cid, sun["id"])["summary"]["items"]}
    check("同一条错题里的重复知识点只算一次", sun_items["移项"]["count"] == 1, sun_items["移项"])
    check("一条错题涉及两个知识点时两边都计", set(sun_items) == {"移项", "去括号"}, list(sun_items))
    add_mistake(data_dir, cid, zhao["id"], [], days_ago=3, question="没有分类")
    zhao_items = svc.student_insight(cid, zhao["id"])["summary"]["items"]
    check("没有知识点时标为未分类", [item["name"] for item in zhao_items] == ["未分类"], zhao_items)

    section("四、发送内容不含身份字段")
    prompt = T.build_prompt(svc.student_insight(cid, zhang["id"])["summary"])
    for forbidden in ("张三", "XY001", "七年级 1 班", "teacher"):
        check("提示词不含 %s" % forbidden, forbidden not in prompt)
    check("提示词带个人学情标记", T.PROMPT_MARKER in prompt)
    check("提示词带题目原文", "2x+3=11" in prompt)
    check("提示词带学生步骤原文", "x=7" in prompt)
    check("提示词写明计分规则", "30" in prompt and "90" in prompt)

    st = Stub()
    svc.save_ai_settings("custom", st.base, "stub-vision", "sk-x", service_name="本地模拟服务")
    try:
        svc.generate_student_advice(cid, zhang["id"], consent=False)
        refused = False
    except Exception as exc:
        refused = "确认发送范围" in str(exc)
    check("真实模式没有确认就不发送", refused)
    check("没有确认时一条请求都没发", st.records() == [], st.records())

    section("五、生成建议（本地桩）")
    result = svc.generate_student_advice(cid, zhang["id"], consent=True)
    check("生成成功", result.get("ok") is True, result)
    records_seen = st.records()
    check("桩收到一条辅导建议请求", len(records_seen) == 1, len(records_seen))
    record = records_seen[-1]
    check("请求标为 tutor", record.get("kind") == "tutor", record.get("kind"))
    text = record.get("text") or ""
    check("请求带个人学情标记", T.PROMPT_MARKER in text)
    for forbidden in ("张三", "XY001", "七年级 1 班", "teacher"):
        check("请求不含 %s" % forbidden, forbidden not in text)
    advice = result["advice"]
    content = advice["content"]
    check("建议只针对最优先的问题", "1." in content and "2." not in content, content)
    check("建议写明排序依据", "排序" in content, content)
    check("建议引用了原文证据", "2x+3=11" in content or "x=7" in content, content)
    check("建议里有具体做法", "做法" in content, content)
    check("建议声明不是成绩或能力评级", "不是学生成绩或能力评级" in content)
    check("建议记录生成来源", advice["mode"] == "本地模拟服务", advice["mode"])
    check("生成后仍可追溯到证据", advice_row(data_dir)[0][3] != "", advice_row(data_dir))

    section("六、编造的问题与证据会被丢弃")
    stub.TUTOR["mode"] = "fabricate"
    fake = svc.generate_student_advice(cid, zhang["id"], consent=True)["advice"]["content"]
    check("编造的问题名被忽略", "灵魂出窍" not in fake, fake)
    check("说明里写明问题名被忽略", "不在汇总里" in fake, fake)
    check("编造的证据被忽略", "做题时心里默念口诀" not in fake, fake)
    check("说明里写明证据找不到", "找不到" in fake, fake)
    check("留下的仍是有效建议", "1." in fake, fake)

    section("七、证据不足时不强行下结论")
    stub.TUTOR["mode"] = "uncertain"
    unsure = svc.generate_student_advice(cid, zhang["id"], consent=True)["advice"]["content"]
    check("证据不足时写明不确定", "不确定" in unsure or "证据" in unsure, unsure)
    check("没有编出具体问题编号", "1." not in unsure, unsure)

    stub.TUTOR["mode"] = "empty"
    empty = svc.generate_student_advice(cid, zhang["id"], consent=True)["advice"]["content"]
    check("模型没给问题时明确说明", "没有给出" in empty or "证据" in empty, empty)
    stub.TUTOR["mode"] = "ok"

    section("八、老师编辑、保存、重开与过期提示")
    saved = svc.save_student_advice(cid, zhang["id"], "老师改过的建议：先练移项。")
    check("保存后以老师版本为准", saved["advice"]["content"].startswith("老师改过"), saved["advice"])
    check("记录按老师修改保存", saved["advice"]["teacher_edited"] is True, saved["advice"])
    reopened = DesktopService(data_dir)
    reopened.login("teacher", "pw123456")
    again = reopened.student_insight(cid, zhang["id"])["advice"]
    check("重开后还能看到老师的版本", again["content"].startswith("老师改过"), again)
    check("重开后改动标记还在", again["teacher_edited"] is True, again)
    check("数据没变时不提示过期", again["stale"] is False, again["stale"])
    add_mistake(data_dir, cid, zhang["id"], ["移项"], days_ago=0, question="2x+2=2")
    later = svc.student_insight(cid, zhang["id"])["advice"]
    check("新增已确认错题后提示可能需要更新", later["stale"] is True, later["stale"])
    refreshed = svc.generate_student_advice(cid, zhang["id"], consent=True)["advice"]
    check("重新生成后不再提示过期", refreshed["stale"] is False, refreshed["stale"])
    check("重新生成会覆盖老师的改动", refreshed["teacher_edited"] is False, refreshed["teacher_edited"])

    section("九、空状态与隔离")
    rows_before = len(advice_row(data_dir))
    blank = svc.create_student(cid, "周八", "")
    try:
        svc.generate_student_advice(cid, blank["id"], consent=True)
        empty_raise = ""
    except Exception as exc:
        empty_raise = str(exc)
    check("没有已确认错题时不能生成", "还没有已确认的错题" in empty_raise, empty_raise)
    check("空状态不写建议行", len(advice_row(data_dir)) == rows_before, advice_row(data_dir))

    other = svc.create_class("七年级 2 班")
    try:
        svc.student_insight(other["id"], zhang["id"])
        cross_class = ""
    except Exception as exc:
        cross_class = str(exc)
    check("学生不属于这个班级时拒绝", "不属于该班级" in cross_class, cross_class)

    other_dir, other_svc = make_service("other")
    other_cls = other_svc.create_class("七年级 1 班")
    other_svc.create_student(other_cls["id"], "张三", "XY001")
    try:
        other_svc.student_insight(cid, 9999)
        leaked = False
    except Exception as exc:
        leaked = "找不到" in str(exc) or "不存在" in str(exc) or "404" in str(exc)
    check("另一个账号看不到这个学生的学情", leaked)

    section("十、旧版本数据与备份")
    up_dir = os.path.join(WORK, "upgrade")
    os.makedirs(up_dir)
    shutil.copy(LEGACY_160, os.path.join(up_dir, "app.db"))
    Database(os.path.join(up_dir, "app.db"))
    up_svc = DesktopService(up_dir)
    up_svc.login("exe_teacher", "pw123456")
    check("旧库升级后补上建议表", tutor_schema_ready(os.path.join(up_dir, "app.db")))
    up_classes = up_svc.list_classes()
    up_cls = up_classes[0]
    up_students = up_svc.list_students(up_cls["id"])
    up_insight = up_svc.student_insight(up_cls["id"], up_students[0]["id"])
    check("旧库里的学生能打开个人学情", up_insight["student"]["name"] == up_students[0]["name"],
          up_insight["student"])
    check("旧库已确认错题进入汇总", up_insight["summary"]["confirmed"] >= 1,
          up_insight["summary"]["confirmed"])

    conn = sqlite3.connect(os.path.join(data_dir, "app.db"))
    uid = conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    conn.close()
    advice_total = len(advice_row(data_dir))
    backup_zip = os.path.join(WORK, "backup-170.zip")
    manifest = B.create_backup(data_dir, backup_zip, uid)
    check("备份格式为 6", manifest.get("format") == 6, manifest.get("format"))
    check("备份清单里有辅导建议条数", manifest["counts"].get("tutor_advice") == advice_total,
          manifest.get("counts"))
    with zipfile.ZipFile(backup_zip) as bundle:
        names = bundle.namelist()
    check("备份里带数据库与清单", {"manifest.json", "app.db"} <= set(names), names)

    target = os.path.join(WORK, "restore")
    os.makedirs(target)
    holder = sqlite3.connect(os.path.join(data_dir, "app.db"))
    holder.backup(sqlite3.connect(os.path.join(target, "app.db")))
    holder.close()
    staging = os.path.join(target, "staging")
    B.inspect_backup(backup_zip, staging)
    restored = B.apply_staged(staging, target, uid, "1.7.0", mode="restore")
    check("恢复后辅导建议条数一致", restored["counts"].get("tutor_advice") == 1, restored["counts"])
    target_svc = DesktopService(target)
    target_svc.login("teacher", "pw123456")
    restored_advice = target_svc.student_insight(cid, zhang["id"])["advice"]
    check("恢复后建议内容还在", restored_advice["content"].startswith("【优先辅导问题】"),
          restored_advice["content"][:20])

    import_dir = os.path.join(WORK, "import")
    os.makedirs(import_dir)
    Database(os.path.join(import_dir, "app.db"))
    imp_svc = DesktopService(import_dir)
    imp_svc.register("teacher2", "pw123456")
    imp_svc.login("teacher2", "pw123456")
    imp_target = os.path.join(import_dir, "staging")
    B.inspect_backup(backup_zip, imp_target)
    imp_uid = sqlite3.connect(os.path.join(import_dir, "app.db")).execute(
        "SELECT id FROM users LIMIT 1").fetchone()[0]
    imported = B.apply_staged(imp_target, import_dir, imp_uid, "1.7.0", mode="import")
    check("跨电脑导入带上辅导建议", imported["counts"].get("tutor_advice") == 1, imported["counts"])
    imp_svc2 = DesktopService(import_dir)
    imp_svc2.login("teacher2", "pw123456")
    imp_cls = None
    imp_student = None
    for _class in imp_svc2.list_classes():
        for _student in imp_svc2.list_students(_class["id"]):
            if _student["name"] == "张三":
                imp_cls, imp_student = _class, _student
                break
        if imp_student is not None:
            break
    check("导入后仍能打开这个学生的建议",
          imp_svc2.student_insight(imp_cls["id"], imp_student["id"])["advice"] is not None)

    legacy_backup = load_legacy_backup()
    try:
        legacy_backup.inspect_backup(backup_zip, os.path.join(WORK, "legacy-read"))
        legacy_error = ""
    except Exception as exc:
        legacy_error = str(exc)
    check("旧版本读 1.8 备份时给出提示", "备份" in legacy_error or "版本" in legacy_error, legacy_error)

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
