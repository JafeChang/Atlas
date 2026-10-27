"""Web 前端的**启动装配**（组合根）：把归档、条目派生层、标签库与配置接成可运行的前端。

为什么需要这个模块（以及它为什么不在 `atlas.webui` 里）
=====================================================

SPEC §6.5 的结论是"把 T-130 的条目接进 feed 与前端"。接线需要同时用到四个包：

| 包 | 提供什么 |
|---|---|
| `atlas.archive`（T-103） | 不可变原文的字节 |
| `atlas.entries`（T-130） | feed → 条目（**派生层**） |
| `atlas.feed`（T-106） | 筛选 / 排序 / 分页（条目由调用方注入） |
| `atlas.webui`（T-109） | 服务端渲染 + 打标 |

而目标是"前端渲染真实条目"，所以**必须**有人同时碰到 `atlas.entries` 与
`atlas.webui`。两个包的 AST 边界都不允许对方进来：

- `atlas.webui` 的 import 白名单只允许 `atlas` 根与 stdlib
  （`tests/test_webui_app.py::test_webui_uses_only_stdlib_and_the_atlas_package`）；
- `atlas.feed` 只允许 `atlas.contracts` / `atlas.archive`
  （`tests/test_feed_http.py::test_feed_package_does_not_import_labels_or_registry_impl`）。

因此接线放在这里，作为**唯一的组合根**。这也是 §2.5 那条"闭环由组合根注入完成"
的同一手法（`industry_of` 就是这么接的）——不新增抽象，只是把已有的注入点填上。

容器口径（SPEC §6.3 裁决 B）怎么落地
------------------------------------

| raw 种类 | 在条目列表里 |
|---|---|
| **容器**（内容能解析成 feed，实测 8 条） | **本身不出现**；它的派生条目出现 |
| **本身就是条目**（按篇归档的文章，实测 65 条） | 整篇作为一条出现 |
| Algolia/Hacker News JSON 搜索响应 | 展开为新闻条目，使用原始 JSON 对象的字符区间 |
| 明确的验证码跳转响应 | 显示来源提示；原始响应仍可从文档视图查看 |
| 其余未知内容 | 仍按整篇保留，便于检查 |

判据是**内容**：RSS/Atom、Algolia 的 hits 数组、验证码页的实际 meta 重定向目标。
不根据 endpoint 后缀猜格式，空订阅不回落成文章。JSON 浏览适配与机器分类分流独立。

> ⚠️ 分类是**派生量**，没有落库：`raw_records` 没有 `entry_kind` 列，加列会牵动
> T-103 的物理表（超出本任务范围）。因此它在浏览时按内容计算并缓存（确定性、可重建）。

标签库为什么**不**在这里初始化
------------------------------

`build_webapp` **不**创建 `confirmed_labels` 表，也不创建标签库文件：那是写操作，
只允许由显式调用 `WebUIApplication`/`build_application` 的部署入口完成
（`StoreLabelAccess.initialize()` 归 T-108）。本模块只给出路径，不落盘。

命令行入口
----------

```bash
PYTHONPATH=src ./.venv-new/bin/python -m atlas.webapp
```

`main()` 是这个模块的 CLI（`python -m atlas.webapp` 使 `__name__ == "__main__"`），
风格照 `atlas.compose.cli`：argparse、`--store-root` 默认 `data/store`
（可用 `ATLAS_STORE_ROOT` 覆盖）、失败打印到 stderr 并非零退出。

**它负责界面与按需阅读服务**：Feed / 原文详情不采集、不调用模型、不改配置；
图片按浏览需要进入独立缓存，中文全文仅由用户切换 tab 后显式生成。三条硬要求：

1. 启动时打印**真实 URL**（`--port 0` 时是内核分配的那个端口）与退出方式；
2. **store 不存在 / 没有归档时响亮失败**，并写清下一步（先跑采集）——
   否则用户看到空 feed 会以为功能坏了；
3. **默认只绑 `127.0.0.1`**：本服务能写标签且**没有认证**（认证是 SPEC §5 登记 #10
   的延后项），因此不提供"一键对外绑定"的便利开关。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from atlas.analytics import EventStore
from atlas.archive import (
    ArchiveStore,
    enable_wal,
    open_archive,
    read_article_body,
    read_article_contents,
    read_article_response,
)
from atlas.cognition import (
    BriefSource,
    brief_source,
    display_brief,
    read_articles,
    read_classifications,
)
from atlas.contracts import RawRecord
from atlas.entries import (
    EntryParseError,
    is_challenge_page,
    parse_algolia_entries,
    parse_entries,
)
from atlas.feed import (
    ArchiveFeedSource,
    FeedEntry,
    RawEntriesView,
    title_from_endpoint,
)
from atlas.normalize.text import decode_bytes
from atlas.cognition.admission import read_admissions
from atlas.registry import RegistryService, read_subscription_sources
from atlas.registry import open_store as open_registry_store
from atlas.reader import ReaderService, make_document
from atlas.reader.content import feed_excerpt
from atlas.webui import (
    DEFAULT_ACTOR,
    StoreLabelAccess,
    WebUIApplication,
    raw_exists_from_source,
)

__all__ = [
    "ArchiveEntriesAccess",
    "DEFAULT_PORT",
    "FEED_ENTRY_KIND",
    "LocatedEntry",
    "StoreSummary",
    "build_parser",
    "build_webapp",
    "main",
    "render_startup",
    "store_summary",
]

#: 容器分类的唯一取值（写进 `RawRecord.entry_kind`）。用字面量而不是新枚举：
#: 契约侧的字段文档已经写明"`None` = 不是容器，`"feed"` = 容器"。
FEED_ENTRY_KIND = "feed"

#: 默认端口：挑一个不常用的高位端口，避免撞上 8000/8080/3000 这类常用位。
DEFAULT_PORT = 8765

#: 默认存储根（与 `atlas.compose.cli` 同一口径，含同一个环境变量覆盖）。
STORE_ROOT_ENV_VAR = "ATLAS_STORE_ROOT"
DEFAULT_STORE_ROOT = "data/store"


# ---------------------------------------------------------------------- #
# 一份原文的条目层结论
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Analysis:
    """一份原文的条目层结论；容器身份与是否有文章分别记录。"""

    entries: Tuple[Any, ...]
    text: str
    text_length: int
    is_feed: bool = False
    problems: Tuple[str, ...] = ()
    unavailable_reason: str = ""


class LocatedEntry:
    """`/entry` 所需的定位结果：条目 + **已切好的展示文本** + 是否精确命中。"""

    __slots__ = ("entry", "content", "sliced", "notice")

    def __init__(
        self, entry: FeedEntry, content: str, sliced: bool, notice: str = ""
    ) -> None:
        self.entry = entry
        self.content = content
        self.sliced = sliced
        self.notice = notice


class ArchiveEntriesAccess:
    """归档 + T-130 条目化：`atlas.feed.EntriesLookup` 与 `/entry` 定位器的实现。

    只读：读原文用 `ArchiveStore.get_content`，不写任何东西。
    解析结果按 `raw_id` 缓存（同一进程内重复请求同一份 feed 不重复解析）。
    """

    def __init__(self, archive: ArchiveStore, *, cache_size: int = 256) -> None:
        if cache_size < 1:
            raise ValueError("cache_size 必须大于 0")
        self._archive = archive
        self._cache_size = cache_size
        self._cache: Dict[str, _Analysis] = {}
        self._brief_sources: Dict[str, Dict[tuple[int, int], BriefSource]] = {}
        self._cache_lock = threading.RLock()
        self._subscription_sources = read_subscription_sources(archive.db_path)

    # -- 基础 -----------------------------------------------------------------
    def content(self, raw_id: str) -> bytes:
        return self._archive.get_content(raw_id)

    def _analyse(self, record: RawRecord) -> _Analysis:
        with self._cache_lock:
            cached = self._cache.get(record.raw_id)
            if cached is not None:
                self._cache[record.raw_id] = self._cache.pop(record.raw_id)
                return cached

        raw_bytes = self.content(record.raw_id)
        text = decode_bytes(raw_bytes)[0]
        algolia = parse_algolia_entries(raw_bytes, raw_id=record.raw_id)
        if algolia is not None:
            analysis = _Analysis(
                entries=algolia.entries, text=algolia.feed_text, text_length=len(algolia.feed_text),
                is_feed=True, problems=algolia.problems,
            )
        elif is_challenge_page(text):
            analysis = _Analysis(
                entries=(), text=text, text_length=len(text),
                unavailable_reason="来源返回验证码验证页，本次没有可展示的文章；原始响应保留在归档中。",
            )
        else:
            analysis = self._analyse_xml(record, raw_bytes)

        with self._cache_lock:
            # 并发请求可能在解析期间已放入同一份不可变原文。
            cached = self._cache.get(record.raw_id)
            if cached is not None:
                self._cache[record.raw_id] = self._cache.pop(record.raw_id)
                return cached
            while len(self._cache) >= self._cache_size:
                evicted = next(iter(self._cache))
                del self._cache[evicted]
                self._brief_sources.pop(evicted, None)
            self._cache[record.raw_id] = analysis
            return analysis

    def _analyse_xml(self, record: RawRecord, raw_bytes: bytes) -> _Analysis:
        try:
            entrieset = parse_entries(raw_bytes, "", raw_id=record.raw_id)
        except EntryParseError as error:
            if (record.channel_id, record.endpoint) in self._subscription_sources:
                return _Analysis(
                    entries=(), text=decode_bytes(raw_bytes)[0], text_length=_text_length(raw_bytes),
                    unavailable_reason=f'订阅端点返回了非订阅内容，本次未展示为文章：{error}',
                )
            # Algolia 与验证码响应已经处理；其他非 XML 内容按整篇保留。
            # **不吞掉**这个事实：它正是 §6.3 的"非容器"那一类。
            analysis = _Analysis(entries=(), text="", text_length=_text_length(raw_bytes))
        else:
            if (record.channel_id, record.endpoint) in self._subscription_sources and entrieset.kind not in ('rss', 'atom'):
                return _Analysis(
                    entries=(), text=entrieset.feed_text, text_length=len(entrieset.feed_text),
                    unavailable_reason='订阅端点响应未包含可识别的订阅条目，原始响应仍保留在归档中。',
                )
            analysis = _Analysis(
                entries=tuple(entrieset.entries),
                text=entrieset.feed_text,
                text_length=len(entrieset.feed_text),
                is_feed=entrieset.kind in ("rss", "atom"),
                problems=entrieset.problems,
            )

        return analysis

    # -- atlas.feed.EntriesLookup -------------------------------------------
    def entries_of(self, record: RawRecord) -> RawEntriesView:
        """`RawEntriesView` 契约：条目 + 解码后原文的字符数。

        字符数**任何情况下都给**（整篇条目的区间靠它），
        因此非容器也要解码一次——这正是 `RawEntriesView` 存在的理由。
        """
        analysis = self._analyse(record)
        return _RawEntriesView(
            entries=analysis.entries, text_length=analysis.text_length,
            is_feed=analysis.is_feed, problems=analysis.problems,
            unavailable_reason=analysis.unavailable_reason,
        )

    def is_container(self, raw_id: str) -> bool:
        """RSS/Atom 或受支持的 JSON 订阅即容器，包括本次没有文章的响应。"""
        return self._analyse(self._archive.get(raw_id)).is_feed

    def brief_sources(self, raw_id: str) -> Dict[tuple[int, int], BriefSource]:
        """简介的标题 / 正文来源；和文章区间对应，绝不把 JSON 元数据当成正文。"""
        with self._cache_lock:
            if raw_id in self._brief_sources:
                self._cache[raw_id] = self._cache.pop(raw_id)
                return self._brief_sources[raw_id]
        record = self._archive.get(raw_id)
        analysis = self._analyse(record)
        sources = {}
        if not analysis.unavailable_reason:
            if analysis.is_feed:
                for entry in analysis.entries:
                    body = getattr(entry, "entry_text", "")
                    if getattr(entry, "kind", "") == "algolia-hn":
                        hit = json.loads(entry.content_slice(analysis.text))
                        body = hit.get("story_text") or hit.get("text") or ""
                        if not isinstance(body, str):
                            body = ""
                    sources[(entry.char_start, entry.char_end)] = brief_source(entry.title, body, url=entry.link or record.endpoint)
            else:
                text = analysis.text or decode_bytes(self.content(raw_id))[0]
                sources[(0, max(1, analysis.text_length))] = brief_source(
                    title_from_endpoint(record.endpoint), text, kind="excerpt", url=record.endpoint,
                )
        with self._cache_lock:
            # 计算期间若另一请求淘汰了原文，不留下无界的孤立缓存。
            if raw_id in self._cache:
                self._brief_sources[raw_id] = sources
        return sources

    # -- 容器分类 -----------------------------------------------------------
    def container_ids(self) -> Tuple[str, ...]:
        """哪些 raw 是**容器**（`entry_kind == "feed"`）。

        判据是内容被识别为 RSS/Atom 或受支持的 JSON，与本次文章数量无关。
        解析失败和未知 XML 仍按整篇原文保留，不伪装成空订阅。
        """
        containers: list[str] = []
        for raw_id in self._archive.all_raw_ids():
            if self.is_container(raw_id):
                containers.append(raw_id)
        return tuple(containers)

    # -- `/entry` 的定位器 ---------------------------------------------------
    def locate_entry(
        self, record: RawRecord, *, char_start: int, char_end: int
    ) -> LocatedEntry:
        """按整数区间定位条目并切出展示文本（**不做任何坐标推算**）。

        - 区间**恰好**等于某个条目的区间 ⇒ 精确命中（`sliced=True`）；
        - 区间落在某个条目**内部** ⇒ 用那个条目并把请求区间如实写进提示；
        - 区间落在所有条目之外 ⇒ 退回"整篇"，并说明理由（不假装高亮过）。
        """
        analysis = self._analyse(record)
        for entry in analysis.entries:
            if entry.char_start == char_start and entry.char_end == char_end:
                return LocatedEntry(
                    entry=_feed_entry(record, entry),
                    content=entry.content_slice(analysis.text),
                    sliced=True,
                )

        if not analysis.entries:
            raw_bytes = self.content(record.raw_id)
            return LocatedEntry(
                entry=_whole_document_entry(record, text_length=analysis.text_length),
                content=decode_bytes(raw_bytes, "")[0],
                sliced=False,
                notice=(
                    "该原文不是 feed 容器（内容解析不出条目），整篇即一个条目；"
                    "没有可高亮的子区间。"
                ),
            )

        containing = next(
            (
                entry
                for entry in analysis.entries
                if entry.char_start <= char_start and char_end <= entry.char_end
            ),
            None,
        )
        target = containing or analysis.entries[0]
        notice = (
            f"请求区间 [{char_start}, {char_end}) 不等于任何条目的区间；"
            f"已按包含它的条目 entry[{target.index}] "
            f"[{target.char_start}, {target.char_end}) 展示（条目层可能已换解析器版本）。"
        )
        return LocatedEntry(
            entry=_feed_entry(record, target),
            content=target.content_slice(analysis.text),
            sliced=False,
            notice=notice,
        )


@dataclass(frozen=True)
class _RawEntriesView:
    """`atlas.feed.RawEntriesView` 的显式实现（Protocol 的结构实现，无继承）。"""

    entries: Sequence[Any]
    text_length: int
    is_feed: bool = False
    problems: Tuple[str, ...] = ()
    unavailable_reason: str = ""


# ---------------------------------------------------------------------- #
# 装配
# ---------------------------------------------------------------------- #
def build_webapp(
    root: Any,
    *,
    db_path: Any,
    industry_provider: Callable[[], Sequence[str]],
    industry_of: Any = None,
    actor: str = "me",
    host: str = "127.0.0.1",
    port: int = 0,
    initialize_labels: bool = True,
    cache_root: Any = None,
    image_cache: Any = None,
    translation_client_factory: Any = None,
) -> WebUIApplication:
    """装配一个**按条目渲染**的真实前端（已启动）。

    Args:
        root: `data/store`（归档根；`atlas.archive.open_archive`）。
        db_path: Confirmed 标签库路径（**必填**，没有指向仓库 `data/` 的默认值）。
        industry_provider: 零参可调用对象，返回当前启用的行业 id
            （生产上接 `atlas.registry.RegistryService.label_space`，C8 闭环）。
        industry_of: 渠道 → 行业（`dict` 或可调用对象），喂给 feed 的筛选维度。
        initialize_labels: 是否在建服务前初始化标签库（建表 + 触发器，归 T-108）。
            默认 `True`（部署入口的语义）；**只想浏览、不想在磁盘上留下任何东西时
            传 `False`**（此时标签库文件不会被创建）。

    注入的三件套（`entries_of` / `locate_entry` / `text_length`）都来自
    `atlas.entries`（T-130），且**必须**注入：缺了它们 `/feed` 会响亮失败，
    不会退回"把容器当条目"的旧行为。

    归档句柄的生命周期跟着应用走：`close()` 时由 `on_close` 关掉（本模块负责接线，
    `atlas.webui` 不需要知道 `atlas.archive` 的存在）。
    """
    archive = open_archive(root)
    access = ArchiveEntriesAccess(archive)
    kinds: Dict[str, Optional[str]] = {}

    def entry_kind_of(raw_id: str) -> Optional[str]:
        # Raw 不可变，单条分类可缓存；归档清单不能缓存，采集进程会持续追加。
        if raw_id not in kinds:
            kinds[raw_id] = FEED_ENTRY_KIND if access.is_container(raw_id) else None
        return kinds[raw_id]

    source = ArchiveFeedSource(
        archive,
        industry_of=industry_of,
        entry_kind_of=entry_kind_of,
    )

    def entries_of(record: RawRecord) -> RawEntriesView:
        return access.entries_of(record)

    def locate_entry(record: RawRecord, **kwargs: Any) -> LocatedEntry:
        return access.locate_entry(record, **kwargs)

    def text_length(record: RawRecord) -> int:
        return access.entries_of(record).text_length

    labels = StoreLabelAccess(db_path, actor=actor)
    if initialize_labels:
        labels.initialize()

    def project_entries(entries: Sequence[FeedEntry], session: Any) -> Sequence[FeedEntry]:
        views = read_classifications(db_path)
        briefs = read_articles(db_path)
        admission = read_admissions(db_path)
        bodies = read_article_contents(db_path)
        articles = {v.raw_id: v for v in views.values() if v.unit_kind == "article"}
        projected = []
        sources_by_raw: dict[str, dict[tuple[int, int], BriefSource]] = {}
        for entry in entries:
            if entry.raw_id not in sources_by_raw:
                sources_by_raw[entry.raw_id] = access.brief_sources(entry.raw_id)
            summary_source = sources_by_raw[entry.raw_id].get((entry.char_start, entry.char_end))
            summary, summary_origin = display_brief(summary_source, briefs) if summary_source else ("", "")
            article = briefs.get(summary_source.key) if summary_source else None
            title_zh = article.title_zh if article and article.status == 'ready' and not summary_source.native_chinese else ''
            body = bodies.get(summary_source.url) if summary_source else None
            review = admission.reviews.get(summary_source.key) if summary_source and admission.enabled else None
            admission_status = (review.decision if review else "pending") if admission.enabled else ""
            admission_reason = review.reason if review else "尚未完成入站审核"
            body_note = ''
            saved_body = read_article_body(db_path,summary_source.url) if summary_source and len(entries)==1 else None
            if body:
                if body.status == 'ready':
                    body_note = f'{"论文全文" if body.content_kind == "paper" else "正文"}已留存，清洗后 {body.body_chars} 字'
                    if body.truncated:
                        body_note += '（内容受限，已标记截断）'
                    if not article or article.status != 'ready':
                        summary_origin = '简介待生成：正文已留存'
                elif body.status == 'partial':
                    body_note = '目前留存摘要或片段，完整正文待补抓'
                else:
                    body_note = '本次正文获取失败：' + body.failure_label
                    if not article or article.status != 'ready':
                        summary_origin = '简介暂缺：' + body.failure_label
            values = session.values_for_entry(entry)
            view = views.get(entry.entry_id) if entry.from_feed else articles.get(entry.raw_id)
            industry_source, status, model = "channel", "pending", ""
            channel_industry = entry.industry or source.industry_of(entry.channel_id)
            industries = (channel_industry,) if channel_industry else ()
            if view is not None:
                status, model = view.status, view.model
                industry_source = view.origin
                industries = tuple(view.labels)
                if status == "classified" and not industries:
                    status = "outdated"
            manual_tags = tuple(value for key, value in values.items() if key.startswith("tag:"))
            ai_tags = review.tags if review and review.decision == "accepted" else ()
            # 渠道行业只是来源配置，不能推断每篇文章的主题。
            legacy_tags = (industries if view is not None else ()) + ((values["industry"],) if "industry" in values else ())
            tags = tuple(dict.fromkeys((*ai_tags, *legacy_tags, *manual_tags)))
            projected.append(replace(
                entry, tags=tags, industry=industries[0] if industries else None,
                industry_values=industries, industry_source=industry_source,
                classification_status=status, classification_model=model,
                confirmed_values=tuple(sorted(values.items())), labels=tuple(sorted(values)),
                summary=summary, summary_origin=summary_origin, title_zh=title_zh,
                admission_status=admission_status, admission_reason=admission_reason,
                body_note=("入站审核：" + admission_reason + "；" if admission_status in ("pending", "rejected") else "") + body_note,
                body_text=saved_body.cleaned_text if saved_body else '',
            ))
        return tuple(projected)

    def load_document(entry):
        url = entry.link or entry.endpoint
        saved = read_article_body(db_path, url)
        if saved:
            raw, content_type = read_article_response(db_path, saved.id)
            return make_document(
                raw, content_type, saved.resolved_url or url,
                fallback_text=saved.cleaned_text,
                note="正文获取时受到长度限制，当前展示已留存部分。" if saved.truncated else "",
            )
        if not entry.from_feed:
            return make_document(access.content(entry.raw_id), "", url)
        source = access.brief_sources(entry.raw_id).get((entry.char_start, entry.char_end))
        # 订阅摘要作为回落，不把它标成已经取得完整正文。
        analysis = access._analyse(archive.get(entry.raw_id))
        fragment = analysis.text[entry.char_start:entry.char_end]
        excerpt = feed_excerpt(fragment, source.body if source else "")
        return make_document(excerpt, "", url, note="正文尚未留存，以下为来源提供的摘要。")

    reader = ReaderService(
        load_document, Path(cache_root) if cache_root is not None else Path(root).parent / "cache",
        image_cache=image_cache, client_factory=translation_client_factory,
    )

    def close_resources():
        reader.close()
        archive.close()

    return WebUIApplication(
        source,
        labels=labels,
        events=EventStore(Path(db_path).with_name("analytics.db")),
        industry_provider=industry_provider,
        raw_exists=raw_exists_from_source(source),
        actor=actor,
        entries_lookup=entries_of,
        locate_entry=locate_entry,
        text_length=text_length,
        entry_projection=project_entries,
        reader=reader,
        on_close=close_resources,
        host=host,
        port=port,
    )


# ---------------------------------------------------------------------- #
# 内部
# ---------------------------------------------------------------------- #
def _text_length(raw_bytes: bytes) -> int:
    """非 feed 原文的长度：按 T-104 的严格解码链算字符数（不是字节数）。"""
    return len(decode_bytes(raw_bytes, "")[0])


def _whole_document_entry(record: RawRecord, *, text_length: int) -> FeedEntry:
    """整篇即条目：区间 `[0, text_length)`，`entry_id=None`（没有条目层 ID）。"""
    return FeedEntry(
        entry_id=None,
        raw_id=record.raw_id,
        raw_sha256=record.content_sha256,
        title=title_from_endpoint(record.endpoint),
        link=record.endpoint,
        published_at=None,
        char_start=0,
        char_end=max(1, text_length),
        ordinal=0,
        from_feed=False,
        channel_id=record.channel_id,
        industry=None,
        endpoint=record.endpoint,
        content_sha256=record.content_sha256,
        byte_length=record.byte_length,
        fetched_at=record.fetched_at,
        http_status=record.http_status,
        labels=(),
    )


def _feed_entry(record: RawRecord, entry: Any) -> FeedEntry:
    """T-130 的 `Entry` → T-106 的 `FeedEntry`（**逐字段搬运，无任何推算**）。"""
    return FeedEntry(
        entry_id=entry.entry_id,
        raw_id=entry.raw_id,
        raw_sha256=entry.raw_sha256,
        title=entry.title,
        link=entry.link or record.endpoint,
        published_at=entry.published_at,
        char_start=entry.char_start,
        char_end=entry.char_end,
        ordinal=entry.index,
        from_feed=True,
        channel_id=record.channel_id,
        industry=None,
        endpoint=record.endpoint,
        content_sha256=record.content_sha256,
        byte_length=record.byte_length,
        fetched_at=record.fetched_at,
        http_status=record.http_status,
        labels=(),
    )


# ---------------------------------------------------------------------- #
# 命令行入口：python -m atlas.webapp
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class StoreSummary:
    """启动前对存储的一次**只读**盘点（用来在界面上说清"你到底在看什么"）。"""

    root: Path
    records: int
    containers: int
    derived_entries: int
    unavailable_records: int = 0

    @property
    def direct_entries(self) -> int:
        """本身就是条目的 Raw 数（整篇即一条）。"""
        return self.records - self.containers - self.unavailable_records

    @property
    def entries(self) -> int:
        """真实条目总数 = 派生条目 + 本身即条目的 Raw（SPEC §6.3 的 895 口径）。"""
        return self.derived_entries + self.direct_entries


def store_summary(root: Any) -> StoreSummary:
    """只读盘点一份归档：归档记录数 / 容器数 / 派生条目数。

    **不写任何字节**：只 `get_content` + `parse_entries`。归档句柄用完即关。
    """
    path = Path(root)
    archive = open_archive(path)
    try:
        access = ArchiveEntriesAccess(archive)
        containers = access.container_ids()
        derived = 0
        for raw_id in containers:
            derived += len(access.entries_of(archive.get(raw_id)).entries)
        return StoreSummary(
            root=path,
            records=len(archive.all_raw_ids()),
            containers=len(containers),
            derived_entries=derived,
            unavailable_records=sum(
                bool(access.entries_of(archive.get(raw_id)).unavailable_reason)
                for raw_id in archive.all_raw_ids()
            ),
        )
    finally:
        archive.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m atlas.webapp",
        description=(
            "Atlas 打标前端（T-109）：按**条目**浏览 feed 并一条一次点击打标。"
            "本进程只把界面摆出来——不采集、不抓网、不改配置。"
        ),
        epilog=(
            "安全说明：本服务可以写入人工标签，且**没有认证**（认证是 SPEC §5 登记 #10 "
            "的延后项）。因此默认只绑回环地址 127.0.0.1，不提供对外绑定的开关。"
        ),
    )
    parser.add_argument(
        "--store-root",
        default=os.environ.get(STORE_ROOT_ENV_VAR, DEFAULT_STORE_ROOT),
        help=f"存储根（默认 %(default)s；也可用 {STORE_ROOT_ENV_VAR} 覆盖）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "监听地址（默认 %(default)s，只对本机可见）。"
            "刻意不提供「绑定所有网卡」的便利开关：本服务能写标签且没有认证"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="监听端口（默认 %(default)s；0 = 让内核分配一个空闲端口）",
    )
    parser.add_argument(
        "--actor",
        default=DEFAULT_ACTOR,
        help="打标判断的署名（写进 ConfirmedLabel.actor；默认 %(default)s）",
    )
    return parser


def render_startup(
    summary: StoreSummary, *, url: str, actor: str, host: str, port: int
) -> str:
    """启动横幅：把**去哪儿看**、**在看什么**、**怎么退出**一次说清。"""
    lines = [
        f"Atlas 打标前端已启动：{url}",
        f"  存储根：{summary.root}",
        (
            f"  归档记录 {summary.records} 条 = 容器 {summary.containers} 条"
            f"（不作为条目出现）+ 本身即条目 {summary.direct_entries} 条"
        ),
        (
            f"  可浏览条目：{summary.entries} 条"
            f"（容器派生 {summary.derived_entries} + 直接 {summary.direct_entries}）"
        ),
        f"  打标署名（actor）：{actor}（标签展示与已打标筛选仅使用此署名的判断）",
        f"  Ctrl-C 退出（默认只监听 {host}，本机可见；服务无认证）",
    ]
    if port == 0:
        lines.insert(1, "  （--port 0 ⇒ 端口由内核分配，上面的 URL 是实际端口）")
    return "\n".join(lines)


def _fail(message: str) -> int:
    """响亮失败：写到 stderr 并返回非零退出码（绝不静默起一个空界面）。"""
    print(message, file=sys.stderr)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not 0 <= args.port <= 65535:
        return _fail(f"--port 必须在 [0, 65535]，收到 {args.port}")
    if not str(args.actor).strip():
        return _fail("--actor 不得为空：人工标签必须能追溯到人")

    root = Path(args.store_root)
    if not root.is_dir():
        return _fail(
            f"存储根不存在：{root}\n"
            "先跑一次采集把归档建出来，例如：\n"
            f"  PYTHONPATH=src ./.venv-new/bin/python -m atlas.compose run --store-root {root}\n"
            "（真实采集还需要 ATLAS_LIVE=1；只想看看计划可以先跑 `... -m atlas.compose plan`。）"
        )

    try:
        summary = store_summary(root)
    except Exception as exc:  # noqa: BLE001 - 如实报错，不吞
        return _fail(
            f"读取存储失败：{type(exc).__name__}: {exc}\n"
            f"（存储根 {root}；确认它是一份完整的 atlas 归档）"
        )
    if summary.records == 0:
        return _fail(
            f"存储根里没有任何归档记录：{root}\n"
            "空 feed 不是「功能坏了」，是**还没有采集**。先跑：\n"
            f"  PYTHONPATH=src ./.venv-new/bin/python -m atlas.compose run --store-root {root}"
        )

    db_path = root / "atlas.db"
    enable_wal(db_path)
    # 行业取值集合来自**配置**（SPEC §2.5 的 C8 闭环），不硬编码任何行业名。
    registry = RegistryService(open_registry_store(db_path, author=str(args.actor)))
    industry_of = {
        channel.id: channel.industry_id for channel in registry.list_channels()
    }

    print(
        f"配置：行业 {len(registry.label_space())} 个 / 渠道 {len(industry_of)} 个"
        f"（来自 {db_path} 的注册表）"
    )
    app: Optional[WebUIApplication] = None
    try:
        app = build_webapp(
            root,
            db_path=db_path,
            industry_provider=registry.label_space,
            industry_of=industry_of,
            actor=str(args.actor),
            host=args.host,
            port=args.port,
        )
        print(
            render_startup(
                summary,
                url=app.base_url,
                actor=str(args.actor),
                host=app.host,
                port=args.port,
            ),
            flush=True,
        )
        _block_until_interrupted()
        print("\n收到中断，正在关闭…", flush=True)
    except KeyboardInterrupt:  # pragma: no cover - 交互路径
        print("\n收到中断，正在关闭…", flush=True)
    finally:
        if app is not None:
            app.close()
        registry.store.close()
    return 0


def _block_until_interrupted() -> None:
    """阻塞到 Ctrl-C（超时轮询而非裸 `wait()`：裸等待在部分平台不可中断）。"""
    stop = threading.Event()
    while not stop.wait(0.5):  # pragma: no cover - 交互路径
        pass


if __name__ == "__main__":
    raise SystemExit(main())
