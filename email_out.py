"""开通邮件 —— 管理员建完账号，自动把登录信息发给代理人。

用标准库 smtplib，不引第三方 SDK：任何邮箱服务商（Gmail 应用专用密码、
Resend、SendGrid、自建）都提供 SMTP，换服务商只是改环境变量。

两条硬规则：
1. **没配 SMTP 就整个降级为 no-op**，返回「没发」，绝不报错。
2. **发信失败绝不能让建账号失败。** 账号已经建好了，邮件只是通知手段；
   发不出去时管理站会把凭据显示出来让管理员手动发。
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("hivora.email")

HOST = os.environ.get("SMTP_HOST", "").strip()
PORT = int(os.environ.get("SMTP_PORT", "587"))
USER = os.environ.get("SMTP_USER", "").strip()
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM = os.environ.get("SMTP_FROM", "").strip() or USER
TIMEOUT = int(os.environ.get("SMTP_TIMEOUT", "10"))
LOGIN_URL = os.environ.get("APP_LOGIN_URL", "https://hivora-frontend.vercel.app").rstrip("/")


def setup_link(token: str) -> str:
    """设密码页的完整地址。前端用 ?setup=<token> 识别。"""
    return f"{LOGIN_URL}/?setup={token}"


def configured() -> bool:
    return bool(HOST and FROM)


def send(to: str, subject: str, body: str) -> bool:
    """发一封纯文本邮件。成功返回 True，其余一律 False —— 从不抛异常。"""
    if not configured():
        log.info("没配 SMTP，跳过发信 to=%s", to)
        return False
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
        return True
    except Exception:
        # 只记异常类型和收件人，绝不把邮件正文写进日志——里面有密码
        log.warning("发信失败 to=%s", to, exc_info=True)
        return False


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
