# RUNBOOK — 出事了怎么办

面向：凌晨两点被代理人的 WhatsApp 叫醒的那个人（就是你）。
按「先判断在哪一层 → 再按症状查」的顺序用。

---

## 0. 三十秒定位

```bash
curl -s https://hivora-agent-stage.onrender.com/healthz    # 进程活着吗
curl -s https://hivora-agent-stage.onrender.com/readyz     # 数据库通吗
curl -s -o /dev/null -w "%{http_code}\n" https://hivora-frontend.vercel.app
curl -s -o /dev/null -w "%{http_code}\n" https://hivora-agent-stage.onrender.com/console
```

### 只有一套环境

名字叫 stage，但它**跑的是真实数据和 Neon 生产库**。没有独立的预发环境 ——
在这里改坏了就是改坏了。

| 角色 | 地址 | 托管在 | 谁用 |
|---|---|---|---|
| 客户端 | `https://hivora-frontend.vercel.app` | Vercel（repo `hivora-frontend`） | 代理人 / 租户 |
| 管理站 | `https://hivora-agent-stage.onrender.com/console` | Render，与后端同源 | 你（hivora admin） |
| 后端 API | `https://hivora-agent-stage.onrender.com` | Render（repo `hivora-agent`） | 上面两个都调它 |
| 终端客户 | `t.me/<各公司自己的 bot>` | Telegram | 租户的客户，**没有网页** |

> ⚠️ **命名不一致**：后端带 `-stage`，前端不带。同一套环境两个名字，
> 别误以为是两个环境。改前端域名要同步改 Render 的 `ALLOWED_ORIGINS`
> 和 `APP_LOGIN_URL`（设密码链接靠它拼），漏一个的现象分别是
> 「点登录没反应」和「开通信里的链接指向旧域名」。

| 现象 | 结论 | 跳到 |
|---|---|---|
| `/healthz` 超时或 502 | 后端没起来 | §1 |
| `/healthz` 通、`/readyz` 返回 503 | 应用活着，数据库不通 | §2 |
| 两个都通、前端 404 | Vercel 部署问题 | §3 |
| 都通但用户说「点了没反应」 | 十有八九是 CORS | §4 |

`/readyz` 会告诉你具体哪一项挂了：

```json
{"ok": false, "checks": {"db": "fail: OperationalError", "storage": "postgres"}}
```

---

## 1. 后端起不来

先看 Render → Logs。**新代码启动失败时会明确说缺什么**，守卫是故意这么设计的：

| 日志里的话 | 原因 | 处理 |
|---|---|---|
| `生产环境未设置 DATABASE_URL` | Neon 连接串没了或被改坏 | Render Environment 补回去 |
| `SECRET_KEY 未设置或短于 32 字符` | 密钥丢了 | 补一个 ≥32 字符的。**注意：换了它所有人要重新登录** |
| `生产环境必须设置 ALLOWED_ORIGINS` | 前端域名白名单空了 | 填两个站的域名，逗号分隔 |
| `ADMIN_PASSWORD 太弱` | 有人把管理员口令改成了弱口令 | 换成 ≥10 位的强口令 |
| `还没有管理员账号` | 空库首次启动，没给管理员 | 设 `ADMIN_EMAIL` / `ADMIN_PASSWORD` |

这些都不是 bug，是拒绝带着不安全配置上线。

**回滚**：Render → Deploys → 找上一个绿色的部署 → Rollback。或者：

```bash
cd server && git revert HEAD && git push      # 保留历史，别用 reset
```

---

## 2. 数据库不通

1. Neon 控制台看项目是不是被暂停（免费档闲置会挂起，首次请求会自动唤醒，慢一两秒是正常的）
2. 看用量有没有撞上免费档存储上限
3. 连接串是不是过期/被轮换了（Neon 可以在 Roles 里重置密码，重置后**记得同步改 Render**）

手动确认：

```bash
psql "$DATABASE_URL" -c "select count(*) from agents;"
```

---

## 3. 前端 404 / 白屏 / 还是旧版

```bash
cd frontend && git log --oneline -1 && git log --oneline -1 origin/main
```

本地领先远端就是**没推**。Vercel 只认 GitHub 上的东西。

前端改动必须走同步脚本，直接改 `frontend/index.html` 会被 pre-push 钩子拦下：

```bash
./sync-frontend.sh && ./sync-frontend.sh --check
```

---

## 4. 「点了登录没反应，也不报错」

几乎一定是 CORS。浏览器在预检阶段就把请求拦了，页面拿不到任何响应，所以连错误提示都显示不出来。

按 F12 看 Console，会有 `blocked by CORS policy`。

处理：把出问题的域名加进 Render 的 `ALLOWED_ORIGINS`（逗号分隔，不要有空格）：

```
https://hivora-frontend.vercel.app
```

> 管理站不在这里 —— 它跟后端同源（`/console`），浏览器根本不发预检。
> 「管理站点登录没反应」不会是 CORS，去看 §1。

---

## 5. 代理人说「AI 不回答了」

按顺序排查：

```bash
# a) 是不是用量超了？管理站 → 用量，或者：
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  https://hivora-agent-stage.onrender.com/api/admin/usage | jq
```

超额时接口返回 **429**，用户看到的是「本月 AI 用量已达上限」。提额：管理站给该账号设 `token_quota`（-1 = 不限）。

```bash
# b) OpenRouter 那边的余额和状态
# c) Render 日志里搜 "llm.invoke 失败" / "llm.stream 失败"
```

返回 **502「AI 服务暂时不可用」**= 模型侧的问题（超时、限流、余额）。应用本身是好的，客户数据不受影响。

**注意**：如果代理人问条款问题得到「条款库里没查到相关内容」，这不是故障 —— 是他还没上传过条款 PDF。

---

## 5b. 邮件发不出去

先看管理站点 ✉️ 发测试邮件的报错，它会说走的是哪条通道。日志关键词：`发信失败`、`Resend`。

| 现象 | 原因 | 处理 |
|---|---|---|
| traceback 停在 `smtplib.py … _get_socket` | **Render 免费档封了出站 25/465/587**（2025-09 起） | 配 `RESEND_API_KEY` 走 HTTP，或升级付费实例 |
| `SMTPAuthenticationError (535)` | Gmail 要应用专用密码，不是账号密码 | Google 账号 → 两步验证 → 应用专用密码 |
| `Resend 拒绝了这封信 status=403` | 没验证域名时 Resend 只发给账号持有者本人 | 改用 Brevo（只需验证单个发件邮箱），或验证域名 |
| `Brevo 拒绝了这封信 status=400 … not verified` | `MAIL_FROM` 那个邮箱没在 Brevo 的 Senders 里验证 | 去 Senders 加它，填收到的 6 位码 |
| `Brevo … status=401 … unrecognised IP address` | Brevo 的 IP 白名单里没有 Render 的出站 IP | 见下 |
| `status=401`（没提 IP） | key 不对或被撤销 | 重新生成 |

**Brevo 的 IP 白名单**：Render 的出站不是固定 IP，而是两个**共享网段**
（服务页 → **Connect → Outbound**，当前是 `74.220.48.0/24` 和 `74.220.56.0/24`，
合计 512 个地址，同区域其他 Render 服务也在用）。

处理顺序：

1. 去 https://app.brevo.com/security/authorised_ips 试着直接加这两个 CIDR 段
2. 不接受 CIDR 就**把 IP 白名单关掉** —— 这个功能假设你有固定出口 IP，
   云上跑的服务没有，逐个加 512 个地址不现实

> ⚠️ 只加报错里那**一个** IP 会变成**间歇性故障**：从同网段另一个出口走时才失败，
> 比每次都失败难查得多。
>
> 关掉白名单之后，保护发信权限的就只剩 API key 本身了 —— 它泄露等于别人能用
> 你验证过的发件人地址发信。key 只放 Render 环境变量，泄露了立刻在 Brevo 重新生成。

> 两条通道都没配也不会出事：账号照常建，管理站把设密码链接摆出来让你手动转。
> **发信失败从来不会让建账号失败** —— 这是设计上的硬规则。

---

## 6. 账号相关

| 要做的事 | 怎么做 |
|---|---|
| 代理人忘了密码 | 管理站 → 账号 → 重置密码 → 系统发一次性链接，他自己设。**你看不到也设不了他的密码** |
| 停掉不续费的客户 | 管理站 → 停用。**对方手里的登录状态立刻失效**，不用等过期 |
| 到期自动停 | 管理站 → 套餐/到期 → 设日期，到期当天之后自动拦 |
| 客户要求删除个人数据（PDPA） | 管理站 → 客户数据/PDPA → 选代理人 → 彻底删除（不可恢复，会记审计） |
| 查「这条数据是谁改的」 | 管理站 → 审计日志，可按账号和动作筛 |
| 监管/客户要审计记录 | 管理站 → 审计日志 → **⬇️ 导出 CSV**（带当前筛选条件，Excel 可直接打开） |

> 停用账号和彻底删除客户都要**打一遍邮箱/姓名**确认，服务端会校验。
> 这两个操作一个影响对方生意、一个不可恢复，不想让人手滑。

---

## 7. 数据恢复

**先备份当前状态再动手**，哪怕当前状态是坏的：

```bash
DATABASE_URL="$PROD_URL" ./server/scripts/backup.sh
```

从备份恢复：

```bash
# 强烈建议先恢复到一个新库验证，确认没问题再切
gzip -dc backups/hivora-2026-08-11-0200.sql.gz | psql "$NEW_DATABASE_URL"
psql "$NEW_DATABASE_URL" -c "select count(*) from agents; select count(*) from clients;"
# 数字对得上，再把 Render 的 DATABASE_URL 指向新库
```

备份来源有两处：
- GitHub Actions 的 `Nightly backup`（每天 UTC 18:00，产物保留 90 天）
- Neon 自带的时间点恢复 —— **去确认你那个档位的窗口有多长**，免费档很短

> ⚠️ **恢复演练**：每季度手动跑一次 `Nightly backup` 工作流，下载产物，恢复到一个临时 Neon 分支验证。
> 没验证过的备份不算备份。

---

## 8. 部署流程

```bash
cd server && .venv/bin/python -m pytest tests -q --ignore=tests/test_browser_smoke.py
cd server && .venv/bin/python -m pytest tests/test_browser_smoke.py -q
./sync-frontend.sh --check

cd server && git push        # → Render，2-4 分钟（后端 + 管理站 /console）
cd frontend && git push      # → Vercel，<1 分钟
cd admin && git push         # 只是留档；线上那份由 server 带上去

curl -s https://hivora-agent-stage.onrender.com/readyz    # 部署后必查
```

---

## 9. 追一次请求

每个响应都带 `X-Request-ID`，日志里也有同一个 id：

```bash
curl -sD- https://hivora-agent-stage.onrender.com/healthz | grep -i x-request-id
# 拿这个 id 去 Render 日志里搜，能捞到这次请求的全部日志
```

配了 `SENTRY_DSN` 的话，未处理异常会自动上报（`send_default_pii=False`，不会把客户数据送出去）。

---

## 10. 只有你能做、且必须定期做的事

- [ ] **每季度**跑一次恢复演练（§7）
- [ ] **每月**看一次管理站的用量页，确认单客户成本在预期内
- [ ] 轮换 `SECRET_KEY` 前先通知用户 —— 会让所有人登出
- [ ] Neon 和 Render 的档位：免费档会休眠，上真实用户前升级
