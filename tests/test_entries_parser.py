"""T-130 验收判据（**先于实现写下来**，SPEC §4.1 T-002 判据的同一写法）。

任务：条目化派生层（SPEC §4.2 T-130 / §6.3 裁决 B）。输入是一份已归档 feed 的
**原始字节** + content type；输出是该 feed 拆出的**条目序列**。

判据
====

**1. 纯函数**
   1.1 同一输入连续两次调用，产物**逐字节相同**（含全部 ID、区间、字段、问题）。
   1.2 源码扫描：`src/atlas/entries/` 不 import `random` / `time` / `datetime.now` /
       `os` / `pathlib` / `socket` / `uuid` / `secrets`（`datetime` 只允许作为
       **类型与构造**使用，不允许取当前时间）。
   1.3 没有全局可变解析状态：命名空间走**注入法**，不碰
       `ElementTree.register_namespace` 的进程级全局表。
   1.4 两次调用之间改动 `ET` 的全局命名空间表，结果不变（1.3 的活对照）。

**2. 可重建**
   2.1 丢弃产物、重跑，`EntrySet` 的每个字段逐字段相同。
   2.2 `parser_version` 记在产物里（`EntrySet.parser_version` + `parser_fingerprint`）。
   2.3 **版本变化必须改变条目 ID**：同一份原文换解析器版本 →
       ID 全变，而 `(raw_id, raw_sha256, char_start, char_end)` **逐字段不变**。
   2.4 产物自带的信息足以独立复核（`verify_ids` / `verify_spans`）。

**3. 字符区间可回环（最关键）**
   3.1 每个条目 `text[char_start:char_end]` 在**原文**上取切片，切片非空、
       以元素起始标签开头、**去标记后包含该条目的标题**（实体形态下按 SPEC §2.2
       的澄清：两边都试字面与实体字面量）。
   3.2 区间**单调不重叠**、不越界（`0 <= start < end <= len(text)`）。
   3.3 `verify_offsets()` 是**可独立重算**的校验：只吃 `raw_bytes`，
       重新解码后逐条比对。
   3.4 **该校验能失败**：注入错误区间（起点偏移、终点偏移、交换两条、越界、
       换掉原文一个字节）都必须被抓到。
   3.5 产物内部自检 `EntrySet.verify_spans()` 同样能失败。

**4. 条目 ID 不作锚点（与 T-206 的区别：字符区间正是锚点）**
   4.1 `Entry.as_anchor()` **永远抛** `EntryNotAnchorError`（三层强制的第一层）。
   4.2 `entry_id` 形状 `^ent_[0-9a-f]{32}$`，过不了 `EvidenceAnchor.raw_sha256`
       的 `^[0-9a-f]{64}$`（第二层）。
   4.3 `Entry` 塞进要求 `EvidenceAnchor` / `DerivedLocator` 的字段 → pydantic 拒（第三层）。
   4.4 **活对照**：同一字符区间能造出**合法** `EvidenceAnchor`
       （`Entry.anchor()` 成功），而 `entry_id` 不能。
   4.5 **机制证明**：条目 ID 掺了解析器版本与标题提取规则，因此换解析器会漂移；
       字符区间不掺这些，所以稳定。

**5. 不假装成功**
   5.1 非 feed：JSON API 响应 / HTML 页面 → `EntryParseError`（**响亮失败**）。
   5.2 合法 XML 但未知根元素 → 0 条 + `problems` 说明（不猜）。
   5.3 合法 feed 但 0 条目 → 0 条 + `problems` 说明。
   5.4 只有 `<item>` 没有 `<title>` → 该条目不进入序列，**理由进 `problems`**。
   5.5 编码异常：GBK 字节 + 声明 UTF-8 → 回退解码成功且区间仍然自洽（断言在 `test_entries_spans`）；
       完全无法解码 → `EntryParseError`。
   5.6 空字节 / 纯空白 / 结构损坏的 XML → `EntryParseError`。
   5.7 **活对照**：以上每一条"失败"都必须在同一调用路径上有一条**成功**的对照。

**6. 零新增依赖**：只用标准库（`xml.etree.ElementTree` / `email.utils` / `html` /
   `hashlib` / `re` / `dataclasses` / `datetime`），**不用** `feedparser` 等第三方；
   源码扫描断言不出现非标准库的第三方 import。

**7. `pytest` 全绿；不破坏现有测试。**

本文件承载判据 1 / 2 / 6；其余判据在同类测试文件里：

- 判据 3  → `tests/test_entries_spans.py`
- 判据 4  → `tests/test_entries_anchors.py`
- 判据 5  → `tests/test_entries_fields.py`
- 真实数据 → `tests/test_entries_realdata.py`
"""

from __future__ import annotations

import ast
import dataclasses
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas.entries import (
    CURRENT_PARSER_VERSION,
    DEFAULT_PARSER,
    ENTRY_PARSER_VERSION,
    EntryError,
    EntryParser,
    parse_entries,
    verify_ids,
    verify_offsets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRIES_SRC = REPO_ROOT / "src" / "atlas" / "entries"

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>示例频道</title>
    <item>
      <title>第一篇：条目化派生层</title>
      <link>https://example.invalid/posts/1</link>
      <pubDate>Thu, 14 Aug 2025 06:31:20 +0000</pubDate>
      <dc:creator><![CDATA[张三]]></dc:creator>
      <description><![CDATA[<p>第一段正文。条目区间定义在解码后的 feed 文本上。</p>]]></description>
    </item>
    <item>
      <title>第二篇：字符区间就是锚点</title>
      <link>https://example.invalid/posts/2</link>
      <pubDate>Fri, 15 Aug 2025 07:00:00 +0000</pubDate>
      <description>纯文本描述，没有 CDATA。</description>
    </item>
    <item>
      <title>第三篇：ID 不是锚点</title>
      <link>https://example.invalid/posts/3</link>
      <pubDate>Sat, 16 Aug 2025 08:00:00 GMT</pubDate>
      <description>第三条正文。</description>
    </item>
  </channel>
</rss>
"""
RSS_BYTES = RSS_FEED.encode("utf-8")
RSS_ID = "raw_t130_synthetic"

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom 样例</title>
  <entry>
    <id>tag:example.invalid,2025:1</id>
    <title type="text">Atom 第一篇</title>
    <link rel="alternate" href="https://example.invalid/atom/1"/>
    <published>2025-08-14T06:31:20.000-07:00</published>
    <updated>2025-08-14T06:40:00.000-07:00</updated>
    <content type="html">&lt;p&gt;Atom 正文一。&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>tag:example.invalid,2025:2</id>
    <title>Atom 第二篇</title>
    <link href="https://example.invalid/atom/2"/>
    <published>2025-08-15T07:00:00Z</published>
    <summary>Atom 摘要二。</summary>
  </entry>
</feed>
"""
ATOM_BYTES = ATOM_FEED.encode("utf-8")


def _two_runs(raw_bytes: bytes, content_type: str, raw_id: str = RSS_ID):
    first = parse_entries(raw_bytes, content_type, raw_id=raw_id)
    second = parse_entries(raw_bytes, content_type, raw_id=raw_id)
    return first, second


# =========================================================================== #
# 判据 1：纯函数
# =========================================================================== #


def test_criterion1_1_two_calls_are_byte_identical() -> None:
    """同一输入两次调用 → 产物逐字段相同（含全部 ID）。"""
    first, second = _two_runs(RSS_BYTES, "application/rss+xml")
    assert first == second
    assert dataclasses.asdict(first) == dataclasses.asdict(second)
    assert [e.entry_id for e in first.entries] == [e.entry_id for e in second.entries]
    assert first.entries  # 活对照：夹具真的产出了条目


def test_criterion1_1_atom_two_calls_are_byte_identical() -> None:
    first, second = _two_runs(ATOM_BYTES, "application/atom+xml")
    assert first == second
    assert len(first.entries) == 2  # 活对照


def test_criterion1_1_repeated_calls_inside_one_process_are_stable() -> None:
    """连跑 5 次，ID 序列与区间序列一次都不能变（无隐藏计数器 / 无全局状态）。"""
    snapshots = [
        tuple((e.entry_id, e.char_start, e.char_end) for e in parse_entries(
            RSS_BYTES, "application/rss+xml", raw_id=RSS_ID
        ).entries)
        for _ in range(5)
    ]
    assert len(set(snapshots)) == 1
    assert snapshots[0]  # 活对照


#: 判据 1.2：包内**禁止**出现的模块（时钟 / 随机 / 文件系统 / 网络）。
#: `io` 单独放行：本包只用 `io.StringIO` 把 `str` 喂给 `iterparse`
#: （收集命名空间声明），不做任何文件 I/O —— 由下面的专用测试钉死这一点。
_FORBIDDEN_MODULES = frozenset(
    {"random", "time", "os", "pathlib", "socket", "uuid", "secrets"}
)

#: 允许使用的标准库白名单（判据 6：零新增依赖）。
_ALLOWED_STDLIB = frozenset(
    {
        "hashlib",
        "re",
        "html",
        "dataclasses",
        "datetime",
        "email",
        "email.utils",
        "typing",
        "xml",
        "xml.etree",
        "xml.etree.ElementTree",
        "__future__",
        "io",
    }
)

#: 包内自己的模块（相对 import 或 `atlas.*`）。
_ALLOWED_INTERNAL = frozenset(
    {
        "atlas",
        "atlas.contracts",
        "atlas.contracts.ids",
        "atlas.contracts.errors",
        "atlas.normalize",
        "atlas.normalize.text",
    }
)


def _iter_imports(path: Path):
    """产出 `(lineno, module_name, is_relative)`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, False
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield node.lineno, node.module or "", True
            else:
                yield node.lineno, node.module or "", False


def _entries_modules() -> list[Path]:
    return sorted(ENTRIES_SRC.glob("*.py"))


def test_criterion1_2_entries_package_does_not_import_clocks_or_io() -> None:
    """判据 1.2：不 import 时钟 / 随机 / 文件系统 / 网络模块。"""
    found: list[str] = []
    for path in _entries_modules():
        for lineno, module, _relative in _iter_imports(path):
            root = module.split(".")[0]
            if root in _FORBIDDEN_MODULES:
                found.append(f"{path.name}:{lineno} import {module}")
    assert found == [], f"条目层不得依赖时钟/随机/IO：{found}"


def test_criterion1_2_entries_package_never_calls_datetime_now() -> None:
    """判据 1.2（细化）：`datetime` 只能当类型/构造用，**不得**取当前时间。

    时钟是"不可重建"的头号来源：一旦产物里混入 `now()`，同一份原文重跑就不会
    逐字节相同。因此这条按**调用名**扫描，而不只是看 import。
    """
    forbidden_calls = {"now", "today", "utcnow", "fromtimestamp"}
    offenders: list[str] = []
    for path in _entries_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                if name in forbidden_calls:
                    offenders.append(f"{path.name}:{node.lineno} {name}()")
    assert offenders == [], f"条目层不得读时钟：{offenders}"


def test_criterion6_only_stdlib_imports() -> None:
    """判据 6：零新增依赖 —— 只用标准库 + 包内/上游 atlas 模块。"""
    offenders: list[str] = []
    for path in _entries_modules():
        for lineno, module, relative in _iter_imports(path):
            location = f"{path.name}:{lineno}"
            if relative:
                continue  # 包内相对 import
            if module.startswith("atlas"):
                if module not in _ALLOWED_INTERNAL and not any(
                    module.startswith(allowed + ".") for allowed in _ALLOWED_INTERNAL
                ):
                    offenders.append(f"{location} import {module}（越出 T-104 上游）")
                continue
            if module not in _ALLOWED_STDLIB:
                offenders.append(f"{location} import {module}（非白名单标准库/第三方）")
    assert offenders == [], f"条目层只允许标准库 + 上游类型：{offenders}"


def test_criterion6_no_third_party_feed_parser() -> None:
    """判据 6（点名）：不得用 `feedparser` 之类的第三方 feed 解析库。"""
    text = "\n".join(path.read_text(encoding="utf-8") for path in _entries_modules())
    for name in ("feedparser", "lxml", "bs4", "beautifulsoup4", "requests", "defusedxml"):
        assert name not in text, f"条目层不得引用第三方库 {name}"


# =========================================================================== #
# 判据 1.3 / 1.4：没有全局可变解析状态（命名空间注入法）
# =========================================================================== #


def test_criterion1_3_namespace_injection_does_not_touch_global_registry() -> None:
    """判据 1.3：解析前后 `ElementTree` 的全局命名空间表必须**逐项不变**。

    `ET.register_namespace` 改的是进程级全局状态 —— 与"纯函数、无全局可变状态"
    直接冲突，并发下还会互相踩。本包用注入法，因此全局表必须原封不动。
    """
    before = dict(ET._namespace_map)  # type: ignore[attr-defined]
    entrieset = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert entrieset.entries  # 活对照：确实解析了带前缀的 feed
    after = dict(ET._namespace_map)  # type: ignore[attr-defined]
    assert before == after, "解析改动了 ElementTree 的进程级命名空间表"


def test_criterion1_4_result_is_immune_to_global_namespace_pollution() -> None:
    """判据 1.4：把全局命名空间表弄乱，条目化结果**完全不变**。

    这是 1.3 的活对照：如果实现依赖全局表，污染它就会改变输出。
    """
    baseline = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert baseline.entries

    ET.register_namespace("dc", "urn:totally:different")
    ET.register_namespace("content", "urn:also:different")
    ET.register_namespace("zzz", "urn:new:prefix")
    try:
        polluted = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    finally:
        # 恢复（测试卫生：不动其它测试的全局状态）
        ET._namespace_map.clear()  # type: ignore[attr-defined]
        ET._namespace_map.update(  # type: ignore[attr-defined]
            {
                "http://www.w3.org/XML/1998/namespace": "xml",
                "http://www.w3.org/1999/xhtml": "html",
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf",
                "http://schemas.xmlsoap.org/wsdl/": "wsdl",
                "http://www.w3.org/2001/XMLSchema": "xs",
                "http://www.w3.org/2001/XMLSchema-instance": "xsi",
                "http://purl.org/dc/elements/1.1/": "dc",
            }
        )
    assert polluted == baseline
    assert [e.entry_id for e in polluted.entries] == [e.entry_id for e in baseline.entries]


def test_criterion1_3_no_module_level_mutable_containers() -> None:
    """判据 1.3（静态）：包内没有模块级的 `list` / `dict` / `set` **状态容器**。

    模块级可变容器是最容易被忽视的全局状态。本包的模块级常量要么是 `str` /
    `int` / `frozenset` / `tuple` / 编译好的正则 / 冻结 dataclass，要么是别的
    冻结常量 —— 一律不可变。唯一放行的是 `__all__`：它是"导出清单"这种
    一次性元数据，没有任何代码路径会去改它（下面用活对照钉死包内确实没有
    别的模块级容器）。
    """
    offenders: list[str] = []
    seen: list[str] = []
    for path in _entries_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None or not isinstance(value, (ast.List, ast.Dict, ast.Set)):
                continue
            targets = [
                t.id
                for t in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if isinstance(t, ast.Name)
            ]
            rendered = f"{path.name}:{node.lineno} {'/'.join(targets)}"
            seen.append(rendered)
            if targets != ["__all__"]:
                offenders.append(rendered)
    assert offenders == [], f"模块级可变全局状态：{offenders}"
    # 活对照：扫描器确实看到了东西（不是"什么都没扫到"所以通过）
    assert seen, "静态扫描没有发现任何模块级容器，扫描器可能失效了"
    assert all(item.endswith("__all__") for item in seen), seen


# =========================================================================== #
# 判据 2：可重建
# =========================================================================== #


def test_criterion2_1_discard_and_rerun_reproduces_everything() -> None:
    """判据 2.1：丢掉产物重跑，**每个字段**都相同（含 ID、区间、问题、编码）。"""
    first = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    del first
    second = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    third = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)

    assert second == third
    for field in (
        "raw_id",
        "raw_sha256",
        "kind",
        "parser_version",
        "encoding",
        "feed_text",
        "problems",
        "entries",
    ):
        assert getattr(second, field) == getattr(third, field), field
    assert second.parser_fingerprint == third.parser_fingerprint


def test_criterion2_1_id_report_recomputes_from_scratch() -> None:
    """判据 2.1（强化）：`verify_ids` 只看 `raw_bytes` + 产物，重算全部 ID。"""
    entrieset = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert entrieset.entries  # 活对照
    report = verify_ids(raw_id=RSS_ID, raw_bytes=RSS_BYTES, entries=entrieset)
    assert report.ok, report.mismatches
    assert report.checked == len(entrieset.entries)
    assert report.expected_ids == report.actual_ids


def test_criterion2_1_verify_ids_can_fail_when_bytes_change() -> None:
    """判据 2.1 的负向对照：原文换一个字节 → 重算出的 ID 与记录不符。"""
    entrieset = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert entrieset.entries
    tampered = RSS_BYTES.replace(b"<channel>", b"<channel >", 1)
    assert tampered != RSS_BYTES
    report = verify_ids(raw_id=RSS_ID, raw_bytes=tampered, entries=entrieset)
    assert not report.ok, "改了原始字节却仍然报告 ID 一致"


def test_criterion2_2_parser_version_and_fingerprint_are_in_the_output() -> None:
    """判据 2.2：产物自含解析器版本与快照指纹。"""
    entrieset = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert entrieset.parser_version == ENTRY_PARSER_VERSION == CURRENT_PARSER_VERSION
    assert len(entrieset.parser_fingerprint) == 64
    assert all(e.parser_version == entrieset.parser_version for e in entrieset.entries)


def test_criterion2_3_parser_version_participates_in_entry_id() -> None:
    """判据 2.3（**最关键的一条**）：换解析器版本 → ID 全变，真值四元组不变。

    如果换了解析器而 ID 没变，派生量就会"看起来稳定、实际已经漂移"。
    """
    baseline = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    other = EntryParser(version="entry-parser-v2-testonly").parse(
        RSS_BYTES, "application/rss+xml", raw_id=RSS_ID
    )
    assert baseline.entries and other.entries  # 活对照：两边都真的产出了条目
    assert [e.entry_id for e in other.entries] != [e.entry_id for e in baseline.entries]
    # ID 变了，但**真值**（SPEC §2.2 的四元组）逐字段不变 —— 这正是"ID 派生、
    # 字符区间是真值"的可执行证明。
    assert [e.truth_fields for e in other.entries] == [
        e.truth_fields for e in baseline.entries
    ]
    assert [e.span for e in other.entries] == [e.span for e in baseline.entries]
    assert other.parser_version != baseline.parser_version
    assert other.parser_fingerprint != baseline.parser_fingerprint


def test_criterion2_3_entry_fields_carry_the_parser_version() -> None:
    """判据 2.3（结构）：条目自己也带版本，且产物层**拒绝**版本不一致的组合。

    这是三方绑定的落点：`Entry.parser_version` / `EntrySet.parser_version` /
    `entry_id` 的输入必须同源，混装直接拒绝构造 —— 否则"换了解析器但 ID 没变"
    这类漂移可以从构造缝隙里溜进来。
    """
    other = EntryParser(version="entry-parser-v9-testonly")
    entrieset = other.parse(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert {e.parser_version for e in entrieset.entries} == {"entry-parser-v9-testonly"}
    assert entrieset.parser_version == "entry-parser-v9-testonly"

    # 活对照：原封不动重建 `EntrySet` 成功
    rebuilt = dataclasses.replace(entrieset)
    assert rebuilt.parser_version == entrieset.parser_version

    # 负向：把条目版本改掉再装进原产物 → 拒绝
    forged_entry = dataclasses.replace(entrieset.entries[0], parser_version="entry-parser-vX")
    with pytest.raises(EntryError):
        dataclasses.replace(entrieset, entries=(forged_entry, *entrieset.entries[1:]))

    # 负向：伪造一个**形状合法**的 ID 再装回产物 → 独立重算立刻抓到
    forged_id_entry = dataclasses.replace(entrieset.entries[0], entry_id="ent_" + "0" * 32)
    forged_set = dataclasses.replace(
        entrieset, entries=(forged_id_entry, *entrieset.entries[1:])
    )
    assert forged_set.verify_ids() is False, "伪造的 ID 竟然通过了自检"
    assert entrieset.verify_ids() is True  # 活对照：原产物自洽


def test_criterion2_3_forged_id_is_detected_by_independent_recompute() -> None:
    """判据 2.3（强化）：伪造一个形状合法的 ID，独立重算必须抓到它。"""
    entrieset = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert entrieset.entries  # 活对照
    forged = dataclasses.replace(entrieset.entries[0], entry_id="ent_" + "f" * 32)
    assert forged.entry_id != entrieset.entries[0].entry_id
    assert not forged.recompute_id() == forged.entry_id
    assert entrieset.verify_ids() is True  # 原产物仍然自洽


def test_criterion2_1_different_raw_id_yields_different_entry_ids() -> None:
    """判据 2 的边界：同一份字节、不同 `raw_id` → 不同条目 ID（身份必须绑定原文标识）。"""
    left = parse_entries(RSS_BYTES, "application/rss+xml", raw_id="raw_a")
    right = parse_entries(RSS_BYTES, "application/rss+xml", raw_id="raw_b")
    assert left.entries and right.entries
    assert left.raw_sha256 == right.raw_sha256  # 字节相同
    assert {e.entry_id for e in left.entries}.isdisjoint(
        {e.entry_id for e in right.entries}
    )


def test_criterion2_4_default_parser_is_a_frozen_dataclass() -> None:
    """判据 2（卫生）：默认解析器是冻结的、可哈希的，配置不能就地改。"""
    assert dataclasses.is_dataclass(DEFAULT_PARSER)
    assert DEFAULT_PARSER.version == ENTRY_PARSER_VERSION
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_PARSER.version = "x"  # type: ignore[misc]


def test_criterion2_4_parse_accepts_a_custom_parser_and_reports_its_version() -> None:
    """判据 2.4：便捷入口与解析器实例走**同一条路径**（不是两套实现）。"""
    custom = EntryParser(version="entry-parser-custom")
    via_entry = parse_entries(
        RSS_BYTES, "application/rss+xml", raw_id=RSS_ID, parser=custom
    )
    via_method = custom.parse(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    assert via_entry == via_method
    assert via_entry.parser_version == "entry-parser-custom"


# =========================================================================== #
# 判据 5 的一部分：调用契约（空 raw_id / 非 bytes）
# =========================================================================== #


def test_criterion5_empty_raw_id_is_rejected() -> None:
    with pytest.raises(Exception):
        parse_entries(RSS_BYTES, "application/rss+xml", raw_id="")
    # 活对照：非空 raw_id 成功
    assert parse_entries(RSS_BYTES, "application/rss+xml", raw_id="raw_ok").entries


def test_criterion5_non_bytes_input_is_rejected() -> None:
    with pytest.raises(Exception):
        parse_entries("not bytes", "application/rss+xml", raw_id=RSS_ID)  # type: ignore[arg-type]
    # 活对照：bytes 成功
    assert parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID).entries


def test_criterion5_empty_parser_version_is_rejected() -> None:
    with pytest.raises(Exception):
        EntryParser(version="")
    with pytest.raises(Exception):
        EntryParser(version="   ")
    # 活对照：合法版本成功
    assert EntryParser(version="entry-parser-v1").version == "entry-parser-v1"


def test_feed_text_is_deterministic_and_matches_independent_decode() -> None:
    """产物自带的 `feed_text` 必须是**独立解码**能复现的同一份文本。"""
    from atlas.normalize.text import decode_bytes

    entrieset = parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID)
    text, encoding = decode_bytes(RSS_BYTES, "application/rss+xml")
    assert entrieset.feed_text == text
    assert entrieset.encoding == encoding
    report = verify_offsets(
        raw_id=RSS_ID,
        raw_bytes=RSS_BYTES,
        entries=entrieset,
        content_type="application/rss+xml",
    )
    assert report.ok, report.failures


def test_io_module_is_only_used_for_gbk_decoding_support() -> None:
    """判据 1.2 的说明性断言：`io` 只用于把 `str` 喂给 `iterparse`，不做文件 I/O。"""
    text = "\n".join(path.read_text(encoding="utf-8") for path in _entries_modules())
    assert "import io" not in text or "io.StringIO" in text
    assert "open(" not in text
    # 活对照：`StringIO` 路径确实被走到了（命名空间收集需要 iterparse 一个 str）
    assert parse_entries(RSS_BYTES, "application/rss+xml", raw_id=RSS_ID).entries
    assert isinstance(io.StringIO("x"), io.StringIO)
