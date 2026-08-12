"""开通邮件。全程不发真信 —— smtplib 被替换成假的。

最要紧的一条：**发信失败绝不能让建账号失败**。账号已经建好了，
邮件只是通知手段；发不出去时管理站会把凭据显示出来让管理员手动发。
"""
import json

import pytest

from conftest import H


@pytest.fixture
def smtp(monkeypatch):
    """假 SMTP：记下发了什么，不真的连网。"""
    import email_out
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, **kw):   # SMTP_SSL 会多传 context
            sent.append({"connect": (host, port)})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            sent.append({"login": u})

        def send_message(self, msg):
            sent.append({"to": msg["To"], "subject": msg["Subject"],
                         "body": msg.get_content()})

    monkeypatch.setattr(email_out.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_out.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(email_out, "HOST", "smtp.example.com")
    monkeypatch.setattr(email_out, "PORT", 587)
    monkeypatch.setattr(email_out, "USER", "bot@hivora.my")
    monkeypatch.setattr(email_out, "FROM", "bot@hivora.my")
    return type("S", (), {"sent": sent,
                          "mails": property(lambda self: [x for x in sent if "to" in x])})()


@pytest.fixture
def no_smtp(monkeypatch):
    import email_out
    monkeypatch.setattr(email_out, "HOST", "")
    monkeypatch.setattr(email_out, "FROM", "")
    monkeypatch.setattr(email_out, "RESEND_KEY", "")


@pytest.fixture
def resend(monkeypatch):
    """假的 Resend HTTP API：记下请求，不真的联网。"""
    import email_out
    calls = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "method": req.get_method(),
                      "headers": {k.lower(): v for k, v in req.header_items()},
                      "json": json.loads(req.data.decode("utf-8"))})
        return FakeResp()

    monkeypatch.setattr(email_out.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(email_out, "RESEND_KEY", "re_test_key_123")
    monkeypatch.setattr(email_out, "FROM", "Hivora <onboarding@resend.dev>")
    monkeypatch.setattr(email_out, "HOST", "")      # 免费档上 SMTP 就是不可用的
    return type("R", (), {"calls": calls,
                          "mails": property(lambda self: [c["json"] for c in calls])})()


def _create(app_client, admin_token, email, password="Welcome-2026x"):
    return app_client.post("/api/admin/agents/create", headers=H(admin_token),
                           json={"email": email, "password": password, "name": "新代理人"})


# ── 开通信 ────────────────────────────────────────────────────
def test_welcome_email_goes_out_on_account_creation(app_client, admin_token, smtp):
    r = _create(app_client, admin_token, "newbie@test.local")
    assert r.status_code == 200
    assert r.json()["email_sent"] is True

    mail = [x for x in smtp.sent if "to" in x][-1]
    assert mail["to"] == "newbie@test.local"
    assert "开通" in mail["subject"]
    assert "newbie@test.local" in mail["body"]
    # 给的是一次性链接，**密码绝不能出现在邮件里**
    assert "Welcome-2026x" not in mail["body"]
    assert "?setup=" in mail["body"], "信里得有设密码的链接"
    assert r.json()["setup_link"].startswith("http")


def test_welcome_email_tells_them_what_to_do_next(app_client, admin_token, smtp):
    """账号是空的，信里必须说清楚接下来干什么，否则登进来一片空白就流失了。"""
    _create(app_client, admin_token, "guide@test.local")
    body = [x for x in smtp.sent if "to" in x][-1]["body"]
    for step in ("条款", "产品", "Telegram", "客户"):
        assert step in body, f"开通信没提到 {step}"


def test_no_smtp_config_degrades_quietly(app_client, admin_token, no_smtp):
    """没配 SMTP 时账号照常建，只是告诉管理员没发出去。"""
    r = _create(app_client, admin_token, "nosmtp@test.local")
    assert r.status_code == 200
    assert r.json()["email_sent"] is False
    assert r.json()["email_configured"] is False
    assert app_client.post("/api/auth/login",
                           json={"email": "nosmtp@test.local",
                                 "password": "Welcome-2026x"}).status_code == 200


def test_smtp_failure_never_fails_account_creation(app_client, admin_token, monkeypatch):
    """SMTP 炸了，账号必须照样建成功 —— 这是这个功能最重要的一条。"""
    import email_out

    def boom(*a, **kw):
        raise OSError("SMTP 连不上")
    monkeypatch.setattr(email_out.smtplib, "SMTP", boom)
    monkeypatch.setattr(email_out, "HOST", "smtp.example.com")
    monkeypatch.setattr(email_out, "FROM", "bot@hivora.my")

    r = _create(app_client, admin_token, "smtpdown@test.local")
    assert r.status_code == 200 and r.json()["email_sent"] is False
    # 账号真的建出来了，能登录
    assert app_client.post("/api/auth/login",
                           json={"email": "smtpdown@test.local",
                                 "password": "Welcome-2026x"}).status_code == 200


def test_ssl_port_uses_smtp_ssl(app_client, admin_token, smtp, monkeypatch):
    import email_out
    monkeypatch.setattr(email_out, "PORT", 465)
    assert _create(app_client, admin_token, "ssl@test.local").json()["email_sent"] is True


# ── HTTP 通道（Resend）──────────────────────────────────────────
# 存在的理由是一次真实事故：Render 免费档从 2025-09 起封了出站 25/465/587，
# SMTP 在 connect 就超时。HTTP API 走 443，不受影响。
def test_resend_is_used_when_the_key_is_set(app_client, admin_token, resend):
    r = _create(app_client, admin_token, "viahttp@test.local")
    assert r.status_code == 200 and r.json()["email_sent"] is True

    call = resend.calls[-1]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["method"] == "POST"
    assert call["headers"]["authorization"] == "Bearer re_test_key_123"
    assert call["json"]["to"] == ["viahttp@test.local"]
    assert call["json"]["from"] == "Hivora <onboarding@resend.dev>"
    assert "?setup=" in call["json"]["text"]
    assert "Welcome-2026x" not in call["json"]["text"], "密码不能进邮件"


def test_http_channel_works_with_no_smtp_at_all(resend):
    """免费档上根本连不上 SMTP —— 只有 key 也必须算「配好了」。"""
    import email_out
    assert email_out.HOST == ""
    assert email_out.provider() == "resend"
    assert email_out.configured() is True


def test_resend_takes_precedence_over_smtp(app_client, admin_token, smtp, monkeypatch,
                                           resend):
    """两条都配了走 HTTP —— SMTP 是退路，不是首选。"""
    monkeypatch.setattr(__import__("email_out"), "HOST", "smtp.example.com")
    before = len(smtp.mails)
    assert _create(app_client, admin_token, "both@test.local").json()["email_sent"] is True
    assert len(resend.calls) == 1
    assert len(smtp.mails) == before, "配了 key 还走 SMTP"


def test_resend_rejection_never_fails_account_creation(app_client, admin_token,
                                                       monkeypatch, caplog):
    """服务商返回 4xx —— 账号照样建成功，原话记进日志好排查。"""
    import urllib.error

    import email_out

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 422, "Unprocessable", {},
            __import__("io").BytesIO(b'{"message":"The from address is not verified"}'))

    monkeypatch.setattr(email_out.urllib.request, "urlopen", boom)
    monkeypatch.setattr(email_out, "RESEND_KEY", "re_bad_key")
    monkeypatch.setattr(email_out, "FROM", "noreply@unverified.test")
    monkeypatch.setattr(email_out, "HOST", "")

    with caplog.at_level("WARNING"):
        r = _create(app_client, admin_token, "rejected@test.local",
                    password="Sup3r-Secret-Pw")
    assert r.status_code == 200 and r.json()["email_sent"] is False
    assert "not verified" in caplog.text, "服务商的原话得留下来，否则没法查"
    assert "re_bad_key" not in caplog.text, "API key 不能进日志"
    assert "Sup3r-Secret-Pw" not in caplog.text
    # 账号是真建出来了
    assert app_client.post("/api/auth/login",
                           json={"email": "rejected@test.local",
                                 "password": "Sup3r-Secret-Pw"}).status_code == 200


def test_resend_network_failure_is_swallowed(app_client, admin_token, monkeypatch):
    import email_out

    def boom(req, timeout=None):
        raise OSError("connection reset")
    monkeypatch.setattr(email_out.urllib.request, "urlopen", boom)
    monkeypatch.setattr(email_out, "RESEND_KEY", "re_x")
    monkeypatch.setattr(email_out, "FROM", "a@b.test")
    monkeypatch.setattr(email_out, "HOST", "")
    assert _create(app_client, admin_token,
                   "netdown@test.local").json()["email_sent"] is False


def test_settings_reports_which_channel_is_live(app_client, admin_token, resend):
    j = app_client.get("/api/admin/settings", headers=H(admin_token)).json()
    assert j["email_configured"] is True and j["email_provider"] == "resend"


def test_settings_reports_smtp_when_that_is_the_channel(app_client, admin_token, smtp):
    j = app_client.get("/api/admin/settings", headers=H(admin_token)).json()
    assert j["email_provider"] == "smtp"


def test_test_email_goes_through_the_http_channel(app_client, admin_token, resend):
    r = app_client.post("/api/admin/email/test", headers=H(admin_token),
                        json={"to": "me@test.local"})
    assert r.status_code == 200
    assert resend.calls[-1]["json"]["to"] == ["me@test.local"]


def test_test_email_failure_points_at_the_right_channel(app_client, admin_token,
                                                        monkeypatch):
    import email_out
    monkeypatch.setattr(email_out.urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(email_out, "RESEND_KEY", "re_x")
    monkeypatch.setattr(email_out, "FROM", "a@b.test")
    monkeypatch.setattr(email_out, "HOST", "")
    r = app_client.post("/api/admin/email/test", headers=H(admin_token),
                        json={"to": "me@test.local"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "RESEND_API_KEY" in detail and "SMTP_HOST" not in detail


# ── 重置密码 ──────────────────────────────────────────────────
def test_password_reset_notifies_the_owner(app_client, admin_token, agent_factory, smtp):
    _, email = agent_factory()
    aid = next(a["id"] for a in app_client.get("/api/admin/agents",
                                               headers=H(admin_token)).json()
               if a["email"] == email)
    r = app_client.post(f"/api/admin/agents/{aid}/password", headers=H(admin_token),
                        json={})
    assert r.json()["email_sent"] is True
    mail = [x for x in smtp.sent if "to" in x][-1]
    assert mail["to"] == email and "?setup=" in mail["body"]


def test_password_reset_can_skip_the_email(app_client, admin_token, agent_factory, smtp):
    _, email = agent_factory()
    aid = next(a["id"] for a in app_client.get("/api/admin/agents",
                                               headers=H(admin_token)).json()
               if a["email"] == email)
    before = len([x for x in smtp.sent if "to" in x])
    r = app_client.post(f"/api/admin/agents/{aid}/password", headers=H(admin_token),
                        json={"password": "Quiet-Reset-2026", "notify": False})
    assert r.json()["email_sent"] is False
    assert len([x for x in smtp.sent if "to" in x]) == before


# ── 不泄密 ────────────────────────────────────────────────────
def test_password_never_reaches_the_logs(app_client, admin_token, monkeypatch, caplog):
    """发信失败时只能记收件人和异常类型，绝不能把正文（含密码）写进日志。"""
    import email_out

    def boom(*a, **kw):
        raise OSError("nope")
    monkeypatch.setattr(email_out.smtplib, "SMTP", boom)
    monkeypatch.setattr(email_out, "HOST", "smtp.example.com")
    monkeypatch.setattr(email_out, "FROM", "bot@hivora.my")

    with caplog.at_level("DEBUG"):
        _create(app_client, admin_token, "secret@test.local", password="Sup3r-Secret-Pw")
    assert "Sup3r-Secret-Pw" not in caplog.text


def test_audit_records_whether_the_mail_went_out(app_client, admin_token, smtp):
    import db
    _create(app_client, admin_token, "audited@test.local")
    s = db.SessionLocal()
    try:
        row = (s.query(db.Audit).filter_by(action="welcome_email")
               .order_by(db.Audit.id.desc()).first())
        assert row and "audited@test.local" in row.detail and "sent" in row.detail
        assert "Welcome-2026x" not in row.detail, "审计日志里不能有密码"
    finally:
        s.close()


# ── 管理站需要的能力开关 ──────────────────────────────────────
def test_settings_tells_the_admin_console_what_is_configured(app_client, admin_token,
                                                             no_smtp):
    j = app_client.get("/api/admin/settings", headers=H(admin_token)).json()
    assert j["email_configured"] is False
    assert j["login_url"].startswith("http")
    assert "version" in j


def test_settings_requires_admin(app_client, agent_factory):
    tok, _ = agent_factory()
    assert app_client.get("/api/admin/settings", headers=H(tok)).status_code == 403


# ── 测试邮件（配完 SMTP 用来验）────────────────────────────────
def test_admin_can_send_a_test_email(app_client, admin_token, smtp):
    r = app_client.post("/api/admin/email/test", headers=H(admin_token),
                        json={"to": "me@test.local"})
    assert r.status_code == 200
    mail = [x for x in smtp.sent if "to" in x][-1]
    assert mail["to"] == "me@test.local" and "配置测试" in mail["subject"]


def test_test_email_without_smtp_says_so(app_client, admin_token, no_smtp):
    r = app_client.post("/api/admin/email/test", headers=H(admin_token),
                        json={"to": "me@test.local"})
    assert r.status_code == 400 and "SMTP" in r.json()["detail"]


def test_test_email_reports_send_failure(app_client, admin_token, monkeypatch):
    import email_out
    monkeypatch.setattr(email_out, "HOST", "smtp.example.com")
    monkeypatch.setattr(email_out, "FROM", "bot@hivora.my")
    monkeypatch.setattr(email_out.smtplib, "SMTP",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("refused")))
    r = app_client.post("/api/admin/email/test", headers=H(admin_token),
                        json={"to": "me@test.local"})
    assert r.status_code == 502 and "SMTP_HOST" in r.json()["detail"]


def test_test_email_requires_admin(app_client, agent_factory):
    tok, _ = agent_factory()
    assert app_client.post("/api/admin/email/test", headers=H(tok),
                           json={"to": "x@y.z"}).status_code == 403


# ── 设密码链接 ────────────────────────────────────────────────
def _setup_link_token(app_client, admin_token, email):
    r = app_client.post("/api/admin/agents/create", headers=H(admin_token),
                        json={"email": email, "name": "新公司"})
    assert r.status_code == 200
    return r.json()["setup_link"].split("?setup=")[1]


def test_account_created_without_a_password_cannot_be_logged_into(app_client,
                                                                  admin_token, no_smtp):
    """不给密码时账号先不可登录 —— 等对方点链接自己设。"""
    _setup_link_token(app_client, admin_token, "pending@test.local")
    r = app_client.post("/api/auth/login",
                        json={"email": "pending@test.local", "password": ""})
    assert r.status_code in (401, 429)


def test_setup_link_lets_the_owner_choose_a_password(app_client, admin_token, no_smtp):
    tok = _setup_link_token(app_client, admin_token, "choose@test.local")

    who = app_client.post("/api/auth/setup/check", json={"token": tok}).json()
    assert who["email"] == "choose@test.local" and who["kind"] == "welcome"

    r = app_client.post("/api/auth/setup",
                        json={"token": tok, "password": "My-Own-Pass-2026"})
    assert r.status_code == 200 and r.json()["token"], "设完密码应该直接登录"

    assert app_client.post("/api/auth/login",
                           json={"email": "choose@test.local",
                                 "password": "My-Own-Pass-2026"}).status_code == 200


def test_setup_link_is_single_use(app_client, admin_token, no_smtp):
    tok = _setup_link_token(app_client, admin_token, "once@test.local")
    assert app_client.post("/api/auth/setup",
                           json={"token": tok, "password": "First-Pass-2026"}
                           ).status_code == 200
    r = app_client.post("/api/auth/setup",
                        json={"token": tok, "password": "Second-Pass-2026"})
    assert r.status_code == 400
    # 第二次没生效，第一次设的还能用
    assert app_client.post("/api/auth/login",
                           json={"email": "once@test.local",
                                 "password": "First-Pass-2026"}).status_code == 200


def test_expired_setup_link_is_refused(app_client, admin_token, no_smtp):
    import db
    tok = _setup_link_token(app_client, admin_token, "stale@test.local")
    s = db.SessionLocal()
    try:
        s.query(db.SetupToken).filter_by(token=tok).first().expires = 0
        s.commit()
    finally:
        s.close()
    assert app_client.post("/api/auth/setup/check",
                           json={"token": tok}).status_code == 400
    assert app_client.post("/api/auth/setup",
                           json={"token": tok, "password": "Too-Late-2026"}
                           ).status_code == 400


def test_bogus_setup_token_is_refused(app_client):
    assert app_client.post("/api/auth/setup/check",
                           json={"token": "made-up"}).status_code == 400


def test_setup_rejects_a_short_password(app_client, admin_token, no_smtp):
    tok = _setup_link_token(app_client, admin_token, "shortpw@test.local")
    assert app_client.post("/api/auth/setup",
                           json={"token": tok, "password": "1234567"}
                           ).status_code == 400


def test_issuing_a_new_link_invalidates_the_old_one(app_client, admin_token, no_smtp):
    """重发链接后，旧链接必须立刻失效。"""
    old = _setup_link_token(app_client, admin_token, "reissue@test.local")
    aid = next(a["id"] for a in app_client.get("/api/admin/agents",
                                               headers=H(admin_token)).json()
               if a["email"] == "reissue@test.local")
    new = (app_client.post(f"/api/admin/agents/{aid}/password", headers=H(admin_token),
                           json={}).json()["setup_link"].split("?setup=")[1])
    assert new != old
    assert app_client.post("/api/auth/setup",
                           json={"token": old, "password": "Old-Link-2026"}
                           ).status_code == 400
    assert app_client.post("/api/auth/setup",
                           json={"token": new, "password": "New-Link-2026"}
                           ).status_code == 200


# ── 本人改密码 ────────────────────────────────────────────────
def test_owner_can_change_their_own_password(app_client, agent_factory):
    tok, email = agent_factory()
    r = app_client.post("/api/password", headers=H(tok),
                        json={"old_password": "Agent-Pass-2026",
                              "new_password": "Chosen-By-Me-2026"})
    assert r.status_code == 200 and r.json()["token"]
    assert app_client.post("/api/auth/login",
                           json={"email": email, "password": "Chosen-By-Me-2026"}
                           ).status_code == 200
    assert app_client.post("/api/auth/login",
                           json={"email": email, "password": "Agent-Pass-2026"}
                           ).status_code == 401


def test_changing_password_requires_the_current_one(app_client, agent_factory):
    """光有 token 不够 —— 否则 token 被偷就等于账号被永久夺走。"""
    tok, email = agent_factory()
    r = app_client.post("/api/password", headers=H(tok),
                        json={"old_password": "猜的", "new_password": "Hijacked-2026"})
    assert r.status_code == 400
    assert app_client.post("/api/auth/login",
                           json={"email": email, "password": "Agent-Pass-2026"}
                           ).status_code == 200


def test_changing_password_invalidates_pending_setup_links(app_client, admin_token,
                                                           agent_factory, no_smtp):
    """自己改完密码，管理员之前发的链接就不该还能用。"""
    import db
    tok, email = agent_factory()
    aid = next(a["id"] for a in app_client.get("/api/admin/agents",
                                               headers=H(admin_token)).json()
               if a["email"] == email)
    link = app_client.post(f"/api/admin/agents/{aid}/password", headers=H(admin_token),
                           json={}).json()["setup_link"]
    token = link.split("?setup=")[1]

    app_client.post("/api/password", headers=H(tok),
                    json={"old_password": "Agent-Pass-2026",
                          "new_password": "Mine-Now-2026"})
    assert app_client.post("/api/auth/setup",
                           json={"token": token, "password": "Stale-Link-2026"}
                           ).status_code == 400


def test_change_password_rejects_a_short_one(app_client, agent_factory):
    tok, _ = agent_factory()
    assert app_client.post("/api/password", headers=H(tok),
                           json={"old_password": "Agent-Pass-2026",
                                 "new_password": "1234567"}).status_code == 400
