"""账号与会话：PBKDF2 口令哈希、随机会话令牌、登录失败限流。

设计取舍（原型阶段）：
- 单个教师账号模型，一个账号可以看到自己建立的班级，看不到其他账号的班级。
- 会话令牌放在 HttpOnly + SameSite=Lax 的 Cookie 里，服务端存库、可吊销。
- 登录失败按来源 IP 计数，连续失败 5 次锁定 60 秒，抵御暴力猜口令。
"""

import hashlib
import hmac
import secrets
import time

from .db import now_iso

ITERATIONS = 200_000
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 60

_failures = {}  # ip -> [count, locked_until_ts]


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return salt, digest.hex(), iterations


def verify_password(password, salt, expected_hash, iterations):
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return hmac.compare_digest(digest.hex(), expected_hash)


def create_user(db, username, password, display_name):
    salt, digest, iterations = hash_password(password)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, display_name, pwd_salt, pwd_hash, iterations, created_at, backup_key)"
            " VALUES (?,?,?,?,?,?,?)",
            (username, display_name or username, salt, digest, iterations, now_iso(), secrets.token_hex(16)),
        )
        return cur.lastrowid


def set_password(db, user_id, password):
    salt, digest, iterations = hash_password(password)
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET pwd_salt=?, pwd_hash=?, iterations=? WHERE id=?",
            (salt, digest, iterations, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def check_login(db, username, password, ip):
    """返回 (user_id or None, 错误信息)。"""
    count, locked_until = _failures.get(ip, (0, 0.0))
    if locked_until > time.time():
        return None, "登录失败次数过多，请 %d 秒后再试" % int(locked_until - time.time() + 1)

    user = db.query_one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    ok = bool(user) and verify_password(password or "", user["pwd_salt"], user["pwd_hash"], user["iterations"])
    if not ok:
        count += 1
        _failures[ip] = (count, time.time() + LOCK_SECONDS if count >= MAX_FAILURES else 0.0)
        return None, "账号或密码不正确"
    _failures.pop(ip, None)
    return user["id"], ""


def create_session(db, user_id):
    token = secrets.token_urlsafe(32)
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, now_iso(), _expiry()),
        )
    return token


def _expiry():
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() + timedelta(seconds=SESSION_SECONDS)).isoformat(timespec="seconds")


def lookup_session(db, token):
    if not token:
        return None
    row = db.query_one(
        "SELECT s.token, s.expires_at, u.id, u.username, u.display_name"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?",
        (token,),
    )
    if not row or row["expires_at"] < now_iso():
        return None
    return row


def destroy_session(db, token):
    if token:
        with db.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
