"""Telegram 入口 —— 代理人在自己的 bot 里用 Hivora。

每个代理人自己去 BotFather 建一个 bot，把 token 填进 Hivora。这样：
- 客户/代理人看到的是他自己品牌的 bot
- 你不用替任何人保管 token
- 多租户天然隔离：一个 bot 只服务一个 agent_id

安全上有三道：
1. token 用 Fernet 加密存，接口只回最后 4 位，永不明文回传
2. webhook 路径带随机片段，且校验 Telegram 的 secret header
3. 只有绑过的 chat_id 才会得到回答 —— bot 链接被转发出去也没用
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


def disconnect(s, agent_id: str):
    row = s.query(db.TelegramBot).filter_by(agent_id=agent_id).first()
    if not row:
        return
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
        return dict(connected=False, chats=[])
    return dict(connected=True, username=row.username, since=row.connected,
                # 只回最后 4 位，够用来确认"是不是我填的那个"，泄漏了也没用
                token_hint="…" + decrypt(row.token_enc)[-4:],
                chats=[dict(id=c.id, chat_id=c.chat_id, name=c.name,
                            created=c.created) for c in chats])


def new_bind_code(s, agent_id: str) -> str:
    s.query(db.TelegramBind).filter(db.TelegramBind.expires < time.time()).delete()
    code = "HV" + secrets.token_hex(3).upper()
    s.add(db.TelegramBind(code=code, agent_id=agent_id, expires=time.time() + BIND_TTL))
    s.commit()
    return code


def unlink_chat(s, agent_id: str, row_id: int):
    s.query(db.TelegramChat).filter_by(agent_id=agent_id, id=row_id).delete()
    s.commit()


# ── 收到消息 ──────────────────────────────────────────────────
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
    token = decrypt(bot.token_enc)

    # /start <code> 或直接发绑定码
    if text.startswith("/start"):
        code = text[6:].strip()
        send(token, chat_id,
             _bind(s, bot.agent_id, code, chat_id, name) if code
             else "请在 Hivora 网页版点「连接 Telegram」拿绑定码，然后发给我。")
        return

    linked = (s.query(db.TelegramChat)
              .filter_by(agent_id=bot.agent_id, chat_id=chat_id).first())
    if not linked:
        if text.upper().startswith("HV"):
            send(token, chat_id, _bind(s, bot.agent_id, text, chat_id, name))
        else:
            send(token, chat_id,
                 "这台设备还没绑定。到 Hivora 网页版点「连接 Telegram」拿绑定码发给我。")
        return

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
