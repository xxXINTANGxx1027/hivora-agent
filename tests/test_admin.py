"""V0.1 管理站：重置密码、套餐到期、审计日志、PDPA，以及前后台的分离。"""
from conftest import SERVER_DIR, WORKSPACE, H, needs_workspace


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
def _read(path):
    return path.read_text(encoding="utf-8")


CLIENT_APP = SERVER_DIR / "static" / "index.html"
CONSOLE_APP = SERVER_DIR / "static" / "console.html"
LEAKS = ("api/admin", "loadAdmin", "makeInvite", "toggleAgent",
         "hivora_role", "v-admin", "auth/register")


def test_client_app_contains_no_admin_code():
    """客户拿到的包里不能有一行管理代码——这是拆站的全部意义。"""
    leaked = [k for k in LEAKS if k in _read(CLIENT_APP)]
    assert not leaked, f"server/static/index.html 里泄漏了管理代码：{leaked}"


@needs_workspace
def test_the_copy_vercel_serves_has_no_admin_code_either():
    """frontend/ 是另一个 repo，只有在完整工作区里才看得到。"""
    leaked = [k for k in LEAKS if k in _read(WORKSPACE / "frontend" / "index.html")]
    assert not leaked, f"frontend/index.html 里泄漏了管理代码：{leaked}"


def test_admin_app_escapes_and_is_noindex():
    """查的是后端真正发出去的那份 console.html —— 它才是线上跑的东西。"""
    html = _read(CONSOLE_APP)
    assert "const esc=" in html
    assert 'name="robots"' in html and "noindex" in html
    raw = ["${a.email}</td>", "${a.name}</td>", "${c.name}</td>", "${r.detail}</td>"]
    assert not [x for x in raw if x in html]


def _strip_meta(html):
    """去掉两个由构建期写入的 meta，剩下的内容两份必须一模一样。"""
    import re
    for name in ("hivora-api", "hivora-build"):
        html = re.sub(f'(<meta name="{name}" content=")[^"]*(">)', r"\1\2", html)
    return html


def test_console_is_served_by_the_backend_itself():
    """管理站挂在后端同源路径上：不用单独的托管项目，也不吃 CORS。"""
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        r = c.get("/console")
        assert r.status_code == 200
        assert "noindex" in r.headers.get("x-robots-tag", "")
        assert "const esc=" in r.text          # 确实是管理站那份，不是客户端
        assert "api/admin" in r.text


@needs_workspace
def test_console_copy_stays_in_sync_with_admin_source():
    """admin/index.html 是唯一来源；改了不跑 sync-frontend.sh 就该红。"""
    assert _strip_meta(_read(CONSOLE_APP)) == \
           _strip_meta(_read(WORKSPACE / "admin" / "index.html"))


def test_console_calls_its_own_origin():
    """后端自带的这份后端地址必须留空 —— 否则同源的意义就没了。"""
    html = _read(CONSOLE_APP)
    assert '<meta name="hivora-api" content="">' in html
    assert "onrender.com" not in html


def test_both_apps_read_backend_from_meta():
    for path in (CLIENT_APP, CONSOLE_APP):
        html = _read(path)
        assert '<meta name="hivora-api"' in html
        assert "onrender.com" not in html.split("</head>")[1], \
            f"{path.name} 的 body 里硬编码了后端地址"


# ── 二次确认（服务端强制，不只靠前端弹窗）────────────────────
def test_purge_needs_the_client_name_typed_out(app_client, admin_token, agent_factory):
    """误调接口、脚本写错一样会删数据，所以确认要在服务端。"""
    import db
    tok, _ = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "不该被误删的人"})
    cid = app_client.get("/api/clients", headers=H(tok)).json()[0]["id"]

    for bad in ({}, {"confirm": ""}, {"confirm": "随便打的"}):
        r = app_client.post(f"/api/admin/clients/{cid}/purge",
                            headers=H(admin_token), json=bad)
        assert r.status_code == 400, bad
    # 人还在
    assert app_client.get("/api/clients", headers=H(tok)).json()[0]["id"] == cid

    r = app_client.post(f"/api/admin/clients/{cid}/purge", headers=H(admin_token),
                        json={"confirm": "不该被误删的人"})
    assert r.status_code == 200
    assert app_client.get("/api/clients", headers=H(tok)).json() == []


def test_disabling_an_account_needs_the_email_typed_out(app_client, admin_token,
                                                        agent_factory):
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)

    r = app_client.post(f"/api/admin/agents/{aid}/toggle", headers=H(admin_token),
                        json={"active": False})
    assert r.status_code == 400 and email in r.json()["detail"]
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 200

    r = app_client.post(f"/api/admin/agents/{aid}/toggle", headers=H(admin_token),
                        json={"active": False, "confirm": email.upper()})
    assert r.status_code == 200, "确认时大小写不该卡人"
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 403


def test_re_enabling_needs_no_confirmation(app_client, admin_token, agent_factory):
    """启用是无害操作，不该给管理员添麻烦。"""
    tok, email = agent_factory()
    aid = _agent_id(app_client, admin_token, email)
    app_client.post(f"/api/admin/agents/{aid}/toggle", headers=H(admin_token),
                    json={"active": False, "confirm": email})
    r = app_client.post(f"/api/admin/agents/{aid}/toggle", headers=H(admin_token),
                        json={"active": True})
    assert r.status_code == 200
    assert app_client.get("/api/dashboard", headers=H(tok)).status_code == 200


# ── 审计日志导出 ──────────────────────────────────────────────
def test_audit_exports_as_csv(app_client, admin_token, agent_factory):
    import db
    tok, email = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "导出测试"})
    s = db.SessionLocal()
    try:
        key = s.query(db.Agent).filter_by(email=email).first().agent_key
    finally:
        s.close()

    r = app_client.get(f"/api/admin/audit/export?agent_key={key}",
                       headers=H(admin_token))
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert ".csv" in r.headers["content-disposition"]

    body = r.content.decode("utf-8")
    assert body.startswith("﻿"), "少了 BOM，Excel 打开中文会乱码"
    lines = [l for l in body.splitlines() if l.strip()]
    assert "动作" in lines[0] and "agent_key" in lines[0]
    assert any("add_client" in l and "导出测试" in l for l in lines[1:])
    assert all(key in l for l in lines[1:]), "筛了账号却混进了别人的记录"


def test_audit_export_can_filter_by_action(app_client, admin_token):
    r = app_client.get("/api/admin/audit/export?action=login&limit=50",
                       headers=H(admin_token))
    rows = [l for l in r.content.decode("utf-8").splitlines()[1:] if l.strip()]
    assert rows and all(",login," in l for l in rows)


def test_audit_export_requires_admin(app_client, agent_factory):
    tok, _ = agent_factory()
    assert app_client.get("/api/admin/audit/export", headers=H(tok)).status_code == 403
