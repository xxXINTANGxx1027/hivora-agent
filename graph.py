"""Hivora Insurance Agent —— LangGraph 大脑（DeepSeek@OpenRouter，可回退本地 Ollama）。

图：Supervisor → Policy / ClientBook / Drafting / Action / Chat / Fallback → Compliance
数据读写全部走 db.py（真实落库）。
"""
from __future__ import annotations

import json
import os
from typing import Annotated, Literal, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

import db
from knowledge import search_policy_chunks

load_dotenv()

if os.environ.get("OPENROUTER_API_KEY"):
    from langchain_openai import ChatOpenAI
    MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")
    llm = ChatOpenAI(model=MODEL, temperature=0.2,
                     base_url="https://openrouter.ai/api/v1",
                     api_key=os.environ["OPENROUTER_API_KEY"])
else:
    from langchain_ollama import ChatOllama
    MODEL = "qwen2.5:7b"
    llm = ChatOllama(model=MODEL, temperature=0.2)

DISCLAIMER = ("\n\n---\n⚠️ 仅供参考，以保单条款原文为准。"
              "(For reference only — refer to the official policy wording.)")


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    agent_id: str
    route: str
    citations: list[dict]
    needs_human: bool


def _q(state) -> str:
    for m in reversed(state["messages"]):
        if getattr(m, "type", None) == "human":
            return m.content
        if isinstance(m, tuple) and m[0] == "user":
            return m[1]
    return ""


FASTPATH = {
    "action": ["新增", "帮我加", "加一个", "加个", "添加", "记一下", "记录", "安排",
               "约他", "约她", "加保单", "加预约", "加产品", "加客户"],
    "policy": ["等待期", "保障范围", "限额", "除外", "条款", "宽限", "对比", "vs", "保额",
               "medishield", "careplus", "familyguard", "coverage", "waiting period"],
    "clientbook": ["客户", "保单号", "有哪些保单", "续保", "到期", "缺口", "谁", "档案"],
    "drafting": ["写", "话术", "翻译", "三语", "draft", "whatsapp", "消息", "文案"],
}
ROUTES = ("policy", "clientbook", "drafting", "action", "chat", "fallback")


def supervisor(state):
    q = _q(state).lower()
    for route, kws in FASTPATH.items():
        if any(kw in q for kw in kws):
            return {"route": route}
    prompt = ("你是保险代理人助手的路由器。把用户消息分类为其一：\n"
              "policy=问保险条款/产品对比｜clientbook=查客户/保单｜drafting=写话术/翻译"
              "｜action=要求新增/记录数据｜chat=问候闲聊｜fallback=超范围\n"
              f"用户消息：{q}\n只输出一个词。")
    r = llm.invoke(prompt).content.strip().lower()
    return {"route": r if r in ROUTES else "fallback"}


def policy_node(state):
    q = _q(state)
    chunks = search_policy_chunks(q, state["agent_id"])
    if not chunks:
        return {"messages": [("assistant",
                "抱歉，条款库里没查到相关内容，请核对保单原文或换个问法。（绝不编造条款）")],
                "citations": []}
    ctx = "\n\n".join(f"[出处{i+1}] {c['insurer']}《{c['product']}》第{c['page']}页：{c['text']}"
                      for i, c in enumerate(chunks))
    prompt = ("你是马来西亚保险代理人的条款助手。仅根据以下条款片段回答，"
              "答案末尾列出用到的出处（格式：📄 产品·第X页）。查不到就直说，绝不编造。"
              "不要推荐哪家保险公司更好。\n\n"
              f"{ctx}\n\n用户问题：{q}")
    ans = llm.invoke(prompt).content
    return {"messages": [("assistant", ans)],
            "citations": [dict(insurer=c["insurer"], product=c["product"], page=c["page"])
                          for c in chunks]}


def clientbook_node(state):
    q = _q(state)
    s = db.SessionLocal()
    try:
        clients = [db.client_dict(c) for c in
                   s.query(db.Client).filter_by(agent_id=state["agent_id"]).all()]
    finally:
        s.close()
    prompt = ("你是保险代理人的客户档案助手。根据以下该代理人自己的客户保单数据回答，"
              "可指出续保临近、保障缺口。数据里没有的客户就说没有。"
              f"今天是 {db.today().isoformat()}。\n\n"
              f"客户数据：{json.dumps(clients, ensure_ascii=False)}\n\n代理人的问题：{q}")
    return {"messages": [("assistant", llm.invoke(prompt).content)], "citations": []}


def drafting_node(state):
    prompt = ("你是保险代理人的话术助手。按用户要求生成话术；若用户没有指定语言，"
              "则同时给出三个版本：English / Bahasa Melayu / 中文。"
              "语气专业亲切，适合直接在 WhatsApp 发给客户，别太长。\n\n"
              f"需求：{_q(state)}")
    return {"messages": [("assistant", llm.invoke(prompt).content)], "citations": []}


def action_node(state):
    q = _q(state)
    td = db.today()
    prompt = ("从用户指令中提取一个数据操作，只输出 JSON（不要代码块标记、不要解释）。\n"
              f"今天是 {td.isoformat()}（周{'一二三四五六日'[td.weekday()]}）。"
              "相对日期换算成 YYYY-MM-DD。add_appointment 的 client 必须填人名。\n"
              'schema 四选一：\n'
              '{"action":"add_client","name":"...","phone":"...","notes":"..."}\n'
              '{"action":"add_policy","client":"...","product":"...","policy_no":"...","premium":"...","renewal":"YYYY-MM-DD"}\n'
              '{"action":"add_appointment","client":"...","date":"YYYY-MM-DD","time":"HH:MM","purpose":"...","channel":"..."}\n'
              '{"action":"add_product","name":"...","type":"...","price":"...","highlights":["..."]}\n'
              "提取不到的字段留空字符串。\n\n用户指令：" + q)
    raw = llm.invoke(prompt).content.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    s = db.SessionLocal()
    try:
        a = json.loads(raw)
        act = a.get("action")
        if act == "add_client" and a.get("name"):
            s.add(db.Client(agent_id=state["agent_id"], name=a["name"],
                            phone=a.get("phone", ""), notes=a.get("notes", "")))
            msg = f"已新增客户 {a['name']}"
        elif act == "add_policy" and a.get("client") and a.get("product"):
            c = (s.query(db.Client).filter_by(agent_id=state["agent_id"])
                 .filter(db.Client.name.contains(a["client"])).first())
            if not c:
                return {"messages": [("assistant", f"没找到客户 {a['client']}，请先新增客户")],
                        "citations": []}
            s.add(db.Policy(client_id=c.id, product=a["product"],
                            policy_no=a.get("policy_no") or "待补",
                            premium=a.get("premium") or "待补",
                            renewal=a.get("renewal") or ""))
            msg = f"已给 {c.name} 加保单：{a['product']}"
        elif act == "add_appointment" and a.get("client") and a.get("date"):
            s.add(db.Appointment(agent_id=state["agent_id"], client=a["client"],
                                 date=a["date"], time=a.get("time") or "10:00",
                                 purpose=a.get("purpose", ""), channel=a.get("channel", "")))
            msg = f"已加预约：{a['date']} {a.get('time') or '10:00'} 与 {a['client']}"
        elif act == "add_product" and a.get("name"):
            s.add(db.Product(name=a["name"], type=a.get("type") or "保险",
                             price=a.get("price") or "待补",
                             highlights=json.dumps(a.get("highlights") or [], ensure_ascii=False)))
            msg = f"已加产品：{a['name']}"
        else:
            return {"messages": [("assistant",
                    "没识别出要做的操作，请说具体一点（加客户/加保单/加预约/加产品），"
                    "例如：帮我加客户 Ahmad，电话 012-3456789")], "citations": []}
        db.audit(s, "ai_action", raw)
        s.commit()
        return {"messages": [("assistant", "✅ " + msg + "\n（可在对应页面查看/修改）")],
                "citations": []}
    except json.JSONDecodeError:
        return {"messages": [("assistant",
                "抱歉没听懂要录入什么，请再说一次，例如：帮我加客户 Ahmad，电话 012-3456789")],
                "citations": []}
    finally:
        s.close()


def chat_node(state):
    prompt = ("你是 Hivora Insurance Agent，服务马来西亚保险代理人，"
              "能查条款、管客户保单、写三语话术、帮忙录入数据。自然简短地回应：\n" + _q(state))
    return {"messages": [("assistant", llm.invoke(prompt).content)], "citations": []}


def fallback_node(state):
    return {"messages": [("assistant",
            "这个不在我的服务范围内哦～ 我只帮你：查条款/对比产品、查你自己的客户保单、"
            "写三语话术、录入数据。（跨公司比价推荐、理赔核保决策我不做，需要的话请转人工。）")],
            "citations": [], "needs_human": True}


def compliance_node(state):
    last = state["messages"][-1]
    content = last.content if hasattr(last, "content") else last[1]
    s = db.SessionLocal()
    try:
        db.audit(s, "chat", json.dumps(dict(route=state["route"], q=_q(state)[:200]),
                                       ensure_ascii=False))
        s.commit()
    finally:
        s.close()
    if state["route"] in ("policy", "clientbook") and DISCLAIMER not in content:
        return {"messages": [("assistant", content + DISCLAIMER)]}
    return {}


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("supervisor", supervisor)
    for name, fn in [("policy", policy_node), ("clientbook", clientbook_node),
                     ("drafting", drafting_node), ("action", action_node),
                     ("chat", chat_node), ("fallback", fallback_node),
                     ("compliance", compliance_node)]:
        g.add_node(name, fn)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda st: st["route"], {r: r for r in ROUTES})
    for r in ROUTES:
        g.add_edge(r, "compliance")
    g.add_edge("compliance", END)
    return g.compile()


graph = build_graph()


def ask(question: str, agent_id: str = "agent_demo") -> dict:
    out = graph.invoke({"messages": [("user", question)], "agent_id": agent_id,
                        "route": "", "citations": [], "needs_human": False})
    last = out["messages"][-1]
    return dict(route=out["route"],
                answer=last.content if hasattr(last, "content") else str(last),
                citations=out.get("citations", []),
                needs_human=out.get("needs_human", False))
