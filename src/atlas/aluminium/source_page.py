"""信源管理的紧凑只读视图；筛选留在浏览器，配置修改仍使用既有表单。"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from atlas.webui.pages import _safe_href, _shell, escape

from . import INDUSTRY_NAME
from .models import INTERVALS, Source, collection_source, probe_verified


@dataclass(frozen=True)
class RegisteredSource:
    """通用渠道的展示字段；不强加行业目录特有的优先级、地区或验证状态。"""

    name: str
    url: str
    mode: str
    ingestion: str
    focus: str
    priority: str = ""
    region: str = ""
    type: str = "注册表渠道"
    status: str = "未单独验证"


def registered_source_view(
    channel, *, name, group, industry, latest=None, success=None
):
    mode = {"rss": "rss", "atom": "atom", "json_api": "api", "html_xpath": "web"}[
        channel["type"]
    ]
    mode_label = {"rss": "RSS 订阅", "atom": "Atom 订阅", "api": "API", "web": "网页"}[
        mode
    ]
    source = RegisteredSource(
        name=name,
        url=channel["endpoint"],
        mode=mode,
        ingestion=channel["type"],
        focus="渠道标签：" + " / ".join(channel.get("tags", ()))
        if channel.get("tags")
        else "未配置监控说明",
    )
    failed = (
        latest and latest["http_status"] is not None and latest["http_status"] >= 400
    )
    reason = f"最近归档响应为 HTTP {latest['http_status']}" if failed else None
    return {
        "source": source,
        "row": {
            "source_id": channel["id"],
            "last_success_at": success["fetched_at"] if success else None,
        },
        "probe": None,
        "channel": channel,
        "last": {"attempted_at": latest["fetched_at"]} if latest else None,
        "enabled": channel["enabled"],
        "allow_toggle": True,
        "registered": True,
        "state": "不可用" if failed else "已归档" if latest else "已配置",
        "reason": reason,
        "tone": "error" if failed else "verified" if latest else "neutral",
        "mode": mode_label,
        "group": group,
        "industry": industry,
        "note": reason or ("有历史归档记录" if latest else "尚无归档记录"),
    }


def _time(value, empty="尚未成功"):
    if not value:
        return f'<span class="source-muted">{escape(empty)}</span>'
    local = datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai"))
    return (
        f'<time datetime="{escape(value)}" title="{escape(value)}">'
        f"{local:%m-%d %H:%M}</time>"
    )


def _source_view(row, channels):
    source = Source.model_validate({key: row[key] for key in Source.model_fields})
    effective = collection_source(source, row.get("connection"))
    probe, last = row["probe"], row["last_attempt"]
    channel = channels.get(row["channel_id"])
    verified = probe_verified(probe, row.get("connection"), channel)
    enabled = bool(channel and channel["enabled"])
    if effective.pending_reason:
        state = "需要授权" if source.mode == "paid" else "待配置"
    elif last:
        state = "运行正常" if last["successful"] else "不可用"
    elif probe:
        state = "已验证" if verified else "不可用"
    else:
        state = "已配置" if channel else "待配置"
    reason = (
        effective.pending_reason
        or (last["error"] if last and not last["successful"] else None)
        or (probe["reason"] if probe and not verified else None)
    )
    tone = {
        "运行正常": "good",
        "已验证": "verified",
        "不可用": "error",
        "需要授权": "pending",
        "待配置": "pending",
        "已配置": "neutral",
    }[state]
    mode = {
        "rss": "RSS 订阅",
        "web": "网页",
        "paid": "付费情报",
        "email": "邮件订阅",
        "rss_directory": "RSS 目录",
        "api_pending": "API",
    }[effective.mode]
    if reason:
        note = (
            "robots 访问限制"
            if "robots" in reason.lower()
            else "等待订阅授权"
            if source.mode == "paid"
            else "等待配置"
            if effective.pending_reason
            else "采集或验证失败"
        )
    else:
        note = (
            "实际采集成功"
            if last and last["successful"]
            else "连通及结构已验证"
            if verified
            else "尚未完成采集"
        )
    return {
        "source": source,
        "row": row,
        "probe": probe,
        "last": last,
        "channel": channel,
        "enabled": enabled,
        "allow_toggle": bool(channel and verified and not effective.pending_reason),
        "collection_mode": effective.mode,
        "state": state,
        "reason": reason,
        "tone": tone,
        "mode": mode,
        "note": note,
        "group": INDUSTRY_NAME,
        "industry": INDUSTRY_NAME,
    }


def _source_row(view, version):
    source, row = view["source"], view["row"]
    probe, last, channel = view["probe"], view["last"], view["channel"]
    sid = row["source_id"]
    detail_id = f"source-detail-{sid}"
    enabled, reason = view["enabled"], view["reason"]
    allowed = (
        view["allow_toggle"]
        if "allow_toggle" in view
        else bool(
            channel and probe and probe["collectible"] and not source.pending_reason
        )
    )
    search = " ".join(
        (
            source.name,
            source.url,
            source.region,
            source.focus,
            view["industry"],
            view["group"],
        )
    )
    priority = source.priority or "unassigned"
    priority_label = source.priority or "—"
    parts = [
        f'<tr class="source-row" data-source-id="{escape(sid)}" data-search="{escape(search.lower())}" '
        f'data-state="{escape(view["state"])}" data-mode="{escape(view.get("collection_mode", source.mode))}" '
        f'data-priority="{priority}" data-enabled="{int(enabled)}" data-industry="{escape(view["group"])}">',
        '<th scope="row" class="source-identity">'
        f'<a class="source-name" href="{_safe_href(source.url)}" target="_blank" rel="noreferrer noopener">{escape(source.name)}</a>'
        f'<div class="source-location"><span class="source-mobile-priority">{priority_label}</span>'
        f'<span>{escape(urlsplit(source.url).hostname)}</span><span class="source-region">{escape(source.region or view["industry"])}</span></div></th>',
        f'<td class="source-mode"><span>{escape(view["mode"])}</span>'
        + (
            "<small>页面快照</small>"
            if probe and probe.get("structure") == "html"
            else ""
        )
        + "</td>",
        f'<td class="source-priority"><span class="priority-{priority.lower()}" title="{source.priority or "未设置优先级"}">{priority_label}</span></td>',
        f'<td class="source-health"><span class="source-state source-state-{view["tone"]}">{escape(view["state"])}</span>'
        f'<small title="{escape(reason or view["note"])}">{escape(view["note"])}</small></td>',
        f'<td class="source-time">{_time(row["last_success_at"])}<small class="source-mobile-time">最近成功</small></td>',
        '<td class="source-actions"><div class="source-action-group">',
        f'<button class="source-detail-toggle" type="button" aria-expanded="false" aria-controls="{escape(detail_id)}" aria-label="查看 {escape(source.name)} 的详情">详情<span aria-hidden="true">⌄</span></button>',
    ]
    if enabled or allowed:
        action = "禁用" if enabled else "启用"
        parts.append(
            f'<form method="post" action="/sources/toggle"><input type="hidden" name="source_id" value="{escape(sid)}">'
            f'<input type="hidden" name="enabled" value="{0 if enabled else 1}"><input type="hidden" name="version" value="{escape(version)}">'
            f'<button class="source-switch" type="submit" role="switch" aria-checked="{str(enabled).lower()}" aria-label="{action} {escape(source.name)}">'
            f'<span class="source-switch-track" aria-hidden="true"></span><span>{"已启用" if enabled else "已禁用"}</span></button></form>'
        )
    else:
        parts.append(
            f'<button class="source-switch" type="button" role="switch" aria-checked="false" disabled '
            f'aria-label="{escape(source.name)}：完成配置及验证后可启用" title="{escape(reason or "完成配置及验证后可启用")}">'
            '<span class="source-switch-track" aria-hidden="true"></span><span>未启用</span></button>'
        )
    interval = channel["interval_seconds"] if channel else INTERVALS[source.priority]
    schedule = f"每 {interval / 3600:g} 小时" + ("" if channel else "（建议）")
    fields = [
        ("监控内容", escape(source.focus)),
        (
            "原始 URL",
            f'<a href="{_safe_href(source.url)}" target="_blank" rel="noreferrer noopener">{escape(source.url)}</a>',
        ),
        ("接入方式", escape(source.ingestion)),
        ("行业", escape(view["industry"])),
        ("地区", escape(source.region or "未设置")),
        ("来源类型", escape(source.type)),
        ("采集周期", escape(schedule)),
        (
            "最近归档响应（非完整尝试记录）"
            if view.get("registered")
            else "最近采集尝试",
            _time(last["attempted_at"] if last else None, "尚未采集"),
        ),
        (
            "最近成功响应（HTTP 2xx）" if view.get("registered") else "最近成功",
            _time(row["last_success_at"]),
        ),
        ("最近结构验证", _time(probe.get("tested_at") if probe else None, "尚未验证")),
        ("原始验证状态", escape(source.status)),
    ]
    if row.get("connection"):
        endpoint = row["connection"]["endpoint"]
        fields.append(
            (
                "实际 RSS 订阅",
                f'<a href="{_safe_href(endpoint)}" target="_blank" rel="noreferrer noopener">{escape(endpoint)}</a>',
            )
        )
    parts.append(
        f'</div></td></tr><tr id="{escape(detail_id)}" class="source-detail-row" hidden><td colspan="6"><div class="source-detail-panel">'
    )
    if reason:
        parts.append(
            f'<p class="source-reason"><strong>需要处理</strong><span>{escape(reason)}</span></p>'
        )
    if probe and probe.get("structure") == "html":
        parts.append(
            f'<p class="source-detail-note">页面监控检测到 {probe.get("article_links", 0)} 个候选新闻链接，新闻相关性仍需逐条筛选。</p>'
        )
    parts.append(
        '<dl class="source-metadata">'
        + "".join(
            f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in fields
        )
        + "</dl></div></td></tr>"
    )
    return "".join(parts)


def _events(observations):
    current = [
        e
        for e in observations
        if e["id"] == e["preferred_id"] and not e.get("is_superseded")
    ]
    parts = [
        f'<details id="intelligence" class="source-intelligence"><summary><span>产能事件</span><span class="source-count">{len(current)}</span><span class="source-event-hint">查看结构化结果与证据</span></summary>',
        '<p class="source-section-note">仅明确的海外原铝实际事件具备统计资格；名义产能、运行产能与产量分别保留，不自动相加。</p>',
    ]
    if not current:
        parts.append(
            '<p class="source-empty-events">尚无完成结构化提取的事件。采集记录和待提取候选均已保留。</p>'
        )
    for event in current:
        data = event["data"]
        parts.append(
            f'<article class="source-event"><h2>{escape(data["title_zh"])}</h2><p class="source-muted" lang="en">{escape(event["original_title"])}</p>'
            f'<p>{escape(data["summary_zh"])}</p><p class="source-event-meta">{escape(data["event_type"] or "未确定事件")} · {escape(data["business_type"])} · {escape(data["event_status"])}</p>'
            f"<details><summary>字段、证据与来源</summary><pre>{escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"
            f"<p>关联项目：{escape(event['project_id'] or '待关联')}；替代记录：{escape(event['supersedes'] or '无')}</p>"
        )
        parts.extend(
            f'<p><a href="{_safe_href(url)}" rel="noreferrer">{escape(url)}</a></p>'
            for url in event["related_sources"]
        )
        parts.append("</details></article>")
    history = [e for e in observations if e not in current]
    if history:
        parts.append(
            f'<details class="source-history"><summary>其他报道与历史版本（{len(history)}）</summary>'
        )
        for event in history:
            parts.append(
                f'<p><a href="{_safe_href(event["source_url"])}">{escape(event["original_title"])}</a></p><pre>{escape(json.dumps(event["data"], ensure_ascii=False, indent=2))}</pre>'
            )
        parts.append("</details>")
    parts.append("</details>")
    return "".join(parts)


def render_sources(
    rows,
    channels,
    version,
    observations,
    message="",
    *,
    registered=(),
    show_intelligence=True,
):
    views = [_source_view(row, channels) for row in rows] + list(registered)
    groups = sorted(
        {v["group"] for v in views},
        key=lambda name: (
            0
            if name == "AI 信源"
            else 1
            if name == INDUSTRY_NAME
            else 3
            if name == "其他来源"
            else 2,
            name,
        ),
    )
    views.sort(
        key=lambda view: (
            view["source"].priority or "~",
            view["source"].name.casefold(),
        )
    )
    counts = Counter(view["state"] for view in views)
    filters = [
        ("all", "全部信源", len(views)),
        ("enabled", "已启用", sum(v["enabled"] for v in views)),
        ("issues", "不可用", counts["不可用"]),
        ("pending", "待配置 / 授权", counts["待配置"] + counts["需要授权"]),
    ]
    parts = [
        '<header class="sources-header"><nav aria-label="页面导航"><a href="/feed">← 返回信息流</a><span aria-hidden="true">/</span><span>信源管理</span></nav>',
        '<div class="sources-title"><div><h1>信源管理</h1>'
        '<p><span class="sources-industry">按行业分组管理</span><span>查看采集状态与启停设置</span></p></div>'
        + (
            '<a class="source-events-link" href="#intelligence">产能事件 <span aria-hidden="true">↓</span></a>'
            if show_intelligence
            else ""
        )
        + "</div></header>",
        '<main><section id="sources" aria-label="信源目录">',
    ]
    if message:
        parts.append(f'<p class="source-feedback" role="alert">{escape(message)}</p>')
    parts.append(
        '<div class="source-filter-tabs" role="group" aria-label="按采集状态筛选">'
    )
    for value, label, count in filters:
        parts.append(
            f'<button type="button" data-source-filter="{value}" aria-pressed="{str(value == "all").lower()}">{label}<span class="source-count">{count}</span></button>'
        )
    parts.extend(
        [
            '</div><div class="source-toolbar"><label class="source-search"><span class="source-sr-only">搜索信源名称、域名、地区或监控内容</span><input type="search" id="source-search" placeholder="搜索名称、域名或地区…" autocomplete="off"></label>',
            '<label class="source-industry-control"><span class="source-sr-only">行业分组</span><select id="source-industry"><option value="">全部行业</option>'
            + "".join(
                f'<option value="{escape(group)}">{escape(group)}（{sum(v["group"] == group for v in views)}）</option>'
                for group in groups
            )
            + "</select></label>",
            '<label><span class="source-sr-only">接入方式</span><select id="source-mode"><option value="">所有接入方式</option><option value="web">网页</option><option value="rss">RSS 订阅</option><option value="atom">Atom 订阅</option><option value="rss_directory">RSS 目录</option><option value="email">邮件订阅</option><option value="api">API</option><option value="api_pending">API · 待配置</option><option value="paid">付费情报</option></select></label>',
            '<label><span class="source-sr-only">优先级</span><select id="source-priority"><option value="">所有优先级</option><option>P0</option><option>P1</option><option>P2</option><option value="unassigned">未设置</option></select></label></div>',
            f'<div class="source-list-caption"><p id="source-result-count" role="status" aria-live="polite">共 {len(views)} 个来源</p><span>时间均为北京时间</span></div>',
            '<div class="source-table-wrap"><table class="source-table"><caption class="source-sr-only">全部信源按行业分组，组内按优先级及名称排序</caption><thead><tr><th scope="col">信源 / 地区</th><th scope="col">接入方式</th><th scope="col">优先级</th><th scope="col">采集状态</th><th scope="col">最近成功</th><th scope="col" class="source-actions-heading">操作 / 启用</th></tr></thead>',
        ]
    )
    for group in groups:
        members = [v for v in views if v["group"] == group]
        parts.append(
            f'<tbody class="source-industry-group" data-industry-group="{escape(group)}"><tr class="source-group-heading"><th colspan="6" scope="rowgroup"><div class="source-group-label"><span>{escape(group)}</span><span class="source-count source-group-count">{len(members)}</span></div></th></tr>'
        )
        parts.extend(_source_row(view, version) for view in members)
        parts.append("</tbody>")
    parts.append(
        '</table><div class="source-filter-empty" hidden><strong>没有匹配的信源</strong><p>试试其他关键词，或清除当前筛选。</p><button type="button" id="source-reset">清除筛选</button></div></div>'
    )
    parts.append(
        '<p class="source-status-note">已验证：连通及结构测试通过；运行正常：实际采集成功。已归档仅表示有历史响应，不代表当前已验证。启停状态单独展示。</p></section>'
    )
    if show_intelligence:
        parts.append(_events(observations))
    parts.append(
        "</main><noscript><style>.source-detail-row[hidden]{display:table-row!important}.source-detail-toggle,.source-toolbar,.source-filter-tabs{display:none!important}</style></noscript>"
    )
    return _shell(
        title="信源管理 · Atlas",
        heading="信源管理",
        body="<style>" + STYLE + "</style>" + "".join(parts) + SCRIPT,
        body_class="source-management-page",
    )


STYLE = """
body.source-management-page { max-width: 1280px; padding: 24px 32px 40px; font-size: 14px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif; }
.source-management-page > h1 { display: none; }
.source-management-page a { text-decoration: none; }
.source-management-page a:hover { text-decoration: underline; }
.source-management-page [hidden] { display: none !important; }
.sources-header nav { display: flex; align-items: center; gap: 12px; color: var(--muted); font-size: 13px; }
.sources-header nav a { color: var(--muted); }
.sources-title { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 22px 0 24px; }
.sources-title h1 { margin: 0 0 6px; font-size: 28px; font-weight: 650; letter-spacing: -.04em; }
.sources-title p { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin: 0; color: var(--muted); font-size: 12px; }
.sources-industry { color: var(--text); font-size: 14px; font-weight: 550; }
.source-events-link { display: flex; align-items: center; justify-content: center; gap: 12px; min-height: 44px; white-space: nowrap; }
.source-filter-tabs { display: flex; gap: 24px; border-bottom: 1px solid var(--border); }
.source-management-page .source-filter-tabs button { display: flex; align-items: center; gap: 8px; padding: 10px 0 12px; min-height: 48px; border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: transparent; color: var(--muted); font-size: 13px; white-space: nowrap; flex-shrink: 0; }
.source-filter-tabs button[aria-pressed=true] { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.source-count { padding: 1px 7px; border-radius: 5px; background: var(--neutral-bg); font-size: 12px; font-variant-numeric: tabular-nums; color: var(--muted); }
.source-filter-tabs button[aria-pressed=true] .source-count { background: var(--tint); color: var(--accent); }
.source-toolbar { display: flex; align-items: center; gap: 10px; margin-top: 18px; }
.source-toolbar label { min-width: 0; }
.source-search { flex: 1; }
.source-toolbar input, .source-toolbar select { min-height: 42px; height: 42px; width: 100%; border: 1px solid var(--border); background: var(--surface); color: var(--text); border-radius: 7px; padding: 8px 12px; font-size: 13px; }
.source-toolbar input:focus-visible, .source-toolbar select:focus-visible { outline-width: 2px; outline-offset: 2px; }
.source-toolbar select { min-width: 130px; }
.source-list-caption { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 12px; padding: 12px 0 8px; }
.source-list-caption p { margin: 0; }
.source-table-wrap { border: 1px solid var(--border); border-radius: 9px; overflow: hidden; background: var(--surface); }
.source-table { border-collapse: collapse; width: 100%; text-align: left; table-layout: fixed; }
.source-table thead { background: var(--neutral-bg); color: var(--muted); }
.source-table thead th { padding: 10px 14px; font-size: 12px; font-weight: 500; }
.source-table thead th:nth-child(1) { width: 33%; }
.source-table thead th:nth-child(2) { width: 11%; }
.source-table thead th:nth-child(3) { width: 8%; }
.source-table thead th:nth-child(4) { width: 16%; }
.source-table thead th:nth-child(5) { width: 13%; }
.source-table thead th:nth-child(6) { width: 19%; }
.source-group-heading th { padding: 9px 14px; background: var(--bg); border-top: 1px solid var(--border); color: var(--text); font-size: 12px; font-weight: 600; }
.source-group-label { display: flex; align-items: center; gap: 8px; }
.source-row > th, .source-row > td { padding: 8px 14px; border-top: 1px solid var(--border); vertical-align: middle; font-size: 13px; font-weight: 400; }
.source-row:hover { background: var(--bg); }
.source-name { color: var(--text); font-weight: 600; line-height: 1.5; display: block; overflow-wrap: anywhere; }
.source-location { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0 9px; margin-top: 3px; color: var(--muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.source-region { color: var(--muted); }
.source-row small { display: block; margin-top: 3px; font-size: 11px; line-height: 1.4; color: var(--muted); }
.source-mobile-priority, .source-row .source-mobile-time { display: none; }
.source-priority span { font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
.priority-p0 { color: var(--text); }.priority-p1,.priority-p2 { color: var(--muted); }
.source-state { display: flex; align-items: center; gap: 6px; font-size: 12px; white-space: nowrap; }
.source-state::before { content: ''; width: 6px; height: 6px; flex: 0 0 6px; border-radius: 50%; background: currentColor; }
.source-state-good { color: var(--valid); }.source-state-verified { color: var(--accent); }
.source-state-error { color: var(--invalid); }.source-state-pending { color: var(--history); }.source-state-neutral { color: var(--muted); }
.source-time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.source-muted { color: var(--muted); }
.source-action-group { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.source-actions form { margin: 0; }
.source-management-page .source-action-group .source-detail-toggle, .source-management-page .source-action-group .source-switch { display: flex; align-items: center; gap: 6px; min-height: 44px; border: 0; padding: 4px 0; background: transparent; color: var(--muted); border-radius: 4px; font-size: 12px; white-space: nowrap; }
.source-detail-toggle { min-width: 44px; }.source-detail-toggle[aria-expanded=true] { color: var(--accent); }
.source-detail-toggle[aria-expanded=true] span { transform: rotate(180deg); }
.source-actions-heading { text-align: right; }
.source-switch-track { display: block; width: 27px; height: 16px; padding: 2px; border-radius: 20px; background: var(--muted); }
.source-switch-track::after { content: ''; display: block; width: 12px; height: 12px; border-radius: 50%; background: var(--surface); }
.source-switch[aria-checked=true] .source-switch-track { background: var(--accent); }
.source-switch[aria-checked=true] .source-switch-track::after { transform: translateX(11px); }
.source-management-page .source-action-group .source-switch[aria-checked=true] { color: var(--text); }
.source-management-page .source-switch:disabled { opacity: .55; cursor: not-allowed; }
.source-detail-row > td { padding: 0; }
.source-detail-panel { background: var(--bg); padding: 18px 22px 20px; border-top: 1px solid var(--border); }
.source-metadata { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px 24px; margin: 0; }
.source-metadata > div:first-child, .source-metadata > div:nth-child(2) { grid-column: 1 / -1; }
.source-metadata dt { font-size: 11px; color: var(--muted); margin-bottom: 3px; }
.source-metadata dd { margin: 0; font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.source-detail-note { margin: 0 0 14px; color: var(--muted); font-size: 12px; }
.source-reason { display: flex; align-items: baseline; gap: 12px; color: var(--invalid); font-size: 12px; margin: 0 0 16px; overflow-wrap: anywhere; }
.source-reason strong { flex-shrink: 0; font-weight: 600; }
.source-status-note { color: var(--muted); font-size: 12px; margin: 12px 0 0; }
.source-feedback { padding: 12px 16px; background: var(--history-bg); color: var(--history); border: 1px solid var(--border); border-radius: 6px; }
.source-filter-empty { padding: 48px 20px; text-align: center; }.source-filter-empty p { color: var(--muted); }
.source-management-page .source-filter-empty button { background: var(--surface); color: var(--accent); border-color: var(--border); }
.source-intelligence { margin-top: 32px; border-top: 1px solid var(--border); }
.source-intelligence > summary { display: flex; gap: 10px; align-items: center; padding: 16px 0; font-size: 16px; font-weight: 600; }
.source-intelligence > summary::after { content: '+'; margin-left: auto; font-size: 22px; color: var(--muted); font-weight: 400; }
.source-intelligence[open] > summary::after { content: '−'; }
.source-event-hint { font-size: 12px; color: var(--muted); font-weight: 400; }
.source-section-note { font-size: 13px; color: var(--muted); margin: 0 0 16px; }
.source-event { padding: 18px 0; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
.source-event h2 { margin: 0 0 8px; font-size: 16px; }.source-event p { margin: 6px 0; font-size: 13px; line-height: 1.8; }
.source-event-meta { color: var(--muted); font-size: 12px; }.source-event summary { font-size: 12px; color: var(--accent); }
.source-intelligence pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 16px; background: var(--neutral-bg); font-size: 12px; border-radius: 6px; }
.source-history { padding: 16px 0; }.source-history p { overflow-wrap: anywhere; }
.source-sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.source-management-page > footer { margin-top: 36px; color: var(--muted); font-size: 11px; }
@media (max-width: 1000px) and (min-width: 701px) {
  body.source-management-page { padding: 20px; }
  .source-table thead th, .source-row > td, .source-row > th { padding-inline: 10px; }
  .source-table thead th:nth-child(1) { width: 28%; }.source-table thead th:nth-child(6) { width: 24%; }
  .source-location .source-region { display: none; }
}
@media (max-width: 700px) {
  body.source-management-page { padding: 20px 16px 32px; }
  .sources-title { padding: 20px 0; gap: 10px; }.sources-title h1 { font-size: 25px; }
  .sources-title p { font-size: 11px; }.sources-title p > span:last-child { flex-basis: 100%; }
  .source-events-link { font-size: 12px; gap: 5px; }
  .source-filter-tabs { gap: 8px; justify-content: space-between; overflow-x: auto; }.source-management-page .source-filter-tabs button { gap: 5px; font-size: 12px; }
  .source-count { padding-inline: 5px; font-size: 11px; }
  .source-toolbar { flex-wrap: wrap; gap: 8px; margin-top: 14px; }.source-search { flex: 1 0 100%; }
  .source-toolbar label:not(.source-search) { flex: 1; }.source-toolbar input, .source-toolbar select { min-height: 44px; height: 44px; }
  .source-toolbar label.source-industry-control { flex: 1 0 100%; }
  .source-list-caption { font-size: 11px; }
  .source-table, .source-table tbody { display: block; }.source-table thead { display: none; }
  .source-group-heading { display: block; }.source-group-heading th { display: block; text-align: left; padding: 10px 0; }
  .source-table-wrap { border-inline: 0; border-radius: 0; background: transparent; }
  .source-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0 10px; padding: 12px 0 4px; border-top: 1px solid var(--border); }
  .source-row:first-child { border-top: 0; }.source-row > th, .source-row > td { border: 0; padding: 0; }
  .source-row .source-identity { grid-column: 1; grid-row: 1; text-align: left; }
  .source-row .source-health { grid-column: 2; grid-row: 1; text-align: right; padding-top: 1px; }
  .source-health .source-state { justify-content: flex-end; }.source-health small { font-size: 10px; }
  .source-row .source-time { grid-column: 1; grid-row: 2; align-self: center; font-size: 12px; }
  .source-row .source-actions { grid-column: 2; grid-row: 2; }
  .source-action-group { gap: 12px; }.source-actions .source-switch { min-width: 78px; }
  .source-row .source-mode, .source-row .source-priority { display: none; }
  .source-mobile-priority { display: block; font-weight: 650; color: var(--text); }
  .source-name { font-size: 13px; }.source-location { gap: 0 7px; font-size: 10px; }.source-region { display: none; }
  .source-row .source-mobile-time { display: inline; font-size: 10px; margin-left: 7px; }
  .source-detail-row { display: block; }.source-detail-row > td { display: block; }
  .source-detail-panel { padding: 14px 12px; }.source-metadata { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .source-metadata dd { font-size: 12px; }.source-reason { display: grid; gap: 4px; }
  .source-status-note { line-height: 1.8; font-size: 11px; }.source-event-hint { display: none; }
}
"""

SCRIPT = """<script>
(() => {
  const rows = [...document.querySelectorAll('.source-row')];
  const search = document.querySelector('#source-search');
  const mode = document.querySelector('#source-mode');
  const priority = document.querySelector('#source-priority');
  const industry = document.querySelector('#source-industry');
  const filters = [...document.querySelectorAll('[data-source-filter]')];
  const key = 'atlas-source-filters';
  let selected = 'all';
  const update = () => {
    const query = search.value.trim().toLowerCase();
    const scoped = rows.filter(row => !industry.value || row.dataset.industry === industry.value);
    const counts = {
      all: scoped.length,
      enabled: scoped.filter(row => row.dataset.enabled === '1').length,
      issues: scoped.filter(row => row.dataset.state === '不可用').length,
      pending: scoped.filter(row => ['待配置', '需要授权'].includes(row.dataset.state)).length,
    };
    filters.forEach(button => button.querySelector('.source-count').textContent = counts[button.dataset.sourceFilter]);
    let count = 0;
    for (const row of rows) {
      const data = row.dataset;
      const matchesState = selected === 'all' || (selected === 'enabled' && data.enabled === '1')
        || (selected === 'issues' && data.state === '不可用')
        || (selected === 'pending' && ['待配置', '需要授权'].includes(data.state));
      const visible = matchesState && (!query || data.search.includes(query))
        && (!industry.value || data.industry === industry.value)
        && (!mode.value || data.mode === mode.value) && (!priority.value || data.priority === priority.value);
      row.hidden = !visible;
      const button = row.querySelector('.source-detail-toggle');
      document.getElementById(button.getAttribute('aria-controls')).hidden = !visible || button.getAttribute('aria-expanded') !== 'true';
      if (visible) count++;
    }
    filters.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.sourceFilter === selected)));
    for (const group of document.querySelectorAll('.source-industry-group')) {
      const visible = [...group.querySelectorAll('.source-row')].filter(row => !row.hidden).length;
      group.hidden = visible === 0;
      group.querySelector('.source-group-count').textContent = visible;
    }
    document.querySelector('#source-result-count').textContent = count === rows.length ? `共 ${rows.length} 个来源` : `显示 ${count} / ${rows.length} 个来源`;
    document.querySelector('.source-filter-empty').hidden = count !== 0;
    try { sessionStorage.setItem(key, JSON.stringify({selected, query: search.value, mode: mode.value, priority: priority.value, industry: industry.value})); } catch (_) {}
  };
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || '{}');
    selected = filters.some(b => b.dataset.sourceFilter === saved.selected) ? saved.selected : 'all';
    search.value = typeof saved.query === 'string' ? saved.query : '';
    mode.value = saved.mode || ''; priority.value = saved.priority || '';
    industry.value = saved.industry || '';
  } catch (_) {}
  for (const filter of filters) filter.addEventListener('click', () => { selected = filter.dataset.sourceFilter; update(); });
  search.addEventListener('input', update); mode.addEventListener('change', update); priority.addEventListener('change', update);
  industry.addEventListener('change', update);
  document.querySelector('#source-reset').addEventListener('click', () => { selected = 'all'; search.value = ''; mode.value = ''; priority.value = ''; industry.value = ''; update(); search.focus(); });
  for (const button of document.querySelectorAll('.source-detail-toggle')) {
    button.addEventListener('click', () => {
      const expanded = button.getAttribute('aria-expanded') !== 'true';
      button.setAttribute('aria-expanded', String(expanded));
      document.getElementById(button.getAttribute('aria-controls')).hidden = !expanded;
    });
  }
  document.querySelector('.source-events-link')?.addEventListener('click', () => { document.querySelector('#intelligence').open = true; });
  if (location.hash === '#intelligence' && document.querySelector('#intelligence')) document.querySelector('#intelligence').open = true;
  update();
})();
</script>"""
