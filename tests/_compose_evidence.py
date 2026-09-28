"""T-107 接线测试的共享辅助：**真的跑一遍流水线**（不外打网络）的手脚。

为什么需要它（而不是每个测试文件各写一份）：T-107 的证据校验节点读的是
"真实归档里的字节"，因此测试必须先真的走完 `collect → archive → normalize`。
这段脚手架与 `tests/test_compose_pipeline.py` 里的 `FakeFetcher` / `_dependencies`
是同一套做法（注入假 fetcher，一条网络都不打）；两份实现会漂移，漂移的那一份
会让"节点真的跑通了"这条证据失真。

文件名用 `_` 前缀：它**不是**测试模块（pytest 不该收集它，见 CLAUDE.md 的测试约定）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from atlas.cognition import (
    CLAIM_STATUS_CLASSIFIED,
    CLAIM_STATUS_UNCLASSIFIED,
    ProposedClaimRow,
    output_digest_for,
)
from atlas.collect import (
    DomainThrottle,
    FetchResult,
    RobotsCache,
    RobotsFetchResult,
    RetryPolicy,
)
from atlas.compose import ComposeDependencies
from atlas.registry import (
    Channel,
    FetchSpec,
    FetchType,
    Industry,
    RegistryService,
    open_store as open_registry_store,
)

__all__ = [
    "ACTOR",
    "CHANNEL_ID",
    "QUOTE_A",
    "QUOTE_B",
    "QUOTE_MISSING",
    "RAW_TEXT",
    "WINDOW",
    "ENDPOINT",
    "classified_row",
    "evidence_db_rows",
    "evidence_pipeline",
    "make_store_root",
    "register_channel",
    "unclassified_row",
]

ACTOR = "t107-tester"
WINDOW = "2026-01-01T00:00:00+00:00"
ENDPOINT = "http://127.0.0.1:9/t107.txt"
CHANNEL_ID = "chan-t107"
INDUSTRY_ID = "ai"

#: 纯文本原文（**没有**标签）：归一化后文本与原文逐字符一致，因此"单元区间"
#: 就是原文区间，锚点落界这件事可以直接核对。
RAW_TEXT = (
    "Atlas 证据校验接线的测试原文 2026-01-01。\n"
    "第一，归档的原始字节必须能按 sha256 核对。\n"
    "第二，证据锚点只能由确定性匹配算出，不得来自模型。\n"
    "第三，证据锚点必须落在它自己的单元区间内，否则响亮失败。\n"
)
RAW_BYTES = RAW_TEXT.encode("utf-8")

#: 两个真实存在于原文里的 quote（用来做"活对照"）。
QUOTE_A = "归档的原始字节必须能按 sha256 核对"
QUOTE_B = "证据锚点必须落在它自己的单元区间内"
#: 一个**不可能**匹配到的 quote（负路径）。
QUOTE_MISSING = "这段文字不在原文里、任何确定性匹配都找不到它"


class FakeFetcher:
    """只回放内存响应；从不打开 socket。"""

    def __init__(self, body: bytes = RAW_BYTES) -> None:
        self._response = FetchResult(url=ENDPOINT, status_code=200, content=body)
        self.calls: list[str] = []

    def __call__(self, request: Any) -> FetchResult:
        self.calls.append(request.url)
        if request.url != ENDPOINT:
            raise AssertionError(
                f"假 fetcher 没有为 {request.url!r} 准备响应：测试禁止访问真实网络"
            )
        return self._response


def _robots_absent(robots_url: str, *, user_agent: str = "") -> RobotsFetchResult:
    """RFC 9309：404 = 站方明确表示没有 robots.txt → 允许（SPEC §2.12）。"""
    return RobotsFetchResult(url=robots_url, status_code=404, body=b"")


def dependencies(fetcher: Any = None) -> ComposeDependencies:
    return ComposeDependencies(
        fetcher=fetcher or FakeFetcher(),
        robots=RobotsCache(_robots_absent, user_agent="Atlas-Test/1.0 (offline)"),
        throttle=DomainThrottle(
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            global_min_interval=0.0,
        ),
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        retry=RetryPolicy(max_attempts=2, backoff_seconds=0.0),
    )


def register_channel(root: Path, *, channel_id: str = CHANNEL_ID, endpoint: str = ENDPOINT) -> Path:
    """给临时存储根配一个行业 + 渠道（走 T-101 的真实 API）。"""
    store = open_registry_store(root / "atlas.db", author=ACTOR)
    try:
        service = RegistryService(store)
        if not any(item.id == INDUSTRY_ID for item in service.list_industries()):
            service.create_industry(
                Industry(id=INDUSTRY_ID, name="AI", enabled=True),
                author=ACTOR,
                note="T-107 接线测试前置",
            )
        service.create_channel(
            Channel(
                id=channel_id,
                industry_id=INDUSTRY_ID,
                type=FetchType.RSS,
                endpoint=endpoint,
                fetch_spec=FetchSpec(type=FetchType.RSS),
                interval_seconds=3600,
                rate_limit_seconds=0,
                enabled=True,
            ),
            author=ACTOR,
            note="T-107 接线测试前置",
        )
    finally:
        store.close()
    return root


def make_store_root(tmp_path: Path, **kwargs: Any) -> Path:
    root = tmp_path / "store"
    register_channel(root, **kwargs)
    return root


def evidence_pipeline(store_root: Path, fetcher: Any = None, **kwargs: Any):
    """装配一条真的流水线（默认窗口固定 → 同窗口重跑 = 幂等）。"""
    from atlas.compose import Pipeline, PipelineConfig

    options: Dict[str, Any] = {"window": WINDOW, "actor": ACTOR}
    pipeline_class = kwargs.pop("pipeline_class", Pipeline)
    options.update(kwargs)
    config = PipelineConfig(store_root=Path(store_root), **options)
    return pipeline_class(config, dependencies=dependencies(fetcher))


def classified_row(
    *,
    raw_id: str,
    quote: str,
    unit_char_start: int,
    unit_char_end: int,
    unit_id: str = "ent_" + "b" * 32,
    value: str = "machine-learning",
    confidence: float = 0.8,
    version: int = 0,
) -> ProposedClaimRow:
    """构造一行 `classified`（字段与 `tests/test_propose_store.py` 同一口径）。"""
    return ProposedClaimRow(
        raw_id=raw_id,
        unit_id=unit_id,
        unit_kind="entry",
        unit_char_start=unit_char_start,
        unit_char_end=unit_char_end,
        entry_index=0,
        title="T-107 测试单元",
        kind="industry",
        value=value,
        quote=quote,
        confidence=confidence,
        status=CLAIM_STATUS_CLASSIFIED,
        output_digest=output_digest_for(
            value=value,
            quote=quote,
            confidence=confidence,
            status=CLAIM_STATUS_CLASSIFIED,
            reason=None,
        ),
        plan_digest="p" * 64,
        code_version="cognition-extract-prompt/1",
        config_version="cognition-config/1",
        model_version="deepseek-flash",
        label_space_version="cfg/7#deadbeef",
        input_digest="i" * 64,
        batch_id="bat_" + "c" * 32,
        batch_position=0,
        batch_size=1,
        version=version,
    )


def unclassified_row(*, raw_id: str, reason: str = "timeout") -> ProposedClaimRow:
    """构造一行 `unclassified`（降级行：没有 quote，**不得**当证据）。"""
    return ProposedClaimRow(
        raw_id=raw_id,
        unit_id="ent_" + "d" * 32,
        unit_kind="entry",
        unit_char_start=0,
        unit_char_end=max(1, len(RAW_TEXT)),
        entry_index=0,
        title="T-107 降级单元",
        kind="industry",
        status=CLAIM_STATUS_UNCLASSIFIED,
        reason=reason,
        output_digest=output_digest_for(
            value=None,
            quote=None,
            confidence=None,
            status=CLAIM_STATUS_UNCLASSIFIED,
            reason=reason,
        ),
        plan_digest="p" * 64,
        code_version="cognition-extract-prompt/1",
        config_version="cognition-config/1",
        model_version="deepseek-flash",
        label_space_version="cfg/7#deadbeef",
        input_digest="i" * 64,
        batch_id="bat_" + "c" * 32,
        batch_position=0,
        batch_size=1,
    )


def evidence_db_rows(db_path: Path) -> list[Dict[str, Any]]:
    """从**另一个连接**读 `evidence_spans` 的全部行。

    跨连接可见性才是"真的写进去了"的证据：同一连接的自我可见不算
    （SPEC §6.6 第 4 条记载的实测事故）。
    """
    import sqlite3

    connection = sqlite3.connect(str(db_path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT claim_id, claim_version, raw_id, raw_sha256, quote, "
            "char_start, char_end, normalized_start, normalized_end "
            "FROM evidence_spans ORDER BY claim_id, claim_version"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]
