# Hivora Insurance Agent · Stage (Production Skeleton)

Python + FastAPI + LangGraph + SQLAlchemy。真实数据落库（SQLite 本地 / Postgres 云端）。

## 本地运行
```bash
pip install -r requirements.txt
cp .env.example .env   # 填 OPENROUTER_API_KEY
uvicorn main:app --port 8791
```

## 部署 (Render)
render.yaml Blueprint；环境变量：OPENROUTER_API_KEY（必填）、DATABASE_URL（Postgres，可选，缺省 SQLite）。

架构：Supervisor → Policy/ClientBook/Drafting/Action → Compliance（LangGraph）；前端 static/index.html。
