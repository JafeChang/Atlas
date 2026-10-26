"""T-206 真实数据流证据（硬规则 1）：对**真实归档正文**跑 raw → normalize → chunk。

数据从哪来（**只读**）
----------------------

`data/` 里没有新结构的分块产物，存在的是旧系统**真实抓取**下来的 534 份 JSON：
`data/raw/<频道>/*.json`，正文在 `raw_content` 字段（SPEC §7.1 / §8.1 记录的那批
"唯一有真实产物佐证"的数据）。本测试取其中 `source_type == "rss_feed"` 且正文
≥ 500 字符的记录作为**真实 raw 字节**：

    字节 = raw_content.encode("utf-8")
    content_type = "text/plain; charset=utf-8"

（RSS 摘要正文是纯文本 + 实体字面量，不是 HTML 文档；HTML 路径已由
`tests/test_chunk_chunker.py` 的固定夹具覆盖，这里不重复。）

真实数字里有一个必须说明的落差：420 份可用 JSON 里旧系统**没有去重**——同一篇
文章被重复采集了多次（同 `source_url`、同正文、不同 UUID）。按 T-002 的内容寻址
`raw_id = f(channel, endpoint, content_sha256)` 收敛后是 **91 条真实文档**，本测试
就在这 91 条上断言（去重让"每条文档"与"每个 `raw_id`"一一对应，计数才有意义）。

本测试**只读** `data/raw/`：进入与退出时各算一次整树 sha256 并断言相等。

实体边界为什么是**显式判据**而不是 `except`
-------------------------------------------

真实 RSS 正文里的 HTML 实体是**字面量**（`&#8217;`）；纯文本路径按 SPEC §2.2 不做
实体解码，归一化文本里就是 `&#8217;` 这 7 个字符。于是当 quote 的端点恰好落在实体
字面量中间时（`…person&#82`），SPEC §2.2 的判据：

> **原文切片经实体解码后**包含该区间的首尾非空白字符

必然不成立 —— `html.unescape("…&#82") == "…R"`，末尾那个 `2` 在解码后消失了。
这是**真实数据特性**，不是分块缺陷。

因此本测试不写 `try/except`：先用**判据本身**算一遍（不抛异常的方式），
把样本分成两支，两支都断言，一个都不放过：

- 判据成立 → `verify_quote` 必须返回 `VERIFIED`，且锚点的原文切片与 quote 的
  原文切片逐字符相同（真值锚点由 quote 确定性重算，分块完全不参与）；
- 判据不成立（切片端点切开了一个实体字面量）→ `verify_quote` 必须**响亮拒绝**
  （`AnchorError`），而不是返回一个编造出来的坐标（硬规则 2）。

否定断言都带**活对照**（硬规则 4）：被拒绝的那一支里，同一篇文档、同一条调用路径
换一段合法 quote 必须 `VERIFIED`——证明拒绝来自输入（实体边界），不是签名/接线不对。

一旦某一支的结果与判据不符，测试立刻失败 —— 没有静默吞错的分支。

数据不在时跳过
--------------

`data/` **不进 git**（SPEC §8.1），所以干净 worktree 里没有它——此时本测试 `skip`
（而不是失败），真实证据由主工作区的那次运行给出。机制与 T-205 的
`tests/test_search_realdata.py` 相同。
"""

from __future__ import annotations

import hashlib
import html
import json
import statistics
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from atlas.chunk import (
    DEFAULT_POLICY,
    ChunkNotAnchorError,
    ChunkPolicy,
    ChunkSet,
    chunk,
    chunk_id_for,
    policy_fingerprint,
)
from atlas.contracts import AnchorError, EvidenceAnchor, VerificationStatus
from atlas.contracts.ids import content_sha256, raw_id_for
from atlas.evidence import verify_quote
from atlas.normalize import NormalizedText

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_RAW = REPO_ROOT / "data" / "raw"

#: 真实 raw 字节的内容类型（RSS 摘要正文 = 纯文本 + 实体字面量）。
CONTENT_TYPE = "text/plain; charset=utf-8"

#: 正文下限：过短的抓取产物不足以做分块证据（与 T-206 证据脚本同一门槛）。
MIN_CONTENT_CHARS = 500

#: 两个策略：默认（target=1000 / max=1600 / 无重叠）与一个小窗口策略（含重叠）。
DEFAULT_LABEL = "default(target=1000,max=1600,overlap=0)"
SMALL_LABEL = "small(target=200,max=320,overlap=40)"
POLICIES: Dict[str, ChunkPolicy] = {
    DEFAULT_LABEL: DEFAULT_POLICY,
    SMALL_LABEL: ChunkPolicy(target_chars=200, max_chars=320, overlap_chars=40),
}

pytestmark = pytest.mark.skipif(
    not (LEGACY_RAW.is_dir() and any(LEGACY_RAW.rglob("*.json"))),
    reason="本地真实归档数据缺失（data/ 不进 git，见 SPEC §8.1）",
)

RealRecord = Tuple[Path, str, bytes]


# --------------------------------------------------------------------------- #
# 读取真实数据（只读）
# --------------------------------------------------------------------------- #
def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


def _load_rss_records() -> List[RealRecord]:
    """归档里 `source_type == "rss_feed"` 的真实正文，按 `raw_id` 去重。

    返回 `(path, raw_id, content)`；`content` 为 `raw_content.encode("utf-8")`。
    `data/` 只读：这里只有 `read_bytes` / `rglob`。
    """
    records: Dict[str, RealRecord] = {}
    for path in sorted(LEGACY_RAW.rglob("*.json")):
        try:
            payload = json.loads(path.read_bytes().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("source_type") != "rss_feed":
            continue
        content = payload.get("raw_content")
        if not isinstance(content, str) or len(content.strip()) < MIN_CONTENT_CHARS:
            continue
        raw_bytes = content.encode("utf-8")
        channel_id = payload.get("source_id") or path.parent.name
        endpoint = payload.get("source_url") or f"file:{path.name}"
        raw_id = raw_id_for(channel_id, endpoint, content_sha256(raw_bytes))
        records.setdefault(raw_id, (path, raw_id, raw_bytes))
    return list(records.values())


def _count_rss_json_files() -> int:
    """可用（`rss_feed` 且正文够长）的 JSON 文件数——含重复采集，用于报告落差值。"""
    total = 0
    for path in LEGACY_RAW.rglob("*.json"):
        try:
            payload = json.loads(path.read_bytes().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("source_type") != "rss_feed":
            continue
        content = payload.get("raw_content")
        if isinstance(content, str) and len(content.strip()) >= MIN_CONTENT_CHARS:
            total += 1
    return total


def _chunkset_bytes(chunkset: ChunkSet) -> bytes:
    """分块集合的**规范字节序列**（重建比较用；文本按 utf-8 编码后逐字节可比）。"""
    parts = [chunkset.policy_fingerprint.encode("ascii")]
    for item in chunkset.chunks:
        parts.append(
            (
                f"{item.chunk_id}\x1f{item.raw_id}\x1f{item.index}\x1f{item.policy_version}"
                f"\x1f{item.normalized_start}\x1f{item.normalized_end}"
                f"\x1f{item.raw_start}\x1f{item.raw_end}\x1f"
            ).encode("utf-8")
        )
        parts.append(item.text.encode("utf-8"))
        parts.append(b"\x1e")
    return b"".join(parts)


def _seed_quote(chunkset: ChunkSet) -> str:
    """从最大分块里取一段 quote（沿用 T-206 证据脚本的取法，端点重新 strip）。

    `verify_quote` 内部按 `quote.strip()` 匹配，所以这里也 strip——否则占位空白会让
    "我算的原文切片"与"锚点的原文切片"错开一个字符。
    """
    candidate = max(chunkset.chunks, key=lambda item: len(item.text))
    quote = candidate.text.strip()
    quote = quote[10:70] if len(quote) > 80 else quote[:60]
    return quote.strip()


def _entity_safe_quote(normalized: NormalizedText, chunkset: ChunkSet) -> str | None:
    """同一篇真实文档里第一段**满足 SPEC §2.2 判据**的 quote（否定断言的活对照）。

    按分块长度降序找：quote 必须唯一（锚点取首个匹配），且其原文切片经实体解码后
    同时包含首尾字符。找不到返回 `None`（调用方必须响亮失败，不得静默放过）。
    """
    for candidate in sorted(chunkset.chunks, key=lambda item: -len(item.text)):
        quote = candidate.text.strip()
        quote = (quote[10:70] if len(quote) > 80 else quote[:60]).strip()
        if len(quote) < 8 or normalized.text.count(quote) != 1:
            continue
        position = normalized.text.index(quote)
        window = html.unescape(normalized.raw_slice(position, position + len(quote)))
        if quote[0] in window and quote[-1] in window:
            return quote
    return None


@pytest.fixture(scope="module")
def real_records() -> List[RealRecord]:
    """真实正文（按 `raw_id` 收敛后）只读一次。"""
    records = _load_rss_records()
    assert records, "真实归档里没有可用的 rss_feed 正文"
    return records


# --------------------------------------------------------------------------- #
# 证据
# --------------------------------------------------------------------------- #
def test_real_chunking_is_rebuildable_complete_and_self_verifying(
    real_records: List[RealRecord],
) -> None:
    """真实数据流：真实抓取正文 → normalize → chunk（两个策略）→ 重建/覆盖/ID/偏移自检。"""
    legacy_before = _tree_digest(LEGACY_RAW)

    counts: Dict[str, List[int]] = {name: [] for name in POLICIES}
    chars: Dict[str, int] = {name: 0 for name in POLICIES}
    total_chunks = {name: 0 for name in POLICIES}
    unstable = coverage_failures = id_failures = offset_failures = 0
    text_mismatches = over_max = anchor_misuse = 0

    for path, raw_id, raw_bytes in real_records:
        for name, policy in POLICIES.items():
            normalized, chunkset = chunk(raw_bytes, CONTENT_TYPE, raw_id=raw_id, policy=policy)

            # ① 可重建：丢弃结果后从 raw 重跑一遍，必须**逐字节**相同（含策略快照）
            again_normalized, again = chunk(
                raw_bytes, CONTENT_TYPE, raw_id=raw_id, policy=policy
            )
            if _chunkset_bytes(again) != _chunkset_bytes(chunkset):
                unstable += 1
                print(f"  !! 重建不一致：{path.name} / {name}")
            if again.policy != chunkset.policy or again_normalized.text != normalized.text:
                unstable += 1
                print(f"  !! 重建后策略/归一化文本不一致：{path.name} / {name}")

            # ② 覆盖完整：非空白字符必须全部被分块覆盖（无空隙）
            gaps = chunkset.uncovered_non_whitespace(normalized.text)
            if gaps:
                coverage_failures += 1
                print(f"  !! 未覆盖非空白：{path.name} / {name} → {gaps[:5]}")

            # ③ 逐块独立重算：chunk_id / 原文偏移 / 文本，并做长度上界检查
            for item in chunkset.chunks:
                expected_id = chunk_id_for(
                    raw_id=raw_id,
                    policy_version=item.policy_version,
                    normalized_start=item.normalized_start,
                    normalized_end=item.normalized_end,
                    text=item.text,
                )
                if expected_id != item.chunk_id:
                    id_failures += 1
                start, end = normalized.to_raw_offset.to_raw_range(
                    item.normalized_start, item.normalized_end
                )
                if (start, end) != (item.raw_start, item.raw_end):
                    offset_failures += 1
                if normalized.text[item.normalized_start : item.normalized_end] != item.text:
                    text_mismatches += 1
                if len(item.text) > policy.max_chars:
                    over_max += 1
                if item.locator.block_id != item.chunk_id:
                    id_failures += 1

            # ④ 产物自带的自检入口（判据 5.3 / 4.2）
            if not chunkset.verify_ids():
                id_failures += 1
            if not chunkset.verify_offsets(normalized):
                offset_failures += 1

            # ⑤ 分块是派生物：真实分块上调用 as_anchor() 必须响亮失败（SPEC §2.2 / §5 #7）。
            #    **活对照**（硬规则 4）：同一条真实分块映射出的 raw 字符区间**能**造出合法
            #    锚点 —— 因此拒绝来自"分块不是锚点"的语义，而不是参数/签名不对。
            head = chunkset.chunks[0]
            control_anchor = EvidenceAnchor.create(
                raw_id=raw_id,
                raw_sha256=content_sha256(raw_bytes),
                char_start=head.raw_start,
                char_end=head.raw_end,
            )
            assert control_anchor.raw_id == raw_id
            assert control_anchor.length == head.raw_end - head.raw_start
            with pytest.raises(ChunkNotAnchorError):
                head.as_anchor()
            anchor_misuse += 1

            counts[name].append(len(chunkset.chunks))
            chars[name] += normalized.text_length
            total_chunks[name] += len(chunkset.chunks)

    files = _count_rss_json_files()
    print(
        f"\n[T-206 真实数据] rss_feed JSON 文件 {files} 份 → 去重后 {len(real_records)} 条 raw_id"
        f"（旧系统未去重，按内容寻址收敛）"
    )
    for name in POLICIES:
        tally = counts[name]
        print(
            f"[T-206 真实数据] 策略 {name}：文档 {len(tally)} 条，归一化字符 {chars[name]}，"
            f"分块 {total_chunks[name]} 块，每条文档分块数 min/中位/max = "
            f"{min(tally)}/{int(statistics.median(tally))}/{max(tally)}，"
            f"平均分块长度 {chars[name] / total_chunks[name]:.1f} 字符"
        )
    expected_anchor_misuse = len(real_records) * len(POLICIES)
    print(
        f"[T-206 真实数据] 自检：重建不一致 {unstable}，非空白未覆盖 {coverage_failures}，"
        f"chunk_id 重算失败 {id_failures}，偏移回算失败 {offset_failures}，"
        f"文本与归一化区间不符 {text_mismatches}，超 max_chars {over_max}"
        f"（以上必须全为 0）；as_anchor() 拒绝 {anchor_misuse}/{expected_anchor_misuse}"
        f"（必须全拒绝）"
    )

    assert unstable == 0
    assert coverage_failures == 0
    assert id_failures == 0
    assert offset_failures == 0
    assert text_mismatches == 0
    assert over_max == 0
    assert anchor_misuse == expected_anchor_misuse

    # 两个策略下的真实分块数：小窗口必然切得更碎，且都在真实语料上非空
    assert all(total_chunks[name] > 0 for name in POLICIES)
    assert all(len(counts[name]) == len(real_records) for name in POLICIES)
    assert min(min(counts[name]) for name in POLICIES) >= 1, "每条真实文档至少应产出一块"
    assert total_chunks[SMALL_LABEL] > total_chunks[DEFAULT_LABEL]
    assert POLICIES[DEFAULT_LABEL] != POLICIES[SMALL_LABEL]
    assert (
        policy_fingerprint(DEFAULT_POLICY.snapshot_fields())
        != policy_fingerprint(POLICIES[SMALL_LABEL].snapshot_fields())
    )

    # 真实数据一个字节都没被改动（只读判据）
    assert _tree_digest(LEGACY_RAW) == legacy_before


def test_real_corpus_is_not_tiny(real_records: List[RealRecord]) -> None:
    """真实数据规模下限（防止"数据没了但测试还绿"）。"""
    assert len(real_records) >= 60, f"真实可分块文档只有 {len(real_records)} 篇，数据可能已缺失"
    channels = {path.parent.name for path, _, _ in real_records}
    assert len(channels) >= 3, f"真实频道只有 {len(channels)} 个：{sorted(channels)}"
    assert all(len(raw_bytes) > 0 for _, _, raw_bytes in real_records)
    assert all(raw_id.startswith("raw_") and len(raw_id) == 36 for _, raw_id, _ in real_records)


def test_real_quote_anchors_are_independent_of_chunks(
    real_records: List[RealRecord],
) -> None:
    """真实 quote → 真值锚点：由 `verify_quote` 独立重算，分块完全不参与（SPEC §2.2）。

    每个样本都落在两支之一并**都被断言**：判据成立 → `VERIFIED`；
    判据不成立（切片端点切开了 HTML 实体字面量）→ 响亮拒绝 `AnchorError`。
    """
    verified = entity_boundary = 0

    for path, raw_id, raw_bytes in real_records:
        normalized, chunkset = chunk(raw_bytes, CONTENT_TYPE, raw_id=raw_id)
        quote = _seed_quote(chunkset)
        assert len(quote) >= 8, f"真实正文取不出可用 quote：{path.name} → {quote!r}"

        # 锚点唯一性：`match_quote` 取首个匹配，因此 quote 必须在真实正文里唯一，
        # "我算的位置"与"锚点算的位置"才必然是同一处。
        occurrences = normalized.text.count(quote)
        assert occurrences == 1, f"quote 在真实正文里出现 {occurrences} 次：{path.name}"

        position = normalized.text.index(quote)
        raw_slice = normalized.raw_slice(position, position + len(quote))
        first, last = quote[0], quote[-1]

        # SPEC §2.2 判据本身（**不抛异常**地先算一遍）：原文切片经实体解码后，
        # 必须同时包含 quote 的首字符与尾字符。
        window = html.unescape(raw_slice)
        if first not in window or last not in window:
            # 显式分支：切片端点切开了一个实体字面量（如 `…person&#82`），
            # 解码会把 `&#82;` 变成 `R`，端字符在解码后的窗口里必然消失。
            # 证据层此时必须**响亮拒绝**，不得返回编造坐标（硬规则 2）。
            with pytest.raises(AnchorError):
                verify_quote(
                    raw_id=raw_id, raw_bytes=raw_bytes, quote=quote, content_type=CONTENT_TYPE
                )
            # **活对照**（硬规则 4）：同一篇文档、同一条调用路径换一段合法 quote
            # 必须 VERIFIED —— 证明上面的拒绝来自输入（实体边界），不是签名/接线不对。
            control = _entity_safe_quote(normalized, chunkset)
            assert control is not None, f"同一文档里找不到合法 quote 做活对照：{path.name}"
            control_outcome = verify_quote(
                raw_id=raw_id, raw_bytes=raw_bytes, quote=control, content_type=CONTENT_TYPE
            )
            assert control_outcome.status is VerificationStatus.VERIFIED, (
                f"活对照失败：{path.name} 的合法 quote 也没通过校验（{control_outcome.status}）"
            )
            entity_boundary += 1
            print(
                f"[T-206 真实数据] 实体边界（真实数据特性，SPEC §2.2）：{path.name} "
                f"quote={quote[:16]!r}… 端点落在实体字面量内 → verify_quote 响亮拒绝"
                f"（活对照：同文档 {control[:16]!r}… → VERIFIED）"
            )
            continue

        outcome = verify_quote(
            raw_id=raw_id, raw_bytes=raw_bytes, quote=quote, content_type=CONTENT_TYPE
        )
        assert outcome.status is VerificationStatus.VERIFIED, f"{path.name}: {outcome.status}"
        assert outcome.anchor is not None and outcome.derived is not None
        assert outcome.anchor.raw_id == raw_id
        assert outcome.anchor.raw_sha256 == content_sha256(raw_bytes)
        # 锚点的原文切片就是 quote 的原文切片（坐标由 quote 确定性重算）
        assert outcome.raw_slice == raw_slice
        assert (
            normalized.text[outcome.derived.normalized_start : outcome.derived.normalized_end]
            == quote
        )
        # 锚点落在某个分块内部——但锚点**不依赖**那个分块
        assert any(
            item.normalized_start <= outcome.derived.normalized_start < item.normalized_end
            for item in chunkset.chunks
        )
        verified += 1

    assert verified + entity_boundary == len(real_records), "有样本既没验证也没归类（静默跳过）"
    assert verified >= 60, f"真实数据只验证了 {verified} 条 quote，样本太少"
    assert entity_boundary >= 1, (
        "真实语料里应当存在被实体字面量切开的 quote；若为 0，"
        "说明该分支已成死代码，或数据已被替换"
    )
    print(
        f"[T-206 真实数据] 真实 quote → 真值锚点：{verified} 条 VERIFIED，"
        f"{entity_boundary} 条落在实体字面量边界（响亮拒绝 AnchorError），"
        f"共 {len(real_records)} 条真实文档，无一静默跳过"
    )
