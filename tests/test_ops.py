"""可观测性、用量记账与配额、性能上限、限流落库 —— 「工程做到 A」这一批。"""
import pytest

from conftest import SERVER_DIR, WORKSPACE, H, needs_workspace


# ── A1 可观测性 ───────────────────────────────────────────────
def test_healthz_reports_version(app_client):
    j = app_client.get("/healthz").json()
    assert j["ok"] and j["version"]


def test_readyz_actually_touches_the_database(app_client):
    j = app_client.get("/readyz").json()
    assert j["ok"] is True and j["checks"]["db"] == "ok"
    assert "storage" in j["checks"]


def test_every_response_carries_a_request_id(app_client):
    r = app_client.get("/healthz")
    assert r.headers.get("X-Request-ID")
    # 客户端给了就沿用，方便把前后端日志串起来
    r2 = app_client.get("/healthz", headers={"X-Request-ID": "trace-me-123"})
    assert r2.headers["X-Request-ID"] == "trace-me-123"


def test_security_headers_present(app_client):
    h = app_client.get("/healthz").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"


def test_sentry_is_opt_in_only():
    """没设 DSN 时不能引入 Sentry —— 本地和 CI 不该被它影响。"""
    import os
    import main
    assert not os.environ.get("SENTRY_DSN")
    assert main.VERSION


# ── A2 用量与配额 ─────────────────────────────────────────────
def test_usage_is_recorded_per_agent(app_client, agent_factory):
    import db
    tok, email = agent_factory()
    s = db.SessionLocal()
    try:
        key = s.query(db.Agent).filter_by(email=email).first().agent_key
    finally:
        s.close()

    db.record_usage(key, "test-model", 1000, 500)
    db.record_usage(key, "test-model", 200, 100)
    assert db.month_tokens(key) == 1800

    j = app_client.get("/api/usage", headers=H(tok)).json()
    assert j["tokens"] == 1800 and j["limit"] == db.MONTHLY_TOKEN_QUOTA


def test_usage_is_isolated_between_agents(agent_factory):
    import db
    _, e1 = agent_factory()
    _, e2 = agent_factory()
    s = db.SessionLocal()
    try:
        k1 = s.query(db.Agent).filter_by(email=e1).first().agent_key
        k2 = s.query(db.Agent).filter_by(email=e2).first().agent_key
    finally:
        s.close()
    db.record_usage(k1, "m", 999, 1)
    assert db.month_tokens(k2) == 0


def test_quota_blocks_the_llm_call(monkeypatch, agent_factory):
    """超额必须在调模型之前就拦住——不然还是要花钱。"""
    import db
    import graph
    _, email = agent_factory()
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(email=email).first()
        a.token_quota = 100
        key = a.agent_key
        s.commit()
    finally:
        s.close()
    db.record_usage(key, "m", 90, 20)      # 110 > 100

    called = []

    class FakeLLM:                       # ChatOpenAI 是 pydantic 模型，改不了属性
        def invoke(self, prompt):
            called.append(prompt)
            raise AssertionError("不该走到这里")
    monkeypatch.setattr(graph, "llm", FakeLLM())
    with pytest.raises(graph.QuotaExceeded):
        graph.llm_text("会不会被拦住", key)
    assert not called, "超额了还是调了模型"


def test_unlimited_quota_never_blocks(agent_factory):
    import db
    import graph
    _, email = agent_factory()
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(email=email).first()
        a.token_quota = -1
        key = a.agent_key
        s.commit()
    finally:
        s.close()
    db.record_usage(key, "m", 10 ** 9, 0)
    graph._check_quota(key)      # 不该抛


def test_quota_exceeded_surfaces_as_429(app_client, agent_factory, monkeypatch):
    import db
    import graph
    tok, email = agent_factory()
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(email=email).first()
        a.token_quota = 1
        key = a.agent_key
        s.commit()
    finally:
        s.close()
    db.record_usage(key, "m", 5, 5)
    r = app_client.post("/api/chat", headers=H(tok), json={"message": "你好"})
    assert r.status_code == 429
    assert "上限" in r.json()["detail"]


def test_admin_usage_rollup(app_client, admin_token, agent_factory):
    import db
    _, email = agent_factory()
    s = db.SessionLocal()
    try:
        key = s.query(db.Agent).filter_by(email=email).first().agent_key
    finally:
        s.close()
    db.record_usage(key, "m", 3000, 2000)

    j = app_client.get("/api/admin/usage", headers=H(admin_token)).json()
    row = next(r for r in j["agents"] if r["agent_key"] == key)
    assert row["tokens"] == 5000 and row["calls"] == 1
    assert row["cost_usd"] == db.cost_usd(3000, 2000) > 0
    assert j["total_tokens"] >= 5000


def test_admin_can_set_quota(app_client, admin_token, agent_factory):
    _, email = agent_factory()
    aid = next(a["id"] for a in app_client.get("/api/admin/agents",
                                               headers=H(admin_token)).json()
               if a["email"] == email)
    r = app_client.post(f"/api/admin/agents/{aid}/quota",
                        headers=H(admin_token), json={"token_quota": 12345})
    assert r.json()["token_quota"] == 12345
    row = next(a for a in app_client.get("/api/admin/agents",
                                         headers=H(admin_token)).json()
               if a["email"] == email)
    assert row["token_quota"] == 12345


def test_usage_endpoints_need_the_right_role(app_client, agent_factory):
    tok, _ = agent_factory()
    assert app_client.get("/api/usage", headers=H(tok)).status_code == 200
    assert app_client.get("/api/admin/usage", headers=H(tok)).status_code == 403


def test_accounting_failure_never_breaks_the_request(monkeypatch):
    """记账挂了也不能让用户的请求失败——它是附带动作，不是主流程。"""
    import db
    import graph

    def boom(*a, **kw):
        raise RuntimeError("模拟数据库炸了")
    monkeypatch.setattr(db, "SessionLocal", boom)
    db.record_usage("ag_x", "m", 10, 5)          # 不该抛

    class FakeMsg:
        content = "答案"
        usage_metadata = {"input_tokens": 10, "output_tokens": 5}

    class FakeLLM:
        def invoke(self, prompt):
            return FakeMsg()
    monkeypatch.setattr(graph, "llm", FakeLLM())
    monkeypatch.setattr(graph, "_check_quota", lambda k: None)
    assert graph.llm_text("问题", "ag_x") == "答案"


# ── A3 列表上限 ───────────────────────────────────────────────
def test_lists_are_bounded(app_client, agent_factory, monkeypatch):
    """返回条数必须有上限，且截断时要留日志，不能静默丢数据。"""
    import main
    monkeypatch.setattr(main, "LIST_CAP", 3)
    tok, _ = agent_factory()
    for i in range(5):
        app_client.post("/api/clients", headers=H(tok), json={"name": f"客户{i}"})
    assert len(app_client.get("/api/clients", headers=H(tok)).json()) == 3


def test_dashboard_counts_are_correct_after_the_sql_rewrite(app_client, agent_factory):
    """改成 SQL 聚合之后，数字必须还是对的。"""
    import datetime as dt
    tok, _ = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "统计客户"})
    cid = app_client.get("/api/clients", headers=H(tok)).json()[0]["id"]
    soon = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    far = (dt.date.today() + dt.timedelta(days=200)).isoformat()
    app_client.post("/api/policies", headers=H(tok),
                    json={"client_id": cid, "product": "快到期", "renewal": soon})
    app_client.post("/api/policies", headers=H(tok),
                    json={"client_id": cid, "product": "还早", "renewal": far})
    app_client.post("/api/appointments", headers=H(tok),
                    json={"client": "统计客户",
                          "date": (dt.date.today() + dt.timedelta(days=2)).isoformat()})

    d = app_client.get("/api/dashboard", headers=H(tok)).json()
    assert d["clients"] == 1
    assert d["policies"] == 2
    assert d["renewals_30d"] == 1      # 只有 10 天后那张
    assert d["appts_7d"] == 1


def test_soft_deleted_rows_stay_out_of_the_aggregates(app_client, agent_factory):
    import datetime as dt
    tok, _ = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "会被删"})
    cid = app_client.get("/api/clients", headers=H(tok)).json()[0]["id"]
    app_client.post("/api/policies", headers=H(tok),
                    json={"client_id": cid, "product": "P",
                          "renewal": (dt.date.today() + dt.timedelta(days=5)).isoformat()})
    assert app_client.get("/api/dashboard", headers=H(tok)).json()["renewals_30d"] == 1
    app_client.post("/api/delete", headers=H(tok), json={"kind": "client", "id": cid})
    d = app_client.get("/api/dashboard", headers=H(tok)).json()
    assert d["clients"] == 0 and d["policies"] == 0 and d["renewals_30d"] == 0


# ── A4 限流落库 ───────────────────────────────────────────────
def test_login_lock_lives_in_the_database(app_client, agent_factory):
    """进程内的字典扩到第二个实例就失效，所以必须落库。"""
    import db
    _, email = agent_factory()
    for _ in range(6):
        app_client.post("/api/auth/login", json={"email": email, "password": "wrong"})
    s = db.SessionLocal()
    try:
        row = s.query(db.LoginLock).filter_by(email=email).first()
        assert row is not None and row.fails >= 5
    finally:
        s.close()


def test_successful_login_clears_the_lock(app_client, agent_factory):
    import db
    _, email = agent_factory()
    for _ in range(2):
        app_client.post("/api/auth/login", json={"email": email, "password": "wrong"})
    assert app_client.post("/api/auth/login",
                           json={"email": email, "password": "Agent-Pass-2026"}
                           ).status_code == 200
    s = db.SessionLocal()
    try:
        assert s.query(db.LoginLock).filter_by(email=email).first() is None
    finally:
        s.close()


# ── A5/A6 运维资产存在性 ──────────────────────────────────────
def test_backup_script_exists():
    script = SERVER_DIR / "scripts" / "backup.sh"
    assert script.exists(), "没有备份脚本"
    body = script.read_text(encoding="utf-8")
    assert "pg_dump" in body
    assert "CREATE TABLE" in body, "备份没有做内容校验"
    assert (SERVER_DIR / ".github" / "workflows" / "backup.yml").exists()


@needs_workspace
def test_runbook_covers_the_incidents_we_have_actually_had():
    runbook = (WORKSPACE / "RUNBOOK.md").read_text(encoding="utf-8")
    for topic in ("readyz", "回滚", "恢复", "CORS", "PDPA", "SMTP"):
        assert topic in runbook, f"RUNBOOK 没写 {topic}"


# ── 部署工具链 ────────────────────────────────────────────────
@needs_workspace
def test_build_fingerprint_is_stable_and_api_independent():
    """指纹必须跟填哪个后端地址无关，否则没法拿本地的去比对线上的。"""
    import subprocess
    src = SERVER_DIR / "static" / "index.html"
    run = lambda *a: subprocess.run(["python3", str(WORKSPACE / "stamp.py"), str(src), *a],
                                    capture_output=True, text=True, check=True).stdout

    want = run("--build-id").strip()
    assert len(want) == 12

    import re
    for api in ("", "https://a.example.com", "https://b.example.com"):
        got = re.search(r'hivora-build" content="([^"]*)"', run(api)).group(1)
        assert got == want, f"换了 API_BASE 指纹就变了：{api}"


@needs_workspace
def test_deployed_frontend_can_be_verified():
    """frontend/index.html 里的指纹要跟源文件算出来的一致 —— push.sh 靠这个验部署。"""
    import re
    import subprocess
    want = subprocess.run(
        ["python3", str(WORKSPACE / "stamp.py"),
         str(SERVER_DIR / "static" / "index.html"), "--build-id"],
        capture_output=True, text=True, check=True).stdout.strip()
    dest = (WORKSPACE / "frontend" / "index.html").read_text(encoding="utf-8")
    got = re.search(r'hivora-build" content="([^"]*)"', dest).group(1)
    assert got == want, "frontend 没同步，跑 ./sync-frontend.sh"


@needs_workspace
def test_push_script_exists_and_is_runnable():
    p = WORKSPACE / "push.sh"
    assert p.exists() and p.stat().st_mode & 0o111, "push.sh 不存在或没有执行权限"
    body = p.read_text(encoding="utf-8")
    assert "--status" in body
    assert "sync-frontend.sh" in body and "--check" in body, "push.sh 应该校验前端同步"
    assert "pytest" in body, "push.sh 应该在推之前跑测试"
    assert "read -r -p" in body, "推生产之前应该要确认一次"


# ── 开通引导 ──────────────────────────────────────────────────
def test_new_account_sees_four_unfinished_steps(app_client, agent_factory):
    tok, _ = agent_factory()
    o = app_client.get("/api/onboarding", headers=H(tok)).json()
    assert [s["key"] for s in o["steps"]] == ["docs", "products", "telegram", "clients"]
    assert all(s["done"] is False for s in o["steps"])
    assert o["done"] is False


def test_steps_tick_off_as_the_agent_sets_up(app_client, agent_factory):
    tok, _ = agent_factory()
    step = lambda k: next(s for s in app_client.get("/api/onboarding", headers=H(tok))
                          .json()["steps"] if s["key"] == k)

    app_client.post("/api/clients", headers=H(tok), json={"name": "第一个客户"})
    assert step("clients")["done"] is True and step("clients")["count"] == 1
    assert step("products")["done"] is False

    app_client.post("/api/products", headers=H(tok), json={"name": "MediShield"})
    assert step("products")["done"] is True

    app_client.post("/api/documents", headers=H(tok),
                    files={"file": ("terms.txt", "条款内容" * 20, "text/plain")})
    assert step("docs")["done"] is True

    o = app_client.get("/api/onboarding", headers=H(tok)).json()
    assert o["done"] is False, "还没连 Telegram 就不该算完成"


def test_onboarding_disappears_once_everything_is_done(app_client, agent_factory,
                                                       monkeypatch):
    import telegram
    monkeypatch.setattr(telegram, "call",
                        lambda token, method, payload=None:
                            {"username": "bot"} if method == "getMe" else {})
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    tok, _ = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "客户"})
    app_client.post("/api/products", headers=H(tok), json={"name": "产品"})
    app_client.post("/api/documents", headers=H(tok),
                    files={"file": ("t.txt", "条款" * 30, "text/plain")})
    app_client.post("/api/telegram/connect", headers=H(tok), json={"token": "111:AAA"})

    assert app_client.get("/api/onboarding", headers=H(tok)).json()["done"] is True


def test_onboarding_is_per_agent(app_client, agent_factory):
    a, _ = agent_factory()
    b, _ = agent_factory()
    app_client.post("/api/clients", headers=H(a), json={"name": "A 的客户"})
    step_b = next(s for s in app_client.get("/api/onboarding", headers=H(b))
                  .json()["steps"] if s["key"] == "clients")
    assert step_b["done"] is False


def test_deleted_data_reopens_the_step(app_client, agent_factory):
    """把唯一的客户删掉，这一步该退回未完成 —— 引导要反映真实状态。"""
    tok, _ = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "会被删"})
    cid = app_client.get("/api/clients", headers=H(tok)).json()[0]["id"]
    app_client.post("/api/delete", headers=H(tok), json={"kind": "client", "id": cid})
    step = next(s for s in app_client.get("/api/onboarding", headers=H(tok))
                .json()["steps"] if s["key"] == "clients")
    assert step["done"] is False
