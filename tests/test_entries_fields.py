"""T-130 判据 5：**不假装成功**（每一种异常输入都有明确行为，不得静默返回空）。

判据原文见 `tests/test_entries_parser.py` 的模块 docstring。真实数据里就有两类
"HTTP 200 但不是 feed"的输入（SPEC §6.3）：

| 渠道 | 实际内容 | 本层行为 |
|---|---|---|
| `hacker-news-frontpage` | Algolia **JSON API 响应** | `EntryParseError`（**响亮失败**） |
| `ai-news-blog` | WordPress **HTML 页面** | `EntryParseError`（**响亮失败**） |

**硬规则 4 的"活对照"贯穿全文件**：每一条"拒绝"断言旁边都有一条同一调用路径上
**成功**的对照，否则签名不匹配 / 异常类型不对会伪装成"拒绝成功"。

（所有夹具都用 `_u(...)` 把字符串编成 UTF-8 字节 —— 含中文的夹具因此在源码里
保持可读，同时字节形态与真实抓取完全一致。）
"""

from __future__ import annotations

import pytest

from atlas.entries import (
    EntryParseError,
    FeedKind,
    decode_feed,
    parse_entries,
    verify_offsets,
)

RAW_ID = "raw_t130_fields"
CONTENT_TYPE = "application/rss+xml"


def _u(text: str) -> bytes:
    """字符串 → UTF-8 字节（夹具统一入口，含中文时仍然可读）。"""
    return text.encode("utf-8")


MINIMAL_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>最小可用</title>
<item><title>唯一一篇</title><link>https://example.invalid/1</link>
<description>正文</description></item>
</channel></rss>
"""


def _ok_control(raw_bytes: bytes | None = None):
    """活对照：同一条调用路径上的**成功**样本。"""
    entrieset = parse_entries(
        MINIMAL_FEED.encode("utf-8") if raw_bytes is None else raw_bytes,
        CONTENT_TYPE,
        raw_id=RAW_ID,
    )
    assert entrieset.entry_count == 1, entrieset.problems
    return entrieset


# =========================================================================== #
# 判据 5.1：非 feed → 响亮失败
# =========================================================================== #


def test_criterion5_1_json_api_response_fails_loudly() -> None:
    """JSON API 响应（真实数据 hacker-news-frontpage 的形态）。"""
    payload = _u(
        '{"exhaustive":{"nbHits":false},"hits":[{"title":"a story",'
        '"url":"https://example.invalid/x","created_at":"2026-09-25T00:00:00Z"}]}'
    )
    with pytest.raises(EntryParseError) as excinfo:
        parse_entries(payload, "application/json", raw_id=RAW_ID)
    assert "JSON" in str(excinfo.value)
    _ok_control()  # 活对照


def test_criterion5_1_json_detected_by_content_without_content_type() -> None:
    """即使没有 content_type，JSON 结构也能被认出来（真实归档里就没有 content type）。"""
    with pytest.raises(EntryParseError):
        parse_entries(_u('[{"id":1,"title":"x"}]'), "", raw_id=RAW_ID)
    _ok_control()


def test_criterion5_1_html_page_fails_loudly() -> None:
    """HTML 页面（真实数据 ai-news-blog 的形态）。"""
    payload = _u(
        '<!doctype html><html lang="en-GB"><head><title>AI News</title></head>'
        "<body><h1>Latest</h1></body></html>"
    )
    with pytest.raises(EntryParseError) as excinfo:
        parse_entries(payload, "text/html; charset=utf-8", raw_id=RAW_ID)
    assert "HTML" in str(excinfo.value)
    _ok_control()


def test_criterion5_1_html_detected_by_content_without_content_type() -> None:
    with pytest.raises(EntryParseError):
        parse_entries(_u("<html><body>no feed here</body></html>"), "", raw_id=RAW_ID)
    _ok_control()


def test_criterion5_1_plain_text_fails_loudly() -> None:
    """一句话的响应体也不是 feed：不是 XML → 响亮失败（带着开头内容便于定位）。"""
    with pytest.raises(EntryParseError) as excinfo:
        parse_entries(_u("Just a sentence, not a feed at all.\n"), "", raw_id=RAW_ID)
    assert "良构" in str(excinfo.value) or "XML" in str(excinfo.value)
    _ok_control()


# =========================================================================== #
# 判据 5.2：合法 XML 但未知根元素 → 0 条 + 理由（不猜）
# =========================================================================== #


def test_criterion5_2_unknown_xml_root_returns_zero_with_reason() -> None:
    """XML 是良构的，只是不认识 → **不是错误**（不猜），但必须给出理由。"""
    payload = _u(
        '<?xml version="1.0"?><sitemapindex><sitemap>'
        "<loc>https://x/1</loc></sitemap></sitemapindex>"
    )
    entrieset = parse_entries(payload, "application/xml", raw_id=RAW_ID)
    assert entrieset.entry_count == 0
    assert entrieset.kind == FeedKind.UNKNOWN
    assert entrieset.problems, "0 条却没有理由"
    assert any("无法识别" in problem for problem in entrieset.problems)
    _ok_control()  # 活对照


def test_criterion5_2_unknown_root_is_not_reported_as_success() -> None:
    """0 条**不等于**成功：`problems` 必须非空，且 `kind` 明确是 unknown。"""
    payload = _u('<opml version="2.0"><head></head><body></body></opml>')
    entrieset = parse_entries(payload, "", raw_id=RAW_ID)
    assert entrieset.entries == ()
    assert entrieset.kind == FeedKind.UNKNOWN
    assert entrieset.problems


# =========================================================================== #
# 判据 5.3：合法 feed 但 0 条目 → 0 条 + 理由
# =========================================================================== #


def test_criterion5_3_empty_rss_channel_returns_zero_with_reason() -> None:
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>空频道</title></channel></rss>'
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.kind == FeedKind.RSS
    assert entrieset.entry_count == 0
    assert any("没有任何 <item> 条目" in problem for problem in entrieset.problems)
    _ok_control()


def test_criterion5_3_empty_atom_feed_returns_zero_with_reason() -> None:
    payload = _u(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>empty</title></feed>'
    )
    entrieset = parse_entries(payload, "application/atom+xml", raw_id=RAW_ID)
    assert entrieset.kind == FeedKind.ATOM
    assert entrieset.entry_count == 0
    assert any("没有任何 <entry> 条目" in problem for problem in entrieset.problems)
    _ok_control()


# =========================================================================== #
# 判据 5.4：只有 <item> 没有 <title> → 丢弃但**留下理由**
# =========================================================================== #


def test_criterion5_4_item_without_title_is_dropped_with_reason() -> None:
    """无标题条目不进入序列（不编造标题），但理由必须出现在 `problems`。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><link>https://example.invalid/no-title</link>"
        "<description>没有标题的条目</description></item>"
        "<item><title>有标题</title><link>https://example.invalid/ok</link>"
        "<description>正常条目</description></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.entry_count == 1  # 活对照：有标题的那条被保留了
    assert entrieset.entries[0].title == "有标题"
    assert entrieset.problems, "无标题条目被静默丢弃了"
    joined = "\n".join(entrieset.problems)
    assert "无标题" in joined and "entry[0]" in joined


def test_criterion5_4_blank_title_is_treated_as_missing() -> None:
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>   </title><link>https://example.invalid/blank</link></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.entry_count == 0
    assert any("无标题" in problem for problem in entrieset.problems)
    _ok_control()


def test_criterion5_4_all_items_without_titles_yields_explicit_zero() -> None:
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><description>a</description></item>"
        "<item><description>b</description></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.entry_count == 0
    assert len(entrieset.problems) >= 2, "两条无标题条目至少各留一条理由"
    _ok_control()


# =========================================================================== #
# 字段缺失/异常：仍产出条目，但逐条记录理由
# =========================================================================== #


def test_entry_without_link_is_kept_with_a_recorded_problem() -> None:
    """缺链接不是致命伤（真实 feed 里常见）：条目保留，理由记进 `Entry.problems`。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>没有链接</title><description>正文</description></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.entry_count == 1
    entry = entrieset.entries[0]
    assert entry.link == ""
    assert any(problem.field == "link" for problem in entry.problems), entry.problems
    # 活对照：有链接的条目没有 link 问题
    control = _ok_control()
    assert not any(p.field == "link" for p in control.entries[0].problems)


def test_entry_without_date_is_kept_with_a_recorded_problem() -> None:
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>没有时间</title><link>https://example.invalid/nd</link></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    entry = entrieset.entries[0]
    assert entry.published_at is None
    assert any(p.field == "published_at" for p in entry.problems)
    # 活对照：同一条条目加上 pubDate 后不再报这个字段
    dated = parse_entries(
        _u(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>c</title>'
            "<item><title>有时间</title><link>https://example.invalid/d</link>"
            "<pubDate>Thu, 14 Aug 2025 06:31:20 +0000</pubDate></item>"
            "</channel></rss>"
        ),
        CONTENT_TYPE,
        raw_id=RAW_ID,
    )
    assert not any(p.field == "published_at" for p in dated.entries[0].problems)
    assert dated.entries[0].published_at == "2025-08-14T06:31:20.000000+00:00"


def test_unparseable_date_is_recorded_and_date_is_none() -> None:
    """时间字段存在但无法解析 → 记为问题，`published_at` 为 `None`（不编造时间）。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>坏时间</title><link>https://example.invalid/bd</link>"
        "<pubDate>not a date at all</pubDate></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    entry = entrieset.entries[0]
    assert entry.published_at is None
    assert any("无法解析" in p.reason for p in entry.problems), entry.problems
    # 活对照：合法 RFC 2822 日期能解析出来
    good = parse_entries(
        _u(
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<item><title>T</title><link>https://e/1</link>"
            "<pubDate>Fri, 25 Sep 2026 14:00:07 +0000</pubDate></item></channel></rss>"
        ),
        CONTENT_TYPE,
        raw_id=RAW_ID,
    )
    assert good.entries[0].published_at == "2026-09-25T14:00:07.000000+00:00"
    assert not good.entries[0].problems


def test_dates_are_normalized_to_utc_strings() -> None:
    """RSS（RFC 2822，带 +0000/GMT）与 Atom（ISO 8601，带 -07:00 / Z）都归一到 UTC。"""
    rss = parse_entries(
        _u(
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<item><title>A</title><link>https://e/1</link>"
            "<pubDate>Thu, 14 Aug 2025 06:31:20 GMT</pubDate></item>"
            "<item><title>B</title><link>https://e/2</link>"
            "<pubDate>Thu, 14 Aug 2025 13:31:20 +0000</pubDate></item>"
            "</channel></rss>"
        ),
        CONTENT_TYPE,
        raw_id=RAW_ID,
    )
    assert [e.published_at for e in rss.entries] == [
        "2025-08-14T06:31:20.000000+00:00",
        "2025-08-14T13:31:20.000000+00:00",
    ]
    atom = parse_entries(
        _u(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            '<entry><title>C</title><link href="https://e/3"/>'
            "<published>2025-08-14T06:31:20.000-07:00</published></entry>"
            '<entry><title>D</title><link href="https://e/4"/>'
            "<published>2025-08-14T13:31:20Z</published></entry></feed>"
        ),
        "application/atom+xml",
        raw_id=RAW_ID,
    )
    assert [e.published_at for e in atom.entries] == [
        "2025-08-14T13:31:20.000000+00:00",
        "2025-08-14T13:31:20.000000+00:00",
    ]
    # 跨形态一致性：RSS 的 `Thu, 14 Aug 2025 13:31:20 GMT` 与 Atom 的
    # `2025-08-14T06:31:20.000-07:00` 表示**同一时刻**，归一化后必须逐字符相同。
    assert rss.entries[1].published_at == atom.entries[0].published_at
    assert rss.entries[0].published_at != atom.entries[0].published_at  # 时区换算真的发生了


# =========================================================================== #
# 判据 5.5：编码异常
# =========================================================================== #


def test_criterion5_5_undecodable_bytes_fail_loudly() -> None:
    """既不是 UTF-8 也不是 GB18030 的字节 → 响亮失败（不用 replace 编造文本）。"""
    payload = (
        _u('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>')
        + bytes(range(0x80, 0x100))
        + _u("</channel></rss>")
    )
    with pytest.raises(EntryParseError) as excinfo:
        parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert "解码" in str(excinfo.value)
    _ok_control()  # 活对照


def test_criterion5_5_latin1_feed_decodes_and_is_reported_as_latin1() -> None:
    """声明 iso-8859-1 的 feed：T-104 的声明优先解码链支持它 —— 解出来，并如实报编码。

    这一条同时是"不假装成功"的另一半：**LATIN-1 能解就必须解**（不能因为
    "不是 utf-8 就拒绝"），而实际编码必须出现在产物里，因为字符区间相对它。
    """
    payload = (
        '<?xml version="1.0" encoding="iso-8859-1"?><rss version="2.0"><channel>'
        "<item><title>caf\u00e9 na\u00efve</title><link>https://e/1</link>"
        "<pubDate>Thu, 14 Aug 2025 06:31:20 +0000</pubDate></item>"
        "</channel></rss>"
    ).encode("iso-8859-1")
    entrieset = parse_entries(payload, "application/rss+xml; charset=iso-8859-1", raw_id=RAW_ID)
    assert entrieset.entry_count == 1
    assert entrieset.entries[0].title == "café naïve"
    assert entrieset.encoding.lower() in ("iso-8859-1", "latin-1", "latin1", "iso8859-1")
    report = verify_offsets(
        raw_id=RAW_ID,
        raw_bytes=payload,
        entries=entrieset,
        content_type="application/rss+xml; charset=iso-8859-1",
    )
    assert report.ok, report.failures
    _ok_control()  # 活对照：UTF-8 夹具同样成功


def test_criterion5_5_utf8_bom_is_handled() -> None:
    """UTF-8 BOM：必须解出来（T-104 的 BOM 分支），且条目正常。"""
    payload = b"\xef\xbb\xbf" + MINIMAL_FEED.encode("utf-8")
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.entry_count == 1
    assert entrieset.entries[0].title == "唯一一篇"
    assert entrieset.encoding.startswith("utf-8")


def test_criterion5_5_encoding_is_reported_so_offsets_have_a_reference_frame() -> None:
    """实际编码必须记进产物 —— 字符区间是相对"解码后的文本"的，编码是参照系的一半。"""
    entrieset = _ok_control()
    assert entrieset.encoding == "utf-8"
    text, encoding, kind = decode_feed(MINIMAL_FEED.encode("utf-8"), CONTENT_TYPE)
    assert text == entrieset.feed_text
    assert encoding == entrieset.encoding
    assert kind == entrieset.kind == FeedKind.RSS


# =========================================================================== #
# 判据 5.6：空字节 / 纯空白 / 结构损坏
# =========================================================================== #


def test_criterion5_6_zero_byte_response_fails_loudly() -> None:
    """SPEC §2.12：2xx + 空响应体是**失败**，不是空 feed。"""
    with pytest.raises(EntryParseError) as excinfo:
        parse_entries(b"", "application/rss+xml", raw_id=RAW_ID)
    assert "空" in str(excinfo.value)
    _ok_control()


def test_criterion5_6_whitespace_only_fails_loudly() -> None:
    with pytest.raises(EntryParseError):
        parse_entries(b"   \n\t  ", "application/rss+xml", raw_id=RAW_ID)
    _ok_control()


def test_criterion5_6_truncated_xml_fails_loudly() -> None:
    """截断的 XML（没有根元素闭合）→ 响亮失败。"""
    payload = MINIMAL_FEED.encode("utf-8")[:120]
    with pytest.raises(EntryParseError):
        parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    _ok_control()


def test_criterion5_6_mismatched_tags_fail_loudly() -> None:
    payload = _u('<rss version="2.0"><channel><title>x</rss></channel>')
    with pytest.raises(EntryParseError):
        parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    _ok_control()


def test_criterion5_6_unclosed_item_fails_loudly() -> None:
    """未闭合的 `<item>` 让整份 XML 不良构 → 解码阶段就响亮失败（**不猜**边界）。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>未闭合</title><link>https://e/1</link>"
        "<description>d</description>"
    )
    with pytest.raises(EntryParseError):
        parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    _ok_control()


# =========================================================================== #
# 真实形态的合成复刻（命名空间前缀 / CDATA / Atom href / guid 兜底）
# =========================================================================== #


def test_prefixed_namespaces_are_injected_per_entry() -> None:
    """真实 RSS 把 `xmlns:dc` 声明在根上、条目切片里没有 —— 必须能解析。

    这是 10 份真实 feed 里 8 份的形态；用全局 `register_namespace` 也能"解决"，
    但那会引入进程级全局可变状态（判据 1.3 已禁）。本测试钉死注入法的正确性。
    """
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel><title>c</title>"
        "<item><title>前缀条目</title><link>https://e/p</link>"
        "<pubDate>Thu, 14 Aug 2025 06:31:20 +0000</pubDate>"
        "<dc:creator><![CDATA[作者]]></dc:creator>"
        "<content:encoded><![CDATA[<p>富文本正文</p>]]></content:encoded></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    entry = entrieset.entries[0]
    assert entry.title == "前缀条目"
    assert entry.link == "https://e/p"
    assert "富文本正文" in entry.entry_text, entry.entry_text
    assert not entry.problems


def test_atom_entry_with_href_link_and_html_content() -> None:
    payload = _u(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>f</title>'
        "<entry><id>tag:e,2025:1</id>"
        '<title type="text">Atom 标题</title>'
        '<link rel="alternate" href="https://e/atom/1"/>'
        '<link rel="self" href="https://e/atom/1.xml"/>'
        "<published>2025-08-14T06:31:20Z</published>"
        '<content type="html">&lt;p&gt;Atom 正文&lt;/p&gt;</content></entry>'
        "</feed>"
    )
    entrieset = parse_entries(payload, "application/atom+xml", raw_id=RAW_ID)
    entry = entrieset.entries[0]
    assert entry.kind == FeedKind.ATOM
    assert entry.link == "https://e/atom/1"  # 取 rel="alternate"
    assert "Atom 正文" in entry.entry_text
    assert not entry.problems


def test_rss_guid_permalink_is_used_when_link_is_absent() -> None:
    """RSS 的链接兜底路径：`<guid isPermaLink="true">` 也是合法链接。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>靠 guid</title>"
        '<guid isPermaLink="true">https://e/guid/1</guid></item>'
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    entry = entrieset.entries[0]
    assert entry.link == "https://e/guid/1"
    assert not any(p.field == "link" for p in entry.problems)


def test_rss_guid_that_is_not_a_permalink_does_not_become_the_link() -> None:
    """`isPermaLink="false"` 的 guid 不是链接 —— 不得冒充。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>非永久链接</title>"
        '<guid isPermaLink="false">tag:example.invalid,2025:1</guid></item>'
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    entry = entrieset.entries[0]
    assert entry.link == ""
    assert any(p.field == "link" for p in entry.problems)
    # 活对照：真 permalink 会被采纳 —— 确认这个区分真的有效果
    good = parse_entries(
        _u(
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<item><title>t</title>"
            '<guid isPermaLink="true">https://e/yes</guid></item></channel></rss>'
        ),
        CONTENT_TYPE,
        raw_id=RAW_ID,
    )
    assert good.entries[0].link == "https://e/yes"


def test_markup_only_item_is_kept_but_has_no_entry_text() -> None:
    """没有正文的条目仍然有效（标题+链接足够），`entry_text` 为空串而不是编造。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<item><title>只有标题</title><link>https://e/1</link>"
        "<pubDate>Thu, 14 Aug 2025 06:31:20 +0000</pubDate></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    entry = entrieset.entries[0]
    assert entry.entry_text == ""
    assert entry.title == "只有标题"
    assert not entry.problems


def test_items_element_is_not_confused_with_item() -> None:
    """`<items>` 不得被当成 `<item>`（边界判据：标签名后必须是空白 / `>` / `/`）。"""
    payload = _u(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>c</title>'
        "<items><title>伪条目</title></items>"
        "<item><title>真条目</title><link>https://e/1</link></item>"
        "</channel></rss>"
    )
    entrieset = parse_entries(payload, CONTENT_TYPE, raw_id=RAW_ID)
    assert [e.title for e in entrieset.entries] == ["真条目"]
    # 活对照：真条目确实被识别到了
    assert entrieset.entry_count == 1
