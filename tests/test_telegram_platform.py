"""官方共享 bot（平台模式）：一键接入、客户专属链接归属、设备绑定、租户隔离。

全程不打真实 Telegram —— telegram.call 换成假的。
"""
import pytest

from conftest import H


@pytest.fixture
def fake_platform(monkeypatch):
    import telegram
    sent, calls = [], []

    def fake_call(token, method, payload=None):
        calls.append((token, method, payload or {}))
        if method == "getMe":
            return {"username": "hivora_official_bot", "id": 999}
        if method == "sendMessage":
            sent.append((payload["chat_id"], payload["text"]))
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PLATFORM_BOT_TOKEN", "888:PLATFORM")
    return type("T", (), {"sent": sent, "calls": calls})


def _upd(text, chat_id, name="Ali"):
    return {"message": {"text": text, "chat": {"id": int(chat_id), "first_name": name}}}


def _post_platform(app_client, update, secret=None):
    import telegram
    return app_client.post(
        "/api/tg/platform", json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret or telegram.platform_header_secret()})


def _connect(app_client, tok):
    return app_client.post("/api/telegram/connect-platform", headers=H(tok))


# ── 接入 ──────────────────────────────────────────────────────
def test_one_click_connect_needs_no_token(app_client, agent_factory, fake_platform):
    tok, _ = agent_factory()
    r = _connect(app_client, tok)
    assert r.status_code == 200
    assert r.json()["mode"] == "platform"

    st = app_client.get("/api/telegram", headers=H(tok)).json()
    assert st["connected"] and st["mode"] == "platform"
    assert st["token_hint"] == "官方 bot"          # 永远不泄漏平台 token
    assert "PLATFORM" not in str(st)
    assert st["cust_link"].startswith("https://t.me/hivora_official_bot?start=")

    hook = next(p for _, m, p in fake_platform.calls if m == "setWebhook")
    assert hook["url"] == "https://api.example.com/api/tg/platform"


def test_connect_platform_rejected_when_not_configured(app_client, agent_factory,
                                                       fake_platform, monkeypatch):
    monkeypatch.delenv("PLATFORM_BOT_TOKEN", raising=False)
    tok, _ = agent_factory()
    assert _connect(app_client, tok).status_code == 400


# ── 客户专属链接归属 ───────────────────────────────────────────
def test_customer_start_link_routes_to_right_tenant(app_client, agent_factory, fake_platform):
    import db
    tok_a, email_a = agent_factory()
    tok_b, _ = agent_factory()
    _connect(app_client, tok_a)
    _connect(app_client, tok_b)
    link_a = app_client.get("/api/telegram", headers=H(tok_a)).json()["cust_link"]
    code_a = link_a.split("start=")[1]

    r = _post_platform(app_client, _upd(f"/start {code_a}", 700100, "Customer Wong"))
    assert r.status_code == 200

    s = db.SessionLocal()
    try:
        key_a = s.query(db.Agent).filter_by(email=email_a).first().agent_key
        t = s.query(db.Thread).filter_by(tg_chat_id="700100").first()
        assert t is not None and t.agent_id == key_a, "客户必须归到链接所属的租户"
    finally:
        s.close()
    assert any("700100" in str(c) for c, _ in fake_platform.sent), "客户应收到欢迎语"


def test_wrong_secret_is_dropped(app_client, agent_factory, fake_platform):
    import db
    tok, _ = agent_factory()
    _connect(app_client, tok)
    before = db.SessionLocal().query(db.Thread).count()
    _post_platform(app_client, _upd("/start whatever", 700200), secret="WRONG")
    assert db.SessionLocal().query(db.Thread).count() == before


def test_stranger_without_link_is_not_assigned_to_any_tenant(app_client, agent_factory,
                                                             fake_platform):
    import db
    tok, _ = agent_factory()
    _connect(app_client, tok)
    _post_platform(app_client, _upd("你好，我想买保险", 700300))
    assert db.SessionLocal().query(db.Thread).filter_by(tg_chat_id="700300").count() == 0, \
        "没走专属链接的陌生人绝不能被猜派给任何租户"
    assert any("专属链接" in t for _, t in fake_platform.sent)


# ── 设备绑定（代理人本人）──────────────────────────────────────
def test_device_bind_via_platform_bot(app_client, agent_factory, fake_platform):
    tok, _ = agent_factory()
    _connect(app_client, tok)
    code = app_client.post("/api/telegram/bindcode", headers=H(tok)).json()["code"]
    _post_platform(app_client, _upd(f"/start {code}", 700400, "XT Phone"))

    st = app_client.get("/api/telegram", headers=H(tok)).json()
    assert any(c["chat_id"] == "700400" for c in st["chats"]), "设备应绑定成功"
    assert any("绑定成功" in t for _, t in fake_platform.sent)


def test_platform_disconnect_keeps_shared_webhook(app_client, agent_factory, fake_platform):
    tok, _ = agent_factory()
    _connect(app_client, tok)
    fake_platform.calls.clear()
    app_client.post("/api/telegram/disconnect", headers=H(tok))
    assert "deleteWebhook" not in [m for _, m, _ in fake_platform.calls], \
        "平台 webhook 是共享的，单个租户断开绝不能删"
