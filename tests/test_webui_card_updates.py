"""打标只更新当前卡片；HTTP 真实落库，浏览器验证无跳转及失败恢复。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
from bs4 import BeautifulSoup

from atlas.labels import open_store
from tests.test_webui_entries_render import (
    _entry_forms,
    _get,
    _NoRedirect,
    running_frontend,
)


def _json_label(app, fields):
    request = urllib.request.Request(
        app.base_url + "/label",
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.code, json.load(response), response.headers


def _fields(app):
    status, page, _ = _get(app, "/feed?order=asc")
    assert status == 200
    return next(fields for fields in _entry_forms(page) if fields["raw_id"] == "raw-feed-render")


@pytest.mark.parametrize("key,value", [("valid", "valid"), ("valid", "invalid"), ("industry", "web")])
def test_async_label_returns_persisted_entry_state_without_redirect(tmp_path, key, value):
    with running_frontend(tmp_path) as world:
        fields = {**_fields(world.app), "label_key": key, "label_value": value,
                  "return_to": "/feed?labeled=false&industry=ai&limit=1&offset=1"}
        status, result, headers = _json_label(world.app, fields)
        assert status == 200
        assert "Location" not in headers
        assert headers["Content-Type"].startswith("application/json")
        assert headers["Cache-Control"] == "no-store"
        assert result == {"raw_id": fields["raw_id"], "values": {key: value}}
        with open_store(world.db_path) as store:
            label, = store.all_for(fields["raw_id"])
            assert label.label_key == key and label.label_value == value
            assert label.actor == "tester"
            assert label.anchor.char_start == int(fields["char_start"])
            assert label.anchor.char_end == int(fields["char_end"])
        # 同一 feed 中的另一篇文章不继承这篇的人工标签。
        _, page, _ = _get(world.app, "/feed?labeled=true")
        soup = BeautifulSoup(page, "html.parser")
        cards = soup.select("article.item")
        assert len(cards) == 1 and "Alpha & Beta" in cards[0].get_text()


def test_async_label_uses_current_actor_and_exact_anchor_with_document_fallback(tmp_path):
    with running_frontend(tmp_path) as world:
        fields = _fields(world.app)
        world.app.labels.record(raw_id=fields["raw_id"], label_key="industry", label_value="ai", actor="tester")
        world.app.labels.record(raw_id=fields["raw_id"], label_key="valid", label_value="invalid", actor="probe")
        assert _json_label(world.app, fields)[1]["values"] == {"industry": "ai", "valid": "valid"}
        fields.update(label_key="industry", label_value="web")
        assert _json_label(world.app, fields)[1]["values"] == {"industry": "web", "valid": "valid"}
        # 重复提交必须显示实际存储的最新状态，不能直接回显本次请求值。
        fields.update(label_key="valid", label_value="invalid")
        assert _json_label(world.app, fields)[1]["values"]["valid"] == "invalid"
        fields["label_value"] = "valid"
        result = _json_label(world.app, fields)[1]
        with open_store(world.db_path) as store:
            anchor = next(label.anchor for label in store.all_for(fields["raw_id"])
                          if label.actor == "tester" and label.anchor is not None)
        with world.app.labels.read_session() as session:
            assert result["values"] == session.values_for_anchor(
                fields["raw_id"], anchor,
            )
        assert result["values"]["valid"] == "invalid"


@pytest.mark.parametrize("changes,expected", [
    ({"label_value": "not-a-valid-value"}, 400),
    ({"raw_sha256": "0" * 64}, 409),
])
def test_async_validation_errors_are_json_and_do_not_write(tmp_path, changes, expected):
    with running_frontend(tmp_path) as world:
        fields = {**_fields(world.app), **changes}
        status, result, headers = _json_label(world.app, fields)
        assert status == expected
        assert result["error"] and result["message"]
        assert "Location" not in headers
        with open_store(world.db_path) as store:
            assert store.count() == 0
        # 同一路径的合法请求仍可成功。
        assert _json_label(world.app, _fields(world.app))[0] == 200


def test_async_storage_failure_reports_error_and_can_retry(tmp_path, monkeypatch):
    with running_frontend(tmp_path) as world:
        fields = _fields(world.app)
        def fail(**kwargs):
            raise RuntimeError("test storage unavailable")
        with monkeypatch.context() as patch:
            patch.setattr(world.app.labels, "record", fail)
            status, result, _ = _json_label(world.app, fields)
        assert status == 500 and result["error"] == "打标写入失败"
        with open_store(world.db_path) as store:
            assert store.count() == 0
        assert _json_label(world.app, fields)[0] == 200


def test_async_document_label_and_saved_but_unreadable_state(tmp_path, monkeypatch):
    with running_frontend(tmp_path) as world:
        fields = {"raw_id": "raw-article-render", "label_key": "valid", "label_value": "valid",
                  "actor": "tester", "return_to": "/feed"}
        assert _json_label(world.app, fields)[1]["values"] == {"valid": "valid"}

        def fail():
            raise RuntimeError("test read unavailable")

        fields["label_value"] = "invalid"
        with monkeypatch.context() as patch:
            patch.setattr(world.app.labels, "read_session", fail)
            status, result, _ = _json_label(world.app, fields)
        assert status == 500 and "标签已保存" in result["message"]
        with open_store(world.db_path) as store:
            assert store.count() == 2
            assert store.latest_value(fields["raw_id"], "valid") == "invalid"
        assert _json_label(world.app, fields)[1]["values"] == {"valid": "invalid"}


@pytest.mark.parametrize("width,height,scheme", [(375, 812, "light"), (812, 375, "dark"), (1280, 900, "dark")])
def test_browser_labels_update_only_the_current_card(tmp_path, width, height, scheme):
    # 可选测试工具不加入应用依赖；干净环境可跳过，本次交付需实际运行。
    api = pytest.importorskip("playwright.sync_api", reason="需要 Playwright 和 Chromium 运行浏览器回归")
    with running_frontend(tmp_path) as world, api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        except api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("需先运行 playwright install chromium")
            raise
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme, reduced_motion="reduce")
            page.goto(world.app.base_url + "/feed?labeled=false&order=asc&limit=50")
            card = page.locator("article.item").filter(has_text="Alpha & Beta")
            sibling = page.locator("article.item").filter(has_text="Gamma Report")
            card.evaluate("el => { window.originalCard = el; }")
            sibling.evaluate("el => { window.sibling = el; window.siblingHTML = el.outerHTML; }")
            navigations, requests, errors = [], [], []
            page.on("framenavigated", lambda frame: navigations.append(frame.url))
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.on("pageerror", lambda error: errors.append(str(error)))
            card.locator(".industry-editor summary").click()
            card.locator(".record-details summary").click()
            card.locator('select[name="label_value"]').select_option("web")
            assert card.locator('.meta').first.evaluate("el => el.nextElementSibling.classList.contains('tag-row')")
            assert card.locator('.tag-row .industry-badges').count() == 1
            assert card.locator('.tag-row .labels').count() == 1
            assert "人工确认" not in card.inner_text() and "已打标" not in card.inner_text()
            assert card.locator('.vote-button svg').count() == 2
            assert card.locator('.vote-button').first.inner_text() == ""

            # 暂停真实请求，验证保存反馈、禁止重复提交，然后放行真实落库。
            pending = []
            page.route("**/label", lambda route: pending.append(route))
            valid = card.get_by_role("button", name="标记有效", exact=True)
            valid.scroll_into_view_if_needed()
            before_scroll = page.evaluate("window.scrollY")
            valid.click()
            page.wait_for_function("document.querySelector('article[aria-busy]') !== null")
            assert card.locator(".label-status").inner_text() == "正在保存…"
            assert card.locator(".actions button:enabled").count() == 0
            card.locator("form.act").first.evaluate("el => el.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}))")
            assert len(pending) == 1
            pending.pop().continue_()
            page.unroute("**/label")
            card.locator(".label-status").filter(has_text="已保存").wait_for()
            assert "valid=" not in card.inner_text()
            assert card.locator('[data-label-key="valid"]').count() == 0
            assert valid.get_attribute('aria-pressed') == 'true'
            valid_color = valid.evaluate("el => getComputedStyle(el).backgroundColor")
            assert abs(page.evaluate("window.scrollY") - before_scroll) <= 1
            assert card.locator('select[name="label_value"]').input_value() == "web"

            card.get_by_role("button", name="标记无效", exact=True).click()
            card.locator('.act-invalid .vote-button[aria-pressed="true"]').wait_for()
            page.wait_for_function("document.querySelector('article[aria-busy]') === null")
            assert "valid=" not in card.inner_text()
            assert valid.get_attribute('aria-pressed') == 'false'
            assert card.get_by_role('button', name='标记无效', exact=True).get_attribute('aria-pressed') == 'true'
            assert card.locator('.act-invalid .vote-button').evaluate("el => getComputedStyle(el).backgroundColor") != valid_color

            # 网络失败不改标签，不离开页面；重试仍走同一个表单。
            page.route("**/label", lambda route: route.abort())
            card.get_by_role("button", name="修正行业", exact=True).click()
            card.locator(".label-status[data-error]").wait_for()
            assert card.locator('.tag-manual').count() == 0
            assert card.get_by_role("button", name="修正行业", exact=True).is_enabled()
            page.unroute("**/label")
            card.get_by_role("button", name="修正行业", exact=True).click()
            card.locator(".industry-badges .tag-manual").filter(has_text="web").wait_for()
            page.wait_for_function("document.querySelector('article[aria-busy]') === null")
            assert card.locator('[data-label-key="industry"]').inner_text() == 'web'
            assert "人工确认" not in card.inner_text() and "已打标" not in card.inner_text()

            # 服务端拒绝也在原卡片提示；保留先前成功的标签。
            card.locator('select[name="label_value"]').evaluate("el => el.add(new Option('invalid', 'invalid', true, true))")
            card.get_by_role("button", name="修正行业", exact=True).click()
            card.locator(".label-status[data-error]").wait_for()
            assert "不在当前配置" in card.locator(".label-status").inner_text()
            assert card.locator('[data-label-key="industry"]').inner_text() == 'web'
            assert card.locator(".industry-editor").evaluate("el => el.open")
            assert card.locator(".record-details").evaluate("el => el.open")
            assert card.evaluate("el => el === window.originalCard")
            assert sibling.evaluate("el => el === window.sibling && el.outerHTML === window.siblingHTML")
            assert not navigations and not errors
            assert requests and all(method == "POST" and url.endswith("/label") for method, url in requests)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            with open_store(world.db_path) as store:
                assert store.count() == 3
            # 整页重新读取后，标签位置和选中图标与局部更新结果保持一致。
            page.goto(world.app.base_url + '/feed?order=asc&limit=50')
            assert card.locator('.tag-row .tag-manual').inner_text() == 'web'
            assert "valid=" not in card.inner_text()
            assert card.locator('[data-label-key="valid"]').count() == 0
            assert card.get_by_role('button', name='标记无效', exact=True).get_attribute('aria-pressed') == 'true'
            assert '人工确认' not in card.inner_text() and '已打标' not in card.inner_text()
        finally:
            browser.close()
