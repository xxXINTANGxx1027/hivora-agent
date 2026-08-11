"""真浏览器冒烟：接口全绿但界面点不动的那类 bug，只有这里能拦住。

起因是一个真实事故：回收站浮层的内联 `display:flex` 压过了 `.hide`，
浮层常驻全屏，整个应用无法点击 —— 而当时 60 个接口测试全部通过。

Playwright 的 click 自带可操作性检查：元素被别的东西盖住时会直接失败，
正好就是这类 bug 的特征。
"""
import pytest

pytest.importorskip("playwright.sync_api")


def _login(page, base, email, pw):
    """登录并等登录框消失。注意是等它 hidden——等 `.hide` 元素「可见」永远等不到。"""
    page.goto(base, wait_until="load")
    box = "#login-overlay" if page.locator("#login-overlay").count() else "#login"
    page.wait_for_selector(f"{box}:not(.hide)", timeout=15000)
    page.fill("#lg-email", email)
    page.fill("#lg-pw", pw)
    page.click(f"{box} button")
    page.wait_for_selector(box, state="hidden", timeout=15000)
    page.hivora_errors.clear()   # 只关心登录之后的交互有没有报错


def _assert_nothing_covers_the_page(page):
    """页面正中央的元素不该是某个全屏浮层。"""
    tag = page.evaluate("""() => {
        const el = document.elementFromPoint(innerWidth/2, innerHeight/2);
        if (!el) return "null";
        for (let n = el; n; n = n.parentElement) {
            const cs = getComputedStyle(n);
            if (cs.position === "fixed" && cs.display !== "none" &&
                n.getBoundingClientRect().width >= innerWidth * 0.9 &&
                n.getBoundingClientRect().height >= innerHeight * 0.9)
                return "COVERED:" + (n.id || n.className);
        }
        return "ok";
    }""")
    assert tag == "ok", f"有全屏元素盖住了页面：{tag}"


def _assert_no_js_errors(page):
    real = [e for e in page.hivora_errors
            if "favicon" not in e and "404" not in e]
    assert not real, f"控制台报错：{real[:3]}"


# ── 客户端 ────────────────────────────────────────────────────
def test_client_app_is_clickable_everywhere(live_server, page, agent_factory):
    """逐个点开每一页。任何一页被浮层盖住，click 就会超时失败。"""
    _, email = agent_factory()
    _login(page, live_server, email, "Agent-Pass-2026")
    _assert_nothing_covers_the_page(page)

    for view in ("dash", "inbox", "train", "clients", "products", "calendar", "renewals"):
        page.click(f'nav button[data-v="{view}"]')
        page.wait_for_selector(f"#v-{view}:not(.hide)", timeout=10000)
        _assert_nothing_covers_the_page(page)
    _assert_no_js_errors(page)


def test_trash_overlay_opens_and_closes(live_server, page, agent_factory):
    """回收站正是出事的那个浮层：开得起来，也必须关得掉。"""
    _, email = agent_factory()
    _login(page, live_server, email, "Agent-Pass-2026")
    page.click('nav button[data-v="clients"]')

    page.click("#trash-btn")
    page.wait_for_selector("#trash-overlay:not(.hide)", timeout=10000)
    assert page.locator("#trash-body").is_visible()

    page.click("text=关闭")
    page.wait_for_selector("#trash-overlay", state="hidden", timeout=10000)
    _assert_nothing_covers_the_page(page)
    # 关掉之后底下的按钮必须还能点
    page.click('nav button[data-v="dash"]')
    page.wait_for_selector("#v-dash:not(.hide)", timeout=10000)
    _assert_no_js_errors(page)


def test_client_can_add_and_delete_a_client(live_server, page, agent_factory):
    """走一遍完整的增删链路，顺带验证弹窗不会卡住界面。"""
    _, email = agent_factory()
    _login(page, live_server, email, "Agent-Pass-2026")
    page.click('nav button[data-v="clients"]')

    page.click("text=＋ 新增客户")
    page.wait_for_selector("#modal:not(.hide)", timeout=10000)
    page.fill("#m-name", "浏览器测试客户")
    page.click("#modal-save")
    page.wait_for_selector("#modal", state="hidden", timeout=10000)
    page.wait_for_selector("text=浏览器测试客户", timeout=10000)
    _assert_nothing_covers_the_page(page)

    page.once("dialog", lambda d: d.accept())
    page.click('#client-cards button[aria-label="delete"]')
    page.wait_for_selector("text=浏览器测试客户", state="detached", timeout=10000)

    page.click("#trash-btn")
    page.wait_for_selector("#trash-overlay:not(.hide)", timeout=10000)
    # 浮层先显示、数据后到，要等内容真的渲染出来
    page.wait_for_selector("#trash-body >> text=浏览器测试客户", timeout=10000)
    _assert_no_js_errors(page)


def test_language_toggle_relabels_everything(live_server, page, agent_factory):
    """中英切换。以前 applyLang 在中途抛异常，后面所有文案都不会更新——
    接口测试永远看不见，只有真浏览器能发现。"""
    _, email = agent_factory()
    _login(page, live_server, email, "Agent-Pass-2026")
    page.click('nav button[data-v="clients"]')
    page.wait_for_selector("#v-clients:not(.hide)", timeout=10000)

    zh = {sel: page.locator(sel).inner_text() for sel in
          ("#btn-add-client", "#btn-add-policy", "#trash-btn")}
    assert "新增客户" in zh["#btn-add-client"]
    assert "回收站" in zh["#trash-btn"], f"回收站按钮被改错了文案：{zh['#trash-btn']}"

    page.click("#lang-btn")
    page.wait_for_function(
        """() => document.querySelector('#btn-add-client')
                 .textContent.includes('New Client')""", timeout=10000)
    assert "Add Policy" in page.locator("#btn-add-policy").inner_text()
    assert "Trash" in page.locator("#trash-btn").inner_text()
    assert page.locator("#v-clients h1").inner_text() == "Client Database"

    page.click("#lang-btn")   # 切回中文
    page.wait_for_function(
        """() => document.querySelector('#btn-add-client')
                 .textContent.includes('新增客户')""", timeout=10000)
    _assert_no_js_errors(page)


def test_client_app_has_no_admin_ui(live_server, page, agent_factory):
    """代理人界面里不该出现任何管理入口。"""
    _, email = agent_factory()
    _login(page, live_server, email, "Agent-Pass-2026")
    html = page.content()
    for token in ("管理后台", "邀请码", "创建账号"):
        assert token not in html, f"客户端里出现了管理入口：{token}"


# ── 管理站 ────────────────────────────────────────────────────
def test_admin_site_loads_and_lists_agents(admin_site, page):
    _login(page, admin_site, "admin@test.local", "Test-Admin-2026")
    page.wait_for_selector("#tb-agents tr", timeout=15000)
    assert "admin@test.local" in page.locator("#tb-agents").inner_text()
    _assert_nothing_covers_the_page(page)
    _assert_no_js_errors(page)


def test_admin_tabs_all_clickable(admin_site, page):
    _login(page, admin_site, "admin@test.local", "Test-Admin-2026")
    for view in ("agents", "audit", "pdpa"):
        page.click(f'nav.tabs button[data-v="{view}"]')
        page.wait_for_selector(f"#v-{view}:not(.hide)", timeout=10000)
        _assert_nothing_covers_the_page(page)
    _assert_no_js_errors(page)


def test_admin_rejects_non_admin_login(admin_site, page, agent_factory):
    """普通代理人拿自己的账号登管理站，必须被挡在门外。"""
    _, email = agent_factory()
    page.goto(admin_site, wait_until="domcontentloaded")
    page.fill("#lg-email", email)
    page.fill("#lg-pw", "Agent-Pass-2026")
    page.click("#login button")
    page.wait_for_selector("#lg-err:not(:empty)", timeout=10000)
    assert "管理员" in page.locator("#lg-err").inner_text()
    assert not page.locator("#login").evaluate("el => el.classList.contains('hide')")
    assert page.evaluate("() => localStorage.getItem('hivora_admin_token')") is None


def test_admin_create_account_modal_works(admin_site, page):
    _login(page, admin_site, "admin@test.local", "Test-Admin-2026")
    page.click("text=＋ 创建账号")
    page.wait_for_selector("#modal:not(.hide)", timeout=10000)
    page.fill("#f-email", "browser-made@test.local")
    page.fill("#f-password", "Browser-Made-2026")
    page.fill("#f-name", "浏览器建的")
    page.click("#m-save")
    page.wait_for_selector("#modal", state="hidden", timeout=15000)
    page.wait_for_selector("text=browser-made@test.local", timeout=10000)
    _assert_nothing_covers_the_page(page)
    _assert_no_js_errors(page)
