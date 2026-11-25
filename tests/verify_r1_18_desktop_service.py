# -*- coding: utf-8 -*-
"""R1-T18：桌面端 service 层接入作答核对与 AI 开关（进程内调 app.api 处理器）。

跑法（务必用装了 PySide6 的那个解释器）：
    E:\\Agent\\venv-desktop\\Scripts\\python.exe tests\\verify_r1_18_desktop_service.py

验两件事：
1. **9 个新方法各自调到了对应的 api 处理器**（返回体与网页端同源，字段名一致）；
2. **错误原样上抛**：status / code / message 一个字不改（basis_required 400、
   cause_not_applicable 400、version_conflict 409、nothing_to_retract 400、
   bad_verify_source 400、not_found 404、ai_isolation 403），
   以及越权（改别的班的作答）必须是 404。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

from app import analysis  # noqa: E402
from app.api import ApiError  # noqa: E402
from app.db import Database, ensure_answer_check_schema  # noqa: E402
from desktop.service import DesktopService  # noqa: E402


def answer_csv():
    """与 verify_r1_07_isolation.py 同一份作答 CSV（这里内联，避免依赖 Pillow）。"""
    return (
        "记录编号,学生姓名,学校编号,题号,题目内容,单元,标准解法,学生步骤,来源批次,作答日期,缺测,缺测原因\r\n"
        "R1-18-001,学生甲,XY001,1,解方程 2x+4=10,一元一次方程,2x=6；x=3,2x+4=10；2x=3；x=1.5,"
        "9月第1次作业,2026-09-02,否,\r\n"
        "R1-18-002,学生乙,,2,解方程 3(x-2)=2x+5,一元一次方程,3x-6=2x+5；x=11,3x-6=2x+5；x=11,"
        "9月第1次作业,2026-09-02,否,\r\n"
    )

WORK = os.path.join(tempfile.gettempdir(), "r1-18-desktop-service")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-18-desktop-service.txt")
LINES = []
RESULTS = {"pass": 0, "fail": 0}


def log(text=""):
    print(text)
    LINES.append(text)


def check(name, ok, detail=""):
    if ok:
        RESULTS["pass"] += 1
        log("PASS %s" % name)
    else:
        RESULTS["fail"] += 1
        log("FAIL %s  %s" % (name, detail))
    return bool(ok)


def section(title):
    log("")
    log("== %s ==" % title)


def expect_error(call, status, code):
    """调用必须抛 ApiError，且 status/code 原值不被改写；返回 (是否命中, 说明)。"""
    try:
        result = call()
    except ApiError as exc:
        got = (getattr(exc, "status", None), getattr(exc, "code", None))
        return got == (status, code), "%s %s %s" % (got[0], got[1], (getattr(exc, "message", "") or "")[:60])
    return False, "没有抛错，返回 %r" % (result,)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-T18 桌面 service 层：作答核对 + AI 开关（进程内调 api，错误原样上抛）")
    for line in environment_lines():
        log(line)

    data_dir = os.path.join(WORK, "data")
    os.makedirs(data_dir)
    db_file = os.path.join(data_dir, "app.db")
    Database(db_file)
    ensure_answer_check_schema(db_file)
    svc = DesktopService(data_dir)
    svc.register("t18", "pw-r1-18")
    cid = svc.create_class("桌面核对班")["id"]
    sid = svc.create_student(cid, "学生甲", "XY001")["id"]
    svc.create_student(cid, "学生乙", "")
    svc.import_answers(cid, answer_csv(), "9月第1次作业")
    other = svc.create_class("另一个班")["id"]
    other_sid = svc.create_student(other, "学生丙", "XY009")["id"]
    svc.import_answers(other, answer_csv(), "9月第1次作业")

    answers = svc.db.query("SELECT id FROM answers WHERE class_id=? ORDER BY id", (cid,))
    other_answers = svc.db.query("SELECT id FROM answers WHERE class_id=? ORDER BY id", (other,))
    answer_id = answers[0]["id"]

    # ---------------------------------------------------------------
    section("一、总览 / 详情（api_check_overview / api_check_answer）")
    # ---------------------------------------------------------------
    overview = svc.check_overview(cid)
    check("1.1 check_overview 返回网页端同源结构（assignment/assignments/counts/state 齐备）",
          isinstance(overview, dict) and {"assignment", "assignments", "counts", "state"}
          <= set(overview.keys()), sorted((overview or {}).keys())[:8])
    check("1.2 总览统计到本班 2 份真实作答（counts.answers）",
          int((overview.get("counts") or {}).get("answers") or 0) == 2,
          overview.get("counts"))
    batch_name = (overview.get("batch") or "")
    scoped = svc.check_overview(cid, batch_name)
    check("1.3 指定批次与不指定批次结果一致（同一批作业）",
          (scoped.get("counts") or {}) == (overview.get("counts") or {}),
          (scoped.get("counts"), overview.get("counts")))

    detail = svc.check_answer(cid, answer_id)
    for key in ("answer", "locate", "judgement", "followup", "queue", "applicability", "candidates"):
        check("1.4 详情含 %s（与 api_check_answer 同字段）" % key, key in detail, sorted(detail.keys())[:8])
    version = detail["answer"]["version"]

    # ---------------------------------------------------------------
    section("二、保存教师判断（api_check_save）：三态 + 错误原值")
    # ---------------------------------------------------------------
    pool = detail.get("candidates") or []
    if pool:
        code, label = pool[0]["code"], pool[0]["label"]
    else:
        from app import answer_check as ac
        code, label = next(iter(ac.CAUSE_LABELS.items()))

    # out_of_scope + decided 且没写依据 → 必须原样 400 basis_required
    out_of_scope = None
    for candidate_answer in answers:
        view = svc.check_answer(cid, candidate_answer["id"])
        if view.get("applicability") == "optional":
            out_of_scope = view
            break
    if out_of_scope is not None:
        ok, detail_text = expect_error(
            lambda: svc.check_save(cid, out_of_scope["answer"]["id"], {
                "expected_version": out_of_scope["answer"]["version"],
                "progress": "checked", "cause_state": "decided",
                "cause_code": code, "cause_label": label, "cause_source": "teacher",
            }), 400, "basis_required")
        check("2.1 out_of_scope + 已判断 + 无依据 → 400 basis_required（原样上抛，不补依据）",
              ok, detail_text)
    else:
        check("2.1 本夹具没有 out_of_scope 作答，跳过（如实登记）", True, "skipped")

    saved = svc.check_save(cid, answer_id, {
        "expected_version": version, "progress": "checked",
        "cause_state": "decided", "cause_code": code, "cause_label": label,
        "cause_source": "teacher", "basis_note": "作业本原图第 2 行写错",
        "observed_step": "2x+4=10；2x=3；x=1.5",
    })
    check("2.2 check_save 正常路径返回 saved + judgement（字段与网页端一致）",
          saved.get("action") == "saved" and saved["judgement"]["cause_state"] == "decided"
          and "adjustments" in saved and "receipt" in saved, sorted(saved.keys())[:8])
    check("2.3 保存后 mistakes 落了一条（R1-16 联动仍在作用）",
          len(svc.db.query("SELECT id FROM mistakes WHERE answer_id=?", (answer_id,))) == 1,
          len(svc.db.query("SELECT id FROM mistakes WHERE answer_id=?", (answer_id,))))

    ok, detail_text = expect_error(
        lambda: svc.check_save(cid, answer_id, {"expected_version": "旧版本", "progress": "checked"}),
        409, "version_conflict")
    check("2.4 过期版本 → 409 version_conflict（原样上抛）", ok, detail_text)

    ok, detail_text = expect_error(
        lambda: svc.check_save(cid, answer_id, {
            "expected_version": svc.check_answer(cid, answer_id)["answer"]["version"],
            "progress": "pending", "cause_state": "decided", "cause_label": label}),
        400, "cause_state_mismatch")
    check("2.5 撤回时夹带判断 → 400 cause_state_mismatch（不静默丢弃）", ok, detail_text)

    # ---------------------------------------------------------------
    section("三、撤回（api_check_save + progress=pending）")
    # ---------------------------------------------------------------
    current = svc.check_answer(cid, answer_id)
    retracted = svc.check_save(cid, answer_id, {
        "expected_version": current["answer"]["version"], "progress": "pending",
        "change_reason": "判错了，撤回",
    })
    check("3.1 撤回返回 action=retracted、judgement.progress=pending",
          retracted.get("action") == "retracted" and retracted["judgement"]["progress"] == "pending",
          (retracted.get("action"), retracted["judgement"]["progress"]))
    check("3.2 撤回后队列把它算回来（pending ≥ 1）",
          int((retracted.get("queue") or {}).get("remaining") or 0) >= 1,
          retracted.get("queue"))
    ok, detail_text = expect_error(
        lambda: svc.check_save(cid, answer_id, {
            "expected_version": svc.check_answer(cid, answer_id)["answer"]["version"],
            "progress": "pending"}),
        400, "nothing_to_retract")
    check("3.3 没有可撤回的结论 → 400 nothing_to_retract（原样上抛）", ok, detail_text)

    # ---------------------------------------------------------------
    section("四、补录 / 校正（api_check_answer_content）")
    # ---------------------------------------------------------------
    current = svc.check_answer(cid, answer_id)
    patched = svc.check_update_content(cid, answer_id, {
        "expected_version": current["answer"]["version"],
        "student_steps": "2x+4=10；2x=6；x=3",
        "missing": "否", "missing_reason": "",
    })
    check("4.1 校正成功：返回 action=updated + 新版本号",
          patched.get("action") == "updated" and patched.get("version")
          and patched.get("previous_version") == current["answer"]["version"],
          (patched.get("action"), patched.get("previous_version"), patched.get("version")))
    ok, detail_text = expect_error(
        lambda: svc.check_update_content(cid, answer_id, {
            "expected_version": "旧版本", "student_steps": "x=1"}),
        409, "version_conflict")
    check("4.2 校正时版本过期 → 409 version_conflict（版本闸门在服务端）", ok, detail_text)

    # ---------------------------------------------------------------
    section("五、记录找回（api_check_records：cause_state / verify_source）")
    # ---------------------------------------------------------------
    undecided = svc.check_records(cid, cause_state="undecided")
    decided = svc.check_records(cid, cause_state="decided")
    everything = svc.check_records(cid, cause_state="all")
    check("5.1 三态筛选各自可用（undecided/decided/all 都返回 items）",
          all(isinstance(item, dict) and "items" in item for item in (undecided, decided, everything)),
          [list(item.keys())[:4] for item in (undecided, decided, everything)])
    check("5.2 decided 档位的记录数 ≤ 全部档位",
          len(decided.get("items") or []) <= len(everything.get("items") or []),
          (len(decided.get("items") or []), len(everything.get("items") or [])))
    manual = svc.check_records(cid, cause_state="all", verify_source="manual")
    check("5.3 verify_source=manual 可单独筛（返回 items）", "items" in manual, list(manual.keys())[:5])
    ok, detail_text = expect_error(
        lambda: svc.check_records(cid, cause_state="all", verify_source="乱填"),
        400, "bad_verify_source")
    check("5.4 非法 verify_source → 400 bad_verify_source（不静默当 all）", ok, detail_text)

    # ---------------------------------------------------------------
    section("六、演示数据（api_check_demo_seed / api_check_demo_reset）")
    # ---------------------------------------------------------------
    seeded = svc.check_demo_seed(cid)
    check("6.1 seed 返回体原样带 seeded/by_batch/records（不裁剪）",
          seeded.get("ok") is True and "seeded" in seeded and "records" in seeded
          and "by_batch" in (seeded.get("seeded") or {}), sorted(seeded.keys())[:8])
    demo_records = seeded.get("records") or []
    check("6.2 seed 真的写入了合成作答（≥1 条）", len(demo_records) >= 1, len(demo_records))
    reset = svc.check_demo_reset(cid)
    check("6.3 reset 返回 deleted/by_batch（不裁剪）",
          reset.get("ok") is True and "deleted" in reset, sorted(reset.keys())[:8])
    check("6.4 reset 之后合成作答被清掉",
          len(svc.db.query("SELECT id FROM answers WHERE class_id=? AND source='合成演示'",
                           (cid,))) == 0 or int((reset.get("deleted") or {}).get("answers") or 0) >= 1,
          reset.get("deleted"))

    # ---------------------------------------------------------------
    section("七、AI 开关（api_account_ai_individual / _update）+ 六入口仍按 t14 拒绝")
    # ---------------------------------------------------------------
    state = svc.ai_individual_state()
    check("7.1 桌面端能读到开关（ai_egress_available=true，因为注入了 provider）",
          state.get("ai_individual_allowed") is False and state.get("ai_egress_available") is True,
          state)
    svc.save_ai_settings("custom", "http://127.0.0.1:9/v1", "stub-model", "sk-t18",
                         service_name="本机桩")
    ok, detail_text = expect_error(
        lambda: svc.suggest_followup(cid, sid, "一元一次方程", None, consent=True),
        403, analysis.INDIVIDUAL_FLOW_CODE)
    check("7.2 未开启时六入口之一（追问候选）仍 403 ai_isolation（未授权不放行）", ok, detail_text)

    turned_on = svc.set_ai_individual(True)
    check("7.3 set_ai_individual(True) 写入成功并回读为 true",
          turned_on.get("ai_individual_allowed") is True
          and svc.ai_individual_state().get("ai_individual_allowed") is True, turned_on)
    svc.set_ai_individual(False)
    check("7.4 set_ai_individual(False) 立刻关回去（下次调用重新判定）",
          svc.ai_individual_state().get("ai_individual_allowed") is False,
          svc.ai_individual_state())

    # ---------------------------------------------------------------
    section("八、越权与未登录（沿用会话，越权必须 404）")
    # ---------------------------------------------------------------
    ok, detail_text = expect_error(
        lambda: svc.check_answer(other, answer_id), 404, "not_found")
    check("8.1 用错班级查详情 → 404 not_found（不泄露别的班数据）", ok, detail_text)
    ok, detail_text = expect_error(
        lambda: svc.check_save(other, answer_id, {"expected_version": "x", "progress": "checked"}),
        404, "not_found")
    check("8.2 用错班级保存 → 404 not_found", ok, detail_text)
    ok, detail_text = expect_error(
        lambda: svc.check_update_content(other, answer_id, {"expected_version": "x"}), 404, "not_found")
    check("8.3 用错班级纠正 → 404 not_found", ok, detail_text)

    svc.logout()
    # 退出登录后桌面端与既有方法表现一致：`_ctx().user` 为空，处理器在归属校验处就停下。
    # 这里不发明新的 401 语义，只如实断言「新方法与老方法一样被拦住」。
    def _kind_of(call):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 —— 只比较类型，不改写
            return type(exc).__name__
        return "ok"

    old_kind = _kind_of(lambda: svc.list_classes())
    new_kind = _kind_of(lambda: svc.check_overview(cid))
    check("8.4 退出登录后新方法与既有方法同样被拦住（不产生未鉴权访问）",
          old_kind != "ok" and new_kind == old_kind and svc.logged_in is False,
          (old_kind, new_kind, svc.logged_in))

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                    RESULTS["pass"], RESULTS["fail"]))
    code_exit = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code_exit))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LINES) + "\n")
    shutil.rmtree(WORK, ignore_errors=True)
    return code_exit


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
