"""V0.1 管理站：重置密码、套餐到期、审计日志、PDPA，以及前后台的分离。"""
import pathlib

from conftest import H

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _agent_id(client, admin_token, email):
    return next(a["id"] for a in client.get("/api/admin/agents",
                                            headers=H(admin_token)).json()
                if a["email"] == email)


# ── 重置密码 ──────────────────────────────────────────────────
def test_admin_resets_forgotten_password(app_client, admin_token, agent_factory):
    """代理人忘密码时唯一的自救途径。以前根本没有这个接口。"""
    _, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)

    r = app_client.post(f"/api/admin/agents/{aid}/password",
                        headers=H(admin_token), json={"password": "Brand-New-2026"})
    assert r.status_code == 200

    assert app_client.post("/api/auth/login",
                           json={"email": email, "password": "Agent-Pass-2026"}
                           ).status_code == 401
    assert app_client.post("/api/auth/login",
                           json={"email": email, "password": "Brand-New-2026"}
                           ).status_code == 200


def test_reset_rejects_short_password(app_client, admin_token, agent_factory):
    _, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    assert app_client.post(f"/api/admin/agents/{aid}/password",
                           headers=H(admin_token), json={"password": "1234567"}
                           ).status_code == 400


def test_reset_requires_admin(app_client, admin_token, agent_factory):
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    assert app_client.post(f"/api/admin/agents/{aid}/password",
                           headers=H(tok), json={"password": "Whatever-2026"}
                           ).status_code == 403


# ── 套餐与到期日 ──────────────────────────────────────────────
def test_expiry_locks_account_without_manual_toggle(app_client, admin_token, agent_factory):
    """到期日一到，登录和已签发的 token 都失效——不用你记着手动停用。"""
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 200

    app_client.post(f"/api/admin/agents/{aid}/plan", headers=H(admin_token),
                    json={"plan": "paid", "expires": "2020-01-01"})

    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 403
    assert app_client.post("/api/auth/login",
                           json={"email": email, "password": "Agent-Pass-2026"}
                           ).status_code == 403


def test_expiry_can_be_cleared(app_client, admin_token, agent_factory):
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    app_client.post(f"/api/admin/agents/{aid}/plan", headers=H(admin_token),
                    json={"expires": "2020-01-01"})
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 403
    app_client.post(f"/api/admin/agents/{aid}/plan", headers=H(admin_token),
                    json={"expires": ""})
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 200


def test_bad_expiry_format_rejected(app_client, admin_token, agent_factory):
    _, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    assert app_client.post(f"/api/admin/agents/{aid}/plan", headers=H(admin_token),
                           json={"expires": "下周三"}).status_code == 400


# ── 审计日志 ──────────────────────────────────────────────────
def test_audit_shows_who_did_what(app_client, admin_token, agent_factory):
    import db
    tok, email = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "审计对象"})
    s = db.SessionLocal()
    try:
        key = s.query(db.Agent).filter_by(email=email).first().agent_key
    finally:
        s.close()

    rows = app_client.get(f"/api/admin/audit?agent_key={key}",
                          headers=H(admin_token)).json()
    actions = {r["action"] for r in rows}
    assert "add_client" in actions
    assert all(r["agent_key"] == key for r in rows)
    assert any(r["agent"] == email for r in rows)   # 解析成了可读的邮箱


def test_audit_filters_by_action(app_client, admin_token):
    rows = app_client.get("/api/admin/audit?action=login&limit=50",
                          headers=H(admin_token)).json()
    assert rows and {r["action"] for r in rows} == {"login"}


def test_audit_requires_admin(app_client, agent_factory):
    tok, _ = agent_factory()
    assert app_client.get("/api/admin/audit", headers=H(tok)).status_code == 403


# ── PDPA ─────────────────────────────────────────────────────
def test_admin_lists_clients_of_one_agent(app_client, admin_token, agent_factory):
    import db
    tok, email = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "PDPA 对象"})
    s = db.SessionLocal()
    try:
        key = s.query(db.Agent).filter_by(email=email).first().agent_key
    finally:
        s.close()
    rows = app_client.get(f"/api/admin/clients?agent_key={key}",
                          headers=H(admin_token)).json()
    assert [r["name"] for r in rows] == ["PDPA 对象"]


def test_admin_client_list_requires_admin(app_client, agent_factory):
    tok, _ = agent_factory()
    assert app_client.get("/api/admin/clients?agent_key=x",
                          headers=H(tok)).status_code == 403


def test_agent_list_reports_client_counts(app_client, admin_token, agent_factory):
    tok, email = agent_factory()
    for n in ("A", "B"):
        app_client.post("/api/clients", headers=H(tok), json={"name": n})
    row = next(a for a in app_client.get("/api/admin/agents",
                                         headers=H(admin_token)).json()
               if a["email"] == email)
    assert row["clients"] == 2 and row["agent_key"]


# ── closed SaaS：没有自助注册，也没有邀请码 ────────────────────
def test_self_registration_is_gone(app_client):
    r = app_client.post("/api/auth/register",
                        json={"email": "walkin@test.local", "password": "Sneaky-2026"})
    assert r.status_code == 404


def test_invite_endpoints_are_gone(app_client, admin_token):
    assert app_client.get("/api/admin/invites", headers=H(admin_token)).status_code == 404
    assert app_client.post("/api/admin/invites", headers=H(admin_token)).status_code == 404


# ── 前后台分离的静态回归 ──────────────────────────────────────
def _read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_client_app_contains_no_admin_code():
    """客户拿到的包里不能有一行管理代码——这是拆站的全部意义。"""
    for path in (("server", "static", "index.html"), ("frontend", "index.html")):
        html = _read(*path)
        leaked = [k for k in ("api/admin", "loadAdmin", "makeInvite", "toggleAgent",
                              "hivora_role", "v-admin", "auth/register")
                  if k in html]
        assert not leaked, f"{'/'.join(path)} 里泄漏了管理代码：{leaked}"


def test_admin_app_escapes_and_is_noindex():
    html = _read("admin", "index.html")
    assert "const esc=" in html
    assert 'name="robots"' in html and "noindex" in html
    raw = ["${a.email}</td>", "${a.name}</td>", "${c.name}</td>", "${r.detail}</td>"]
    assert not [x for x in raw if x in html]


def test_both_apps_read_backend_from_meta():
    for path in (("server", "static", "index.html"), ("admin", "index.html")):
        html = _read(*path)
        assert '<meta name="hivora-api"' in html
        assert "onrender.com" not in html.split("</head>")[1], \
            f"{'/'.join(path)} 的 body 里硬编码了后端地址"
