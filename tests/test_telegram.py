"""Telegram 入口：绑定流程、未授权拒答、token 不外泄、配额与多租户仍然生效。

全程不碰真实 Telegram —— telegram.call 被替换成假的，只记录调了什么。
"""
import pytest

from conftest import H


@pytest.fixture
def fake_tg(monkeypatch):
    """拦住所有出站请求，返回可控结果，并记下发出去的消息。"""
    import telegram
    sent, calls = [], []

    def fake_call(token, method, payload=None):
        calls.append((token, method, payload or {}))
        if method == "getMe":
            if not token.startswith("111:"):
                raise telegram.TelegramError("Unauthorized")
            return {"username": "my_agent_bot", "id": 111}
        if method == "sendMessage":
            sent.append((payload["chat_id"], payload["text"]))
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    return type("T", (), {"sent": sent, "calls": calls})


def _connect(app_client, tok, token="111:AAA"):
    return app_client.post("/api/telegram/connect", headers=H(tok),
                           json={"token": token})


def _update(text, chat_id="900001", name="Ali"):
    return {"message": {"text": text,
                        "chat": {"id": int(chat_id), "first_name": name}}}


def _bot_row(email):
    import db
    s = db.SessionLocal()
    try:
        key = s.query(db.Agent).filter_by(email=email).first().agent_key
        return key, s.query(db.TelegramBot).filter_by(agent_id=key).first()
    finally:
        s.close()


# ── 连接 ──────────────────────────────────────────────────────
def test_connect_validates_token_and_registers_webhook(app_client, agent_factory, fake_tg):
    tok, _ = agent_factory()
    r = _connect(app_client, tok)
    assert r.status_code == 200 and r.json()["username"] == "my_agent_bot"

    methods = [m for _, m, _ in fake_tg.calls]
    assert "getMe" in methods and "setWebhook" in methods
    hook = next(p for _, m, p in fake_tg.calls if m == "setWebhook")
    assert hook["url"].startswith("https://api.example.com/api/tg/")
    assert hook["secret_token"], "webhook 没有设 secret，谁都能伪造回调"


def test_bad_token_is_rejected(app_client, agent_factory, fake_tg):
    tok, _ = agent_factory()
    r = _connect(app_client, tok, token="999:WRONG")
    assert r.status_code == 400
    assert "setWebhook" not in [m for _, m, _ in fake_tg.calls]


def test_token_is_never_returned_in_full(app_client, agent_factory, fake_tg):
    import db
    tok, email = agent_factory()
    _connect(app_client, tok, token="111:SUPERSECRETVALUE")
    body = app_client.get("/api/telegram", headers=H(tok)).text
    assert "SUPERSECRETVALUE" not in body
    assert app_client.get("/api/telegram", headers=H(tok)).json()["token_hint"] == "…ALUE"

    # 库里也不能是明文
    _, row = _bot_row(email)
    assert "SUPERSECRETVALUE" not in row.token_enc
    import telegram
    assert telegram.decrypt(row.token_enc) == "111:SUPERSECRETVALUE"


def test_connect_needs_public_base_url(app_client, agent_factory, fake_tg, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    tok, _ = agent_factory()
    r = _connect(app_client, tok)
    assert r.status_code == 400 and "PUBLIC_BASE_URL" in r.json()["detail"]


# ── 绑定 ──────────────────────────────────────────────────────
def test_unbound_chat_gets_no_answers(app_client, agent_factory, fake_tg):
    """bot 链接被转发出去也没用 —— 没绑过就不回答任何业务问题。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("张伟明有哪些保单？"))
    finally:
        s.close()
    assert len(fake_tg.sent) == 1
    assert "还没绑定" in fake_tg.sent[0][1]


def test_bind_then_ask(app_client, agent_factory, fake_tg, monkeypatch):
    import db
    import graph
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)

    code = app_client.post("/api/telegram/bindcode", headers=H(tok)).json()["code"]

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update(f"/start {code}"))
    finally:
        s.close()
    assert "绑定成功" in fake_tg.sent[-1][1]

    # 绑定后提问 —— 走的是同一个大脑
    monkeypatch.setattr(graph, "ask",
                        lambda q, aid: dict(route="clientbook", answer="答案在此",
                                            citations=[], needs_human=False))
    monkeypatch.setattr(telegram, "ask", graph.ask, raising=False)
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("张伟明有哪些保单？"))
    finally:
        s.close()
    assert "答案在此" in fake_tg.sent[-1][1]

    assert app_client.get("/api/telegram", headers=H(tok)).json()["chats"]


def test_bind_code_expires(app_client, agent_factory, fake_tg):
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)
    code = app_client.post("/api/telegram/bindcode", headers=H(tok)).json()["code"]

    s = db.SessionLocal()
    try:
        b = s.query(db.TelegramBind).filter_by(code=code).first()
        b.expires = 0          # 假装过期
        s.commit()
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update(f"/start {code}"))
    finally:
        s.close()
    assert "过期" in fake_tg.sent[-1][1]


def test_bind_code_of_another_agent_is_refused(app_client, agent_factory, fake_tg):
    """拿别人的绑定码来绑自己的 bot，不能过。"""
    import db
    import telegram
    tok_a, email_a = agent_factory()
    tok_b, email_b = agent_factory()
    _connect(app_client, tok_a)
    _connect(app_client, tok_b, token="111:BBB")
    code_a = app_client.post("/api/telegram/bindcode", headers=H(tok_a)).json()["code"]
    key_b, row_b = _bot_row(email_b)

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row_b.path_secret, row_b.header_secret,
                               _update(f"/start {code_a}"))
        assert s.query(db.TelegramChat).filter_by(agent_id=key_b).count() == 0
    finally:
        s.close()
    assert "不属于这个 bot" in fake_tg.sent[-1][1]


# ── webhook 认证 ──────────────────────────────────────────────
def test_wrong_secret_header_is_ignored(app_client, agent_factory, fake_tg):
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, "伪造的", _update("你好"))
    finally:
        s.close()
    assert fake_tg.sent == [], "secret 不对还回了消息"


def test_unknown_path_is_ignored(app_client, fake_tg):
    import db
    import telegram
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, "根本不存在的路径", "x", _update("你好"))
    finally:
        s.close()
    assert fake_tg.sent == []


def test_webhook_endpoint_always_returns_200(app_client):
    """给 Telegram 返错它会不停重投，所以永远 200。"""
    r = app_client.post("/api/tg/bogus-path", json={"message": {}})
    assert r.status_code == 200
    r = app_client.post("/api/tg/bogus-path", content=b"not json")
    assert r.status_code == 200


# ── 配额与多租户 ──────────────────────────────────────────────
def test_quota_applies_to_telegram_too(app_client, agent_factory, fake_tg):
    """从 Telegram 进来的提问一样要受配额约束，否则是个绕过口子。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)
    code = app_client.post("/api/telegram/bindcode", headers=H(tok)).json()["code"]

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update(f"/start {code}"))
        a = s.query(db.Agent).filter_by(agent_key=key).first()
        a.token_quota = 1
        s.commit()
    finally:
        s.close()
    db.record_usage(key, "m", 50, 50)

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("帮我写个话术"))
    finally:
        s.close()
    assert "上限" in fake_tg.sent[-1][1]


def test_disconnect_clears_everything(app_client, agent_factory, fake_tg):
    import db
    tok, email = agent_factory()
    _connect(app_client, tok)
    app_client.post("/api/telegram/bindcode", headers=H(tok))
    app_client.post("/api/telegram/disconnect", headers=H(tok))

    assert app_client.get("/api/telegram", headers=H(tok)).json()["connected"] is False
    key, row = _bot_row(email)
    assert row is None
    s = db.SessionLocal()
    try:
        assert s.query(db.TelegramChat).filter_by(agent_id=key).count() == 0
        assert s.query(db.TelegramBind).filter_by(agent_id=key).count() == 0
    finally:
        s.close()


def test_telegram_endpoints_require_login(app_client):
    for path in ("/api/telegram",):
        assert app_client.get(path).status_code == 401
    assert app_client.post("/api/telegram/connect", json={"token": "x"}).status_code == 401
