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


# ── 客户渠道 ──────────────────────────────────────────────────
def test_customer_message_lands_in_the_inbox(app_client, agent_factory, fake_tg):
    """没绑过码的人就是客户：消息进收件箱，等代理人确认。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("我的保单是不是快到期了？", "555001", "Lim Mei Ling"))
    finally:
        s.close()

    inbox = app_client.get("/api/inbox", headers=H(tok)).json()
    assert len(inbox) == 1
    assert inbox[0]["client"] == "Lim Mei Ling"
    assert inbox[0]["channel"] == "telegram"
    assert inbox[0]["unread"] == 1 and inbox[0]["status"] == "pending"

    msgs = app_client.get(f"/api/inbox/{inbox[0]['id']}", headers=H(tok)).json()["messages"]
    assert [m["role"] for m in msgs] == ["customer"]
    assert msgs[0]["text"] == "我的保单是不是快到期了？"


def test_no_policy_basis_means_no_auto_reply(app_client, agent_factory, fake_tg,
                                             monkeypatch):
    """条款库里查不到依据就绝不能开口——宁可转人工，也不许猜。"""
    import db
    import graph
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)

    def must_not_run(*a, **kw):
        raise AssertionError("没有条款依据却调了模型")
    monkeypatch.setattr(graph, "llm_text", must_not_run)

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("等待期多久？", "555002", "客户甲"))
    finally:
        s.close()

    replies = [t for c, t in fake_tg.sent if c == "555002"]
    assert replies == [telegram.CUSTOMER_ACK], replies
    tid = app_client.get("/api/inbox", headers=H(tok)).json()[0]["id"]
    msgs = app_client.get(f"/api/inbox/{tid}", headers=H(tok)).json()["messages"]
    assert all(m["role"] == "customer" for m in msgs)


def _with_policy_doc(app_client, tok):
    app_client.post("/api/documents", headers=H(tok),
                    files={"file": ("terms.txt",
                                    "本产品的一般等待期为 30 天，特定疾病 120 天。" * 4,
                                    "text/plain")},
                    data={"product": "TestPlan"})


def test_policy_question_gets_answered_with_citation(app_client, agent_factory,
                                                     fake_tg, monkeypatch):
    """有依据的条款问题：自动回，且必须带出处和免责声明。"""
    import db
    import graph
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    _with_policy_doc(app_client, tok)
    key, row = _bot_row(email)
    monkeypatch.setattr(graph, "llm_text", lambda p, agent_id="": "一般等待期是 30 天。")

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("等待期多久？", "556001", "问条款的客户"))
    finally:
        s.close()

    reply = [t for c, t in fake_tg.sent if c == "556001"][-1]
    assert "30 天" in reply
    assert "📄" in reply, "自动回复必须带出处"
    assert graph.DISCLAIMER.strip() in reply, "必须带免责声明"
    assert "人工" in reply, "必须给客户一条转人工的出路"

    th = app_client.get("/api/inbox", headers=H(tok)).json()[0]
    assert th["status"] == "sent"
    msgs = app_client.get(f"/api/inbox/{th['id']}", headers=H(tok)).json()["messages"]
    assert [m["role"] for m in msgs] == ["customer", "ai"], "自动回复要留痕，代理人能复核"


@pytest.mark.parametrize("q", [
    "我要理赔，怎么弄？",
    "这个能核保通过吗",
    "帮我报价，多少钱",
    "你觉得哪家好？推荐哪个",
    "我要投诉",
    "我想退保",
])
def test_sensitive_topics_always_go_to_a_human(app_client, agent_factory, fake_tg,
                                               monkeypatch, q):
    """涉及钱、涉及决定、涉及推荐 —— 一律转人工，哪怕条款库里查得到。"""
    import db
    import graph
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    _with_policy_doc(app_client, tok)
    key, row = _bot_row(email)

    def must_not_run(*a, **kw):
        raise AssertionError(f"敏感话题却调了模型：{q}")
    monkeypatch.setattr(graph, "llm_text", must_not_run)

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update(q, "557001", "客户"))
    finally:
        s.close()
    tid = app_client.get("/api/inbox", headers=H(tok)).json()[0]["id"]
    msgs = app_client.get(f"/api/inbox/{tid}", headers=H(tok)).json()["messages"]
    assert all(m["role"] == "customer" for m in msgs)


def test_customer_can_always_ask_for_a_human(app_client, agent_factory, fake_tg,
                                             monkeypatch):
    import db
    import graph
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    _with_policy_doc(app_client, tok)
    key, row = _bot_row(email)
    monkeypatch.setattr(graph, "llm_text",
                        lambda p, agent_id="": (_ for _ in ()).throw(
                            AssertionError("客户要人工却调了模型")))
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("等待期多久？我要人工", "558001", "客户"))
    finally:
        s.close()
    tid = app_client.get("/api/inbox", headers=H(tok)).json()[0]["id"]
    msgs = app_client.get(f"/api/inbox/{tid}", headers=H(tok)).json()["messages"]
    assert all(m["role"] == "customer" for m in msgs)


def test_quota_exhaustion_is_never_shown_to_the_customer(app_client, agent_factory,
                                                         fake_tg):
    """配额用完是我们的内部问题，客户只该看到「转给同事」。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    _with_policy_doc(app_client, tok)
    key, row = _bot_row(email)
    s = db.SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(agent_key=key).first()
        a.token_quota = 1
        s.commit()
    finally:
        s.close()
    db.record_usage(key, "m", 50, 50)

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("等待期多久？", "559001", "客户"))
    finally:
        s.close()
    to_customer = " ".join(t for c, t in fake_tg.sent if c == "559001")
    for leak in ("上限", "quota", "tokens", "配额"):
        assert leak not in to_customer, f"把内部信息「{leak}」发给客户了"


def test_escalation_reason_is_only_in_the_audit_log(app_client, agent_factory, fake_tg,
                                                    monkeypatch):
    import db
    import graph
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("我要理赔", "560001", "客户"))
    finally:
        s.close()
    s = db.SessionLocal()
    try:
        row_a = (s.query(db.Audit).filter_by(agent_id=key, action="tg_escalate")
                 .order_by(db.Audit.id.desc()).first())
        assert row_a and "原因=" in row_a.detail
    finally:
        s.close()


def test_auto_reply_can_be_turned_off_per_account(app_client, agent_factory, fake_tg,
                                                  monkeypatch):
    import db
    import graph
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    _with_policy_doc(app_client, tok)
    key, row = _bot_row(email)
    s = db.SessionLocal()
    try:
        s.query(db.Agent).filter_by(agent_key=key).first().auto_reply = 0
        s.commit()
    finally:
        s.close()
    monkeypatch.setattr(graph, "llm_text",
                        lambda p, agent_id="": (_ for _ in ()).throw(
                            AssertionError("关掉了自动回复却还是调了模型")))
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("等待期多久？", "561001", "客户"))
    finally:
        s.close()
    tid = app_client.get("/api/inbox", headers=H(tok)).json()[0]["id"]
    msgs = app_client.get(f"/api/inbox/{tid}", headers=H(tok)).json()["messages"]
    assert all(m["role"] == "customer" for m in msgs)


def test_only_the_first_customer_message_gets_an_ack(app_client, agent_factory, fake_tg):
    """第二条起不再回执，否则客户会被刷屏。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)
    for text in ("第一条", "第二条", "第三条"):
        s = db.SessionLocal()
        try:
            telegram.handle_update(s, row.path_secret, row.header_secret,
                                   _update(text, "555003", "话多的客户"))
        finally:
            s.close()
    acks = [t for c, t in fake_tg.sent if c == "555003" and t == telegram.CUSTOMER_ACK]
    assert len(acks) == 1

    tid = app_client.get("/api/inbox", headers=H(tok)).json()[0]["id"]
    assert len(app_client.get(f"/api/inbox/{tid}",
                              headers=H(tok)).json()["messages"]) == 3


def test_agent_reply_is_delivered_to_the_customer(app_client, agent_factory, fake_tg):
    """代理人在网页上点发送 → 客户真的在 Telegram 收到。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("在吗", "555004", "客户乙"))
    finally:
        s.close()
    tid = app_client.get("/api/inbox", headers=H(tok)).json()[0]["id"]

    r = app_client.post(f"/api/inbox/{tid}/send", headers=H(tok),
                        json={"text": "在的，你的保单 8/20 到期"})
    assert r.json()["delivered"] is True
    assert ("555004", "在的，你的保单 8/20 到期") in fake_tg.sent

    th = app_client.get(f"/api/inbox/{tid}", headers=H(tok)).json()
    assert th["status"] == "sent" and th["unread"] == 0


def test_manual_thread_reply_does_not_try_telegram(app_client, agent_factory, fake_tg):
    """手工模拟的会话没有 chat_id，不该尝试外发。"""
    import auth
    import db
    tok, _ = agent_factory()
    key = auth.verify_token(tok)
    s = db.SessionLocal()
    try:
        t = db.Thread(agent_id=key, client="手工客户", channel="manual")
        s.add(t)
        s.commit()
        tid = t.id
    finally:
        s.close()

    before = len(fake_tg.sent)
    r = app_client.post(f"/api/inbox/{tid}/send", headers=H(tok), json={"text": "你好"})
    assert r.json()["delivered"] is False
    assert len(fake_tg.sent) == before


def test_customer_message_notifies_the_agent(app_client, agent_factory, fake_tg):
    """代理人绑过设备的话，客户一来就该收到推送提醒。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)
    code = app_client.post("/api/telegram/bindcode", headers=H(tok)).json()["code"]
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update(f"/start {code}", "700001", "代理人"))
    finally:
        s.close()
    fake_tg.sent.clear()

    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("我想加保", "555005", "潜在客户"))
    finally:
        s.close()
    to_agent = [t for c, t in fake_tg.sent if c == "700001"]
    assert to_agent and "潜在客户" in to_agent[0] and "我想加保" in to_agent[0]


def test_customer_threads_are_isolated_between_agents(app_client, agent_factory, fake_tg):
    import db
    import telegram
    tok_a, email_a = agent_factory()
    tok_b, _ = agent_factory()
    _connect(app_client, tok_a)
    key_a, row_a = _bot_row(email_a)
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row_a.path_secret, row_a.header_secret,
                               _update("A 的客户", "555006", "只属于A"))
    finally:
        s.close()
    assert app_client.get("/api/inbox", headers=H(tok_b)).json() == []


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


# ── 客户主动找上门 → 线索 → 客户 ──────────────────────────────
def _lead_thread(app_client, tok, email, fake_tg, chat="666001", name="陌生人"):
    import db
    import telegram
    _connect(app_client, tok)
    key, row = _bot_row(email)
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update("我想了解一下医疗险", chat, name))
    finally:
        s.close()
    return app_client.get("/api/inbox", headers=H(tok)).json()[0]


def test_stranger_arrives_as_a_lead(app_client, agent_factory, fake_tg):
    """客户是自己找过来的，第一次接触时还不在客户库里。"""
    tok, email = agent_factory()
    th = _lead_thread(app_client, tok, email, fake_tg)
    assert th["is_lead"] is True and th["client_id"] is None
    assert app_client.get("/api/clients", headers=H(tok)).json() == []


def test_lead_becomes_a_new_client(app_client, agent_factory, fake_tg):
    tok, email = agent_factory()
    th = _lead_thread(app_client, tok, email, fake_tg)

    r = app_client.post(f"/api/inbox/{th['id']}/link", headers=H(tok),
                        json={"name": "Ahmad Faiz", "phone": "012-345 6789"})
    assert r.status_code == 200

    cs = app_client.get("/api/clients", headers=H(tok)).json()
    assert [c["name"] for c in cs] == ["Ahmad Faiz"]

    after = app_client.get(f"/api/inbox/{th['id']}", headers=H(tok)).json()
    assert after["is_lead"] is False
    assert after["client_id"] == cs[0]["id"]
    assert after["client"] == "Ahmad Faiz"


def test_lead_links_to_an_existing_client(app_client, agent_factory, fake_tg):
    """老客户换了个 Telegram 号找来，应该能关联到已有档案而不是重复建。"""
    tok, email = agent_factory()
    app_client.post("/api/clients", headers=H(tok),
                    json={"name": "Lim Mei Ling", "phone": "016-777 9911"})
    cid = app_client.get("/api/clients", headers=H(tok)).json()[0]["id"]
    th = _lead_thread(app_client, tok, email, fake_tg)

    app_client.post(f"/api/inbox/{th['id']}/link", headers=H(tok),
                    json={"client_id": cid})
    after = app_client.get(f"/api/inbox/{th['id']}", headers=H(tok)).json()
    assert after["client_id"] == cid and after["client"] == "Lim Mei Ling"
    assert len(app_client.get("/api/clients", headers=H(tok)).json()) == 1


def test_linked_thread_gives_the_ai_real_context(app_client, agent_factory, fake_tg):
    """关联之后，起草时才拿得到这个人的保单——这就是关联的意义。"""
    import db
    import main
    tok, email = agent_factory()
    app_client.post("/api/clients", headers=H(tok), json={"name": "Tan Ah Kow"})
    cid = app_client.get("/api/clients", headers=H(tok)).json()[0]["id"]
    app_client.post("/api/policies", headers=H(tok),
                    json={"client_id": cid, "product": "MediShield Plus",
                          "policy_no": "MSP-9001"})
    th = _lead_thread(app_client, tok, email, fake_tg)

    import auth
    key = auth.verify_token(tok)
    s = db.SessionLocal()
    try:
        t = s.query(db.Thread).filter_by(id=th["id"]).first()
        ci, _, _, _ = main._ctx_for(s, t, key)
        assert ci == "", "还没关联就不该有客户档案"
    finally:
        s.close()

    app_client.post(f"/api/inbox/{th['id']}/link", headers=H(tok),
                    json={"client_id": cid})
    s = db.SessionLocal()
    try:
        t = s.query(db.Thread).filter_by(id=th["id"]).first()
        ci, _, _, _ = main._ctx_for(s, t, key)
        assert "Tan Ah Kow" in ci and "MSP-9001" in ci
    finally:
        s.close()


def test_cannot_link_a_thread_to_another_agents_client(app_client, agent_factory, fake_tg):
    tok_a, email_a = agent_factory()
    tok_b, _ = agent_factory()
    app_client.post("/api/clients", headers=H(tok_b), json={"name": "B 的客户"})
    other = app_client.get("/api/clients", headers=H(tok_b)).json()[0]["id"]
    th = _lead_thread(app_client, tok_a, email_a, fake_tg)
    r = app_client.post(f"/api/inbox/{th['id']}/link", headers=H(tok_a),
                        json={"client_id": other})
    assert r.status_code == 404


def test_link_without_a_name_is_refused(app_client, agent_factory, fake_tg):
    tok, email = agent_factory()
    th = _lead_thread(app_client, tok, email, fake_tg, name="")
    s_id = th["id"]
    import db
    s = db.SessionLocal()
    try:
        t = s.query(db.Thread).filter_by(id=s_id).first()
        t.client = ""
        s.commit()
    finally:
        s.close()
    assert app_client.post(f"/api/inbox/{s_id}/link", headers=H(tok),
                           json={}).status_code == 400


def test_binding_a_phone_completes_the_onboarding_step(app_client, agent_factory,
                                                       fake_tg):
    """走真实绑定流程：扫码前那一步不算完成，绑完才算。"""
    import db
    import telegram
    tok, email = agent_factory()
    _connect(app_client, tok)
    key, row = _bot_row(email)

    step = lambda: next(s for s in app_client.get("/api/onboarding", headers=H(tok))
                        .json()["steps"] if s["key"] == "telegram")
    assert step()["done"] is False and step()["bot"] is True

    code = app_client.post("/api/telegram/bindcode", headers=H(tok)).json()["code"]
    s = db.SessionLocal()
    try:
        telegram.handle_update(s, row.path_secret, row.header_secret,
                               _update(f"/start {code}", "700009", "我的手机"))
    finally:
        s.close()

    assert step()["done"] is True and step()["count"] == 1
