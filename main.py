"""Hivora Insurance Agent —— 生产 API（FastAPI + LangGraph + SQLAlchemy）。"""
import datetime as dt
import json
import logging
import os
import pathlib

import time
import uuid

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import func
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import auth
import db
import email_out
import telegram
from contextvars import ContextVar
from db import Appointment, Client, Fact, Message, Policy, Product, SessionLocal, Thread
from graph import MODEL, LLMUnavailable, QuotaExceeded, ask, ask_stream, llm_text
from knowledge import search_policy_chunks

BASE_DIR = pathlib.Path(__file__).resolve().parent
VERSION = "0.1"
REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
)


class _ReqIdFilter(logging.Filter):
    """让每条日志都带上请求 id，出事时能把一次请求的所有日志串起来。"""
    def filter(self, record):
        record.request_id = REQUEST_ID.get() or "-"
        return True


logging.getLogger().handlers[0].addFilter(_ReqIdFilter())
log = logging.getLogger("hivora")

# Sentry：设了 DSN 才启用，没设就完全不引入（本地和测试不受影响）
if os.environ.get("SENTRY_DSN"):
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"],
                        environment="production" if db.IS_PROD else "dev",
                        release=f"hivora@{VERSION}",
                        traces_sample_rate=float(os.environ.get("SENTRY_TRACES", "0.0")),
                        send_default_pii=False)   # 别把客户数据送出去
        log.info("Sentry 已启用")
    except ImportError:
        log.warning("设了 SENTRY_DSN 但没装 sentry-sdk，跳过")

app = FastAPI(title="Hivora Insurance Agent", version=VERSION)


@app.middleware("http")
async def observability(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = REQUEST_ID.set(rid)
    started = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        raise
    finally:
        REQUEST_ID.reset(token)
    ms = (time.perf_counter() - started) * 1000
    if ms > SLOW_MS or resp.status_code >= 500:
        log.warning("%s %s → %s (%.0fms)", request.method, request.url.path,
                    resp.status_code, ms)
    resp.headers["X-Request-ID"] = rid
    # 基础安全响应头
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp

# CORS：生产必须显式列出前端域名，不能是 *。
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if not _origins:
    if db.IS_PROD:
        raise RuntimeError("生产环境必须设置 ALLOWED_ORIGINS（逗号分隔的前端域名）。")
    _origins = ["*"]
app.add_middleware(CORSMiddleware, allow_origins=_origins,
                   allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type"])

db.ensure_schema()
db.migrate_columns()
db.seed_if_empty()
auth.ensure_demo_agent()
auth.ensure_admin()
db.maybe_purge_trash()
AID = Depends(auth.current_agent)
ADM = Depends(auth.current_admin)

SLOW_MS = int(os.environ.get("SLOW_REQUEST_MS", "3000"))
LIST_CAP = int(os.environ.get("LIST_CAP", "500"))   # 单次返回的最大条数


def _capped(rows: list, what: str, aid: str) -> list:
    """超过上限就截断并记一条日志——绝不静默丢数据。"""
    if len(rows) > LIST_CAP:
        log.warning("列表截断 %s agent=%s 返回 %d 条（共 >%d）", what, aid, LIST_CAP, LIST_CAP)
        return rows[:LIST_CAP]
    return rows
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
ALLOWED_UPLOAD_EXT = (".pdf", ".txt", ".md")


def now_hm():
    return dt.datetime.now().strftime("%H:%M")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# 管理站跟后端同源 —— 省掉 CORS 白名单，也省掉一个单独的托管项目。
# 这份是 admin/index.html 由 sync-frontend.sh 生成的副本，后端地址写成空串，
# 所以它调的永远是自己所在的域名。页面本身不含任何密钥，进去要管理员口令。
@app.get("/console")
@app.get("/console/")
def console():
    return FileResponse(BASE_DIR / "static" / "console.html",
                        headers={"X-Robots-Tag": "noindex, nofollow"})


# Render 会注入 RENDER_GIT_COMMIT；本地跑就是空。用来确认线上到底是哪一版。
BUILD = (os.environ.get("RENDER_GIT_COMMIT")
         or os.environ.get("HIVORA_BUILD", ""))[:12]


@app.get("/healthz")
def healthz():
    """存活探针 —— 只说明进程还在。顺路当回收站清理的心跳（每天最多真跑一次）。"""
    db.maybe_purge_trash()
    return {"ok": True, "model": MODEL, "version": VERSION, "build": BUILD}


@app.get("/readyz")
def readyz():
    """就绪探针 —— 真去点一下数据库。挂了要能立刻看出是 DB 还是应用。"""
    from sqlalchemy import text
    checks = {}
    try:
        s = SessionLocal()
        try:
            s.execute(text("SELECT 1"))
            checks["db"] = "ok"
        finally:
            s.close()
    except Exception as e:
        log.exception("readyz: 数据库不可用")
        checks["db"] = f"fail: {type(e).__name__}"
    checks["llm_configured"] = bool(os.environ.get("OPENROUTER_API_KEY"))
    checks["storage"] = "postgres" if not db.DB_URL.startswith("sqlite") else "sqlite"
    ok = checks["db"] == "ok"
    return JSONResponse(status_code=200 if ok else 503,
                        content={"ok": ok, "version": VERSION, "checks": checks})


# ── Copilot ───────────────────────────────────────────────────
class ChatReq(BaseModel):
    message: str


@app.exception_handler(LLMUnavailable)
def _llm_unavailable(request, exc):
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.exception_handler(QuotaExceeded)
def _quota_exceeded(request, exc):
    return JSONResponse(status_code=429, content={"error": str(exc), "detail": str(exc)})


@app.post("/api/chat")
def chat(req: ChatReq, aid: str = AID):
    """整段返回。前端默认走 /api/chat/stream，这个保留给非流式调用方和测试。"""
    try:
        return ask(req.message, aid)
    except QuotaExceeded as e:
        return JSONResponse(status_code=429, content={"error": str(e), "detail": str(e)})
    except LLMUnavailable as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    except Exception:
        # 不要把内部异常字符串回给前端（可能带连接串/模型 key/堆栈信息）
        log.exception("chat failed for agent=%s", aid)
        return JSONResponse(status_code=502,
                            content={"error": "AI 服务暂时不可用，请稍后再试"})


@app.post("/api/chat/stream")
def chat_stream(req: ChatReq, aid: str = AID):
    """SSE 逐字输出。事件：route → token* → done｜error。"""
    def events():
        try:
            for ev in ask_stream(req.message, aid):
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
        except Exception:
            log.exception("chat stream failed for agent=%s", aid)
            yield "data: " + json.dumps(
                {"type": "error", "message": "AI 服务暂时不可用，请稍后再试"}) + "\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── Dashboard ─────────────────────────────────────────────────
@app.get("/api/dashboard")
def dashboard(aid: str = AID):
    s = SessionLocal()
    try:
        # 全部走 SQL 聚合。原来把 policies/threads/appointments 整表拉进内存再数，
        # 数据一多就是 O(全表) 的内存和延迟。
        today_s = db.today().isoformat()
        in30 = (db.today() + dt.timedelta(days=30)).isoformat()
        in7 = (db.today() + dt.timedelta(days=7)).isoformat()

        clients = db.live(s.query(Client), Client).filter_by(agent_id=aid).count()
        pol_q = db.live(db.live(s.query(Policy), Policy).join(Client), Client).filter(
            Client.agent_id == aid)
        policies = pol_q.count()
        renew30 = pol_q.filter(Policy.renewal >= today_s, Policy.renewal <= in30).count()

        total_threads = s.query(func.count(Thread.id)).filter_by(agent_id=aid).scalar() or 0
        sent = (s.query(func.count(Thread.id))
                .filter_by(agent_id=aid, status="sent").scalar() or 0)
        pending = total_threads - sent
        ai_rate = round(sent / total_threads * 100) if total_threads else 0

        appts7 = (db.live(s.query(Appointment), Appointment)
                  .filter(Appointment.agent_id == aid,
                          Appointment.date >= today_s, Appointment.date <= in7).count())
        facts = db.live(s.query(Fact), Fact).filter_by(agent_id=aid).count()
        return dict(clients=clients, policies=policies, renewals_30d=renew30,
                    pending_replies=pending, ai_handled_pct=ai_rate,
                    facts=facts, appts_7d=appts7,
                    today=db.today().isoformat(), model=MODEL)
    finally:
        s.close()


class BrandReq(BaseModel):
    brand: str = ""
    auto_reply: bool | None = None


class ChangePwReq(BaseModel):
    old_password: str = ""
    new_password: str


@app.post("/api/password")
def change_password(req: ChangePwReq, aid: str = AID):
    """本人改密码。密码是他自己的，不该只能找管理员重发链接。"""
    s = SessionLocal()
    try:
        return auth.change_password(s, aid, req.old_password, req.new_password)
    finally:
        s.close()


@app.get("/api/brand")
def get_brand(aid: str = AID):
    """白牌：界面标题、AI 自称、给客户的消息都用这个名字。"""
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(agent_key=aid).first()
        return dict(brand=db.brand_of(aid),
                    is_default=not (a and (a.brand or "").strip()),
                    auto_reply=bool(a.auto_reply if a and a.auto_reply is not None else 1))
    finally:
        s.close()


@app.post("/api/brand")
def set_brand(req: BrandReq, aid: str = AID):
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(agent_key=aid).first()
        if not a:
            raise HTTPException(404)
        if req.brand is not None:
            a.brand = req.brand.strip()[:120]
        if req.auto_reply is not None:
            a.auto_reply = 1 if req.auto_reply else 0
        db.audit(s, aid, "set_brand", f"{a.brand}:auto_reply={a.auto_reply}")
        s.commit()
        return dict(ok=True, brand=db.brand_of(aid), auto_reply=bool(a.auto_reply))
    finally:
        s.close()


@app.get("/api/onboarding")
def onboarding(aid: str = AID):
    """新账号是空的，前四步决定他会不会留下来。"""
    s = SessionLocal()
    try:
        docs = db.live(s.query(db.Document), db.Document).filter_by(agent_id=aid).count()
        prods = s.query(Product).filter_by(agent_id=aid).count()
        # 连上 bot 只是一半：没把自己的手机绑上去，客户消息进来时没人会被叫醒，
        # 而代理人几乎不会一直开着网页。所以这一步要两个条件都满足才算完成。
        tg_bot = s.query(db.TelegramBot).filter_by(agent_id=aid).first() is not None
        tg_devices = s.query(db.TelegramChat).filter_by(agent_id=aid).count()
        clients = db.live(s.query(Client), Client).filter_by(agent_id=aid).count()
        steps = [dict(key="docs", done=docs > 0, count=docs),
                 dict(key="products", done=prods > 0, count=prods),
                 dict(key="telegram", done=tg_bot and tg_devices > 0,
                      count=tg_devices, bot=tg_bot),
                 dict(key="clients", done=clients > 0, count=clients)]
        return dict(steps=steps, done=all(x["done"] for x in steps))
    finally:
        s.close()


# ── 收件箱 ────────────────────────────────────────────────────
@app.get("/api/inbox")
def inbox(aid: str = AID):
    s = SessionLocal()
    try:
        out = []
        for t in s.query(Thread).filter_by(agent_id=aid).all():
            last = t.messages[-1] if t.messages else None
            out.append(dict(id=t.id, client=t.client, unread=t.unread, status=t.status,
                            channel=t.channel or "manual",
                            client_id=t.client_id, is_lead=not t.client_id,
                            last=(last.text[:60] if last else ""), ts=(last.ts if last else "")))
        return out
    finally:
        s.close()


def _thread(s, tid, aid):
    t = s.query(Thread).filter_by(id=tid, agent_id=aid).first()
    if not t:
        raise HTTPException(404)
    return t


def _thread_dict(t):
    return dict(id=t.id, client=t.client, lang=t.lang, status=t.status, mode=t.mode,
                channel=t.channel or "manual", unread=t.unread,
                client_id=t.client_id, is_lead=not t.client_id,
                suggestions=json.loads(t.suggestions or "[]"),
                messages=[dict(role=m.role, text=m.text, ts=m.ts) for m in t.messages])


@app.get("/api/inbox/{tid}")
def thread(tid: int, aid: str = AID):
    s = SessionLocal()
    try:
        return _thread_dict(_thread(s, tid, aid))
    finally:
        s.close()


def _ctx_for(s, t, aid):
    # 关联过就用关联的档案；没关联（还是线索）再按名字兜底找一次
    client = None
    if t.client_id:
        client = (db.live(s.query(Client), Client)
                  .filter_by(agent_id=aid, id=t.client_id).first())
    if client is None:
        client = (db.live(s.query(Client), Client).filter_by(agent_id=aid)
                  .filter(Client.name == t.client).first())
    convo = "\n".join(f"{m.role}: {m.text}" for m in t.messages[-5:])
    q = t.messages[-1].text if t.messages else ""
    chunks = search_policy_chunks(q, aid)
    ctx = "\n".join(f"- {c['product']} 第{c['page']}页: {c['text']}" for c in chunks)
    facts = "\n".join(f"- {f.text}" for f in db.live(s.query(Fact), Fact).filter_by(agent_id=aid))
    ci = ""
    if client:
        pols = "; ".join(f"{p.product}({p.policy_no}, 续保 {p.renewal}, {p.premium})"
                         for p in client.policies if not p.deleted)
        ci = f"客户档案：{client.name}，保单：{pols}。备注：{client.notes}"
    return ci, ctx, facts, convo


@app.post("/api/inbox/{tid}/suggest")
def suggest(tid: int, aid: str = AID):
    s = SessionLocal()
    try:
        t = _thread(s, tid, aid)
        ci, ctx, facts, convo = _ctx_for(s, t, aid)
        prompt = ("你是马来西亚保险代理人的 WhatsApp 回复助手。给出 3 条可选回复，"
                  "风格分别是：1) 专业详细 2) 亲切热情 3) 简短快捷。\n"
                  f"要求：用{t.lang}写；像真人代理人；只根据资料回答，拿不准就说会帮客户确认；"
                  "不构成 financial advice；不编数字。\n"
                  '只输出 JSON 数组（无代码块、无解释）：["回复1","回复2","回复3"]\n\n'
                  f"{ci}\n\n相关条款：\n{ctx}\n\n代理人补充知识：\n{facts}\n\n对话：\n{convo}")
        raw = llm_text(prompt, aid).strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            sugg = [x for x in json.loads(raw) if isinstance(x, str)][:3]
        except json.JSONDecodeError:
            sugg = [raw]
        t.suggestions = json.dumps(sugg, ensure_ascii=False)
        t.status = "drafted"
        db.audit(s, aid, "suggest", f"thread={tid}")
        s.commit()
        return dict(suggestions=sugg)
    finally:
        s.close()


class SendReq(BaseModel):
    text: str


@app.post("/api/inbox/{tid}/send")
def send(tid: int, req: SendReq, aid: str = AID):
    s = SessionLocal()
    try:
        t = _thread(s, tid, aid)
        s.add(Message(thread_id=t.id, role="agent", text=req.text, ts=now_hm()))
        t.status, t.suggestions, t.unread = "sent", "[]", 0
        db.audit(s, aid, "send", f"thread={tid}")
        s.commit()
        # Telegram 来的会话，回复要真的发回给客户
        delivered = False
        if t.channel == "telegram" and t.tg_chat_id:
            try:
                delivered = telegram.send_to_chat(s, aid, t.tg_chat_id, req.text)
            except Exception:
                log.exception("回复没能发到 Telegram thread=%s", tid)
        return dict(ok=True, delivered=delivered)
    finally:
        s.close()


class ModeReq(BaseModel):
    mode: str


@app.post("/api/inbox/{tid}/mode")
def set_mode(tid: int, req: ModeReq, aid: str = AID):
    s = SessionLocal()
    try:
        t = _thread(s, tid, aid)
        t.mode = req.mode if req.mode in ("ai", "human") else "ai"
        if t.mode == "human":
            t.suggestions = "[]"
        s.commit()
        return dict(mode=t.mode)
    finally:
        s.close()


@app.post("/api/inbox/{tid}/customer")
def customer_message(tid: int, req: SendReq, aid: str = AID):
    s = SessionLocal()
    try:
        t = _thread(s, tid, aid)
        s.add(Message(thread_id=t.id, role="customer", text=req.text, ts=now_hm()))
        t.status, t.suggestions, t.unread = "pending", "[]", t.unread + 1
        s.commit()
        return dict(ok=True)
    finally:
        s.close()


class LinkReq(BaseModel):
    client_id: int | None = None      # 关联到已有客户
    name: str = ""                    # 或者用这些字段新建一个
    phone: str = ""
    notes: str = ""


@app.post("/api/inbox/{tid}/link")
def link_thread(tid: int, req: LinkReq, aid: str = AID):
    """把主动找上门的线索转成客户档案。

    客户是自己找过来的，第一次接触时他还不在库里 —— 转成客户之后
    AI 起草才拿得到保单、续保日这些上下文。
    """
    s = SessionLocal()
    try:
        t = _thread(s, tid, aid)
        if req.client_id:
            c = _client_by_id(s, aid, req.client_id)
        else:
            name = (req.name or t.client or "").strip()
            if not name:
                raise HTTPException(400, "请填客户姓名")
            c = Client(agent_id=aid, name=name, phone=req.phone,
                       notes=req.notes or f"来自 {t.channel or 'manual'} 主动咨询")
            s.add(c)
            s.flush()
        t.client_id = c.id
        t.client = c.name
        db.audit(s, aid, "link_thread", f"thread={tid} client={c.id} {c.name}")
        s.commit()
        return dict(ok=True, client_id=c.id, name=c.name)
    finally:
        s.close()


# ── 训练 ─────────────────────────────────────────────────────
class FactReq(BaseModel):
    text: str


@app.get("/api/facts")
def get_facts(aid: str = AID):
    s = SessionLocal()
    try:
        return {"facts": [dict(id=f.id, text=f.text)
                          for f in db.live(s.query(Fact), Fact).filter_by(agent_id=aid)]}
    finally:
        s.close()


@app.post("/api/facts")
def add_fact(req: FactReq, aid: str = AID):
    s = SessionLocal()
    try:
        if req.text.strip():
            s.add(Fact(agent_id=aid, text=req.text.strip()))
            db.audit(s, aid, "teach", req.text[:100])
            s.commit()
        return {"ok": True}
    finally:
        s.close()


# ── 客户 / 保单 ───────────────────────────────────────────────
class ClientReq(BaseModel):
    name: str
    phone: str = ""
    notes: str = ""


class ClientUpdateReq(ClientReq):
    id: int | None = None
    orig: str = ""          # 旧前端兼容：按原名定位


class PolicyReq(BaseModel):
    client: str = ""
    client_id: int | None = None
    product: str
    policy_no: str = ""
    premium: str = ""
    renewal: str = ""


class PolicyUpdateReq(PolicyReq):
    id: int | None = None
    orig_no: str = ""       # 旧前端兼容：按原保单号定位


def _client_by_id(s, aid: str, cid: int) -> Client:
    c = db.live(s.query(Client), Client).filter_by(agent_id=aid, id=cid).first()
    if not c:
        raise HTTPException(404, "客户不存在")
    return c


def _client_by_name(s, aid: str, name: str) -> Client:
    """兼容路径：精确名优先，模糊匹配只在唯一命中时才认，避免挂错人。"""
    base = db.live(s.query(Client), Client).filter_by(agent_id=aid)
    c = base.filter(Client.name == name).first()
    if c:
        return c
    hits = base.filter(Client.name.contains(name)).limit(5).all()
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise HTTPException(400, f"「{name}」匹配到多个客户：" +
                            "、".join(h.name for h in hits) + "。请用全名。")
    raise HTTPException(404, f"没找到客户 {name}，请先新增客户")


@app.get("/api/clients")
def clients(aid: str = AID):
    s = SessionLocal()
    try:
        rows = [db.client_dict(c)
                for c in db.live(s.query(Client), Client)
                          .filter_by(agent_id=aid).limit(LIST_CAP + 1)]
        return _capped(rows, "clients", aid)
    finally:
        s.close()


@app.post("/api/clients")
def add_client(req: ClientReq, aid: str = AID):
    s = SessionLocal()
    try:
        s.add(Client(agent_id=aid, name=req.name, phone=req.phone, notes=req.notes))
        db.audit(s, aid, "add_client", req.name)
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/clients/update")
def update_client(req: ClientUpdateReq, aid: str = AID):
    s = SessionLocal()
    try:
        c = (_client_by_id(s, aid, req.id) if req.id
             else _client_by_name(s, aid, req.orig))
        c.name, c.phone, c.notes = req.name, req.phone, req.notes
        db.audit(s, aid, "edit_client", f"id={c.id}")
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/policies")
def add_policy(req: PolicyReq, aid: str = AID):
    s = SessionLocal()
    try:
        try:
            c = (_client_by_id(s, aid, req.client_id) if req.client_id
                 else _client_by_name(s, aid, req.client))
        except HTTPException as e:
            return {"ok": False, "msg": e.detail}
        s.add(Policy(client_id=c.id, product=req.product,
                     policy_no=req.policy_no or "待补", premium=req.premium or "待补",
                     renewal=req.renewal or ""))
        db.audit(s, aid, "add_policy", f"{c.name}:{req.product}")
        s.commit()
        return {"ok": True, "msg": "已加"}
    finally:
        s.close()


@app.post("/api/policies/update")
def update_policy(req: PolicyUpdateReq, aid: str = AID):
    s = SessionLocal()
    try:
        if req.id:
            p = (db.live(s.query(Policy), Policy).join(Client)
                 .filter(Client.agent_id == aid, Policy.id == req.id).first())
        else:   # 旧前端兼容
            c = _client_by_name(s, aid, req.client)
            p = next((x for x in c.policies
                      if x.policy_no == req.orig_no and not x.deleted), None)
        if not p:
            raise HTTPException(404, "保单不存在")
        p.product = req.product
        p.policy_no = req.policy_no or p.policy_no
        p.premium = req.premium or p.premium
        p.renewal = req.renewal or p.renewal
        db.audit(s, aid, "edit_policy", f"id={p.id}")
        s.commit()
        return {"ok": True}
    finally:
        s.close()


# ── 产品 ─────────────────────────────────────────────────────
class ProductReq(BaseModel):
    name: str
    type: str = ""
    price: str = ""
    highlights: str = ""
    insurer: str = ""


@app.get("/api/products")
def products(aid: str = AID):
    s = SessionLocal()
    try:
        return [dict(name=p.name, insurer=p.insurer, type=p.type, price=p.price,
                     highlights=json.loads(p.highlights or "[]"))
                for p in s.query(Product).filter_by(agent_id=aid)]
    finally:
        s.close()


@app.post("/api/products")
def add_product(req: ProductReq, aid: str = AID):
    s = SessionLocal()
    try:
        s.add(Product(agent_id=aid, name=req.name, insurer=req.insurer or "（自填）",
                      type=req.type or "保险", price=req.price or "待补",
                      highlights=json.dumps([h.strip() for h in req.highlights.split(",") if h.strip()],
                                            ensure_ascii=False)))
        db.audit(s, aid, "add_product", req.name)
        s.commit()
        return {"ok": True}
    finally:
        s.close()


# ── 预约 ─────────────────────────────────────────────────────
class ApptReq(BaseModel):
    client: str
    date: str
    time: str = "10:00"
    purpose: str = ""
    channel: str = ""


@app.get("/api/appointments")
def appointments(aid: str = AID):
    s = SessionLocal()
    try:
        rows = [dict(id=a.id, client=a.client, date=a.date, time=a.time,
                     purpose=a.purpose, channel=a.channel, days=db.days_until(a.date))
                for a in db.live(s.query(Appointment), Appointment)
                          .filter_by(agent_id=aid).limit(LIST_CAP + 1)]
        rows.sort(key=lambda r: (r["date"], r["time"]))
        return _capped(rows, "appointments", aid)
    finally:
        s.close()


@app.post("/api/appointments")
def add_appointment(req: ApptReq, aid: str = AID):
    s = SessionLocal()
    try:
        s.add(Appointment(agent_id=aid, client=req.client, date=req.date,
                          time=req.time or "10:00", purpose=req.purpose, channel=req.channel))
        db.audit(s, aid, "add_appt", f"{req.client}@{req.date}")
        s.commit()
        return {"ok": True}
    finally:
        s.close()


# ── 续保 / 加保分析 ──────────────────────────────────────────
@app.get("/api/renewals")
def renewals(aid: str = AID):
    s = SessionLocal()
    try:
        # 一次 join 查完，不再对每个客户各查一次保单（N+1）
        q = (db.live(db.live(s.query(Policy, Client), Policy).join(Client), Client)
             .filter(Client.agent_id == aid)
             .order_by(Policy.renewal))
        rows = [dict(client=c.name, phone=c.phone, product=p.product,
                     policy_no=p.policy_no, renewal=p.renewal,
                     premium=p.premium, days=db.days_until(p.renewal))
                for p, c in q.limit(LIST_CAP + 1).all()]
        return _capped(rows, "renewals", aid)
    finally:
        s.close()


class OppReq(BaseModel):
    client: str = ""
    client_id: int | None = None


@app.post("/api/opportunity")
def opportunity(req: OppReq, aid: str = AID):
    s = SessionLocal()
    try:
        c = (_client_by_id(s, aid, req.client_id) if req.client_id
             else _client_by_name(s, aid, req.client))
        catalog = "\n".join(
            f"- {p.name}（{p.type}，{p.price}）：{'；'.join(json.loads(p.highlights or '[]'))}"
            for p in s.query(Product).filter_by(agent_id=aid))
        profile = json.dumps(db.client_dict(c), ensure_ascii=False)
        prompt = ("你是保险代理人的展业分析助手。根据客户档案和产品目录，分析该客户的保障缺口，"
                  "并提议 1-2 个最值得跟进的加保机会。\n"
                  "输出格式：\n【保障缺口】一两句\n【建议跟进】产品名 + 为什么适合他 + 一句开场话术\n"
                  "务实、简短。只能从产品目录里选，不得编造产品或价格。"
                  "结尾注明：仅供代理人参考，最终以客户需求分析(BNM要求)为准。\n\n"
                  f"客户档案：{profile}\n\n产品目录：\n{catalog}")
        db.audit(s, aid, "opportunity", req.client)
        s.commit()
        return dict(analysis=llm_text(prompt, aid))
    finally:
        s.close()


# ── 删除 / 回收站（软删）──────────────────────────────────────
# 代理人自己删 = 软删（可恢复）；PDPA 的"被遗忘权"由管理员硬删接口彻底清除。
SOFT_KINDS = {"client": Client, "policy": Policy, "appointment": Appointment,
              "fact": Fact, "document": db.Document}
TRASH_KEEP_DAYS = db.TRASH_KEEP_DAYS   # 「可恢复 N 天」的承诺和自动清理必须是同一个数


class DelReq(BaseModel):
    kind: str
    id: int


def _owned(s, aid: str, kind: str, oid: int):
    """取一行并确认属于该代理人。保单没有 agent_id，经 client 反查。"""
    model = SOFT_KINDS.get(kind)
    if model is None:
        raise HTTPException(400, "不支持的类型")
    if kind == "policy":
        row = (s.query(Policy).join(Client)
               .filter(Client.agent_id == aid, Policy.id == oid).first())
    else:
        row = s.query(model).filter_by(agent_id=aid, id=oid).first()
    if not row:
        raise HTTPException(404, "记录不存在")
    return row


def _label(kind: str, row) -> str:
    """回收站里给人看的名字。按类型显式取字段——早先用 getattr 猜，
    Policy.client 是指向 Client 的反向关系，结果把整个客户对象吐给了前端。"""
    if kind == "client":
        return row.name or ""
    if kind == "policy":
        return " ".join(x for x in (row.product, row.policy_no) if x)
    if kind == "appointment":
        return f"{row.date} {row.time} {row.client}".strip()
    if kind == "fact":
        return (row.text or "")[:40]
    if kind == "document":
        return row.filename or ""
    return str(row.id)


@app.post("/api/delete")
def soft_delete(req: DelReq, aid: str = AID):
    s = SessionLocal()
    try:
        row = _owned(s, aid, req.kind, req.id)
        row.deleted = db.now_ts()
        if req.kind == "client":     # 客户连带它的保单一起进回收站
            for p in row.policies:
                p.deleted = p.deleted or row.deleted
        db.audit(s, aid, "delete_" + req.kind, f"id={req.id} {_label(req.kind, row)}")
        s.commit()
        return {"ok": True, "restorable_days": TRASH_KEEP_DAYS}
    finally:
        s.close()


@app.post("/api/restore")
def restore(req: DelReq, aid: str = AID):
    s = SessionLocal()
    try:
        row = _owned(s, aid, req.kind, req.id)
        was = row.deleted
        row.deleted = ""
        if req.kind == "client":
            for p in row.policies:
                if p.deleted == was:      # 只恢复随客户一起删掉的那批
                    p.deleted = ""
        db.audit(s, aid, "restore_" + req.kind, f"id={req.id}")
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.get("/api/trash")
def trash(aid: str = AID):
    s = SessionLocal()
    try:
        out = []
        for kind, model in SOFT_KINDS.items():
            if kind == "policy":
                rows = (s.query(Policy).join(Client)
                        .filter(Client.agent_id == aid, Policy.deleted != "").all())
            else:
                rows = s.query(model).filter(model.agent_id == aid,
                                             model.deleted != "").all()
            out += [dict(kind=kind, id=r.id, label=_label(kind, r), deleted=r.deleted)
                    for r in rows]
        out.sort(key=lambda r: r["deleted"], reverse=True)
        return out
    finally:
        s.close()


class PurgeReq(BaseModel):
    confirm: str = ""      # 必须打一遍客户姓名


@app.post("/api/admin/clients/{cid}/purge")
def admin_purge_client(cid: int, req: PurgeReq = PurgeReq(), adm: str = ADM):
    """PDPA 被遗忘权：彻底删除某客户及其保单、预约、对话。不可恢复。

    **服务端强制二次确认**：必须把客户姓名原样打一遍。只靠前端弹窗不够——
    误调接口、脚本写错一样会删掉。
    """
    s = SessionLocal()
    try:
        c = s.query(Client).filter_by(id=cid).first()
        if not c:
            raise HTTPException(404, "客户不存在")
        if (req.confirm or "").strip() != (c.name or "").strip():
            raise HTTPException(400, f"请把客户姓名原样打一遍确认：{c.name}")
        name, owner = c.name, c.agent_id
        pol = s.query(Policy).filter_by(client_id=c.id).delete()
        appt = (s.query(Appointment)
                .filter_by(agent_id=owner, client=name).delete())
        threads = s.query(Thread).filter_by(agent_id=owner, client=name).all()
        msgs = 0
        for t in threads:
            msgs += s.query(Message).filter_by(thread_id=t.id).delete()
            s.delete(t)
        s.delete(c)
        db.audit(s, adm, "purge_client",
                 f"agent={owner} client={name} policies={pol} appts={appt} "
                 f"threads={len(threads)} msgs={msgs}")
        s.commit()
        return dict(ok=True, purged=dict(policies=pol, appointments=appt,
                                         threads=len(threads), messages=msgs))
    finally:
        s.close()


# ── 认证 ─────────────────────────────────────────────────────
class AuthReq(BaseModel):
    email: str
    password: str


class SetupReq(BaseModel):
    token: str
    password: str = ""


@app.post("/api/auth/setup/check")
def api_setup_check(req: SetupReq):
    """打开设密码页时先校验链接，顺便把账号显示出来。"""
    s = SessionLocal()
    try:
        return auth.peek_setup(s, req.token)
    finally:
        s.close()


@app.post("/api/auth/setup")
def api_setup(req: SetupReq):
    """用一次性链接设密码，成功直接返回登录态。"""
    s = SessionLocal()
    try:
        return auth.consume_setup(s, req.token, req.password)
    finally:
        s.close()


@app.post("/api/auth/login")
def api_login(req: AuthReq):
    s = SessionLocal()
    try:
        return auth.login(s, req.email, req.password)
    finally:
        s.close()


# ── 条款文档上传（PDF/TXT → 分块入库 → 参与检索）───────────────
@app.get("/api/documents")
def list_documents(aid: str = AID):
    s = SessionLocal()
    try:
        return [dict(id=d.id, filename=d.filename, insurer=d.insurer,
                     product=d.product, pages=d.pages, chunks=len(d.chunks))
                for d in db.live(s.query(db.Document), db.Document).filter_by(agent_id=aid)]
    finally:
        s.close()


def _chunk_text(text: str, size: int = 700) -> list[str]:
    parts, buf = [], ""
    for para in text.replace("\r", "").split("\n"):
        if len(buf) + len(para) > size and buf.strip():
            parts.append(buf.strip()); buf = ""
        buf += para + "\n"
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if len(p) > 30]


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...), insurer: str = Form(""),
                          product: str = Form(""), aid: str = AID):
    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED_UPLOAD_EXT):
        raise HTTPException(400, f"只支持 {'/'.join(ALLOWED_UPLOAD_EXT)} 文件")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB}MB 上限")
    pages: list[str] = []
    if name.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(raw))
            pages = [(p.extract_text() or "") for p in reader.pages]
        except Exception:
            log.exception("PDF 解析失败 agent=%s file=%s", aid, file.filename)
            raise HTTPException(400, "PDF 解析失败，请确认文件没有加密或损坏")
    else:
        pages = [raw.decode("utf-8", errors="ignore")]
    s = SessionLocal()
    try:
        doc = db.Document(agent_id=aid, filename=file.filename,
                          insurer=insurer, product=product, pages=len(pages))
        s.add(doc); s.flush()
        n = 0
        for pno, ptext in enumerate(pages, 1):
            for ch in _chunk_text(ptext):
                s.add(db.Chunk(doc_id=doc.id, agent_id=aid, product=product,
                               insurer=insurer, page=pno, text=ch))
                n += 1
        db.audit(s, aid, "upload_doc", f"{file.filename}:{n}chunks")
        s.commit()
        return dict(ok=True, chunks=n, pages=len(pages))
    finally:
        s.close()


# ── Telegram（代理人自己的 bot）──────────────────────────────
class TgConnectReq(BaseModel):
    token: str


@app.get("/api/telegram")
def tg_status(aid: str = AID):
    s = SessionLocal()
    try:
        return telegram.status(s, aid)
    finally:
        s.close()


@app.post("/api/telegram/connect-platform")
def tg_connect_platform(aid: str = AID):
    """一键接入官方共享 bot（无需 token）。"""
    s = SessionLocal()
    try:
        out = telegram.connect_platform(s, aid)
        db.audit(s, aid, "tg_connect_platform", out.get("username", ""))
        s.commit()
        return out
    except telegram.TelegramError as e:
        raise HTTPException(400, str(e))
    finally:
        s.close()


@app.post("/api/telegram/connect")
def tg_connect(req: TgConnectReq, aid: str = AID):
    s = SessionLocal()
    try:
        out = telegram.connect(s, aid, req.token)
        db.audit(s, aid, "tg_connect", out.get("username", ""))
        s.commit()
        return dict(ok=True, **out)
    except telegram.TelegramError as e:
        s.rollback()
        raise HTTPException(400, str(e))
    finally:
        s.close()


@app.post("/api/telegram/disconnect")
def tg_disconnect(aid: str = AID):
    s = SessionLocal()
    try:
        telegram.disconnect(s, aid)
        db.audit(s, aid, "tg_disconnect", "")
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/telegram/bindcode")
def tg_bind_code(aid: str = AID):
    s = SessionLocal()
    try:
        row = s.query(db.TelegramBot).filter_by(agent_id=aid).first()
        if not row:
            raise HTTPException(400, "请先连接你的 bot")
        return dict(code=telegram.new_bind_code(s, aid),
                    username=row.username, ttl_seconds=telegram.BIND_TTL)
    finally:
        s.close()


class TgUnlinkReq(BaseModel):
    id: int


@app.post("/api/telegram/unlink")
def tg_unlink(req: TgUnlinkReq, aid: str = AID):
    s = SessionLocal()
    try:
        telegram.unlink_chat(s, aid, req.id)
        db.audit(s, aid, "tg_unlink", f"row={req.id}")
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/tg/{path_secret}")
async def tg_webhook(path_secret: str, request: Request):
    """Telegram 回调。无登录态——靠路径随机片段 + secret header 认身份。

    永远返回 200：给 Telegram 返错它会不停重投。
    """
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    s = SessionLocal()
    try:
        if path_secret == "platform":
            telegram.handle_platform_update(s, header, update)
        else:
            telegram.handle_update(s, path_secret, header, update)
    except Exception:
        log.exception("Telegram webhook 处理失败")
    finally:
        s.close()
    return {"ok": True}


# ── 管理后台（仅 admin）──────────────────────────────────────
import secrets as _secrets


@app.get("/api/admin/agents")
def admin_agents(_: str = ADM):
    s = SessionLocal()
    try:
        counts = dict(s.query(Client.agent_id, func.count(Client.id))
                      .filter((Client.deleted == "") | (Client.deleted.is_(None)))
                      .group_by(Client.agent_id).all())
        month = db.today().isoformat()[:7]
        used = dict(s.query(db.UsageDaily.agent_id,
                            func.sum(db.UsageDaily.prompt_tokens)
                            + func.sum(db.UsageDaily.completion_tokens))
                    .filter(db.UsageDaily.day.like(month + "%"))
                    .group_by(db.UsageDaily.agent_id).all())
        bots = {b.agent_id: b for b in s.query(db.TelegramBot).all()}

        def _tg(key):
            b = bots.get(key)
            if not b:
                return ""
            return "官方 bot" if (getattr(b, "mode", "own") or "own") == "platform" \
                else "@" + (b.username or "?")

        return [dict(id=a.id, email=a.email, name=a.name, role=a.role,
                     active=bool(a.active), plan=a.plan, expires=a.expires or "",
                     agent_key=a.agent_key, clients=counts.get(a.agent_key, 0),
                     tokens=int(used.get(a.agent_key, 0) or 0),
                     token_quota=a.token_quota or 0, brand=a.brand or "",
                     tg=_tg(a.agent_key),
                     auto_reply=bool(a.auto_reply if a.auto_reply is not None else 1))
                for a in s.query(db.Agent).order_by(db.Agent.id).all()]
    finally:
        s.close()


class ToggleReq(BaseModel):
    active: bool
    confirm: str = ""      # 停用时必须打一遍邮箱；启用不需要


@app.post("/api/admin/agents/{agent_id}/toggle")
def admin_toggle(agent_id: int, req: ToggleReq, adm: str = ADM):
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404)
        if a.role == "admin":
            raise HTTPException(400, "不能停用管理员")
        # 停用会让对方立刻用不了，属于影响生意的操作，要二次确认。启用无害，不用。
        if not req.active and (req.confirm or "").strip().lower() != (a.email or "").lower():
            raise HTTPException(400, f"停用会让对方立刻登不进去。请打一遍邮箱确认：{a.email}")
        a.active = 1 if req.active else 0
        db.audit(s, adm, "toggle_agent", f"{a.email}:{a.active}")
        s.commit()
        return dict(ok=True, active=bool(a.active))
    finally:
        s.close()


class DeleteAgentReq(BaseModel):
    confirm: str = ""      # 必须把邮箱原样打一遍


@app.post("/api/admin/agents/{agent_id}/delete")
def admin_delete_agent(agent_id: int, req: DeleteAgentReq, adm: str = ADM):
    """彻底删除账号和该租户的全部数据，不可恢复。误建重来、试用结束清场时用。
    区别于「停用」：停用保数据可再启用；删除是 PDPA 意义上的清除。"""
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404, "账号不存在")
        if a.role == "admin":
            raise HTTPException(400, "不能删除管理员")
        if (req.confirm or "").strip().lower() != (a.email or "").lower():
            raise HTTPException(400, f"删除不可恢复。请把邮箱原样打一遍确认：{a.email}")
        key, email = a.agent_key, a.email
        # 自建 bot 顺手注销 webhook；平台共享 webhook 内部会跳过，动不得
        telegram.disconnect(s, key)
        cids = [cid for (cid,) in s.query(db.Client.id).filter(db.Client.agent_id == key)]
        if cids:
            s.query(db.Policy).filter(db.Policy.client_id.in_(cids)).delete(synchronize_session=False)
        tids = [tid for (tid,) in s.query(db.Thread.id).filter(db.Thread.agent_id == key)]
        if tids:
            s.query(db.Message).filter(db.Message.thread_id.in_(tids)).delete(synchronize_session=False)
        for M in (db.Client, db.Thread, db.Appointment, db.Fact, db.Product,
                  db.UsageDaily, db.SetupToken, db.Chunk, db.Document, db.Audit):
            s.query(M).filter(M.agent_id == key).delete(synchronize_session=False)
        s.query(db.LoginLock).filter_by(email=email).delete(synchronize_session=False)
        s.delete(a)
        db.audit(s, adm, "delete_agent", email)
        s.commit()
        return dict(ok=True)
    finally:
        s.close()


class AttachBotReq(BaseModel):
    token: str


@app.post("/api/admin/agents/{agent_id}/telegram")
def admin_attach_bot(agent_id: int, req: AttachBotReq, adm: str = ADM):
    """白手套：内部替客户在 BotFather 建好 bot，把 token 挂到他名下。
    客户零操作就得到显示自己公司名的专属 bot（共享官方 bot 做不到改名）。"""
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404, "账号不存在")
        row = s.query(db.TelegramBot).filter_by(agent_id=a.agent_key).first()
        if row is not None and (getattr(row, "mode", "own") or "own") == "platform":
            # 从官方共享 bot 换到专属 bot：设备绑定和客户会话都是跟旧 bot 的对话，
            # 带不过来，清掉让对方用新 bot 重新绑（收件箱里的历史 thread 不动）
            s.query(db.TelegramChat).filter_by(agent_id=a.agent_key).delete()
            s.query(db.TelegramBind).filter_by(agent_id=a.agent_key).delete()
        try:
            info = telegram.connect(s, a.agent_key, req.token)
        except telegram.TelegramError as e:
            raise HTTPException(400, str(e))
        db.audit(s, adm, "admin_attach_bot", f"{a.email}:@{info.get('username', '')}")
        s.commit()
        return {"ok": True, **info}
    finally:
        s.close()


@app.post("/api/admin/agents/{agent_id}/telegram/disconnect")
def admin_detach_bot(agent_id: int, adm: str = ADM):
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404, "账号不存在")
        telegram.disconnect(s, a.agent_key)
        db.audit(s, adm, "admin_detach_bot", a.email)
        s.commit()
        return {"ok": True}
    finally:
        s.close()


class PasswordReq(BaseModel):
    password: str = ""       # 留空 = 发链接让对方自己设（推荐）
    notify: bool = True


@app.post("/api/admin/agents/{agent_id}/password")
def admin_reset_password(agent_id: int, req: PasswordReq, adm: str = ADM):
    """重置代理人密码。代理人忘密码时唯一的自救途径——以前只能手写 SQL。"""
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404, "账号不存在")
        if req.password:
            if len(req.password) < 8:
                raise HTTPException(400, "密码至少 8 位")
            salt = _secrets.token_hex(16)
            a.pw_hash, a.salt = auth.hash_pw(req.password, salt), salt
        db.audit(s, adm, "reset_password", a.email)
        s.commit()
        link = email_out.setup_link(auth.new_setup_token(s, a.agent_key, "reset"))
        sent = bool(req.notify and email_out.password_reset(
            a.email, a.name, link, db.brand_of(a.agent_key)))
        return {"ok": True, "email_sent": sent, "setup_link": link,
                "email_configured": email_out.configured()}
    finally:
        s.close()


class PlanReq(BaseModel):
    plan: str = ""
    expires: str = ""      # YYYY-MM-DD，空 = 不限期


@app.post("/api/admin/agents/{agent_id}/plan")
def admin_set_plan(agent_id: int, req: PlanReq, adm: str = ADM):
    """设套餐和到期日。到期后登录和每个请求都会被挡，不用你记着手动停用。"""
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404, "账号不存在")
        exp = req.expires.strip()
        if exp:
            try:
                dt.date.fromisoformat(exp)
            except ValueError:
                raise HTTPException(400, "到期日格式应为 YYYY-MM-DD")
        a.plan = req.plan.strip() or a.plan
        a.expires = exp
        db.audit(s, adm, "set_plan", f"{a.email}:{a.plan}:{exp or '不限期'}")
        s.commit()
        return dict(ok=True, plan=a.plan, expires=a.expires)
    finally:
        s.close()


@app.get("/api/usage")
def my_usage(aid: str = AID):
    """代理人看自己本月的 AI 用量和剩余额度。"""
    used, limit = db.month_tokens(aid), db.quota_for(aid)
    return dict(month=db.today().isoformat()[:7], tokens=used,
                limit=(None if limit < 0 else limit),
                pct=(None if limit < 0 else round(used / limit * 100) if limit else 0))


@app.get("/api/admin/usage")
def admin_usage(month: str = "", _: str = ADM):
    """按账号汇总的 token 用量与成本 —— 定价和防滥用都靠它。"""
    month = month or db.today().isoformat()[:7]
    s = SessionLocal()
    try:
        rows = (s.query(db.UsageDaily.agent_id,
                        func.sum(db.UsageDaily.prompt_tokens),
                        func.sum(db.UsageDaily.completion_tokens),
                        func.sum(db.UsageDaily.calls))
                .filter(db.UsageDaily.day.like(month + "%"))
                .group_by(db.UsageDaily.agent_id).all())
        emails = dict(s.query(db.Agent.agent_key, db.Agent.email).all())
        quotas = dict(s.query(db.Agent.agent_key, db.Agent.token_quota).all())
        out = []
        for key, inp, outp, calls in rows:
            inp, outp = int(inp or 0), int(outp or 0)
            q = quotas.get(key) or 0
            limit = db.MONTHLY_TOKEN_QUOTA if q == 0 else q
            out.append(dict(agent_key=key, agent=emails.get(key, key),
                            prompt_tokens=inp, completion_tokens=outp,
                            tokens=inp + outp, calls=int(calls or 0),
                            cost_usd=db.cost_usd(inp, outp),
                            limit=(None if limit < 0 else limit)))
        out.sort(key=lambda r: -r["tokens"])
        return dict(month=month, agents=out,
                    total_tokens=sum(r["tokens"] for r in out),
                    total_cost_usd=round(sum(r["cost_usd"] for r in out), 4))
    finally:
        s.close()


class QuotaReq(BaseModel):
    token_quota: int = 0      # 0=用全局默认，-1=不限


@app.post("/api/admin/agents/{agent_id}/quota")
def admin_set_quota(agent_id: int, req: QuotaReq, adm: str = ADM):
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404, "账号不存在")
        a.token_quota = int(req.token_quota)
        db.audit(s, adm, "set_quota", f"{a.email}:{a.token_quota}")
        s.commit()
        return dict(ok=True, token_quota=a.token_quota)
    finally:
        s.close()


@app.get("/api/admin/settings")
def admin_settings(_: str = ADM):
    """管理站需要知道哪些能力已配好，才知道该提示什么。"""
    return dict(email_configured=email_out.configured(),
                email_provider=email_out.provider(),   # resend / smtp / 空
                login_url=email_out.LOGIN_URL,
                telegram_ready=bool(os.environ.get("PUBLIC_BASE_URL")),
                version=VERSION)


class TestMailReq(BaseModel):
    to: str


@app.post("/api/admin/email/test")
def admin_test_email(req: TestMailReq, adm: str = ADM):
    """配完发信通道用它验一下，不用为了试邮件去建个真账号。"""
    if not email_out.configured():
        raise HTTPException(400, "还没配发信通道（RESEND_API_KEY + MAIL_FROM，或 SMTP_HOST + SMTP_FROM）")
    to = req.to.strip()
    if "@" not in to:
        raise HTTPException(400, "收件地址不对")
    how = email_out.provider()
    ok, err = email_out.send_detailed(
        to, "Hivora 邮件配置测试",
        f"能看到这封信，说明发信配好了（通道：{how}）。\n\n"
        "之后管理员创建账号时，代理人会自动收到开通信。\n\n—— Hivora")
    s = SessionLocal()
    try:
        db.audit(s, adm, "test_email", f"{to}:{'sent' if ok else 'failed'}")
        s.commit()
    finally:
        s.close()
    if not ok:
        # 服务商的原话直接给管理员看 —— 翻日志才能知道原因的话，这个按钮就白做了
        # Brevo 默认开 IP 白名单，但 Render 的出站是两个共享 /24 网段（512 个地址），
        # 只加报错里那一个必然时好时坏。加不了 CIDR 就只能把白名单关掉。
        brevo_hint = ("Brevo 开了 IP 白名单，而 Render 的出站是共享网段"
                      "（服务页 Connect → Outbound，形如 74.220.48.0/24），"
                      "不是固定 IP。去 https://app.brevo.com/security/authorised_ips "
                      "加这两个网段；加不了 CIDR 就把白名单关掉 —— "
                      "只加报错里那一个 IP 会时好时坏。"
                      if "IP address" in err else
                      "MAIL_FROM 那个邮箱要先在 Brevo 的 Senders 里验证过"
                      "（它会发一个 6 位验证码到那个邮箱）。")
        hint = {
            "brevo": brevo_hint,
            "resend": "没验证域名的话，Resend 只允许发给你注册它时用的那个邮箱。"
                      "要发给别人，改配 BREVO_API_KEY（只需验证单个发件邮箱，不用域名）。",
        }.get(how, "检查 SMTP_HOST / 端口 / 账号密码。"
                   "注意 Render 免费档封了 25/465/587，此时应改走 HTTP 通道。")
        raise HTTPException(502, f"发送失败（通道：{how}）\n\n"
                                 f"服务商原话：{err or '（无）'}\n\n{hint}")
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(agent_key: str = "", action: str = "", limit: int = 100,
                _: str = ADM):
    """审计日志。谁、什么时候、做了什么——PDPA/BNM 问起来要拿得出。"""
    s = SessionLocal()
    try:
        q = s.query(db.Audit)
        if agent_key:
            q = q.filter(db.Audit.agent_id == agent_key)
        if action:
            q = q.filter(db.Audit.action == action)
        rows = q.order_by(db.Audit.id.desc()).limit(min(limit, 500)).all()
        names = dict(s.query(db.Agent.agent_key, db.Agent.email).all())
        return [dict(id=r.id, ts=r.ts, agent_key=r.agent_id,
                     agent=names.get(r.agent_id, r.agent_id),
                     action=r.action, detail=r.detail) for r in rows]
    finally:
        s.close()


@app.get("/api/admin/audit/export")
def admin_audit_export(agent_key: str = "", action: str = "", limit: int = 5000,
                       _: str = ADM):
    """导出审计日志为 CSV。监管或客户来问的时候要拿得出东西。"""
    import csv
    import io
    s = SessionLocal()
    try:
        q = s.query(db.Audit)
        if agent_key:
            q = q.filter(db.Audit.agent_id == agent_key)
        if action:
            q = q.filter(db.Audit.action == action)
        rows = q.order_by(db.Audit.id.desc()).limit(min(limit, 50000)).all()
        names = dict(s.query(db.Agent.agent_key, db.Agent.email).all())
        buf = io.StringIO()
        buf.write("\ufeff")            # BOM，Excel 打开中文才不乱码
        w = csv.writer(buf)
        w.writerow(["id", "时间", "账号", "agent_key", "动作", "详情"])
        for r in rows:
            w.writerow([r.id, r.ts, names.get(r.agent_id, ""), r.agent_id,
                        r.action, r.detail])
        stamp = db.today().isoformat()
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode("utf-8")), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="hivora-audit-{stamp}.csv"'})
    finally:
        s.close()


@app.get("/api/admin/clients")
def admin_clients(agent_key: str, _: str = ADM):
    """按代理人列出客户——PDPA 删除请求要先找到人。"""
    s = SessionLocal()
    try:
        return [dict(id=c.id, name=c.name, phone=c.phone,
                     deleted=c.deleted or "",
                     policies=sum(1 for p in c.policies if not p.deleted))
                for c in s.query(Client).filter_by(agent_id=agent_key)
                         .order_by(Client.id).all()]
    finally:
        s.close()


class CreateAgentReq(BaseModel):
    email: str
    name: str = ""
    plan: str = "paid"
    brand: str = ""          # 白牌：公司名
    password: str = ""       # 留空 = 发链接让对方自己设（推荐）


@app.post("/api/admin/agents/create")
def admin_create_agent(req: CreateAgentReq, adm: str = ADM):
    import secrets as _s
    s = SessionLocal()
    try:
        email = req.email.strip().lower()
        if not email or "@" not in email:
            raise HTTPException(400, "请填一个有效的邮箱")
        if req.password and len(req.password) < 8:
            raise HTTPException(400, "密码至少 8 位")
        if s.query(db.Agent).filter_by(email=email).first():
            raise HTTPException(400, "该账号已存在")
        salt = _s.token_hex(16)
        name = req.name or email.split("@")[0]
        key = "ag_" + _s.token_hex(8)
        # 不给密码时塞一个随机的：账号先不可登录，等对方点链接自己设
        s.add(db.Agent(agent_key=key, email=email,
                       name=name, brand=req.brand.strip()[:120],
                       pw_hash=auth.hash_pw(req.password or _s.token_urlsafe(32), salt),
                       salt=salt, plan=req.plan or "paid"))
        db.audit(s, adm, "admin_create_agent", email)
        s.commit()

        link = email_out.setup_link(auth.new_setup_token(s, key, "welcome"))
        # 账号已经建好了。发信只是通知手段，发不出去也不能让这个接口失败——
        # 管理站会把链接显示出来让管理员手动发。
        sent = email_out.welcome(email, name, link, db.brand_of(key))
        db.audit(s, adm, "welcome_email", f"{email}:{'sent' if sent else 'skipped'}")
        s.commit()
        return {"ok": True, "email_sent": sent, "setup_link": link,
                "email_configured": email_out.configured()}
    finally:
        s.close()
