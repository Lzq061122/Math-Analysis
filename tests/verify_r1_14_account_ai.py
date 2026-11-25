# -*- coding: utf-8 -*-
"""R1-14：个体 AI 入口「默认不外发 + 教师账号显式开启后可用（含脱敏与审计）」。

跑法：C:\\Python314\\python.exe tests\\verify_r1_14_account_ai.py

验的四件事（与任务书验收一一对应）：
1. **默认关**：6 个个体入口在真实通道下一律 403 `ai_isolation`，且**一次网络尝试都没有**
   （与 R1-07 逐字等价——R1-07 套件仍原样跑 73 项）；
2. **开启后放行**：同一个 provider、同一批调用能真的发出去，且发出去的请求体
   **只含白名单字段**（把实际字段名逐条打出来，证明没有姓名/学号/班级名/answer_id/account）；
3. **审计**：每次真实外发都留一行（谁 / 何时 / 哪个流程 / 哪些字段 / 哪条通道），
   审计里只记字段名不记取值；
4. **关回去立刻恢复 403**；网页端不注入 provider、`/api/meta` 里连开关这个键都没有。
旧库兼容：没有 `users.ai_individual_allowed` 列、没有审计表的库，读取按「关」、写入时补列、
首次外发时建表——都要在旧库上实测。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from logmeta import environment_lines, exit_line  # noqa: E402

import server as server_mod  # noqa: E402
from app import analysis, api, vision  # noqa: E402
from app import db as db_mod  # noqa: E402
from app.api import ApiError  # noqa: E402
from app.db import Database, ensure_answer_check_schema  # noqa: E402
from app.uploads import UploadStore  # noqa: E402
from desktop.service import DesktopService  # noqa: E402
from verify_r1_07_isolation import (  # noqa: E402  同族夹具：复用桩服务与图片/CSV 生成，不复制粘贴
    FakeCtx, _stub_reply, answer_csv, make_photo, png_bytes,
)

WORK = os.path.join(ROOT, "tests", ".tmp-r1-14")
LOG_PATH = os.path.join(ROOT, "docs", "verify-r1-14-account-ai.txt")
STUB = {"calls": [], "lock": threading.Lock()}
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


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with STUB["lock"]:
            STUB["calls"].append({
                "path": self.path,
                "body": raw.decode("utf-8", "ignore"),
                "auth": bool(self.headers.get("Authorization")),
            })
        data = json.dumps(_stub_reply(self.path), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def stub_calls():
    with STUB["lock"]:
        return list(STUB["calls"])


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def expect_isolation(call):
    """调用应抛 ai_isolation；返回 (是否命中, 说明)。"""
    try:
        result = call()
    except ApiError as exc:
        return getattr(exc, "code", "") == analysis.INDIVIDUAL_FLOW_CODE, "%s %s" % (
            exc.status, exc.code)
    return False, "没有抛错，返回 %r" % (result,)


def expect_no_isolation(call):
    """调用不应被闸门拦（其它业务/格式错误不算拦截失败）；返回 (是否放行, 说明)。"""
    try:
        result = call()
        return True, "returned %s" % type(result).__name__
    except ApiError as exc:
        return getattr(exc, "code", "") != analysis.INDIVIDUAL_FLOW_CODE, "%s %s" % (
            exc.status, exc.code)


IDENTITY_TOKENS = ("学生甲", "学生乙", "隔离验证班", "XY001", "account", "username",
                   "student_name", "class_name")


def payload_field_names_of(body_text):
    """把桩服务收到的请求体还原成字段名清单（只用于证明形状，不打印取值）。"""
    try:
        payload = json.loads(body_text)
    except ValueError:
        return []
    return vision.payload_field_names(payload)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    log("数智伴学｜R1-14 个体 AI 账号开关（默认关 / 开启可用 / 脱敏 / 审计）")
    for line in environment_lines():
        log(line)

    stub, stub_root = start_stub()
    try:
        data_root = os.path.join(WORK, "data")
        os.makedirs(data_root)
        db_file = os.path.join(data_root, "app.db")
        Database(db_file)
        ensure_answer_check_schema(db_file)
        svc = DesktopService(data_root)
        svc.register("oc-14", "pw-r1-14")
        cid = svc.create_class("隔离验证班")["id"]
        sid = svc.create_student(cid, "学生甲", "XY001")["id"]
        svc.create_student(cid, "学生乙", "")
        photo = os.path.join(WORK, "题图.png")
        make_photo(photo)
        csv_text = answer_csv()
        svc.import_answers(cid, csv_text, "9月第1次作业")

        db = svc.db
        user = svc.user
        uploads = UploadStore(os.path.join(data_root, "uploads"))
        class_row = db.query_one("SELECT * FROM classes WHERE id=?", (cid,))
        answers = db.query("SELECT * FROM answers WHERE class_id=? ORDER BY id", (cid,))

        # 桌面端账号设置指向本机回环桩：provider 是「真实通道」，只是地址在回环上，
        # 这样「发没发出去」可以用桩计数 + 请求体逐字节证明，不需要真的联网。
        svc.save_ai_settings("custom", stub_root + "/v1", "stub-model", "sk-r1-14",
                             service_name="本机桩")
        provider = svc._ctx().ai_provider
        check("0.0 桌面端 provider 是真实通道（custom），不是 mock",
              provider.mode == "custom", provider.mode)

        # ---- 造 6 个入口需要的行：错题（带照片）+ 已确认追问 + 作答 ----
        created = api.api_create_mistake(FakeCtx(db, user, uploads, body={
            "class_id": cid, "student_id": sid,
            "source_type": "作业", "source_detail": "课本 P42", "note": "",
            "occurred_on": "2026-11-20", "question_text": "解方程 3(x-2)=2x+5",
            "solution_text": "3x-6=2x+5；x=11",
            "image": {
                "data_url": "data:image/png;base64,"
                            + __import__("base64").b64encode(png_bytes()).decode("ascii"),
                "width": 900, "height": 600,
            },
            "local_only": True,
        }))
        mid = created["id"]
        fresh = api.api_get_mistake(FakeCtx(db, user, uploads, params={"id": mid}))
        api.api_confirm_mistake(FakeCtx(
            db, user, uploads, params={"id": mid},
            body={"base_fingerprint": fresh["base_fingerprint"]}))
        followup_mod = __import__("app.followup", fromlist=["x"])
        followup = followup_mod.create(
            db, cid, sid, "一元一次方程", "你这一步为什么把 -3 变成 3？", "teacher", {},
            followup_mod.evidence_payload(db, cid, sid, "一元一次方程", mid),
        )
        check("0.1 夹具就绪：错题（带照片）+ 追问 + 2 份作答",
              bool(mid) and bool(followup["id"]) and len(answers) == 2,
              (mid, followup["id"], len(answers)))

        flows = [
            ("错题重新分析", lambda: api.api_reanalyze(
                FakeCtx(db, user, uploads, params={"id": mid}, ai_provider=provider))),
            ("错题照片分析建议", lambda: api.api_preview_analysis(
                FakeCtx(db, user, uploads, params={"id": mid},
                        body={"consent": True}, ai_provider=provider))),
            ("个人辅导建议生成", lambda: api.api_student_advice_generate(
                FakeCtx(db, user, uploads, params={"id": cid, "student_id": sid},
                        body={"consent": True}, ai_provider=provider))),
            ("追问候选生成", lambda: api.api_followup_suggest(
                FakeCtx(db, user, uploads, params={"id": cid, "student_id": sid},
                        body={"problem": "一元一次方程", "mistake_id": mid, "consent": True},
                        ai_provider=provider))),
            ("追问后的错因候选生成", lambda: api.api_followup_update(
                FakeCtx(db, user, uploads,
                        params={"id": cid, "student_id": sid, "fid": followup["id"]},
                        body={"consent": True}, ai_provider=provider))),
            ("作答分析", lambda: api.api_answer_analyse(
                FakeCtx(db, user, uploads, params={"id": cid, "answer_id": answers[0]["id"]},
                        body={"consent": True}, ai_provider=provider))),
        ]

        # ---------------------------------------------------------------
        section("一、开关默认关：6 个入口一律 403 ai_isolation，零网络尝试")
        # ---------------------------------------------------------------
        mark_calls = len(stub_calls())
        blocked = []
        for flow, call in flows:
            ok, detail = expect_isolation(call)
            blocked.append((flow, ok, detail))
        check("1.1 6 个入口全部命中 ai_isolation（返回与 R1-07 逐字等价）",
              all(item[1] for item in blocked), blocked)
        check("1.2 默认关时一次请求都没发出去", len(stub_calls()) == mark_calls,
              len(stub_calls()) - mark_calls)
        check("1.3 账号开关默认是关（库里读出来 false）",
              db_mod.ai_individual_allowed(db, user["id"]) is False,
              db_mod.ai_individual_allowed(db, user["id"]))
        check("1.4 审计表此时没有任何记录（没发就没有台账）",
              db_mod.ai_egress_audit_rows(db) == [], db_mod.ai_egress_audit_rows(db))

        # ---------------------------------------------------------------
        section("二、网页端：不注入 provider、/api/meta 里没有这个开关")
        # ---------------------------------------------------------------
        web = server_mod.make_server("127.0.0.1", 0, os.path.join(WORK, "web"), use_demo=False)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        try:
            import urllib.error as _uerr
            import urllib.parse as _uparse

            base = "http://127.0.0.1:%d" % web.server_address[1]
            opener = urllib.request.build_opener()
            token = {"value": ""}

            def http_call(method, path, body=None):
                headers = {}
                data = None
                if token["value"]:
                    headers["Cookie"] = "teacher_session=" + token["value"]
                if body is not None:
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(base + path, data=data, headers=headers,
                                             method=method)
                try:
                    with opener.open(req, timeout=20) as resp:
                        raw = resp.read()
                        cookie = resp.headers.get("Set-Cookie") or ""
                        if "teacher_session=" in cookie:
                            token["value"] = cookie.split("teacher_session=", 1)[1].split(";", 1)[0]
                        return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
                except _uerr.HTTPError as exc:
                    raw = exc.read()
                    try:
                        return exc.code, json.loads(raw.decode("utf-8")) if raw else {}
                    except ValueError:
                        return exc.code, {"raw": raw.decode("utf-8", "replace")}

            status, _ = http_call("POST", "/api/auth/register",
                                  {"username": "web14", "password": "pw-r1-14"})
            check("2.0 网页端能注册登录（用于取证）", status == 200, status)
            status, meta = http_call("GET", "/api/meta")
            check("2.1 网页端 /api/meta 里没有 ai_individual_allowed 这个键",
                  status == 200 and "ai_individual_allowed" not in (meta or {}),
                  sorted((meta or {}).keys())[:8])
            check("2.2 网页端 ai_mode 仍是 mock（没有 provider 注入）",
                  (meta or {}).get("ai_mode") == "mock", (meta or {}).get("ai_mode"))
            status, read_switch = http_call("GET", "/api/account/ai-individual")
            check("2.3 网页端能读这个偏好（GET 全字段），但 ai_egress_available=false",
                  status == 200 and (read_switch or {}).get("ai_individual_allowed") is False
                  and (read_switch or {}).get("ai_egress_available") is False, read_switch)
            status, wrote = http_call("POST", "/api/account/ai-individual", {"allowed": True})
            check("2.4 网页端能改这个偏好，但仍不能外发（ai_egress_available 恒 false）",
                  status == 200 and (wrote or {}).get("ai_individual_allowed") is True
                  and (wrote or {}).get("ai_egress_available") is False, wrote)
            status, reverted = http_call("POST", "/api/account/ai-individual", {"allowed": False})
            check("2.5 网页端可以关回去（偏好读写对称）",
                  status == 200 and (reverted or {}).get("ai_individual_allowed") is False, reverted)
        finally:
            web.shutdown()
            web.server_close()

        # ---------------------------------------------------------------
        section("三、账号开关打开：6 个入口放行 + payload 无身份字段 + 审计有记录")
        # ---------------------------------------------------------------
        switch_ctx = FakeCtx(db, user, uploads, body={"allowed": True}, ai_provider=provider)
        switched = api.api_account_ai_individual_update(switch_ctx)
        check("3.0 通过 /api/account/ai-individual 打开开关（桌面端）",
              switched.get("ai_individual_allowed") is True
              and db_mod.ai_individual_allowed(db, user["id"]) is True, switched)
        meta_ctx = FakeCtx(db, user, uploads, ai_provider=provider)
        meta_ctx.server = svc  # api_meta 只看 server 上的 portable/use_demo/data_dir
        try:
            desktop_meta = api.api_meta(meta_ctx)
        except Exception as exc:  # noqa: BLE001 —— meta 读不到不算本任务失败，但要如实记
            desktop_meta = {"error": str(exc)[:120]}
        check("3.1 桌面端 /api/meta 能读到开关（网页端读不到，见 2.1）",
              desktop_meta.get("ai_individual_allowed") is True,
              desktop_meta.get("ai_individual_allowed"))

        mark_calls = len(stub_calls())
        mark_audit = len(db_mod.ai_egress_audit_rows(db))
        allowed = []
        for flow, call in flows:
            ok, detail = expect_no_isolation(call)
            allowed.append((flow, ok, detail))
        check("3.2 6 个入口全部放行（不再 ai_isolation）",
              all(item[1] for item in allowed), allowed)
        sent = len(stub_calls()) - mark_calls
        check("3.3 开启后确实发生了外发（桩收到 %d 次请求）" % sent, sent >= 1, sent)

        # 逐条把实际 payload 字段打出来，并证明没有身份字段
        leaked = []
        shapes = []
        for item in stub_calls()[mark_calls:]:
            names = payload_field_names_of(item["body"])
            shapes.append({"path": item["path"], "fields": names})
            for token in IDENTITY_TOKENS:
                if token in item["body"]:
                    leaked.append((item["path"], token))
            lowered = item["body"]
            for key in ("answer_id", "student_id", "class_id", "user_id", "account"):
                if ('"%s"' % key) in lowered:
                    leaked.append((item["path"], key))
        log("实际发送的请求体字段（%d 条）：%s" % (len(shapes), json.dumps(shapes, ensure_ascii=False)))
        check("3.4 payload 里没有任何身份字段（姓名/学号/班级名/answer_id/account）",
              leaked == [], leaked)
        check("3.5 payload 字段全在白名单形状里（只有 model/messages/content/image/temperature 等）",
              all(set(name.split("[")[0] for name in item["fields"])
                  <= {"model", "messages", "content", "image_url", "image_source", "image",
                      "temperature", "max_tokens", "system", "usage", "stream"}
                  for item in shapes), shapes[:2])

        audit_rows = db_mod.ai_egress_audit_rows(db)
        new_rows = audit_rows[:len(audit_rows) - mark_audit]
        check("3.6 每次真实外发都留了审计（审计 %d 行 / 发送 %d 次）"
              % (len(new_rows), sent), len(new_rows) == sent, (len(new_rows), sent))
        check("3.7 审计字段齐全：人 / 时间 / 流程 / 字段清单 / 通道",
              all(row.get("user_id") == user["id"] and row.get("username") == user["username"]
                  and row.get("flow") and row.get("created_at")
                  and isinstance(row.get("payload_fields"), list) and row["payload_fields"]
                  and row.get("model_channel")
                  for row in new_rows),
              new_rows[:1])
        check("3.8 审计只记字段名、不记取值（不含学生姓名/学号/题干原文）",
              all(not any(token in json.dumps(row, ensure_ascii=False)
                          for token in ("学生甲", "学生乙", "隔离验证班", "XY001", "解方程", "3x-6"))
                  and all(isinstance(name, str) and len(name) <= 40 for name in row["payload_fields"])
                  for row in new_rows), new_rows[:1])
        flows_logged = {row.get("flow") for row in new_rows}
        check("3.9 审计里能区分是哪个流程（flow 名来自 6 个入口之一）",
              bool(flows_logged) and flows_logged <= {item[0] for item in flows}, flows_logged)

        # ---------------------------------------------------------------
        section("四、关回去：立刻恢复 403（不需要重启）")
        # ---------------------------------------------------------------
        api.api_account_ai_individual_update(FakeCtx(db, user, uploads, body={"allowed": False},
                                                     ai_provider=provider))
        check("4.0 开关已关回 false", db_mod.ai_individual_allowed(db, user["id"]) is False,
              db_mod.ai_individual_allowed(db, user["id"]))
        mark_calls = len(stub_calls())
        reblocked = [expect_isolation(call)[0] for _, call in flows]
        check("4.1 关闭后 6 个入口又全部 ai_isolation", all(reblocked), reblocked)
        check("4.2 关闭后没有再发生任何外发", len(stub_calls()) == mark_calls,
              len(stub_calls()) - mark_calls)
        check("4.3 拒绝文案与 R1-07 逐字一致（默认关 = 行为不变）",
              "没有把任何内容发出去" in analysis.INDIVIDUAL_FLOW_MESSAGE,
              analysis.INDIVIDUAL_FLOW_MESSAGE)

        # ---------------------------------------------------------------
        section("四之二、闸门顺序：mock/None 必须是 409 mock，**不是** 403 ai_isolation")
        # ---------------------------------------------------------------
        # 这条是给「闸门顺序写反」兜底的专项断言：t14 恢复四个入口时，tutor.generate 曾把
        # 「403 未授权」守卫写在「if provider is None: 409 mock」之前，于是 mock 模式也被当成
        # 未授权真实通道 → 界面拿到 403，而不是原版 1.8.1 的 409「当前是模拟分析…」。
        # 该类回归只有桌面 UI 套件能抓到（verify_tutor_170 全绿也发现不了），因此单列在此。
        from app import tutor as tutor_mod

        def _generate_with(provider):
            try:
                tutor_mod.generate(db, cid, sid, provider, "", "")
            except Exception as exc:  # noqa: BLE001 —— 只读 status/code/message
                return getattr(exc, "status", None), getattr(exc, "code", None), \
                    getattr(exc, "message", "") or ""
            return None, None, ""

        status, code, text = _generate_with(analysis.get_provider({"mode": "mock"}))
        check("4b.1 mock provider → 409 + code=mock（文案含「当前是模拟分析」，绝不是 403 ai_isolation）",
              (status, code) == (409, "mock") and "当前是模拟分析" in text,
              (status, code, text[:40]))
        status_none, code_none, _ = _generate_with(None)
        check("4b.2 provider=None（模拟）→ 同样 409 + code=mock",
              (status_none, code_none) == (409, "mock"), (status_none, code_none))
        unauthorized = vision.CloudVisionProvider(vision.resolve_config({
            "mode": "custom", "api_key": "sk-r1-14", "base_url": "http://127.0.0.1:9/v1",
            "model": "stub-model", "allow_env": False,
        }))
        status_real, code_real, _ = _generate_with(unauthorized)
        check("4b.3 真实通道 + 未开启 → 仍 403 ai_isolation（放行顺序不反噬真实通道）",
              (status_real, code_real) == (403, analysis.INDIVIDUAL_FLOW_CODE),
              (status_real, code_real))

        # ---------------------------------------------------------------
        section("五、旧库兼容：缺开关列 / 缺审计表时按「关」处理，写入时补列建表")
        # ---------------------------------------------------------------
        legacy_dir = os.path.join(WORK, "legacy")
        os.makedirs(legacy_dir)
        legacy_file = os.path.join(legacy_dir, "app.db")
        legacy = Database(legacy_file)
        try:
            import sqlite3 as _sqlite
            conn = _sqlite.connect(legacy_file)
            try:
                with conn:
                    conn.execute("ALTER TABLE users DROP COLUMN ai_individual_allowed")
                    conn.execute("DROP TABLE IF EXISTS ai_egress_audit")
            finally:
                conn.close()
            check("5.0 造好旧库替身：没有开关列、没有审计表",
                  not db_mod.ai_switch_ready(legacy)
                  and db_mod.ai_egress_audit_rows(legacy) == [], "legacy")
            check("5.1 旧库读开关不报错，按「关」处理（不会因为少一列崩掉）",
                  db_mod.ai_individual_allowed(legacy, 1) is False, "off")
            inserted = db_mod.record_ai_egress(legacy, user_id=1, username="old",
                                               flow="错题重新分析",
                                               model_channel="real/console",
                                               fields=["model", "messages[system,user]"],
                                               payload_bytes=42)
            check("5.2 旧库首次外发时自建审计表并写入成功",
                  bool(inserted) and len(db_mod.ai_egress_audit_rows(legacy)) == 1, inserted)
            added = db_mod.ensure_ai_switch_column(legacy)
            check("5.3 旧库写入开关时自动补列（幂等：第二次不再加）",
                  added is True and db_mod.ensure_ai_switch_column(legacy) is False, added)
            check("5.4 补列后默认仍是关（老账号不会被悄悄打开）",
                  db_mod.ai_individual_allowed(legacy, 1) is False, "off")
        finally:
            legacy.conn.close()
    finally:
        stub.shutdown()
        stub.server_close()

    log("")
    log("合计 %d 项，通过 %d，失败 %d" % (RESULTS["pass"] + RESULTS["fail"],
                                    RESULTS["pass"], RESULTS["fail"]))
    code = 0 if RESULTS["fail"] == 0 else 1
    log(exit_line(code))
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LINES) + "\n")
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
