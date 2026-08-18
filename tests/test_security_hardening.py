"""安全加固：每 IP 限速、Telegram 绑定码防爆破、长效码加长、HSTS。"""
import pytest
from conftest import H


# ── 每 IP 限速 ────────────────────────────────────────────────
def test_auth_endpoints_rate_limited_per_ip(app_client, monkeypatch):
    import main
    monkeypatch.setattr(main, "RL_AUTH_MAX", 5)
    main._RL.clear()
    codes = [app_client.post("/api/auth/login",
                             json={"email": f"x{i}@nowhere.local", "password": "wrong"}
                             ).status_code for i in range(8)]
    assert 429 in codes, "换着邮箱爆破也该被按 IP 拦下"
    assert codes[0] != 429, "正常第一发不该被拦"
    main._RL.clear()


def test_general_api_rate_limit_kicks_in(app_client, agent_factory, monkeypatch):
    import main
    tok, _ = agent_factory()
    monkeypatch.setattr(main, "RL_MAX", 5)
    main._RL.clear()
    codes = [app_client.get("/api/dashboard", headers=H(tok)).status_code
             for _ in range(8)]
    assert codes[-1] == 429
    main._RL.clear()


def test_telegram_webhook_not_ip_limited(app_client, monkeypatch):
    """Telegram 机房少数几个出口 IP 发所有租户的消息，按 IP 限会误杀整个渠道。"""
    import main
    monkeypatch.setattr(main, "RL_MAX", 1)
    main._RL.clear()
    # webhook 路径不限速（错 secret 会被丢弃但返回 200，绝不是 429）
    for _ in range(5):
        r = app_client.post("/api/tg/whatever", json={},
                            headers={"X-Telegram-Bot-Api-Secret-Token": "x"})
        assert r.status_code != 429
    main._RL.clear()


# ── 绑定码防爆破 ──────────────────────────────────────────────
def test_bind_code_guessing_gets_locked_out(app_client, agent_factory, monkeypatch):
    import telegram

    sent = []
    monkeypatch.setattr(telegram, "call",
                        lambda t, m, p=None: ({"username": "b"} if m == "getMe"
                                              else sent.append((p or {}).get("text"))) or {})
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PLATFORM_BOT_TOKEN", "888:PLATFORM")
    telegram._GUESS_FAILS.clear()

    tok, _ = agent_factory()
    assert app_client.post("/api/telegram/connect-platform",
                           headers=H(tok)).status_code == 200

    def start(arg):
        return app_client.post(
            "/api/tg/platform",
            json={"message": {"text": f"/start {arg}",
                              "chat": {"id": 555001, "first_name": "Mallory"}}},
            headers={"X-Telegram-Bot-Api-Secret-Token": telegram.platform_header_secret()})

    for i in range(5):
        start(f"HVWRON{i}")
    start("HV999999")
    assert any("尝试次数太多" in (t or "") for t in sent), "第 6 次猜码该被节流"

    # 关键：锁死期间连正确的码也不放行 —— 否则爆破到最后一发照样得手
    import db
    s = db.SessionLocal()
    try:
        agents = app_client.get("/api/telegram", headers=H(tok))
        code = telegram.new_bind_code(s, "whatever-key")
    finally:
        s.close()
    sent.clear()
    start(code)
    assert any("尝试次数太多" in (t or "") for t in sent)
    telegram._GUESS_FAILS.clear()


def test_long_ttl_bind_codes_are_longer(app_client):
    import db
    import telegram
    s = db.SessionLocal()
    try:
        short = telegram.new_bind_code(s, "k1")
        long_ = telegram.new_bind_code(s, "k2", ttl=24 * 3600)
    finally:
        s.close()
    assert len(short) == 8, "短效码保持 6 位十六进制方便手打"
    assert len(long_) == 16, "24 小时的码没人手打，加长抗猜测"


# ── HSTS ─────────────────────────────────────────────────────
def test_hsts_only_in_prod(app_client, monkeypatch):
    import db
    r = app_client.get("/healthz")
    assert "strict-transport-security" not in {k.lower() for k in r.headers}
    monkeypatch.setattr(db, "IS_PROD", True)
    r = app_client.get("/healthz")
    assert "strict-transport-security" in {k.lower() for k in r.headers}
