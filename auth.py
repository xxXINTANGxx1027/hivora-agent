"""认证：email+密码登录，HMAC 签名 token（带过期），每请求校验账号状态，多租户隔离。

closed SaaS：没有自助注册，账号一律由管理员在管理站创建。

Token 格式：`agent_key.exp.sig`，sig = HMAC-SHA256(SECRET, "agent_key.exp")。
每次请求都回库确认 active / expires —— 管理员点「停用」后，对方手里的 token 立刻失效。
"""
import datetime as dt
import hashlib
import hmac
import os
import secrets
import time

from fastapi import Header, HTTPException

import db

TOKEN_TTL_DAYS = int(os.environ.get("TOKEN_TTL_DAYS", "14"))


def _load_secret() -> bytes:
    """签名密钥。绝不要默认常量——否则任何人都能离线伪造 admin token。"""
    raw = os.environ.get("SECRET_KEY", "").strip()
    if len(raw) >= 32:
        return raw.encode()
    if db.IS_PROD:
        raise RuntimeError(
            "SECRET_KEY 未设置或短于 32 字符 —— 生产环境拒绝启动。"
            "请在 Render Environment 里设置（`openssl rand -hex 32` 生成）。")
    print("⚠️  SECRET_KEY 未设置：本次启动使用随机密钥，重启后所有登录态失效（仅限本地开发）。")
    return secrets.token_hex(32).encode()


SECRET = _load_secret()


def hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def make_token(agent_key: str) -> str:
    exp = int(time.time()) + TOKEN_TTL_DAYS * 86400
    payload = f"{agent_key}.{exp}"
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str) -> str | None:
    """只校验签名和过期时间；账号状态由 _authed 回库确认。"""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    key, exp, sig = parts
    payload = f"{key}.{exp}"
    good = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return key


def _authed(authorization: str) -> dict:
    """验签 + 回库确认账号仍然可用。返回 {key, role}。"""
    key = verify_token(authorization.removeprefix("Bearer ").strip())
    if not key:
        raise HTTPException(401, "登录已过期，请重新登录")
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(agent_key=key).first()
        if not a:
            raise HTTPException(401, "账号不存在")
        if not (1 if a.active is None else a.active):
            raise HTTPException(403, "账号已停用，请联系管理员")
        if a.expires and a.expires < dt.date.today().isoformat():
            raise HTTPException(403, f"账号已于 {a.expires} 到期，请联系管理员续费")
        return dict(key=a.agent_key, role=a.role or "agent")
    finally:
        s.close()


def current_agent(authorization: str = Header(default="")) -> str:
    return _authed(authorization)["key"]


def current_admin(authorization: str = Header(default="")) -> str:
    a = _authed(authorization)
    if a["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return a["key"]


# ── 登录限流（进程内，够用于当前单实例规模）────────────────────
_FAILS: dict[str, tuple[int, float]] = {}
_LOCK_AFTER, _LOCK_SECS = 5, 300


def _check_lock(email: str):
    n, until = _FAILS.get(email, (0, 0.0))
    if n >= _LOCK_AFTER and time.time() < until:
        raise HTTPException(429, f"登录失败次数过多，请 {int(until - time.time())} 秒后再试")


def _note_fail(email: str):
    n, _ = _FAILS.get(email, (0, 0.0))
    _FAILS[email] = (n + 1, time.time() + _LOCK_SECS)


def login(s, email: str, password: str) -> dict:
    email = email.strip().lower()
    _check_lock(email)
    a = s.query(db.Agent).filter_by(email=email).first()
    if not a or not hmac.compare_digest(a.pw_hash, hash_pw(password, a.salt)):
        _note_fail(email)
        raise HTTPException(401, "邮箱或密码不对")
    if not (1 if a.active is None else a.active):
        raise HTTPException(403, "账号已停用，请联系管理员")
    if a.expires and a.expires < dt.date.today().isoformat():
        raise HTTPException(403, f"账号已于 {a.expires} 到期，请联系管理员续费")
    _FAILS.pop(email, None)
    db.audit(s, a.agent_key, "login", email)
    s.commit()
    return dict(token=make_token(a.agent_key), name=a.name, role=a.role or "agent")


# ── 初始账号 ────────────────────────────────────────────────
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_WEAK = {"admin123", "admin", "password", "123456", "hivora"}


def ensure_admin():
    """创建管理员。已存在时**不**重置密码——除非显式设置了 ADMIN_PASSWORD。"""
    email, pw = ADMIN_EMAIL, ADMIN_PASSWORD
    if db.IS_PROD:
        if pw and (pw.lower() in _WEAK or len(pw) < 10):
            raise RuntimeError("ADMIN_PASSWORD 太弱（至少 10 位且不能是常见口令），生产环境拒绝启动。")
    elif not email:
        email, pw = email or "admin123", pw or "admin123"   # 仅本地开发的便利默认值

    s = db.SessionLocal()
    try:
        a = (s.query(db.Agent).filter_by(agent_key="agent_admin").first()
             or s.query(db.Agent).filter_by(role="admin").first())
        if a:
            a.role, a.plan = "admin", a.plan or "owner"
            if email and email != a.email:
                a.email = email
            if pw:      # 只有显式给了密码才改；不给就保持原样，避免静默退回默认口令
                salt = secrets.token_hex(16)
                a.pw_hash, a.salt = hash_pw(pw, salt), salt
            s.commit()
            return
        if not email or not pw:
            raise RuntimeError(
                "还没有管理员账号，且未设置 ADMIN_EMAIL / ADMIN_PASSWORD。"
                "请在 Render Environment 里设置后重新部署。")
        salt = secrets.token_hex(16)
        s.add(db.Agent(agent_key="agent_admin", email=email, name="Admin",
                       pw_hash=hash_pw(pw, salt), salt=salt, role="admin", plan="owner"))
        s.commit()
    finally:
        s.close()


def ensure_demo_agent():
    """演示账号 demo@hivora.my / demo1234 —— 只在 DEMO_DATA=1（非生产）时创建。"""
    if not db.DEMO_DATA:
        return
    s = db.SessionLocal()
    try:
        if not s.query(db.Agent).filter_by(email="demo@hivora.my").first():
            salt = secrets.token_hex(16)
            s.add(db.Agent(agent_key=db.DEMO_AGENT, email="demo@hivora.my",
                           name="Demo Agent", pw_hash=hash_pw("demo1234", salt), salt=salt))
            s.commit()
    finally:
        s.close()
