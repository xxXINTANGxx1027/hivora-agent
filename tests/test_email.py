"""开通邮件。全程不发真信 —— smtplib 被替换成假的。

最要紧的一条：**发信失败绝不能让建账号失败**。账号已经建好了，
邮件只是通知手段；发不出去时管理站会把凭据显示出来让管理员手动发。
"""
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
    assert "Welcome-2026x" in mail["body"], "信里得有密码，不然收信人登不进去"
    assert "newbie@test.local" in mail["body"]


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


# ── 重置密码 ──────────────────────────────────────────────────
def test_password_reset_notifies_the_owner(app_client, admin_token, agent_factory, smtp):
    _, email = agent_factory()
    aid = next(a["id"] for a in app_client.get("/api/admin/agents",
                                               headers=H(admin_token)).json()
               if a["email"] == email)
    r = app_client.post(f"/api/admin/agents/{aid}/password", headers=H(admin_token),
                        json={"password": "Reset-Me-2026"})
    assert r.json()["email_sent"] is True
    mail = [x for x in smtp.sent if "to" in x][-1]
    assert mail["to"] == email and "Reset-Me-2026" in mail["body"]


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
