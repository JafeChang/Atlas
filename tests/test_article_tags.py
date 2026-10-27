import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from bs4 import BeautifulSoup

from atlas.archive import open_archive
from atlas.cognition.admission import AdmissionClient, AdmissionStore, read_admissions
from atlas.webapp import ArchiveEntriesAccess
from tests.test_cognition_support import fake_config
from tests.test_feed_refresh import put
from tests.test_webui_entries_render import _get, _post, running_frontend


def test_manual_tags_are_unique_per_article_and_filter_before_pagination(tmp_path):
    with running_frontend(tmp_path) as world:
        status, page, _ = _get(world.app, '/feed?limit=200')
        cards = BeautifulSoup(page, 'html.parser').select('article.entry')
        siblings = [c for c in cards if c.select_one('input[name=raw_id]')['value'] == 'raw-feed-render']
        assert len(siblings) == 2
        assert all('is-empty' in c.select_one('.tag-row').get('class', []) for c in siblings)
        assert all(not c.select_one('.tag-row').get_text(strip=True) for c in siblings)
        # 渠道默认为 ai，但没有文章级分类时不能把它当作文章标签。
        assert all('ai' not in [a.get_text() for a in c.select('.industry-badges a')] for c in siblings)
        assert _get(world.app, '/feed?tag=ai')[1].count('class="count">共 0 条') > 0
        def fields(card, tag):
            form = card.select_one('form input[name=label_key][value=tag]').parent
            return {i['name']: i.get('value', '') for i in form.select('input')} | {'label_value': tag}
        for card, tag in [(siblings[0], '严肃内容'), (siblings[0], '航天'), (siblings[0], '严肃内容'), (siblings[1], '严肃内容')]:
            assert _post(world.app, fields(card, tag))[0] == 303
        status, page, _ = _get(world.app, '/feed?tag=' + quote('严肃内容') + '&limit=1')
        assert status == 200 and '共 2 条' in page
        status, page, _ = _get(world.app, '/feed?tag=' + quote('航天') + '&limit=200')
        assert status == 200 and '共 1 条' in page
        card = BeautifulSoup(page, 'html.parser').select_one('article.entry')
        texts = [a.get_text() for a in card.select('.industry-badges a')]
        assert texts.count('严肃内容') == 1 and texts.count('航天') == 1
        assert 'is-empty' not in card.select_one('.tag-row').get('class', [])
        assert _post(world.app, fields(siblings[0], 'x' * 81))[0] == 400


def test_ai_admission_tags_persist_and_filter(tmp_path, monkeypatch):
    root = tmp_path / 'store'
    record = put(root)
    with open_archive(root) as archive:
        source = next(iter(ArchiveEntriesAccess(archive).brief_sources(record.raw_id).values()))
    item = {'id': '1', 'decision': 'accepted', 'reason_code': 'substantive', 'reason': '实质内容', 'evidence': '', 'tags': ['科技', '严肃内容', '科技']}
    monkeypatch.setattr('atlas.cognition.admission.requests.post', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'reviews': [item]})}}]}))
    client = AdmissionClient(fake_config())
    reviews, _ = client.review([source], [])
    assert reviews[source.key].tags == ('科技', '严肃内容')
    store = AdmissionStore(root / 'atlas.db')
    state = read_admissions(root / 'atlas.db')
    store.record(source, reviews[source.key], policy=state.policy)
    store.enable()
    store.close()
    assert read_admissions(root / 'atlas.db').reviews[source.key].tags == ('科技', '严肃内容')
    from atlas.webapp import build_webapp
    app = build_webapp(root, db_path=root / 'atlas.db', industry_provider=lambda: ('ai',), industry_of={'feed': 'ai'})
    try:
        status, page, _ = _get(app, '/feed?tag=' + quote('严肃内容'))
        assert status == 200 and 'Article One' in page and '科技' in page
        status, page, _ = _get(app, '/feed?tag=' + quote('不存在'))
        assert status == 200 and '共 0 条' in page
    finally:
        app.close()
    item['tags'] = []
    with pytest.raises(ValueError, match='标签'):
        client.review([source], [])


@pytest.mark.parametrize('width,height', [(375, 812), (1280, 900)])
def test_article_actions_and_bottom_sheet_in_browser(tmp_path, width, height):
    api = pytest.importorskip('playwright.sync_api')
    with running_frontend(tmp_path) as world, api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        except api.Error as exc:
            if 'Executable doesn\'t exist' in str(exc):
                pytest.skip('需要 Chromium')
            raise
        try:
            page = browser.new_page(viewport={'width': width, 'height': height})
            page.goto(world.app.base_url + '/feed?limit=50')
            card = page.locator('article.entry').filter(has_text='Alpha & Beta')
            status = card.locator('.label-status')
            assert status.evaluate('el => el.getBoundingClientRect().height') == 0
            source_link = card.locator('.actions a[aria-label="阅读原文"]')
            assert source_link.count() == 1
            assert source_link.get_attribute('target') == '_blank'
            assert {'noopener', 'noreferrer'} <= set(source_link.get_attribute('rel').split())
            page.context.route('https://example.test/**', lambda route: route.fulfill(status=200, body='source'))
            with page.expect_popup() as popup_info:
                source_link.click()
            popup = popup_info.value
            popup.wait_for_load_state('domcontentloaded')
            assert popup.url.startswith('https://example.test/')
            assert '/feed?' in page.url
            popup.close()
            assert card.locator('.actions summary[aria-label="添加标签"]').count() == 1
            assert card.get_by_text('查看归档', exact=True).count() == 0
            menu = card.get_by_role('button', name='更多操作')
            menu.click()
            sheet = card.get_by_role('dialog', name='文章操作')
            assert sheet.is_visible()
            close_button = sheet.get_by_role('button', name='关闭')
            assert not close_button.evaluate('el => document.activeElement === el')
            assert close_button.evaluate('el => getComputedStyle(el).outlineStyle') == 'none'
            page.keyboard.press('Tab')
            assert close_button.evaluate('el => document.activeElement === el')
            assert close_button.evaluate('el => getComputedStyle(el).outlineStyle') != 'none'
            rect = sheet.bounding_box()
            assert abs(rect['y'] + rect['height'] - height) < 2
            sheet.get_by_text('归档详情', exact=True).click()
            assert sheet.locator('.raw-id').is_visible()
            page.screenshot(path=f'/tmp/atlas-sheet-{width}.png')
            page.keyboard.press('Escape')
            assert not sheet.is_visible()
            assert menu.evaluate('el => document.activeElement === el')
            menu.click()
            sheet.get_by_role('button', name='关闭').click()
            assert not sheet.is_visible()
            menu.click()
            page.mouse.click(2, 2)
            assert not sheet.is_visible()
            card.locator('.industry-editor summary').click()
            field = card.get_by_label('标签名称')
            assert field.is_visible()
            field.click()
            assert field.is_visible()
            card.locator('.meta').first.click()
            assert not field.is_visible()
            assert '/feed?' in page.url
            card.locator('.industry-editor summary').click()
            field.focus()
            page.keyboard.press('Escape')
            assert not field.is_visible()
            assert card.locator('.industry-editor summary').evaluate('el => document.activeElement === el')
            card.locator('.industry-editor summary').click()
            assert field.is_visible()
            assert field.bounding_box()['x'] >= 0
            field.fill('测试标签')
            card.get_by_role('button', name='添加标签', exact=True).click()
            page.wait_for_load_state('load')
            card = page.locator('article.entry').filter(has_text='Alpha & Beta')
            card.get_by_text('测试标签', exact=True).wait_for()
            card.get_by_role('button', name='标记有效').click()
            card.locator('.label-status').filter(has_text='已保存').wait_for()
            assert card.locator('.label-status').is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        finally:
            browser.close()
