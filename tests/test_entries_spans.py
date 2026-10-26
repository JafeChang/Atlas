"""T-130 判据 3：字符区间可回环（本任务最关键的一条）。

判据原文见 `tests/test_entries_parser.py` 的模块 docstring。

为什么这条最关键
----------------

SPEC §2.2 把证据真值定为 `{raw_id, raw_sha256, char_start, char_end}`。条目层
唯一的合法性来源就是"这些偏移真的指向原文里的这一段"。如果区间只是"看起来像"，
那么锚在区间上的人工标签会**静默指向另一段文字** —— 比没有锚点更糟。

因此本文件不满足于"解析器自己也说它对"，而是要求：

1. 切片在**解码后的原文**上独立取出来，非空、以元素起始标签开头、
   **去标记后包含条目标题**（SPEC §2.2 的实体澄清）；
2. 区间**单调不重叠**、不越界；
3. `verify_offsets()` 只吃 `raw_bytes` 重新算一遍；
4. **校验能失败** —— 注入错误区间必须被抓到（判据 3.4 / 3.5）。
"""

from __future__ import annotations

import dataclasses
import html
import re

import pytest

from atlas.entries import (
    EntryError,
    EntryParseError,
    EntrySet,
    parse_entries,
    sanitize_for_quote,
    verify_offsets,
)
from atlas.normalize.text import decode_bytes

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>回环测试频道</title>
    <item>
      <title>第一条标题：区间必须指向它自己</title>
      <link>https://example.invalid/a</link>
      <pubDate>Thu, 14 Aug 2025 06:31:20 +0000</pubDate>
      <dc:creator><![CDATA[李四]]></dc:creator>
      <description><![CDATA[<p>第一条正文，含 <b>标记</b> 与实体 &amp; 符号。</p>]]></description>
    </item>
    <item>
      <title>Second title with &#8217; a numeric reference</title>
      <link>https://example.invalid/b</link>
      <pubDate>Fri, 15 Aug 2025 07:00:00 +0000</pubDate>
      <description>第二条正文。</description>
    </item>
    <item>
      <title>第三条 &amp; 命名实体标题</title>
      <link>https://example.invalid/c</link>
      <pubDate>Sat, 16 Aug 2025 08:00:00 +0000</pubDate>
      <description>第三条正文。</description>
    </item>
  </channel>
</rss>
"""
RSS_BYTES = RSS_FEED.encode("utf-8")
CONTENT_TYPE = "application/rss+xml"
RAW_ID = "raw_t130_spans"

TAG_RE = re.compile(r"<[^>]*>")


def _entrieset() -> EntrySet:
    entrieset = parse_entries(RSS_BYTES, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.entries, "夹具应当产出条目"
    return entrieset


# =========================================================================== #
# 判据 3.1：切片真的对应那个条目
# =========================================================================== #


def test_criterion3_1_every_span_slices_the_text_of_its_own_entry() -> None:
    """每条：切片非空、以元素起始标签开头、去标记后包含标题。"""
    entrieset = _entrieset()
    for entry in entrieset.entries:
        slice_text = entrieset.feed_text[entry.char_start:entry.char_end]
        assert slice_text.strip(), f"entry[{entry.index}] 切片为空"
        assert slice_text.lstrip().startswith("<item"), slice_text[:40]
        assert slice_text.rstrip().endswith("</item>"), slice_text[-40:]

        plain = TAG_RE.sub("", slice_text)
        collapsed = " ".join(html.unescape(plain).split())
        title = " ".join(entry.title.split())
        assert title in collapsed, (
            f"entry[{entry.index}] 的切片不包含标题：title={title!r} slice={collapsed[:200]!r}"
        )


def test_criterion3_1_title_and_link_are_the_real_ones() -> None:
    """标题 / 链接必须与原文里那条 `<item>` 的字段逐字符一致（不是邻居的）。"""
    entrieset = _entrieset()
    assert [e.title for e in entrieset.entries] == [
        "第一条标题：区间必须指向它自己",
        "Second title with \u2019 a numeric reference",
        "第三条 & 命名实体标题",
    ]
    assert [e.link for e in entrieset.entries] == [
        "https://example.invalid/a",
        "https://example.invalid/b",
        "https://example.invalid/c",
    ]


def test_criterion3_1_span_length_matches_the_slice() -> None:
    """区间长度必须等于切片长度（`char_end - char_start == len(slice)`）。"""
    entrieset = _entrieset()
    for entry in entrieset.entries:
        slice_text = entrieset.feed_text[entry.char_start:entry.char_end]
        assert entry.length == len(slice_text)
        assert entry.raw_slice(entrieset.feed_text) == slice_text


def test_criterion3_1_content_slice_drops_markup_but_keeps_length_relation() -> None:
    """`content_slice` 只删标记：可读文本必须能在原文切片里找到（含实体形态）。"""
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    content = entry.content_slice(entrieset.feed_text)
    assert "<p>" not in content and "第一条正文" in content

    raw = entry.raw_slice(entrieset.feed_text)
    # 切片里可能存在实体（`&amp;`），因此两边的**空白归一形态**都要试
    # —— 与 SPEC §2.2 的实体澄清同一口径。
    haystacks = (raw, sanitize_for_quote(raw))
    for piece in (p for p in content.split() if p):
        assert any(piece in haystack for haystack in haystacks), piece


# =========================================================================== #
# 判据 3.2：单调、不越界、非空
# =========================================================================== #


def test_criterion3_2_spans_are_monotonic_and_within_bounds() -> None:
    entrieset = _entrieset()
    text_length = len(entrieset.feed_text)
    previous_end = 0
    for entry in entrieset.entries:
        assert 0 <= entry.char_start < entry.char_end <= text_length
        assert entry.char_start >= previous_end, "区间重叠或逆序"
        previous_end = entry.char_end


def test_criterion3_2_overlapping_entries_are_rejected_at_construction() -> None:
    """重叠区间在 `EntrySet` 构造期就被拒绝（不靠调用方自觉）。"""
    entrieset = _entrieset()
    forged = dataclasses.replace(entrieset.entries[1], char_start=entrieset.entries[0].char_start + 1)
    with pytest.raises(EntryError):
        dataclasses.replace(entrieset, entries=(entrieset.entries[0], forged))
    # 活对照：不重叠的区间可以构造
    assert dataclasses.replace(entrieset, entries=entrieset.entries).entry_count == 3


def test_criterion3_2_out_of_bounds_span_is_rejected_at_construction() -> None:
    entrieset = _entrieset()
    too_far = dataclasses.replace(
        entrieset.entries[-1], char_end=len(entrieset.feed_text) + 10
    )
    with pytest.raises(EntryError):
        dataclasses.replace(
            entrieset, entries=(*entrieset.entries[:-1], too_far)
        )
    # 活对照：正好的末尾可以构造
    exact = dataclasses.replace(
        entrieset.entries[-1], char_end=len(entrieset.feed_text)
    )
    assert dataclasses.replace(entrieset, entries=(*entrieset.entries[:-1], exact))


# =========================================================================== #
# 判据 3.3：可独立重算
# =========================================================================== #


def test_criterion3_3_verify_offsets_passes_and_reports_every_entry() -> None:
    entrieset = _entrieset()
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=entrieset, content_type=CONTENT_TYPE
    )
    assert report.ok, report.failures
    assert report.checked == len(entrieset.entries) == report.passed
    assert report.failed == 0
    assert report.text_length == len(entrieset.feed_text)
    assert all(item.title_represented and item.slice_non_empty for item in report.entries)


def test_criterion3_3_verify_offsets_recomputes_the_text_from_bytes() -> None:
    """`verify_offsets` 的文本必须来自**重新解码**，而不是产物自带的字段。

    做法：把产物自带的 `feed_text` 换掉，`verify_offsets` 仍应按 `raw_bytes`
    重新解码并察觉不一致（否则它只是在自证）。
    """
    entrieset = _entrieset()
    tampered = dataclasses.replace(entrieset, feed_text=entrieset.feed_text + "\n<!-- x -->")
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=tampered, content_type=CONTENT_TYPE
    )
    assert not report.ok
    assert any("不一致" in failure for failure in report.failures), report.failures


def test_criterion3_3_independent_slice_equality_without_using_feed_text() -> None:
    """完全绕开产物：只用 `raw_bytes` 解码，逐条比对区间与切片。"""
    entrieset = _entrieset()
    text, _encoding = decode_bytes(RSS_BYTES, CONTENT_TYPE)
    assert text == entrieset.feed_text  # 活对照：两边一致
    for entry in entrieset.entries:
        assert text[entry.char_start:entry.char_end] == entry.raw_slice(text)
        assert entry.title.split()[0] in TAG_RE.sub("", text[entry.char_start:entry.char_end])


# =========================================================================== #
# 判据 3.4：校验**能失败**（注入错误区间必须被抓到）
# =========================================================================== #


def _forge(entrieset: EntrySet, index: int, **changes) -> EntrySet:
    """造一个**绕过构造期自检**的产物，专门用来检验 `verify_offsets` 能不能抓到。

    为什么要绕过：`EntrySet.__post_init__` 已经会拒绝重叠 / 越界区间（这是好事），
    因此"校验器能否发现重叠"这条断言必须在**构造期防线之外**再验一次 ——
    否则我们测的只是构造期，而 `verify_offsets` 自己的重叠检测就成了死代码。
    这里用 `object.__new__` + `object.__setattr__` 显式伪造一个冻结记录，
    正是"绕过防线"的最直白写法。
    """
    entries = list(entrieset.entries)
    entries[index] = dataclasses.replace(entries[index], **changes)
    forged = object.__new__(EntrySet)
    for field in dataclasses.fields(EntrySet):
        object.__setattr__(forged, field.name, getattr(entrieset, field.name))
    object.__setattr__(forged, "entries", tuple(entries))
    return forged


def test_criterion3_4_shifted_start_is_caught() -> None:
    """把区间起点后移 5 字符（不越界、不重叠）→ 切片不再包含标题，必须被抓到。"""
    entrieset = _entrieset()
    forged = _forge(entrieset, 1, char_start=entrieset.entries[1].char_start + 5)
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=forged, content_type=CONTENT_TYPE
    )
    assert not report.ok, "把区间起点后移 5 字符竟然通过了回环校验"
    assert report.failed == 1
    assert report.entries[1].ok is False
    assert report.entries[0].ok is True  # 只该失败被改的那一条
    assert report.entries[2].ok is True
    # 失败原因是"切片不再是一个完整的元素"（`<item>` 被削掉了一截），
    # 这正是回环校验要抓的那类错误。
    assert "不以元素起始标签开头" in report.entries[1].reason


def test_criterion3_4_overlapping_start_is_caught_as_overlap() -> None:
    """把区间起点前移进入上一个条目 → 报"重叠或逆序"。"""
    entrieset = _entrieset()
    forged = _forge(entrieset, 1, char_start=entrieset.entries[0].char_start)
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=forged, content_type=CONTENT_TYPE
    )
    assert not report.ok
    assert any("重叠或逆序" in item.reason for item in report.entries), [
        item.reason for item in report.entries
    ]


def test_criterion3_4_shortened_end_is_caught() -> None:
    entrieset = _entrieset()
    forged = _forge(entrieset, 0, char_end=entrieset.entries[0].char_start + 12)
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=forged, content_type=CONTENT_TYPE
    )
    assert not report.ok
    assert report.entries[0].title_represented is False


def test_criterion3_4_swapped_spans_are_caught() -> None:
    """交换两条的区间 → 单调性被破坏。

    两道防线都要验：`EntrySet` **构造期**拒绝（正常路径），
    绕过构造期后 `verify_offsets` 仍然报"重叠或逆序"（校验器自身的检测）。
    """
    entrieset = _entrieset()
    first, second = entrieset.entries[0], entrieset.entries[1]
    swapped = (
        dataclasses.replace(first, char_start=second.char_start, char_end=second.char_end),
        dataclasses.replace(second, char_start=first.char_start, char_end=first.char_end),
        entrieset.entries[2],
    )
    with pytest.raises(EntryError):
        # 第一道：构造期就会因为重叠/逆序拒绝
        dataclasses.replace(entrieset, entries=swapped)

    # 第二道：绕过构造期后，校验器必须自己发现
    forged = _forge(entrieset, 0, char_start=second.char_start, char_end=second.char_end)
    forged = _forge(forged, 1, char_start=first.char_start, char_end=first.char_end)
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=forged, content_type=CONTENT_TYPE
    )
    assert not report.ok
    assert any("重叠或逆序" in item.reason for item in report.entries), [
        item.reason for item in report.entries
    ]


def test_criterion3_4_neighbour_span_is_caught() -> None:
    """把第 2 条的区间改成第 3 条的区间（切片合法、但不属于它）。"""
    entrieset = _entrieset()
    third = entrieset.entries[2]
    forged = _forge(
        entrieset, 1, char_start=third.char_start, char_end=third.char_end
    )
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=forged, content_type=CONTENT_TYPE
    )
    assert not report.ok
    assert report.entries[1].title_represented is False
    # 复现真正的原因：那条切片属于**别的**条目
    neighbour = dataclasses.replace(
        entrieset.entries[1],
        char_start=third.char_start,
        char_end=third.char_end,
    )
    assert neighbour.raw_slice(entrieset.feed_text) == third.raw_slice(entrieset.feed_text)
    assert neighbour.title not in neighbour.raw_slice(entrieset.feed_text)


def test_criterion3_4_changed_byte_is_caught() -> None:
    """原文换一个字节 → 指纹不符 + 解码文本与产物不符，两条都要报。"""
    entrieset = _entrieset()
    tampered = RSS_BYTES.replace(b"https://example.invalid/a", b"https://example.invalid/z")
    assert tampered != RSS_BYTES
    report = verify_offsets(
        raw_id=RAW_ID, raw_bytes=tampered, entries=entrieset, content_type=CONTENT_TYPE
    )
    assert not report.ok
    assert any("指纹" in failure for failure in report.failures), report.failures


def test_criterion3_4_wrong_raw_id_is_caught() -> None:
    entrieset = _entrieset()
    report = verify_offsets(
        raw_id="raw_someone_else",
        raw_bytes=RSS_BYTES,
        entries=entrieset,
        content_type=CONTENT_TYPE,
    )
    assert not report.ok
    assert any("raw_id" in failure for failure in report.failures)
    # 活对照：正确的 raw_id 通过
    good = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=entrieset, content_type=CONTENT_TYPE
    )
    assert good.ok


def test_criterion3_4_wrong_content_type_is_refused_loudly() -> None:
    """用错 content_type 会走另一条解码路径 —— 必须**响亮失败**，不能静默错位。

    这是回环校验真正的价值：它把"区间是不是对着同一份文本"从假设降级为断言。
    `text/html` 会让 T-104 走 HTML 路径（剥标签），区间必然错位；条目层因此
    在解码阶段就拒绝，而不是产出一批悄悄指错地方的区间。
    """
    entrieset = _entrieset()
    with pytest.raises(EntryParseError):
        verify_offsets(
            raw_id=RAW_ID,
            raw_bytes=RSS_BYTES,
            entries=entrieset,
            content_type="text/html; charset=utf-8",
        )
    # 活对照：正确的 content_type 通过
    good = verify_offsets(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, entries=entrieset, content_type=CONTENT_TYPE
    )
    assert good.ok


# =========================================================================== #
# 判据 3.5：产物内部自检也能失败
# =========================================================================== #


def test_criterion3_5_verify_spans_passes_on_the_real_output() -> None:
    entrieset = _entrieset()
    report = entrieset.verify_spans()
    assert report.ok, report.failures
    assert report.checked == len(entrieset.entries)
    assert entrieset.verify_ids() is True


def test_criterion3_5_verify_spans_detects_a_tampered_feed_text() -> None:
    entrieset = _entrieset()
    tampered = dataclasses.replace(entrieset, feed_text="x" + entrieset.feed_text)
    report = tampered.verify_spans()
    assert not report.ok
    assert any("raw_sha256" in failure for failure in report.failures), report.failures


def test_criterion3_5_verify_spans_detects_forged_span() -> None:
    entrieset = _entrieset()
    forged = _forge(entrieset, 0, char_end=entrieset.entries[0].char_start + 12)
    report = forged.verify_spans()
    assert not report.ok
    assert report.entries[0].title_represented is False


# =========================================================================== #
# 编码：区间所在的参照系必须是**实际解码出的文本**
# =========================================================================== #


def test_gbk_feed_with_wrong_declaration_decodes_and_spans_still_line_up() -> None:
    """GBK 字节 + 声明 UTF-8（真实世界常见）→ 回退解码成功，区间仍然自洽。

    判据 5.5 的正向一半：编码异常不等于必须失败 —— 能严格解码就要如实解码，
    **同时把实际编码记进产物**（`EntrySet.encoding`），因为区间是相对它的。
    """
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<rss version=\"2.0\"><channel><title>频道</title>"
        "<item><title>中文标题一</title><link>https://example.invalid/g1</link>"
        "<description>中文正文一</description></item>"
        "<item><title>中文标题二</title><link>https://example.invalid/g2</link>"
        "<description>中文正文二</description></item>"
        "</channel></rss>\n"
    )
    payload = feed.encode("gb18030")
    assert b"<title>\xd6\xd0\xce\xc4" in payload  # 活对照：确实是 GBK 字节

    entrieset = parse_entries(payload, "application/rss+xml", raw_id="raw_gbk")
    assert entrieset.encoding.lower() in ("gb18030", "gbk")
    assert [e.title for e in entrieset.entries] == ["中文标题一", "中文标题二"]

    report = verify_offsets(
        raw_id="raw_gbk",
        raw_bytes=payload,
        entries=entrieset,
        content_type="application/rss+xml",
    )
    assert report.ok, report.failures
    for entry in entrieset.entries:
        assert entrieset.feed_text[entry.char_start:entry.char_end].startswith("<item>")
        assert entry.title in entrieset.feed_text[entry.char_start:entry.char_end]
