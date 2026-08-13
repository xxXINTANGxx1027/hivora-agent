# AGENTS.md — 给 AI 编码助手（Cursor/Claude）的项目规则

## 项目是什么
Hivora：马来西亚保险代理人 AI Copilot（closed SaaS，账号内部发放）。
Python + FastAPI + LangGraph + SQLAlchemy 后端，单文件 HTML SPA 前端。
线上：前端 Vercel（hivora-frontend.vercel.app）+ 后端 Render（hivora-agent-stage.onrender.com）。

## 接到一个工作包？
先看 [WORKPLAN.md](WORKPLAN.md) —— 剩下的活拆成了独立的工作包，
写清了改哪些文件、验收标准、以及哪些包之间会冲突不能并行。

## 主开发目录
`server/`（= GitHub repo hivora-agent，push main 即自动部署 Render）。
`admin/` 是内部管理站的**唯一来源**（= repo hivora-admin）。线上那份由
`./sync-frontend.sh` 生成到 `server/static/console.html`，跟后端同源挂在 `/console`
—— 改完 `admin/index.html` 必须跑一次同步脚本，否则 pre-push 钩子会拦。
`pages-demo/` / `pages-stage/` 是冻结的静态 demo，**不要在里面开发**。

## 前后台分离（V0.1 起）
客户拿到的包里**一行管理代码都不能有**。管理功能只写在 `admin/index.html`，
客户端只写在 `server/static/index.html`。`sync-frontend.sh` 会在同步和 pre-push
时检查 `frontend/index.html` 里有没有 `api/admin`，有就直接拒绝。

## 铁律（违反=事故）
1. **数据隔离**：任何涉及 clients/policies/appointments/facts/threads/documents 的查询，
   必须 `filter_by(agent_id=...)`，agent_id 只能来自 `auth.current_agent` 依赖，绝不信任请求体。
2. **合规（2026-08-12 改过，别按旧版本理解）**：
   不得实现"跨保险公司比价/推荐哪家好"（BNM 红线）；条款查不到就说查不到。

   **面向客户是混合策略，不是全自动、也不再是全人工**：
   - 只有**条款类**问题才自动回，且必须查得到依据，回复带出处 + 免责声明 +
     一条「回复『人工』找真人」的出路
   - 以下一律进收件箱等真人：理赔 / 核保 / 报价 / 推荐 / 投诉 / 退保、
     客户说「人工」、条款库无依据、配额用完、模型不可用
   - **转人工的原因绝不能发给客户** —— 里面可能是「本月配额用完了」
   - 判断逻辑集中在 `graph.customer_reply()`，改它之前先看
     `tests/test_telegram.py` 里那一组合规测试

   面向代理人本人（绑过码的 Telegram、网页 dashboard）不受此限，照常回答。
3. **密码不走邮件，也不归管理员**：
   - 开通和重置都发一次性链接（`auth.new_setup_token`，48 小时、只能用一次、
     重发即作废），对方点进 `?setup=<token>` **自己设**
   - 本人可随时改（`POST /api/password`，**必须验旧密码** —— 否则谁偷到 token
     就能把账号锁走）；改完之前发出的链接一律作废
   - 管理员**看不到也设不了**别人的真密码，只能重发链接
   - `email_out` 里绝不能出现明文密码
4. **密钥**：绝不把 API key/连接串写进代码或提交 git。用 .env（本地）/ Render Environment（云端）。
   `server/.env` 在 .gitignore 里，保持这样。
5. **审计**：新增的写操作和 AI 动作要调用 `db.audit(session, agent_id, action, detail)`。
   `agent_id` 必填且只能来自 `auth.current_agent` / `auth.current_admin`。
6. **前端转义**：任何服务端数据（客户名、消息正文、文件名、**LLM 输出**）进 `innerHTML`
   前必须过 `esc()`。客户发一条带 `<img onerror>` 的 WhatsApp 消息就能偷走 token。
7. **产品目录也是租户数据**：`products` 表有 `agent_id`，读写都要带上。
8. **虚构条款**：`knowledge.POLICY_CHUNKS` 是假的示例条款，只给演示账号。
   绝不能让真实用户拿到——AI 会带着"第X页"的出处引用它们。
9. **前端同步**：`server/static/index.html` 是唯一来源。改完跑 `./sync-frontend.sh`
   生成 `frontend/index.html`（只有 `<meta name="hivora-api">` 不同），**不要手动 cp**。
   两个 repo 都装了 pre-push 钩子，不同步会拒绝 push。
10. **软删**：clients/policies/appointments/facts/documents 都有 `deleted` 列。
   任何面向用户的读取都要包 `db.live(query, Model)`，否则删掉的数据会漏回来。
   彻底删除只走 `/api/admin/clients/{id}/purge`（PDPA 被遗忘权）。
11. **LLM 调用**：一律走 `graph.llm_text(prompt, agent_id)` / `graph.llm_tokens(...)`，
    它们带超时、重试、并发闸门、**配额检查和 token 记账**。直接 `llm.invoke()`
    会绕过全部保护，还会让这个客户的成本统计凭空少一块。
12. **列表接口**：一律用 `_capped(rows, 名字, aid)` 收口。截断要打日志——
    静默丢数据会让人以为"就这么多"。
13. **聚合用 SQL**：统计类查询别把整表拉进内存再数（dashboard 原来就这么干的）。
14. **白牌**：界面标题、AI 自称、给客户的消息一律走 `db.brand_of(agent_id)`，
    **不要再往代码里写死 "Hivora"**。租户没设就回落 `DEFAULT_BRAND`。
15. **Telegram**：客户渠道 + 代理人助手共用一个 bot（代理人自己在 BotFather 建）。
    - **绑过码 = 代理人本人**，提问走 `ask()`，配额/合规/审计照常
    - **没绑过 = 客户**，消息只进收件箱，**绝不能让 AI 自动回复**（铁律 2）。
      客户只收到 `telegram.CUSTOMER_ACK` 这句写死的回执
    - token 用 Fernet 加密存、接口只回后 4 位；webhook 靠随机路径 + secret header 认身份
    - 代理人在网页点发送 → 通过 `telegram.send_to_chat` 真的发回客户
16. **不可逆操作要服务端二次确认**：停用账号要打一遍邮箱、彻底删除客户要打一遍姓名。
    **确认必须在服务端校验** —— 只靠前端弹窗挡不住误调接口和写错的脚本。
    启用、改套餐这类可逆的不要加，否则管理员会养成盲点确认的习惯。
17. **管理员看不到租户业务数据**：`/api/admin/clients` 只返回姓名电话和保单数，
    是为 PDPA 删除服务的。对话内容、条款、AI 回复一律不给管理员。
    「以租户身份查看」这个功能**明确不做** —— 项目所有者 2026-08-12 决定。
18. **线索 vs 客户**：客户是主动找上门的，第一次接触时还不在库里。
    `Thread.client_id` 为空 = 新线索。`_ctx_for` 优先按 `client_id` 取档案，
    没关联才按名字兜底 —— 所以「加为客户」不只是整理数据，它决定了 AI 起草
    能不能看到这个人的保单。

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
cd server
.venv/bin/python -m pytest tests -q --ignore=tests/test_browser_smoke.py  # 接口层，2 秒
.venv/bin/python -m pytest tests/test_browser_smoke.py -q                 # 真浏览器，约 40 秒
```
接口层覆盖：token 伪造/过期/停用即失效、多租户隔离、审计 agent_id、软删与硬删、
按 id 定位、AI 录入容错、流式事件、管理接口、前端转义与前后台分离的静态检查。

**浏览器冒烟不是可选的。** 有两类 bug 接口测试永远看不见，而且都真实发生过：
浮层的内联 `display` 压过 `.hide`，界面整个点不动；`applyLang()` 中途抛异常，
后面所有文案都不再更新。改动前端后请跑一遍。首次要装：
`.venv/bin/pip install playwright && .venv/bin/playwright install chromium`

测试跑在临时 SQLite 上，不碰 `server/hivora.db`，也不会打真实模型。
