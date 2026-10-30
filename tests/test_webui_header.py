"""真实 Chromium 验证有账号、长名称和大分页数字时，页头仍保持两行。"""

import pytest

from tests.test_webui_entries_render import running_frontend


@pytest.mark.parametrize("width", [320, 375, 1100, 1366, 1500])
def test_header_keeps_two_rows_and_menus_work(tmp_path, width):
    api = pytest.importorskip("playwright.sync_api")
    with (
        running_frontend(tmp_path, require_login=True) as world,
        api.sync_playwright() as p,
    ):
        name = "A very long overseas aluminium source name with complete context"
        world.app.server.source_management.channel_names = lambda: {"chan-feed": name}
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(world.app.base_url + "/login")
            page.locator("#name").fill("alice")
            page.get_by_role("button", name="登录", exact=True).click()
            page.wait_for_url("**/feed")
            page.goto(world.app.base_url + "/feed?channel=chan-feed&limit=1")
            # Stress the same header with the counter values in the user's screenshot.
            page.locator(".compact-count").evaluate(
                "el => el.textContent = '1–50 / 41863'"
            )
            page.locator(".page-number").evaluate("el => el.textContent = '1/838'")
            header = page.locator(".feed-header").bounding_box()
            top = page.locator(".feed-header-main").bounding_box()
            bottom = page.locator(".feed-header-controls").bounding_box()
            assert header["height"] <= 104
            assert top["y"] + top["height"] <= bottom["y"]
            for selector in (
                ".feed-brand",
                ".feed-source-management",
                ".filter-panel summary",
                ".theme-toggle",
                ".feed-user summary",
            ):
                box = page.locator(selector).bounding_box()
                assert top["x"] - 1 <= box["x"]
                assert box["x"] + box["width"] <= top["x"] + top["width"] + 1
                assert box["y"] + box["height"] <= top["y"] + top["height"] + 1
            last_right = bottom["x"]
            for selector in (".source-menu summary", ".quick", ".compact-pager"):
                box = page.locator(selector).bounding_box()
                assert box["x"] >= last_right - 1
                assert bottom["y"] <= box["y"]
                assert box["y"] + box["height"] <= bottom["y"] + bottom["height"] + 1
                assert box["x"] + box["width"] <= bottom["x"] + bottom["width"] + 1
                last_right = box["x"] + box["width"]
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator(".source-menu summary").get_attribute("title") == name
            page.locator(".source-menu summary").click()
            assert page.locator(".source-menu nav").is_visible()
            page.keyboard.press("Escape")
            assert not page.locator(".source-menu nav").is_visible()
            page.locator(".filter-panel summary").click()
            filters = page.locator(".filter-panel .filters").bounding_box()
            assert filters["x"] >= 0 and filters["x"] + filters["width"] <= width
            page.locator(".feed-brand").click()
            assert not page.locator(".filter-panel .filters").is_visible()
            page.locator(".feed-user summary").click()
            assert (
                page.locator(".feed-user").get_by_text("当前用户：alice").is_visible()
            )
            page.keyboard.press("Escape")
            assert not page.locator(".feed-user > div").is_visible()
            page.get_by_role("button", name="切换到深色模式").click()
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            page.locator(".compact-pager").get_by_role("link", name="下一页").click()
            page.wait_for_url("**offset=1**")
            assert "channel=chan-feed" in page.url
            assert errors == []
        finally:
            browser.close()
