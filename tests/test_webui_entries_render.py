"""T-109 前端的**条目渲染**验收（真实 HTTP 服务、本地临时端口、**不出网**）。

判据（先定义后实现，正文与理由见 `tests/test_feed_entries_contract.py`）：

1. **标题/链接真的可见**：`GET /feed` 的 HTML 里出现**条目自己的标题**（不是 `raw_id`）
   与原文链接；这是 SPEC §6.5 那句"浏览而不是看见一串 ID"的直接判据。
2. **容器不出现在列表里**：feed 容器自身的 endpoint 不得作为条目行出现，
   而它的派生条目的标题必须出现。
3. **XSS 不退化**：标题 / 链接 / 正文里的 `<script>`、引号、`javascript:` 一律被
   `escape()`（含 `quote=True`）处理，`<script>` 不得以可执行形态出现在响应里。
4. **条目级打标带锚点落库**：`POST /label` 带 `raw_sha256` / `char_start` / `char_end`
   ⇒ `303`，并且**独立打开 `LabelStore`** 能读回完全相同的四元组。
5. **打开原文并高亮**：`GET /entry` 渲染该条目区间的切片（`<mark>` 包住），
   区间由**整数**参数给出，不做任何坐标推算。
6. **状态可见**：未打标 / 已打标都能从页面看出来（沿用 `label-tag` / "未打标"）。
7. **半截锚点被拒绝**：只给 `char_start` 不给 `char_end` ⇒ 明确 4xx，且不落库。
8. **锚点指纹不符被拒绝**：`raw_sha256` 与归档里那条不一致 ⇒ 4xx，且不落库。

判据 4/7/8 的"不落库"都用**独立打开的库**计数，不看服务返回的页面文字。
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, Tuple

from atlas.archive import open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256
from atlas.labels import open_store
from atlas.webapp import build_webapp

#: 真实形状的 RSS：**两个**条目，标题里带 `&`（原文里是 `&amp;`）。
FEED_TEXT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rss version="2.0"><channel><title>demo feed</title>\n'
    "<item><title>Alpha &amp; Beta &lt;script&gt;alert(1)&lt;/script&gt;</title>"
    '<link>https://example.test/alpha?a=1&amp;b=2</link>'
    "<pubDate>Mon, 02 Mar 2026 10:00:00 +0000</pubDate>"
    "<description><![CDATA[<p>alpha body with <b>markup</b> &amp; entity</p>]]></description>"
    "</item>\n"
    "<item><title>Gamma Report</title>"
    '<link>https://example.test/gamma</link>'
    "<pubDate>Mon, 02 Mar 2026 11:00:00 +0000</pubDate>"
    "<description><![CDATA[<p>gamma body</p>]]></description>"
    "</item>\n"
    "</channel></rss>"
)

#: 一条"按篇归档"的旧语料：正文是纯文本，没有 `<item>`。
ARTICLE_TEXT = "A legacy article page stored as one whole document."

#: 标题里带引号与 `javascript:` 链接的条目，用来钉死 XSS 防线。
NASTY_TITLE = '"><script>alert("xss")</script><a href="javascript:alert(1)">x</a>'

#: 第二份 feed：标题与链接都是攻击载荷。
NASTY_TEXT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rss version="2.0"><channel><title>nasty</title>'
    f"<item><title>{NASTY_TITLE}</title>"
    '<link>javascript:alert(1)</link>'
    "<pubDate>Mon, 02 Mar 2026 12:00:00 +0000</pubDate>"
    "<description><![CDATA[<p>nasty</p>]]></description></item>"
    "</channel></rss>"
)

#: 每份 raw 的字节（放在一张表里，避免两处构造同一份字符串而漂移）。
PAYLOADS = {
    "raw-feed-render": FEED_TEXT,
    "raw-feed-nasty": NASTY_TEXT,
    "raw-article-render": ARTICLE_TEXT,
}


def _records() -> Tuple[RawRecord, ...]:
    return (
        RawRecord(
            raw_id="raw-feed-render",
            channel_id="chan-feed",
            endpoint="https://example.test/feed.xml",
            content_sha256=content_sha256(FEED_TEXT.encode("utf-8")),
            byte_length=len(FEED_TEXT.encode("utf-8")),
            fetched_at=_moment(0),
            http_status=200,
            entry_kind="feed",
        ),
        RawRecord(
            raw_id="raw-feed-nasty",
            channel_id="chan-feed",
            endpoint="https://example.test/nasty.xml",
            content_sha256=content_sha256(NASTY_TEXT.encode("utf-8")),
            byte_length=len(NASTY_TEXT.encode("utf-8")),
            fetched_at=_moment(1),
            http_status=200,
            entry_kind="feed",
        ),
        RawRecord(
            raw_id="raw-article-render",
            channel_id="chan-article",
            endpoint="https://example.test/legacy-article",
            content_sha256=content_sha256(ARTICLE_TEXT.encode("utf-8")),
            byte_length=len(ARTICLE_TEXT.encode("utf-8")),
            fetched_at=_moment(2),
            http_status=200,
            entry_kind=None,
        ),
    )


def _moment(minutes: int):
    from datetime import datetime, timedelta, timezone

    return datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


@contextmanager
def running_frontend(tmp_path: Path, *, actor: str = "tester") -> Iterator[SimpleNamespace]:
    """把 fixture 写进临时归档根，再用**真实组合根**装一个前端（`127.0.0.1:0`）。"""
    root = tmp_path / "store"
    archive = open_archive(root)
    try:
        for record in _records():
            archive.put(record, PAYLOADS[record.raw_id].encode("utf-8"))
        assert archive.verify() == []
    finally:
        archive.close()

    db_path = root / "atlas.db"
    app = build_webapp(
        root,
        db_path=db_path,
        industry_provider=lambda: ("ai", "web"),
        industry_of={"chan-feed": "ai", "chan-article": "web"},
        actor=actor,
        initialize_labels=True,
    )
    try:
        yield SimpleNamespace(app=app, root=root, db_path=db_path)
    finally:
        app.close()


def _get(app, path: str) -> Tuple[int, str, Dict[str, str]]:
    try:
        with urllib.request.urlopen(app.base_url + path, timeout=10) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def _post(app, fields: Dict[str, str]) -> Tuple[int, str, Dict[str, str]]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        app.base_url + "/label",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _label_rows(db_path: Path) -> int:
    with open_store(db_path) as store:
        return store.count()


def test_probe_labels_do_not_count_as_current_actor_judgments(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        world.app.labels.record(
            raw_id="raw-feed-render", label_key="industry", label_value="ai",
            actor="e2e-probe",
        )
        status, page, _ = _get(world.app, "/feed?labeled=true")
        assert status == 200
        assert 'class="count">共 0 条' in page

        status, _, _ = _post(world.app, {
            "raw_id": "raw-article-render", "label_key": "valid",
            "label_value": "valid", "actor": "tester", "return_to": "/feed",
        })
        assert status == 303
        # 同一文档上更晚的其他署名判断不能覆盖当前署名的显示。
        world.app.labels.record(
            raw_id="raw-article-render", label_key="valid", label_value="invalid",
            actor="e2e-probe",
        )
        status, page, _ = _get(world.app, "/feed?labeled=true")
        assert status == 200
        assert 'class="count">共 1 条' in page
        assert 'id="item-raw-article-render' in page
        assert "Gamma Report" not in page
        with world.app.labels.read_session() as session:
            assert session.values_for("raw-article-render") == {"valid": "valid"}
            assert [label.actor for label, _ in session.rates_for("raw-article-render")] == ["tester"]
        status, page, _ = _get(world.app, "/feed?labeled=false")
        assert status == 200
        assert "Gamma Report" in page
        assert 'id="item-raw-article-render' not in page
        # 浏览投影不删除或改写探针；无署名筛选的审计入口仍能看到全部记录。
        from atlas.webui import StoreLabelAccess
        with StoreLabelAccess(world.db_path).read_session() as session:
            assert session.values_for("raw-article-render") == {"valid": "invalid"}
        assert _label_rows(world.db_path) == 3


def test_new_feed_collected_after_startup_can_be_browsed_and_labeled(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        # 用另一条归档连接模拟定时采集，而不重启 Web 应用。
        payload = FEED_TEXT.replace("Gamma Report", "Fresh Scheduled Article").encode("utf-8")
        record = RawRecord.create(
            channel_id="chan-feed", endpoint="https://example.test/fresh.xml",
            content=payload, http_status=200,
        )
        with open_archive(world.root) as archive:
            archive.put(record, payload)
        status, page, _ = _get(world.app, "/feed?limit=200")
        assert status == 200
        assert "Fresh Scheduled Article" in page
        fields = _span_of(world.app, record.raw_id)
        fields["actor"] = "tester"
        status, body, _ = _post(world.app, fields)
        assert status == 303, body[:400]
        with open_store(world.db_path) as store:
            labels = store.all_for(record.raw_id)
            assert len(labels) == 1
            assert labels[0].anchor is not None
            assert labels[0].anchor.char_start == int(fields["char_start"])
            assert labels[0].anchor.char_end == int(fields["char_end"])


def test_empty_rss_and_atom_are_subscription_notices_instead_of_articles(tmp_path: Path) -> None:
    from atlas.webapp import store_summary

    with running_frontend(tmp_path) as world:
        samples = (
            b'<rss version="2.0"><channel><title>No papers today</title></channel></rss>',
            b'<feed xmlns="http://www.w3.org/2005/Atom"><title>No articles today</title></feed>',
        )
        empty_ids = []
        with open_archive(world.root) as archive:
            for index, payload in enumerate(samples):
                record = RawRecord.create(
                    channel_id=f"empty-source-{index}",
                    endpoint=f"https://example.test/empty-{index}",
                    content=payload, http_status=200,
                )
                archive.put(record, payload)
                empty_ids.append(record.raw_id)
        before = hashlib.sha256(world.db_path.read_bytes()).hexdigest()
        status, page, _ = _get(world.app, "/feed?limit=200")
        assert status == 200
        assert 'class="count">共 4 条' in page
        assert "本次订阅没有可展示的文章" in page
        assert "Gamma Report" in page  # 非空订阅仍展示真实文章。
        for raw_id in empty_ids:
            assert f'id="item-{raw_id}"' not in page
        status, documents, _ = _get(world.app, "/feed?granularity=document&limit=200")
        assert status == 200
        for raw_id in empty_ids:
            assert f'id="item-{raw_id}"' in documents
        summary = store_summary(world.root)
        assert summary.records == 5
        assert summary.containers == 4  # 两个原有订阅 + 两个空订阅。
        assert summary.entries == 4
        assert hashlib.sha256(world.db_path.read_bytes()).hexdigest() == before


def test_unrecognized_xml_remains_visible_for_inspection(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        payload = b'<response><message>Not a subscription</message></response>'
        record = RawRecord.create(
            channel_id="unknown-xml", endpoint="https://example.test/unknown.xml",
            content=payload, http_status=200,
        )
        with open_archive(world.root) as archive:
            archive.put(record, payload)
        status, page, _ = _get(world.app, "/feed?limit=200")
        assert status == 200
        assert f'id="item-{record.raw_id}"' in page


def test_json_news_are_articles_and_challenges_are_not(tmp_path: Path) -> None:
    from atlas.webapp import store_summary

    with running_frontend(tmp_path) as world:
        hits = [
            {"title": "中文 Hacker News article", "url": "https://example.test/news", "objectID": "123", "created_at": "2026-10-26T01:00:00Z"},
            {"title": "Another discussion", "url": None, "objectID": "124", "created_at": "2026-10-26T02:00:00Z"},
        ]
        payload = json.dumps({"hits": hits, "nbHits": 2, "hitsPerPage": 20}, ensure_ascii=False).encode()
        challenge = b'<html><head><meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/"></head></html>'
        with open_archive(world.root) as archive:
            news = RawRecord.create(channel_id="hn", endpoint="https://example.test/api", content=payload, http_status=200)
            blocked = RawRecord.create(channel_id="blocked", endpoint="https://example.test/feed/", content=challenge, http_status=202)
            archive.put(news, payload)
            archive.put(blocked, challenge)
        before = hashlib.sha256(world.db_path.read_bytes()).hexdigest()
        status, page, _ = _get(world.app, "/feed?limit=200")
        assert status == 200 and 'class="count">共 6 条' in page
        assert hits[0]["title"] in page and hits[1]["title"] in page
        assert 'href="https://example.test/news"' in page
        assert 'href="https://news.ycombinator.com/item?id=124"' in page
        assert "验证码验证页" in page
        assert f'id="item-{blocked.raw_id}"' not in page
        status, documents, _ = _get(world.app, "/feed?granularity=document&limit=200")
        assert status == 200 and f'id="item-{blocked.raw_id}"' in documents
        assert f'id="item-{news.raw_id}"' in documents
        summary = store_summary(world.root)
        assert summary.records == 5 and summary.entries == 6 and summary.unavailable_records == 1
        assert hashlib.sha256(world.db_path.read_bytes()).hexdigest() == before

        # 浏览 / 高亮 / 单条打标实际通过 HTTP，锚点必须落在 JSON 中的原始对象上。
        fields = _span_of(world.app, news.raw_id)
        start, end = int(fields["char_start"]), int(fields["char_end"])
        selected = json.loads(payload.decode()[start:end])
        status, archive_page, _ = _get(world.app, "/entry?" + urllib.parse.urlencode({k: fields[k] for k in ("raw_id", "char_start", "char_end")}))
        assert status == 200 and "<mark>" in archive_page
        assert selected["title"] in archive_page
        fields["actor"] = "tester"
        status, _, _ = _post(world.app, fields)
        assert status == 303
        with open_store(world.db_path) as store:
            label, = store.all_for(news.raw_id)
            assert label.anchor.char_start == start and label.anchor.char_end == end
            assert label.anchor.raw_sha256 == hashlib.sha256(payload).hexdigest()


def test_ai_industries_are_per_entry_live_filtered_and_overridden_only_by_own_manual_label(tmp_path):
    from bs4 import BeautifulSoup

    from atlas.cognition import classify_document
    from tests.test_classification_readview import add_classification

    with running_frontend(tmp_path) as world:
        first, second = classify_document(FEED_TEXT.encode(), raw_id="raw-feed-render").units
        add_classification(world.db_path, first, values=("web",), plan="ai-first")
        add_classification(world.db_path, second, values=("ai", "web"), plan="ai-second")
        before = hashlib.sha256(world.db_path.read_bytes()).digest()
        status, page, _ = _get(world.app, "/feed?industry=web&limit=1")
        assert status == 200 and 'class="count">共 3 条' in page
        assert "AI 建议" in page and "Gamma Report" in page
        # 筛选 / total 先使用逐篇分类，再分页；渠道原来只有 ai。
        status, page, _ = _get(world.app, "/feed?industry=web&limit=200")
        soup = BeautifulSoup(page, "html.parser")
        assert len(soup.select('.item')) == 3
        assert "Gamma Report" in page and first.title.replace("&", "&amp;")[:10] in page
        assert hashlib.sha256(world.db_path.read_bytes()).digest() == before
        status, page, _ = _get(world.app, "/feed?labeled=true")
        assert status == 200 and 'class="count">共 0 条' in page  # AI 不算人工打标。

        fields = _span_of(world.app, "raw-feed-render")
        fields.update(label_key="industry", label_value="ai", actor="tester")
        status, _, _ = _post(world.app, fields)
        assert status == 303
        status, page, _ = _get(world.app, "/feed?labeled=true")
        assert status == 200 and 'class="count">共 1 条' in page
        assert "人工确认" in page
        # 更换另一个条目的模型结果，不重启也能读到；失败不得继续显示旧分类。
        selected_start = int(fields['char_start'])
        other = first if first.char_start != selected_start else second
        add_classification(world.db_path, other, status="unclassified", plan="ai-failed")
        status, page, _ = _get(world.app, "/feed?limit=200")
        assert status == 200 and "AI 未分类" in page and "人工确认" in page
        status, page, _ = _get(world.app, "/feed?industry=web&limit=200")
        assert status == 200 and 'class="count">共 1 条' in page  # 只剩渠道为 web 的整篇文章。


def test_industry_projection_cannot_change_an_articles_anchor(tmp_path):
    from dataclasses import replace

    with running_frontend(tmp_path) as world:
        assert _get(world.app, "/feed")[0] == 200  # 同一 HTTP / 投影路径的活对照。
        original = world.app._httpd.entry_projection

        def wrong(entries, session):
            rows = list(original(entries, session))
            rows[0] = replace(rows[0], char_end=rows[0].char_end + 1)
            return rows

        world.app._httpd.entry_projection = wrong
        status, page, _ = _get(world.app, "/feed")
        assert status == 500 and "投影不得更改" in page


def test_feed_reads_title_and_summary_columns_and_places_translation_below_original(tmp_path):
    from dataclasses import replace

    from bs4 import BeautifulSoup

    from atlas.cognition import ArticleBrief, BriefStore
    from atlas.webapp import ArchiveEntriesAccess

    with running_frontend(tmp_path) as world:
        with open_archive(world.root) as archive:
            source = next(iter(ArchiveEntriesAccess(archive).brief_sources('raw-feed-render').values()))
        enriched = replace(source, body='The article explains an algorithm and its evaluation. ' * 10)
        store = BriefStore(world.db_path)
        store.record([enriched], {source.key:ArticleBrief('阿尔法与贝塔', '这是该文章的中文简介，介绍研究方法及其主要发现。' * 30)}, model='test-model')
        store.close()
        before = hashlib.sha256(world.db_path.read_bytes()).digest()
        status, page, _ = _get(world.app, '/feed?limit=200')
        soup = BeautifulSoup(page, 'html.parser')
        assert status == 200
        translated = soup.select_one('.title-zh')
        assert translated.get_text() == '阿尔法与贝塔'
        assert translated.find_previous_sibling('a')['class'] == ['title']
        assert 'Alpha' in translated.find_previous_sibling('a').get_text()
        card = translated.find_parent('article')
        assert '这是该文章的中文简介' in card.select_one('.summary').get_text()
        assert len(card.select_one('.summary').get_text()) <= 300
        assert 'AI 中文简介' in page
        assert '标题译述' not in page
        assert hashlib.sha256(world.db_path.read_bytes()).digest() == before


def test_feed_chinese_original_title_is_not_repeated_but_has_summary(tmp_path):
    from bs4 import BeautifulSoup

    from atlas.cognition import ArticleBrief, BriefStore
    from atlas.webapp import ArchiveEntriesAccess

    with running_frontend(tmp_path) as world:
        payload = ('<rss><channel><item><title>中文原始标题</title><link>https://example.test/zh</link>'
                   '<description>' + '这是中文正文，介绍图像识别研究的核心方法及其评估。' * 10 + '</description></item></channel></rss>').encode()
        record = RawRecord.create(channel_id='chan-feed', endpoint='https://example.test/zh.xml', content=payload, http_status=200)
        with open_archive(world.root) as archive:
            archive.put(record, payload)
            source = next(iter(ArchiveEntriesAccess(archive).brief_sources(record.raw_id).values()))
        store = BriefStore(world.db_path)
        store.record([source], {source.key:ArticleBrief('不应显示的重复译题', '文章介绍图像识别研究的方法及评估。')}, model='test-model')
        store.close()
        status, page, _ = _get(world.app, '/feed?limit=200')
        soup = BeautifulSoup(page, 'html.parser')
        card = next(c for c in soup.select('article') if c.select_one('.title').get_text() == '中文原始标题')
        assert status == 200
        assert not card.select('.title-zh')
        assert card.select_one('.summary').get_text() == '文章介绍图像识别研究的方法及评估。'


# ===================================================================== #
# 判据 1 / 2：真实标题与链接可见；容器不出现
# ===================================================================== #
def test_feed_page_shows_entry_titles_and_links(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        # `order=asc`：把两份 fixture feed 的条目排成确定的顺序（Alpha 在前）
        status, page, _ = _get(world.app, "/feed?limit=50&order=asc")
        assert status == 200, page[:400]

        # 条目自己的标题（`&` 在 HTML 里必须被转义成 `&amp;`）
        assert "Alpha &amp; Beta" in page
        assert "Gamma Report" in page
        # 原文链接
        assert "https://example.test/gamma" in page
        # 整篇即条目的旧语料也在列表里，标题取自链接末段
        assert "legacy-article" in page
        # 不再是"只有 raw_id 看不出是什么"
        assert "Alpha" in page and 'class="title"' in page


def test_feed_page_does_not_list_the_container_itself(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        status, page, _ = _get(world.app, "/feed?limit=50")
        assert status == 200
        # 容器自身的 endpoint 不得作为条目链接出现
        assert 'href="https://example.test/feed.xml"' not in page
        assert 'href="https://example.test/nasty.xml"' not in page
        # 但它的派生条目的链接必须出现
        assert 'href="https://example.test/alpha' in page
        # 整篇即条目的那条**出现**（它的 endpoint 就是它的链接）
        assert "https://example.test/legacy-article" in page


def test_feed_page_shows_empty_text_when_nothing_matches(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        status, page, _ = _get(world.app, "/feed?industry=nope")
        assert status == 200
        assert "没有符合条件的记录" in page


def test_feed_page_respects_an_explicit_granularity(tmp_path: Path) -> None:
    """浏览页**默认**按条目渲染；调用方显式给 `granularity` 时必须尊重它。

    不尊重就会出现"看着接受了参数、其实被静默改写"——那正是本项目禁止的那类静默。
    """
    with running_frontend(tmp_path) as world:
        status, entries_page, _ = _get(world.app, "/feed?limit=50&order=asc")
        assert status == 200
        assert 'class="title"' in entries_page

        status, docs_page, _ = _get(
            world.app, "/feed?limit=50&order=asc&granularity=document"
        )
        assert status == 200
        # 文档粒度：没有条目标题链接，但有 raw_id（旧渲染）
        assert 'class="title"' not in docs_page
        assert '<code class="raw-id">raw-feed-render</code>' in docs_page
        # 容器**在文档粒度下**是可见的（它就是一条 raw）
        assert "https://example.test/feed.xml" in docs_page

        status, _implicit_entry, _ = _get(
            world.app, "/feed?limit=50&order=asc&granularity=entry"
        )
        assert status == 200


# ===================================================================== #
# 判据 3：XSS 不退化
# ===================================================================== #
def test_external_content_is_escaped_in_the_feed_page(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        status, page, _ = _get(world.app, "/feed?limit=50")
        assert status == 200
        assert "<script>alert" not in page, "标题里的 <script> 必须以转义形态出现"
        assert "&lt;script&gt;alert" in page, "转义必须真的发生（不是被丢掉）"
        assert 'javascript:alert(1)"' not in page, "javascript: 链接不得原样进 href"
        assert 'href="#"' in page, "不安全的链接必须退化成 #"


def _entry_forms(page: str) -> list:
    """页面上每一个打标表单的 hidden 字段（用 `html.parser` 精确按表单切分）。

    不用正则：正则很容易跨过 `</form>` 抓到后面那个表单的字段
    （实测踩过：字典里拿到的是最后一条条目的值），而"点了哪个按钮"必须精确到表单。
    """
    from html.parser import HTMLParser

    class _Forms(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.forms: list = []
            self.current = None

        def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
            attributes = dict(attrs)
            if tag == "form":
                self.current = {} if attributes.get("action") == "/label" else None
                if self.current is not None:
                    self.forms.append(self.current)
                return
            if tag == "input" and self.current is not None:
                if attributes.get("type") == "hidden":
                    self.current[attributes.get("name", "")] = attributes.get("value", "")

        def handle_endtag(self, tag: str) -> None:  # noqa: ANN001
            if tag == "form":
                self.current = None

    parser = _Forms()
    parser.feed(page)
    assert parser.forms, "页面里必须有打标表单"
    return parser.forms


def _span_of(app, raw_id: str) -> Dict[str, str]:
    """取**指定容器**第一条派生条目的表单字段。

    条目按时间排序，而 fixture 里有两份 feed —— 测试不该依赖与它无关的排序，
    因此显式按 `raw_id` 挑表单。
    """
    status, page, _ = _get(app, "/feed?limit=50&order=asc")
    assert status == 200, page[:300]
    for fields in _entry_forms(page):
        if fields.get("raw_id") == raw_id:
            return fields
    raise AssertionError(f"页面里没有 raw_id={raw_id} 的条目表单")


# ===================================================================== #
# 判据 4 / 7 / 8：条目级打标
# ===================================================================== #
def test_entry_level_label_lands_with_anchor(tmp_path: Path) -> None:
    with running_frontend(tmp_path, actor="e2e-t109") as world:
        fields = _span_of(world.app, "raw-feed-render")
        assert {"raw_id", "raw_sha256", "char_start", "char_end"} <= set(fields)
        assert "entry_id" not in fields, "entry_id 绝不能进锚点（T-130）"

        status, _, headers = _post(
            world.app,
            {
                "raw_id": fields["raw_id"],
                "label_key": "industry",
                "label_value": "ai",
                "actor": "e2e-t109",
                "raw_sha256": fields["raw_sha256"],
                "char_start": fields["char_start"],
                "char_end": fields["char_end"],
                "return_to": "/feed",
            },
        )
        assert status == 303, (status, headers)
        assert headers["Location"] == "/feed"

        # **独立打开库**（不经任何前端代码）读回四元组
        with open_store(world.db_path) as store:
            rows = store.all_for(fields["raw_id"])
            assert len(rows) == 1
            anchor = rows[0].anchor
            assert anchor is not None, "条目级标签必须带锚点落库"
            assert anchor.raw_id == fields["raw_id"]
            assert anchor.raw_sha256 == fields["raw_sha256"]
            assert anchor.char_start == int(fields["char_start"])
            assert anchor.char_end == int(fields["char_end"])
            assert rows[0].actor == "e2e-t109"

        # 状态回显
        status, again, _ = _get(world.app, "/feed?limit=50&order=asc")
        assert status == 200
        assert '<span class="label-tag">industry=ai</span>' in again


def test_half_anchor_is_refused(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        fields = _span_of(world.app, "raw-feed-render")
        before = _label_rows(world.db_path)

        status, body, _ = _post(
            world.app,
            {
                "raw_id": fields["raw_id"],
                "label_key": "industry",
                "label_value": "ai",
                "actor": "e2e-t109",
                "char_start": fields["char_start"],  # 只给一半
                "return_to": "/feed",
            },
        )
        assert status == 400, (status, body[:300])
        assert "锚点字段不完整" in body
        assert _label_rows(world.db_path) == before

        # 活对照：同一路径把三个字段给全 → 303 且落库（证明拒绝不是因为签名问题）
        ok, _, _ = _post(
            world.app,
            {
                "raw_id": fields["raw_id"],
                "label_key": "industry",
                "label_value": "ai",
                "actor": "e2e-t109",
                "raw_sha256": fields["raw_sha256"],
                "char_start": fields["char_start"],
                "char_end": fields["char_end"],
                "return_to": "/feed",
            },
        )
        assert ok == 303
        assert _label_rows(world.db_path) == before + 1


def test_anchor_fingerprint_mismatch_is_refused(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        fields = _span_of(world.app, "raw-feed-render")
        before = _label_rows(world.db_path)

        status, body, _ = _post(
            world.app,
            {
                "raw_id": fields["raw_id"],
                "label_key": "industry",
                "label_value": "ai",
                "actor": "e2e-t109",
                "raw_sha256": "0" * 64,
                "char_start": fields["char_start"],
                "char_end": fields["char_end"],
                "return_to": "/feed",
            },
        )
        assert status == 409, (status, body[:300])
        assert "指纹" in body
        assert _label_rows(world.db_path) == before


def test_anchor_span_outside_the_document_is_refused(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        fields = _span_of(world.app, "raw-feed-render")
        before = _label_rows(world.db_path)

        status, body, _ = _post(
            world.app,
            {
                "raw_id": fields["raw_id"],
                "label_key": "industry",
                "label_value": "ai",
                "actor": "e2e-t109",
                "raw_sha256": fields["raw_sha256"],
                "char_start": "0",
                "char_end": "99999999",
                "return_to": "/feed",
            },
        )
        assert status == 400, (status, body[:300])
        assert "越界" in body
        assert _label_rows(world.db_path) == before


# ===================================================================== #
# 判据 5：打开原文并高亮该条目区间
# ===================================================================== #
def test_entry_page_highlights_the_entry_span(tmp_path: Path) -> None:
    with running_frontend(tmp_path) as world:
        fields = _span_of(world.app, "raw-feed-render")
        assert fields["label_value"] == "valid", "抓到的应当是 Alpha 那条条目"
        query = urllib.parse.urlencode(
            {
                "raw_id": fields["raw_id"],
                "char_start": fields["char_start"],
                "char_end": fields["char_end"],
            }
        )
        status, detail, _ = _get(world.app, "/entry?" + query)
        assert status == 200, detail[:400]
        assert "<mark>" in detail, "整段切片必须被 <mark> 包住"
        # 切片来自 `Entry.content_slice()`：标签被去掉、CDATA 外壳被剥掉。
        # ⚠️ 它**不**解实体（SPEC §6.3 的硬要求：解实体会让字符数与偏移解耦），
        # 因此原文里的 `&amp;` 会原样留在切片里；渲染时再过 `escape()`（XSS 防线），
        # 于是在 HTML 里看到的是 `&amp;amp;` —— 这是**有意的**，不是双重转义事故。
        assert "alpha body with markup" in detail
        assert "&amp;amp; entity" in detail
        assert "<![CDATA[" not in detail
        # 标记被去掉（只留文本），且页面本身没有可执行的 script
        assert "<p>alpha body" not in detail
        # 返回 feed 的链接
        assert "返回 feed" in detail

        # 区间非法（char_end <= char_start）→ 明确 400
        bad = urllib.parse.urlencode(
            {
                "raw_id": fields["raw_id"],
                "char_start": "50",
                "char_end": "10",
            }
        )
        status, body, _ = _get(world.app, "/entry?" + bad)
        assert status == 400
        assert "区间非法" in body

        # 未知参数 → 明确 400（不静默忽略）
        noisy = query + "&evil=1"
        status, body, _ = _get(world.app, "/entry?" + noisy)
        assert status == 400
        assert "未知参数" in body


def test_entry_page_for_whole_document_entry_says_it_is_not_highlighted(
    tmp_path: Path,
) -> None:
    with running_frontend(tmp_path) as world:
        status, page, _ = _get(world.app, "/feed?limit=50")
        assert status == 200
        assert "legacy-article" in page
        query = urllib.parse.urlencode(
            {"raw_id": "raw-article-render", "char_start": "0", "char_end": str(len(ARTICLE_TEXT))}
        )
        status, detail, _ = _get(world.app, "/entry?" + query)
        assert status == 200, detail[:400]
        assert "未高亮" in detail
        assert ARTICLE_TEXT in detail


# ===================================================================== #
# 只读性：浏览路径不得创建标签库文件、不得写归档
# ===================================================================== #
def test_browsing_does_not_touch_the_archive_or_create_a_label_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store-2"
    archive = open_archive(root)
    try:
        for record in _records():
            archive.put(record, PAYLOADS[record.raw_id].encode("utf-8"))
    finally:
        archive.close()

    def snapshot() -> Dict[str, str]:
        out: Dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[str(path.relative_to(root))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return out

    db_path = tmp_path / "labels" / "atlas.db"
    app = build_webapp(
        root,
        db_path=db_path,
        industry_provider=lambda: ("ai", "web"),
        actor="tester",
        initialize_labels=False,  # 显式关掉初始化：纯浏览不该建库
    )
    try:
        before = snapshot()
        for path in ("/feed", "/feed?order=asc", "/feed?labeled=true", "/health"):
            status, _, _ = _get(app, path)
            assert status == 200, path
        assert not db_path.exists(), "纯浏览不得创建标签库（初始化归部署入口）"
        assert snapshot() == before, "浏览不得改动归档"
    finally:
        app.close()


def test_cleaned_body_and_failure_category_are_visible_without_network_or_writes(tmp_path):
    from bs4 import BeautifulSoup

    from atlas.archive import ArticleContentStore
    with running_frontend(tmp_path) as world:
        store=ArticleContentStore(world.db_path)
        store.record(source_url='https://example.test/alpha?a=1&b=2',status='ready',content_kind='article',cleaned_text='已经清洗留存的正文第一段。\n\n第二段包含代码 <script>unsafe()</script>。',cleaner_version='test')
        store.record(source_url='https://example.test/gamma',status='unavailable',cleaner_version='test',failure_code='http_forbidden',failure_category='access',failure_label='访问受限',failure_detail='原文 HTTP 403',http_status=403)
        store.close()
        digest=hashlib.sha256(world.db_path.read_bytes()).digest()
        status,page,_=_get(world.app,'/feed?limit=200')
        soup=BeautifulSoup(page,'html.parser')
        assert status==200
        assert '本次正文获取失败：访问受限' in page and '清洗后' in page
        card=next(c for c in soup.select('article.entry') if 'Alpha' in c.select_one('.title').get_text())
        link=next(a['href'] for a in card.select('a') if a.get_text()=='查看归档')
        status,detail,_=_get(world.app,link)
        soup=BeautifulSoup(detail,'html.parser')
        assert status==200 and '清洗后留存的正文' in detail
        assert '<script>unsafe()</script>' in soup.select_one('.cleaned-body').get_text()
        assert not soup.select('.cleaned-body script')
        assert hashlib.sha256(world.db_path.read_bytes()).digest()==digest


def test_recovered_body_does_not_keep_showing_old_unreadable_message(tmp_path):
    from atlas.archive import ArticleContentStore
    from atlas.cognition import BriefStore
    from atlas.webapp import ArchiveEntriesAccess
    with running_frontend(tmp_path) as world:
        with open_archive(world.root) as archive:
            source=next(iter(ArchiveEntriesAccess(archive).brief_sources('raw-feed-render').values()))
        briefs=BriefStore(world.db_path)
        briefs.ensure_sources([source])
        briefs.record_body(source,body='',error='原文 HTTP 403')
        briefs.close()
        bodies=ArticleContentStore(world.db_path)
        bodies.record(source_url=source.url,status='ready',content_kind='article',cleaned_text='已经恢复获取并留存的正文。'*20,cleaner_version='test')
        bodies.close()
        status,page,_=_get(world.app,'/feed?limit=200')
        assert status==200 and '简介待生成：正文已留存' in page
        assert '原文暂时无法读取' not in page
