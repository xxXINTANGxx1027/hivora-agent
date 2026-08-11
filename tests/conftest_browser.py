"""浏览器冒烟测试用的夹具：起一个真的 uvicorn，再起一个静态站放管理页。

为什么需要：接口测试全绿也可能整个界面点不动 —— 一个内联 `display:flex`
压过 `.hide`，浮层就会盖住全屏拦掉所有点击。这类问题只有真浏览器能发现。
"""
import http.server
import pathlib
import socket
import threading
import time

import pytest

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
ROOT = SERVER_DIR.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 30):
    import urllib.error
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"{url} 没起来")


@pytest.fixture(scope="session")
def live_server(_schema):
    """真的 uvicorn —— TestClient 不监听端口，浏览器连不上。"""
    import uvicorn
    import main

    port = _free_port()
    cfg = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _wait(base + "/healthz")
    yield base
    server.should_exit = True


@pytest.fixture(scope="session")
def admin_site(live_server, tmp_path_factory):
    """把 admin/index.html 的后端地址指向测试服务器，用静态服务器托管。

    不能走 file://，那样浏览器的来源是 null，请求会被 CORS 挡掉。
    """
    src = ROOT / "admin" / "index.html"
    if not src.exists():
        pytest.skip("admin/index.html 不存在")
    d = tmp_path_factory.mktemp("admin-site")
    html = src.read_text(encoding="utf-8").replace(
        '<meta name="hivora-api" content="https://hivora-agent-stage.onrender.com">',
        f'<meta name="hivora-api" content="{live_server}">')
    (d / "index.html").write_text(html, encoding="utf-8")

    port = _free_port()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(d), **kw)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _wait(base + "/index.html")
    yield base
    httpd.shutdown()


@pytest.fixture(scope="session")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                     # 没下过 chromium 的机器
            pytest.skip(f"chromium 起不来：{e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.hivora_errors = errors
    yield pg
    ctx.close()
