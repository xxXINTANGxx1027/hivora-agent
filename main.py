"""Hivora Insurance Agent —— 生产 API（FastAPI + LangGraph + SQLAlchemy）。"""
import datetime as dt
import json
import logging
import os
import pathlib

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import auth
import db
from db import Appointment, Client, Fact, Message, Policy, Product, SessionLocal, Thread
from graph import MODEL, LLMUnavailable, ask, ask_stream, llm_text
from knowledge import search_policy_chunks

BASE_DIR = pathlib.Path(__file__).resolve().parent
log = logging.getLogger("hivora")

app = FastAPI(title="Hivora Insurance Agent")

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
AID = Depends(auth.current_agent)
ADM = Depends(auth.current_admin)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
ALLOWED_UPLOAD_EXT = (".pdf", ".txt", ".md")


def now_hm():
    return dt.datetime.now().strftime("%H:%M")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": MODEL}


# ── Copilot ───────────────────────────────────────────────────
class ChatReq(BaseModel):
    message: str


@app.exception_handler(LLMUnavailable)
def _llm_unavailable(request, exc):
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.post("/api/chat")
def chat(req: ChatReq, aid: str = AID):
    """整段返回。前端默认走 /api/chat/stream，这个保留给非流式调用方和测试。"""
    try:
        return ask(req.message, aid)
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
        clients = db.live(s.query(Client), Client).filter_by(agent_id=aid).count()
        pols = db.live(db.live(s.query(Policy), Policy).join(Client), Client).filter(
            Client.agent_id == aid).all()
        renew30 = sum(1 for p in pols if 0 <= db.days_until(p.renewal) <= 30)
        threads = s.query(Thread).filter_by(agent_id=aid).all()
        pending = sum(1 for t in threads if t.status != "sent")
        ai_rate = round(sum(1 for t in threads if t.status == "sent") / len(threads) * 100) if threads else 0
        appts7 = sum(1 for a in db.live(s.query(Appointment), Appointment).filter_by(agent_id=aid)
                     if 0 <= db.days_until(a.date) <= 7)
        facts = db.live(s.query(Fact), Fact).filter_by(agent_id=aid).count()
        return dict(clients=clients, policies=len(pols), renewals_30d=renew30,
                    pending_replies=pending, ai_handled_pct=ai_rate,
                    facts=facts, appts_7d=appts7,
                    today=db.today().isoformat(), model=MODEL)
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
                unread=t.unread, suggestions=json.loads(t.suggestions or "[]"),
                messages=[dict(role=m.role, text=m.text, ts=m.ts) for m in t.messages])


@app.get("/api/inbox/{tid}")
def thread(tid: int, aid: str = AID):
    s = SessionLocal()
    try:
        return _thread_dict(_thread(s, tid, aid))
    finally:
        s.close()


def _ctx_for(s, t, aid):
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
        raw = llm_text(prompt).strip()
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
        return dict(ok=True)
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
        return [db.client_dict(c)
                for c in db.live(s.query(Client), Client).filter_by(agent_id=aid)]
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
                for a in db.live(s.query(Appointment), Appointment).filter_by(agent_id=aid)]
        rows.sort(key=lambda r: (r["date"], r["time"]))
        return rows
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
        rows = []
        for c in db.live(s.query(Client), Client).filter_by(agent_id=aid):
            for p in (x for x in c.policies if not x.deleted):
                rows.append(dict(client=c.name, phone=c.phone, product=p.product,
                                 policy_no=p.policy_no, renewal=p.renewal,
                                 premium=p.premium, days=db.days_until(p.renewal)))
        rows.sort(key=lambda r: r["days"])
        return rows
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
        return dict(analysis=llm_text(prompt))
    finally:
        s.close()


# ── 删除 / 回收站（软删）──────────────────────────────────────
# 代理人自己删 = 软删（可恢复）；PDPA 的"被遗忘权"由管理员硬删接口彻底清除。
SOFT_KINDS = {"client": Client, "policy": Policy, "appointment": Appointment,
              "fact": Fact, "document": db.Document}
TRASH_KEEP_DAYS = int(os.environ.get("TRASH_KEEP_DAYS", "30"))


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


@app.post("/api/admin/clients/{cid}/purge")
def admin_purge_client(cid: int, adm: str = ADM):
    """PDPA 被遗忘权：彻底删除某客户及其保单、预约、对话。不可恢复。"""
    s = SessionLocal()
    try:
        c = s.query(Client).filter_by(id=cid).first()
        if not c:
            raise HTTPException(404, "客户不存在")
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
    name: str = ""
    code: str = ""


@app.post("/api/auth/register")
def api_register(req: AuthReq):
    s = SessionLocal()
    try:
        return auth.register(s, req.email, req.password, req.name, req.code)
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


# ── 管理后台（仅 admin）──────────────────────────────────────
import secrets as _secrets


@app.get("/api/admin/agents")
def admin_agents(_: str = ADM):
    s = SessionLocal()
    try:
        return [dict(id=a.id, email=a.email, name=a.name, role=a.role,
                     active=bool(a.active), plan=a.plan, expires=a.expires or "")
                for a in s.query(db.Agent).all()]
    finally:
        s.close()


@app.get("/api/admin/invites")
def admin_invites(_: str = ADM):
    s = SessionLocal()
    try:
        return [dict(code=i.code, used_by=i.used_by, created=i.created, used=i.used)
                for i in s.query(db.InviteCode).order_by(db.InviteCode.id.desc()).limit(30)]
    finally:
        s.close()


@app.post("/api/admin/invites")
def admin_make_invite(adm: str = ADM):
    import datetime as _dt
    s = SessionLocal()
    try:
        code = "HIV-" + _secrets.token_hex(3).upper()
        s.add(db.InviteCode(code=code, created_by=adm,
                            created=_dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
        db.audit(s, adm, "make_invite", code)
        s.commit()
        return dict(code=code)
    finally:
        s.close()


class ToggleReq(BaseModel):
    active: bool


@app.post("/api/admin/agents/{agent_id}/toggle")
def admin_toggle(agent_id: int, req: ToggleReq, adm: str = ADM):
    s = SessionLocal()
    try:
        a = s.query(db.Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(404)
        if a.role == "admin":
            raise HTTPException(400, "不能停用管理员")
        a.active = 1 if req.active else 0
        db.audit(s, adm, "toggle_agent", f"{a.email}:{a.active}")
        s.commit()
        return dict(ok=True, active=bool(a.active))
    finally:
        s.close()


class CreateAgentReq(BaseModel):
    email: str
    password: str
    name: str = ""
    plan: str = "paid"


@app.post("/api/admin/agents/create")
def admin_create_agent(req: CreateAgentReq, adm: str = ADM):
    import secrets as _s
    s = SessionLocal()
    try:
        email = req.email.strip().lower()
        if not email or len(req.password) < 8:
            raise HTTPException(400, "账号必填，密码至少 8 位")
        if s.query(db.Agent).filter_by(email=email).first():
            raise HTTPException(400, "该账号已存在")
        salt = _s.token_hex(16)
        s.add(db.Agent(agent_key="ag_" + _s.token_hex(8), email=email,
                       name=req.name or email.split("@")[0],
                       pw_hash=auth.hash_pw(req.password, salt), salt=salt,
                       plan=req.plan or "paid"))
        db.audit(s, adm, "admin_create_agent", email)
        s.commit()
        return {"ok": True}
    finally:
        s.close()
