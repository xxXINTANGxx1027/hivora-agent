# AGENTS.md — 给 AI 编码助手（Cursor/Claude）的项目规则

## 项目是什么
Hivora：马来西亚保险代理人 AI Copilot（closed SaaS，账号内部发放）。
Python + FastAPI + LangGraph + SQLAlchemy 后端，单文件 HTML SPA 前端。
线上：前端 Vercel（hivora-frontend.vercel.app）+ 后端 Render（hivora-agent-stage.onrender.com）。

## 主开发目录
`server/`（= GitHub repo hivora-agent，push main 即自动部署 Render）。
`simulation/` 和 `pages-demo/` 是历史版本，**不要在里面开发**。

## 铁律（违反=事故）
1. **数据隔离**：任何涉及 clients/policies/appointments/facts/threads/documents 的查询，
   必须 `filter_by(agent_id=...)`，agent_id 只能来自 `auth.current_agent` 依赖，绝不信任请求体。
2. **合规**：不得实现"跨保险公司比价/推荐哪家好"功能（BNM 监管红线）；
   Policy/ClientBook 的 AI 回答必须经 Compliance 节点加免责声明；条款查不到就说查不到。
3. **密钥**：绝不把 API key/连接串写进代码或提交 git。用 .env（本地）/ Render Environment（云端）。
   `server/.env` 在 .gitignore 里，保持这样。
4. **审计**：新增的写操作和 AI 动作要调用 `db.audit(session, action, detail)`。
5. **前端同步**：改 `server/static/index.html` 后必须 `cp` 到 `frontend/index.html` 并两边 push，
   否则 Vercel 上是旧版。

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

## 测试方式（无测试框架，用 curl 冒烟）
```bash
TOK=$(curl -s -X POST localhost:8791/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin123","password":"admin123"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
curl -s localhost:8791/api/dashboard -H "Authorization: Bearer $TOK"
```
