"""条款检索、AI 录入容错、流式输出，以及前端转义的静态回归。"""
import json

import pytest

from conftest import H


# ── 条款检索 ──────────────────────────────────────────────────
def test_fake_chunks_never_reach_real_agents():
    """虚构示例条款只能给演示账号。泄漏 = AI 带着假出处引用编造条款。"""
    import db
    import knowledge
    assert knowledge.search_policy_chunks("等待期多久", "ag_realuser") == []
    assert knowledge.search_policy_chunks("等待期多久", db.DEMO_AGENT)


def test_chinese_tokenizer_actually_splits():
    """原来 query.split() 对中文无效，整句变成一个 term。"""
    import knowledge
    terms = knowledge._terms("MediShield 的等待期是多久？")
    assert "medishield" in terms
    assert "等待" in terms
    cjk = [t for t in terms if any("\u4e00" <= ch <= "\u9fff" for ch in t)]
    assert cjk and all(len(t) == 2 for t in cjk), cjk


def test_deleted_document_leaves_retrieval(app_client, agent_factory):
    import knowledge
    tok, email = agent_factory()
    app_client.post("/api/documents", headers=H(tok),
                    files={"file": ("terms.txt", "本产品的等待期为 45 天，特定疾病 180 天。" * 3,
                                    "text/plain")},
                    data={"product": "TestPlan"})
    import db
    s = db.SessionLocal()
    try:
        key = s.query(db.Agent).filter_by(email=email).first().agent_key
    finally:
        s.close()
    assert knowledge.search_policy_chunks("等待期", key)

    doc_id = app_client.get("/api/documents", headers=H(tok)).json()[0]["id"]
    app_client.post("/api/delete", headers=H(tok), json={"kind": "document", "id": doc_id})
    assert knowledge.search_policy_chunks("等待期", key) == []


# ── AI 录入容错（#19）──────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "这不是 JSON",
    '{"action":"add_client"}',              # 缺 name
    '{"action":"add_client","name":null}',  # null
    '{"action":"add_client","name":{"x":1}}',   # 嵌套对象
    '[1,2,3]',                              # 不是对象
    '{"action":"add_appointment","client":"A","date":"下周三"}',   # 日期不合法
    '{"action":"add_product","name":"X","highlights":"不是数组"}',
])
def test_action_node_survives_garbage(monkeypatch, raw):
    """模型什么都可能吐出来，绝不能 500。"""
    import db
    import graph
    monkeypatch.setattr(graph, "llm_text", lambda p, agent_id="": raw)
    out = graph.action_node({"messages": [("user", "帮我加客户")],
                             "agent_id": "ag_test", "route": "action",
                             "citations": [], "needs_human": False})
    assert isinstance(out["messages"][0][1], str) and out["messages"][0][1]


def test_action_node_adds_client(monkeypatch):
    import db
    import graph
    monkeypatch.setattr(graph, "llm_text",
                        lambda p, agent_id="": '{"action":"add_client","name":"Ahmad","phone":"012"}')
    out = graph.action_node({"messages": [("user", "帮我加客户 Ahmad")],
                             "agent_id": "ag_actiontest", "route": "action",
                             "citations": [], "needs_human": False})
    assert "Ahmad" in out["messages"][0][1]
    s = db.SessionLocal()
    try:
        assert s.query(db.Client).filter_by(agent_id="ag_actiontest", name="Ahmad").first()
    finally:
        s.close()


def test_ambiguous_client_in_ai_path_refuses(monkeypatch):
    import db
    import graph
    s = db.SessionLocal()
    try:
        s.add_all([db.Client(agent_id="ag_dup", name="Tan Ah Kow"),
                   db.Client(agent_id="ag_dup", name="Tan Ah Seng")])
        s.commit()
    finally:
        s.close()
    monkeypatch.setattr(graph, "llm_text",
                        lambda p, agent_id="": '{"action":"add_policy","client":"Tan","product":"X"}')
    out = graph.action_node({"messages": [("user", "给 Tan 加保单")],
                             "agent_id": "ag_dup", "route": "action",
                             "citations": [], "needs_human": False})
    assert "多个客户" in out["messages"][0][1]


# ── LLM 不可用时的表现（#17）──────────────────────────────────
def test_llm_failure_becomes_502_not_stacktrace(app_client, agent_factory, monkeypatch):
    import graph
    def boom(prompt, agent_id=""):
        raise graph.LLMUnavailable("AI 服务暂时不可用，请稍后再试")
    monkeypatch.setattr(graph, "llm_text", boom)
    tok, _ = agent_factory()
    r = app_client.post("/api/chat", headers=H(tok), json={"message": "你好"})
    assert r.status_code in (200, 502)
    assert "Traceback" not in r.text and "sqlite" not in r.text.lower()


# ── 流式输出（#18）────────────────────────────────────────────
def test_stream_emits_route_tokens_done(app_client, agent_factory, monkeypatch):
    import graph
    monkeypatch.setattr(graph, "route_of", lambda q, agent_id="": "drafting")
    monkeypatch.setattr(graph, "llm_tokens", lambda p, agent_id="": iter(["你", "好", "呀"]))
    tok, _ = agent_factory()
    with app_client.stream("POST", "/api/chat/stream", headers=H(tok),
                           json={"message": "写个话术"}) as r:
        assert r.status_code == 200
        events = [json.loads(l[5:]) for l in r.iter_lines()
                  if l.startswith("data:")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "route" and kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "token") == "你好呀"


def test_stream_error_is_an_event_not_a_crash(app_client, agent_factory, monkeypatch):
    import graph
    monkeypatch.setattr(graph, "route_of", lambda q, agent_id="": "chat")

    def boom(prompt, agent_id=""):
        raise graph.LLMUnavailable("AI 服务暂时不可用，请稍后再试")
    monkeypatch.setattr(graph, "llm_tokens", boom)
    tok, _ = agent_factory()
    with app_client.stream("POST", "/api/chat/stream", headers=H(tok),
                           json={"message": "你好"}) as r:
        events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
    assert events[-1]["type"] == "error"


def test_policy_answers_carry_disclaimer(app_client, agent_factory, monkeypatch):
    """合规红线 2：Policy/ClientBook 的回答必须带免责声明。"""
    import graph
    monkeypatch.setattr(graph, "route_of", lambda q, agent_id="": "clientbook")
    monkeypatch.setattr(graph, "llm_tokens", lambda p, agent_id="": iter(["答案"]))
    tok, _ = agent_factory()
    with app_client.stream("POST", "/api/chat/stream", headers=H(tok),
                           json={"message": "我有哪些客户"}) as r:
        text = "".join(json.loads(l[5:]).get("text", "")
                       for l in r.iter_lines() if l.startswith("data:"))
    assert graph.DISCLAIMER.strip() in text


# ── 前端转义的静态回归（#5）──────────────────────────────────
def test_frontend_escapes_server_data():
    """曾经 40+ 处 innerHTML 直接拼服务端数据。这里守住不回退。"""
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent
            / "static" / "index.html").read_text(encoding="utf-8")
    assert "const esc=" in html
    raw = ["${c.name}", "${c.notes}", "${c.phone}", "${m.text}", "${th.client}",
           "${th.last}", "${p.name}", "${p.insurer}", "${r.client}", "${a.client}",
           "${d.filename}", "${r.analysis}", "${a.email}", "${i.code}", "${s}</div>"]
    found = [x for x in raw if x in html]
    assert not found, f"这些服务端字段又被直接拼进 HTML 了：{found}"


def test_frontend_has_no_hardcoded_backend_url():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent
            / "static" / "index.html").read_text(encoding="utf-8")
    assert "onrender.com" not in html, "后端地址应由 <meta name=hivora-api> 注入"
