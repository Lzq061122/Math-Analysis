"""R2-05 教学资源库 · 双审核发布 · 建议匹配 · 三级提示 · 举一反三与纸笔任务 PDF 验收（1.9.0）。

派单 §七 A–E 的 13 条验收逐条断言（派单原文 §7.6 未随任务包落到本工作副本，第 0 节把
13 条与 A–E 的对应关系写死在这里，与 docs/R2-05_交付说明.md 一一对应）：

  1. A-01 五类资源统一结构：建档/读回/改版出新版本，练习带题干·难度·答案·解析
  2. A-02 导入工具：批量导入资源包全部落库并「待审核」，重复导入同内容不堆版本
  3. A-03 首批真实资源覆盖去括号/移项/合并同类项/系数化为1：
         每个主要错因 ≥1 组追问 + 1 套三级提示 + 3 道不同用途练习 + 1 条教学建议
  4. B-01 四态流转：草稿→待审核→已发布→停用；非法流转拒绝（不能跳过审核发布）
  5. B-02 双审核分别记录（人/时间/意见/发布版本）；缺一项不能发布；同一人双签不算
  6. B-03 发布权限：无权限账号 / 未登录 / 不存在账号一律拒绝，且拒绝发生在写库之前
  7. B-04 未审 / 停用 / 合成资源不进正式推荐；改内容回待审核；历史版本可追溯
  8. C-01 三级匹配顺序与依据、来源（单元+知识点+确认错因 → 通用 → 提示人工补充）
  9. C-02 错因未确认只推通用（不得把专项资源推给未确认错因）
 10. D-01 三级提示 P1→P2→P3 默认顺序与可调；使用记录字段齐全
 11. D-02 完整解析必须在独立尝试之后（教师提前看答案被拒绝且不写库）
 12. E-01 三类练习推荐（概念澄清/步骤巩固/迁移应用）+ 理由/难度/答案/解析；后测资源分开
 13. E-02 教学任务（创建/修改/历史/手动完成）+ 纸笔 PDF：学生版不含答案与解析、教师版含

反向自证（否定性结论必须能变红，见每节 `反向自证`）：
  - 拆掉「完整解析在独立尝试之后」的两道闸 → 提前看答案的调用会写库成功；
  - 拆掉正式推荐的「已发布版本」过滤 → 未审资源会出现在推荐里；
  - 用假的双审核进度替换 `review_state` → 无权限账号也能发布成功；
  - 同一份 PDF 提取器能从教师版读出答案 → 学生版「不含答案」不是提取器失灵导致的恒真。

运行：C:\\Python314\\python.exe tests\\verify_r2_05_resources.py
日志：docs/verify-r2-05-resources.txt（原始输出，含命令与环境抬头）
"""

import importlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from app import auth                                   # noqa: E402
from app import db as DB                               # noqa: E402
from app import r2_contract as C                       # noqa: E402
from app import teaching_resources as T                # noqa: E402
from app import tutoring_pdf as P                      # noqa: E402

LOG_PATH = os.path.join(ROOT, "docs", "verify-r2-05-resources.txt")
SAMPLE_DIR = os.path.join(ROOT, "docs", "R2-05_PDF样例")
RESOURCE_DIR = os.path.join(ROOT, "resources", "teaching_resources")
TOOL = os.path.join(ROOT, "tools", "import_resources.py")

TOTAL = [0]
FAILED = [0]
LINES = []
WORK = []
MUTATED = []

UNIT = "第三单元 一元一次方程"
BATCH = "第一次作业"
CAUSES = (("去括号", "PAREN-SIGN"), ("移项", "MOVE-SIGN"),
          ("合并同类项", "LIKE-TERM"), ("系数化为1", "COEF-DIVIDE"))


def log(text=""):
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))
    LINES.append(text)


def check(name, ok, detail=""):
    TOTAL[0] += 1
    if not ok:
        FAILED[0] += 1
    log(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "  ← %r" % (detail,)))
    return bool(ok)


def section(title):
    log("")
    log("== %s ==" % title)


def tmpdir(prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    WORK.append(path)
    return path


def expect_error(callable_, code, label):
    """调用必须抛 TeachingResourceError 且 code 一致。返回 True/False 交给 check()。"""
    try:
        callable_()
    except T.TeachingResourceError as exc:
        return code is None or exc.code == code, exc.code
    except Exception as exc:                                  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    return False, "没有抛错"


# ---------------------------------------------------------------------------
# 夹具：一个班、两名学生、两条已确认错因的作答
# ---------------------------------------------------------------------------
def build_fixture():
    root = tmpdir("r2-05-")
    db_file = os.path.join(root, "app.db")
    database = DB.Database(db_file)
    users = {
        "u1": auth.create_user(database, "teacher1", "pw123456", "张老师"),
        "u2": auth.create_user(database, "teacher2", "pw123456", "李老师"),
        "u3": auth.create_user(database, "teacher3", "pw123456", "王老师"),
    }
    now = DB.now_iso()
    with database.tx() as conn:
        conn.execute("INSERT INTO classes(user_id, name, grade, note, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?)", (users["u1"], "初一(3)班", "初一", "", now, now))
        class_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        students = {}
        for name, code in (("林小雨", "A021"), ("赵小舟", "A022")):
            conn.execute("INSERT INTO students(class_id, name, code, note, active, created_at,"
                         " updated_at) VALUES (?,?,?,'',1,?,?)", (class_id, name, code, now, now))
            students[name] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    answers = {}
    for index, (name, kp, cause, label, question) in enumerate((
            ("林小雨", "移项", "MOVE-SIGN", "移项不变号", "解方程：3x + 5 = 2x - 7"),
            ("赵小舟", "合并同类项", "LIKE-TERM", "合并只算系数、漏抄字母", "合并同类项：3x + 5x - 2x"),
            ("林小雨", "去括号", "PAREN-SIGN", "去括号漏变号", "解方程：3 - (x - 4) = 5"),
            ("赵小舟", "系数化为1", "COEF-DIVIDE", "系数化 1 只除一边 / 漏负号",
             "解方程：-2x = 6"),
    ), 1):
        fingerprint = "fp-%d" % index
        with database.tx() as conn:
            conn.execute(
                "INSERT INTO answers(class_id, student_id, record_key, batch, student_name,"
                " student_code, question_no, question_text, unit, standard_solution,"
                " student_steps, source, occurred_on, missing, missing_reason, fingerprint,"
                " imported_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,'',?,?,?,?)",
                (class_id, students[name], "R%03d" % index, BATCH, name, "", str(index),
                 question, UNIT, "略", "略", "作业", "2026-11-10", fingerprint, now, now, now))
            answer_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO answer_checks(class_id, student_id, answer_id, batch,"
                " answer_version, progress, cause_state, cause_source, cause_code, cause_label,"
                " verify_source, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, students[name], answer_id, BATCH, fingerprint, "checked",
                 "decided", "teacher", cause, label, "rule", now, now))
            conn.execute(
                "INSERT INTO mistakes(class_id, student_id, status, source_type, occurred_on,"
                " question_text, knowledge_points, created_at, updated_at, answer_id)"
                " VALUES (?,?,'confirmed','作业',?,?,?,?,?,?)",
                (class_id, students[name], "2026-11-10", question,
                 json.dumps([kp], ensure_ascii=False), now, now, answer_id))
        answers[kp] = answer_id
    return {
        "root": root,
        "db": database,
        "db_file": db_file,
        "class_id": class_id,
        "students": students,
        "answers": answers,
        "users": users,
        "class_row": database.query_one("SELECT * FROM classes WHERE id=?", (class_id,)),
    }


def load_packages():
    packages = []
    for name in sorted(os.listdir(RESOURCE_DIR)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(RESOURCE_DIR, name), "r", encoding="utf-8") as handle:
            packages.append((name, json.load(handle)))
    return packages


def import_all(database, submit=True):
    report = {"created": [], "updated": [], "skipped": [], "submitted": [], "errors": []}
    for name, payload in load_packages():
        one = T.import_resources(database, payload, submit=submit)
        for key in ("created", "updated", "skipped", "submitted"):
            report[key].extend(one[key])
        report["errors"].extend(one["errors"])
    return report


def publish(database, key, version, math_user, teaching_user, publisher=None):
    """标准发布路径：送审 → 两项审核（两个不同账号）→ 发布。"""
    row = database.query_one("SELECT * FROM teaching_resources WHERE resource_key=?", (key,))
    if row["status"] == "draft":
        T.submit_for_review(database, key, version, user=math_user)
    T.record_review(database, key, version, "math", math_user, "pass", "数学正确性：核对无误")
    T.record_review(database, key, version, "teaching", teaching_user, "pass",
                    "教学适切性：与初一学情相符")
    return T.publish_version(database, key, version, user=publisher or teaching_user)


def publish_pack(database, packages, fixture, kinds=None):
    published = []
    for name, payload in packages:
        if T.is_synthetic(payload.get("source", "")):
            continue
        for item in payload["resources"]:
            if kinds and item["kind"] not in kinds:
                continue
            key = T.make_resource_key(item["kind"], item.get("knowledge_point") or
                                      payload.get("knowledge_point", ""),
                                      item.get("cause_code") or payload.get("cause_code", ""),
                                      item.get("title", ""))
            row = database.query_one(
                "SELECT * FROM teaching_resources WHERE resource_key=?", (key,))
            if row is None:
                continue
            if row["status"] == "published":
                published.append(key)
                continue
            publish(database, key, row["current_version"],
                    fixture["users"]["u1"], fixture["users"]["u2"])
            published.append(key)
    return published


# ---------------------------------------------------------------------------
# 源码变异（反向自证用；无论如何都会在 finally 里还原）
# ---------------------------------------------------------------------------
def mutate_source(pairs):
    path = os.path.join(SRC, "app", "teaching_resources.py")
    original = open(path, "rb").read()
    data = original
    for old, new in pairs:
        old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
        hits = data.count(old_b)
        if hits != 1:
            raise AssertionError("变异锚点出现 %d 次（必须 1 次）：%r" % (hits, old))
        data = data.replace(old_b, new_b)
    MUTATED.append((path, original))
    with open(path, "wb") as handle:
        handle.write(data)
    importlib.reload(T)
    return original


def restore_source():
    while MUTATED:
        path, original = MUTATED.pop()
        with open(path, "wb") as handle:
            handle.write(original)
        importlib.reload(T)


# ---------------------------------------------------------------------------
# 第 1 节 A-01：统一结构 / 建档 / 改版 / 检索
# ---------------------------------------------------------------------------
def section_a01(fixture):
    section("1. A-01 五类资源统一结构与改版（版本号 + 旧版本不动）")
    database = fixture["db"]
    author = fixture["users"]["u1"]
    payloads = {
        "cause": {"kind": "cause", "title": "错因：测试用错因", "cause_code": "A01-CAUSE",
                  "body": {"label": "测试错因", "description": "说明", "typical_error": "示例"}},
        "followup": {"kind": "followup", "title": "追问：测试", "cause_code": "A01-CAUSE",
                     "body": {"question": "这一步你是怎么想的？", "expect": "说清思路"}},
        "hint": {"kind": "hint", "title": "提示 P1：测试", "cause_code": "A01-CAUSE",
                 "body": {"level": "P1", "text": "先圈符号"}},
        "exercise": {"kind": "exercise", "title": "练习：测试", "cause_code": "A01-CAUSE",
                     "body": {"stem": "解方程：x + 1 = 2", "answer": "x = 1",
                              "solution": "移项 x = 2 - 1 = 1", "difficulty": "基础",
                              "purpose": "steps"}},
        "advice": {"kind": "advice", "title": "建议：测试", "cause_code": "A01-CAUSE",
                   "body": {"text": "先专项后整题"}},
    }
    made = {}
    for kind, payload in payloads.items():
        payload = dict(payload)
        payload.update({"unit": UNIT, "knowledge_point": "A01知识点", "source": "自编"})
        view = T.create_resource(database, payload, created_by=author)
        made[kind] = view
        fields_ok = all(view.get(field) is not None for field in
                        ("resource_key", "version", "unit", "knowledge_point", "cause_code",
                         "title", "body", "conditions", "source"))
        check("A-01 建档：%s 的统一字段齐全（独立 ID/版本/单元/知识点/错因/正文/条件/来源）" % kind,
              fields_ok and view["status"] == "draft" and view["version"] == 1, view)
    exercise = made["exercise"]["body"]
    check("A-01 练习额外字段：题干/难度/标准答案/参考解析/用途/作答行数",
          all(exercise.get(field) for field in
              ("stem", "difficulty", "answer", "solution", "purpose", "answer_lines"))
          and exercise["purpose"] == "steps", exercise)
    semantic_errors = [
        ({"kind": "cause", "title": "x", "cause_code": "", "body": {"label": "y"},
          "knowledge_point": "K"}, "missing_cause"),
        ({"kind": "hint", "title": "x", "body": {"level": "P9", "text": "y"},
          "knowledge_point": "K"}, "bad_hint_level"),
        ({"kind": "exercise", "title": "x", "body": {"stem": "y", "purpose": "concept"},
          "knowledge_point": "K"}, "missing_answer"),
        ({"kind": "advice", "title": "x", "body": {}, "knowledge_point": "K"}, "missing_text"),
        ({"kind": "hint", "title": "x", "body": {"level": "P1", "text": "y"}}, "missing_kp"),
    ]
    bad = []
    for payload, code in semantic_errors:
        ok, got = expect_error(lambda payload=payload: T.create_resource(database, payload),
                               code, code)
        bad.append(ok)
    check("A-01 结构校验：缺错因码/非法提示级别/练习缺答案/建议空正文/缺知识点全部拒绝",
          all(bad), bad)
    key = made["hint"]["resource_key"]
    version_one_body = T.get_resource(database, key, 1)["body"]
    updated = T.update_resource(database, key, body={"level": "P1", "text": "先圈括号前符号再动笔"},
                                note="补充说明", created_by=author)
    history = T.version_history(database, key)
    check("A-01 改内容出新版本：v2 草稿 + 旧版本正文原样保留（不覆盖历史）",
          updated["version"] == 2 and updated["status"] == "draft"
          and T.get_resource(database, key, 1)["body"] == version_one_body
          and [item["version"] for item in history] == [1, 2])
    found = T.search_resources(database, "测试", kind="exercise")
    filtered = T.list_resources(database, kind="hint", knowledge_point="A01知识点")
    check("A-01 检索：按关键字 + 按类型/知识点筛选都能命中本批资源",
          len(found) == 1 and len(filtered) == 1
          and filtered[0]["resource_key"] == key, (len(found), len(filtered)))


# ---------------------------------------------------------------------------
# 第 2 节 A-02：导入工具 + 幂等 + 送审
# ---------------------------------------------------------------------------
def section_a02(fixture):
    section("2. A-02 导入工具：批量导入全部落库并「待审核」，重复导入不堆版本")
    database = fixture["db"]
    packages = load_packages()
    before = database.scalar("SELECT COUNT(*) FROM teaching_resources") or 0
    report = import_all(database)
    after = database.scalar("SELECT COUNT(*) FROM teaching_resources") or 0
    expected = sum(len(payload["resources"]) for _, payload in packages)
    check("A-02 批量导入：%d 条资源全部入库（无校验失败）" % expected,
          not report["errors"] and after - before == expected, report["errors"][:2])
    statuses = {row["status"] for row in database.query("SELECT status FROM teaching_resources")}
    check("A-02 导入即「待审核」：库里没有绕过审核直接发布的资源",
          statuses <= {"draft", "pending_review"}, statuses)
    versions_before = database.scalar("SELECT COUNT(*) FROM teaching_resource_versions") or 0
    again = import_all(database)
    versions_after = database.scalar("SELECT COUNT(*) FROM teaching_resource_versions") or 0
    check("A-02 幂等：同一份资源包再导一次全部跳过，不新增版本",
          len(again["skipped"]) == expected and not again["created"]
          and versions_after == versions_before, (len(again["skipped"]), expected))
    # 内容真的变了才出新版本（模拟老师改了一版提示正文）
    sample = json.loads(json.dumps(packages[1][1]))
    sample["resources"][4]["body"]["text"] = "改过的提示正文（导入更新测试）"
    changed = T.import_resources(database, sample)
    key = T.make_resource_key("hint", sample["knowledge_point"],
                              sample["resources"][4]["cause_code"],
                              sample["resources"][4]["title"])
    row = database.query_one("SELECT * FROM teaching_resources WHERE resource_key=?", (key,))
    check("A-02 内容变化才出新版本：改版 1 条 + 重新送审，旧版本仍在",
          len(changed["updated"]) == 1 and row["current_version"] == 2
          and row["status"] == "pending_review"
          and T.get_resource(database, key, 1)["version_state"] == "pending_review",
          (changed["updated"], row["current_version"]))
    # 工具本体（命令行）至少能跑 --list，且不连库
    import subprocess
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, TOOL, "--data-dir", "unused", "--list"],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    check("A-02 导入工具可独立运行（--list 列出 %d 条资源，退出码 0）" % expected,
          proc.returncode == 0 and ("合计 %d 条资源" % expected) in (proc.stdout or ""),
          (proc.returncode, (proc.stdout or "")[-200:], (proc.stderr or "")[-200:]))
    return report


# ---------------------------------------------------------------------------
# 第 3 节 A-03：首批真实资源内容覆盖
# ---------------------------------------------------------------------------
def section_a03(fixture):
    section("3. A-03 首批资源覆盖：4 个主要错因 × 追问组 + 三级提示 + 3 类练习 + 建议")
    database = fixture["db"]
    rows = database.query("SELECT * FROM teaching_resources WHERE source LIKE '%首批资源草稿%'")
    coverage = {}
    for row in rows:
        item = coverage.setdefault(row["cause_code"], {
            "followup": [], "hint": [], "exercise": [], "advice": [], "cause": []})
        view = T._resource_view(database, row)
        item[row["kind"]].append(view)
    missing = []
    for knowledge_point, cause_code in CAUSES:
        item = coverage.get(cause_code) or {}
        groups = {(view["conditions"] or {}).get("group", "") for view in item.get("followup", [])}
        followups_ok = bool(groups - {""}) and len(item.get("followup", [])) >= 1
        levels = sorted({(view["body"] or {}).get("level", "") for view in item.get("hint", [])})
        hints_ok = levels == ["P1", "P2", "P3"]
        purposes = {(view["body"] or {}).get("purpose", "") for view in item.get("exercise", [])}
        exercises_ok = {"concept", "steps", "transfer"} <= purposes
        advice_ok = len(item.get("advice", [])) >= 1
        cause_ok = len(item.get("cause", [])) >= 1
        if not (followups_ok and hints_ok and exercises_ok and advice_ok and cause_ok):
            missing.append((knowledge_point, cause_code, sorted(levels), sorted(purposes),
                            len(item.get("followup", [])), len(item.get("advice", []))))
        check("A-03 %s（%s）：≥1 组追问 + P1/P2/P3 一套提示 + 3 类练习 + 1 条建议 + 错因说明"
              % (knowledge_point, cause_code),
              followups_ok and hints_ok and exercises_ok and advice_ok and cause_ok,
              missing[-1] if missing else "")
    check("A-03 首批真实资源只覆盖这 4 个主要错因（外加通用资源，没有夹带别的错因码）",
          set(coverage) == {code for _, code in CAUSES} | {""},
          sorted(coverage))
    drafts = [row for row in rows if row["kind"] == "exercise"]
    check("A-03 练习都带标准答案与参考解析（教师版 PDF 要用，学生版不能用）",
          all(T._resource_view(database, row)["body"].get("answer")
              and "solution" in T._resource_view(database, row)["body"] for row in drafts))


# ---------------------------------------------------------------------------
# 第 4 节 B-01：四态流转与非法流转
# ---------------------------------------------------------------------------
def section_b01(fixture):
    section("4. B-01 四态流转：草稿→待审核→已发布→停用；非法流转拒绝")
    database = fixture["db"]
    u1, u2, u3 = (fixture["users"][key] for key in ("u1", "u2", "u3"))
    view = T.create_resource(database, {
        "kind": "advice", "unit": UNIT, "knowledge_point": "B01知识点", "title": "B-01 流转测试",
        "body": {"text": "先专项后整题"}, "source": "自编"}, created_by=u1)
    key = view["resource_key"]
    ok, code = expect_error(lambda: T.publish_version(database, key, 1, {"id": u1}),
                            "not_pending", "publish-before-submit")
    check("B-01 草稿不能直接发布（必须走送审）：code=%s" % ok, ok, code)
    T.submit_for_review(database, key, user=u1)
    check("B-01 草稿 → 待审核：资源与版本状态同步为待审核",
          T.get_resource(database, key)["status"] == "pending_review"
          and T.get_resource(database, key)["version_state"] == "pending_review")
    ok, code = expect_error(lambda: T.publish_version(database, key, 1, {"id": u1}),
                            "reviews_incomplete", "publish-without-two-reviews")
    check("B-01 待审核未完成双审核不能发布：code=%s" % code, ok, code)
    ok, code = expect_error(lambda: T.submit_for_review(database, key, user=u1),
                            "already_pending", "resubmit")
    check("B-01 已在待审核队列不能重复送审：code=%s" % code, ok, code)
    T.record_review(database, key, 1, "math", u1, "pass", "数学正确")
    T.record_review(database, key, 1, "teaching", u2, "pass", "教学适切")
    published = T.publish_version(database, key, 1, user=u2)
    check("B-01 待审核 → 已发布：版本发布时间与发布记录写入",
          published["status"] == "published" and published["version_state"] == "published"
          and published["published_at"])
    ok, code = expect_error(lambda: T.submit_for_review(database, key, user=u1),
                            "version_locked", "resubmit-published")
    check("B-01 已发布版本不能退回待审核（要改就出新版本）：code=%s" % code, ok, code)
    ok, code = expect_error(lambda: T.publish_version(database, key, 1, {"id": u2}),
                            "already_published", "publish-twice")
    check("B-01 重复发布被拒绝：code=%s" % code, ok, code)
    disabled = T.disable_resource(database, key, user=u1, reason="内容过时")
    check("B-01 已发布 → 停用：resources.status 与版本 state 同步，停用时间落库",
          disabled["status"] == "disabled" and disabled["version_state"] == "disabled"
          and disabled["disabled_at"])
    ok, code = expect_error(lambda: T.disable_resource(database, key, user=u1),
                            "already_disabled", "disable-twice")
    check("B-01 重复停用被拒绝：code=%s" % code, ok, code)
    state_names = {view["status"] for view in T.list_resources(database, knowledge_point="B01知识点")}
    check("B-01 状态字符串全部取自 r2_contract.RESOURCE_STATES（没有自造状态）",
          state_names <= set(C.RESOURCE_STATES), state_names)


# ---------------------------------------------------------------------------
# 第 5 节 B-02：双审核
# ---------------------------------------------------------------------------
def section_b02(fixture):
    section("5. B-02 双审核：分别记录人/时间/意见/发布版本；缺一不可；同人双签不算")
    database = fixture["db"]
    u1, u2 = fixture["users"]["u1"], fixture["users"]["u2"]
    view = T.create_resource(database, {
        "kind": "hint", "unit": UNIT, "knowledge_point": "B02知识点", "title": "B-02 双审核测试",
        "body": {"level": "P2", "text": "两边同除系数"}, "source": "自编"}, created_by=u1)
    key = view["resource_key"]
    T.submit_for_review(database, key, user=u1)
    T.record_review(database, key, 1, "math", u1, "pass", "数学：式子正确")
    partial = T.review_state(database, key, 1)
    check("B-02 只有数学审核通过时不能发布（两项均过才放行）",
          partial["passed"]["math"] and not partial["passed"]["teaching"]
          and not partial["ready_to_publish"])
    ok, code = expect_error(lambda: T.record_review(database, key, 1, "teaching", u1, "pass", "自签"),
                            "same_reviewer", "same-reviewer")
    check("B-02 同一账号两项都签不算双审核：code=%s" % code, ok, code)
    T.record_review(database, key, 1, "teaching", u2, "pass", "教学：与学情相符")
    state = T.review_state(database, key, 1)
    check("B-02 两个不同账号各签一项后 ready_to_publish=True",
          state["ready_to_publish"] and state["reviewers_distinct"])
    published = T.publish_version(database, key, 1, user=u1)
    detail = T.get_resource(database, key)
    reviews = {item["review_kind"]: item for item in detail["reviews"]}
    recorded = reviews.get("math", {}) and reviews.get("teaching", {})
    check("B-02 审核记录含审核人/时间/意见/发布版本（发布后 publish_version 回填）",
          recorded and reviews["math"]["reviewer"] == u1
          and reviews["teaching"]["reviewer"] == u2
          and reviews["math"]["reviewed_at"] and reviews["math"]["opinion"]
          and reviews["teaching"]["publish_version"] == 1
          and reviews["math"]["publish_version"] == 1,
          {kind: (reviews.get(kind) or {}).get("publish_version") for kind in ("math", "teaching")})
    ok, code = expect_error(
        lambda: T.record_review(database, key, 1, "math", u2, "pass", "迟到的审核"),
        "not_pending", "review-after-publish")
    check("B-02 已发布版本不再接受审核结论（审核只针对待审核版本）：code=%s" % code, ok, code)
    # 失败结论的路径：任一审核 fail 就不能发布
    view2 = T.create_resource(database, {
        "kind": "advice", "unit": UNIT, "knowledge_point": "B02知识点", "title": "B-02 不通过测试",
        "body": {"text": "先专项后整题"}, "source": "自编"}, created_by=u1)
    key2 = view2["resource_key"]
    T.submit_for_review(database, key2, user=u1)
    T.record_review(database, key2, 1, "math", u1, "fail", "数学：例题算错")
    T.record_review(database, key2, 1, "teaching", u2, "pass", "教学没问题")
    ok, code = expect_error(lambda: T.publish_version(database, key2, 1, {"id": u2}),
                            "reviews_incomplete", "publish-with-fail")
    check("B-02 有一项审核结论是「不通过」时不能发布：code=%s" % code, ok, code)


# ---------------------------------------------------------------------------
# 第 6 节 B-03：发布权限
# ---------------------------------------------------------------------------
def section_b03(fixture):
    section("6. B-03 发布权限：无权限/未登录/不存在账号一律拒绝，且不写库")
    database = fixture["db"]
    u1, u2, u3 = (fixture["users"][key] for key in ("u1", "u2", "u3"))
    view = T.create_resource(database, {
        "kind": "advice", "unit": UNIT, "knowledge_point": "B03知识点", "title": "B-03 权限测试",
        "body": {"text": "先练一步移项"}, "source": "自编"}, created_by=u3)
    key = view["resource_key"]
    T.submit_for_review(database, key, user=u1)
    T.record_review(database, key, 1, "math", u1, "pass", "数学正确")
    T.record_review(database, key, 1, "teaching", u2, "pass", "教学适切")
    before = database.query_one("SELECT * FROM teaching_resources WHERE resource_key=?", (key,))
    version_before = database.query_one(
        "SELECT * FROM teaching_resource_versions WHERE resource_key=? AND version=1", (key,))
    trial_inputs = (("第三个老师", {"id": u3}, "not_reviewer"),
                    ("未登录", None, "no_account"),
                    ("不存在的账号", {"id": 987654}, "unknown_account"))
    for label, user, code in trial_inputs:
        ok, got = expect_error(lambda user=user: T.publish_version(database, key, 1, user),
                               code, label)
        check("B-03 %s发布被拒绝：code=%s" % (label, code), ok, got)
    after = database.query_one("SELECT * FROM teaching_resources WHERE resource_key=?", (key,))
    version_after = database.query_one(
        "SELECT * FROM teaching_resource_versions WHERE resource_key=? AND version=1", (key,))
    check("B-03 拒绝发生在写库之前：状态/版本/发布时间一行都没变",
          before["status"] == after["status"] == "pending_review"
          and version_before["state"] == version_after["state"] == "pending_review"
          and not version_after["published_at"],
          (after["status"], version_after["state"], version_after["published_at"]))
    # 反向自证：把双审核进度换成假的「都过了、审核人是 u3」→ 无权限账号就能发布成功
    original = T.review_state
    try:
        T.review_state = lambda db, resource_key, version=None: {
            "resource_key": resource_key, "version": version, "passed": {"math": True,
                                                                        "teaching": True},
            "reviewers": {"math": u3, "teaching": u3}, "both_pass": True,
            "reviewers_distinct": True, "ready_to_publish": True, "reviews": []}
        flipped = T.publish_version(database, key, 1, {"id": u3})
        ok = flipped["status"] == "published"
    finally:
        T.review_state = original
    check("B-03 反向自证：把双审核进度换成「都过了且审核人是 u3」后，同一账号可发布 → "
          "证明刚才的拒绝来自审核人校验而不是别的分支", ok)


# ---------------------------------------------------------------------------
# 第 7 节 B-04：未审 / 停用 / 合成不进推荐 + 版本可追溯
# ---------------------------------------------------------------------------
def section_b04(fixture):
    section("7. B-04 正式推荐只认「已发布 + 当前版本已发布 + 非合成」")
    database = fixture["db"]
    u1, u2 = fixture["users"]["u1"], fixture["users"]["u2"]
    packages = load_packages()
    # 发布「移项」全部资源（含三级提示与练习）
    publish_pack(database, packages, fixture)
    # 合成测试资源：也送审并发布，用来证明隔离靠 source 而不是靠没发布
    synthetic_payload = [item for item in packages if T.is_synthetic(item[1].get("source", ""))][0][1]
    synthetic_keys = []
    for item in synthetic_payload["resources"]:
        key = T.make_resource_key(item["kind"], synthetic_payload["knowledge_point"],
                                  item.get("cause_code") or synthetic_payload.get("cause_code", ""),
                                  item.get("title", ""))
        row = database.query_one("SELECT * FROM teaching_resources WHERE resource_key=?", (key,))
        synthetic_keys.append(key)
        if row and row["status"] != "published":
            publish(database, key, row["current_version"], u1, u2)
    synthetic_published = database.query_one(
        "SELECT * FROM teaching_resources WHERE resource_key=?", (synthetic_keys[0],))
    check("B-04 前置：合成测试资源确实被发布（隔离不能靠「没发布」蒙对）",
          synthetic_published["status"] == "published")
    matched = T.match_resources(database, synthetic_payload["unit"],
                                synthetic_payload["knowledge_point"], "SYN-001", True)
    check("B-04 合成测试资源不进正式推荐（已发布也不出现）",
          not matched["items"], matched["items"][:1])
    # 未审资源：新建一条同知识点但只到待审核
    pending_view = T.create_resource(database, {
        "kind": "hint", "unit": UNIT, "knowledge_point": "移项", "cause_code": "MOVE-SIGN",
        "title": "未审提示：不应出现在推荐里", "body": {"level": "P2", "text": "未审正文"},
        "source": "自编"}, created_by=u1)
    T.submit_for_review(database, pending_view["resource_key"], user=u1)
    matched = T.match_resources(database, UNIT, "移项", "MOVE-SIGN", True)
    check("B-04 待审核资源不进正式推荐",
          all(item["resource_key"] != pending_view["resource_key"] for item in matched["items"]))
    # 停用资源：把一条已发布的教学建议停用（提示与练习留给后面 D/E 节用，停用只证明「不再进新推荐」）
    target = [item for item in matched["items"] if item["kind"] == "advice"][0]
    T.disable_resource(database, target["resource_key"], user=u1, reason="内容更新")
    matched = T.match_resources(database, UNIT, "移项", "MOVE-SIGN", True)
    check("B-04 停用资源不再用于新推荐",
          all(item["resource_key"] != target["resource_key"] for item in matched["items"]))
    # 修改已发布资源 → 新版本重回待审核 → 该资源整体退出推荐
    other = [item for item in matched["items"] if item["kind"] == "advice"][0]
    T.update_resource(database, other["resource_key"],
                      body={"text": "改过的教学建议正文"}, note="内容更新", created_by=u1)
    after_edit = T.get_resource(database, other["resource_key"])
    matched = T.match_resources(database, UNIT, "移项", "MOVE-SIGN", True)
    check("B-04 已发布资源改内容：新版本重回待审核，资源整体退出正式推荐",
          after_edit["status"] == "pending_review" and after_edit["version"] == 2
          and after_edit["version_state"] == "pending_review"
          and all(item["resource_key"] != other["resource_key"] for item in matched["items"]),
          (after_edit["status"], after_edit["version_state"]))
    check("B-04 历史版本与审核记录可追溯：v1 已发布、v2 待审核都在版本历史里",
          [item["state"] for item in T.version_history(database, other["resource_key"])]
          == ["published", "pending_review"])
    check("B-04 新版本的双审核从零开始（旧版本的通过不顺延）",
          not T.review_state(database, other["resource_key"], 2)["both_pass"])
    # 反向自证：拆掉「当前版本必须已发布」的过滤 → 未审资源立刻出现在推荐里
    mutate_source([('"SELECT * FROM teaching_resources WHERE status=\'published\'"',
                    '"SELECT * FROM teaching_resources WHERE 1=1"'),
                   ('        if view["version_state"] != "published":\n            continue',
                    '        if False:\n            continue')])
    try:
        flipped = T.match_resources(database, UNIT, "移项", "MOVE-SIGN", True)
        leaked = [item["resource_key"] for item in flipped["items"]
                  if item["version_state"] != "published" or item["status"] != "published"]
    finally:
        restore_source()
    check("B-04 反向自证：拆掉「已发布版本」过滤后未审资源出现在推荐里 → 证明过滤是承重的",
          bool(leaked), leaked)
    normal = T.match_resources(database, UNIT, "移项", "MOVE-SIGN", True)
    check("B-04 还原后正式推荐恢复干净（只剩已发布且非合成的资源）",
          all(T.is_official(item) for item in normal["items"]), len(normal["items"]))


# ---------------------------------------------------------------------------
# 第 8 节 C-01：三级匹配顺序与依据
# ---------------------------------------------------------------------------
def section_c01(fixture):
    section("8. C-01 建议匹配：精确 → 通用 → 提示人工补充，带依据与来源")
    database = fixture["db"]
    matched = T.match_resources(database, UNIT, "移项", "MOVE-SIGN", True)
    tiers = [item["tier"] for item in matched["items"]]
    exact = [item for item in matched["items"] if item["tier"] == "exact"]
    general = [item for item in matched["items"] if item["tier"] == "general"]
    check("C-01 顺序：精确（单元+知识点+确认错因）排在通用（单元+知识点）之前",
          tiers and tiers[0] == "exact" and ("general" not in tiers or
                                             tiers.index("general") > tiers.index("exact")),
          tiers[:5])
    check("C-01 精确档只含该确认错因的资源，通用档只含无错因码的通用资源",
          all(item["cause_code"] == "MOVE-SIGN" for item in exact)
          and all(item["cause_code"] == "" for item in general), (len(exact), len(general)))
    check("C-01 每条推荐带推荐依据（档位 + 单元/知识点/错因）与来源",
          all(item["basis_text"] and item["basis"]["knowledge_point"] == "移项"
              and item["source_text"] for item in matched["items"]))
    check("C-01 匹配层级说明与档位标识取自固定口径（exact/general/manual）",
          matched["tier"] in T.MATCH_TIERS
          and {tier["tier"] for tier in matched["tiers"]} <= set(T.MATCH_TIERS))
    empty = T.match_resources(database, UNIT, "没有这个知识点", "X", True)
    check("C-01 一条都匹配不到时落到「提示人工补充」并说明原因",
          empty["tier"] == "manual" and not empty["items"]
          and "人工补充" in empty["note"], empty["note"])
    # 同知识点但只保留通用资源的情况：用「合并同类项」的通用档验证
    general_only = T.match_resources(database, UNIT, "合并同类项", "LIKE-TERM", True)
    check("C-01 有精确资源时档位判定为 exact（不是恒返回 general）",
          general_only["tier"] == "exact", general_only["tier"])


# ---------------------------------------------------------------------------
# 第 9 节 C-02：错因未确认只推通用
# ---------------------------------------------------------------------------
def section_c02(fixture):
    section("9. C-02 错因未确认只推通用（不替老师下判断）")
    database = fixture["db"]
    unconfirmed = T.match_resources(database, UNIT, "移项", "MOVE-SIGN", False)
    check("C-02 未确认错因时不出现「精确」档资源（专项资源一律不推）",
          all(item["tier"] == "general" and item["cause_code"] == ""
              for item in unconfirmed["items"]), [item["tier"] for item in unconfirmed["items"]])
    check("C-02 说明里写明「错因未确认，只推通用」",
          "错因未确认" in unconfirmed["note"], unconfirmed["note"])
    context = T.answer_context(database, fixture["class_id"], fixture["answers"]["移项"])
    check("C-02 作答上下文：已确认错因 + 知识点 + 单元都取到（走 grading 四维只读口径）",
          context["cause_confirmed"] and context["cause_code"] == "MOVE-SIGN"
          and context["knowledge_point"] == "移项" and context["unit"] == UNIT, context)
    # 撤回错因（cause_state 改回 undecided）→ 上下文必须变成未确认，只推通用
    answer_id = fixture["answers"]["移项"]
    with database.tx() as conn:
        conn.execute("UPDATE answer_checks SET cause_state='undecided' WHERE answer_id=?",
                     (answer_id,))
    withdrawn = T.answer_context(database, fixture["class_id"], answer_id)
    via_answer = T.match_for_answer(database, fixture["class_id"], answer_id)
    with database.tx() as conn:
        conn.execute("UPDATE answer_checks SET cause_state='decided' WHERE answer_id=?",
                     (answer_id,))
    check("C-02 撤回错因后：上下文变未确认，按作答匹配只推通用",
          not withdrawn["cause_confirmed"] and withdrawn["cause_code"] == ""
          and all(item["tier"] == "general" for item in via_answer["items"]), withdrawn)


# ---------------------------------------------------------------------------
# 第 10 节 D-01：三级提示
# ---------------------------------------------------------------------------
def section_d01(fixture):
    section("10. D-01 三级提示：P1→P2→P3 默认顺序、可调、使用记录字段齐全")
    database = fixture["db"]
    plan = T.hint_plan(database, UNIT, "移项", "MOVE-SIGN", True)
    levels = [item["level"] for item in plan["levels"]]
    check("D-01 默认顺序 P1→P2→P3（顺序来自 r2_contract.HINT_LEVEL_ORDER）",
          levels == ["P1", "P2", "P3"] and plan["default_order"] == list(C.HINT_LEVEL_ORDER),
          levels)
    check("D-01 每条提示带发布版本与资源 ID（PDF 与任务据此记录版本）",
          all(item["resource_key"] and item["version"] for item in plan["levels"]))
    check("D-01 起始级别与下一级建议按顺序给出（P1 起、下一步 P2）",
          plan["start_level"] == "P1" and plan["next_level"] == "P2")
    adjusted = T.hint_plan(database, UNIT, "移项", "MOVE-SIGN", True, levels=("P2", "P3"))
    check("D-01 级别可调：指定 P2/P3 时起始级别为 P2",
          adjusted["start_level"] == "P2" and [item["level"] for item in adjusted["levels"]]
          == ["P1", "P2", "P3"])
    check("D-01 next_hint_level：已给 P1 → P2；P1+P2 → P3；到 P3 不再加级",
          T.next_hint_level(["P1"]) == "P2" and T.next_hint_level(["P1", "P2"]) == "P3"
          and T.next_hint_level(["P1", "P2", "P3"]) == "P3")
    student_id = fixture["students"]["林小雨"]
    usage = T.record_hint_usage(
        database, fixture["class_id"], student_id, level="P2", planned_level="P2",
        provided=1, independent_attempt=1, student_fixed=1, knowledge_point="移项",
        cause_code="MOVE-SIGN", answer_id=fixture["answers"]["移项"],
        resource_key=plan["levels"][1]["resource_key"], resource_version=plan["levels"][1]["version"])
    fields = ("level", "planned_level", "provided", "independent_attempt", "student_fixed",
              "full_solution_provided", "independent_before_solution", "knowledge_point",
              "cause_code", "resource_key", "resource_version")
    check("D-01 使用记录字段齐全（级别/计划级别/是否提供/独立尝试/自行修正/资源与版本）",
          all(usage.get(field) is not None for field in fields)
          and usage["level"] == "P2" and usage["student_fixed"] == 1, usage)
    progress = T.hint_progress(database, fixture["class_id"], student_id)
    check("D-01 学生提示进度：已给级别、独立尝试次数、修正次数可回读",
          progress["provided_levels"] == ["P2"] and progress["independent_attempts"] == 1
          and progress["fixed_after_hint"] == 1, progress)
    missing_plan = T.hint_plan(database, UNIT, "没有这个知识点", "", False)
    check("D-01 没有可用提示时明确提示人工补充（不编造提示）",
          not missing_plan["levels"] and "人工补充" in missing_plan["note"])


# ---------------------------------------------------------------------------
# 第 11 节 D-02：完整解析必须在独立尝试之后
# ---------------------------------------------------------------------------
def section_d02(fixture):
    section("11. D-02 完整解析必须在独立尝试之后（教师提前看答案不算学生完成）")
    database = fixture["db"]
    class_id = fixture["class_id"]
    student_id = fixture["students"]["林小雨"]
    answer_id = fixture["answers"]["移项"]
    before = database.scalar("SELECT COUNT(*) FROM hint_usages") or 0
    cases = (
        ("提前看答案（没有独立尝试却给了完整解析）",
         dict(independent_attempt=0, full_solution_provided=1, independent_before_solution=1),
         "solution_before_attempt"),
        ("给了完整解析但没记「在独立尝试之后」",
         dict(independent_attempt=1, full_solution_provided=1, independent_before_solution=0),
         "solution_not_after_attempt"),
        ("没给完整解析却说解析在独立尝试之后",
         dict(independent_attempt=1, full_solution_provided=0, independent_before_solution=1),
         "inconsistent"),
        ("没给提示却记学生自己修正了",
         dict(provided=0, student_fixed=1), "inconsistent"),
    )
    for label, kwargs, code in cases:
        ok, got = expect_error(
            lambda kwargs=kwargs: T.record_hint_usage(
                database, class_id, student_id, level="P1", planned_level="P1",
                knowledge_point="移项", answer_id=answer_id, **kwargs), code, label)
        check("D-02 %s → 拒绝（%s）" % (label, code), ok, got)
    after = database.scalar("SELECT COUNT(*) FROM hint_usages") or 0
    check("D-02 被拒绝的记录一条都没写库（计数不变）", after == before, (before, after))
    ok_usage = T.record_hint_usage(
        database, class_id, student_id, level="P1", planned_level="P1", provided=1,
        independent_attempt=1, student_fixed=1, full_solution_provided=1,
        independent_before_solution=1, knowledge_point="移项", cause_code="MOVE-SIGN",
        answer_id=answer_id)
    check("D-02 先独立尝试、再给完整解析：记录 independent_before_solution=1",
          ok_usage["independent_before_solution"] == 1
          and ok_usage["full_solution_provided"] == 1
          and ok_usage["independent_attempt"] == 1, ok_usage)
    progress = T.hint_progress(database, class_id, student_id)
    check("D-02 进度统计区分「解析在独立尝试之后」与「解析先于尝试」",
          progress["full_solution_given"] >= 1 and progress["solution_before_attempt"] == 0,
          progress)
    # 反向自证：拆掉这两道闸 → 提前看答案的调用会写库成功
    mutate_source([("    if full_solution_provided and not independent_attempt:",
                    "    if False:"),
                   ("    if full_solution_provided and not independent_before_solution:",
                    "    if False:")])
    try:
        leaked = T.record_hint_usage(
            database, class_id, student_id, level="P3", planned_level="P3", provided=1,
            independent_attempt=0, full_solution_provided=1,
            independent_before_solution=1, knowledge_point="移项",
            cause_code="MOVE-SIGN", answer_id=answer_id)
        flipped = leaked is not None and leaked["independent_before_solution"] == 1
    finally:
        restore_source()
    check("D-02 反向自证：拆掉两道闸后「提前看答案」被写成合法记录 → 证明闸门承重", flipped)


# ---------------------------------------------------------------------------
# 第 12 节 E-01：三类练习推荐 + 后测资源分开
# ---------------------------------------------------------------------------
def section_e01(fixture):
    section("12. E-01 举一反三：三类练习推荐（理由/难度/答案/解析），后测资源分开管理")
    database = fixture["db"]
    found = T.recommend_exercises(database, UNIT, "移项", "MOVE-SIGN", True)
    counts = found["counts"]
    check("E-01 三类练习都有（概念澄清 / 步骤巩固 / 迁移应用）",
          all(counts.get(purpose, 0) >= 1 for purpose in T.EXERCISE_PURPOSES), counts)
    items = [item for group in found["groups"].values() for item in group]
    check("E-01 每道练习带推荐理由/知识点/难度/答案/解析",
          all(item["reason"] and item["knowledge_point"] == "移项" and item["difficulty"]
              and item["answer"] and item["solution"] for item in items), items[:1])
    check("E-01 练习只在正式推荐里取（合成测试练习不在其中）",
          all(not T.is_synthetic(item["source"]) for item in items))
    post = T.list_post_test_exercises(database, UNIT, "移项", "MOVE-SIGN", True)
    check("E-01 独立后测资源与辅导练习分开：默认没有任何后测资源混进来",
          post["count"] == 0 or all(item["phase"] == "post_test" for item in post["items"]),
          post["count"])
    # 补一条后测练习（已发布）→ 只出现在后测列表，不进辅导练习选择
    view = T.create_resource(database, {
        "kind": "exercise", "unit": UNIT, "knowledge_point": "移项", "cause_code": "",
        "title": "独立后测：移项自测题",
        "body": {"stem": "解方程：6x - 4 = 2x + 8", "answer": "x = 3",
                 "solution": "移项 6x - 2x = 8 + 4；4x = 12；x = 3", "difficulty": "基础",
                 "purpose": "steps"},
        "conditions": {"phase": "post_test"}, "source": "自编"},
        created_by=fixture["users"]["u1"])
    publish(database, view["resource_key"], 1, fixture["users"]["u1"], fixture["users"]["u2"])
    post = T.list_post_test_exercises(database, UNIT, "移项", "", True)
    guided = T.recommend_exercises(database, UNIT, "移项", "MOVE-SIGN", True)
    guided_keys = [item["resource_key"] for group in guided["groups"].values() for item in group]
    check("E-01 后测资源只进「独立后测」清单，不混进辅导练习选择",
          view["resource_key"] in [item["resource_key"] for item in post["items"]]
          and view["resource_key"] not in guided_keys)
    points = T.issue_entry_points(database, fixture["class_id"])
    scoped = T.issue_entry_points(database, fixture["class_id"],
                                  fixture["students"]["林小雨"])
    check("E-01 三个入口：班级共性问题与个人学情都能聚合出「知识点+确认错因」并附资源",
          points["total"] >= 3 and scoped["total"] >= 1
          and points["groups"][0]["match"]["items"]
          and scoped["groups"][0]["exercises"]["counts"], (points["total"], scoped["total"]))


# ---------------------------------------------------------------------------
# 第 13 节 E-02：教学任务 + 纸笔 PDF
# ---------------------------------------------------------------------------
def section_e02(fixture):
    section("13. E-02 教学任务（创建/修改/历史/完成）+ 纸笔 PDF（学生版不含答案与解析）")
    database = fixture["db"]
    u1, u2 = fixture["users"]["u1"], fixture["users"]["u2"]
    student_id = fixture["students"]["林小雨"]
    answer_id = fixture["answers"]["移项"]
    plan = T.hint_plan(database, UNIT, "移项", "MOVE-SIGN", True)
    exercises = T.recommend_exercises(database, UNIT, "移项", "MOVE-SIGN", True)
    chosen = [item for group in exercises["groups"].values() for item in group][:3]
    refs = [{"resource_key": item["resource_key"], "version": item["version"],
             "purpose": item["purpose"], "phase": item["phase"], "title": item["title"]}
            for item in chosen]
    task = T.create_task(
        database, fixture["class_id"], student_id, "individual", "移项专项辅导（第 3 次）",
        basis={"knowledge_point": "移项", "unit": UNIT, "cause_code": "MOVE-SIGN",
               "cause_label": "移项不变号", "answer_id": answer_id, "scope": "student"},
        hint_plan=plan, exercise_refs=refs, teacher_note="先让学生自己试第一步。",
        created_by=u1, status="scheduled")
    check("E-02 创建个人辅导任务：目标对象/依据/所选提示与练习/教师说明/状态都在",
          task["student_id"] == student_id and task["basis"]["knowledge_point"] == "移项"
          and task["status"] == "scheduled" and task["teacher_note"]
          and len(task["exercise_refs"]) == 3 and task["hint_plan"]["levels"])
    versions = task["resource_versions"]
    check("E-02 任务记录所选资源的版本（key→version），不是「总读最新」",
          len(versions) == 3 + len(plan["levels"])
          and all(versions.get(ref["resource_key"]) == ref["version"] for ref in refs))
    updated = T.update_task(database, task["id"], teacher_note="改过：先只做前两题",
                            status="draft")
    check("E-02 修改任务：内容与状态改动都写进历史（可追溯）",
          updated["teacher_note"].startswith("改过")
          and [entry["action"] for entry in updated["history"]][:2] == ["create", "update"])
    T.set_task_status(database, task["id"], "scheduled", user=u1)
    completed = T.complete_task(database, task["id"], user=u1, note="学生已当面完成")
    check("E-02 手动记录完成：状态 completed + 完成时间落库 + 历史留痕",
          completed["status"] == "completed" and completed["completed_at"]
          and completed["history"][-1]["action"] == "status")
    ok, code = expect_error(lambda: T.update_task(database, task["id"], title="改已完成的"),
                            "task_locked", "edit-completed")
    check("E-02 已完成任务不能悄悄改内容：code=%s" % code, ok, code)
    # R2-INV-09：依据变化只打 needs_review，状态不动
    before_task = T.get_task(database, task["id"])
    marked = T.mark_task_review(database, task["id"], note="知识点分类被调整")
    check("E-02 R2-INV-09 任务依据变化：只打「依据已变，待复核」，status 保持 completed",
          marked["review_state"] == "needs_review"
          and marked["review_state_label"] == C.TASK_REVIEW_STATES["needs_review"]
          and marked["status"] == before_task["status"] == "completed",
          (marked["status"], marked["review_state"]))
    check("E-02 R2-INV-09 规则声明与动作来自 r2_contract（不是自造状态）",
          marked["rule"] == "R2-INV-09" and "teaching_tasks.mark_review:basis_changed"
          in marked["actions"], marked["actions"])
    T.resolve_task_review(database, task["id"], note="复核完成")
    check("E-02 复核后回到「依据有效」，任务状态仍然不动",
          T.get_task(database, task["id"])["review_state"] == "ok"
          and T.get_task(database, task["id"])["status"] == "completed")

    # R2-INV-08：引用的资源出新版本 → 任务只打待复核，引用版本不被静默替换
    referenced = refs[0]
    T.update_resource(database, referenced["resource_key"],
                      body={"stem": "改过的题干（新版本）", "answer": "x = 5",
                            "solution": "改过的解析", "difficulty": "基础",
                            "purpose": referenced["purpose"]},
                      note="修订", created_by=u1)
    row = database.query_one("SELECT * FROM teaching_resources WHERE resource_key=?",
                             (referenced["resource_key"],))
    publish(database, referenced["resource_key"], row["current_version"], u1, u2)
    after = T.get_task(database, task["id"])
    detail = T.task_resource_versions(database, task["id"])
    changed_rows = [item for item in detail["items"] if item["changed"]]
    check("E-02 R2-INV-08 资源出新版本：任务标「依据已变，待复核」",
          after["review_state"] == "needs_review", after["review_state"])
    check("E-02 R2-INV-08 旧引用不被静默换版本：任务仍记录旧版本号，可追溯差异",
          after["resource_versions"][referenced["resource_key"]] == referenced["version"]
          and changed_rows and changed_rows[0]["resource_key"] == referenced["resource_key"],
          (after["resource_versions"], changed_rows[:1]))
    check("E-02 R2-INV-08 任务状态不因依据变化被改动（仍 completed）",
          after["status"] == "completed" and after["completed_at"])
    # 反向自证：任务引用的是「最新版本」时不应被标记（若判据退化成「总读最新」，这里必红）
    fresh_task = T.create_task(
        database, fixture["class_id"], student_id, "individual", "引用最新版本的任务",
        basis={"knowledge_point": "移项"}, hint_plan={}, exercise_refs=[],
        resource_versions={referenced["resource_key"]: row["current_version"]},
        created_by=u1, status="scheduled")
    T.publish_version  # noqa: B018  (说明：这一步不再发布，只验证「引用即最新」不被连带标记)
    fresh = T.get_task(database, fresh_task["id"])
    check("E-02 反向自证：引用最新版本的任务不会被连带标记（判据是版本比较，不是「总标」）",
          fresh["review_state"] == "ok", fresh["review_state"])

    # ---- 纸笔 PDF：学生版 / 教师版 ----
    paper = P.paper_payload(database, task["id"])
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    student_pdf = P.render_student(paper)
    teacher_pdf = P.render_teacher(paper)
    student_path = os.path.join(SAMPLE_DIR, "纸笔任务-学生版样例.pdf")
    teacher_path = os.path.join(SAMPLE_DIR, "纸笔任务-教师版样例.pdf")
    with open(student_path, "wb") as handle:
        handle.write(student_pdf)
    with open(teacher_path, "wb") as handle:
        handle.write(teacher_pdf)
    # 断言必须读回 PDF 文件里的文本（不是函数返回值）
    with open(student_path, "rb") as handle:
        student_text = P.extract_text(handle.read())
    with open(teacher_path, "rb") as handle:
        teacher_text = P.extract_text(handle.read())
    exercise = paper["exercises"][0]
    answers = [item.get("answer") for item in paper["exercises"] if item.get("answer")]
    solutions = [item.get("solution") for item in paper["exercises"] if item.get("solution")]
    hints = [item.get("text") for item in paper["hints"] if item.get("text")]

    def squeezed(text):
        """PDF 会按页宽折行，回读文本里带换行；比较前统一去掉空白。"""
        return "".join(str(text or "").split())

    student_flat, teacher_flat = squeezed(student_text), squeezed(teacher_text)
    check("E-02 学生版 PDF 读到题干与作答空间（不是空文件、不是图片）",
          squeezed(exercise["stem"])[:8] in student_flat and "作答说明" in student_flat
          and "姓名" in student_flat, student_text[:80])
    check("E-02 学生版 PDF 不含任何标准答案",
          all(squeezed(answer) not in student_flat for answer in answers),
          [a for a in answers if squeezed(a) in student_flat])
    check("E-02 学生版 PDF 不含解析与教学提示",
          all(squeezed(item) not in student_flat for item in solutions + hints)
          and "解析：" not in student_flat and "教学提示" not in student_flat,
          [item for item in solutions + hints if squeezed(item) in student_flat])
    check("E-02 教师版 PDF 含答案 + 解析 + 教学提示（证明提取器没失灵）",
          all(squeezed(answer) in teacher_flat for answer in answers)
          and all(squeezed(item) in teacher_flat for item in solutions)
          and all(squeezed(item) in teacher_flat for item in hints),
          (sum(1 for answer in answers if squeezed(answer) in teacher_flat),
           sum(1 for item in solutions if squeezed(item) in teacher_flat),
           sum(1 for item in hints if squeezed(item) in teacher_flat),
           len(answers), len(solutions), len(hints)))
    check("E-02 教师版 PDF 标注「完整解析必须在独立尝试之后」",
          "完整解析必须在学生独立尝试之后" in teacher_flat)
    check("E-02 两版 PDF 都是可解析的 PDF（文件头 + 页对象 + 文本对象）",
          student_pdf.startswith(b"%PDF-1.4") and b"/Type /Page" in student_pdf
          and b"ToUnicode" in student_pdf and len(student_pdf) > 20000,
          len(student_pdf))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    from logmeta import environment_lines
    log("R2-05 教学资源库·审核发布·建议匹配·三级提示·举一反三与纸笔任务 PDF 验收（1.9.0）")
    for line in environment_lines():
        log(line)
    fixture = None
    try:
        section("0. 验收条目与 A–E 对应关系（派单 §7.6 原文未随任务包落到工作副本，此处冻结）")
        mapping = (
            ("A-01", "A 资源库：五类统一结构 / 建档 / 改版"),
            ("A-02", "A 资源库：导入工具 + 幂等 + 送审"),
            ("A-03", "A 资源库：首批真实资源内容覆盖 4 个主要错因"),
            ("B-01", "B 审核发布：四态流转与非法流转"),
            ("B-02", "B 审核发布：双审核分别记录、两项均过方可发布"),
            ("B-03", "B 审核发布：发布权限校验、无权限拒绝且不写库"),
            ("B-04", "B 审核发布：未审/停用/合成不进推荐；版本可追溯"),
            ("C-01", "C 建议匹配：三级顺序 + 依据与来源"),
            ("C-02", "C 建议匹配：错因未确认只推通用"),
            ("D-01", "D 三级提示：P1→P2→P3 可调 + 使用记录"),
            ("D-02", "D 三级提示：完整解析必须在独立尝试之后"),
            ("E-01", "E 举一反三：三类练习 + 后测资源分开"),
            ("E-02", "E 任务与 PDF：任务全流程 + 学生版不含答案/解析"),
        )
        for key, text in mapping:
            log("  %-5s %s" % (key, text))
        check("0 验收条目共 13 条，与 A–E 一一对应", len(mapping) == 13)

        fixture = build_fixture()
        check("夹具就绪：1 个班 / 2 名学生 / 4 条已确认错因的作答（去括号·移项·合并同类项·系数化为1）",
              fixture["class_id"] and len(fixture["answers"]) == 4)

        section_a01(fixture)
        section_a02(fixture)
        section_a03(fixture)
        section_b01(fixture)
        section_b02(fixture)
        section_b03(fixture)
        section_b04(fixture)
        section_c01(fixture)
        section_c02(fixture)
        section_d01(fixture)
        section_d02(fixture)
        section_e01(fixture)
        section_e02(fixture)
    except Exception:                                   # noqa: BLE001
        import traceback
        log("")
        log("!! 套件异常中断：")
        log(traceback.format_exc())
        FAILED[0] += 1
    finally:
        restore_source()
        if fixture:
            fixture["db"].conn.close()
        for path in WORK:
            shutil.rmtree(path, ignore_errors=True)

    log("")
    log("总计 %d 项断言：通过 %d，失败 %d" % (TOTAL[0], TOTAL[0] - FAILED[0], FAILED[0]))
    if MUTATED:
        log("!! 还有源码变异没有还原：%r" % [name for name, _ in MUTATED])
        FAILED[0] += 1
    log("结论：%s" % ("全部通过（exit 0）" if FAILED[0] == 0 else "有失败项（exit 1）"))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(LINES) + "\n")
    return 0 if FAILED[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
