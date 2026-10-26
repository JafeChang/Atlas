import hashlib
import json

import pytest

from atlas.entries import is_challenge_page, parse_algolia_entries


def test_algolia_objects_keep_original_character_spans_and_stable_anchors() -> None:
    # 嵌套 hits、重复标题、JSON 转义、UTF-8 字节偏移均不能干扰原文中的区间。
    text = r'''{"nested":{"hits":[{"title":"misleading"}]},"hits" : [
      {"title":"中文 \"quoted\" } [ \\ 标题", "url":"https://example.test/一", "created_at":"2026-10-26T09:00:00+08:00","objectID":"1"},
      {"title":"中文 \"quoted\" } [ \\ 标题", "url":null, "created_at":"2026-10-26T01:00:00Z","objectID":"2"},
      {"title":"\u4e2d\u6587", "url":"javascript:alert(1)", "objectID":"3"}
    ], "nbHits":3, "hitsPerPage":20}'''
    raw = text.encode()
    result = parse_algolia_entries(raw, raw_id="raw-json-test")
    assert result is not None and len(result.entries) == 3
    assert result == parse_algolia_entries(raw, raw_id="raw-json-test")
    previous = 0
    for entry, expected in zip(result.entries, json.loads(text)["hits"]):
        assert previous <= entry.char_start < entry.char_end <= len(text)
        assert json.loads(entry.content_slice(text)) == expected
        assert entry.raw_sha256 == hashlib.sha256(raw).hexdigest()
        assert entry.anchor().char_start == entry.char_start
        previous = entry.char_end
    first, second, third = result.entries
    assert first.title == second.title
    assert first.entry_id != second.entry_id
    assert first.published_at == second.published_at == "2026-10-26T01:00:00.000000+00:00"
    assert second.link == "https://news.ycombinator.com/item?id=2"
    assert third.title == "中文" and third.link.endswith("id=3")
    assert result.problems  # 缺少日期有明确诊断，不能伪造发布时间。


@pytest.mark.parametrize("hits", [[], [None, {"title": None}, {"title": "missing link"}]])
def test_empty_or_invalid_hits_are_not_synthetic_articles(hits) -> None:
    raw = json.dumps({"hits": hits, "nbHits": len(hits), "hitsPerPage": 20}).encode()
    result = parse_algolia_entries(raw, raw_id="raw-empty")
    assert result is not None and result.entries == () and result.problems


@pytest.mark.parametrize("raw", [b'{"hits":[]}', b'{"error":"unavailable"}', b'{"hits":', b'plain article', b'<rss/>'])
def test_other_content_is_left_to_existing_parsers(raw: bytes) -> None:
    assert parse_algolia_entries(raw, raw_id="raw-other") is None


def test_only_actual_challenge_redirects_are_unavailable() -> None:
    assert is_challenge_page('<html><head><meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=feed"></head></html>')
    assert is_challenge_page("<META HTTP-EQUIV='Refresh' CONTENT='0; URL=https://example.test/.well-known/sgcaptcha/'>")
    assert not is_challenge_page('<article>This article discusses /.well-known/sgcaptcha/.</article>')
    assert not is_challenge_page('<meta http-equiv="refresh" content="0;url=/real-article">')
