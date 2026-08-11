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


def register(s, email: str, password: str, name: str) -> dict:
    email = email.strip().lower()
    if not email or len(password) < 6:
        raise HTTPException(400, "邮箱必填，密码至少 6 位")
    if s.query(db.Agent).filter_by(email=email).first():
        raise HTTPException(400, "该邮箱已注册")
    salt = secrets.token_hex(16)
    key = "ag_" + secrets.token_hex(8)
    s.add(db.Agent(agent_key=key, email=email, name=name or email.split("@")[0],
                   pw_hash=hash_pw(password, salt), salt=salt))
    s.commit()
    return dict(token=make_token(key), name=name or email.split("@")[0])


def login(s, email: str, password: str) -> dict:
    a = s.query(db.Agent).filter_by(email=email.strip().lower()).first()
    if not a or a.pw_hash != hash_pw(password, a.salt):
        raise HTTPException(401, "邮箱或密码不对")
    return dict(token=make_token(a.agent_key), name=a.name)


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
