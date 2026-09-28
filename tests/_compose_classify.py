"""分类节点（T-105 接进组合根）测试的共享辅助：**真的跑一遍流水线**的手脚。

为什么需要它：`classify` 节点读的是"归档里的字节 + 注册表里的标签空间"，因此测试必须
先真的走完 `collect → archive → normalize`。这段脚手架与
`tests/test_compose_pipeline.py` / `tests/_compose_evidence.py` 是同一套做法
（注入假 fetcher，一条网络都不打），只有一处不同：认知层端口是**注入的哑端口**
（`QuotingPort`），因此本文件的所有用例**都不调用模型**、也不需要 node / 凭据 / 边车依赖。

文件名用 `_` 前缀：它**不是**测试模块（pytest 不该收集它，见 CLAUDE.md 的测试约定）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from atlas.cognition import (
    CallStatus,
    CognitionCallRecord,
    CognitionConfig,
    CognitionOutput,
    CognitionResult,
    CognitionUsage,
    DegradeReason,
    ExtractedClaim,
)
from atlas.collect import (
    DomainThrottle,
    FetchResult,
    RobotsCache,
    RobotsFetchResult,
    RetryPolicy,
)
from atlas.compose import ComposeDependencies, Pipeline, PipelineConfig
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
    "ARTICLE_ENDPOINT",
    "CHANNEL_ARTICLE",
    "CHANNEL_FEED",
    "CHANNEL_HTML_FEED",
    "FEED_BYTES",
    "FEED_ENDPOINT",
    "HTML_FEED_BYTES",
    "HTML_FEED_ENDPOINT",
    "INDUSTRY_AI",
    "INDUSTRY_WEB",
    "QUOTE_ARTICLE_A",
    "QUOTE_ARTICLE_B",
    "QUOTE_FEED_A",
    "QUOTE_FEED_B",
    "QUOTE_HTML_FEED_A",
    "QUOTE_HTML_FEED_B",
    "RAW_TEXT",
    "WINDOW",
    "FakeFetcher",
    "QuotingPort",
    "classify_pipeline",
    "dependencies",
    "make_store_root",
    "proposal_runs_rows",
    "proposed_rows",
    "raw_id_of",
    "register_channel",
    "register_industry",
]

ACTOR = "t105-compose-tester"
WINDOW = "2026-01-01T00:00:00+00:00"

INDUSTRY_AI = "ai"
INDUSTRY_WEB = "web"

CHANNEL_ARTICLE = "chan-article"
CHANNEL_FEED = "chan-feed"
ARTICLE_ENDPOINT = "http://127.0.0.1:9/article.txt"
FEED_ENDPOINT = "http://127.0.0.1:9/feed.xml"

#: 纯文本原文：**单行、无多余空白**，因此归一化文本与原文逐字符一致
#: （`reduce_text` 只做空白折叠，单行已经折好），于是"整篇文档"的单元文本 == 原文，
#: 由 quote 重算出的锚点必然落在单元区间内 —— 闭环可以端到端核对，不需要任何宽容。
RAW_TEXT = (
    "Atlas 分类接线的测试原文 2026-01-01。"
    "归档的原始字节必须能按 sha256 核对，证据锚点必须由确定性匹配算出。"
).encode("utf-8")

#: 两条 quote 都**逐字**存在于原文里（一条用于正常路径，一条用于活对照 / 多单元）。
QUOTE_ARTICLE_A = "归档的原始字节必须能按 sha256 核对"
QUOTE_ARTICLE_B = "证据锚点必须由确定性匹配算出"

#: 一份真 feed（两个条目 = 两个分类单元）。description 里的文字就是条目正文。
FEED_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Scaling laws</title><link>https://example.com/a</link>
<description>We study transformer scaling for language models in this report.</description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
<item><title>Image segmentation</title><link>https://example.com/b</link>
<description>A new approach to semantic image segmentation is described here.</description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
</channel></rss>
"""

QUOTE_FEED_A = "transformer scaling for language models"
QUOTE_FEED_B = "semantic image segmentation"

#: 一份**良构 RSS**，但 description 里带 HTML 标记 ⇒ `normalize()` 的 `looks_like_html`
#: 会在前 4096 字符里看到 `<p>` 而把它嗅探成 `text/html`。
#: 这正是真实 store 里 syncedreview / marktechpost 两份 feed 的形态（实测），
#: 因此这条数据是**从真实缺陷反推出来**的回归样本，不是凭空造的。
CHANNEL_HTML_FEED = "chan-htmlfeed"
HTML_FEED_ENDPOINT = "http://127.0.0.1:9/htmlfeed.xml"
HTML_FEED_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Retrieval augmented generation</title><link>https://example.com/c</link>
<description><![CDATA[<p>Retrieval augmented generation grounds answers in documents.</p>]]></description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
<item><title>Sparse attention</title><link>https://example.com/d</link>
<description><![CDATA[<p>Sparse attention lowers the cost of long context.</p>]]></description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
</channel></rss>
"""

QUOTE_HTML_FEED_A = "Retrieval augmented generation grounds answers in documents"
QUOTE_HTML_FEED_B = "Sparse attention lowers the cost of long context"


class FakeFetcher:
    """按 URL 回放内存响应；从不打开 socket。"""

    def __init__(self, responses: Optional[Dict[str, bytes]] = None) -> None:
        bodies = responses if responses is not None else {
            ARTICLE_ENDPOINT: RAW_TEXT,
            FEED_ENDPOINT: FEED_BYTES,
        }
        self._responses = {
            url: FetchResult(url=url, status_code=200, content=body)
            for url, body in bodies.items()
        }
        self.calls: List[str] = []

    def __call__(self, request: Any) -> FetchResult:
        self.calls.append(request.url)
        if request.url not in self._responses:
            raise AssertionError(
                f"假 fetcher 没有为 {request.url!r} 准备响应：测试禁止访问真实网络"
            )
        return self._responses[request.url]


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


class QuotingPort:
    """哑端口：把脚本里的 quote 回放成**真实契约对象**（不联网、不起边车）。

    行为刻意只有两条，都是真实模型会发生的事：

    - **正常**：某一批单元里，凡是 `script` 中"逐字出现在这批内容里"的 quote，
      各产出一条 claim（`value` 来自 `value_of`）。因此同一份脚本能覆盖多个 raw、
      多个批次 —— 归属仍然由 T-105 的确定性匹配完成（本端口**不**指定单元）。
    - **降级**：`degrade_with` 非空时每次调用都返回 `unclassified` + 该原因码，
      `claims` 恒空（SPEC §2.14 决策四：降级绝不是猜测）。

    同时记录收到的 `CognitionRequest`，因此"到底调用了几次、给了什么标签"可断言。
    """

    def __init__(
        self,
        script: Sequence[Tuple[str, str]] = (),
        *,
        value_of: str = INDUSTRY_AI,
        degrade_with: Optional[DegradeReason] = None,
        hallucinate: bool = False,
        input_tokens: int = 120,
        output_tokens: int = 30,
        reasoning_tokens: int = 20,
        elapsed_ms: int = 700,
    ) -> None:
        self.config = CognitionConfig(
            provider="quoting",
            model="quoting-model",
            base_url="http://127.0.0.1:1/v1",
            route_name="quoting",
            api_key="not-a-secret",
            model_version="quoting-model",
        )
        self.script: List[Tuple[str, str]] = [
            (str(value), str(quote)) for value, quote in script
        ]
        self.value_of = value_of
        self.degrade_with = degrade_with
        #: `True` = 不管 quote 在不在这一批内容里都回放（模拟模型**改写 / 翻译引文**）。
        self.hallucinate = hallucinate
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
        self.elapsed_ms = elapsed_ms
        self.requests: List[Any] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def candidate_labels_seen(self) -> List[Tuple[str, ...]]:
        return [tuple(request.candidate_labels) for request in self.requests]

    def _record(
        self, request: Any, status: CallStatus, reason: Optional[DegradeReason], detail: str
    ) -> CognitionCallRecord:
        return CognitionCallRecord(
            status=status,
            reason=reason,
            detail=detail,
            provider=self.config.provider,
            model=self.config.model,
            response_model=self.config.model,
            credential_route=self.config.credential_route(),
            sidecar_code_version="sha256:" + "0" * 64,
            code_version=self.config.prompt_version,
            config_version=self.config.config_version,
            model_version=self.config.model_version,
            config_digest=self.config.public_digest(),
            idempotency_key="k" * 64,
            input_digest=request.digest(),
            elapsed_ms=self.elapsed_ms,
            usage=CognitionUsage(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                reasoning_tokens=self.reasoning_tokens,
                total_tokens=self.input_tokens + self.output_tokens,
            ),
            parse_strategy="direct",
            degraded=status is CallStatus.UNCLASSIFIED,
            tool_calls=0,
            tools_declared=0,
            thinking_chars=0,
        )

    def extract(self, request: Any) -> CognitionResult:
        self.requests.append(request)
        if self.degrade_with is not None:
            return CognitionResult(
                record=self._record(
                    request,
                    CallStatus.UNCLASSIFIED,
                    self.degrade_with,
                    f"脚本化降级 {self.degrade_with.value}",
                ),
                output=None,
            )
        content = request.external_content
        claims = [
            ExtractedClaim(kind="industry", value=value, quote=quote, confidence=0.9)
            for value, quote in self.script
            if self.hallucinate or quote in content
        ]
        return CognitionResult(
            record=self._record(request, CallStatus.OK, None, "ok"),
            output=CognitionOutput(claims=claims),
        )


def register_industry(root: Path, industry_id: str) -> None:
    store = open_registry_store(root / "atlas.db", author=ACTOR)
    try:
        service = RegistryService(store)
        if not any(item.id == industry_id for item in service.list_industries()):
            service.create_industry(
                Industry(id=industry_id, name=industry_id.upper(), enabled=True),
                author=ACTOR,
                note="T-105 接线测试前置",
            )
    finally:
        store.close()


def register_channel(
    root: Path,
    *,
    channel_id: str,
    industry_id: str,
    endpoint: str,
    fetch_type: FetchType = FetchType.RSS,
) -> None:
    store = open_registry_store(root / "atlas.db", author=ACTOR)
    try:
        service = RegistryService(store)
        if not any(item.id == industry_id for item in service.list_industries()):
            service.create_industry(
                Industry(id=industry_id, name=industry_id.upper(), enabled=True),
                author=ACTOR,
                note="T-105 接线测试前置",
            )
        service.create_channel(
            Channel(
                id=channel_id,
                industry_id=industry_id,
                type=fetch_type,
                endpoint=endpoint,
                fetch_spec=FetchSpec(type=fetch_type),
                interval_seconds=3600,
                rate_limit_seconds=0,
                enabled=True,
            ),
            author=ACTOR,
            note="T-105 接线测试前置",
        )
    finally:
        store.close()


def make_store_root(tmp_path: Path, *, feed: bool = True, article: bool = True) -> Path:
    """临时存储根：`ai` 行业 + 一条纯文本渠道（+ 可选的真 feed 渠道）。

    两个渠道放在**两个行业**里，因此"标签空间来自注册表"这件事有区分度
    （`label_space()` = 两个启用行业，而 feed 的 `industry_of` 是另一个维度）。
    """
    root = tmp_path / "store"
    if article:
        # `type` 在本测试里只是注册表的元数据：抓取由假 fetcher 回放，而 T-105 的
        # 分流**看内容不看 endpoint**（SPEC §2.16），因此这里不需要真配一个 HTML 协议。
        register_channel(
            root,
            channel_id=CHANNEL_ARTICLE,
            industry_id=INDUSTRY_AI,
            endpoint=ARTICLE_ENDPOINT,
            fetch_type=FetchType.RSS,
        )
    if feed:
        register_channel(
            root,
            channel_id=CHANNEL_FEED,
            industry_id=INDUSTRY_WEB,
            endpoint=FEED_ENDPOINT,
            fetch_type=FetchType.RSS,
        )
    return root


def classify_pipeline(
    store_root: Path,
    *,
    fetcher: Any = None,
    port: Any = None,
    classify: bool = True,
    cognition_factory: Any = None,
    **kwargs: Any,
) -> Pipeline:
    """装配一条**离线**流水线（固定窗口 ⇒ 同窗口重跑 = 幂等；端口注入 ⇒ 不调模型）。

    `**kwargs` 里除 `PipelineConfig` 的字段外，还可以直接给 `Pipeline` 的注入参数
    （`proposed` / `registry` / `evidence` / `execution_store` / …），因为
    "组合根打开还是调用方注入"是这些用例要验证的东西之一。
    """
    options: Dict[str, Any] = {"window": WINDOW, "actor": ACTOR, "classify": classify}
    pipeline_kwargs: Dict[str, Any] = {}
    for name in (
        "dependencies",
        "execution_store",
        "archive",
        "labels",
        "registry",
        "evidence",
        "proposed",
        "cognition",
    ):
        if name in kwargs:
            pipeline_kwargs[name] = kwargs.pop(name)
    options.update(kwargs)
    config = PipelineConfig(store_root=Path(store_root), **options)
    pipeline_kwargs.setdefault("dependencies", dependencies(fetcher))
    pipeline_kwargs.setdefault("cognition", port)
    if cognition_factory is not None:
        pipeline_kwargs["cognition_factory"] = cognition_factory
    return Pipeline(config, **pipeline_kwargs)


def raw_id_of(raw_bytes: bytes, *, channel_id: str, endpoint: str) -> str:
    from atlas.contracts import content_sha256, raw_id_for

    return raw_id_for(channel_id, endpoint, content_sha256(raw_bytes))


def _rows(db_path: Path, table: str, columns: Iterable[str]) -> List[Dict[str, Any]]:
    """从**另一个连接**读表（跨连接可见才算"真的写进去了"）。"""
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1, 2"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def proposed_rows(db_path: Path) -> List[Dict[str, Any]]:
    """`proposed_claims` 的全部行（跨连接读）。表不存在时返回空列表。"""
    try:
        return _rows(
            db_path,
            "proposed_claims",
            (
                "claim_key",
                "version",
                "raw_id",
                "unit_id",
                "kind",
                "value",
                "quote",
                "status",
                "reason",
                "unit_char_start",
                "unit_char_end",
            ),
        )
    except sqlite3.OperationalError:
        return []


def proposal_runs_rows(db_path: Path) -> List[Dict[str, Any]]:
    """`proposal_runs` 的全部行（跨连接读）。表不存在时返回空列表。"""
    try:
        return _rows(
            db_path,
            "proposal_runs",
            ("run_id", "unit_id", "raw_id", "plan_digest", "status", "reason", "retry_count"),
        )
    except sqlite3.OperationalError:
        return []
