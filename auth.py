"""认证：email+密码注册/登录，HMAC 签名 token，多租户隔离。"""
import hashlib
import hmac
import os
import secrets

from fastapi import Header, HTTPException

import db

SECRET = os.environ.get("SECRET_KEY", "dev-secret-hivora").encode()


def hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def make_token(agent_key: str) -> str:
    sig = hmac.new(SECRET, agent_key.encode(), hashlib.sha256).hexdigest()
    return f"{agent_key}.{sig}"


def verify_token(token: str) -> str | None:
    if not token or "." not in token:
        return None
    key, sig = token.rsplit(".", 1)
    good = hmac.new(SECRET, key.encode(), hashlib.sha256).hexdigest()
    return key if hmac.compare_digest(sig, good) else None


def current_agent(authorization: str = Header(default="")) -> str:
    tok = authorization.removeprefix("Bearer ").strip()
    key = verify_token(tok)
    if not key:
        raise HTTPException(401, "unauthorized")
    return key


def register(s, email: str, password: str, name: str, code: str = "") -> dict:
    email = email.strip().lower()
    if not email or len(password) < 6:
        raise HTTPException(400, "邮箱必填，密码至少 6 位")
    if s.query(db.Agent).filter_by(email=email).first():
        raise HTTPException(400, "该邮箱已注册")
    import datetime as _dt
    inv = s.query(db.InviteCode).filter_by(code=code.strip().upper(), used_by="").first()
    if not inv:
        raise HTTPException(403, "需要有效的邀请码（请联系管理员购买获取）")
    salt = secrets.token_hex(16)
    key = "ag_" + secrets.token_hex(8)
    s.add(db.Agent(agent_key=key, email=email, name=name or email.split("@")[0],
                   pw_hash=hash_pw(password, salt), salt=salt))
    inv.used_by = email
    inv.used = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    s.commit()
    return dict(token=make_token(key), name=name or email.split("@")[0], role="agent")


def login(s, email: str, password: str) -> dict:
    import datetime as _dt
    a = s.query(db.Agent).filter_by(email=email.strip().lower()).first()
    if not a or a.pw_hash != hash_pw(password, a.salt):
        raise HTTPException(401, "邮箱或密码不对")
    if not getattr(a, "active", 1):
        raise HTTPException(403, "账号已停用，请联系管理员")
    if getattr(a, "expires", "") and a.expires < _dt.date.today().isoformat():
        raise HTTPException(403, f"账号已于 {a.expires} 到期，请联系管理员续费")
    return dict(token=make_token(a.agent_key), name=a.name,
                role=getattr(a, "role", "agent"))


def current_admin(authorization: str = Header(default="")) -> str:
    key = current_agent(authorization)
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(agent_key=key).first()
        if not a or getattr(a, "role", "agent") != "admin":
            raise HTTPException(403, "需要管理员权限")
        return key
    finally:
        s.close()


ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin123")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


def ensure_admin():
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(agent_key="agent_admin").first()
        salt = secrets.token_hex(16)
        if a:   # 已存在 → 同步为当前配置的账号/密码
            a.email, a.role, a.plan = ADMIN_EMAIL, "admin", "owner"
            a.pw_hash, a.salt = hash_pw(ADMIN_PASSWORD, salt), salt
        else:
            s.add(db.Agent(agent_key="agent_admin", email=ADMIN_EMAIL, name="Admin (XT)",
                           pw_hash=hash_pw(ADMIN_PASSWORD, salt), salt=salt,
                           role="admin", plan="owner"))
        s.commit()
    finally:
        s.close()


def ensure_demo_agent():
    """种子演示账号：demo@hivora.my / demo1234 → 绑定 agent_demo 的种子数据。"""
    s = db.SessionLocal()
    try:
        if not s.query(db.Agent).filter_by(email="demo@hivora.my").first():
            salt = secrets.token_hex(16)
            s.add(db.Agent(agent_key="agent_demo", email="demo@hivora.my",
                           name="Demo Agent", pw_hash=hash_pw("demo1234", salt), salt=salt))
            s.commit()
    finally:
        s.close()
