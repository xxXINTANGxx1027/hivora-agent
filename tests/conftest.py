"""每个测试跑在独立的临时 SQLite 上，绝不碰开发库。"""
import itertools
import os
import pathlib
import sys
import tempfile

import pytest

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

_TMP = tempfile.mkdtemp(prefix="hivora-test-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{_TMP}/test.db",
    SECRET_KEY="t" * 64,
    ADMIN_EMAIL="admin@test.local",
    ADMIN_PASSWORD="Test-Admin-2026",
    DEMO_DATA="1",
    ALLOWED_ORIGINS="*",   # 浏览器冒烟的静态站端口随机，没法预先白名单
)
os.environ.pop("OPENROUTER_API_KEY", None)   # 测试绝不打真实模型
os.environ.pop("HIVORA_ENV", None)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """导入 main 会建表 + 灌演示数据。所有测试都依赖它先跑。"""
    import main
    return main


@pytest.fixture(scope="session")
def app_client():
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_token(app_client):
    r = app_client.post("/api/auth/login",
                        json={"email": "admin@test.local", "password": "Test-Admin-2026"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="session")
def demo_token(app_client):
    r = app_client.post("/api/auth/login",
                        json={"email": "demo@hivora.my", "password": "demo1234"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


_SEQ = itertools.count()


@pytest.fixture
def agent_factory(app_client, admin_token):
    """建一个全新的付费代理人账号，返回 (token, email)。邮箱全局唯一。"""
    made = []

    def _make(email=None, password="Agent-Pass-2026"):
        email = email or f"agent{next(_SEQ)}-{os.getpid()}@test.local"
        r = app_client.post("/api/admin/agents/create",
                            headers=H(admin_token),
                            json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        tok = app_client.post("/api/auth/login",
                              json={"email": email, "password": password}).json()["token"]
        made.append(email)
        return tok, email
    return _make


def H(token):
    return {"Authorization": f"Bearer {token}"}


# 浏览器冒烟的夹具（live_server / admin_site / browser / page）
from conftest_browser import *  # noqa: E402,F401,F403
