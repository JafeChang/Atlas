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
            page.on("request", lambda request: requests.append((request.method, request.url)) if request.url.endswith("/label") else None)
            page.on("pageerror", lambda error: errors.append(str(error)))

            # 归档详情现在位于弹窗中；关闭弹窗后其展开状态仍应保留。
            card.get_by_role("button", name="更多操作").click()
            sheet = card.get_by_role("dialog", name="文章操作")
            sheet.get_by_text("归档详情", exact=True).click()
            assert sheet.locator(".raw-id").is_visible()
            sheet.get_by_role("button", name="关闭").click()
            assert not sheet.is_visible()
            assert card.locator(".record-details").evaluate("el => el.open")

            # 当前界面使用标签文本输入框，保留未提交的内容以检测卡片是否重建。
            card.locator(".industry-editor summary").click()
            tag_input = card.get_by_label("标签名称")
            tag_input.fill("尚未提交的标签")
            assert card.locator('.meta').first.evaluate("el => el.nextElementSibling.classList.contains('tag-row')")
            assert card.locator('.tag-row .industry-badges').count() == 1
            assert card.locator('.tag-row .labels').count() == 1
            assert "人工确认" not in card.inner_text() and "已打标" not in card.inner_text()
            assert card.locator('.vote-button svg').count() == 2
            assert card.locator('.vote-button').first.inner_text() == ""

            # 暂停真实请求，验证保存反馈和防重复提交，再放行真实落库。
            pending = []
            page.route("**/label", lambda route: pending.append(route))
            valid = card.get_by_role("button", name="标记有效", exact=True)
            invalid = card.get_by_role("button", name="标记无效", exact=True)
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
            assert tag_input.input_value() == "尚未提交的标签"

            # 网络失败保留原判断和页面；重试成功后只更新当前卡片。
            page.route("**/label", lambda route: route.abort())
            invalid.click()
            card.locator(".label-status[data-error]").wait_for()
            assert valid.get_attribute('aria-pressed') == 'true'
            assert invalid.get_attribute('aria-pressed') == 'false'
            assert invalid.is_enabled()
            page.unroute("**/label")
            invalid.click()
            card.locator('.act-invalid .vote-button[aria-pressed="true"]').wait_for()
            page.wait_for_function("document.querySelector('article[aria-busy]') === null")
            assert valid.get_attribute('aria-pressed') == 'false'
            assert invalid.evaluate("el => getComputedStyle(el).backgroundColor") != valid_color

            # 非法输入由服务端拒绝，之前成功的判断仍显示为选中。
            valid_value = valid.locator('xpath=..').locator('input[name="label_value"]')
            valid_value.evaluate("el => el.value = 'not-valid'")
            valid.click()
            card.locator(".label-status[data-error]").wait_for()
            assert "只接受" in card.locator(".label-status").inner_text()
            assert invalid.get_attribute('aria-pressed') == 'true'
            valid_value.evaluate("el => el.value = 'valid'")
            assert card.locator(".record-details").evaluate("el => el.open")
            assert tag_input.input_value() == "尚未提交的标签"
            assert card.evaluate("el => el === window.originalCard")
            assert sibling.evaluate("el => el === window.sibling && el.outerHTML === window.siblingHTML")
            assert not navigations and not errors
            assert requests and all(method == "POST" for method, _ in requests)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            with open_store(world.db_path) as store:
                assert store.count() == 2

            # 整页重新读取后，选中图标与局部更新结果保持一致。
            page.goto(world.app.base_url + '/feed?order=asc&limit=50')
            assert "valid=" not in card.inner_text()
            assert card.locator('[data-label-key="valid"]').count() == 0
            assert card.get_by_role('button', name='标记无效', exact=True).get_attribute('aria-pressed') == 'true'
            assert '人工确认' not in card.inner_text() and '已打标' not in card.inner_text()
        finally:
            browser.close()
