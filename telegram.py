"""Telegram 入口 —— 客户的消息渠道 + 代理人自己的助手，共用一个 bot。

两种接入模式：

    platform（推荐，零配置）：平台方在 BotFather 建**一个**官方 bot，token 放
        PLATFORM_BOT_TOKEN。代理人点一下就连上；客户通过代理人的专属
        /start 链接进来，按链接归属到对应租户。
    own（白牌）：代理人自己去 BotFather 建 bot、贴 token。客户看到的是
        他自己品牌的 bot；平台不保管他的 token。

own 模式一个 bot 只服务一个 agent_id；platform 模式一个 bot 服务所有租户，
靠 chat→租户 的绑定关系隔离（设备=TelegramChat，客户=Thread.tg_chat_id）。

同一个 bot 服务两种人，靠有没有绑过码来分：

    绑过绑定码  → 代理人本人。提问走 ask()，当私人助手用
    没绑过      → 客户。走下面的混合策略

**客户侧是混合策略，不是全自动**（AGENTS.md 铁律 2）：

    只有条款类问题才自动回，且必须查得到条款依据，回复带出处和免责声明。
    以下一律进收件箱等真人：
      · 理赔、核保、报价、推荐、投诉、退保 —— 涉及钱、涉及决定、涉及推荐
      · 客户说「人工」
      · 条款库里查不到依据
      · 配额用完 / 模型不可用

转人工的原因只写审计日志，**绝不发给客户** —— 里面可能是「本月配额用完了」。

安全上有三道：
1. token 用 Fernet 加密存，接口只回最后 4 位，永不明文回传
2. webhook 路径带随机片段，且校验 Telegram 的 secret header
3. 绑定码一次性、10 分钟过期，且只认自己 bot 的码
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request

import db

log = logging.getLogger("hivora.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
HTTP_TIMEOUT = int(os.environ.get("TELEGRAM_TIMEOUT", "10"))
BIND_TTL = 600          # 绑定码有效期（秒）
MSG_LIMIT = 4000        # Telegram 单条上限 4096，留点余量


class TelegramError(RuntimeError):
    """调 Telegram 接口失败。文案可以直接给用户看。"""


# ── token 加解密 ──────────────────────────────────────────────
def _fernet():
    """用 SECRET_KEY 派生加密密钥。

    注意：换了 SECRET_KEY 之后已存的 token 解不开，代理人需要重新连接一次。
    这是可接受的——换密钥本来就会让所有人重新登录。
    """
    from cryptography.fernet import Fernet
    import auth
    key = base64.urlsafe_b64encode(hashlib.sha256(b"tg:" + auth.SECRET).digest())
    return Fernet(key)


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(blob: str) -> str:
    return _fernet().decrypt(blob.encode()).decode()


# ── Telegram API ─────────────────────────────────────────────
def call(token: str, method: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        API.format(token=token, method=method), data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            raise TelegramError(f"Telegram 返回 {e.code}") from e
        raise TelegramError(data.get("description") or f"Telegram 返回 {e.code}")
    except Exception as e:
        log.warning("Telegram %s 调用失败: %s", method, e)
        raise TelegramError("连不上 Telegram，请稍后再试") from e
    if not data.get("ok"):
        raise TelegramError(data.get("description") or "Telegram 拒绝了请求")
    return data.get("result") or {}


def send(token: str, chat_id: str, text: str):
    """超长自动分段。发送失败只记日志——不能反过来影响主流程。"""
    for i in range(0, len(text) or 1, MSG_LIMIT):
        chunk = text[i:i + MSG_LIMIT] or "（空）"
        try:
            call(token, "sendMessage", {"chat_id": chat_id, "text": chunk,
                                        "disable_web_page_preview": True})
        except TelegramError as e:
            log.warning("发消息失败 chat=%s: %s", chat_id, e)
            return


# ── 官方共享 bot（平台模式）──────────────────────────────────
def platform_token() -> str:
    return os.environ.get("PLATFORM_BOT_TOKEN", "").strip()


def platform_available() -> bool:
    return bool(platform_token())


def platform_header_secret() -> str:
    """平台 webhook 的校验头。由 SECRET_KEY 派生，稳定且不入库。"""
    import auth
    return hashlib.sha256(b"tg-platform:" + auth.SECRET).hexdigest()[:40]


def bot_token(bot) -> str:
    """按模式取真正可用的 token。platform 行的 token_enc 是空的。"""
    if (getattr(bot, "mode", "own") or "own") == "platform":
        tok = platform_token()
        if not tok:
            raise TelegramError("官方 bot 未配置（PLATFORM_BOT_TOKEN），请联系管理员。")
        return tok
    return decrypt(bot.token_enc)


# ── 连接 / 断开 ───────────────────────────────────────────────
def public_base() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise TelegramError(
            "服务端没有配置 PUBLIC_BASE_URL，无法给 Telegram 设置回调地址。"
            "请管理员在 Render Environment 里填后端的公开域名。")
    return base


def connect(s, agent_id: str, token: str) -> dict:
    """校验 token、存下来、注册 webhook。"""
    token = token.strip()
    if not token or ":" not in token:
        raise TelegramError("这不像一个 bot token。找 @BotFather 建 bot 后会给你一串。")
    base = public_base()

    me = call(token, "getMe")            # token 不对这一步就会失败
    username = me.get("username") or ""

    row = s.query(db.TelegramBot).filter_by(agent_id=agent_id).first()
    if row is None:
        row = db.TelegramBot(agent_id=agent_id)
        s.add(row)
    row.mode = "own"        # 可能是从官方共享 bot 升级过来的，必须显式切回来
    row.username = username
    row.token_enc = encrypt(token)
    row.path_secret = row.path_secret or secrets.token_urlsafe(24)
    row.header_secret = secrets.token_urlsafe(24)
    row.connected = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    call(token, "setWebhook", {
        "url": f"{base}/api/tg/{row.path_secret}",
        "secret_token": row.header_secret,
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    })
    s.commit()
    return dict(username=username)


def connect_platform(s, agent_id: str) -> dict:
    """一键接入官方共享 bot：不要 token、不用 BotFather，点一下就好。"""
    tok = platform_token()
    if not tok:
        raise TelegramError("官方 bot 暂未开通。可以先用「自己的 bot」方式连接，"
                            "或让管理员配置 PLATFORM_BOT_TOKEN。")
    base = public_base()
    me = call(tok, "getMe")
    username = me.get("username") or ""

    row = s.query(db.TelegramBot).filter_by(agent_id=agent_id).first()
    if row is None:
        row = db.TelegramBot(agent_id=agent_id)
        s.add(row)
    row.mode = "platform"
    row.username = username
    row.token_enc = ""                       # 平台模式不存任何 token
    row.path_secret = row.path_secret or ("p-" + secrets.token_urlsafe(24))
    row.header_secret = platform_header_secret()
    row.cust_code = getattr(row, "cust_code", "") or ("C" + secrets.token_hex(5).upper())
    row.connected = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 平台 webhook 是全局共享的一条，重复注册无害（幂等）
    call(tok, "setWebhook", {
        "url": f"{base}/api/tg/platform",
        "secret_token": platform_header_secret(),
        "allowed_updates": ["message"],
    })
    s.commit()
    return dict(username=username, mode="platform")


def disconnect(s, agent_id: str):
    row = s.query(db.TelegramBot).filter_by(agent_id=agent_id).first()
    if not row:
        return
    if (getattr(row, "mode", "own") or "own") != "platform":
        # 只有自建 bot 才删 webhook；平台 bot 是所有租户共享的，动不得
        try:
            call(decrypt(row.token_enc), "deleteWebhook", {"drop_pending_updates": True})
        except Exception:
            log.warning("deleteWebhook 失败，仍然继续删除本地记录", exc_info=True)
    s.query(db.TelegramChat).filter_by(agent_id=agent_id).delete()
    s.query(db.TelegramBind).filter_by(agent_id=agent_id).delete()
    s.delete(row)
    s.commit()


def status(s, agent_id: str) -> dict:
    row = s.query(db.TelegramBot).filter_by(agent_id=agent_id).first()
    chats = s.query(db.TelegramChat).filter_by(agent_id=agent_id).all()
    if not row:
        return dict(connected=False, chats=[], platform_available=platform_available())
    mode = getattr(row, "mode", "own") or "own"
    if mode == "platform":
        hint = "官方 bot"
        cust_link = (f"https://t.me/{row.username}?start={row.cust_code}"
                     if row.username and getattr(row, "cust_code", "") else "")
    else:
        # 只回最后 4 位，够用来确认"是不是我填的那个"，泄漏了也没用
        hint = "…" + decrypt(row.token_enc)[-4:]
        cust_link = f"https://t.me/{row.username}" if row.username else ""
    return dict(connected=True, username=row.username, since=row.connected,
                mode=mode, token_hint=hint, cust_link=cust_link,
                platform_available=platform_available(),
                chats=[dict(id=c.id, chat_id=c.chat_id, name=c.name,
                            created=c.created) for c in chats])


def new_bind_code(s, agent_id: str, ttl: int = None) -> str:
    """ttl 默认 10 分钟（代理人自己当场绑）。白手套交付时管理员代生成的链接
    要经 WhatsApp 转给客户，给长一点——但它等于客户的身份，不能无限期。"""
    s.query(db.TelegramBind).filter(db.TelegramBind.expires < time.time()).delete()
    code = "HV" + secrets.token_hex(3).upper()
    s.add(db.TelegramBind(code=code, agent_id=agent_id,
                          expires=time.time() + (ttl or BIND_TTL)))
    s.commit()
    return code


def unlink_chat(s, agent_id: str, row_id: int):
    s.query(db.TelegramChat).filter_by(agent_id=agent_id, id=row_id).delete()
    s.commit()


# ── 收到消息 ──────────────────────────────────────────────────
# 客户第一次来时的固定回执。**写死的文案，不是 AI 生成**——
# 合规红线：AI 绝不直接面对客户。
CUSTOMER_ACK = os.environ.get(
    "TELEGRAM_CUSTOMER_ACK",
    "已收到你的消息，我会尽快回复你 🙏")
# 转人工时给客户的话。**永远不能带上内部原因**——那里面可能是配额用完了。
ESCALATED = "这个我帮你转给同事，稍后回复你 🙏"

HELP = ("我是你的 Hivora 助手。直接问就行：\n"
        "· 张伟明有哪些保单？\n"
        "· MediShield 的等待期多久？\n"
        "· 帮我写个续保提醒给 Lim Mei Ling\n"
        "· 帮我加客户 Ahmad，电话 012-3456789\n\n"
        "/unlink 解除这台设备的绑定")


def _bind(s, agent_id_of_bot: str, code: str, chat_id: str, name: str) -> str:
    row = s.query(db.TelegramBind).filter_by(code=code.strip().upper()).first()
    if not row or row.expires < time.time():
        return "绑定码无效或已过期。到 Hivora 网页版重新生成一个。"
    if row.agent_id != agent_id_of_bot:
        # 拿别人的码来绑自己的 bot，不给过
        return "这个绑定码不属于这个 bot。"
    exists = (s.query(db.TelegramChat)
              .filter_by(agent_id=row.agent_id, chat_id=chat_id).first())
    if not exists:
        s.add(db.TelegramChat(agent_id=row.agent_id, chat_id=chat_id, name=name,
                              created=dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
    s.query(db.TelegramBind).filter_by(code=row.code).delete()
    db.audit(s, row.agent_id, "tg_bind", f"chat={chat_id} {name}")
    s.commit()
    return "✅ 绑定成功。\n\n" + HELP


def notify_agent(s, agent_id: str, token: str, text: str):
    """把提醒推给代理人已绑定的所有设备。"""
    for c in s.query(db.TelegramChat).filter_by(agent_id=agent_id).all():
        send(token, c.chat_id, text)


def _customer_message(s, bot, chat_id: str, name: str, text: str) -> None:
    """客户发来的消息。

    混合策略（AGENTS.md 铁律 2）：**只有条款类问题**才自动回，且必须有条款依据、
    带出处和免责声明。理赔/核保/报价/推荐/投诉，客户说「人工」，条款库查不到，
    以及配额用完或模型挂了 —— 一律进收件箱等真人。

    转人工的原因只写进审计日志，**绝不发给客户**。
    """
    ts = dt.datetime.now().strftime("%H:%M")
    t = (s.query(db.Thread)
         .filter_by(agent_id=bot.agent_id, tg_chat_id=chat_id).first())
    first = t is None
    if first:
        t = db.Thread(agent_id=bot.agent_id, client=name or f"TG {chat_id}",
                      lang="中文", channel="telegram", tg_chat_id=chat_id,
                      status="pending", unread=0)
        s.add(t)
        s.flush()
    s.add(db.Message(thread_id=t.id, role="customer", text=text[:4000], ts=ts))
    t.status, t.suggestions = "pending", "[]"
    t.unread = (t.unread or 0) + 1
    if name and t.client.startswith("TG "):
        t.client = name
    db.audit(s, bot.agent_id, "tg_customer_msg", f"chat={chat_id} {text[:60]}")
    s.commit()

    token = bot_token(bot)
    brand = db.brand_of(bot.agent_id)

    reply, why = (None, "该账号关掉了自动回复")
    if _auto_reply_on(s, bot.agent_id):
        from graph import customer_reply
        try:
            reply, why = customer_reply(text, bot.agent_id)
        except Exception:
            log.exception("自动回复出错 agent=%s", bot.agent_id)
            reply, why = None, "内部：自动回复异常"

    if reply:
        s.add(db.Message(thread_id=t.id, role="ai", text=reply[:4000], ts=ts))
        t.status = "sent"          # 客户已经收到答复了，不需要人再回一次
        db.audit(s, bot.agent_id, "tg_auto_reply", f"chat={chat_id} {text[:60]}")
        s.commit()
        send(token, chat_id, reply)
        notify_agent(s, bot.agent_id, token,
                     f"🤖 已自动回复 {t.client}：{text[:80]}\n\n"
                     f"到收件箱可复核。")
        return

    db.audit(s, bot.agent_id, "tg_escalate", f"chat={chat_id} 原因={why}")
    s.commit()
    if first:
        send(token, chat_id, CUSTOMER_ACK)
    else:
        send(token, chat_id, ESCALATED)
    notify_agent(s, bot.agent_id, token,
                 f"💬 {t.client}：{text[:120]}\n\n（{why}）到 {brand} 收件箱确认后回复。")


def _auto_reply_on(s, agent_id: str) -> bool:
    a = s.query(db.Agent).filter_by(agent_key=agent_id).first()
    return bool(a and (a.auto_reply if a.auto_reply is not None else 1))


def send_to_chat(s, agent_id: str, chat_id: str, text: str) -> bool:
    """代理人在网页上发的回复 → 真的发到客户的 Telegram。"""
    bot = s.query(db.TelegramBot).filter_by(agent_id=agent_id).first()
    if not bot or not chat_id:
        return False
    send(bot_token(bot), chat_id, text)
    return True


def handle_update(s, path_secret: str, header_secret: str, update: dict) -> None:
    """处理一条 Telegram 更新。任何异常都不能冒到 webhook 外面。"""
    bot = s.query(db.TelegramBot).filter_by(path_secret=path_secret).first()
    if not bot:
        log.warning("未知的 webhook 路径")
        return
    # 先编码成 bytes：compare_digest 对非 ASCII 的 str 会直接抛 TypeError，
    # 而 header 是外部可控的，什么字符都可能塞进来。
    if not secrets.compare_digest((header_secret or "").encode("utf-8", "replace"),
                                  (bot.header_secret or "").encode()):
        log.warning("webhook secret 不匹配 agent=%s", bot.agent_id)
        return

    msg = (update or {}).get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
        or chat.get("username") or ""
    token = bot_token(bot)

    # /start <code> 或直接发绑定码
    if text.startswith("/start"):
        code = text[6:].strip()
        if code:
            send(token, chat_id, _bind(s, bot.agent_id, code, chat_id, name))
        else:
            # 客户点 Start 时也会走到这里——给固定欢迎语，别提绑定码
            already = (s.query(db.TelegramChat)
                       .filter_by(agent_id=bot.agent_id, chat_id=chat_id).first())
            send(token, chat_id, HELP if already else CUSTOMER_ACK)
        return

    # 同一个 bot 服务两种人：绑过码的是代理人本人（助手模式），
    # 其余一律当客户（消息进收件箱，等代理人确认后回复）。
    linked = (s.query(db.TelegramChat)
              .filter_by(agent_id=bot.agent_id, chat_id=chat_id).first())
    if not linked:
        if text.upper().startswith("HV") and len(text.strip()) <= 10:
            send(token, chat_id, _bind(s, bot.agent_id, text, chat_id, name))
        else:
            _customer_message(s, bot, chat_id, name, text)
        return

    _linked_message(s, bot, token, chat_id, text)


def _linked_message(s, bot, token: str, chat_id: str, text: str) -> None:
    """已绑定设备（代理人本人）的消息。own / platform 两种 webhook 共用。"""
    if text == "/unlink":
        s.query(db.TelegramChat).filter_by(agent_id=bot.agent_id,
                                           chat_id=chat_id).delete()
        db.audit(s, bot.agent_id, "tg_unlink", f"chat={chat_id}")
        s.commit()
        send(token, chat_id, "已解除绑定。需要时重新拿绑定码即可。")
        return
    if text in ("/help", "/start@", "?"):
        send(token, chat_id, HELP)
        return

    # 交给同一个大脑处理 —— 合规节点、配额、审计全都照常生效
    from graph import LLMUnavailable, QuotaExceeded, ask
    try:
        out = ask(text, bot.agent_id)
        answer = out.get("answer") or "（没有内容）"
        cites = out.get("citations") or []
        if cites:
            answer += "\n\n" + " ".join(f"📄 {c['product']}·P{c['page']}" for c in cites)
    except QuotaExceeded as e:
        answer = str(e)
    except LLMUnavailable as e:
        answer = str(e)
    except Exception:
        log.exception("Telegram 处理失败 agent=%s", bot.agent_id)
        answer = "出了点问题，请稍后再试。"
    send(token, chat_id, answer)


def _platform_bot_of(s, agent_id: str):
    row = s.query(db.TelegramBot).filter_by(agent_id=agent_id).first()
    return row if row and (getattr(row, "mode", "own") or "own") == "platform" else None


def _customer_start(s, bot, chat_id: str, name: str) -> None:
    """客户第一次点专属链接进来：建 thread、发品牌欢迎语（写死文案，非 AI）。"""
    t = (s.query(db.Thread)
         .filter_by(agent_id=bot.agent_id, tg_chat_id=chat_id).first())
    if t is None:
        t = db.Thread(agent_id=bot.agent_id, client=name or f"TG {chat_id}",
                      lang="中文", channel="telegram", tg_chat_id=chat_id,
                      status="pending", unread=0)
        s.add(t)
    db.audit(s, bot.agent_id, "tg_cust_start", f"chat={chat_id} {name}")
    s.commit()
    brand = db.brand_of(bot.agent_id)
    send(platform_token(), chat_id,
         f"你好，这里是 {brand} 👋 有什么可以帮你的，直接留言就行。")


def handle_platform_update(s, header_secret: str, update: dict) -> None:
    """官方共享 bot 的回调。一个 webhook 服务所有 platform 模式的租户。

    路由规则（顺序即优先级）：
      /start <客户码>   → 归属该租户的客户，建 thread + 欢迎语
      /start <绑定码>   → 代理人设备绑定（码本身带租户）
      已绑定的设备      → 助手模式（_linked_message）
      已有 thread 的客户 → 客户消息（_customer_message，混合自动回复照常）
      其余              → 固定提示去要专属链接；绝不当成任何租户的客户
    """
    if not platform_available():
        log.warning("收到平台 webhook 但 PLATFORM_BOT_TOKEN 未配置")
        return
    if not secrets.compare_digest((header_secret or "").encode("utf-8", "replace"),
                                  platform_header_secret().encode()):
        log.warning("平台 webhook secret 不匹配")
        return

    msg = (update or {}).get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
        or chat.get("username") or ""
    tok = platform_token()

    def _try_bind(code: str) -> bool:
        b = s.query(db.TelegramBind).filter_by(code=code.strip().upper()).first()
        if not b:
            return False
        bot = _platform_bot_of(s, b.agent_id)
        if not bot:
            send(tok, chat_id, "这个绑定码不属于官方 bot。")
            return True
        send(tok, chat_id, _bind(s, b.agent_id, code, chat_id, name))
        return True

    if text.startswith("/start"):
        arg = text[6:].strip()
        if arg:
            row = (s.query(db.TelegramBot)
                   .filter_by(cust_code=arg, mode="platform").first())
            if row:
                _customer_start(s, row, chat_id, name)
                return
            if _try_bind(arg):
                return
            send(tok, chat_id, "链接无效或已过期。请向你的代理人要一个新的链接。")
            return
        # 无参数 /start：已绑设备给帮助；已是某租户客户给欢迎；否则指路
        linked = s.query(db.TelegramChat).filter_by(chat_id=chat_id).first()
        if linked and _platform_bot_of(s, linked.agent_id):
            send(tok, chat_id, HELP)
            return
        t = (s.query(db.Thread)
             .filter(db.Thread.tg_chat_id == chat_id,
                     db.Thread.channel == "telegram").first())
        if t and _platform_bot_of(s, t.agent_id):
            send(tok, chat_id, CUSTOMER_ACK)
            return
        send(tok, chat_id, "你好 👋 请通过你的保险代理人发给你的专属链接开始对话。")
        return

    # 设备（代理人本人）？
    for c in s.query(db.TelegramChat).filter_by(chat_id=chat_id).all():
        bot = _platform_bot_of(s, c.agent_id)
        if bot:
            _linked_message(s, bot, tok, chat_id, text)
            return

    # 直接打了绑定码？
    if text.upper().startswith("HV") and len(text.strip()) <= 10:
        if _try_bind(text):
            return

    # 已有归属的客户？
    for t in (s.query(db.Thread)
              .filter(db.Thread.tg_chat_id == chat_id,
                      db.Thread.channel == "telegram").all()):
        bot = _platform_bot_of(s, t.agent_id)
        if bot:
            _customer_message(s, bot, chat_id, name, text)
            return

    # 谁都不是：不能猜租户，指路要链接
    send(tok, chat_id, "你好 👋 请通过你的保险代理人发给你的专属链接开始对话。")
