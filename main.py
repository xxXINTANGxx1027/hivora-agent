"""Hivora Insurance Agent —— 生产 API（FastAPI + LangGraph + SQLAlchemy）。"""
import datetime as dt
import json
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import auth
import db
from db import Appointment, Client, Fact, Message, Policy, Product, SessionLocal, Thread
from graph import MODEL, ask, llm
from knowledge import search_policy_chunks

app = FastAPI(title="Hivora Insurance Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
db.ensure_schema()
db.seed_if_empty()
auth.ensure_demo_agent()
AID = Depends(auth.current_agent)


def now_hm():
    return dt.datetime.now().strftime("%H:%M")


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": MODEL}


# ── Copilot ───────────────────────────────────────────────────
class ChatReq(BaseModel):
    message: str


@app.post("/api/chat")
def chat(req: ChatReq, aid: str = AID):
    try:
        return ask(req.message, aid)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── Dashboard ─────────────────────────────────────────────────
@app.get("/api/dashboard")
def dashboard(aid: str = AID):
    s = SessionLocal()
    try:
        clients = s.query(Client).filter_by(agent_id=aid).count()
        pols = s.query(Policy).join(Client).filter(Client.agent_id == aid).all()
        renew30 = sum(1 for p in pols if 0 <= db.days_until(p.renewal) <= 30)
        threads = s.query(Thread).filter_by(agent_id=aid).all()
        pending = sum(1 for t in threads if t.status != "sent")
        ai_rate = round(sum(1 for t in threads if t.status == "sent") / len(threads) * 100) if threads else 0
        appts7 = sum(1 for a in s.query(Appointment).filter_by(agent_id=aid)
                     if 0 <= db.days_until(a.date) <= 7)
        facts = s.query(Fact).filter_by(agent_id=aid).count()
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
    client = (s.query(Client).filter_by(agent_id=aid)
              .filter(Client.name == t.client).first())
    convo = "\n".join(f"{m.role}: {m.text}" for m in t.messages[-5:])
    q = t.messages[-1].text if t.messages else ""
    chunks = search_policy_chunks(q, aid)
    ctx = "\n".join(f"- {c['product']} 第{c['page']}页: {c['text']}" for c in chunks)
    facts = "\n".join(f"- {f.text}" for f in s.query(Fact).filter_by(agent_id=aid))
    ci = ""
    if client:
        pols = "; ".join(f"{p.product}({p.policy_no}, 续保 {p.renewal}, {p.premium})"
                         for p in client.policies)
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
        raw = llm.invoke(prompt).content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            sugg = [x for x in json.loads(raw) if isinstance(x, str)][:3]
        except json.JSONDecodeError:
            sugg = [raw]
        t.suggestions = json.dumps(sugg, ensure_ascii=False)
        t.status = "drafted"
        db.audit(s, "suggest", f"thread={tid}")
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
        db.audit(s, "send", f"thread={tid}")
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
        return {"facts": [f.text for f in s.query(Fact).filter_by(agent_id=aid)]}
    finally:
        s.close()


@app.post("/api/facts")
def add_fact(req: FactReq, aid: str = AID):
    s = SessionLocal()
    try:
        if req.text.strip():
            s.add(Fact(agent_id=aid, text=req.text.strip()))
            db.audit(s, "teach", req.text[:100])
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
    orig: str


class PolicyReq(BaseModel):
    client: str
    product: str
    policy_no: str = ""
    premium: str = ""
    renewal: str = ""


class PolicyUpdateReq(PolicyReq):
    orig_no: str


@app.get("/api/clients")
def clients(aid: str = AID):
    s = SessionLocal()
    try:
        return [db.client_dict(c) for c in s.query(Client).filter_by(agent_id=aid)]
    finally:
        s.close()


@app.post("/api/clients")
def add_client(req: ClientReq, aid: str = AID):
    s = SessionLocal()
    try:
        s.add(Client(agent_id=aid, name=req.name, phone=req.phone, notes=req.notes))
        db.audit(s, "add_client", req.name)
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/clients/update")
def update_client(req: ClientUpdateReq, aid: str = AID):
    s = SessionLocal()
    try:
        c = s.query(Client).filter_by(agent_id=aid, name=req.orig).first()
        if not c:
            raise HTTPException(404)
        c.name, c.phone, c.notes = req.name, req.phone, req.notes
        db.audit(s, "edit_client", req.orig)
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/policies")
def add_policy(req: PolicyReq, aid: str = AID):
    s = SessionLocal()
    try:
        c = (s.query(Client).filter_by(agent_id=aid)
             .filter(Client.name.contains(req.client)).first())
        if not c:
            return {"ok": False, "msg": f"没找到客户 {req.client}，请先新增客户"}
        s.add(Policy(client_id=c.id, product=req.product,
                     policy_no=req.policy_no or "待补", premium=req.premium or "待补",
                     renewal=req.renewal or ""))
        db.audit(s, "add_policy", f"{c.name}:{req.product}")
        s.commit()
        return {"ok": True, "msg": "已加"}
    finally:
        s.close()


@app.post("/api/policies/update")
def update_policy(req: PolicyUpdateReq, aid: str = AID):
    s = SessionLocal()
    try:
        c = s.query(Client).filter_by(agent_id=aid, name=req.client).first()
        if not c:
            raise HTTPException(404)
        for p in c.policies:
            if p.policy_no == req.orig_no:
                p.product = req.product
                p.policy_no = req.policy_no or p.policy_no
                p.premium = req.premium or p.premium
                p.renewal = req.renewal or p.renewal
                db.audit(s, "edit_policy", req.orig_no)
                s.commit()
                return {"ok": True}
        raise HTTPException(404)
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
                for p in s.query(Product).all()]
    finally:
        s.close()


@app.post("/api/products")
def add_product(req: ProductReq, aid: str = AID):
    s = SessionLocal()
    try:
        s.add(Product(name=req.name, insurer=req.insurer or "（自填）",
                      type=req.type or "保险", price=req.price or "待补",
                      highlights=json.dumps([h.strip() for h in req.highlights.split(",") if h.strip()],
                                            ensure_ascii=False)))
        db.audit(s, "add_product", req.name)
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
                for a in s.query(Appointment).filter_by(agent_id=aid)]
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
        db.audit(s, "add_appt", f"{req.client}@{req.date}")
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
        for c in s.query(Client).filter_by(agent_id=aid):
            for p in c.policies:
                rows.append(dict(client=c.name, phone=c.phone, product=p.product,
                                 policy_no=p.policy_no, renewal=p.renewal,
                                 premium=p.premium, days=db.days_until(p.renewal)))
        rows.sort(key=lambda r: r["days"])
        return rows
    finally:
        s.close()


class OppReq(BaseModel):
    client: str


@app.post("/api/opportunity")
def opportunity(req: OppReq, aid: str = AID):
    s = SessionLocal()
    try:
        c = s.query(Client).filter_by(agent_id=aid, name=req.client).first()
        if not c:
            raise HTTPException(404)
        catalog = "\n".join(
            f"- {p.name}（{p.type}，{p.price}）：{'；'.join(json.loads(p.highlights or '[]'))}"
            for p in s.query(Product).all())
        profile = json.dumps(db.client_dict(c), ensure_ascii=False)
        prompt = ("你是保险代理人的展业分析助手。根据客户档案和产品目录，分析该客户的保障缺口，"
                  "并提议 1-2 个最值得跟进的加保机会。\n"
                  "输出格式：\n【保障缺口】一两句\n【建议跟进】产品名 + 为什么适合他 + 一句开场话术\n"
                  "务实、简短。只能从产品目录里选，不得编造产品或价格。"
                  "结尾注明：仅供代理人参考，最终以客户需求分析(BNM要求)为准。\n\n"
                  f"客户档案：{profile}\n\n产品目录：\n{catalog}")
        db.audit(s, "opportunity", req.client)
        s.commit()
        return dict(analysis=llm.invoke(prompt).content)
    finally:
        s.close()


# ── 认证 ─────────────────────────────────────────────────────
class AuthReq(BaseModel):
    email: str
    password: str
    name: str = ""


@app.post("/api/auth/register")
def api_register(req: AuthReq):
    s = SessionLocal()
    try:
        return auth.register(s, req.email, req.password, req.name)
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
                for d in s.query(db.Document).filter_by(agent_id=aid)]
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
    raw = await file.read()
    pages: list[str] = []
    if file.filename.lower().endswith(".pdf"):
        import io
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(raw))
            pages = [(p.extract_text() or "") for p in reader.pages]
        except Exception as e:
            raise HTTPException(400, f"PDF 解析失败：{e}")
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
        db.audit(s, "upload_doc", f"{file.filename}:{n}chunks")
        s.commit()
        return dict(ok=True, chunks=n, pages=len(pages))
    finally:
        s.close()
