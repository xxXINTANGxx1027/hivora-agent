"""多租户隔离 + 审计日志。违反这两条 = PDPA 事故（AGENTS.md 铁律 1 和 4）。"""
from conftest import H


def test_clients_isolated(app_client, agent_factory):
    a, _ = agent_factory()
    b, _ = agent_factory()
    app_client.post("/api/clients", headers=H(a), json={"name": "A 的客户"})
    assert [c["name"] for c in app_client.get("/api/clients", headers=H(b)).json()] == []


def test_products_isolated(app_client, agent_factory, demo_token):
    """产品目录曾经是全局共享的——任何人加的产品所有租户都看得到。"""
    a, _ = agent_factory()
    app_client.post("/api/products", headers=H(a), json={"name": "A 专属产品"})
    b, _ = agent_factory()
    assert app_client.get("/api/products", headers=H(b)).json() == []
    assert [p["name"] for p in app_client.get("/api/products", headers=H(a)).json()] \
        == ["A 专属产品"]
    # 演示种子产品只属于演示账号
    demo = [p["name"] for p in app_client.get("/api/products", headers=H(demo_token)).json()]
    assert "MediShield Plus" in demo


def test_facts_isolated(app_client, agent_factory):
    a, _ = agent_factory()
    b, _ = agent_factory()
    app_client.post("/api/facts", headers=H(a), json={"text": "A 的知识"})
    assert app_client.get("/api/facts", headers=H(b)).json()["facts"] == []


def test_cannot_touch_other_tenant_client_by_id(app_client, agent_factory):
    a, _ = agent_factory()
    b, _ = agent_factory()
    app_client.post("/api/clients", headers=H(a), json={"name": "A 的客户"})
    cid = app_client.get("/api/clients", headers=H(a)).json()[0]["id"]
    assert app_client.post("/api/clients/update", headers=H(b),
                           json={"id": cid, "name": "被改了"}).status_code == 404
    assert app_client.post("/api/delete", headers=H(b),
                           json={"kind": "client", "id": cid}).status_code == 404


def test_audit_records_real_agent(app_client, agent_factory):
    """审计日志曾经全部落成默认值 agent_demo，分不清谁做的。"""
    import db
    tok, email = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "审计测试"})
    s = db.SessionLocal()
    try:
        agent = s.query(db.Agent).filter_by(email=email).first()
        row = (s.query(db.Audit).filter_by(action="add_client", detail="审计测试")
               .order_by(db.Audit.id.desc()).first())
        assert row is not None and row.agent_id == agent.agent_key
    finally:
        s.close()
