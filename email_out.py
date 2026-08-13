"""开通邮件 —— 管理员建完账号，自动把登录信息发给代理人。

三条发信通道，都只用标准库，不引第三方 SDK。按这个顺序选：

1. **Brevo**（`BREVO_API_KEY`）—— **没有自己的域名时用这条。**
   它允许只验证单个发件邮箱（收 6 位验证码），验完就能发给任何人。
2. **Resend**（`RESEND_API_KEY`）—— 有域名之后的首选，送达率好。
   ⚠️ 没验证域名时只能发给你注册 Resend 的那个邮箱，发别人一律 403。
3. **SMTP**（`SMTP_HOST`）—— 退路。

前两条都走 443。这不是偏好问题：**Render 免费档从 2025-09 起封了出站的
25 / 465 / 587**，SMTP 在 connect 阶段就超时，跟账号密码对不对无关。

两条硬规则：
1. **两条都没配就整个降级为 no-op**，返回「没发」，绝不报错。
2. **发信失败绝不能让建账号失败。** 账号已经建好了，邮件只是通知手段；
   发不出去时管理站会把链接显示出来让管理员手动发。
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

log = logging.getLogger("hivora.email")

HOST = os.environ.get("SMTP_HOST", "").strip()
PORT = int(os.environ.get("SMTP_PORT", "587"))
USER = os.environ.get("SMTP_USER", "").strip()
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
TIMEOUT = int(os.environ.get("SMTP_TIMEOUT", "10"))
LOGIN_URL = os.environ.get("APP_LOGIN_URL", "https://hivora-frontend.vercel.app").rstrip("/")

RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_URL = "https://api.resend.com/emails"
BREVO_KEY = os.environ.get("BREVO_API_KEY", "").strip()
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
# 发件人两条通道共用。没有自己的域名时可以先用 Resend 的 onboarding@resend.dev。
FROM = (os.environ.get("MAIL_FROM", "").strip()
        or os.environ.get("SMTP_FROM", "").strip()
        or USER)


def setup_link(token: str) -> str:
    """设密码页的完整地址。前端用 ?setup=<token> 识别。"""
    return f"{LOGIN_URL}/?setup={token}"


def provider() -> str:
    """当前生效的发信通道 —— 管理站拿它告诉你到底在走哪条路。"""
    if not FROM:
        return ""
    if BREVO_KEY:
        return "brevo"
    if RESEND_KEY:
        return "resend"
    return "smtp" if HOST else ""


def configured() -> bool:
    return bool(provider())


def _reason(raw: str) -> str:
    """从服务商的 JSON 里挑出人能看懂的那句。挑不出就原样返回。"""
    try:
        return json.loads(raw).get("message") or raw
    except Exception:
        return raw


def _post_json(who: str, url: str, headers: dict, payload: dict,
               to: str) -> tuple[bool, str]:
    """打一个 JSON API。返回 (成功, 失败原因)，从不抛异常。

    失败原因里只有服务商的话，绝不含 API key、密码或邮件正文。
    """
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return 200 <= r.status < 300, ""
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"")[:300].decode("utf-8", "replace")
        log.warning("%s 拒绝了这封信 to=%s status=%s %s", who, to, e.code, raw)
        return False, f"{e.code}: {_reason(raw)}"
    except Exception as e:
        log.warning("%s 调不通 to=%s", who, to, exc_info=True)
        return False, f"{type(e).__name__}: {e}"


def _from_parts() -> tuple[str, str]:
    """把 "Hivora <a@b.com>" 拆成 (显示名, 地址)。Brevo 要分开传。"""
    name, addr = parseaddr(FROM)
    return name or "Hivora", addr or FROM


def _send_brevo(to: str, subject: str, body: str) -> tuple[bool, str]:
    name, addr = _from_parts()
    return _post_json(
        "Brevo", BREVO_URL, {"api-key": BREVO_KEY, "Accept": "application/json"},
        {"sender": {"name": name, "email": addr}, "to": [{"email": to}],
         "subject": subject, "textContent": body}, to)


def _send_resend(to: str, subject: str, body: str) -> tuple[bool, str]:
    return _post_json(
        "Resend", RESEND_URL, {"Authorization": f"Bearer {RESEND_KEY}"},
        {"from": FROM, "to": [to], "subject": subject, "text": body}, to)


def send_detailed(to: str, subject: str, body: str) -> tuple[bool, str]:
    """发信，并带回失败原因。给管理站用 —— 让管理员不用翻日志就知道卡在哪。

    原因里只会有服务商的错误信息，绝不含邮件正文、密码或 API key。
    """
    how = provider()
    if not how:
        log.info("没配发信通道，跳过发信 to=%s", to)
        return False, "没配发信通道"
    if how in ("brevo", "resend"):
        ok, err = (_send_brevo if how == "brevo" else _send_resend)(to, subject, body)
        if ok:
            log.info("已发信 to=%s subject=%s via=%s", to, subject, how)
        return ok, err
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if PORT == 465:
            with smtplib.SMTP_SSL(HOST, PORT, timeout=TIMEOUT,
                                  context=ssl.create_default_context()) as s:
                if USER:
                    s.login(USER, PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(HOST, PORT, timeout=TIMEOUT) as s:
                s.starttls(context=ssl.create_default_context())
                if USER:
                    s.login(USER, PASSWORD)
                s.send_message(msg)
        log.info("已发信 to=%s subject=%s", to, subject)
        return True, ""
    except Exception as e:
        # 只记异常类型和收件人，绝不把邮件正文写进日志——里面有密码
        log.warning("发信失败 to=%s", to, exc_info=True)
        return False, f"{type(e).__name__}: {e}"


def send(to: str, subject: str, body: str) -> bool:
    """发一封纯文本邮件。成功返回 True，其余一律 False —— 从不抛异常。"""
    return send_detailed(to, subject, body)[0]


# ── 具体的信 ──────────────────────────────────────────────────
def welcome(to: str, name: str, link: str, brand: str = "Hivora") -> bool:
    """开通信。**给的是一次性链接，不是密码** —— 密码不该走邮件。"""
    body = f"""{name or to} 你好，

你的 {brand} 账号已经开通了。点下面的链接设置你自己的密码：

{link}

（链接 48 小时内有效，只能用一次。过期了找管理员重发。）

账号：{to}

设好密码进去之后，建议按这四步把它变成你自己的助手：

1. 上传你常用的条款 PDF —— 之后问条款会带出处，查不到就说查不到，不会编
2. 把常卖的产品加进产品目录
3. 连接 Telegram：用自己的 bot，客户找你直接进收件箱，AI 起草你确认后再发
4. 加第一个客户和保单

有问题直接回这封邮件。

—— {brand}
"""
    return send(to, f"你的 {brand} 账号已开通", body)


def password_reset(to: str, name: str, link: str, brand: str = "Hivora") -> bool:
    body = f"""{name or to} 你好，

管理员给你重置了 {brand} 的密码。点下面的链接设置新密码：

{link}

（链接 48 小时内有效，只能用一次。）

账号：{to}

如果这不是你要求的，请立刻联系管理员。

—— {brand}
"""
    return send(to, f"你的 {brand} 密码重置链接", body)
