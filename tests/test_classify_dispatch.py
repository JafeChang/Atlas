"""T-105 判据 9 / 14：**异质输入的分流**与跨包 import 纪律。

判据全文（**先定义后实现**的唯一出处）见 `tests/_t105_criteria.py`，本文件覆盖：

- **判据 9**：raw 是异质的（SPEC §6.3 的直接结论），必须先判种类再决定分类单元。
  | 输入 | 分流 | 单元 |
  |---|---|---|
  | 是 feed | `FEED` | 条目（走 `parse_entries`） |
  | 已是逐篇文章 | `ARTICLE` | 整篇 |
  | 既不是 feed 也没有可分类内容 | `SKIPPED` | 0 个 + **非空理由码** |
- **判据 14**：T-105 的模块**不** import 兄弟包（AST 扫描钉死）。
- **判据 2 的存储侧**：没有一个"模型给出的坐标"入口 —— 区间只来自单元。
- **判据 8**：新代码只用 stdlib + pydantic + 本项目自己的包。

这一组用**合成字节**（不依赖 `data/`、不依赖边车、不联网），因此门禁里必跑。
真实数据上的同一组判据在 `tests/test_classify_realdata.py`（会自跳过）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from atlas.cognition import (
    ClassificationError,
    DocumentKind,
    DocumentPlan,
    LabelSpace,
    SkipReason,
    Unit,
    classify_document,
    plan_batches,
    reduce_text,
    unit_id_for_article,
)
from tests._t105_criteria import CRITERIA

REPO_ROOT = Path(__file__).resolve().parents[1]
T105_MODULES = ("classify.py", "propose.py", "store.py")

FEED_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Transformer scaling laws</title><link>https://example.com/a</link>
<description><![CDATA[<p>We study transformer scaling for language models.</p>]]></description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
<item><title>Image segmentation</title><link>https://example.com/b</link>
<description><![CDATA[<p>A new approach to semantic image segmentation.</p>]]></description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
</channel></rss>
"""

EMPTY_FEED_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title></channel></rss>
"""

ATOM_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>Atom entry about speech models</title>
<link href="https://example.com/c"/>
<content type="html">&lt;p&gt;We present a speech recognition model.&lt;/p&gt;</content>
<updated>2024-01-01T00:00:00Z</updated></entry>
</feed>
"""

ARTICLE_BYTES = (
    "Winter Release Connects Mechanical and Electrical Design Data to PLM\n"
    "\n"
    "The release adds an integration between the two data models.\n"
).encode("utf-8")

HTML_BYTES = (
    b"<!doctype html><html lang=\"en\"><head><title>x</title></head>"
    b"<body>error page</body></html>"
)

JSON_BYTES = b'{"exhaustive":{"nbHits":false},"hits":[{"title":"a story"}]}'


# =========================================================================== #
# 判据 9：分流三选一，且"跳过"必须带理由
# =========================================================================== #


def test_criterion_9_feed_is_split_into_entries() -> None:
    """feed → 条目（**条目是分类单元**），单元 ID 就是 T-130 的 `entry_id`。"""
    plan = classify_document(FEED_BYTES, raw_id="raw_feed", endpoint="https://e/feed")
    assert plan.kind is DocumentKind.FEED
    assert plan.skip_reason is None
    assert plan.unit_count == 2
    assert [unit.kind for unit in plan.units] == ["entry", "entry"]
    assert all(unit.unit_id.startswith("ent_") for unit in plan.units)
    assert [unit.title for unit in plan.units] == [
        "Transformer scaling laws",
        "Image segmentation",
    ]
    # 真值区间来自 T-130（**不是**本层算的，也不是模型给的）
    assert all(0 <= unit.char_start < unit.char_end for unit in plan.units)
    assert all(unit.raw_sha256 == plan.raw_sha256 for unit in plan.units)
    # 单元文本里带链接（那是可被模型逐字引用的真实内容）
    assert "https://example.com/a" in plan.units[0].text


def test_criterion_9_atom_feed_is_also_split_into_entries() -> None:
    """活对照：**另一种** feed 形态（Atom）也走同一条路 —— 分流判据是结构，不是扩展名。"""
    plan = classify_document(ATOM_BYTES, raw_id="raw_atom")
    assert plan.kind is DocumentKind.FEED
    assert plan.unit_count == 1
    assert plan.units[0].unit_id.startswith("ent_")
    assert "speech recognition model" in plan.units[0].text


def test_criterion_9_article_shaped_input_is_one_whole_document_unit() -> None:
    """**已是逐篇文章**的输入整篇成为一个分类单元（不喂 feed 解析器）。

    活对照：同一份字节喂 `parse_entries` 会**响亮失败**（`EntryParseError`），
    而本层把它分流成 `ARTICLE` —— 两条路的结果不同，证明分流器不是恒等函数。
    """
    from atlas.entries import EntryParseError, parse_entries

    with pytest.raises(EntryParseError):
        parse_entries(ARTICLE_BYTES, "", raw_id="raw_article")

    plan = classify_document(ARTICLE_BYTES, raw_id="raw_article")
    assert plan.kind is DocumentKind.ARTICLE
    assert plan.skip_reason is None
    assert plan.unit_count == 1
    unit = plan.units[0]
    assert unit.kind == "article"
    assert unit.unit_id.startswith("art_")
    assert unit.char_start == 0
    # 区间口径 = `text.strip()`（与单元文本同一份参照系；末尾换行不属于内容）
    assert unit.char_end == len(ARTICLE_BYTES.decode("utf-8").strip())
    # 标题只出现一次（整篇归约的结果本来就以标题开头）
    assert unit.text.count("Winter Release") == 1
    assert "integration between the two data models" in unit.text


def test_criterion_9_html_and_json_are_skipped_with_reasons() -> None:
    """既不是 feed 也没有可分类正文 ⇒ **跳过 + 理由码**，绝不假装成功。"""
    html = classify_document(HTML_BYTES, raw_id="raw_html", endpoint="https://e/feed/")
    assert html.kind is DocumentKind.SKIPPED
    assert html.units == ()
    assert html.skip_reason is SkipReason.UNSUPPORTED_CONTENT_HTML
    assert "HTML" in html.detail
    # detail 里保留了**原始**的条目化失败理由（EntryParseError 没有被吞掉）
    assert "不是 feed" in html.detail

    js = classify_document(JSON_BYTES, raw_id="raw_json")
    assert js.kind is DocumentKind.SKIPPED
    assert js.skip_reason is SkipReason.UNSUPPORTED_CONTENT_JSON
    assert "JSON" in js.detail


def test_criterion_9_empty_feed_is_skipped_but_not_an_error() -> None:
    """合法 feed 但 0 条目 ⇒ 跳过（`empty_feed`）。**空 feed 是事实，不是错误。**"""
    plan = classify_document(EMPTY_FEED_BYTES, raw_id="raw_empty")
    assert plan.kind is DocumentKind.SKIPPED
    assert plan.skip_reason is SkipReason.EMPTY_FEED
    assert plan.units == ()


def test_criterion_9_skipped_plan_must_carry_a_reason() -> None:
    """**否定性断言 + 活对照**：跳过必须带理由码与可读理由，否则构造期就失败。

    活对照：同一构造路径在**合法**输入上必须成功（否则"抛错"可能只是签名不对）。
    """
    # 活对照：非跳过的计划构造成功
    ok = DocumentPlan(
        raw_id="raw_1",
        raw_sha256="a" * 64,
        kind=DocumentKind.ARTICLE,
        units=(
            Unit(
                unit_id=unit_id_for_article("raw_1", "a" * 64, "hello"),
                raw_id="raw_1",
                raw_sha256="a" * 64,
                char_start=0,
                char_end=5,
                kind="article",
                title="hello",
                text="hello",
            ),
        ),
        detail="ok",
    )
    assert ok.unit_count == 1

    with pytest.raises(ClassificationError):
        DocumentPlan(
            raw_id="raw_1",
            raw_sha256="a" * 64,
            kind=DocumentKind.SKIPPED,
            units=(),
            skip_reason=None,
            detail="",
        )


def test_criterion_9_skipped_plan_may_not_carry_units() -> None:
    """跳过却带单元 = 语义自相矛盾 ⇒ 构造期拒绝（活对照见上一个测试）。"""
    unit = Unit(
        unit_id=unit_id_for_article("raw_1", "a" * 64, "hello"),
        raw_id="raw_1",
        raw_sha256="a" * 64,
        char_start=0,
        char_end=5,
        kind="article",
        title="hello",
        text="hello",
    )
    with pytest.raises(ClassificationError):
        DocumentPlan(
            raw_id="raw_1",
            raw_sha256="a" * 64,
            kind=DocumentKind.SKIPPED,
            units=(unit,),
            skip_reason=SkipReason.EMPTY_FEED,
            detail="contradiction",
        )


def test_criterion_9_classification_is_reproducible() -> None:
    """同输入 → 同产物（含全部单元 ID），逐字段相同（SPEC §3 可重算）。"""
    first = classify_document(FEED_BYTES, raw_id="raw_feed")
    second = classify_document(FEED_BYTES, raw_id="raw_feed")
    assert first == second
    assert [u.unit_id for u in first.units] == [u.unit_id for u in second.units]
    assert [u.unit_digest for u in first.units] == [u.unit_digest for u in second.units]


def test_criterion_9_unit_text_change_changes_unit_digest() -> None:
    """**幂等键必须跟着文本变**：换归约规则 / 换正文 ⇒ `unit_digest` 必变。"""
    text, _ = reduce_text("<p>alpha beta</p>")
    assert text == "alpha beta"
    other, _ = reduce_text("<p>alpha gamma</p>")
    r1 = unit_id_for_article("raw", "b" * 64, text)
    r2 = unit_id_for_article("raw", "b" * 64, other)
    assert r1 != r2


# =========================================================================== #
# 判据 1 的分流侧：标签空间必须被注入
# =========================================================================== #


def test_criterion_1_empty_label_space_is_refused_loudly() -> None:
    """忘记注入标签空间 ⇒ **响亮失败**，绝不静默产出空标签（SPEC §2.5 的缝隙）。

    活对照：同一构造路径对**非空**标签空间必须成功。
    """
    good = LabelSpace.of(["machine-learning"], config_version="cfg/1")
    assert good.labels == ("machine-learning",)

    with pytest.raises(ClassificationError) as excinfo:
        LabelSpace.of([], config_version="cfg/1")
    assert "标签空间为空" in str(excinfo.value)
    assert "SPEC §2.5" in str(excinfo.value)


def test_criterion_1_label_space_needs_config_version() -> None:
    """`config_version` 是版本三元组的一部分（SPEC §3），不得为空。"""
    with pytest.raises(ClassificationError):
        LabelSpace.of(["ai"], config_version="")


def test_criterion_1_label_space_fingerprint_is_order_insensitive() -> None:
    """标签空间指纹只由**集合**决定（注入顺序不该改变幂等键）。"""
    a = LabelSpace.of(["ai", "semis"], config_version="cfg/1")
    b = LabelSpace.of(["semis", "ai"], config_version="cfg/1")
    assert a.fingerprint == b.fingerprint
    c = LabelSpace.of(["ai"], config_version="cfg/1")
    assert c.fingerprint != a.fingerprint


# =========================================================================== #
# 判据 14：跨包 import 纪律（AST 扫描，不是靠约定）
# =========================================================================== #

#: T-105 在 DAG 里的**允许**上游（SPEC §4.0 + §4.5 的边 T-002→T-105 / T-104→T-105 /
#: T-003→T-105，以及 T-130 提供的条目层）。
ALLOWED_ATLAS_PREFIXES = (
    "atlas.contracts",
    "atlas.entries",
    "atlas.normalize",
    "atlas.cognition",
)

#: 明确禁止的兄弟包（本任务不得 import 它们的**任何**子模块）。
FORBIDDEN_ATLAS_PREFIXES = (
    "atlas.registry",
    "atlas.labels",
    "atlas.search",
    "atlas.feed",
    "atlas.compose",
    "atlas.webui",
    "atlas.chunk",
    "atlas.migrate",
    "atlas.catalog",
    "atlas.collect",
    "atlas.archive",
    "atlas.evidence",
    "atlas.runner",
    "atlas.collectors",
    "atlas.core",
    "atlas.models",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 相对 import：包内模块（合法）
                continue
            if node.module:
                found.add(node.module)
    return found


@pytest.mark.parametrize("module", T105_MODULES)
def test_criterion_14_no_forbidden_cross_package_imports(module: str) -> None:
    """T-105 只允许 import 自己在 DAG 里的上游。

    活对照：允许清单里的 `atlas.entries` / `atlas.contracts` **确实**出现在 import 里
    —— 否则"没有禁止项"可能只是因为压根没 import 任何 atlas 包（空断言）。
    """
    path = REPO_ROOT / "src" / "atlas" / "cognition" / module
    imported = _imported_modules(path)
    atlas_modules = {name for name in imported if name.split(".")[0] == "atlas"}

    for name in sorted(atlas_modules):
        for forbidden in FORBIDDEN_ATLAS_PREFIXES:
            assert not (name == forbidden or name.startswith(forbidden + ".")), (
                f"{module} import 了禁止的兄弟包 {name}（SPEC §4.0 的跨包规则）"
            )
        assert any(
            name == allowed or name.startswith(allowed + ".")
            for allowed in ALLOWED_ATLAS_PREFIXES
        ), f"{module} import 了允许清单之外的 atlas 模块：{name}"


def test_criterion_14_allowed_upstream_imports_are_actually_used() -> None:
    """活对照：允许的上游**确实**被 import 了（证明上一条断言不是空转）。"""
    all_imported: set[str] = set()
    for module in T105_MODULES:
        all_imported |= _imported_modules(REPO_ROOT / "src" / "atlas" / "cognition" / module)
    assert "atlas.entries" in all_imported, "条目层是本任务的上游，必须真的被用到"
    assert any(name.startswith("atlas.normalize") for name in all_imported)
    assert any(name.startswith("atlas.contracts") for name in all_imported)


def test_criterion_8_no_third_party_imports_beyond_pydantic() -> None:
    """只用 stdlib + 已声明的 pydantic（零新增 Python 依赖）。"""
    stdlib = set(getattr(__import__("sys"), "stdlib_module_names", ()))
    third_party: set[str] = set()
    for module in T105_MODULES:
        for name in _imported_modules(REPO_ROOT / "src" / "atlas" / "cognition" / module):
            root = name.split(".")[0]
            if root == "atlas" or root in stdlib or root.startswith("_"):
                continue
            third_party.add(root)
    assert third_party <= {"pydantic"}, f"出现了未声明的第三方依赖：{sorted(third_party)}"


def test_criteria_module_is_the_single_source_of_truth() -> None:
    """判据只有一份出处（硬规则 3：不另建文档体系）。"""
    assert set(CRITERIA) == set(range(1, 16))
    assert all(text.strip() for text in CRITERIA.values())


# =========================================================================== #
# 批量计划（调用策略的纯函数部分；实际调用见 test_propose_calls.py）
# =========================================================================== #


def test_batch_plan_respects_both_limits() -> None:
    """两条上限**先到者生效**，且至少放一个单元（否则会死循环）。"""
    units = tuple(
        Unit(
            unit_id=unit_id_for_article("raw", "c" * 64, f"unit {index} " + "x" * 50),
            raw_id="raw",
            raw_sha256="c" * 64,
            char_start=index * 10,
            char_end=index * 10 + 9,
            kind="article",
            title=f"unit {index}",
            text=f"unit {index} " + "x" * 50,
        )
        for index in range(5)
    )
    by_count = plan_batches(units, max_units=2, max_chars=10_000)
    assert [len(group) for group in by_count] == [2, 2, 1]

    by_chars = plan_batches(units, max_units=100, max_chars=60)
    # 每个单元 59 字符：两个就超 60 ⇒ 一批一个
    assert [len(group) for group in by_chars] == [1, 1, 1, 1, 1]

    single = plan_batches(units[:1], max_units=100, max_chars=1)
    assert [len(group) for group in single] == [1], "超长单元必须独占一批，不能死循环"


def test_batch_plan_preserves_document_order_and_is_stable() -> None:
    """批次切分确定性、保序（可复算 ⇒ 幂等键稳定）。"""
    plan = classify_document(FEED_BYTES, raw_id="raw_feed")
    first = plan_batches(plan.units, max_units=1, max_chars=10_000)
    second = plan_batches(plan.units, max_units=1, max_chars=10_000)
    assert first == second
    starts = [group[0].char_start for group in first]
    assert starts == sorted(starts)
