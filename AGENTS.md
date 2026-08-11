# AGENTS.md — 给 AI 编码助手（Cursor/Claude）的项目规则

## 项目是什么
Hivora：马来西亚保险代理人 AI Copilot（closed SaaS，账号内部发放）。
Python + FastAPI + LangGraph + SQLAlchemy 后端，单文件 HTML SPA 前端。
线上：前端 Vercel（hivora-frontend.vercel.app）+ 后端 Render（hivora-agent-stage.onrender.com）。

## 主开发目录
`server/`（= GitHub repo hivora-agent，push main 即自动部署 Render）。
`admin/` 是独立的内部管理站（= repo hivora-admin，独立 Vercel 项目）。
`pages-demo/` / `pages-stage/` 是冻结的静态 demo，**不要在里面开发**。

## 前后台分离（V0.1 起）
客户拿到的包里**一行管理代码都不能有**。管理功能只写在 `admin/index.html`，
客户端只写在 `server/static/index.html`。`sync-frontend.sh` 会在同步和 pre-push
时检查 `frontend/index.html` 里有没有 `api/admin`，有就直接拒绝。

## 铁律（违反=事故）
1. **数据隔离**：任何涉及 clients/policies/appointments/facts/threads/documents 的查询，
   必须 `filter_by(agent_id=...)`，agent_id 只能来自 `auth.current_agent` 依赖，绝不信任请求体。
2. **合规**：不得实现"跨保险公司比价/推荐哪家好"功能（BNM 监管红线）；
   Policy/ClientBook 的 AI 回答必须经 Compliance 节点加免责声明；条款查不到就说查不到。
3. **密钥**：绝不把 API key/连接串写进代码或提交 git。用 .env（本地）/ Render Environment（云端）。
   `server/.env` 在 .gitignore 里，保持这样。
4. **审计**：新增的写操作和 AI 动作要调用 `db.audit(session, agent_id, action, detail)`。
   `agent_id` 必填且只能来自 `auth.current_agent` / `auth.current_admin`。
5. **前端转义**：任何服务端数据（客户名、消息正文、文件名、**LLM 输出**）进 `innerHTML`
   前必须过 `esc()`。客户发一条带 `<img onerror>` 的 WhatsApp 消息就能偷走 token。
6. **产品目录也是租户数据**：`products` 表有 `agent_id`，读写都要带上。
7. **虚构条款**：`knowledge.POLICY_CHUNKS` 是假的示例条款，只给演示账号。
   绝不能让真实用户拿到——AI 会带着"第X页"的出处引用它们。
8. **前端同步**：`server/static/index.html` 是唯一来源。改完跑 `./sync-frontend.sh`
   生成 `frontend/index.html`（只有 `<meta name="hivora-api">` 不同），**不要手动 cp**。
   两个 repo 都装了 pre-push 钩子，不同步会拒绝 push。
9. **软删**：clients/policies/appointments/facts/documents 都有 `deleted` 列。
   任何面向用户的读取都要包 `db.live(query, Model)`，否则删掉的数据会漏回来。
   彻底删除只走 `/api/admin/clients/{id}/purge`（PDPA 被遗忘权）。
10. **LLM 调用**：一律走 `graph.llm_text()` / `graph.llm_tokens()`，它们带超时、
    重试和并发闸门。直接 `llm.invoke()` 会绕过这些保护。

## 架构速查
- LangGraph 图：Supervisor(关键词fast-path+LLM路由) → policy/clientbook/drafting/action/chat/fallback → compliance
- 模型：DeepSeek v3.2 @OpenRouter（graph.py，无 key 时回退本地 Ollama qwen2.5:7b）
- DB：SQLAlchemy，DATABASE_URL 有值用 Postgres(Neon)，否则 SQLite（hivora.db）
- 认证：HMAC token（auth.py），Header `Authorization: Bearer <token>`
- 前端：无框架单文件 SPA，i18n 用 I18N 字典 + t()，改 UI 文案两种语言都要加
- 日期：一律 `db.today()`，不要写死日期

## 风格
- Python：与现有代码一致（中文注释、简洁函数、SessionLocal try/finally）
- 前端：蓝白 Hivora 蓝 #1e6bf0，SVG 图标（禁 emoji 图标），手机端底部 5-tab 导航
- 提交信息：英文一行，说清做了什么

## 测试
```bash
cd server && .venv/bin/python -m pytest tests -q     # 43 个回归测试，改完必须全绿
```
覆盖：token 伪造/过期/停用即失效、多租户隔离、审计 agent_id、软删与硬删、
按 id 定位、AI 录入容错、流式事件、前端转义静态检查。
测试跑在临时 SQLite 上，不碰 `server/hivora.db`，也不会打真实模型。
CI（`.github/workflows/ci.yml`）跑同一套测试 + 生产守卫冒烟。
