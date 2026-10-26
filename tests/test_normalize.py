"""T-104 归一化文本层 + 偏移映射的验收测试（SPEC §2.2 / §2.10 / §4.2）。

判据对应关系（逐条真实断言，不测"函数能跑"）：

1. 偏移精确性：任意区间 `raw[to_raw(ns):to_raw(ne)]` **经实体解码后**必须包含首尾非空白字符；
   普通字符（非实体来源）仍要求逐字符精确映射
2. 单调性：`to_raw_offset` 对递增输入不减
3. 实体与编码：HTML 实体解码；UTF-8 / gb18030（content_type charset 或 <meta charset>）
4. 块定位是派生物：可由原文重建；且不得作为人工产物的锚点
5. 与 T-002 打通：`build_anchor(...)` 产出 VERIFIED 锚点，且原文切片经解码后覆盖 quote 首尾字符
6. 零新增依赖：只用 stdlib + 已声明依赖

> 修正记录（主代理）：最初判据 1 写作"原文切片包含该区间的首尾字符"，但该表述在
> **实体来源**下不可能成立（原文里是 `&mdash;`，字符是 `—`）。已按 SPEC §2.2 的澄清
> 改为"经实体解码后包含"，并保留普通字符的逐字符精确断言。
"""

from __future__ import annotations

import html

import pytest

from atlas.contracts import (
    VerificationStatus,
    build_anchor,
)
from atlas.normalize import (
    NormalizeError,
    NormalizedText,
    SegmentTable,
    attach_blocks,
    decode_bytes,
    derive_blocks,
    looks_like_html,
    normalize,
    normalize_html,
)
from atlas.normalize.text import normalize_plain_text

# 含标签、实体、多空白的 HTML 样例：判据 1 的核心夹具
HTML_SAMPLE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Atlas  证据 &amp; 归一化</title>
  <style>body { color: red; }</style>
  <script>var x = "1 &lt; 2 &amp;&amp; 3";</script>
</head>
<body>
  <h1>证据锚点</h1>
  <p>  证据锚点是 <b>2C</b>   字符区间 &mdash; 不可变 &nbsp;&nbsp; 真值。</p>
  <p>实体：AT&amp;T 与 &#39;引号&#39; 与 &lt;tag&gt;。</p>
  <div>块三：<br>第二行</div>
</body>
</html>
"""


def _non_whitespace_positions(text: str) -> list[int]:
    return [i for i, ch in enumerate(text) if not ch.isspace()]


# ---------------------------------------------------------------------------
# 判据 1：偏移精确性
# ---------------------------------------------------------------------------


def test_criterion1_raw_slice_contains_first_and_last_non_whitespace_html() -> None:
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    raw = nt.raw_text
    assert nt.source_kind == "html"
    assert nt.text.strip() == nt.text and nt.text

    positions = _non_whitespace_positions(nt.text)
    assert positions, "归一化文本必须有非空白字符"

    checked = 0
    spans: list[tuple[int, int]] = [(p, p + 1) for p in positions]
    for start in range(0, len(nt.text), 3):
        for end in range(start + 1, min(len(nt.text), start + 40) + 1, 7):
            spans.append((start, end))

    for start, end in spans:
        inside = [p for p in positions if start <= p < end]
        if not inside:
            continue
        first, last = inside[0], inside[-1]
        raw_start = nt.to_raw_offset(start)
        raw_end = nt.to_raw_offset(end)
        assert 0 <= raw_start <= raw_end <= len(raw)
        # 实体来源时原文是实数字面量，因此必须**解码后**再判断包含关系
        window = html.unescape(raw[raw_start:raw_end])
        assert nt.text[first] in window, (start, end, first, repr(nt.text[first]), repr(window))
        assert nt.text[last] in window, (start, end, last, repr(nt.text[last]), repr(window))
        # 首尾字符各自映射到的原文位置必须落在窗口内
        assert raw_start <= nt.to_raw_offset(first) < raw_end
        assert raw_start <= nt.to_raw_offset(last) < raw_end
        checked += 1
    assert checked > 100, f"断言覆盖的区间太少：{checked}"


def test_criterion1_plain_text_chars_map_exactly() -> None:
    """普通字符（非实体来源）必须逐字符精确映射。"""
    raw = "OpenAI 发布了新的推理模型，显著降低了延迟。".encode("utf-8")
    nt = normalize(raw, "text/plain; charset=utf-8")
    checked = 0
    for i, ch in enumerate(nt.text):
        if ch.isspace():
            continue
        assert nt.raw_text[nt.to_raw_offset(i)] == ch, (i, ch)
        checked += 1
    assert checked > 10, "样例太短，断言没有意义"


def test_criterion1_html_chars_map_to_their_source_representation() -> None:
    """逐字符精确性**用段表直接验证**，避免在段边界上做过度断言。

    按段类型分别断言：

    - 1:1 段（`raw_length == length`）：原文切片必须与归一化切片逐字符相同
    - 实体段：原文切片**经实体解码后**必须等于归一化切片
    - 折叠空白段：只要求映射到该空白串起点
    - 合成段（`raw_length == 0`）：只要求映射落在合法范围内
    """
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    assert nt.segments, "必须有段"
    checked = 0
    for seg in nt.segments:
        chunk = nt.text[seg.norm_start : seg.norm_start + seg.length]
        raw_slice = nt.raw_text[seg.raw_start : seg.raw_start + seg.raw_length]
        if seg.raw_length == 0:
            assert 0 <= seg.raw_start <= len(nt.raw_text)
            continue
        if seg.raw_length == seg.length:
            assert raw_slice == chunk, (seg, raw_slice, chunk)
            checked += len(chunk)
        elif chunk.isspace():
            assert nt.to_raw_offset(seg.norm_start) == seg.raw_start
        else:
            assert html.unescape(raw_slice) == chunk, (seg, raw_slice, chunk)
            checked += len(chunk)
    assert checked > 20, f"逐字符断言覆盖太少：{checked}"


def test_criterion1_collapsed_whitespace_maps_to_run_start() -> None:
    raw = "abc     \n\n\n   def"
    nt = normalize_plain_text(raw)
    assert nt.text == "abc\n\ndef"
    blank_index = nt.text.index("\n")
    assert nt.to_raw_offset(blank_index) == raw.index(" ")
    d_index = nt.text.index("def")
    assert nt.to_raw_offset(d_index) == raw.index("def")


def test_criterion1_entity_offsets_point_at_entity_start() -> None:
    raw = "<p>x &amp; y &#39;z&#39; &lt;k&gt;</p>"
    nt = normalize(raw.encode("utf-8"), "text/html")
    assert nt.text == "x & y 'z' <k>"
    amp = nt.text.index("&")
    assert nt.to_raw_offset(amp) == raw.index("&amp;")
    quote = nt.text.index("'z'")
    assert nt.to_raw_offset(quote) == raw.index("&#39;")
    # 切片从实体起点开始；不含整段（`'z'` 的 `z` 夹在两个实体之间）
    assert nt.raw_text[nt.to_raw_offset(quote) :].startswith("&#39;")
    assert nt.raw_text[nt.to_raw_offset(quote) : nt.to_raw_offset(quote + 1)] == "&#39;"


# ---------------------------------------------------------------------------
# 判据 2：单调性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type,payload",
    [
        ("text/html; charset=utf-8", HTML_SAMPLE.encode("utf-8")),
        ("text/plain", "  a\u00a0\u00a0b \t c\r\n\r\nd  ".encode("utf-8")),
        ("text/plain", b"one\ntwo\n\nthree"),
        ("text/plain", b""),
        ("text/plain", b"   \n\t  "),
    ],
)
def test_criterion2_to_raw_offset_is_monotonic(content_type: str, payload: bytes) -> None:
    nt = normalize(payload, content_type)
    table = nt.to_raw_offset
    assert isinstance(table, SegmentTable)
    previous = -1
    for offset in range(nt.text_length + 1):
        current = table(offset)
        assert current >= previous, (offset, current, previous, nt.text)
        previous = current
    assert table.is_monotonic()


def test_criterion2_table_rejects_non_monotonic_segment() -> None:
    """段表的不变量是代码强制的：非法段表构造即抛错。"""
    from atlas.normalize.text import NormalizeError, Segment

    with pytest.raises(NormalizeError):
        SegmentTable(
            segments=(
                Segment(norm_start=0, raw_start=10, length=1, raw_length=1),
                Segment(norm_start=1, raw_start=0, length=1, raw_length=1),
            ),
            text_length=2,
            raw_text_length=20,
        )
    with pytest.raises(NormalizeError):
        SegmentTable(segments=(), text_length=3, raw_text_length=3)


def test_offset_out_of_range_fails_loudly() -> None:
    nt = normalize_plain_text("abc")
    with pytest.raises(IndexError):
        nt.to_raw_offset(len(nt.text) + 1)
    with pytest.raises(IndexError):
        nt.to_raw_offset(-1)
    assert nt.to_raw_offset(len(nt.text)) == len(nt.raw_text)


# ---------------------------------------------------------------------------
# 判据 3：实体与编码
# ---------------------------------------------------------------------------


def test_criterion3_entities_decoded() -> None:
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    assert "AT&T" in nt.text
    assert "'引号'" in nt.text
    assert "<tag>" in nt.text
    assert "&amp;" not in nt.text
    assert "&#39;" not in nt.text
    assert "—" in nt.text  # &mdash;


def test_criterion3_gb18030_via_content_type() -> None:
    raw = "中文编码测试：半导体与脑机接口。".encode("gb18030")
    nt = normalize(raw, "text/plain; charset=gb18030")
    assert nt.encoding.lower().replace("_", "-") == "gb18030"
    assert "半导体" in nt.text
    assert "脑机接口" in nt.text


def test_criterion3_charset_by_meta_tag() -> None:
    html_doc = '<html><head><meta charset="gb18030"></head><body>半导体</body></html>'
    raw = html_doc.encode("gb18030")
    nt = normalize(raw, "text/html")  # 头里不给 charset，靠 <meta>
    assert nt.encoding.lower().replace("_", "-") == "gb18030"
    assert "半导体" in nt.text


def test_criterion3_bom_detected() -> None:
    raw = "\ufeff带 BOM 的文本".encode("utf-8-sig")
    text, encoding = decode_bytes(raw, "")
    assert encoding in {"utf-8-sig", "utf-8"}
    assert "带 BOM" in text


def test_criterion3_undecodable_bytes_fail_loudly() -> None:
    """不许用 errors='replace' 编造文本。

    注意测试数据的选择：`\\x81\\x82\\x83\\x84` 在 **gb18030 下是合法序列**，
    因此它能被成功解码属于正确行为。这里用两个编码都非法的字节。
    """
    with pytest.raises(NormalizeError):
        decode_bytes(b"\x80\x81", "text/plain; charset=utf-8")
    with pytest.raises(NormalizeError):
        decode_bytes(b"\xff\xff\xff\xff", "text/plain")


def test_criterion3_script_and_style_are_skipped() -> None:
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    assert "color: red" not in nt.text
    assert 'var x = "1' not in nt.text


def test_criterion3_html_without_content_type_is_sniffed() -> None:
    raw = HTML_SAMPLE.encode("utf-8")
    assert looks_like_html(HTML_SAMPLE)
    nt = normalize(raw, "")  # 无 Content-Type：必须靠嗅探识别为 HTML
    assert "<p>" not in nt.text
    assert "证据锚点" in nt.text
    assert nt.source_kind == "html"


def test_criterion3_unsupported_format_fails_loudly() -> None:
    with pytest.raises(NotImplementedError):
        normalize(b"%PDF-1.4 fake pdf", "application/pdf")
    with pytest.raises(NotImplementedError):
        normalize(b"PK\x03\x04 fake zip", "application/octet-stream")


# ---------------------------------------------------------------------------
# 判据 4：块是派生物、可重建
# ---------------------------------------------------------------------------


def test_criterion4_blocks_are_derivable_and_reproducible() -> None:
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    assert nt.blocks, "HTML 含块级标签，应产出派生块"
    again = derive_blocks(nt)
    assert [b.block_id for b in again] == [b.block_id for b in nt.blocks]
    assert [b.normalized_start for b in again] == [b.normalized_start for b in nt.blocks]


def test_criterion4_attach_blocks_returns_new_record() -> None:
    nt = normalize_html(HTML_SAMPLE, encoding="utf-8")
    assert nt.blocks == ()
    with_blocks = attach_blocks(nt)
    assert with_blocks is not nt
    assert with_blocks.blocks
    assert with_blocks.text == nt.text


def test_criterion4_blocks_are_marked_derived_not_anchors() -> None:
    """块是派生物：可重建，且不得被当作人工产物（Confirmed）的锚点。"""
    import atlas.normalize.blocks as blocks_mod

    doc = blocks_mod.__doc__ or ""
    assert "派生" in doc and "不得" in doc
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    # 块定位只携带归一化区间，不携带原文真值锚点
    for block in nt.blocks:
        assert block.normalized_start is not None
        assert not hasattr(block, "raw_sha256")


# ---------------------------------------------------------------------------
# 判据 5：与 T-002 的证据锚点打通
# ---------------------------------------------------------------------------


def test_criterion5_build_anchor_from_normalized_text() -> None:
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    quote = "字符区间"
    status, anchor, derived = build_anchor(
        raw_id="raw_demo",
        raw_sha256="a" * 64,
        normalized_text=nt.text,
        quote=quote,
        to_raw_offset=nt.to_raw_offset,
    )
    assert status is VerificationStatus.VERIFIED
    assert anchor is not None
    span = html.unescape(nt.raw_text[anchor.char_start : anchor.char_end])
    assert quote[0] in span and quote[-1] in span, f"锚点切片未覆盖 quote：{span!r}"
    assert derived is not None and derived.normalized_start is not None


def test_criterion5_build_anchor_reports_failed_for_absent_quote() -> None:
    nt = normalize(HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8")
    status, anchor, derived = build_anchor(
        raw_id="raw_demo",
        raw_sha256="a" * 64,
        normalized_text=nt.text,
        quote="这段引用并不存在于原文之中",
        to_raw_offset=nt.to_raw_offset,
    )
    assert status is VerificationStatus.FAILED
    assert anchor is None and derived is None


# ---------------------------------------------------------------------------
# 判据 6：零新增依赖
# ---------------------------------------------------------------------------


def test_criterion6_no_third_party_encoding_probe() -> None:
    """检查 **import 语句**，而不是文本里是否出现过某个词。

    （直接搜 "chardet" 会命中文档字符串里那句"**不**引入 chardet"。）
    """
    import re

    import atlas.normalize.text as mod

    source = open(mod.__file__, encoding="utf-8").read()
    import_lines = [
        line for line in source.splitlines() if re.match(r"^\s*(import|from)\s", line)
    ]
    for banned in ("chardet", "charset_normalizer", "cchardet"):
        for line in import_lines:
            assert banned not in line, f"不应 import {banned}：{line.strip()}"
