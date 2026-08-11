"""数据层：SQLAlchemy。本地 SQLite，云端设 DATABASE_URL 即用 Postgres。"""
import datetime as dt
import json
import os

from sqlalchemy import (Column, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, func)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

IS_PROD = os.environ.get("HIVORA_ENV", "").strip().lower() in ("prod", "production")
# 演示数据（假客户/假产品/内置示例条款/demo 账号）：生产默认关闭。
DEMO_DATA = os.environ.get("DEMO_DATA", "0" if IS_PROD else "1").strip() == "1"
DEMO_AGENT = "agent_demo"

_HERE = os.path.dirname(os.path.abspath(__file__))
# 绝对路径：不再依赖进程 CWD（原来靠 main.py 里的 os.chdir 兜底，换个启动方式就会
# 悄悄新建一个空库）。
DB_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(_HERE, 'hivora.db')}")
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
if IS_PROD and DB_URL.startswith("sqlite"):
    raise RuntimeError(
        "生产环境未设置 DATABASE_URL：SQLite 落在临时磁盘上，容器重启/重新部署会丢光所有数据。"
        "请在 Render Environment 里贴 Neon Postgres 连接串。")
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
    deleted = Column(String(24), default="")   # 软删：空=有效，非空=删除时间
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
    deleted = Column(String(24), default="")   # 软删：空=有效，非空=删除时间


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="agent_demo")
    client = Column(String(200), default="")
    date = Column(String(10), default="")
    time = Column(String(5), default="10:00")
    purpose = Column(Text, default="")
    channel = Column(String(200), default="")
    deleted = Column(String(24), default="")   # 软删：空=有效，非空=删除时间


class Fact(Base):
    __tablename__ = "facts"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="agent_demo")
    text = Column(Text, nullable=False)
    deleted = Column(String(24), default="")   # 软删：空=有效，非空=删除时间


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="")   # 多租户隔离
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


class UsageDaily(Base):
    """按 账号 × 日期 × 模型 汇总的 token 用量。

    以前完全不知道一个客户成本多少 —— 没法定价，也拦不住滥用。
    """
    __tablename__ = "usage_daily"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True)
    day = Column(String(10), index=True)        # YYYY-MM-DD
    model = Column(String(120), default="")
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    calls = Column(Integer, default=0)
    __table_args__ = (UniqueConstraint("agent_id", "day", "model", name="uq_usage_day"),)


class LoginLock(Base):
    """登录失败计数。落库而不是放进程内存 —— 否则扩到第二个实例就形同虚设。"""
    __tablename__ = "login_locks"
    email = Column(String(200), primary_key=True)
    fails = Column(Integer, default=0)
    until = Column(Float, default=0.0)          # unix 时间戳


class Audit(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True, default="")
    action = Column(String(64))
    detail = Column(Text, default="")
    ts = Column(String(24), default="")


def live(q, model):
    """过滤掉软删的行。所有面向用户的读取都必须包一层。"""
    return q.filter((model.deleted == "") | (model.deleted.is_(None)))


def now_ts() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> dt.date:
    return dt.date.today()


def days_until(date_s: str) -> int:
    try:
        y, m, d = map(int, date_s.split("-"))
        return (dt.date(y, m, d) - today()).days
    except (ValueError, AttributeError):
        return 9999


MONTHLY_TOKEN_QUOTA = int(os.environ.get("MONTHLY_TOKEN_QUOTA", "2000000"))
# 每百万 token 的价格（美元）。跟着你实际用的模型改，默认按 DeepSeek v3.2 量级。
PRICE_IN = float(os.environ.get("PRICE_PER_MTOK_IN", "0.28"))
PRICE_OUT = float(os.environ.get("PRICE_PER_MTOK_OUT", "0.42"))


def record_usage(agent_id: str, model: str, prompt_tokens: int, completion_tokens: int):
    """累加用量。失败不能影响主流程——记账挂了也不该让用户的请求失败。"""
    if not agent_id:
        return
    import logging
    s = None
    try:
        # 建 session 本身也可能失败（数据库不通），所以整段都要包住：
        # 记账是附带动作，任何情况下都不该把异常抛给用户的请求。
        s = SessionLocal()
        day = today().isoformat()
        row = (s.query(UsageDaily)
               .filter_by(agent_id=agent_id, day=day, model=model or "").first())
        if row is None:
            row = UsageDaily(agent_id=agent_id, day=day, model=model or "")
            s.add(row)
        row.prompt_tokens = (row.prompt_tokens or 0) + max(0, prompt_tokens)
        row.completion_tokens = (row.completion_tokens or 0) + max(0, completion_tokens)
        row.calls = (row.calls or 0) + 1
        s.commit()
    except Exception:
        logging.getLogger("hivora.usage").warning("记账失败 agent=%s", agent_id, exc_info=True)
        if s is not None:
            try:
                s.rollback()
            except Exception:
                pass
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def month_tokens(agent_id: str, month: str = "") -> int:
    """某账号本月已用的 token 总数。"""
    month = month or today().isoformat()[:7]
    s = SessionLocal()
    try:
        row = (s.query(func.coalesce(func.sum(UsageDaily.prompt_tokens), 0)
                       + func.coalesce(func.sum(UsageDaily.completion_tokens), 0))
               .filter(UsageDaily.agent_id == agent_id,
                       UsageDaily.day.like(month + "%")).scalar())
        return int(row or 0)
    finally:
        s.close()


def quota_for(agent_id: str) -> int:
    """该账号的月度上限。-1 表示不限。"""
    s = SessionLocal()
    try:
        a = s.query(Agent).filter_by(agent_key=agent_id).first()
        q = (a.token_quota if a and a.token_quota is not None else 0)
        return MONTHLY_TOKEN_QUOTA if q == 0 else q
    finally:
        s.close()


def cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return round(prompt_tokens / 1e6 * PRICE_IN + completion_tokens / 1e6 * PRICE_OUT, 4)


def audit(s, agent_id, action, detail=""):
    """审计日志。agent_id 必填——只能来自 auth.current_agent，不接受请求体传入。"""
    s.add(Audit(agent_id=agent_id or "unknown", action=action, detail=str(detail)[:500],
                ts=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def client_dict(c: Client) -> dict:
    return dict(id=c.id, name=c.name, phone=c.phone, notes=c.notes,
                policies=[dict(id=p.id, product=p.product, policy_no=p.policy_no,
                               premium=p.premium, renewal=p.renewal,
                               status=p.status, days=days_until(p.renewal))
                          for p in c.policies if not p.deleted])


def seed_if_empty():
    """仅在 DEMO_DATA=1 时灌演示数据（假客户/假产品）。生产默认不灌。"""
    Base.metadata.create_all(engine)
    if not DEMO_DATA:
        return
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
            Product(agent_id=DEMO_AGENT, name="MediShield Plus", insurer="示例人寿 (Demo Life)", type="医疗险",
                    price="RM 180 – 420/月（视年龄/计划）",
                    highlights=json.dumps(["年度限额 RM 1.5M，终身无限额", "病房 R&B RM 250/日",
                        "一般等待期 30 天 / 特定疾病 120 天", "已存在疾病首 24 个月不保"], ensure_ascii=False)),
            Product(agent_id=DEMO_AGENT, name="CarePlus 360", insurer="示例人寿 (Demo Life)", type="医疗险",
                    price="RM 250 – 520/月（视年龄/计划）",
                    highlights=json.dumps(["年度限额 RM 2M + 体检津贴 RM 500/年", "病房 RM 400/日",
                        "等待期 60 天 / 特定疾病 180 天", "已存在疾病首 12 个月不保"], ensure_ascii=False)),
            Product(agent_id=DEMO_AGENT, name="FamilyGuard Term Life", insurer="示例保险 (Demo Assurance)", type="定期寿险",
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


class Agent(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True)
    agent_key = Column(String(64), unique=True, index=True)
    email = Column(String(200), unique=True, index=True)
    name = Column(String(200), default="")
    pw_hash = Column(String(200))
    salt = Column(String(64))
    role = Column(String(16), default="agent")      # admin / agent
    active = Column(Integer, default=1)
    plan = Column(String(32), default="trial")
    expires = Column(String(10), default="")        # YYYY-MM-DD，空=不限
    token_quota = Column(Integer, default=0)        # 月度 token 上限；0=用全局默认，-1=不限


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), index=True)
    filename = Column(String(300))
    insurer = Column(String(200), default="")
    product = Column(String(200), default="")
    pages = Column(Integer, default=0)
    deleted = Column(String(24), default="")   # 软删：空=有效，非空=删除时间
    chunks = relationship("Chunk", cascade="all,delete", backref="document")


class Chunk(Base):
    __tablename__ = "chunks"
    id = Column(Integer, primary_key=True)
    doc_id = Column(Integer, ForeignKey("documents.id"), index=True)
    agent_id = Column(String(64), index=True)
    product = Column(String(200), default="")
    insurer = Column(String(200), default="")
    page = Column(Integer, default=0)
    text = Column(Text)


def ensure_schema():
    Base.metadata.create_all(engine)


class InviteCode(Base):
    __tablename__ = "invite_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, index=True)
    created_by = Column(String(64), default="")
    used_by = Column(String(200), default="")     # email
    created = Column(String(24), default="")
    used = Column(String(24), default="")


def migrate_columns():
    """轻量迁移：给已存在的表补新列（SQLite/Postgres 通用）。

    TODO: 换成 Alembic。这里靠 try/except 判断"列已存在"，无法区分真正的迁移失败，
    所以失败会打印出来而不是静默吞掉。
    """
    import logging
    from sqlalchemy import text
    log = logging.getLogger("hivora.migrate")
    with engine.connect() as conn:
        for ddl in [
            "ALTER TABLE agents ADD COLUMN role VARCHAR(16) DEFAULT 'agent'",
            "ALTER TABLE agents ADD COLUMN active INTEGER DEFAULT 1",
            "ALTER TABLE agents ADD COLUMN plan VARCHAR(32) DEFAULT 'trial'",
            "ALTER TABLE agents ADD COLUMN expires VARCHAR(10) DEFAULT ''",
            "ALTER TABLE products ADD COLUMN agent_id VARCHAR(64) DEFAULT ''",
            "ALTER TABLE clients ADD COLUMN deleted VARCHAR(24) DEFAULT ''",
            "ALTER TABLE policies ADD COLUMN deleted VARCHAR(24) DEFAULT ''",
            "ALTER TABLE appointments ADD COLUMN deleted VARCHAR(24) DEFAULT ''",
            "ALTER TABLE facts ADD COLUMN deleted VARCHAR(24) DEFAULT ''",
            "ALTER TABLE documents ADD COLUMN deleted VARCHAR(24) DEFAULT ''",
            "ALTER TABLE agents ADD COLUMN token_quota INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(text(ddl))
                conn.commit()
                log.warning("migrate: %s", ddl)
            except Exception as e:
                conn.rollback()
                msg = str(e).lower()
                if "duplicate" not in msg and "already exists" not in msg:
                    log.error("migrate FAILED: %s → %s", ddl, e)
        # 历史遗留：agent_id 为空的产品是多租户改造前的全局产品，归属演示账号，
        # 避免它们继续出现在真实用户的目录和 AI prompt 里。
        try:
            conn.execute(text("UPDATE products SET agent_id=:d WHERE agent_id IS NULL OR agent_id=''"),
                         {"d": DEMO_AGENT})
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error("migrate FAILED: products backfill → %s", e)
