"""白手套接 bot（管理员替客户挂专属 bot）+ 回收站超期自动真删。"""
import datetime as dt

import pytest
from conftest import H


@pytest.fixture
def fake_platform(monkeypatch):
    import telegram
    sent, calls = [], []

    def fake_call(token, method, payload=None):
        calls.append((token, method, payload or {}))
        if method == "getMe":
            return {"username": "abc_insurance_bot", "id": 777}
        if method == "sendMessage":
            sent.append((payload["chat_id"], payload["text"]))
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PLATFORM_BOT_TOKEN", "888:PLATFORM")
    return type("T", (), {"sent": sent, "calls": calls})


def _agent_id(client, admin_token, email):
    return next(a["id"] for a in client.get("/api/admin/agents",
                                            headers=H(admin_token)).json()
                if a["email"] == email)


# ── 白手套接 bot ──────────────────────────────────────────────
def test_admin_attaches_own_brand_bot_for_client(app_client, admin_token,
                                                 agent_factory, fake_platform):
    """内部替客户建好 bot 后挂 token，客户零操作得到自己品牌名的 bot。"""
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)

    r = app_client.post(f"/api/admin/agents/{aid}/telegram", headers=H(admin_token),
                        json={"token": "111222:ABCCLIENT"})
    assert r.status_code == 200, r.text

    st = app_client.get("/api/telegram", headers=H(tok)).json()
    assert st["connected"] and st.get("mode", "own") != "platform"

    rows = app_client.get("/api/admin/agents", headers=H(admin_token)).json()
    me = next(a for a in rows if a["email"] == email)
    assert me["tg"].startswith("@"), "账号列表应显示挂的是哪个 bot"


def test_attach_upgrades_platform_tenant_to_own_bot(app_client, admin_token,
                                                    agent_factory, fake_platform):
    """从官方共享 bot 升级到专属 bot：mode 必须切回 own，不能再借平台 token。"""
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    assert app_client.post("/api/telegram/connect-platform",
                           headers=H(tok)).status_code == 200

    r = app_client.post(f"/api/admin/agents/{aid}/telegram", headers=H(admin_token),
                        json={"token": "111222:ABCCLIENT"})
    assert r.status_code == 200

    st = app_client.get("/api/telegram", headers=H(tok)).json()
    assert st["connected"] and st.get("mode") == "own"
    # 新 webhook 一定要用客户自己的 token 注册，而不是平台 token
    hooks = [(t, p) for t, m, p in fake_platform.calls if m == "setWebhook"]
    assert hooks[-1][0] == "111222:ABCCLIENT"


def test_attach_rejects_garbage_token_and_non_admin(app_client, admin_token,
                                                    agent_factory, fake_platform):
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    assert app_client.post(f"/api/admin/agents/{aid}/telegram", headers=H(admin_token),
                           json={"token": "not-a-token"}).status_code == 400
    assert app_client.post(f"/api/admin/agents/{aid}/telegram", headers=H(tok),
                           json={"token": "111222:ABCCLIENT"}).status_code == 403


def test_admin_can_detach_bot(app_client, admin_token, agent_factory, fake_platform):
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    app_client.post(f"/api/admin/agents/{aid}/telegram", headers=H(admin_token),
                    json={"token": "111222:ABCCLIENT"})
    r = app_client.post(f"/api/admin/agents/{aid}/telegram/disconnect",
                        headers=H(admin_token))
    assert r.status_code == 200
    st = app_client.get("/api/telegram", headers=H(tok)).json()
    assert not st["connected"]


# ── 回收站自动清理 ────────────────────────────────────────────
def test_expired_trash_is_purged_but_fresh_trash_survives(app_client, agent_factory):
    import db
    tok, email = agent_factory()
    app_client.post("/api/clients", headers=H(tok),
                    json={"name": "超期客户", "phone": "", "notes": ""})
    app_client.post("/api/clients", headers=H(tok),
                    json={"name": "刚删的客户", "phone": "", "notes": ""})
    clients = app_client.get("/api/clients", headers=H(tok)).json()
    old = next(c for c in clients if c["name"] == "超期客户")
    fresh = next(c for c in clients if c["name"] == "刚删的客户")
    for c in (old, fresh):
        app_client.post("/api/delete", headers=H(tok),
                        json={"kind": "client", "id": c["id"]})

    # 把其中一个的删除时间拨回 31 天前
    s = db.SessionLocal()
    try:
        row = s.query(db.Client).filter_by(id=old["id"]).first()
        row.deleted = (dt.datetime.now() - dt.timedelta(days=31)
                       ).strftime("%Y-%m-%d %H:%M:%S")
        s.commit()
    finally:
        s.close()

    out = db.purge_expired_trash(30)
    assert out.get("clients") == 1

    s = db.SessionLocal()
    try:
        assert s.query(db.Client).filter_by(id=old["id"]).first() is None, "超期的要真删"
        assert s.query(db.Client).filter_by(id=fresh["id"]).first() is not None, "没超期的要留着"
    finally:
        s.close()


def test_purge_disabled_when_days_nonpositive(app_client):
    import db
    assert db.purge_expired_trash(0) == {}
