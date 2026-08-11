"""按 id 定位（#20）、软删/回收站/恢复、管理员硬删（PDPA）。"""
from conftest import H


def _mk_client(c, tok, name, **kw):
    c.post("/api/clients", headers=H(tok), json={"name": name, **kw})
    same = [x for x in c.get("/api/clients", headers=H(tok)).json() if x["name"] == name]
    return max(same, key=lambda x: x["id"])      # 同名时取刚建的那个


def test_update_client_by_id_survives_rename(app_client, agent_factory):
    tok, _ = agent_factory()
    cl = _mk_client(app_client, tok, "原名")
    app_client.post("/api/clients/update", headers=H(tok),
                    json={"id": cl["id"], "name": "新名", "phone": "012"})
    names = [x["name"] for x in app_client.get("/api/clients", headers=H(tok)).json()]
    assert names == ["新名"]


def test_duplicate_names_do_not_collide(app_client, agent_factory):
    """两个同名客户：按 id 改只动一个；按名字加保单必须报错而不是挂错人。"""
    tok, _ = agent_factory()
    a = _mk_client(app_client, tok, "Lim Mei Ling", phone="011")
    b = _mk_client(app_client, tok, "Lim Mei Ling", phone="012")
    assert a["id"] != b["id"]

    app_client.post("/api/clients/update", headers=H(tok),
                    json={"id": b["id"], "name": "Lim Mei Ling (2)", "phone": "012"})
    got = {x["id"]: x["name"] for x in app_client.get("/api/clients", headers=H(tok)).json()}
    assert got[a["id"]] == "Lim Mei Ling" and got[b["id"]] == "Lim Mei Ling (2)"


def test_ambiguous_name_refuses_instead_of_guessing(app_client, agent_factory):
    tok, _ = agent_factory()
    _mk_client(app_client, tok, "Lim Ah Kow")
    _mk_client(app_client, tok, "Lim Ah Seng")
    r = app_client.post("/api/policies", headers=H(tok),
                        json={"client": "Lim", "product": "X"})
    assert r.json()["ok"] is False and "多个客户" in r.json()["msg"]


def test_policy_update_by_id(app_client, agent_factory):
    tok, _ = agent_factory()
    cl = _mk_client(app_client, tok, "保单客户")
    app_client.post("/api/policies", headers=H(tok),
                    json={"client_id": cl["id"], "product": "MediShield", "policy_no": "P-1"})
    pol = app_client.get("/api/clients", headers=H(tok)).json()[0]["policies"][0]
    app_client.post("/api/policies/update", headers=H(tok),
                    json={"id": pol["id"], "product": "CarePlus", "policy_no": "P-2"})
    got = app_client.get("/api/clients", headers=H(tok)).json()[0]["policies"][0]
    assert (got["product"], got["policy_no"]) == ("CarePlus", "P-2")


def test_soft_delete_hides_then_restores(app_client, agent_factory):
    tok, _ = agent_factory()
    cl = _mk_client(app_client, tok, "待删客户")
    app_client.post("/api/policies", headers=H(tok),
                    json={"client_id": cl["id"], "product": "P"})

    app_client.post("/api/delete", headers=H(tok), json={"kind": "client", "id": cl["id"]})
    assert app_client.get("/api/clients", headers=H(tok)).json() == []
    assert app_client.get("/api/dashboard", headers=H(tok)).json()["clients"] == 0

    trash = app_client.get("/api/trash", headers=H(tok)).json()
    assert {"client", "policy"} <= {r["kind"] for r in trash}
    # 标签必须是给人看的字符串，不能把整个 ORM 对象泄出去
    for r in trash:
        assert isinstance(r["label"], str), r
    assert next(r["label"] for r in trash if r["kind"] == "policy").startswith("P")

    app_client.post("/api/restore", headers=H(tok), json={"kind": "client", "id": cl["id"]})
    back = app_client.get("/api/clients", headers=H(tok)).json()
    assert len(back) == 1 and len(back[0]["policies"]) == 1


def test_deleted_fact_leaves_ai_context(app_client, agent_factory):
    tok, _ = agent_factory()
    app_client.post("/api/facts", headers=H(tok), json={"text": "会被删掉的知识"})
    fid = app_client.get("/api/facts", headers=H(tok)).json()["facts"][0]["id"]
    app_client.post("/api/delete", headers=H(tok), json={"kind": "fact", "id": fid})
    assert app_client.get("/api/facts", headers=H(tok)).json()["facts"] == []


def test_admin_purge_is_permanent(app_client, admin_token, agent_factory):
    """PDPA 被遗忘权：管理员硬删后数据库里必须查无此人。"""
    import db
    tok, _ = agent_factory()
    cl = _mk_client(app_client, tok, "要求被遗忘的人")
    app_client.post("/api/policies", headers=H(tok),
                    json={"client_id": cl["id"], "product": "P"})

    r = app_client.post(f"/api/admin/clients/{cl['id']}/purge", headers=H(admin_token))
    assert r.status_code == 200 and r.json()["purged"]["policies"] == 1

    s = db.SessionLocal()
    try:
        assert s.query(db.Client).filter_by(id=cl["id"]).first() is None
        assert s.query(db.Policy).filter_by(client_id=cl["id"]).count() == 0
    finally:
        s.close()
    assert app_client.get("/api/trash", headers=H(tok)).json() == []


def test_purge_requires_admin(app_client, agent_factory):
    tok, _ = agent_factory()
    cl = _mk_client(app_client, tok, "普通客户")
    assert app_client.post(f"/api/admin/clients/{cl['id']}/purge",
                           headers=H(tok)).status_code == 403


def test_upload_rejects_bad_type_and_size(app_client, agent_factory):
    import main
    tok, _ = agent_factory()
    r = app_client.post("/api/documents", headers=H(tok),
                        files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400
    big = b"x" * (main.MAX_UPLOAD_MB * 1024 * 1024 + 10)
    r = app_client.post("/api/documents", headers=H(tok),
                        files={"file": ("big.txt", big, "text/plain")})
    assert r.status_code == 413
