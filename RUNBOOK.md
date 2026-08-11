# RUNBOOK — 出事了怎么办

面向：凌晨两点被代理人的 WhatsApp 叫醒的那个人（就是你）。
按「先判断在哪一层 → 再按症状查」的顺序用。

---

## 0. 三十秒定位

```bash
curl -s https://hivora-agent-stage.onrender.com/healthz    # 进程活着吗
curl -s https://hivora-agent-stage.onrender.com/readyz     # 数据库通吗
curl -s -o /dev/null -w "%{http_code}\n" https://hivora-frontend.vercel.app
```

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
https://hivora-frontend.vercel.app,https://hivora-admin.vercel.app
```

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

## 6. 账号相关

| 要做的事 | 怎么做 |
|---|---|
| 代理人忘了密码 | 管理站 → 账号 → 重置密码 → 把新密码发给他 |
| 停掉不续费的客户 | 管理站 → 停用。**对方手里的登录状态立刻失效**，不用等过期 |
| 到期自动停 | 管理站 → 套餐/到期 → 设日期，到期当天之后自动拦 |
| 客户要求删除个人数据（PDPA） | 管理站 → 客户数据/PDPA → 选代理人 → 彻底删除（不可恢复，会记审计） |
| 查「这条数据是谁改的」 | 管理站 → 审计日志，可按账号和动作筛 |

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

cd server && git push        # → Render，2-4 分钟
cd frontend && git push      # → Vercel，<1 分钟
cd admin && git push         # → Vercel（管理站）

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
