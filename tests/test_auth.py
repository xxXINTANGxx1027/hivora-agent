"""认证回归：token 伪造 / 过期 / 停用即失效 / 限流 / 管理员守卫。

这些都是真实存在过的漏洞，改 auth.py 时必须全绿。
"""
import hashlib
import hmac
import time

import pytest

from conftest import H


def test_no_token_rejected(app_client):
    assert app_client.get("/api/dashboard").status_code == 401


def test_old_two_part_token_rejected(app_client):
    """旧格式 `key.sig` 必须失效——它没有过期时间。"""
    assert app_client.get("/api/dashboard",
                          headers=H("agent_admin.deadbeef")).status_code == 401


def test_forged_token_rejected(app_client):
    """用错的密钥签出来的 token 不能通过。"""
    payload = f"agent_admin.{int(time.time()) + 3600}"
    sig = hmac.new(b"dev-secret-hivora", payload.encode(), hashlib.sha256).hexdigest()
    assert app_client.get("/api/dashboard",
                          headers=H(f"{payload}.{sig}")).status_code == 401


def test_expired_token_rejected(app_client):
    import auth
    payload = f"agent_admin.{int(time.time()) - 10}"
    sig = hmac.new(auth.SECRET, payload.encode(), hashlib.sha256).hexdigest()
    assert app_client.get("/api/dashboard",
                          headers=H(f"{payload}.{sig}")).status_code == 401


def test_token_has_expiry(admin_token):
    key, exp, sig = admin_token.split(".")
    assert int(exp) > time.time()


def test_disabled_agent_token_dies_immediately(app_client, admin_token, agent_factory):
    """核心回归：管理员停用后，对方手里已签发的 token 必须立刻失效。"""
    tok, email = agent_factory()
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 200

    agents = app_client.get("/api/admin/agents", headers=H(admin_token)).json()
    aid = next(a["id"] for a in agents if a["email"] == email)
    app_client.post(f"/api/admin/agents/{aid}/toggle",
                    headers=H(admin_token), json={"active": False})

    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 403


def test_expired_plan_blocks_access(app_client, admin_token, agent_factory):
    import db
    tok, email = agent_factory()
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(email=email).first()
        a.expires = "2020-01-01"
        s.commit()
    finally:
        s.close()
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 403


def test_login_rate_limited(app_client, agent_factory):
    _, email = agent_factory()
    codes = [app_client.post("/api/auth/login",
                             json={"email": email, "password": "wrong"}).status_code
             for _ in range(6)]
    assert codes[-1] == 429, codes


def test_short_password_rejected(app_client, admin_token):
    r = app_client.post("/api/admin/agents/create", headers=H(admin_token),
                        json={"email": "short@test.local", "password": "1234567"})
    assert r.status_code == 400


def test_admin_endpoints_need_admin(app_client, demo_token):
    for path in ("/api/admin/agents", "/api/admin/invites"):
        assert app_client.get(path, headers=H(demo_token)).status_code == 403


def test_secret_key_has_no_default():
    """生产环境绝不能回落到硬编码密钥。"""
    import importlib
    import os
    import sys
    import db
    old_prod, old_key = db.IS_PROD, os.environ.get("SECRET_KEY")
    try:
        db.IS_PROD = True
        os.environ.pop("SECRET_KEY", None)
        sys.modules.pop("auth", None)
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            importlib.import_module("auth")
    finally:
        db.IS_PROD = old_prod
        os.environ["SECRET_KEY"] = old_key
        sys.modules.pop("auth", None)
        importlib.import_module("auth")
