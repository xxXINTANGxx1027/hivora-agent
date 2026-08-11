"""数据层：SQLAlchemy。本地 SQLite，云端设 DATABASE_URL 即用 Postgres。"""
import datetime as dt
import json
import os

from sqlalchemy import (Column, Date, ForeignKey, Integer, String, Text,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///hivora.db")
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="agent_demo")
    name = Column(String(200), nullable=False)
    phone = Column(String(64), default="")
    notes = Column(Text, default="")
    policies = relationship("Policy", cascade="all,delete", backref="client")


class Policy(Base):
    __tablename__ = "policies"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    product = Column(String(200), nullable=False)
    policy_no = Column(String(100), default="待补")
    premium = Column(String(64), default="待补")
    renewal = Column(String(10), default="")   # YYYY-MM-DD
    status = Column(String(32), default="有效")


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="agent_demo")
    client = Column(String(200), default="")
    date = Column(String(10), default="")
    time = Column(String(5), default="10:00")
    purpose = Column(Text, default="")
    channel = Column(String(200), default="")


class Fact(Base):
    __tablename__ = "facts"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="agent_demo")
    text = Column(Text, nullable=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    insurer = Column(String(200), default="")
    type = Column(String(64), default="保险")
    price = Column(String(120), default="待补")
    highlights = Column(Text, default="[]")   # JSON list


class Thread(Base):
    __tablename__ = "threads"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="agent_demo")
    client = Column(String(200), default="")
    lang = Column(String(32), default="中文")
    status = Column(String(16), default="pending")   # pending/drafted/sent
    mode = Column(String(8), default="ai")           # ai/human
    unread = Column(Integer, default=0)
    suggestions = Column(Text, default="[]")         # JSON list
    messages = relationship("Message", cascade="all,delete", backref="thread",
                            order_by="Message.id")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    thread_id = Column(Integer, ForeignKey("threads.id"), index=True)
    role = Column(String(16))    # customer/agent
    text = Column(Text)
    ts = Column(String(20), default="")


class Audit(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), default="agent_demo")
    action = Column(String(64))
    detail = Column(Text, default="")
    ts = Column(String(24), default="")


def today() -> dt.date:
    return dt.date.today()


def days_until(date_s: str) -> int:
    try:
        y, m, d = map(int, date_s.split("-"))
        return (dt.date(y, m, d) - today()).days
    except (ValueError, AttributeError):
        return 9999


def audit(s, action, detail=""):
    s.add(Audit(action=action, detail=str(detail)[:500],
                ts=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def client_dict(c: Client) -> dict:
    return dict(id=c.id, name=c.name, phone=c.phone, notes=c.notes,
                policies=[dict(product=p.product, policy_no=p.policy_no,
                               premium=p.premium, renewal=p.renewal,
                               status=p.status, days=days_until(p.renewal))
                          for p in c.policies])


def seed_if_empty():
    Base.metadata.create_all(engine)
    s = SessionLocal()
    try:
        if s.query(Client).first():
            return
        y = today().year
        m = f"{today().month:02d}"
        c1 = Client(name="张伟明 (Teoh Wei Ming)", phone="+60 12-345 6789",
                    notes="两个孩子（8岁、5岁），太太无收入，家庭唯一经济支柱。缺重疾险。")
        c1.policies = [Policy(product="MediShield Plus", policy_no="MSP-2023-08812",
                              premium="RM 245/月", renewal=f"{y}-05-01"),
                       Policy(product="FamilyGuard Term Life", policy_no="FGT-2024-01277",
                              premium="RM 180/月", renewal=f"{y}-02-15")]
        c2 = Client(name="Nurul Aisyah", phone="+60 17-888 2244",
                    notes="单身，30 岁，IT 工程师。只有医疗险，无寿险/意外险。")
        c2.policies = [Policy(product="CarePlus 360", policy_no="CP3-2025-00341",
                              premium="RM 320/月", renewal=f"{y}-09-01")]
        c3 = Client(name="Lim Mei Ling", phone="+60 16-777 9911",
                    notes=f"45 岁，自雇（餐饮）。保单 {y}-08-20 到期，需尽快联系续保。")
        c3.policies = [Policy(product="MediShield Plus", policy_no="MSP-2022-04455",
                              premium="RM 210/月", renewal=f"{y}-08-20")]
        s.add_all([c1, c2, c3])
        s.add_all([
            Product(name="MediShield Plus", insurer="示例人寿 (Demo Life)", type="医疗险",
                    price="RM 180 – 420/月（视年龄/计划）",
                    highlights=json.dumps(["年度限额 RM 1.5M，终身无限额", "病房 R&B RM 250/日",
                        "一般等待期 30 天 / 特定疾病 120 天", "已存在疾病首 24 个月不保"], ensure_ascii=False)),
            Product(name="CarePlus 360", insurer="示例人寿 (Demo Life)", type="医疗险",
                    price="RM 250 – 520/月（视年龄/计划）",
                    highlights=json.dumps(["年度限额 RM 2M + 体检津贴 RM 500/年", "病房 RM 400/日",
                        "等待期 60 天 / 特定疾病 180 天", "已存在疾病首 12 个月不保"], ensure_ascii=False)),
            Product(name="FamilyGuard Term Life", insurer="示例保险 (Demo Assurance)", type="定期寿险",
                    price="RM 90 – 380/月（视保额/年期）",
                    highlights=json.dumps(["保额 RM 100K – 2M，10/20/30 年期", "身故/TPD 赔全额",
                        "TPD 保障至 65 岁", "适合家庭支柱做收入保障"], ensure_ascii=False)),
        ])
        s.add_all([
            Appointment(client="Lim Mei Ling", date=f"{y}-{m}-12", time="14:30",
                        purpose="续保面谈（MediShield 8/20 到期）", channel="面谈 · Kopitiam Puchong"),
            Appointment(client="Nurul Aisyah", date=f"{y}-{m}-14", time="20:00",
                        purpose="解答住院保障疑问", channel="WhatsApp 通话"),
        ])
        s.add_all([
            Fact(text="MediShield Plus 月缴保费从 RM 180 起，视年龄和计划而定"),
            Fact(text="我的服务时间：周一至周六 9am-7pm，周日紧急事项可 WhatsApp"),
            Fact(text="理赔需要的基本材料：身份证副本、医生报告、原始收据、理赔表格"),
        ])
        t1 = Thread(client="Lim Mei Ling", lang="中文", unread=2)
        t1.messages = [Message(role="customer", text="Hi，我的 MediShield Plus 是不是快到期了？", ts="10:02"),
                       Message(role="customer", text="续保的话保费会涨吗？", ts="10:03")]
        t2 = Thread(client="Nurul Aisyah", lang="English", unread=1)
        t2.messages = [Message(role="customer",
            text="Hi! Quick question — if I admit to hospital next week for a minor surgery, "
                 "is my CarePlus 360 already active? I bought it last September.", ts="09:41")]
        t3 = Thread(client="张伟明 (Teoh Wei Ming)", lang="中文", status="sent")
        t3.messages = [Message(role="customer", text="上次说的理赔材料我准备好了，接下来怎么做？", ts="昨天"),
                       Message(role="agent",
            text="好的伟明，材料齐了就交给我：身份证副本、医生报告、原始收据和理赔表格。"
                 "我今天帮你提交，一般 7-14 个工作日出结果，有消息我第一时间告诉你 👍", ts="昨天")]
        s.add_all([t1, t2, t3])
        s.commit()
    finally:
        s.close()
