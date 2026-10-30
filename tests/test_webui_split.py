"""Wide feed and detail panes keep navigation, labels and scrolling in sync."""

from __future__ import annotations

import pytest

from tests.test_webui_article import article_link, article_world, request


def _playwright():
    return pytest.importorskip('playwright.sync_api')


def _frame(page, title):
    frame = page.frame(name='atlas-detail')
    assert frame is not None
    frame.locator('.article-title').filter(has_text=title).wait_for()
    return frame


def test_wide_feed_detail_navigation_and_shared_labels(tmp_path):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 800})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            feed_url = page.url
            listing = page.locator('.feed-list')
            pane = page.locator('.feed-detail')
            assert listing.is_visible() and pane.is_visible()
            assert listing.bounding_box()['x'] + listing.bounding_box()['width'] <= pane.bounding_box()['x']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('#feed-detail-frame').is_visible()

            alpha = page.locator('article.entry').filter(has_text='Alpha & Beta')
            gamma = page.locator('article.entry').filter(has_text='Gamma Report')
            alpha.locator('a.title').click()
            view = _frame(page, 'Alpha & Beta')
            assert page.url == feed_url
            assert alpha.locator('a.title').get_attribute('aria-current') == 'true'
            assert view.locator('body').get_attribute('class') == 'embedded-detail'
            assert view.get_by_text('返回信息流', exact=True).count() == 0
            assert view.locator('.record-details a[href^="/entry"]').get_attribute('target') == '_top'

            # Validity changes travel in both directions, by exact article anchor.
            alpha.get_by_role('button', name='标记有效', exact=True).click()
            view.locator('.act-valid .vote-button[aria-pressed="true"]').wait_for()
            view.get_by_role('button', name='标记无效', exact=True).click()
            alpha.locator('.act-invalid .vote-button[aria-pressed="true"]').wait_for()
            assert alpha.locator('.act-valid .vote-button').get_attribute('aria-pressed') == 'false'
            # Gamma shares a raw document with Alpha but has a different anchor.
            assert gamma.locator('.act-invalid .vote-button').get_attribute('aria-pressed') == 'false'

            # Tags are reflected in the other pane without replacing the feed.
            view.locator('.industry-editor summary').click()
            view.get_by_label('标签名称').fill('详情标签')
            view.get_by_role('button', name='添加标签', exact=True).click()
            alpha.locator('.tag-row').get_by_text('详情标签', exact=True).wait_for()
            alpha.locator('.industry-editor summary').click()
            alpha.get_by_label('标签名称').fill('列表标签')
            alpha.get_by_role('button', name='添加标签', exact=True).click()
            view.locator('.tag-row').get_by_text('列表标签', exact=True).wait_for()
            assert page.url == feed_url
            assert gamma.locator('.tag-row').get_by_text('详情标签', exact=True).count() == 0

            # Scrolling the article does not move the feed, and vice versa.
            view.evaluate("document.querySelector('.reader-content').insertAdjacentHTML('beforeend', '<div style=height:1500px></div>')")
            toolbar_top = view.locator('.reader-toolbar').bounding_box()['y']
            scroll_area = page.locator('.feed-items')
            scroll_area.evaluate("el => el.insertAdjacentHTML('beforeend', '<div style=height:1200px></div>')")
            scroll_area.evaluate('el => { el.scrollTop = 120; }')
            before = scroll_area.evaluate('el => el.scrollTop')
            assert before > 0
            view.evaluate('window.scrollTo(0, 400)')
            assert view.evaluate('window.scrollY') > 0
            assert abs(view.locator('.reader-toolbar').bounding_box()['y'] - toolbar_top) < 1
            assert scroll_area.evaluate('el => el.scrollTop') == before

            gamma.locator('a.title').focus()
            page.keyboard.press('Enter')
            view = _frame(page, 'Gamma Report')
            assert page.url == feed_url
            assert gamma.locator('a.title').get_attribute('aria-current') == 'true'
            assert alpha.locator('a.title').get_attribute('aria-current') is None
            # Reload restores the chosen detail instead of silently switching articles.
            page.reload()
            _frame(page, 'Gamma Report')
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize('width', [375, 1099])
def test_narrow_feed_keeps_full_page_navigation(tmp_path, width):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': width, 'height': 800})
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            assert not page.locator('.feed-detail').is_visible()
            assert page.locator('#feed-detail-frame').get_attribute('src') is None
            page.locator('article.entry').filter(has_text='Alpha & Beta').locator('a.title').click()
            page.wait_for_url('**/article?**')
            assert page.locator('.article-title').inner_text().startswith('Alpha & Beta')
            assert page.get_by_text('返回信息流', exact=True).is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        finally:
            browser.close()


def test_resize_after_selecting_detail_preserves_narrow_navigation(tmp_path):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 800})
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            page.locator('article.entry').filter(has_text='Alpha & Beta').locator('a.title').click()
            _frame(page, 'Alpha & Beta')
            page.set_viewport_size({'width': 900, 'height': 800})
            page.wait_for_url('**/article?**')
            assert page.locator('.article-title').inner_text().startswith('Alpha & Beta')
            assert page.get_by_text('返回信息流', exact=True).is_visible()
        finally:
            browser.close()


def test_label_saved_while_detail_loads_is_synchronized(tmp_path):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 800})
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            _frame(page, 'legacy-article')
            alpha = page.locator('article.entry').filter(has_text='Alpha & Beta')
            pending = []
            def hold_response(route):
                pending.append((route, route.fetch()))
            page.route('**/article?*', hold_response)
            alpha.locator('a.title').click()
            page.wait_for_function("document.querySelector('#feed-detail-frame').hidden")
            while not pending:
                page.wait_for_timeout(20)
            route, response = pending.pop()
            from bs4 import BeautifulSoup
            stale = BeautifulSoup(response.text(), 'html.parser')
            assert stale.select_one('.act-valid .vote-button')['aria-pressed'] == 'false'
            alpha.get_by_role('button', name='标记有效', exact=True).click()
            alpha.locator('.act-valid .vote-button[aria-pressed="true"]').wait_for()
            route.fulfill(response=response)
            page.unroute('**/article?*')
            view = _frame(page, 'Alpha & Beta')
            view.locator('.act-valid .vote-button[aria-pressed="true"]').wait_for()
        finally:
            browser.close()


def test_wide_title_can_open_in_new_tab_without_changing_selection(tmp_path):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 800})
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            _frame(page, 'legacy-article')
            alpha = page.locator('article.entry').filter(has_text='Alpha & Beta')
            with page.context.expect_page(timeout=5000) as popup_info:
                alpha.locator('a.title').click(modifiers=['Control'])
            popup = popup_info.value
            popup.wait_for_url('**/article?**')
            assert 'embedded=' not in popup.url
            assert popup.locator('.article-title').inner_text().startswith('Alpha & Beta')
            _frame(page, 'legacy-article')
        finally:
            browser.close()


def test_expired_session_in_detail_returns_main_page_to_login(tmp_path):
    from tests.test_webui_entries_render import running_frontend

    api = _playwright()
    with running_frontend(tmp_path, require_login=True) as world, api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 800})
            page.goto(world.app.base_url + '/login')
            page.locator('#name').fill('alice')
            page.get_by_role('button', name='登录', exact=True).click()
            page.wait_for_url('**/feed')
            page.goto(world.app.base_url + '/feed?order=asc&limit=50')
            _frame(page, 'legacy-article')
            response = page.request.post(world.app.base_url + '/logout', data=b'')
            assert response.status == 200  # follows the logout redirect to the login page
            page.locator('article.entry').filter(has_text='Alpha & Beta').locator('a.title').click()
            page.wait_for_url('**/login?**')
            assert page.locator('#name').is_visible()
        finally:
            browser.close()


@pytest.mark.parametrize('width', [1100, 1920])
def test_wide_breakpoint_has_no_horizontal_overflow(tmp_path, width):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': width, 'height': 800})
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            view = _frame(page, 'legacy-article')
            assert page.locator('.feed-detail').is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert view.evaluate('document.documentElement.scrollWidth <= innerWidth')
        finally:
            browser.close()


def test_embedded_detail_parameter_is_validated(tmp_path):
    with article_world(tmp_path) as (app, *_):
        link = article_link(app)
        status, body, _ = request(app, link + '&embedded=1')
        assert status == 200 and 'class="embedded-detail"' in body.decode()
        assert request(app, link + '&embedded=2')[0] == 400
        assert request(app, link + '&embedded=1&embedded=1')[0] == 400
        status, body, _ = request(app, link)
        assert status == 200 and '返回信息流' in body.decode()


def test_embedded_label_uses_logged_in_user(tmp_path):
    from atlas.labels import open_store
    from tests.test_webui_entries_render import running_frontend

    api = _playwright()
    with running_frontend(tmp_path, require_login=True) as world, api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 800})
            page.goto(world.app.base_url + '/login')
            page.locator('#name').fill('alice')
            page.get_by_role('button', name='登录', exact=True).click()
            page.wait_for_url('**/feed')
            page.locator('.feed-user summary').click()
            assert page.locator('.feed-user').get_by_text('当前用户：alice').is_visible()
            assert page.locator('.feed-user').get_by_role('button', name='退出登录').is_visible()
            page.locator('.feed-user summary').click()
            alpha = page.locator('article.entry').filter(has_text='Alpha & Beta')
            alpha.locator('a.title').click()
            view = _frame(page, 'Alpha & Beta')
            view.get_by_role('button', name='标记有效', exact=True).click()
            alpha.locator('.act-valid .vote-button[aria-pressed="true"]').wait_for()
            with open_store(world.db_path) as store:
                label, = store.all_for('raw-feed-render')
                assert label.actor == 'alice' and label.label_value == 'valid'
        finally:
            browser.close()


def test_desktop_source_filter_and_theme_controls_use_real_state(tmp_path):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 800}, color_scheme='light')
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            view = _frame(page, 'legacy-article')
            assert page.get_by_role('button', name='切换到深色模式').is_visible()
            page.get_by_role('button', name='切换到深色模式').click()
            assert page.evaluate("document.documentElement.dataset.theme") == 'dark'
            view.wait_for_function("document.documentElement.dataset.theme === 'dark'")
            assert page.evaluate("getComputedStyle(document.body).backgroundColor") == 'rgb(20, 23, 26)'
            assert view.evaluate("getComputedStyle(document.body).backgroundColor") == 'rgb(26, 29, 33)'
            page.reload()
            view = _frame(page, 'legacy-article')
            assert page.evaluate("document.documentElement.dataset.theme") == 'dark'
            assert view.evaluate("document.documentElement.dataset.theme") == 'dark'
            page.get_by_role('button', name='切换到浅色模式').click()
            view.wait_for_function("document.documentElement.dataset.theme === 'light'")

            page.locator('.source-menu summary').click()
            page.locator('.source-menu nav').get_by_role('link', name='chan-feed', exact=True).click()
            page.wait_for_url('**channel=chan-feed**')
            assert 'chan-feed' in page.locator('.source-menu summary').inner_text()
            assert page.locator('article.entry').count() == 3
            page.locator('.filter-panel summary').click()
            page.locator('.filter-panel select[name=order]').select_option('desc')
            page.locator('.filter-panel').get_by_role('button', name='筛选', exact=True).click()
            page.wait_for_url('**order=desc**')
            assert 'channel=chan-feed' in page.url
        finally:
            browser.close()


def test_titles_and_extra_tags_follow_prototype_on_all_screen_sizes(tmp_path):
    from dataclasses import replace

    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        original = app.server.entry_projection
        app.server.entry_projection = lambda entries, session: tuple(
            replace(entry, title_zh='中文标题', tags=('一', '二', '三', '四', '五'))
            for entry in original(entries, session)
        )
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1500, 'height': 800})
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            alpha = page.locator('article.entry').filter(has_text='Alpha & Beta')
            assert alpha.locator('.title-zh').bounding_box()['y'] < alpha.locator('a.title').bounding_box()['y']
            assert alpha.locator('.tag-overflow').inner_text() == '+2'
            assert not alpha.locator('.badge').nth(3).is_visible()
            alpha.locator('a.title').click()
            view = _frame(page, 'Alpha & Beta')
            assert view.locator('.badge').nth(4).is_visible()

            page.set_viewport_size({'width': 375, 'height': 800})
            page.wait_for_url('**/article?**')
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            alpha = page.locator('article.entry').filter(has_text='Alpha & Beta')
            assert alpha.locator('.title-zh').bounding_box()['y'] < alpha.locator('a.title').bounding_box()['y']
            assert not alpha.locator('.badge').nth(4).is_visible()
            assert alpha.locator('.tag-overflow').inner_text() == '+2'
            assert page.locator('.feed-brand').is_visible()
        finally:
            browser.close()


@pytest.mark.parametrize('width', [320, 375, 1099])
def test_mobile_prototype_controls_and_full_page_reader(tmp_path, width):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': width, 'height': 800}, color_scheme='light', is_mobile=True, has_touch=True)
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            assert page.locator('.feed-brand').is_visible()
            assert page.locator('.source-menu summary').is_visible()
            assert page.locator('.compact-pager').is_visible()
            assert not page.locator('.feed-detail').is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('button', name='切换到深色模式').click()
            page.locator('.source-menu summary').click()
            page.locator('.source-menu nav').get_by_role('link', name='chan-feed', exact=True).click()
            page.wait_for_url('**channel=chan-feed**')
            assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
            page.locator('.filter-panel summary').click()
            assert page.locator('.filter-panel select[name=order]').is_visible()
            page.locator('.filter-panel summary').click()
            alpha = page.locator('article.entry').filter(has_text='Alpha & Beta')
            assert alpha.get_by_role('button', name='标记有效', exact=True).is_visible()
            alpha.locator('.industry-editor summary').click()
            tag_form = alpha.locator('.industry-editor form').bounding_box()
            assert 0 <= tag_form['x'] and tag_form['x'] + tag_form['width'] <= width
            alpha.locator('.industry-editor summary').click()
            alpha.locator('a.title').click()
            page.wait_for_url('**/article?**')
            assert page.locator('.reader-toolbar').is_visible()
            assert page.get_by_role('link', name='返回信息流').is_visible()
            assert page.get_by_role('tab', name='中文', exact=True).is_visible()
            assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('.industry-editor summary').click()
            tag_form = page.locator('.industry-editor form').bounding_box()
            assert 0 <= tag_form['x'] and tag_form['x'] + tag_form['width'] <= width
            page.locator('.industry-editor summary').click()
            page.get_by_role('button', name='标记有效', exact=True).click()
            page.locator('.act-valid .vote-button[aria-pressed="true"]').wait_for()
            assert page.locator('.reader-toolbar .label-status').is_visible()
            page.get_by_role('link', name='返回信息流').click()
            page.wait_for_url('**/feed?**')
            assert 'channel=chan-feed' in page.url
        finally:
            browser.close()


def test_mobile_authenticated_header_fits_and_account_menu_works(tmp_path):
    from tests.test_webui_entries_render import running_frontend

    api = _playwright()
    with running_frontend(tmp_path, require_login=True) as world, api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            page.goto(world.app.base_url + '/login')
            page.locator('#name').fill('alice')
            page.get_by_role('button', name='登录', exact=True).click()
            page.wait_for_url('**/feed')
            brand = page.locator('.feed-brand').bounding_box()
            account = page.locator('.feed-user summary').bounding_box()
            source = page.locator('.source-menu summary').bounding_box()
            quick = page.locator('.quick').bounding_box()
            assert brand['y'] <= account['y'] < brand['y'] + brand['height']
            assert abs(source['y'] - quick['y']) < 3
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('.feed-user summary').click()
            assert page.locator('.feed-user').get_by_text('当前用户：alice').is_visible()
            page.locator('.feed-user').get_by_role('button', name='退出登录').click()
            page.wait_for_url('**/login')
        finally:
            browser.close()


@pytest.mark.parametrize('width', [320, 1100, 1500])
def test_pagination_moves_to_header_and_preserves_filters(tmp_path, width):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': width, 'height': 800})
            page.goto(app.base_url + '/feed?order=asc&limit=1&channel=chan-feed')
            pager = page.locator('.feed-header .compact-pager')
            assert pager.is_visible()
            assert page.locator('.feed-footer').count() == 0
            assert pager.locator('.compact-count').text_content() == '1–1 / 3'
            assert pager.locator('.compact-count').is_visible() == (width == 1500)
            assert pager.locator('[aria-label="上一页"]').get_attribute('aria-disabled') == 'true'
            if width == 1500:
                pager.locator('.compact-count').evaluate("el => el.textContent = '1–50 / 3213'")
                pager.locator('.page-number').evaluate("el => el.textContent = '1/65'")
                quick = page.locator('.quick').bounding_box()
                box = pager.bounding_box()
                assert abs(quick['y'] - box['y']) < 5
                assert box['x'] >= quick['x'] + quick['width']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if width >= 1100:
                scroll_box = page.locator('.feed-items').bounding_box()
                assert abs(scroll_box['y'] + scroll_box['height'] - 800) < 1
            pager.get_by_role('link', name='下一页').click()
            page.wait_for_url('**offset=1**')
            assert 'channel=chan-feed' in page.url
            assert pager.locator('.compact-count').text_content() == '2–2 / 3'
            pager.get_by_role('link', name='上一页').click()
            page.wait_for_url('**offset=0**')
            assert 'channel=chan-feed' in page.url
        finally:
            browser.close()


def test_mobile_feed_scrolls_the_page_and_reaches_last_item(tmp_path):
    api = _playwright()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 393, 'height': 800}, is_mobile=True, has_touch=True)
            page.goto(app.base_url + '/feed?order=asc&limit=50')
            assert 'viewport-fit=cover' in page.locator('meta[name=viewport]').get_attribute('content')
            assert not page.locator('.compact-count').is_visible()
            assert page.locator('.page-number').is_visible()
            items = page.locator('.feed-items')
            items.evaluate("el => el.lastElementChild.insertAdjacentHTML('beforebegin', '<div style=height:1200px></div>')")
            assert items.evaluate("el => getComputedStyle(el).overflowY") == 'visible'
            page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
            assert page.evaluate('window.scrollY') > 0
            assert abs(page.locator('.feed-header').bounding_box()['y']) < 1
            last = page.locator('article.entry').last
            box = last.bounding_box()
            assert box['y'] + box['height'] <= 800
            assert box['y'] >= page.locator('.feed-header').bounding_box()['height']
            last.locator('a.title').click()
            page.wait_for_url('**/article?**')
        finally:
            browser.close()
