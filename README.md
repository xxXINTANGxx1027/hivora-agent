# Hivora Agent — 生产后端（FastAPI + LangGraph + SQLAlchemy）

总手册见仓库外层 `hivora/README.md`；AI 编码规则见本仓库 `AGENTS.md`；路线图见 `hivora/ROADMAP.md`。

## 快速开始
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # 填 OPENROUTER_API_KEY
.venv/bin/uvicorn main:app --port 8791 --reload
# http://localhost:8791 · 管理员 admin123/admin123
```

## 部署
push main → Render 自动部署（render.yaml Blueprint `hivora-stage`）。
环境变量：OPENROUTER_API_KEY（必填）/ DATABASE_URL（Neon Postgres，缺省 SQLite）/ SECRET_KEY / ADMIN_EMAIL / ADMIN_PASSWORD。

## 文件
main.py=API · graph.py=LangGraph 大脑 · db.py=模型+seed · auth.py=认证 · knowledge.py=条款检索 · static/=前端 SPA
改 static/index.html 后同步 `cp static/index.html ../frontend/index.html`，两 repo 都 push。
