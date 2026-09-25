"""T-109 服务端渲染的 HTML 页面（stdlib `string.Template` + `html.escape`）。

SPEC §2.11 的硬边界
-------------------

- **无 Web 框架、无 JS 框架、无构建步骤**：页面就是字符串，模板用 `string.Template`。
- 所有动态内容一律经 `escape()`（原文元数据来自外部站点，直接拼进 HTML 就是 XSS）。
- `href` 只允许 `http://` / `https://`，其它一律退化成 `#`（数据是外部输入，不可信）。

调用关系
--------

本模块**只做渲染**：入参是 `atlas.feed` 的 `FeedResult` 与已算好的标签视图，
不知道 HTTP、不知道数据库、不知道配置从哪来。行业列表由调用方（`app.py`）
从**注入的行业配置**取来后传进 `industries`，页面里**没有**任何行业名枚举。

标签维度常量
------------

`LABEL_KEY_VALID` / `LABEL_KEY_INDUSTRY` 是**维度名**（label_key），不是行业枚举：
前者是"有效/无效"的人工判断，后者的**取值集合完全来自配置**（SPEC §2.5 C8 闭环：
前端配的行业 = 打标时的修正对象）。`"industry"` 这个词同时是 T-106 的查询参数名
（`atlas.feed.query.KNOWN_PARAMS`），因此它的出现是**契约**而非硬编码枚举。
"""

from __future__ import annotations

import html
from string import Template
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlencode

from atlas.feed import FeedItem, FeedQuery, FeedResult

__all__ = [
    "LABEL_KEY_INDUSTRY",
    "LABEL_KEY_VALID",
    "LABEL_VALUE_INVALID",
    "LABEL_VALUE_VALID",
    "escape",
    "feed_query_string",
    "render_error_page",
    "render_feed_page",
]

#: 人工判断"有效/无效"的维度名。
LABEL_KEY_VALID = "valid"
#: 行业修正的维度名（取值集合来自注入的行业配置，本模块不持有任何行业名）。
LABEL_KEY_INDUSTRY = "industry"

LABEL_VALUE_VALID = "valid"
LABEL_VALUE_INVALID = "invalid"

#: 表单字段的长度上限（超限拒绝，不做静默截断）。
MAX_RAW_ID_LENGTH = 200
MAX_ACTOR_LENGTH = 64
MAX_LABEL_VALUE_LENGTH = 200

#: 送达 `<a href>` 的安全协议白名单。
_SAFE_HREF_PREFIXES = ("http://", "https://")

_KEEP = object()


def escape(value: Any) -> str:
    """HTML 转义（含引号）。所有动态内容都必须过这里。"""
    return html.escape(str(value), quote=True)


def _safe_href(url: Any) -> str:
    """只放行 http/https 的链接，其它（`javascript:` / `data:` …）退化成 `#`。"""
    text = str(url).strip()
    if text.startswith(_SAFE_HREF_PREFIXES):
        return escape(text)
    return "#"


# ---------------------------------------------------------------------- #
# 查询串（分页与「返回本页」用）
# ---------------------------------------------------------------------- #
def feed_query_string(query: FeedQuery, *, offset: int = 0, labeled: Any = _KEEP) -> str:
    """把 `FeedQuery` 投影回 URL 查询串（**只做投影**，筛选/排序语义仍归 `atlas.feed`）。

    `labeled` 用于「全部 / 已打标 / 未打标」的快捷切换；不传时沿用查询自身的值。
    """
    params: list[tuple[str, str]] = [
        ("order", query.order),
        ("limit", str(query.limit)),
        ("offset", str(offset)),
    ]
    effective_labeled = query.labeled if labeled is _KEEP else labeled
    if effective_labeled is not None:
        params.append(("labeled", "true" if effective_labeled else "false"))
    for industry in query.industries:
        params.append(("industry", industry))
    for channel in query.channels:
        params.append(("channel", channel))
    if query.labels:
        params.append(("labels", ",".join(query.labels)))
    if query.since is not None:
        params.append(("since", query.since.isoformat()))
    if query.until is not None:
        params.append(("until", query.until.isoformat()))
    return "/feed?" + urlencode(params)


# ---------------------------------------------------------------------- #
# 页面外壳
# ---------------------------------------------------------------------- #
_STYLE = """
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 0 auto; max-width: 62rem; padding: 1rem 1.25rem 5rem; line-height: 1.5; }
h1 { font-size: 1.35rem; margin: 0.5rem 0 0.75rem; }
h2 { font-size: 1rem; margin: 1.5rem 0 0.5rem; }
a { color: inherit; }
code, .raw-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }
.filters { border: 1px solid currentColor; border-radius: 6px; padding: 0.6rem 0.8rem;
           display: flex; flex-wrap: wrap; gap: 0.9rem; align-items: flex-end; }
.filters label { display: flex; flex-direction: column; font-size: 0.8rem; gap: 0.2rem; }
.filters select[multiple] { min-width: 10rem; }
.pager { display: flex; gap: 0.9rem; align-items: center; margin: 0.7rem 0; font-size: 0.85rem; }
.quick { font-size: 0.85rem; margin: 0.5rem 0; display: flex; gap: 0.6rem; }
.item { border: 1px solid currentColor; border-radius: 6px; padding: 0.6rem 0.8rem; margin: 0.6rem 0; }
.item .head { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: baseline; }
.item .meta, .item .labels { font-size: 0.8rem; opacity: 0.85; margin: 0.25rem 0; }
.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-top: 0.5rem; }
.actions form { display: flex; gap: 0.3rem; align-items: center; }
.label-tag { border: 1px solid currentColor; border-radius: 4px; padding: 0 0.3rem; margin-right: 0.3rem; }
.notice { border-left: 4px solid currentColor; padding: 0.4rem 0.6rem; margin: 0.8rem 0; }
.empty { opacity: 0.8; }
footer { margin-top: 2rem; font-size: 0.75rem; opacity: 0.7; }
"""

_SHELL = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>$style</style>
</head>
<body>
<h1>$heading</h1>
$body
<footer>Atlas T-109 打标前端 · 服务端渲染 · 无构建步骤 · 打标写入只经 atlas.labels 的 LabelStore</footer>
</body>
</html>
"""
)

#: 每一条信息一个「提交一个判断」的表单（**一条一次点击**，SPEC §2.1 文档级锚点）。
_ACTION_FORM = Template(
    """<form class="act" method="post" action="/label">
<input type="hidden" name="raw_id" value="$raw_id">
<input type="hidden" name="label_key" value="$label_key">
$value_field
<input type="hidden" name="actor" value="$actor">
<input type="hidden" name="return_to" value="$return_to">
<button type="submit">$caption</button>
</form>"""
)


def _shell(*, title: str, heading: str, body: str) -> str:
    return _SHELL.substitute(
        title=escape(title), heading=escape(heading), style=_STYLE, body=body
    )


# ---------------------------------------------------------------------- #
# feed 页面
# ---------------------------------------------------------------------- #
def render_feed_page(
    result: FeedResult,
    *,
    industries: Sequence[str],
    values: Mapping[str, Mapping[str, str]],
    actor: str,
    return_to: Optional[str] = None,
) -> str:
    """渲染一页 feed：筛选器 + 排序 + 分页 + 每条一次点击打标。

    Args:
        result: `atlas.feed.query.run_query` 的结果（本模块不重算筛选/排序/分页）。
        industries: **注入的行业配置**（C8：配置 = 筛选维度 = 打标修正对象）。
        values: `raw_id -> {label_key: 最新取值}`，由调用方从 Confirmed 存储读出。
        actor: 打标人（写进 `ConfirmedLabel.actor`）。
        return_to: 打标成功后回到的地址；默认为本页的规范查询串。
    """
    page_url = return_to or feed_query_string(result.query, offset=result.offset)
    parts: list[str] = [
        _render_filters(result.query, industries),
        _render_quick_switches(result.query),
        _render_pager(result),
    ]
    if not result.items:
        parts.append(
            '<p class="empty">没有符合条件的记录。可放宽筛选条件，或先跑采集。</p>'
        )
    for item in result.items:
        parts.append(
            _render_item(
                item,
                values.get(item.raw_id, {}),
                industries=industries,
                actor=actor,
                return_to=page_url,
            )
        )
    parts.append(_render_pager(result))
    return _shell(title="Atlas Feed", heading="Atlas Feed", body="\n".join(parts))


def _render_filters(query: FeedQuery, industries: Sequence[str]) -> str:
    options = []
    for industry in industries:
        selected = " selected" if industry in query.industries else ""
        options.append(
            f'<option value="{escape(industry)}"{selected}>{escape(industry)}</option>'
        )
    if not options:
        options.append('<option value="" disabled>（当前配置里没有行业）</option>')

    desc = " selected" if query.order == "desc" else ""
    asc = " selected" if query.order == "asc" else ""
    return (
        '<form class="filters" method="get" action="/feed">'
        '<label>行业（来自配置，可多选）'
        '<select name="industry" multiple size="5">' + "".join(options) + "</select>"
        "</label>"
        "<label>排序"
        '<select name="order">'
        f'<option value="desc"{desc}>最新优先</option>'
        f'<option value="asc"{asc}>最早优先</option>'
        "</select></label>"
        "<label>每页"
        f'<input type="number" name="limit" min="1" max="200" value="{escape(query.limit)}" required>'
        "</label>"
        '<input type="hidden" name="offset" value="0">'
        '<button type="submit">筛选</button>'
        "</form>"
    )


def _render_quick_switches(query: FeedQuery) -> str:
    def link(label: str, labeled: Any) -> str:
        href = feed_query_string(query, offset=0, labeled=labeled)
        return f'<a href="{escape(href)}">{escape(label)}</a>'

    return (
        '<nav class="quick"><span>标签状态：</span>'
        + link("全部", None)
        + link("已打标", True)
        + link("未打标", False)
        + "</nav>"
    )


def _render_pager(result: FeedResult) -> str:
    shown = len(result.items)
    first = result.offset + 1 if shown else 0
    parts = [f'<span class="count">共 {result.total} 条 · 本页 {first}–{result.offset + shown}</span>']
    if result.offset > 0:
        previous = max(0, result.offset - result.limit)
        href = feed_query_string(result.query, offset=previous)
        parts.append(f'<a href="{escape(href)}">上一页</a>')
    if result.has_more and result.next_offset is not None:
        href = feed_query_string(result.query, offset=result.next_offset)
        parts.append(f'<a href="{escape(href)}">下一页</a>')
    parts.append(f'<a href="{escape(feed_query_string(result.query, offset=0))}">回到第一页</a>')
    return '<nav class="pager">' + " ".join(parts) + "</nav>"


def _render_item(
    item: FeedItem,
    labels: Mapping[str, str],
    *,
    industries: Sequence[str],
    actor: str,
    return_to: str,
) -> str:
    if labels:
        tags = " ".join(
            f'<span class="label-tag">{escape(key)}={escape(labels[key])}</span>'
            for key in sorted(labels)
        )
        label_line = f'<p class="labels">已打标：{tags}</p>'
    else:
        label_line = '<p class="labels">未打标</p>'

    industry = item.industry if item.industry is not None else "（未归行业）"
    status = "" if item.http_status is None else f" · HTTP {item.http_status}"
    return (
        f'<article class="item" id="item-{escape(item.raw_id)}">'
        '<div class="head">'
        f'<code class="raw-id">{escape(item.raw_id)}</code>'
        f'<span class="badge">行业：{escape(industry)}</span>'
        f'<span class="badge">渠道：{escape(item.channel_id)}</span>'
        "</div>"
        f'<div class="endpoint"><a href="{_safe_href(item.endpoint)}" rel="noreferrer noopener">'
        f"{escape(item.endpoint)}</a></div>"
        f'<div class="meta">{escape(item.fetched_at.isoformat())} · '
        f"{escape(item.byte_length)} 字节{escape(status)}</div>"
        + label_line
        + '<div class="actions">'
        + _valid_action(item, actor, return_to, LABEL_VALUE_VALID, "标记有效")
        + _valid_action(item, actor, return_to, LABEL_VALUE_INVALID, "标记无效")
        + _industry_action(item, labels, industries, actor, return_to)
        + "</div></article>"
    )


def _valid_action(item: FeedItem, actor: str, return_to: str, value: str, caption: str) -> str:
    field = f'<input type="hidden" name="label_value" value="{escape(value)}">'
    return _ACTION_FORM.substitute(
        raw_id=escape(item.raw_id),
        label_key=escape(LABEL_KEY_VALID),
        value_field=field,
        actor=escape(actor),
        return_to=escape(return_to),
        caption=escape(caption),
    )


def _industry_action(
    item: FeedItem,
    labels: Mapping[str, str],
    industries: Sequence[str],
    actor: str,
    return_to: str,
) -> str:
    current = labels.get(LABEL_KEY_INDUSTRY)
    options = []
    for industry in industries:
        selected = " selected" if industry == current else ""
        options.append(
            f'<option value="{escape(industry)}"{selected}>{escape(industry)}</option>'
        )
    if not options:
        options.append('<option value="" disabled>（当前配置里没有行业）</option>')
    field = (
        '<select name="label_value" required aria-label="修正行业">'
        + '<option value="" disabled selected hidden>（选择行业）</option>'
        + "".join(options)
        + "</select>"
    )
    return _ACTION_FORM.substitute(
        raw_id=escape(item.raw_id),
        label_key=escape(LABEL_KEY_INDUSTRY),
        value_field=field,
        actor=escape(actor),
        return_to=escape(return_to),
        caption=escape("修正行业"),
    )


# ---------------------------------------------------------------------- #
# 错误页
# ---------------------------------------------------------------------- #
def render_error_page(status: int, title: str, message: str, *, back_to: str = "/feed") -> str:
    """可读的错误页：**明确**说明哪个输入非法，绝不静默成功。"""
    body = (
        f'<div class="notice"><p><strong>{escape(status)} {escape(title)}</strong></p>'
        f"<p>{escape(message)}</p></div>"
        f'<p><a href="{escape(back_to)}">返回 feed</a></p>'
    )
    return _shell(title=f"{status} {title}", heading=f"{status} {title}", body=body)
