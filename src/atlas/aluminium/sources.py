"""目录导入、结构验证及采集运行记录。复用现有版本化注册表和采集执行器。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from atlas.collect import Collector, RetryPolicy
from atlas.collect.executor import CollectionFailure, CollectionResult, ResultStatus
from atlas.collect.fetch import FailureKind
from atlas.entries import EntryParseError, parse_entries
from atlas.registry import open_store
from atlas.registry.schema import Channel, FetchSpec, FetchType, Industry
from atlas.registry.versioning import ChangeKind, RegistryMutation

from . import INDUSTRY_ID, INDUSTRY_NAME
from .models import INTERVALS, Manifest, Source, collection_source
from .rules import EVENT_KEYWORDS, INDUSTRY_KEYWORDS
from .store import AluminiumStore, canonical_url, digest, has_aluminium, source_id


def load_manifest(path, *, expected_count=37):
    manifest = Manifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if expected_count is not None and len(manifest.sources) != expected_count:
        raise ValueError(
            f"预期 {expected_count} 条信源，实际 {len(manifest.sources)} 条"
        )
    return manifest


def channel_for(source: Source, *, enabled=False):
    if source.mode == "rss":
        spec = FetchSpec(type=FetchType.RSS)
    else:
        # HTML 快照监控：确切选择器必须在 probe 中通过验证；不是虚构新闻列表。
        spec = FetchSpec(
            type=FetchType.HTML_XPATH,
            list_selector="body",
            title_selector="title",
            content_selector="body",
        )
    return Channel(
        id=source_id(source.url),
        industry_id=INDUSTRY_ID,
        type=spec.type,
        endpoint=source.url,
        fetch_spec=spec,
        enabled=enabled,
        interval_seconds=INTERVALS[source.priority],
        rate_limit_seconds=5,
        tags=(
            source.priority,
            "primary-aluminium",
            "snapshot" if source.mode != "rss" else "rss",
        ),
    )


def import_sources(path, db_path):
    # 在打开任何持久化写连接前完整校验。
    manifest = load_manifest(path)
    with open_store(db_path, author="aluminium-import") as registry:
        mutations = []
        if not any(i.id == INDUSTRY_ID for i in registry.current.industries):
            industry = Industry(
                id=INDUSTRY_ID,
                name=INDUSTRY_NAME,
                enabled=True,
                keywords=tuple(dict.fromkeys((*INDUSTRY_KEYWORDS, *EVENT_KEYWORDS))),
            )
            mutations.append(
                RegistryMutation(
                    kind="create", object_kind="industry", payload=industry.payload()
                )
            )
        existing = {canonical_url(c.endpoint): c.id for c in registry.current.channels}
        added_channels = 0
        for source in manifest.sources:
            url = canonical_url(source.url)
            if url not in existing and not source.pending_reason:
                channel = channel_for(source)
                mutations.append(
                    RegistryMutation(
                        kind="create", object_kind="channel", payload=channel.payload()
                    )
                )
                existing[url] = channel.id
                added_channels += 1
        if mutations:
            registry.commit(
                mutations,
                kind=ChangeKind.IMPORT,
                note="海外电解铝目录增量导入；新端点验证前保持禁用",
            )
        with AluminiumStore(db_path) as store:
            inserted = store.add_sources(manifest.sources, existing)
        return {
            "manifest_count": len(manifest.sources),
            "inserted_sources": inserted,
            "created_channels": added_channels,
            "config_version": registry.current_version,
        }


def configure_rss_source(db_path, sid, endpoint, *, browser=False):
    """将目录绑定到明确的公开 feed；幂等，验证前禁用，不改原始目录。"""
    with AluminiumStore(db_path, read_only=True) as store:
        row = store.source(sid)
        if not row or row["source_id"] != sid:
            raise ValueError("未知目录信源")
        original = Source.model_validate_json(row["original_json"])
        effective = collection_source(original, {"endpoint": endpoint})
        if canonical_url(endpoint) == canonical_url(original.url):
            raise ValueError("目录入口不能直接作为订阅流")
        if row["connection"] and canonical_url(
            row["connection"]["endpoint"]
        ) != canonical_url(endpoint):
            raise ValueError("信源已有其他订阅地址；不能覆盖已有配置")
    channel = Channel.model_validate({**channel_for(effective).payload(), "id": sid})
    browser_agent = None
    if browser:
        from atlas.collect.chromium import ChromiumFetcher

        browser_agent = ChromiumFetcher().user_agent()
    with open_store(db_path, author="aluminium-rss-config") as registry:
        existing = next((c for c in registry.current.channels if c.id == sid), None)
        duplicate = next(
            (
                c
                for c in registry.current.channels
                if canonical_url(c.endpoint) == canonical_url(endpoint) and c.id != sid
            ),
            None,
        )
        if duplicate:
            raise ValueError(f"该订阅地址已由渠道 {duplicate.id} 管理，未重复创建")
        if existing and (
            canonical_url(existing.endpoint) != canonical_url(endpoint)
            or existing.type != FetchType.RSS
            or existing.industry_id != INDUSTRY_ID
        ):
            raise ValueError("渠道已有其他配置，未覆盖")
        if not existing:
            if browser_agent:
                channel = Channel.model_validate(
                    {
                        **channel.payload(),
                        "user_agent": browser_agent,
                        "user_agent_justification": "实际桌面 Chromium 读取官方公开 RSS；使用浏览器自身身份，继续检查 robots 与限速，不执行挑战或登录",
                        "tags": (*channel.tags, "chromium"),
                    }
                )
            registry.commit(
                [
                    RegistryMutation(
                        kind="create", object_kind="channel", payload=channel.payload()
                    )
                ],
                note="RSS 目录配置公开订阅；验证前禁用，保留原始 URL",
            )
        elif browser_agent and (
            existing.user_agent != browser_agent or "chromium" not in existing.tags
        ):
            payload = {
                **existing.payload(),
                "user_agent": browser_agent,
                "user_agent_justification": "实际桌面 Chromium 读取官方公开 RSS；使用浏览器自身身份，继续检查 robots 与限速，不执行挑战或登录",
                "tags": tuple(dict.fromkeys((*existing.tags, "chromium"))),
                "enabled": False,
            }
            registry.commit(
                [
                    RegistryMutation(
                        kind="update",
                        object_kind="channel",
                        object_id=sid,
                        payload=payload,
                    )
                ],
                note="公开 RSS 改用已实测的真实桌面浏览器；重新验证后启用",
            )
            existing = Channel.model_validate(payload)
        with AluminiumStore(db_path) as store:
            store.connect_rss(sid, endpoint, channel.id)
        return {
            "source_id": sid,
            "original_url": original.url,
            "endpoint": endpoint,
            "created_channels": int(existing is None),
            "enabled": existing.enabled if existing else False,
            "config_version": registry.current_version,
        }


def page_documents(content: bytes, url: str):
    soup = BeautifulSoup(content, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for node in soup.select("script, style, nav, header, footer, noscript, form"):
        node.decompose()
    body = soup.select_one("main") or soup.body
    if not body:
        raise ValueError("HTML 缺少可解析的 body/main")
    text = body.get_text(" ", strip=True)
    if len(text) < 100 or not title:
        raise ValueError("HTML 标题或正文不足，可能是空壳/结构变化")
    if re.search(
        r"^(just a moment|access denied|attention required|robot or human|security check|verify you are human|request rejected)",
        title,
        re.I,
    ):
        raise ValueError("访问受限/验证码页面；未作为正常内容")
    documents = []
    seen = set()
    for a in body.select("a[href]"):
        link = urljoin(url, a.get("href", ""))
        label = a.get_text(" ", strip=True)
        p = urlsplit(link)
        attachment = p.path.lower().endswith(".pdf")
        news_container = a.find_parent(
            class_=re.compile(
                r"(?:news-content|pp-block-item|press-release|blog_info)", re.I
            )
        )
        if len(label) < 25 and news_container:
            heading = news_container.select_one("h2,h3,h4,p.text_reg")
            if heading:
                label = heading.get_text(" ", strip=True)
        # 只取本站可辨认的新闻详情链接，不把菜单、目录或外部接口当作文章。
        if p.scheme not in ("http", "https") or p.hostname != urlsplit(url).hostname:
            continue
        if (
            canonical_url(link) == canonical_url(url)
            or canonical_url(link) in seen
            or len(label) < 25
        ):
            continue
        if (
            not re.search(
                r"/(news|news-insights|releases?|press-releases?|insights?|media|article)/.+",
                p.path,
                re.I,
            )
            and not news_container
            and not attachment
        ):
            continue
        if p.path.lower().endswith((".jpg", ".png", ".zip")):
            continue
        seen.add(canonical_url(link))
        # 新闻链接自身已包住标题/摘要时，以它为边界；父容器可能包含整份列表。
        container = (
            a
            if len(a.get_text(" ", strip=True)) >= 25
            else (a.find_parent("article") or news_container or a.parent)
        )
        snippet = container.get_text(" ", strip=True) if container else label
        documents.append({"url": link, "title": label, "body": snippet})
    # 快照本身也是监控对象（统计表/产能基线不一定是文章列表）。
    return title, text, documents[:100]


def inspect_content(source: Source, content: bytes):
    if source.mode == "rss":
        feed = parse_entries(content, "", raw_id="raw_" + "0" * 64)
        if feed.kind not in ("rss", "atom"):
            raise ValueError("响应不是 RSS/Atom")
        # 使用系统严格 XML 解析器；HTML 错误页不能冒充 RSS。
        return {
            "structure": feed.kind,
            "entry_count": len(feed.entries),
            "monitor_mode": "feed",
            "content_hash": digest(content.decode("utf-8", errors="replace")),
        }
    title, body, documents = page_documents(content, source.url)
    return {
        "structure": "html",
        "title": title,
        "article_links": len(documents),
        "monitor_mode": "page_snapshot",
        "content_hash": digest(body),
    }


def probe_sources(
    db_path,
    dependencies,
    *,
    enable_verified=False,
    content_dir=None,
    on_result=None,
    source_ids=None,
):
    """每个目录项至多一次目标请求（robots/重定向除外）；无重试、无登录。"""
    report = []
    collector = Collector(
        fetcher=dependencies.fetcher,
        robots=dependencies.robots,
        throttle=dependencies.throttle,
        retry=RetryPolicy(max_attempts=1),
        timeout_seconds=12,
        sleeper=dependencies.sleeper,
        clock=dependencies.clock,
        user_agent_options=dependencies.user_agent_options,
    )
    with AluminiumStore(db_path) as store:
        rows = store.sources()
        if source_ids is not None:
            selected = set(source_ids)
            unknown = selected - {r["source_id"] for r in rows}
            if unknown:
                raise ValueError("未知信源：" + ", ".join(sorted(unknown)))
            rows = [r for r in rows if r["source_id"] in selected]
        for row in rows:
            original = Source.model_validate_json(row["original_json"])
            source = collection_source(original, row["connection"])
            registered = store.db.execute(
                "SELECT payload FROM channels WHERE channel_id=?", (row["channel_id"],)
            ).fetchone()
            channel = (
                Channel.model_validate({**json.loads(registered[0]), "enabled": True})
                if registered
                else channel_for(source, enabled=True)
            )
            # 测试的是给定公开入口，未配置接口或付费内容不会被请求。
            result = collector.collect(channel)
            check = {
                "source_id": row["source_id"],
                "channel_id": channel.id,
                "name": source.name,
                "url": original.url,
                "endpoint": channel.endpoint,
                "user_agent": channel.user_agent,
                "transport": "chromium" if "chromium" in channel.tags else "http",
                "reachable": result.ok,
                "collectible": False,
                "reason": source.pending_reason,
                "input_status": original.status,
                "http_status": result.raw.http_status
                if result.raw
                else result.failure.status_code
                if result.failure
                else None,
            }
            if result.ok:
                if content_dir:
                    directory = Path(content_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / (row["source_id"] + ".bin")).write_bytes(
                        result.content
                    )
                try:
                    check.update(inspect_content(source, result.content))
                except (ValueError, EntryParseError) as exc:
                    # 结构验证失败按原样保留诊断，不升级成采集成功。
                    check["reason"] = (
                        source.pending_reason + "；" if source.pending_reason else ""
                    ) + f"结构验证失败：{type(exc).__name__}: {exc}"
                else:
                    check["collectible"] = not bool(source.pending_reason)
            else:
                check["connection_error"] = (
                    result.failure.as_dict()
                    if result.failure
                    else {"status": result.status.value}
                )
                check["reason"] = (
                    source.pending_reason + "；" if source.pending_reason else ""
                ) + (result.failure.reason if result.failure else result.status.value)
            store.probe(row["source_id"], check)
            report.append(check)
            if on_result:
                on_result(check)
    if enable_verified:
        with open_store(db_path, author="aluminium-probe") as registry:
            mutations = []
            verified = {r["channel_id"] for r in report if r["collectible"]}
            # 只启用本次新增且有结构证据的渠道；已有重复端点的用户设置不修改。
            for channel in registry.current.channels:
                if (
                    channel.id in verified
                    and channel.industry_id == INDUSTRY_ID
                    and not channel.enabled
                ):
                    payload = channel.payload()
                    payload["enabled"] = True
                    mutations.append(
                        RegistryMutation(
                            kind="update",
                            object_kind="channel",
                            object_id=channel.id,
                            payload=payload,
                        )
                    )
            if mutations:
                registry.commit(
                    mutations,
                    note="实际 robots/连通性/结构验证通过后启用海外电解铝端点",
                )
    return report


def observe_result(db_path, result):
    """采集组合根回调：结构异常记为失败，失败不刷新成功时间。"""
    if not has_aluminium(db_path):
        return result
    with AluminiumStore(db_path) as store:
        row = store.source(result.channel_id)
        if (
            not row
            or row["channel_id"] != result.channel_id
            or result.status == ResultStatus.SKIPPED
        ):
            return result
        source = collection_source(
            Source.model_validate_json(row["original_json"]), row["connection"]
        )
        if result.ok:
            try:
                shape = inspect_content(source, result.content)
            except (ValueError, EntryParseError) as exc:
                failure = CollectionFailure(
                    channel_id=result.channel_id,
                    endpoint=result.endpoint,
                    kind=FailureKind.TRANSPORT,
                    reason=f"结构验证失败：{type(exc).__name__}: {exc}",
                    attempts=result.attempts,
                )
                result = CollectionResult(
                    channel_id=result.channel_id,
                    endpoint=result.endpoint,
                    status=ResultStatus.FAILED,
                    failure=failure,
                    attempts=result.attempts,
                )
            else:
                store.attempt(
                    row["source_id"],
                    result.channel_id,
                    successful=True,
                    content_hash=shape["content_hash"],
                    raw_id=result.raw.raw_id,
                )
                return result
        store.attempt(
            row["source_id"],
            result.channel_id,
            successful=False,
            error=json.dumps(result.failure.as_dict(), ensure_ascii=False),
        )
        return result
