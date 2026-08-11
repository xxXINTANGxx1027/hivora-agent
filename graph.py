"""Hivora Insurance Agent —— LangGraph 大脑（DeepSeek@OpenRouter，可回退本地 Ollama）。

图：Supervisor → Policy / ClientBook / Drafting / Action / Chat / Fallback → Compliance
数据读写全部走 db.py（真实落库）。

两条调用路径：
- `ask()`        走完整的 LangGraph 图，返回整段答案（内部逻辑/测试用）
- `ask_stream()` 路由后直接流式吐 token（前端 SSE 用），最后同样过合规处理
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Annotated, Literal, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

import db
from knowledge import search_policy_chunks

load_dotenv()
log = logging.getLogger("hivora.graph")

LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "60"))        # 单次调用超时（秒）
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "2"))         # 失败重试次数
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "8"))  # 同时在飞的调用数上限

if os.environ.get("OPENROUTER_API_KEY"):
    from langchain_openai import ChatOpenAI
    MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")
    llm = ChatOpenAI(model=MODEL, temperature=0.2,
                     base_url="https://openrouter.ai/api/v1",
                     api_key=os.environ["OPENROUTER_API_KEY"],
                     timeout=LLM_TIMEOUT, max_retries=LLM_RETRIES,
                     stream_usage=True)   # 流式也要拿得到 token 数，否则记账有缺口
else:
    from langchain_ollama import ChatOllama
    MODEL = "qwen2.5:7b"
    llm = ChatOllama(model=MODEL, temperature=0.2)

DISCLAIMER = ("\n\n---\n⚠️ 仅供参考，以保单条款原文为准。"
              "(For reference only — refer to the official policy wording.)")

# OpenRouter 抖动 / 本地 Ollama 没起来时，别让请求线程无限堆积
_slots = threading.BoundedSemaphore(LLM_CONCURRENCY)


class LLMUnavailable(RuntimeError):
    """模型调用失败或排队超时。对外只暴露友好文案，细节进日志。"""


class QuotaExceeded(RuntimeError):
    """账号本月 token 用超了。"""


def _check_quota(agent_id: str):
    if not agent_id:
        return
    limit = db.quota_for(agent_id)
    if limit < 0:
        return
    used = db.month_tokens(agent_id)
    if used >= limit:
        raise QuotaExceeded(
            f"本月 AI 用量已达上限（{used:,}/{limit:,} tokens），请联系管理员提额")


def _account(agent_id: str, msg):
    """从模型响应里取真实 token 数记账。拿不到就不记，绝不猜。"""
    u = getattr(msg, "usage_metadata", None) or {}
    inp, out = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    if inp or out:
        db.record_usage(agent_id, MODEL, inp, out)


def llm_text(prompt: str, agent_id: str = "") -> str:
    _check_quota(agent_id)
    if not _slots.acquire(timeout=LLM_TIMEOUT):
        raise LLMUnavailable("AI 正忙，请稍后再试")
    try:
        msg = llm.invoke(prompt)
    except Exception as e:
        log.warning("llm.invoke 失败: %s", e)
        raise LLMUnavailable("AI 服务暂时不可用，请稍后再试") from e
    finally:
        _slots.release()
    _account(agent_id, msg)
    return msg.content


def llm_tokens(prompt: str, agent_id: str = ""):
    """流式产出 token。失败时抛 LLMUnavailable。"""
    _check_quota(agent_id)
    if not _slots.acquire(timeout=LLM_TIMEOUT):
        raise LLMUnavailable("AI 正忙，请稍后再试")
    final = None
    try:
        for chunk in llm.stream(prompt):
            final = chunk if final is None else final + chunk
            text = getattr(chunk, "content", "") or ""
            if text:
                yield text
    except Exception as e:
        log.warning("llm.stream 失败: %s", e)
        raise LLMUnavailable("AI 服务暂时不可用，请稍后再试") from e
    finally:
        _slots.release()
        if final is not None:
            _account(agent_id, final)


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
STREAMABLE = ("policy", "clientbook", "drafting", "chat")   # 其余是动作/固定文案


def route_of(question: str, agent_id: str = "") -> str:
    q = question.lower()
    for route, kws in FASTPATH.items():
        if any(kw in q for kw in kws):
            return route
    prompt = ("你是保险代理人助手的路由器。把用户消息分类为其一：\n"
              "policy=问保险条款/产品对比｜clientbook=查客户/保单｜drafting=写话术/翻译"
              "｜action=要求新增/记录数据｜chat=问候闲聊｜fallback=超范围\n"
              f"用户消息：{q}\n只输出一个词。")
    try:
        r = llm_text(prompt, agent_id).strip().lower()
    except LLMUnavailable:
        return "chat"       # 路由挂了退回闲聊，后续节点会给出友好报错
    return r if r in ROUTES else "fallback"


def supervisor(state):
    return {"route": route_of(_q(state), state["agent_id"])}


# ── 提示词构造（流式和非流式共用同一套，避免两条路径漂移）──────
def policy_prompt(state) -> tuple[str | None, list[dict], str]:
    q = _q(state)
    chunks = search_policy_chunks(q, state["agent_id"])
    if not chunks:
        return None, [], ("抱歉，条款库里没查到相关内容，请核对保单原文或换个问法。"
                          "（绝不编造条款）")
    ctx = "\n\n".join(f"[出处{i+1}] {c['insurer']}《{c['product']}》第{c['page']}页：{c['text']}"
                      for i, c in enumerate(chunks))
    prompt = ("你是马来西亚保险代理人的条款助手。仅根据以下条款片段回答，"
              "答案末尾列出用到的出处（格式：📄 产品·第X页）。查不到就直说，绝不编造。"
              "不要推荐哪家保险公司更好。\n\n"
              f"{ctx}\n\n用户问题：{q}")
    cites = [dict(insurer=c["insurer"], product=c["product"], page=c["page"]) for c in chunks]
    return prompt, cites, ""


def clientbook_prompt(state) -> str:
    s = db.SessionLocal()
    try:
        clients = [db.client_dict(c) for c in db.live(s.query(db.Client), db.Client)
                   .filter_by(agent_id=state["agent_id"]).all()]
    finally:
        s.close()
    return ("你是保险代理人的客户档案助手。根据以下该代理人自己的客户保单数据回答，"
            "可指出续保临近、保障缺口。数据里没有的客户就说没有。"
            f"今天是 {db.today().isoformat()}。\n\n"
            f"客户数据：{json.dumps(clients, ensure_ascii=False)}\n\n代理人的问题：{_q(state)}")


def drafting_prompt(state) -> str:
    return ("你是保险代理人的话术助手。按用户要求生成话术；若用户没有指定语言，"
            "则同时给出三个版本：English / Bahasa Melayu / 中文。"
            "语气专业亲切，适合直接在 WhatsApp 发给客户，别太长。\n\n"
            f"需求：{_q(state)}")


def chat_prompt(state) -> str:
    return ("你是 Hivora Insurance Agent，服务马来西亚保险代理人，"
            "能查条款、管客户保单、写三语话术、帮忙录入数据。自然简短地回应：\n" + _q(state))


FALLBACK_TEXT = ("这个不在我的服务范围内哦～ 我只帮你：查条款/对比产品、查你自己的客户保单、"
                 "写三语话术、录入数据。（跨公司比价推荐、理赔核保决策我不做，需要的话请转人工。）")


def prompt_for(state) -> tuple[str | None, list[dict], str]:
    """返回 (prompt, citations, 直接给出的固定答复)。prompt 为 None 表示不需要调模型。"""
    r = state["route"]
    if r == "policy":
        return policy_prompt(state)
    if r == "clientbook":
        return clientbook_prompt(state), [], ""
    if r == "drafting":
        return drafting_prompt(state), [], ""
    if r == "chat":
        return chat_prompt(state), [], ""
    return None, [], FALLBACK_TEXT


# ── 节点 ──────────────────────────────────────────────────────
def _llm_node(state, builder):
    prompt, cites, fixed = builder(state)
    if prompt is None:
        return {"messages": [("assistant", fixed)], "citations": cites}
    try:
        ans = llm_text(prompt, state["agent_id"])
    except (LLMUnavailable, QuotaExceeded) as e:
        return {"messages": [("assistant", str(e))], "citations": []}
    return {"messages": [("assistant", ans)], "citations": cites}


def policy_node(state):
    return _llm_node(state, policy_prompt)


def clientbook_node(state):
    return _llm_node(state, lambda st: (clientbook_prompt(st), [], ""))


def drafting_node(state):
    return _llm_node(state, lambda st: (drafting_prompt(st), [], ""))


def chat_node(state):
    return _llm_node(state, lambda st: (chat_prompt(st), [], ""))


def fallback_node(state):
    return {"messages": [("assistant", FALLBACK_TEXT)], "citations": [], "needs_human": True}


# ── 录入动作 ──────────────────────────────────────────────────
ACTION_PROMPT = (
    "从用户指令中提取一个数据操作，只输出 JSON（不要代码块标记、不要解释）。\n"
    "今天是 {today}（周{wd}）。相对日期换算成 YYYY-MM-DD。add_appointment 的 client 必须填人名。\n"
    'schema 四选一：\n'
    '{{"action":"add_client","name":"...","phone":"...","notes":"..."}}\n'
    '{{"action":"add_policy","client":"...","product":"...","policy_no":"...","premium":"...","renewal":"YYYY-MM-DD"}}\n'
    '{{"action":"add_appointment","client":"...","date":"YYYY-MM-DD","time":"HH:MM","purpose":"...","channel":"..."}}\n'
    '{{"action":"add_product","name":"...","type":"...","price":"...","highlights":["..."]}}\n'
    "提取不到的字段留空字符串。\n\n用户指令：{q}")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_ASK_AGAIN = ("没识别出要做的操作，请说具体一点（加客户/加保单/加预约/加产品），"
              "例如：帮我加客户 Ahmad，电话 012-3456789")


def _s(a: dict, key: str, limit: int = 300) -> str:
    """模型可能吐出 null / 数字 / 嵌套对象，一律安全转成字符串。"""
    v = a.get(key)
    if v is None or isinstance(v, (dict, list)):
        return ""
    return str(v).strip()[:limit]


def _find_client(s, agent_id: str, name: str):
    """精确名优先；模糊匹配只在唯一命中时才认，避免挂错人。"""
    base = db.live(s.query(db.Client), db.Client).filter_by(agent_id=agent_id)
    exact = base.filter(db.Client.name == name).first()
    if exact:
        return exact, None
    hits = base.filter(db.Client.name.contains(name)).limit(5).all()
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, "「{}」匹配到多个客户：{}。请说全名。".format(
            name, "、".join(h.name for h in hits))
    return None, f"没找到客户 {name}，请先新增客户"


def action_node(state):
    td = db.today()
    prompt = ACTION_PROMPT.format(today=td.isoformat(),
                                  wd="一二三四五六日"[td.weekday()], q=_q(state))
    try:
        raw = llm_text(prompt, state["agent_id"]).strip()
    except (LLMUnavailable, QuotaExceeded) as e:
        return {"messages": [("assistant", str(e))], "citations": []}
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        a = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        log.info("action_node 解析失败: %r", raw[:200])
        return {"messages": [("assistant", _ASK_AGAIN)], "citations": []}
    if not isinstance(a, dict):
        return {"messages": [("assistant", _ASK_AGAIN)], "citations": []}

    aid = state["agent_id"]
    s = db.SessionLocal()
    try:
        act, msg = _s(a, "action"), None
        if act == "add_client" and _s(a, "name"):
            s.add(db.Client(agent_id=aid, name=_s(a, "name", 200),
                            phone=_s(a, "phone", 64), notes=_s(a, "notes", 1000)))
            msg = f"已新增客户 {_s(a, 'name', 200)}"
        elif act == "add_policy" and _s(a, "client") and _s(a, "product"):
            c, err = _find_client(s, aid, _s(a, "client", 200))
            if err:
                return {"messages": [("assistant", err)], "citations": []}
            renewal = _s(a, "renewal", 10)
            s.add(db.Policy(client_id=c.id, product=_s(a, "product", 200),
                            policy_no=_s(a, "policy_no", 100) or "待补",
                            premium=_s(a, "premium", 64) or "待补",
                            renewal=renewal if _DATE_RE.match(renewal) else ""))
            msg = f"已给 {c.name} 加保单：{_s(a, 'product', 200)}"
        elif act == "add_appointment" and _s(a, "client") and _DATE_RE.match(_s(a, "date", 10)):
            tm = _s(a, "time", 5)
            s.add(db.Appointment(agent_id=aid, client=_s(a, "client", 200),
                                 date=_s(a, "date", 10),
                                 time=tm if _TIME_RE.match(tm) else "10:00",
                                 purpose=_s(a, "purpose", 500), channel=_s(a, "channel", 200)))
            msg = f"已加预约：{_s(a, 'date', 10)} {tm if _TIME_RE.match(tm) else '10:00'} 与 {_s(a, 'client', 200)}"
        elif act == "add_product" and _s(a, "name"):
            hi = a.get("highlights")
            hi = [str(h)[:200] for h in hi if isinstance(h, (str, int, float))] if isinstance(hi, list) else []
            s.add(db.Product(agent_id=aid, name=_s(a, "name", 200),
                             type=_s(a, "type", 64) or "保险",
                             price=_s(a, "price", 120) or "待补",
                             highlights=json.dumps(hi, ensure_ascii=False)))
            msg = f"已加产品：{_s(a, 'name', 200)}"
        if msg is None:
            return {"messages": [("assistant", _ASK_AGAIN)], "citations": []}
        db.audit(s, aid, "ai_action", raw)
        s.commit()
        return {"messages": [("assistant", "✅ " + msg + "\n（可在对应页面查看/修改）")],
                "citations": []}
    except Exception:
        s.rollback()
        log.exception("action_node 落库失败 agent=%s raw=%r", aid, raw[:200])
        return {"messages": [("assistant", "录入失败了，请再说一次或到对应页面手动添加")],
                "citations": []}
    finally:
        s.close()


def _log_chat(agent_id: str, route: str, question: str):
    s = db.SessionLocal()
    try:
        db.audit(s, agent_id, "chat",
                 json.dumps(dict(route=route, q=question[:200]), ensure_ascii=False))
        s.commit()
    finally:
        s.close()


def compliance_node(state):
    last = state["messages"][-1]
    content = last.content if hasattr(last, "content") else last[1]
    _log_chat(state["agent_id"], state["route"], _q(state))
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


def ask(question: str, agent_id: str = db.DEMO_AGENT) -> dict:
    out = graph.invoke({"messages": [("user", question)], "agent_id": agent_id,
                        "route": "", "citations": [], "needs_human": False})
    last = out["messages"][-1]
    return dict(route=out["route"],
                answer=last.content if hasattr(last, "content") else str(last),
                citations=out.get("citations", []),
                needs_human=out.get("needs_human", False))


def ask_stream(question: str, agent_id: str):
    """产出事件字典：route → token* → done（出错则 error）。"""
    state = {"messages": [("user", question)], "agent_id": agent_id,
             "route": "", "citations": [], "needs_human": False}
    try:
        route = route_of(question, agent_id)
    except Exception:
        log.exception("路由失败")
        yield {"type": "error", "message": "AI 服务暂时不可用，请稍后再试"}
        return
    state["route"] = route
    yield {"type": "route", "route": route}

    if route not in STREAMABLE:
        node = action_node if route == "action" else fallback_node
        out = node(state)
        text = out["messages"][0][1]
        yield {"type": "token", "text": text}
        _log_chat(agent_id, route, question)
        yield {"type": "done", "citations": [], "needs_human": route == "fallback"}
        return

    try:
        prompt, cites, fixed = prompt_for(state)
    except Exception:
        log.exception("构造提示词失败 route=%s", route)
        yield {"type": "error", "message": "AI 服务暂时不可用，请稍后再试"}
        return

    parts: list[str] = []
    if prompt is None:
        parts.append(fixed)
        yield {"type": "token", "text": fixed}
    else:
        try:
            for tok in llm_tokens(prompt, agent_id):
                parts.append(tok)
                yield {"type": "token", "text": tok}
        except (LLMUnavailable, QuotaExceeded) as e:
            yield {"type": "error", "message": str(e)}
            return

    answer = "".join(parts)
    if route in ("policy", "clientbook") and DISCLAIMER not in answer:
        yield {"type": "token", "text": DISCLAIMER}
    _log_chat(agent_id, route, question)
    yield {"type": "done", "citations": cites, "needs_human": False}
