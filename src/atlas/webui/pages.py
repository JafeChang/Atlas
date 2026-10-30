"""T-109 服务端渲染的 HTML 页面（stdlib `string.Template` + `html.escape`）。

SPEC §2.11 的硬边界
-------------------

- **无 Web 框架、无 JS 框架、无构建步骤**：页面就是字符串，模板用 `string.Template`。
- 所有动态内容一律经 `escape()`（原文元数据与条目文本来自外部站点，直接拼进 HTML 就是 XSS）。
- `href` 只允许 `http://` / `https://`，其它一律退化成 `#`（数据是外部输入，不可信）。

渲染的是什么（T-109 补完：从"文档"改成"条目"）
---------------------------------------------

SPEC §6.5 的结论：前端此前只显示 `raw_id` / 渠道 / 字节数，**连"这是什么"都看不出来**，
那不叫"feed 浏览"。现在渲染的是 `atlas.feed.FeedEntry`（条目粒度的投影）：

- **标题**：条目自己的标题（容器派生条目），或 URL 末段（整篇即条目的旧语料，
  正文里**没有**标题字段 —— 见 `_entry_title`）；
- **原文链接**：条目自身的 `link`（没有则回落到文档 `endpoint`）；
- **时间**：条目自身的发布时间；缺失时**明确标注**回落到抓取时间，
  而不是装作它就是发布时间；
- **区间**：`[char_start, char_end)` 与"打开原文并高亮该区间"的入口。

`content_slice()` / `sanitize_for_quote()` 的分工
-------------------------------------------------

条目正文是**标记文本**。本模块渲染的是 `content_slice()` 的产物（只去标签与 CDATA 外壳、
**不解实体**），这样字符数与偏移保持一致；**坐标一律不从这里反推**——
条目区间来自条目层（T-130）的确定性切片，本模块只做展示。
需要与 quote 做匹配时用 `sanitize_for_quote()`，那是 `atlas.entries` 的入口。

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
from typing import Any, Mapping, Sequence  # noqa: UP035
from urllib.parse import urlencode

from atlas.analytics.tracking import TRACKING_SCRIPT
from atlas.feed import FeedEntry, FeedItem, FeedQuery, FeedResult

__all__ = [
    "LABEL_KEY_INDUSTRY",
    "LABEL_KEY_VALID",
    "LABEL_VALUE_INVALID",
    "LABEL_VALUE_VALID",
    "escape",
    "entry_anchor_url",
    "feed_query_string",
    "render_entry_page",
    "render_error_page",
    "render_feed_page",
    "article_url",
    "render_article_page",
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


def entry_anchor_url(entry: FeedEntry) -> str:
    """「打开原文并高亮该条目区间」的站内地址（**只有整数参数**，无坐标推算）。"""
    params: list[tuple[str, str]] = [
        ("raw_id", entry.raw_id),
        ("char_start", str(entry.char_start)),
        ("char_end", str(entry.char_end)),
    ]
    if entry.entry_id:
        params.append(("entry_id", entry.entry_id))
    return "/entry?" + urlencode(params)


def article_url(entry: FeedEntry, *, back_to: str = "/feed") -> str:
    return entry_anchor_url(entry).replace("/entry?", "/article?", 1) + "&" + urlencode({"back_to": back_to})


# ---------------------------------------------------------------------- #
# 查询串（分页与「返回本页」用）
# ---------------------------------------------------------------------- #
def feed_query_string(
    query: FeedQuery, *, offset: int = 0, labeled: Any = _KEEP, channels: Any = _KEEP
) -> str:
    """把 `FeedQuery` 投影回 URL 查询串（**只做投影**，筛选/排序语义仍归 `atlas.feed`）。

    `labeled` 和 `channels` 用于快捷切换；不传时沿用查询自身的值。
    """
    params: list[tuple[str, str]] = [
        ("order", query.order),
        ("limit", str(query.limit)),
        ("offset", str(offset)),
    ]
    effective_labeled = query.labeled if labeled is _KEEP else labeled
    if effective_labeled is not None:
        params.append(("labeled", "true" if effective_labeled else "false"))
    for tag in query.tags:
        params.append(("tag", tag))
    for industry in query.industries:
        params.append(("industry", industry))
    for channel in query.channels if channels is _KEEP else channels:
        params.append(("channel", channel))
    if query.granularity == "document":
        params.append(("granularity", query.granularity))
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
:root { color-scheme: light dark; --bg: #f5f7fb; --surface: #fff; --text: #182333;
        --muted: #526174; --border: #d8e0ea; --accent: #175ac6; --tint: #eaf1fc;
        --valid: #146c43; --valid-bg: #e2f3e9; --invalid: #ae233a; --invalid-bg: #fce8ec;
        --manual: #6941a5; --manual-bg: #f1eafa; --neutral-bg: #edf0f4;
        --history: #805313; --history-bg: #fff3d6; }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 0 auto; max-width: 58rem; padding: 1rem 1rem 4rem; line-height: 1.6;
       font-size: 16px; background: var(--bg); color: var(--text); }
h1 { font-size: 1.55rem; letter-spacing: -0.025em; margin: 0.4rem 0 1rem; }
h2 { font-size: 1rem; margin: 1.5rem 0 0.5rem; }
a { color: var(--accent); text-underline-offset: 0.18em; }
button, input, select { font: inherit; }
button, select, input[type=number], input[type=text], input:not([type]) { min-height: 44px; border: 1px solid var(--border);
    border-radius: 9px; padding: 0.5rem 0.75rem; }
select, input { background: var(--surface); color: var(--text); max-width: 100%; }
button { background: var(--accent); color: #fff; border-color: var(--accent); cursor: pointer; }
button:hover { filter: brightness(0.94); }
button:disabled, select:disabled { opacity: 0.6; cursor: wait; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }
summary { cursor: pointer; min-height: 44px; padding-block: 0.55rem; }
code, .raw-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }
.login-card { max-width: 28rem; margin: 3rem auto; padding: 1.5rem; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; }
.login-card label { display: block; font-weight: 600; margin-bottom: 0.4rem; }
.login-card input, .login-card button { width: 100%; }
.login-card button { margin-top: 1rem; }
.login-spinner { display: inline-block; width: 1.2rem; height: 1.2rem; vertical-align: middle;
                 border: 2px solid rgba(255, 255, 255, 0.4); border-top-color: #fff;
                 border-radius: 50%; animation: login-spin 0.7s linear infinite; }
@keyframes login-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .login-spinner { animation: none; } }
.login-card p, .account { color: var(--muted); }
.account { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
.account button { background: transparent; color: var(--accent); border-color: var(--border); }
.filter-panel { border: 1px solid var(--border); border-radius: 12px; padding: 0.2rem 0.9rem;
                background: var(--surface); }
.filters { display: flex; flex-wrap: wrap; gap: 0.8rem; align-items: flex-end; padding-bottom: 0.8rem; }
.filters label { display: flex; flex-direction: column; font-size: 0.875rem; gap: 0.3rem; min-width: 0; }
.filters select[multiple] { width: 100%; }
.filters label:first-child { flex: 1 1 100%; }
.filters label:not(:first-child) { flex: 1 1 6rem; }
.pager { display: flex; flex-wrap: wrap; gap: 0.3rem 0.9rem; align-items: center;
         margin: 0.8rem 0; font-size: 0.875rem; }
.pager .count { flex-basis: 100%; color: var(--muted); }
.pager a { display: inline-flex; align-items: center; min-height: 36px; }
.quick { margin: 0.9rem 0; display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; }
.quick > span { display: none; }
.quick a { text-decoration: none; border-radius: 9px; min-height: 44px; padding: 0.5rem 0.85rem; }
.quick a[aria-current=page] { background: var(--accent); color: #fff; }
.source-tabs { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0 0 1rem;
               padding-bottom: 0.5rem; border-bottom: 1px solid var(--border); }
.source-tabs a { display: inline-flex; align-items: center; min-height: 44px; max-width: 100%;
                 padding: 0.5rem 0.85rem; border-radius: 8px; text-decoration: none;
                 overflow-wrap: anywhere; }
.source-tabs a[aria-current=page] { color: var(--accent); background: var(--tint);
                                  box-shadow: inset 0 -2px var(--accent); font-weight: 650; }
.item { position: relative; border: 1px solid var(--border); border-radius: 14px; padding: 1rem;
        margin: 0.8rem 0; background: var(--surface); }
.item[data-detail-url] { cursor: pointer; }
.item .head { min-width: 0; padding-right: 3rem; }
.item .title { color: var(--text); font-weight: 650; font-size: 1.08rem;
              text-decoration: none; overflow-wrap: anywhere; }
.item .title-zh { color: var(--text); font-size: 1rem; line-height: 1.55;
                  margin: 0.35rem 0 0; overflow-wrap: anywhere; }
.item .meta { display: flex; flex-wrap: wrap; gap: 0.25rem 0.75rem; color: var(--muted);
              font-size: 0.875rem; margin: 0.45rem 0; overflow-wrap: anywhere; }
.tag-row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem;
           margin: 0.3rem 0 0.65rem; min-height: 1.5rem; }
.tag-row.is-empty { display: none; }
.industry-badges, .labels { display: contents; }
.badge, .label-tag { display: inline-flex; max-width: 100%; font-size: 0.75rem;
    line-height: 1.65; border-radius: 5px; padding: 0.1rem 0.45rem;
    background: var(--tint); color: var(--accent); overflow-wrap: anywhere; }
.tag-manual { background: var(--manual-bg); color: var(--manual); }
.tag-neutral, .tag-channel { background: var(--neutral-bg); color: var(--muted); }
.tag-history { background: var(--history-bg); color: var(--history); }
.item .endpoint, .item .trace, .item .open { font-size: 0.78rem; opacity: 0.8;
                                             margin: 0.2rem 0; overflow-wrap: anywhere; }
.item .trace code { margin-right: 0.35rem; }
.item .hint { font-size: 0.75rem; opacity: 0.7; }
.item .label-status { min-height: 1.6em; margin: 0.3rem 0 0;
                      font-size: 0.875rem; color: var(--muted); }
.item .label-status:empty { min-height: 0; margin: 0; }
.item .label-status[data-error] { color: var(--text); }
.item .summary { font-size: 0.95rem; line-height: 1.7; margin: 0.75rem 0 0.25rem;
                 overflow-wrap: anywhere; }
.item .summary-origin { color: var(--muted); font-size: 0.75rem; }
.cleaned-body { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.75;
                font-family: inherit; max-width: 100%; }
.item .problem { font-size: 0.8rem; border-left: 3px solid currentColor;
                 padding-left: 0.5rem; margin: 0.3rem 0; }
.item .open { opacity: 1; font-size: 0.875rem; }
.open a { display: inline-flex; align-items: center; min-height: 40px; margin-right: 1rem; }
.actions { position: relative; display: flex; flex-wrap: wrap; align-items: flex-start; gap: 0.25rem 0.5rem;
           margin-top: 0.2rem; }
.vote-button, .icon-action { display: inline-flex; align-items: center; justify-content: center;
    width: 44px; height: 44px; padding: 0; border: 0; border-radius: 50%;
    background: transparent; color: var(--muted); }
.vote-button svg, .icon-action svg { width: 19px; height: 19px; flex-shrink: 0; }
.icon-action { text-decoration: none; cursor: pointer; }
.icon-action:hover { background: var(--neutral-bg); }
.menu-trigger { position: absolute; top: 0.7rem; right: 0.7rem; }
.act-valid .vote-button:hover, .act-valid .vote-button[aria-pressed=true] {
    color: var(--valid); background: var(--valid-bg); }
.act-invalid .vote-button:hover, .act-invalid .vote-button[aria-pressed=true] {
    color: var(--invalid); background: var(--invalid-bg); }
.vote-button[aria-pressed=true] svg { fill: currentColor; fill-opacity: 0.15; }
.industry-editor { position: static; color: var(--muted); }
.industry-editor summary { list-style: none; }
.industry-editor summary::-webkit-details-marker { display: none; }
.industry-editor form { position: absolute; z-index: 3; top: calc(100% + 0.3rem); left: 0;
    width: min(20rem, 100%); display: flex; flex-wrap: wrap; gap: 0.5rem;
    padding: 0.8rem; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; box-shadow: 0 8px 24px #0003; }
.industry-editor form label { flex: 1 1 11rem; font-size: 0.875rem; }
.industry-editor form input { display: block; width: 100%; }
.industry-editor form button { align-self: end; }
.bottom-sheet { position: fixed; inset: auto 0 0; margin: 0 auto; width: min(100%, 40rem);
    max-width: 100%; max-height: min(80dvh, 42rem); overflow-y: auto; padding: 0;
    border: 1px solid var(--border); border-bottom: 0; border-radius: 18px 18px 0 0;
    background: var(--surface); color: var(--text); box-shadow: 0 -8px 32px #0003; }
.bottom-sheet::backdrop { background: #0009; }
.sheet-content { padding: 0.6rem 1rem max(1rem, env(safe-area-inset-bottom)); }
.sheet-content:focus { outline: none; }
.sheet-bar { display: flex; align-items: center; justify-content: space-between; }
.sheet-bar h2 { margin: 0; }
.sheet-close { flex: 0 0 44px; }
.record-details { color: var(--text); font-size: 0.9rem; }
.record-details summary { border-top: 1px solid var(--border); font-weight: 600; }
.record-details .meta { display: block; }
.notice { border-left: 4px solid currentColor; padding: 0.4rem 0.6rem; margin: 0.8rem 0; }
.empty { opacity: 0.8; }
.entry-slice { margin: 0.8rem 0; padding: 0.6rem 0.8rem; border-left: 4px solid currentColor;
               white-space: pre-wrap; overflow-wrap: anywhere; max-height: 60rem; overflow: auto; }
.entry-slice mark { background: color-mix(in srgb, currentColor 22%, transparent); color: inherit; }
.highlight-note { font-size: 0.82rem; opacity: 0.85; }
footer { margin-top: 2rem; font-size: 0.75rem; opacity: 0.7; }
.article-detail { cursor: auto; }
.article-title { padding-right: 3rem; font-size: clamp(1.35rem, 3.5vw, 2rem); line-height: 1.35; margin: 0 0 0.7rem;
                 overflow-wrap: anywhere; letter-spacing: -0.025em; }
.article-detail .title-zh { font-size: 1.08rem; }
.article-detail h3 { font-size: 1rem; margin: 1.5rem 0 0.65rem; }
.article-intro { padding: 0.1rem 1rem 0.8rem; margin: 1rem 0; border-left: 3px solid var(--border);
                 background: var(--bg); border-radius: 0 8px 8px 0; }
.article-intro h3 { margin-top: 0.7rem; }
.article-intro p { margin: 0.4rem 0; }
.reader-tabs { display: flex; gap: 0.3rem; margin-bottom: 1rem; border-bottom: 1px solid var(--border); }
.reader-tabs button { border: 0; border-radius: 8px 8px 0 0; min-width: 5rem;
                      color: var(--muted); background: transparent; }
.reader-tabs button[aria-selected=true] { color: var(--accent); background: var(--tint);
                                        box-shadow: inset 0 -2px var(--accent); }
[hidden] { display: none !important; }
.article-body { line-height: 1.85; font-size: 1rem; overflow-wrap: anywhere; }
.article-body :first-child { margin-top: 0; }
.article-body p, .article-body ul, .article-body ol { margin-block: 1em; }
.article-body h1, .article-body h2, .article-body h3, .article-body h4 { margin: 1.5em 0 0.7em; line-height: 1.4; }
.article-body h1 { font-size: 1.5rem; }
.article-body h2 { font-size: 1.3rem; }
.article-body h3 { font-size: 1.1rem; }
.article-body img { display: block; max-width: 100%; height: auto; margin: 1.25rem auto; border-radius: 8px; }
.article-body figure { margin: 1.25rem 0; }
.article-body figcaption { font-size: 0.85rem; color: var(--muted); text-align: center; }
.article-body pre { max-width: 100%; overflow-x: auto; padding: 1rem; background: var(--bg);
                    border: 1px solid var(--border); border-radius: 8px; line-height: 1.6; }
.article-body code { white-space: break-spaces; background: var(--bg); padding: 0.1em 0.2em; }
.article-body pre code { white-space: pre; padding: 0; }
.article-body table { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; }
.article-body th, .article-body td { border: 1px solid var(--border); padding: 0.4rem 0.65rem; }
.article-body blockquote { margin-inline: 0; padding-left: 1rem; border-left: 3px solid var(--border); color: var(--muted); }
.body-note, .translation-status { font-size: 0.85rem; color: var(--muted); }
.image-unavailable { display: block; padding: 1rem; background: var(--bg); border-radius: 8px; font-size: 0.875rem; color: var(--muted); }
@media (min-width: 640px) {
  body { padding: 2rem 2rem 4rem; }
  .filters label:first-child { flex: 1 1 14rem; }
  .pager .count { flex-basis: auto; margin-right: auto; }
  .item { padding: 1.25rem; }
}
/* Feed and article keep independent scroll positions on wide screens. */
.feed-detail { display: none; }
@media (min-width: 1100px) {
  body.feed-page { max-width: none; height: 100dvh; padding: 1rem 1.5rem; overflow: hidden;
                   display: flex; flex-direction: column; }
  .feed-page > h1 { flex: none; margin: 0 0 0.8rem; }
  .feed-page > footer { display: none; }
  .feed-layout { display: grid; grid-template-columns: minmax(20rem, 38%) minmax(0, 1fr);
                 gap: 1rem; flex: 1; min-height: 0; }
  .feed-list { min-width: 0; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
               padding: 0 0.5rem 2rem 0; }
  .feed-detail { display: flex; flex-direction: column; min-width: 0; min-height: 0;
                 overflow: hidden; border: 1px solid var(--border); border-radius: 14px;
                 background: var(--surface); }
  .feed-detail iframe { flex: 1; width: 100%; min-height: 0; border: 0; }
  .feed-detail iframe[hidden] { display: none; }
  .detail-placeholder { margin: auto; padding: 2rem; color: var(--muted); text-align: center; }
  .item.is-selected { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); }
}
body.embedded-detail { max-width: none; padding: 1.25rem; }
body.embedded-detail > h1, body.embedded-detail > footer { display: none; }
body.embedded-detail .article-detail { margin: 0; padding: 0; border: 0; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) { --bg: #101720; --surface: #18212d; --text: #e5ebf3; --muted: #a8b6c8;
          --border: #354357; --accent: #8dbaff; --tint: #233653;
          --valid: #7cdbac; --valid-bg: #183c2d; --invalid: #ffa0b3; --invalid-bg: #45232d;
          --manual: #ccb0ff; --manual-bg: #342743; --neutral-bg: #25303d;
          --history: #efca83; --history-bg: #3c3220; }
}
:root[data-theme=dark] { --bg: #101720; --surface: #18212d; --text: #e5ebf3; --muted: #a8b6c8;
  --border: #354357; --accent: #8dbaff; --tint: #233653;
  --valid: #7cdbac; --valid-bg: #183c2d; --invalid: #ffa0b3; --invalid-bg: #45232d;
  --manual: #ccb0ff; --manual-bg: #342743; --neutral-bg: #25303d;
  --history: #efca83; --history-bg: #3c3220; color-scheme: dark; }
:root[data-theme=light] { color-scheme: light; }

/* The reading interface follows the supplied Atlas prototype. */
.feed-brand, .source-menu, .theme-toggle, .feed-user, .compact-pager, .tag-overflow, .filter-panel summary svg { display: none; }
  body.feed-page { --bg: #eceff1; --surface: #fbfcfc; --text: #181b1f; --muted: #626a73;
    --border: #d9dee2; --accent: #27508a; --tint: #dbe5f4; --neutral-bg: #e1e6ea;
    --valid: #1f6e45; --valid-bg: #dcefe3; --invalid: #a23a28; --invalid-bg: #f5dfd9;
    padding: 0; font-size: 14px; -webkit-font-smoothing: antialiased; }
  .feed-page > h1 { display: none; }
  .feed-layout { grid-template-columns: clamp(360px, 31vw, 450px) minmax(0, 1fr); gap: 0; }
  .feed-list { display: flex; flex-direction: column; overflow: hidden; padding: 0;
               border-right: 1px solid var(--border); background: var(--bg);
               container: feed-list / inline-size; }
  .feed-header { position: relative; z-index: 4; display: flex; flex-direction: column;
                 align-items: stretch; gap: 4px; flex: none; padding: 0 10px 10px 14px;
                 border-bottom: 1px solid var(--border); }
  .feed-header-main, .feed-header-controls { display: flex; flex-wrap: nowrap;
    align-items: center; gap: 4px; min-width: 0; }
  .feed-header-main { min-height: 44px; }
  .feed-brand { display: flex; align-items: center; gap: 8px; flex: 1 1 auto;
                min-width: 0; height: 44px; font-size: 15px; }
  .feed-brand span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .feed-source-management { flex: none; text-decoration: none; }
  .feed-brand strong { font-weight: 650; }
  .feed-brand span { color: var(--muted); }
  .feed-brand svg, .feed-source-management svg, .theme-toggle svg, .feed-user svg, .filter-panel summary svg, .source-menu svg {
    width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.75;
    stroke-linecap: round; stroke-linejoin: round; flex: none; }
  .feed-brand svg { width: 20px; height: 20px; color: var(--accent); }
  .account, .source-tabs, .feed-top-pager { display: none; }
  .filter-panel { order: 1; position: static; padding: 0; border: 0; background: transparent; }
  .feed-source-management, .filter-panel summary, .feed-user summary, .theme-toggle { display: grid; place-items: center;
    width: 36px; height: 36px; min-height: 36px; padding: 0; border: 0; border-radius: 8px;
    color: var(--muted); background: transparent; list-style: none; cursor: pointer; }
  .filter-panel summary::-webkit-details-marker, .feed-user summary::-webkit-details-marker,
  .source-menu summary::-webkit-details-marker { display: none; }
  .filter-panel summary svg { display: block; }
  .filter-panel summary span { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  .feed-source-management:hover, .filter-panel summary:hover, .feed-user summary:hover, .theme-toggle:hover { background: var(--neutral-bg); color: var(--text); }
  .filter-panel .filters { position: absolute; top: calc(100% + 4px); right: 10px; z-index: 8;
    display: flex; width: min(21rem, calc(100vw - 2rem)); padding: 0.85rem; border: 1px solid var(--border);
    border-radius: 12px; background: var(--surface); box-shadow: 0 10px 30px #141e2d2e; }
  .filter-panel .filters label:first-child { flex: 1 1 100%; }
  .theme-toggle { order: 2; }
  .feed-user { display: block; position: relative; order: 3; }
  .feed-user > div { position: absolute; right: 0; top: calc(100% + 4px); z-index: 8; min-width: 12rem;
    padding: 0.8rem; border: 1px solid var(--border); border-radius: 12px; background: var(--surface);
    box-shadow: 0 10px 30px #141e2d2e; overflow-wrap: anywhere; }
  .feed-user form { margin-top: 0.5rem; }
  .feed-user button { min-height: 36px; width: 100%; }
  .source-menu { display: block; position: relative; order: 4; flex: 1 1 0; min-width: 0; max-width: none; }
  .source-menu summary { display: flex; align-items: center; gap: 6px; width: fit-content; max-width: 100%;
    height: 34px; min-height: 34px; padding: 0 8px 0 10px; border: 1px solid var(--border);
    border-radius: 8px; background: var(--surface); cursor: pointer; font-size: 13px; list-style: none; }
  .source-menu summary span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .source-menu summary svg { width: 14px; height: 14px; }
  .source-menu nav { position: absolute; top: calc(100% + 6px); left: 0; z-index: 8;
    width: min(19rem, calc(100vw - 2rem)); max-height: min(60dvh, 28rem); overflow-y: auto;
    padding: 6px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface);
    box-shadow: 0 10px 30px #141e2d2e; }
  .source-menu nav a { display: block; min-height: 36px; padding: 8px 10px; border-radius: 8px;
    color: var(--text); text-decoration: none; overflow-wrap: anywhere; }
  .source-menu nav a:hover, .source-menu nav a[aria-current=page] { background: var(--tint); }
  .source-menu-group + .source-menu-group { margin-top: 4px; padding-top: 4px; border-top: 1px solid var(--border); }
  .source-menu-group-title { padding: 6px 10px; font-size: 12px; font-weight: 650; color: var(--muted); overflow-wrap: anywhere; }
  .source-menu-group-count { font-weight: 400; }
  .quick { order: 5; flex: none; gap: 1px; margin: 0 0 0 4px; padding: 2px;
           border-radius: 8px; background: var(--neutral-bg); }
  .quick a { display: inline-flex; align-items: center; min-height: 30px; padding: 0 7px;
             border-radius: 6px; color: var(--text); font-size: 13px; white-space: nowrap; }
  .quick a[aria-current=page] { background: var(--surface); color: var(--text); font-weight: 600;
                                box-shadow: 0 1px 2px #0002; }
  .feed-items { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
  .compact-pager { order: 6; display: flex; flex: none; align-items: center; gap: 1px;
    margin-left: auto; color: var(--muted); font-size: 11px; white-space: nowrap; }
  .compact-count { margin-right: 3px; }
  .page-number { min-width: 28px; max-width: 6ch; overflow: hidden; text-overflow: ellipsis;
    text-align: center; font-variant-numeric: tabular-nums; }
  .page-arrow { display: inline-grid; place-items: center; width: 24px; height: 34px;
    border-radius: 8px; color: var(--muted); text-decoration: none; }
  .page-arrow:hover { color: var(--text); background: var(--neutral-bg); }
  .page-arrow[aria-disabled=true] { opacity: 0.35; }
  .page-arrow svg { width: 18px; height: 18px; fill: none; stroke: currentColor;
    stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; }
@container feed-list (max-width: 430px) {
  .compact-count { display: none; }
}
  .feed-items > .empty { padding: 2rem 16px; text-align: center; }
  .feed-items .item { display: flex; flex-direction: column; position: relative; margin: 0;
    padding: 10px 16px 12px; border: 0; border-bottom: 1px solid var(--border); border-radius: 0;
    background: transparent; }
  .feed-items .item:hover { background: #e3e8ec; }
  .feed-items .item.is-selected { background: #dfe7f2; box-shadow: inset 3px 0 0 var(--accent); }
  .feed-items .item .head { display: flex; flex-direction: column; order: 1; padding: 0; }
  .feed-items .item .title { font-size: 14.5px; line-height: 1.42; font-weight: 650;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .feed-items .item .title-zh { order: -1; margin: 0; font-size: 14.5px; line-height: 1.42; font-weight: 650;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .feed-items .item .head:has(.title-zh) .title { color: var(--muted); font-size: 12px; font-weight: 400;
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .feed-items .item > .feed-item-meta { order: 0; min-height: 30px; margin: 0;
    padding-right: 156px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; }
  .feed-items .item .summary, .feed-items .item .summary-origin { order: 2; margin: 6px 0 8px;
    color: #454c54; font-size: 13px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2;
    -webkit-box-orient: vertical; overflow: hidden; }
  .feed-items .item .summary-pending { color: var(--muted); font-style: italic; }
  .feed-items .item .tag-row { order: 3; flex-wrap: nowrap; min-height: 0; gap: 4px; margin: 0; overflow: hidden; }
  .feed-items .item .badge, .feed-items .item .label-tag { flex: none; max-width: 9em;
    padding: 0 8px; border-radius: 99px; color: #454c54; background: var(--neutral-bg);
    font-size: 11.5px; line-height: 19px; text-decoration: none;
    text-overflow: ellipsis; white-space: nowrap; overflow: hidden; }
  .feed-items .item .badge:hover { background: var(--tint); }
  .feed-items .industry-badges .badge:nth-of-type(n+4) { display: none; }
  .feed-items .industry-badges .tag-overflow { display: inline-flex; flex: none; align-items: center;
    padding: 0 8px; border-radius: 99px; background: var(--neutral-bg); color: var(--muted);
    font-size: 11.5px; line-height: 19px; }
  .feed-items .item .actions { position: absolute; top: 7px; right: 38px; z-index: 2; margin: 0; gap: 1px; }
  .feed-items .item .menu-trigger { top: 7px; right: 6px; z-index: 2; }
  .feed-items .item .actions button, .feed-items .item .icon-action, .feed-items .item .industry-editor summary {
    width: 32px; height: 32px; min-height: 32px; border-radius: 8px; padding: 0; }
  .feed-items .act-valid .vote-button svg, .feed-items .act-invalid .vote-button svg { display: none; }
  .feed-items .act-valid .vote-button::before, .feed-items .act-invalid .vote-button::before {
    content: '✓'; display: inline-grid; place-items: center; width: 14px; height: 14px;
    border: 1.5px solid currentColor; border-radius: 50%; font: 600 10px/1 system-ui, sans-serif; }
  .feed-items .act-invalid .vote-button::before { content: '×'; font-size: 12px; }
  .feed-items .item .actions, .feed-items .item .menu-trigger { opacity: 0; }
  .feed-items .item:hover .actions, .feed-items .item:focus-within .actions,
  .feed-items .item.is-selected .actions, .feed-items .item:hover .menu-trigger,
  .feed-items .item:focus-within .menu-trigger, .feed-items .item.is-selected .menu-trigger { opacity: 1; }
  .feed-items .item .menu-trigger svg { transform: rotate(90deg); }
  .feed-items .item .industry-editor form { left: auto; right: 0; width: min(19rem, calc(100vw - 2rem)); }
  .feed-items .item .label-status { order: 4; font-size: 12px; }
  .feed-detail { border: 0; border-radius: 0; background: var(--surface); }
@media (hover: none) {
  .feed-items .item .actions, .feed-items .item .menu-trigger { opacity: 1; }
  .feed-items .item .actions button, .feed-items .item .icon-action,
  .feed-items .item .industry-editor summary { width: 44px; height: 44px; min-height: 44px; }
  .feed-items .item .actions { right: 48px; }
  .feed-items .item > .feed-item-meta { min-height: 44px; padding-right: 216px; }
}
@media (max-width: 1099px) {
  body.feed-page { max-width: none; min-height: 100dvh; height: auto; overflow: visible; display: block; }
  .feed-page > footer { display: none; }
  .feed-layout { display: block; }
  .feed-list { display: block; min-width: 0; overflow: visible; border-right: 0; }
  .feed-header { position: sticky; top: 0; background: var(--bg);
    padding-top: env(safe-area-inset-top, 0px);
    padding-left: max(14px, env(safe-area-inset-left, 0px));
    padding-right: max(10px, env(safe-area-inset-right, 0px)); }
  .feed-items { overflow: visible; padding-bottom: env(safe-area-inset-bottom, 0px); }
  .feed-items .item { scroll-margin-top: calc(104px + env(safe-area-inset-top, 0px)); }
  .feed-brand { flex-basis: auto; }
  .source-menu { min-width: 0; max-width: none; }
  .source-menu summary { min-height: 40px; height: 40px; }
  .quick a { min-height: 36px; }
  .feed-source-management, .filter-panel summary, .feed-user summary, .theme-toggle { width: 40px; height: 40px; min-height: 40px; }
  .compact-count { display: none; }
  .page-arrow { width: 40px; height: 40px; }
  .feed-items .item .actions, .feed-items .item .menu-trigger { opacity: 1; }
  .feed-items .item .actions button, .feed-items .item .icon-action,
  .feed-items .item .industry-editor summary { width: 36px; height: 36px; min-height: 36px; }
  .feed-items .item .actions { right: 42px; }
  .feed-items .item > .feed-item-meta { min-height: 36px; padding-right: 178px; }
  .feed-items .item .industry-editor form { width: min(19rem, calc(100vw - 4rem)); }
}
@media (max-width: 360px) {
  .feed-brand span { display: none; }
  .quick { margin-left: 0; }
  .quick a { font-size: 12px; padding-inline: 3px; }
  .source-menu summary { gap: 4px; padding-inline: 6px; }
  .source-menu summary span { display: none; }
  .source-menu summary::before { content: '来源'; white-space: nowrap; font-size: 12px; }
}
:is(body.embedded-detail, body.article-page) { --bg: #eceff1; --surface: #fbfcfc; --text: #181b1f; --muted: #626a73;
  --border: #d9dee2; --accent: #27508a; --tint: #dbe5f4; --neutral-bg: #e1e6ea;
  --valid: #1f6e45; --valid-bg: #dcefe3; --invalid: #a23a28; --invalid-bg: #f5dfd9;
  max-width: none; margin: 0; padding: 0; background: var(--surface); font-size: 14px; -webkit-font-smoothing: antialiased; }
:is(body.embedded-detail, body.article-page) > h1, :is(body.embedded-detail, body.article-page) > footer { display: none; }
:is(body.embedded-detail, body.article-page) .article-detail { margin: 0; padding: 0; border: 0; border-radius: 0; }
:is(body.embedded-detail, body.article-page) .reader-toolbar { position: sticky; top: 0; z-index: 3; display: flex;
  align-items: center; gap: 6px; min-height: 48px; padding: 6px 16px; border-bottom: 1px solid var(--border);
  background: var(--surface); }
:is(body.embedded-detail, body.article-page) .reader-tabs { flex: none; gap: 1px; margin: 0 auto 0 0; padding: 2px;
  border: 0; border-radius: 8px; background: var(--neutral-bg); }
:is(body.embedded-detail, body.article-page) .reader-tabs button { min-width: 0; min-height: 28px; padding: 0 10px;
  border-radius: 6px; color: var(--muted); font-size: 13px; }
:is(body.embedded-detail, body.article-page) .reader-tabs button[aria-selected=true] { color: var(--text); background: var(--surface);
  box-shadow: 0 1px 2px #0002; font-weight: 600; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .actions { margin: 0 0 0 auto; gap: 2px; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .vote-button, :is(body.embedded-detail, body.article-page) .reader-toolbar .icon-action,
:is(body.embedded-detail, body.article-page) .reader-toolbar .industry-editor summary { width: 36px; height: 36px; min-height: 36px;
  padding: 0; border-radius: 8px; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-valid .vote-button,
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-invalid .vote-button { width: auto; gap: 6px; padding: 0 8px; font-size: 13px; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-valid .vote-button svg,
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-invalid .vote-button svg { display: none; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-valid .vote-button::before,
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-invalid .vote-button::before { content: '✓'; display: inline-grid;
  place-items: center; width: 14px; height: 14px; border: 1.5px solid currentColor; border-radius: 50%;
  font: 600 10px/1 system-ui, sans-serif; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-invalid .vote-button::before { content: '×'; font-size: 12px; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-valid .vote-button::after { content: '有效'; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .act-invalid .vote-button::after { content: '无效'; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .menu-trigger { position: static; flex: none; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .menu-trigger svg { transform: rotate(90deg); }
:is(body.embedded-detail, body.article-page) .reader-back { display: inline-grid; place-items: center; flex: none;
  width: 40px; height: 40px; border-radius: 8px; color: var(--muted); text-decoration: none; }
:is(body.embedded-detail, body.article-page) .reader-back:hover { background: var(--neutral-bg); color: var(--text); }
:is(body.embedded-detail, body.article-page) .reader-back svg { width: 20px; height: 20px; fill: none; stroke: currentColor;
  stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; }
:is(body.embedded-detail, body.article-page) .reader-back span { position: absolute; width: 1px; height: 1px;
  padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .industry-editor form { left: auto; right: 0; width: 19rem; max-width: calc(100vw - 2rem); }
:is(body.embedded-detail, body.article-page) .reader-toolbar .label-status { position: absolute; right: 16px; top: calc(100% + 8px); z-index: 5;
  margin: 0; min-height: 0; padding: 6px 10px; border-radius: 8px; background: var(--text); color: var(--surface);
  box-shadow: 0 3px 12px #0002; font-size: 12px; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .label-status:empty { display: none; }
:is(body.embedded-detail, body.article-page) .reader-toolbar .label-status[data-error] { background: var(--invalid-bg); color: var(--invalid); }
:is(body.embedded-detail, body.article-page) .reader-paper { max-width: 780px; min-height: calc(100dvh - 48px); margin: 0 auto;
  padding: 34px clamp(20px, 4vw, 52px) 24px; display: flex; flex-direction: column; }
:is(body.embedded-detail, body.article-page) .reader-heading { display: flex; flex-direction: column; }
:is(body.embedded-detail, body.article-page) .reader-heading > .meta { order: -2; margin: 0; color: var(--muted); font-size: 13px; }
:is(body.embedded-detail, body.article-page) .article-title { margin: 12px 0 6px; padding: 0;
  font: 650 28px/1.32 'Source Serif 4', 'Noto Serif SC', 'Songti SC', Georgia, serif; letter-spacing: 0; }
:is(body.embedded-detail, body.article-page) .reader-heading:has(.title-zh) .title-zh { order: -1; margin: 12px 0 6px;
  font: 650 28px/1.32 'Source Serif 4', 'Noto Serif SC', 'Songti SC', Georgia, serif; }
:is(body.embedded-detail, body.article-page) .reader-heading:has(.title-zh) .article-title { margin: 0 0 12px; color: var(--muted);
  font: 400 15px/1.5 system-ui, sans-serif; }
:is(body.embedded-detail, body.article-page) .tag-row { gap: 4px; margin: 0.3rem 0 0; }
:is(body.embedded-detail, body.article-page) .badge, :is(body.embedded-detail, body.article-page) .label-tag { padding: 0 8px; border-radius: 99px;
  color: #454c54; background: var(--neutral-bg); font-size: 11.5px; line-height: 19px; text-decoration: none; }
:is(body.embedded-detail, body.article-page) .badge:hover { background: var(--tint); }
:is(body.embedded-detail, body.article-page) .article-intro { margin: 20px 0 0; padding: 2px 0 2px 14px;
  border-left: 3px solid var(--accent); border-radius: 0; background: transparent;
  color: #454c54; font-size: 15.5px; line-height: 1.75; }
:is(body.embedded-detail, body.article-page) .article-intro h3, :is(body.embedded-detail, body.article-page) .reader-content > h3 { position: absolute;
  width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
:is(body.embedded-detail, body.article-page) .article-intro p { margin: 0; }
:is(body.embedded-detail, body.article-page) .reader-content { margin: 28px 0 44px; }
:is(body.embedded-detail, body.article-page) .article-body { color: var(--text); font-size: 16px; line-height: 1.85; }
:is(body.embedded-detail, body.article-page) .article-body h1, :is(body.embedded-detail, body.article-page) .article-body h2,
:is(body.embedded-detail, body.article-page) .article-body h3 { font-family: 'Source Serif 4', 'Noto Serif SC', 'Songti SC', Georgia, serif; }
:is(body.embedded-detail, body.article-page) .reader-footer { margin-top: auto; padding-top: 16px; border-top: 1px solid var(--border);
  display: flex; flex-wrap: wrap; gap: 6px 22px; color: var(--muted); font-size: 13px; opacity: 1; }
:is(body.embedded-detail, body.article-page) .reader-footer a { margin-left: auto; text-decoration: none; }
@media (max-width: 1099px) {
  body.article-page .reader-toolbar { gap: 2px; padding: 4px 6px; }
  body.article-page .reader-toolbar .actions { flex-wrap: nowrap; gap: 0; }
  body.article-page .reader-toolbar .act-valid .vote-button,
  body.article-page .reader-toolbar .act-invalid .vote-button { width: 36px; padding: 0; }
  body.article-page .reader-toolbar .act-valid .vote-button::after,
  body.article-page .reader-toolbar .act-invalid .vote-button::after { content: none; }
  body.article-page .reader-toolbar .industry-editor form { max-width: calc(100vw - 4rem); }
  body.article-page .reader-paper { padding: 24px 18px 20px; }
  body.article-page .article-title, body.article-page .reader-heading:has(.title-zh) .title-zh { font-size: 24px; }
}
@media (max-width: 360px) {
  body.article-page .reader-back, body.article-page .reader-toolbar .vote-button,
  body.article-page .reader-toolbar .icon-action, body.article-page .reader-toolbar .industry-editor summary,
  body.article-page .reader-toolbar .act-valid .vote-button, body.article-page .reader-toolbar .act-invalid .vote-button {
    width: 34px; height: 38px; min-height: 38px; }
  body.article-page .reader-tabs button { padding: 0 7px; }
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) body.feed-page, :root:not([data-theme=light]) :is(body.embedded-detail, body.article-page) {
    --bg: #14171a; --surface: #1a1d21; --text: #e9ecef; --muted: #8b939c; --border: #2a2f35;
    --accent: #8db4f0; --tint: #233450; --neutral-bg: #262b31; --valid: #6fd09a;
    --valid-bg: #1b3326; --invalid: #f09a86; --invalid-bg: #3a2420; }
  :root:not([data-theme=light]) .feed-items .item:hover { background: #1e2227; }
  :root:not([data-theme=light]) .feed-items .item.is-selected { background: #1f2a3b; }
  :root:not([data-theme=light]) .feed-items .item .summary, :root:not([data-theme=light]) .feed-items .item .badge,
  :root:not([data-theme=light]) :is(body.embedded-detail, body.article-page) .badge, :root:not([data-theme=light]) :is(body.embedded-detail, body.article-page) .article-intro { color: #b3bac2; }
}
:root[data-theme=dark] body.feed-page, :root[data-theme=dark] :is(body.embedded-detail, body.article-page) {
  --bg: #14171a; --surface: #1a1d21; --text: #e9ecef; --muted: #8b939c; --border: #2a2f35;
  --accent: #8db4f0; --tint: #233450; --neutral-bg: #262b31; --valid: #6fd09a;
  --valid-bg: #1b3326; --invalid: #f09a86; --invalid-bg: #3a2420; }
:root[data-theme=dark] .feed-items .item:hover { background: #1e2227; }
:root[data-theme=dark] .feed-items .item.is-selected { background: #1f2a3b; }
:root[data-theme=dark] .feed-items .item .summary, :root[data-theme=dark] .feed-items .item .badge,
:root[data-theme=dark] :is(body.embedded-detail, body.article-page) .badge, :root[data-theme=dark] :is(body.embedded-detail, body.article-page) .article-intro { color: #b3bac2; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) button, :root:not([data-theme=light]) .quick a[aria-current=page] { color: #101720; }
  :root:not([data-theme=light]) .vote-button, :root:not([data-theme=light]) .icon-action { color: var(--muted); }
  :root:not([data-theme=light]) .theme-toggle, :root:not([data-theme=light]) .feed-user summary,
  :root:not([data-theme=light]) .filter-panel summary, :root:not([data-theme=light]) .quick a[aria-current=page] { color: var(--text); }
}
:root[data-theme=dark] button, :root[data-theme=dark] .quick a[aria-current=page] { color: #101720; }
:root[data-theme=dark] .vote-button, :root[data-theme=dark] .icon-action { color: var(--muted); }
:root[data-theme=dark] .theme-toggle, :root[data-theme=dark] .feed-user summary,
:root[data-theme=dark] .filter-panel summary, :root[data-theme=dark] .quick a[aria-current=page] { color: var(--text); }
"""

_LABEL_SCRIPT = """<script>
function atlasApplyLabels(card, values, tagValue) {
  const labels = card.querySelector('.labels');
  const badges = card.querySelector('.industry-badges');
  const row = card.querySelector('.tag-row');
  if (!labels || !badges || !row) return;
  if (tagValue && !Array.from(badges.querySelectorAll('.badge')).some(node => node.textContent === tagValue)) {
    const badge = document.createElement('a');
    badge.className = 'badge tag-ai';
    badge.href = '/feed?' + new URLSearchParams({tag: tagValue});
    badge.textContent = tagValue;
    if (document.body.classList.contains('embedded-detail')) badge.target = '_top';
    badges.append(badge);
  }
  const allBadges = Array.from(badges.querySelectorAll('.badge'));
  let overflow = badges.querySelector('.tag-overflow');
  if (allBadges.length > 3) {
    if (!overflow) { overflow = document.createElement('span'); overflow.className = 'tag-overflow'; }
    overflow.textContent = '+' + (allBadges.length - 3);
    overflow.title = allBadges.slice(3).map(node => node.textContent).join('、');
    overflow.setAttribute('aria-label', '另有 ' + (allBadges.length - 3) + ' 个标签');
    badges.append(overflow);
  } else if (overflow) overflow.remove();
  const keys = Object.keys(values).filter(key => key !== 'industry' && key !== 'valid' && !key.startsWith('tag:')).sort();
  labels.replaceChildren();
  keys.forEach(key => {
    const tag = document.createElement('span');
    tag.className = 'label-tag tag-neutral';
    tag.dataset.labelKey = key;
    tag.dataset.labelValue = values[key];
    tag.textContent = key + '=' + values[key];
    labels.append(tag);
  });
  row.classList.toggle('is-empty', !badges.children.length && !labels.children.length);
  card.querySelectorAll('.vote-button').forEach(button => {
    button.setAttribute('aria-pressed', String(button.form.elements.label_value.value === values.valid));
  });
}
function atlasLabelIdentity(card) {
  return ['rawId', 'rawSha256', 'charStart', 'charEnd'].map(key => card.dataset[key] || '');
}
const atlasPendingLabels = [];
function atlasNotifyLabels(card, values, tagValue) {
  const message = {type: 'atlas-label-saved', identity: atlasLabelIdentity(card), values, tagValue};
  if (window.parent !== window && document.body.classList.contains('embedded-detail')) {
    window.parent.postMessage(message, location.origin);
  } else {
    const frame = document.getElementById('feed-detail-frame');
    if (frame && frame.contentWindow && window.matchMedia('(min-width: 1100px)').matches) {
      if (frame.hidden) atlasPendingLabels.push(message);
      else frame.contentWindow.postMessage(message, location.origin);
    }
  }
}
function atlasFlushLabels(frame) {
  while (atlasPendingLabels.length) frame.contentWindow.postMessage(atlasPendingLabels.shift(), location.origin);
}
window.addEventListener('message', event => {
  if (event.origin !== location.origin || !event.data || event.data.type !== 'atlas-label-saved') return;
  const frame = document.getElementById('feed-detail-frame');
  const fromDetail = frame && event.source === frame.contentWindow;
  const fromFeed = document.body.classList.contains('embedded-detail') && event.source === window.parent;
  if (!fromDetail && !fromFeed) return;
  if (!Array.isArray(event.data.identity) || !event.data.values || typeof event.data.values !== 'object') return;
  document.querySelectorAll('article.item[data-raw-id]').forEach(card => {
    if (atlasLabelIdentity(card).every((value, index) => value === event.data.identity[index])) {
      atlasApplyLabels(card, event.data.values, event.data.tagValue);
    }
  });
});
document.addEventListener('submit', async (event) => {
  const form = event.target;
  if (!form.matches('form.act[action="/label"]')) return;
  const card = form.closest('article.item');
  if (!card || !window.fetch) return;
  event.preventDefault();
  if (card.getAttribute('aria-busy') === 'true') return;

  const body = new URLSearchParams(new FormData(form));
  const status = card.querySelector('.label-status');
  clearTimeout(status._clearTimer);
  const focused = card.contains(document.activeElement) ? document.activeElement : null;
  const controls = Array.from(card.querySelectorAll('.actions button, .actions select, .actions input:not([type=hidden])'));
  const disabled = controls.map((control) => control.disabled);
  card.setAttribute('aria-busy', 'true');
  controls.forEach((control) => { control.disabled = true; });
  status.removeAttribute('data-error');
  status.textContent = '正在保存…';
  try {
    const response = await fetch(form.action, {
      method: 'POST', body, headers: {Accept: 'application/json'},
      mode: 'same-origin', redirect: 'error'
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || result.error || '保存失败，请重试。');
    if (result.raw_id !== body.get('raw_id') || !result.values) {
      throw new Error('未能确认最新标签，请刷新后确认。');
    }
    const tagValue = body.get('label_key') === 'tag' ? body.get('label_value').trim() : null;
    atlasApplyLabels(card, result.values, tagValue);
    atlasNotifyLabels(card, result.values, tagValue);
    const splitActive = document.getElementById('feed-detail-frame') && window.matchMedia('(min-width: 1100px)').matches;
    if (tagValue && !splitActive && !document.body.classList.contains('embedded-detail')) {
      window.location.reload();
      return;
    }
    if (tagValue) {
      form.reset();
      const editor = form.closest('.industry-editor');
      if (editor) editor.open = false;
    }
    status.textContent = '已保存';
    if (document.body.matches('.embedded-detail, .article-page')) {
      status._clearTimer = setTimeout(() => { status.textContent = ''; }, 2500);
    }
  } catch (error) {
    status.setAttribute('data-error', '');
    status.textContent = error instanceof TypeError || error instanceof SyntaxError
      ? '未能确认保存结果，请检查网络后重试。' : error.message;
  } finally {
    card.removeAttribute('aria-busy');
    controls.forEach((control, index) => { control.disabled = disabled[index]; });
    if (focused && document.activeElement === document.body) focused.focus({preventScroll: true});
  }
});
</script>"""

_LABEL_STATUS = '<p class="label-status" role="status" aria-live="polite"></p>'

_READER_SCRIPT = """<script>
const atlasThemeButton = document.querySelector('.theme-toggle');
function atlasThemeDark() {
  const theme = document.documentElement.dataset.theme;
  return theme ? theme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
}
function atlasUpdateThemeButton() {
  if (!atlasThemeButton) return;
  const dark = atlasThemeDark();
  atlasThemeButton.setAttribute('aria-label', dark ? '切换到浅色模式' : '切换到深色模式');
  atlasThemeButton.title = dark ? '切换到浅色模式' : '切换到深色模式';
  atlasThemeButton.innerHTML = dark
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4m11.4-11.4 1.4-1.4"/></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg>';
}
function atlasSetTheme(theme) {
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  atlasUpdateThemeButton();
}
if (atlasThemeButton) {
  atlasUpdateThemeButton();
  atlasThemeButton.addEventListener('click', () => {
    const theme = atlasThemeDark() ? 'light' : 'dark';
    atlasSetTheme(theme);
    try { localStorage.setItem('atlas-theme', theme); } catch (_) {}
    const frame = document.getElementById('feed-detail-frame');
    if (frame && frame.contentWindow) frame.contentWindow.postMessage({type: 'atlas-theme', theme}, location.origin);
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', atlasUpdateThemeButton);
}
window.addEventListener('storage', event => {
  if (event.key === 'atlas-theme') atlasSetTheme(event.newValue);
});
window.addEventListener('message', event => {
  if (event.origin === location.origin && event.source === window.parent &&
      document.body.classList.contains('embedded-detail') && event.data && event.data.type === 'atlas-theme') {
    atlasSetTheme(event.data.theme);
  }
});
const atlasDetailFrame = document.getElementById('feed-detail-frame');
const atlasWideFeed = atlasDetailFrame && window.matchMedia('(min-width: 1100px)');
let atlasManualSelection = false;
let atlasSelectedUrl = '';
function atlasOpenDetail(card, manual = false) {
  if (!atlasDetailFrame || !card || !card.dataset.detailUrl) return;
  const url = card.dataset.detailUrl;
  atlasManualSelection = atlasManualSelection || manual;
  document.querySelectorAll('article[data-detail-url]').forEach(item => {
    const selected = item === card;
    item.classList.toggle('is-selected', selected);
    const link = item.querySelector('a.title');
    if (link) {
      if (selected) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
  });
  try { sessionStorage.setItem('atlas-detail:' + location.pathname + location.search, url); } catch (_) {}
  if (url === atlasSelectedUrl) return;
  atlasSelectedUrl = url;
  const placeholder = document.querySelector('.detail-placeholder');
  placeholder.hidden = false;
  placeholder.textContent = '正在加载文章…';
  atlasDetailFrame.hidden = true;
  const target = new URL(url, location.href);
  target.searchParams.set('embedded', '1');
  atlasDetailFrame.contentWindow.location.replace(target.href);
}
if (atlasDetailFrame) {
  atlasDetailFrame.addEventListener('load', () => {
    if (!atlasSelectedUrl) return;
    try {
      const current = new URL(atlasDetailFrame.contentWindow.location.href);
      if (current.pathname === '/login') {
        location.assign('/login?' + new URLSearchParams({return_to: location.pathname + location.search}));
        return;
      }
      const expected = new URL(atlasSelectedUrl, location.href);
      if (current.pathname !== expected.pathname ||
          Array.from(expected.searchParams).some(([key, value]) => current.searchParams.get(key) !== value)) return;
    } catch (_) { /* A cross-origin access page remains in the detail pane. */ }
    document.querySelector('.detail-placeholder').hidden = true;
    atlasDetailFrame.hidden = false;
    atlasFlushLabels(atlasDetailFrame);
  });
  const selectInitial = () => {
    let saved = '';
    try { saved = sessionStorage.getItem('atlas-detail:' + location.pathname + location.search) || ''; } catch (_) {}
    const cards = Array.from(document.querySelectorAll('article[data-detail-url]'));
    atlasOpenDetail(cards.find(card => card.dataset.detailUrl === saved) || cards[0]);
  };
  if (atlasWideFeed.matches) selectInitial();
  atlasWideFeed.addEventListener('change', event => {
    if (event.matches && !atlasSelectedUrl) selectInitial();
    else if (!event.matches && atlasManualSelection && atlasSelectedUrl) location.assign(atlasSelectedUrl);
  });
}
if (document.body.classList.contains('embedded-detail')) {
  document.querySelectorAll('a[href^="/feed"], a[href^="/entry"]').forEach(link => { link.target = '_top'; });
}
document.addEventListener('click', (event) => {
  let dismissed = false;
  document.querySelectorAll('.feed-header details[open]').forEach((panel) => {
    if (!panel.contains(event.target)) panel.open = false;
  });
  document.querySelectorAll('.industry-editor[open]').forEach((editor) => {
    if (!editor.contains(event.target)) { editor.open = false; dismissed = true; }
  });
  if (dismissed && event.target.closest('article[data-detail-url]') &&
      !event.target.closest('a,button,input,select,textarea,summary,details,form,dialog')) {
    event.preventDefault();
  }
  const menu = event.target.closest('.menu-trigger');
  if (menu) {
    const sheet = menu.closest('article').querySelector('.bottom-sheet');
    if (sheet && !sheet.open) {
      sheet.showModal();
      sheet.querySelector('.sheet-content').focus({preventScroll: true});
    }
    return;
  }
  const close = event.target.closest('.sheet-close');
  if (close) { close.closest('dialog').close(); return; }
  if (event.target.matches('dialog.bottom-sheet')) {
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom) event.target.close();
    return;
  }
});
document.addEventListener('click', (event) => {
  const card = event.target.closest('article[data-detail-url]');
  if (!card || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const title = event.target.closest('a.title');
  if (title && atlasWideFeed && atlasWideFeed.matches) {
    event.preventDefault();
    atlasOpenDetail(card, true);
    return;
  }
  if (event.target.closest('a,button,input,select,textarea,summary,details,form,dialog') || window.getSelection().toString()) return;
  if (atlasWideFeed && atlasWideFeed.matches) atlasOpenDetail(card, true);
  else window.location.assign(card.dataset.detailUrl);
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || event.defaultPrevented) return;
  document.querySelectorAll('.feed-header details[open]').forEach(panel => {
    const focusedInside = panel.contains(document.activeElement);
    panel.open = false;
    if (focusedInside) panel.querySelector('summary').focus();
  });
  document.querySelectorAll('.industry-editor[open]').forEach((editor) => {
    const focusedInside = editor.contains(document.activeElement);
    editor.open = false;
    if (focusedInside) editor.querySelector('summary').focus();
  });
});
document.addEventListener('error', (event) => {
  const img = event.target;
  if (!(img instanceof HTMLImageElement) || !img.closest('.article-body')) return;
  const notice = document.createElement('span');
  notice.className = 'image-unavailable';
  notice.textContent = (img.alt || '图片') + '（图片暂不可用）';
  img.replaceWith(notice);
}, true);
const tabs = document.querySelector('.reader-tabs');
if (tabs) {
  const buttons = Array.from(tabs.querySelectorAll('[role="tab"]'));
  const translated = document.getElementById('body-chinese');
  let loaded = false;
  let loading = false;
  async function loadTranslation() {
    if (loaded || loading) return;
    loading = true;
    translated.replaceChildren();
    const status = document.createElement('p');
    status.className = 'translation-status';
    status.setAttribute('role', 'status');
    status.textContent = '正在翻译中文全文…';
    const body = document.createElement('div');
    body.className = 'article-body';
    translated.append(status, body);
    let initialized = false;
    let lastRevision = -1;
    const show = (result) => {
      if (typeof result.revision === 'number') {
        if (result.revision < lastRevision) return;
        lastRevision = result.revision;
      }
      if (result.html) {
        // HTML comes from the server's sanitized document and escaped text translations.
        const template = document.createElement('template');
        template.innerHTML = result.html;
        const updates = template.content.querySelectorAll('[data-atlas-translation-node]');
        const existing = body.querySelectorAll('[data-atlas-translation-node]');
        if (initialized && updates.length && updates.length === existing.length) {
          const byId = new Map(Array.from(existing, node => [node.dataset.atlasTranslationNode, node]));
          if (Array.from(updates).every(node => byId.has(node.dataset.atlasTranslationNode))) {
            updates.forEach(node => {
              const target = byId.get(node.dataset.atlasTranslationNode);
              if (target.textContent !== node.textContent) target.textContent = node.textContent;
            });
          } else body.replaceChildren(template.content);
        } else body.replaceChildren(template.content);
        initialized = true;
      }
      if (result.patches && initialized) {
        const byId = new Map(Array.from(body.querySelectorAll('[data-atlas-translation-node]'),
          node => [node.dataset.atlasTranslationNode, node]));
        for (const [id, text] of Object.entries(result.patches)) {
          const target = byId.get(id);
          if (target && target.textContent !== text) target.textContent = text;
        }
      }
      if (result.status === 'running') {
        status.textContent = '正在翻译… ' + result.progress + '%（未完成部分显示原文）';
      } else if (result.status === 'ready') {
        status.textContent = 'AI 中文译文';
      }
    };
    try {
      let response = await fetch(tabs.dataset.translationUrl, {method: 'POST', headers: {Accept: 'application/json'}});
      let result = await response.json();
      if (!response.ok) throw new Error(result.message || '翻译请求失败');
      show(result);
      if (result.status === 'running' && window.EventSource && result.stream_url) {
        const streamed = await new Promise(resolve => {
          const source = new EventSource(result.stream_url);
          let lastEventAt = Date.now(), polling = false, settled = false;
          const finish = value => {
            if (settled) return;
            settled = true;
            clearInterval(watchdog);
            source.close();
            resolve(value);
          };
          // Some proxies keep the connection open while buffering the event stream.
          const watchdog = setInterval(async () => {
            if (settled || polling || Date.now() - lastEventAt < 4000) return;
            polling = true;
            try {
              const response = await fetch('/translation?key=' + encodeURIComponent(result.key), {headers: {Accept: 'application/json'}});
              const next = await response.json();
              if (settled) return;
              if (!response.ok) { finish(null); return; }
              show(next);
              if (next.status !== 'running' && (typeof next.revision !== 'number' || next.revision >= lastRevision)) finish(next);
            } catch (_) { /* keep waiting for SSE or the next retry */ }
            finally { polling = false; }
          }, 1200);
          source.addEventListener('update', (event) => {
            try {
              const next = JSON.parse(event.data);
              lastEventAt = Date.now();
              show(next);
              if (next.status !== 'running' && (typeof next.revision !== 'number' || next.revision >= lastRevision)) finish(next);
            } catch (_) { finish(null); }
          });
          source.onerror = () => finish(null);
        });
        if (streamed) result = streamed;
      }
      while (result.status === 'running') {
        await new Promise(resolve => setTimeout(resolve, 1200));
        response = await fetch('/translation?key=' + encodeURIComponent(result.key), {headers: {Accept: 'application/json'}});
        result = await response.json();
        if (!response.ok) throw new Error(result.message || '读取翻译进度失败');
        show(result);
      }
      if (result.status !== 'ready') throw new Error(result.message || '全文翻译尚未完成，请重试。');
      loaded = true;
    } catch (error) {
      status.textContent = error instanceof TypeError ? '网络暂不可用，翻译结果尚未确认。' : error.message;
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = '重试翻译';
      retry.addEventListener('click', loadTranslation);
      translated.append(retry);
    } finally { loading = false; }
  }
  buttons.forEach((button) => button.addEventListener('click', () => {
    buttons.forEach((tab) => {
      const selected = tab === button;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !selected;
    });
    if (button.id === 'tab-chinese') loadTranslation();
  }));
  tabs.addEventListener('keydown', (event) => {
    const index = buttons.indexOf(document.activeElement);
    if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus(); buttons[next].click();
  });
  if (document.getElementById('tab-chinese').getAttribute('aria-selected') === 'true') loadTranslation();
}
</script>"""

_SHELL = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1$viewport_fit">
<title>$title</title>
<script>try { const theme = localStorage.getItem('atlas-theme'); if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme; } catch (_) {}</script>
<style>$style</style>
</head>
<body class="$body_class">
<h1>$heading</h1>
$body
$tracking
<footer>Atlas · 个人信息流</footer>
</body>
</html>
"""
)

#: 每一条信息一个「提交一个判断」的表单（**一条一次点击**）。
#: `$anchor_fields` 为空 ⇒ 文档级（1A）；非空 ⇒ 条目级（2C）。`raw_id` 已在前面的
#: 隐藏字段里，锚点字段只补 `raw_sha256` / `char_start` / `char_end` ——
#: **绝不包含 `entry_id`**（T-130：ID 是派生量，换解析器会漂移）。
_ACTION_FORM = Template(
    """<form class="act act-$label_value" method="post" action="/label">
<input type="hidden" name="raw_id" value="$raw_id">
<input type="hidden" name="label_key" value="$label_key">
$anchor_fields<input type="hidden" name="label_value" value="$label_value">
<input type="hidden" name="actor" value="$actor">
<input type="hidden" name="return_to" value="$return_to">
<button class="vote-button" type="submit" aria-label="$caption" title="$caption" aria-pressed="$pressed">$icon</button>
</form>"""
)

#: 条目表单额外带的锚点字段（与 `atlas.webui.app.FORM_FIELDS` 一一对应）。
_ANCHOR_FIELDS = Template(
    """<input type="hidden" name="raw_sha256" value="$raw_sha256">
<input type="hidden" name="char_start" value="$char_start">
<input type="hidden" name="char_end" value="$char_end">
"""
)

#: 行业修正表单：取值来自 `<select name="label_value">`（因此不放隐藏的 `label_value`）。
_ACTION_SELECT_FORM = Template(
    """<form class="act" method="post" action="/label">
<input type="hidden" name="raw_id" value="$raw_id">
<input type="hidden" name="label_key" value="$label_key">
$anchor_fields$select
<input type="hidden" name="actor" value="$actor">
<input type="hidden" name="return_to" value="$return_to">
<button type="submit">$caption</button>
</form>"""
)


def _shell(*, title: str, heading: str, body: str, body_class: str = "") -> str:
    return _SHELL.substitute(
        title=escape(title), heading=escape(heading), style=_STYLE, body=body,
        body_class=escape(body_class), tracking=TRACKING_SCRIPT,
        viewport_fit=", viewport-fit=cover" if body_class == "feed-page" else "",
    )



def render_login_page(return_to: str, error: str = "") -> str:
    message = f'<p role="alert">{escape(error)}</p>' if error else ""
    body = (f'<main class="login-card">{message}'
            f'<form method="post" action="/login"><label for="name">名字</label>'
            f'<input id="name" name="name" type="text" maxlength="{MAX_ACTOR_LENGTH}" autocomplete="username" required autofocus>'
            f'<input type="hidden" name="return_to" value="{escape(return_to)}">'
            '<button type="submit" aria-live="polite">登录</button></form></main>'
            '''<script>
(() => {
  const form = document.querySelector('.login-card form');
  const button = form.querySelector('button[type="submit"]');
  let submitting = false;
  form.addEventListener('submit', (event) => {
    if (submitting) { event.preventDefault(); return; }
    submitting = true;
    button.disabled = true;
    const spinner = document.createElement('span');
    spinner.className = 'login-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    button.setAttribute('aria-label', '登录中');
    button.replaceChildren(spinner);
  });
  window.addEventListener('pageshow', () => {
    submitting = false;
    button.disabled = false;
    button.textContent = '登录';
    button.removeAttribute('aria-label');
  });
})();
</script>''')
    return _shell(title="登录", heading="登录", body=body)

# ---------------------------------------------------------------------- #
# feed 页面
# ---------------------------------------------------------------------- #
def render_feed_page(
    result: FeedResult,
    *,
    industries: Sequence[str],
    values: Mapping[str, Mapping[str, str]],
    actor: str,
    return_to: str | None = None,
    show_account: bool = False,
    channel_names: Mapping[str, str] | None = None,
    channel_groups: Mapping[str, str] | None = None,
) -> str:
    """渲染一页 feed：筛选器 + 排序 + 分页 + 每条一次点击打标。

    Args:
        result: `atlas.feed.query.run_query` 的结果（本模块不重算筛选/排序/分页）。
            条目粒度时 `result.items` 是 `FeedEntry`（T-109 的默认路径）；
            文档粒度时是 `FeedItem`（旧的 `/feed` 调用路径仍然可用）。
        industries: **注入的行业配置**（C8：配置 = 筛选维度 = 打标修正对象）。
        values: `raw_id -> {label_key: 最新取值}`，由调用方从 Confirmed 存储读出。
        actor: 打标人（写进 `ConfirmedLabel.actor`）。
        return_to: 打标成功后回到的地址；默认为本页的规范查询串。
    """
    channel_names = channel_names or {}
    page_url = return_to or feed_query_string(result.query, offset=result.offset)
    filter_mask = sum(bit for enabled, bit in (
        (bool(result.query.channels), 1), (bool(result.query.industries), 2),
        (bool(result.query.tags), 4), (bool(result.query.labels), 8),
        (result.query.labeled is not None, 16), (result.query.since is not None, 32),
        (result.query.until is not None, 64),
    ) if enabled)
    parts: list[str] = [
        f'<div hidden data-feed-context data-strategy="chronological-v1" '
        f'data-order="{escape(result.query.order)}" data-offset="{result.offset}" '
        f'data-limit="{result.limit}" data-total="{result.total}" data-filter-mask="{filter_mask}"></div>'
    ]
    if show_account:
        parts.append(f'<div class="account"><span>当前用户：{escape(actor)}</span><form method="post" action="/logout"><button type="submit">退出登录</button></form></div>')
    parts += [
        '<div class="feed-header-main">',
        '<div class="feed-brand"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg><strong>Atlas</strong><span>Feed</span></div>',
        '<a class="feed-source-management" href="/sources" aria-label="信源管理" title="信源管理"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1"/></svg></a>',
        _render_filters(result.query, industries),
        '<button class="theme-toggle" type="button" aria-label="切换主题" title="切换主题"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg></button>',
    ]
    if show_account:
        parts.append(f'<details class="feed-user"><summary title="用户" aria-label="当前用户：{escape(actor)}"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg></summary><div><span>当前用户：{escape(actor)}</span><p>标签展示与筛选仅使用当前用户的判断。</p><form method="post" action="/logout"><button type="submit">退出登录</button></form></div></details>')
    parts += [
        '</div><div class="feed-header-controls">',
        _render_source_menu(result.query, result.channels, channel_names, channel_groups),
        _render_source_tabs(result.query, result.channels, channel_names),
        _render_quick_switches(result.query),
        _render_compact_pager(result),
        '</div>',
    ]
    header = "\n".join(parts)
    parts = ['<div class="feed-top-pager">' + _render_pager(result) + '</div>']
    if not result.items:
        parts.append(
            '<p class="empty">没有符合条件的记录。可放宽筛选条件，或先跑采集。</p>'
        )
    for item in result.items:
        labels = values.get(item.raw_id, {})
        if isinstance(item, FeedEntry):
            if item.confirmed_values is not None:
                labels = dict(item.confirmed_values)
            parts.append(
                _render_entry(
                    item,
                    labels,
                    industries=industries,
                    actor=actor,
                    return_to=page_url,
                    source_name=channel_names.get(item.channel_id, item.channel_id),
                )
            )
        else:
            parts.append(
                _render_item(
                    item,
                    labels,
                    industries=industries,
                    actor=actor,
                    return_to=page_url,
                    source_name=channel_names.get(item.channel_id, item.channel_id),
                )
            )
    layout = (
        '<main class="feed-layout"><section class="feed-list" aria-label="信息流">'
        '<header class="feed-header">' + header + '</header>'
        '<div class="feed-items">' + "\n".join(parts) + '</div></section>'
        '<aside class="feed-detail" aria-label="文章详情">'
        '<p class="detail-placeholder" role="status">选择一篇文章查看详情</p>'
        '<iframe id="feed-detail-frame" name="atlas-detail" title="文章详情" hidden></iframe>'
        '</aside></main>'
    )
    return _shell(title="Atlas Feed", heading="Atlas Feed", body=layout + _LABEL_SCRIPT + _READER_SCRIPT, body_class="feed-page")


def _render_filters(query: FeedQuery, industries: Sequence[str]) -> str:
    desc = " selected" if query.order == "desc" else ""
    asc = " selected" if query.order == "asc" else ""
    preserved = [("channel", value) for value in query.channels]
    preserved += [("industry", value) for value in query.industries]
    preserved += [("tag", value) for value in query.tags[1:]]
    if query.labels:
        preserved.append(("labels", ",".join(query.labels)))
    if query.labeled is not None:
        preserved.append(("labeled", "true" if query.labeled else "false"))
    if query.since is not None:
        preserved.append(("since", query.since.isoformat()))
    if query.until is not None:
        preserved.append(("until", query.until.isoformat()))
    if query.granularity == "document":
        preserved.append(("granularity", query.granularity))
    hidden = "".join(
        f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'
        for name, value in preserved
    )
    return (
        '<details class="filter-panel"><summary aria-label="筛选与排序"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg><span>筛选与排序</span></summary>'
        '<form class="filters" method="get" action="/feed">'
        + hidden
        + '<label>标签'
        f'<input type="text" name="tag" value="{escape(query.tags[0] if query.tags else "")}" placeholder="输入标签筛选">'
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
        "</form></details>"
    )


def _render_source_menu(query: FeedQuery, channels: Sequence[str], names: Mapping[str, str] | None = None, groups: Mapping[str, str] | None = None) -> str:
    names = names or {}
    selected = "全部来源" if not query.channels else "、".join(names.get(channel, channel) for channel in query.channels)
    active_channels = frozenset(query.channels)

    def link(label: str, selected_channels: tuple[str, ...], *, active: bool | None = None) -> str:
        href = feed_query_string(query, offset=0, channels=selected_channels)
        if active is None:
            active = active_channels == frozenset(selected_channels)
        current = ' aria-current="page"' if active else ''
        return f'<a href="{escape(href)}" title="{escape(label)}"{current}>{escape(label)}</a>'

    available = sorted(set(channels) | set(query.channels), key=lambda channel: (names.get(channel, channel).casefold(), channel))
    if groups:
        grouped: dict[str, list[str]] = {}
        for channel in available:
            grouped.setdefault(groups.get(channel, "其他来源"), []).append(channel)
        shortcuts = []
        sections = []
        for index, group in enumerate(sorted(grouped, key=lambda label: (label == "其他来源", label.casefold()))):
            channels_in_group = grouped[group]
            all_label = group.removesuffix(" 信源") + "全部"
            # 单来源组仍优先展示具体来源；多来源选择不受 URL 参数顺序影响。
            group_active = len(channels_in_group) > 1 and active_channels == frozenset(channels_in_group)
            if group_active:
                selected = all_label
            shortcuts.append(link(all_label, tuple(channels_in_group), active=group_active))
            label_id = f"source-group-{index}"
            sections.append(
                f'<div class="source-menu-group" role="group" aria-labelledby="{label_id}">'
                f'<div class="source-menu-group-title" id="{label_id}">{escape(group)}'
                f'<span class="source-menu-group-count">（{len(channels_in_group)}）</span></div>'
                + "".join(link(names.get(channel, channel), (channel,)) for channel in channels_in_group)
                + '</div>'
            )
        items = "".join(shortcuts + sections)
    else:
        items = "".join(link(names.get(channel, channel), (channel,)) for channel in available)
    return (f'<details class="source-menu"><summary aria-label="切换来源" title="{escape(selected)}">'
            f'<span title="{escape(selected)}">{escape(selected)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>'
            '<nav aria-label="切换来源">' + link("全部来源", ())
            + items + '</nav></details>')


def _render_source_tabs(query: FeedQuery, channels: Sequence[str], names: Mapping[str, str] | None = None) -> str:
    names = names or {}
    def link(label: str, selected_channels: tuple[str, ...]) -> str:
        href = feed_query_string(query, offset=0, channels=selected_channels)
        current = ' aria-current="page"' if query.channels == selected_channels else ''
        return f'<a href="{escape(href)}"{current}>{escape(label)}</a>'

    # 显式查询不存在的渠道时也保留当前选择，避免页面没有选中状态。
    available = sorted(set(channels) | set(query.channels), key=lambda channel: (names.get(channel, channel).casefold(), channel))
    return (
        '<nav class="source-tabs" aria-label="来源">'
        + link("全部来源", ())
        + "".join(link(names.get(channel, channel), (channel,)) for channel in available)
        + '</nav>'
    )


def _render_quick_switches(query: FeedQuery) -> str:
    def link(label: str, labeled: Any) -> str:
        href = feed_query_string(query, offset=0, labeled=labeled)
        current = ' aria-current="page"' if query.labeled is labeled else ''
        return f'<a href="{escape(href)}"{current}>{escape(label)}</a>'

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


def _render_compact_pager(result: FeedResult) -> str:
    shown = len(result.items)
    first = result.offset + 1 if shown else 0
    last = result.offset + shown if shown else 0
    pages = max(1, (result.total + result.limit - 1) // result.limit)
    current = min(pages, result.offset // result.limit + 1)
    previous = max(0, result.offset - result.limit)

    def arrow(label: str, href: str | None, path: str) -> str:
        icon = f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{path}"/></svg>'
        if href is None:
            return f'<span class="page-arrow" aria-label="{label}" aria-disabled="true">{icon}</span>'
        return f'<a class="page-arrow" href="{escape(href)}" aria-label="{label}">{icon}</a>'

    before = feed_query_string(result.query, offset=previous) if result.offset else None
    after = feed_query_string(result.query, offset=result.next_offset) if result.next_offset is not None else None
    return (f'<nav class="compact-pager" aria-label="分页" title="本页 {first} 到 {last}，共 {result.total} 条"><span class="compact-count" '
            f'aria-label="本页 {first} 到 {last}，共 {result.total} 条">{first}–{last} / {result.total}</span>'
            + arrow('上一页', before, 'm15 6-6 6 6 6')
            + f'<span class="page-number" title="第 {current} 页，共 {pages} 页">{current}/{pages}</span>'
            + arrow('下一页', after, 'm9 6 6 6-6 6') + '</nav>')


# ---------------------------------------------------------------------- #
# 条目（T-109 的主渲染路径）
# ---------------------------------------------------------------------- #
def _label_line(labels: Mapping[str, str]) -> str:
    """行业使用行业徽标，有效性仅由拇指图标的选中状态展示。"""
    tags = " ".join(
        '<span class="label-tag tag-neutral" '
        f'data-label-key="{escape(key)}" data-label-value="{escape(labels[key])}">'
        f'{escape(key)}={escape(labels[key])}</span>'
        for key in sorted(labels) if key not in (LABEL_KEY_INDUSTRY, LABEL_KEY_VALID) and not key.startswith("tag:")
    )
    return f'<span class="labels">{tags}</span>'


def _tag_row(item: FeedEntry | FeedItem, labels: Mapping[str, str]) -> str:
    tags = getattr(item, "tags", ())
    if not tags and (not isinstance(item, FeedEntry) or item.confirmed_values is None):
        tags = getattr(item, "industry_values", ()) or ((item.industry,) if item.industry else ())
        tags = tuple(dict.fromkeys((*tags, *(v for k, v in labels.items() if k.startswith("tag:")))))
    legacy_manual = labels.get(LABEL_KEY_INDUSTRY)
    if legacy_manual and legacy_manual not in tags:
        tags = (*tags, legacy_manual)
    badges = " ".join(
        f'<a class="badge {"label-tag tag-manual" if tag == legacy_manual else "tag-ai"}"'
        + (f' data-label-key="industry" data-label-value="{escape(tag)}"' if tag == legacy_manual else '')
        + f' href="{escape("/feed?" + urlencode({"tag": tag}))}">{escape(tag)}</a>' for tag in tags
    )
    if len(tags) > 3:
        hidden_tags = "、".join(tags[3:])
        badges += (f'<span class="tag-overflow" title="{escape(hidden_tags)}" '
                   f'aria-label="另有 {len(tags) - 3} 个标签">+{len(tags) - 3}</span>')
    label_line = _label_line(labels)
    empty = not badges and label_line == '<span class="labels"></span>'
    return (
        f'<div class="tag-row{" is-empty" if empty else ""}" aria-label="文章标签">'
        + '<span class="industry-badges">' + badges + '</span>' + label_line + '</div>'
    )



def _menu_button() -> str:
    return ('<button type="button" class="icon-action menu-trigger" aria-label="更多操作" title="更多操作" aria-haspopup="dialog">'
            '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">'
            '<circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg></button>')


def _sheet_open() -> str:
    return ('<dialog class="bottom-sheet" aria-label="文章操作"><div class="sheet-content" tabindex="-1">'
            '<div class="sheet-bar"><h2>文章操作</h2><button type="button" class="icon-action sheet-close" aria-label="关闭" title="关闭">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true" focusable="false">'
            '<path d="M6 6l12 12M18 6 6 18"/></svg></button></div>'
            '<details class="record-details"><summary>归档详情</summary>')


def _source_action(url: str) -> str:
    return (f'<a class="icon-action" href="{_safe_href(url)}" target="_blank" rel="noreferrer noopener" aria-label="阅读原文" title="阅读原文">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
            '<path d="M13 5h6v6M19 5l-9 9"/><path d="M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg></a>')


def _tag_editor(item: Any, labels: Mapping[str, str], industries: Sequence[str], actor: str, return_to: str) -> str:
    return ('<details class="industry-editor"><summary class="icon-action" aria-label="添加标签" title="添加标签">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
            '<path d="M20 13l-7 7a2 2 0 0 1-2.8 0L4 13.8V4h9.8L20 10.2a2 2 0 0 1 0 2.8Z"/><circle cx="8" cy="8" r="1"/></svg></summary>'
            + _industry_action(item, labels, industries, actor, return_to) + '</details>')


def _tracking_identity(item: Any) -> str:
    fields = {
        "raw-id": item.raw_id, "entry-id": getattr(item, "entry_id", None),
        "raw-sha256": getattr(item, "raw_sha256", None),
        "char-start": getattr(item, "char_start", None),
        "char-end": getattr(item, "char_end", None), "channel-id": item.channel_id,
    }
    return " ".join(f'data-{key}="{escape(value)}"' for key, value in fields.items() if value is not None and value != "")


def _render_entry(
    entry: FeedEntry,
    labels: Mapping[str, str],
    *,
    industries: Sequence[str],
    actor: str,
    return_to: str,
    source_name: str = "",
) -> str:
    """一个条目：标题 + 原文链接 + 时间 + 区间 + 一条一次点击打标。

    刻意保留 `raw-id` / `item-{raw_id}` / `label-tag` 这些既有标识：
    它们是对外可断言的结构，换渲染粒度不该把它们弄丢（既有测试与页面锚点都在用）。
    """
    status = "" if entry.http_status is None else f" · HTTP {entry.http_status}"
    time_hint = "" if entry.published_from_entry else "（条目无发布时间，显示抓取时间）"
    title_hint = (
        '<span class="hint">（整篇条目：标题取自链接）</span>'
        if entry.entry_id is None
        else ""
    )
    link = entry.link or entry.endpoint
    problems = "".join(
        f'<p class="problem">条目层问题：{escape(text)}</p>' for text in entry.problems
    )
    return (
        f'<article class="item entry" id="item-{escape(entry.raw_id)}" data-detail-url="{escape(article_url(entry, back_to=return_to))}" data-tracking-list-item {_tracking_identity(entry)}>'
        '<div class="head">'
        f'<a class="title" href="{escape(article_url(entry, back_to=return_to))}">'
        f"{escape(entry.title)}</a>"
        + (f'<p class="title-zh" lang="zh-CN">{escape(entry.title_zh)}</p>' if entry.title_zh else '')
        + "</div>" + _menu_button()
        + f'<div class="meta feed-item-meta"><span>{escape(source_name or entry.channel_id)}</span>'
        f'<time datetime="{escape(entry.timestamp.isoformat())}">'
        f'{escape(entry.timestamp.strftime("%Y-%m-%d"))}'
        f'{" · 发布" if entry.published_from_entry else " · 采集"}</time></div>'
        + _tag_row(entry, labels)
        + (f'<p class="summary">{escape(entry.summary[:300])}</p>' if entry.summary else
           f'<p class="summary-origin summary-pending">{escape(entry.summary_origin)}</p>' if entry.summary_origin else '')
        + '<div class="actions">'
        + _valid_action(entry, actor, return_to, LABEL_VALUE_VALID, "标记有效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_VALID)
        + _valid_action(entry, actor, return_to, LABEL_VALUE_INVALID, "标记无效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_INVALID)
        + _source_action(link)
        + _tag_editor(entry, labels, industries, actor, return_to) + '</div>'
        + _LABEL_STATUS
        + _sheet_open()
        + title_hint
        + (f'<p class="body-status">{escape(entry.body_note)}</p>' if entry.body_note else '')
        + (f'<p class="meta">分类模型：{escape(entry.classification_model)}</p>' if entry.classification_model else '')
        + ('<p class="meta">本次运行复用了历史提议；这里显示历史 AI 建议，尚未人工复核。</p>' if entry.industry_source == 'history' else '')
        + f'<div class="endpoint"><a href="{_safe_href(link)}" rel="noreferrer noopener">'
        f"{escape(link)}</a></div>"
        f'<div class="meta">{escape(entry.timestamp.isoformat())}{escape(time_hint)}'
        f" · 区间 [{escape(entry.char_start)}, {escape(entry.char_end)})"
        f" · {escape(entry.byte_length)} 字节{escape(status)}</div>"
        f'<div class="trace"><code class="raw-id">{escape(entry.raw_id)}</code>'
        + (f' · <code class="entry-id">{escape(entry.entry_id)}</code>' if entry.entry_id else "")
        + " · 锚点四元组："
        f'<code>raw_sha256={escape(entry.raw_sha256[:12])}…</code>'
        f"<code>char_start={escape(entry.char_start)}</code>"
        f"<code>char_end={escape(entry.char_end)}</code>"
        "</div>"
        + problems
        + f'<p class="open"><a href="{escape(entry_anchor_url(entry))}">'
        "打开原文并高亮该条目区间</a></p>"
        + "</details></div></dialog></article>"
    )


def render_entry_page(
    entry: FeedEntry,
    *,
    content: str,
    sliced: bool,
    labels: Mapping[str, str],
    notice: str = "",
    back_to: str = "/feed",
    source_name: str = "",
) -> str:
    """「打开原文并高亮该条目区间」的页面。

    `content` 是**已经算好的展示文本**（调用方按 `[char_start, char_end)` 从
    **解码后的原文**上切出条目切片，再交付本函数）。本函数**只做转义与包裹**：

    - 高亮靠 `<mark>` 包住整个切片 —— 区间是**整数**给的，不涉及任何坐标推算；
    - 所有动态内容继续经 `escape()`（含 `quote=True`），与列表页同一道防线；
    - `sliced=False` 表示"这段切片不等于条目自身的区间"（整篇即条目，或条目层
      换了版本），此时**如实说明并未高亮**，不假装高亮过。
    """
    link = entry.link or entry.endpoint
    if sliced:
        highlight = (
            f'<p class="highlight-note">下面的内容是原文解码后 '
            f'<code>[{escape(entry.char_start)}, {escape(entry.char_end)})</code> '
            "区间的切片，整段即该条目（高亮 = 该区间本身）。</p>"
        )
        body = f'<blockquote class="entry-slice"><mark>{escape(content)}</mark></blockquote>'
    else:
        highlight = (
            '<p class="highlight-note">该条目没有精确匹配的字符区间'
            "（整篇即条目，或条目层换了版本）；下面按整篇 / 所在条目展示，**未高亮**。</p>"
        )
        body = f'<blockquote class="entry-slice">{escape(content)}</blockquote>'

    parts = [
        f'<div class="notice" data-tracking-detail {_tracking_identity(entry)}>'
        f'<p><strong>{escape(entry.title)}</strong></p>'
        f'<p><a href="{_safe_href(link)}" rel="noreferrer noopener">{escape(link)}</a></p>'
        f'<p class="meta">时间 {escape(entry.timestamp.isoformat())} · '
        f"来源 {escape(source_name or entry.channel_id)} · 区间 "
        f"[{escape(entry.char_start)}, {escape(entry.char_end)})</p>"
        + _tag_row(entry, labels)
        + f'<p class="trace"><code class="raw-id">{escape(entry.raw_id)}</code>'
        + (f' · <code class="entry-id">{escape(entry.entry_id)}</code>' if entry.entry_id else "")
        + f' · <code>raw_sha256={escape(entry.raw_sha256[:12])}…</code></p>'
        "</div>",
    ]
    if notice:
        parts.append(f'<div class="notice">{escape(notice)}</div>')
    if entry.body_note:
        parts.append(f'<p class="body-status">{escape(entry.body_note)}</p>')
    if entry.body_text:
        parts.append('<section class="saved-body"><h2>清洗后留存的正文</h2>'
                     f'<pre class="cleaned-body">{escape(entry.body_text)}</pre></section>')
    parts.extend(
        [
            highlight,
            body,
            f'<p><a href="{escape(back_to)}">返回 feed</a></p>',
        ]
    )
    return _shell(
        title=f"条目 · {entry.title}", heading="条目原文区间", body="\n".join(parts)
    )


def render_article_page(
    entry: FeedEntry, *, labels: Mapping[str, str], actor: str,
    industries: Sequence[str], back_to: str, body_html: str,
    native_chinese: bool, body_note: str = "", embedded: bool = False,
    source_name: str = "",
) -> str:
    """正文 HTML 仅接受阅读服务的清洗结果；其余字段继续逐项转义。"""
    return_to = article_url(entry, back_to=back_to)
    translation_url = return_to.replace("/article?", "/translate?", 1)
    tabs = ""
    if body_html and not native_chinese:
        tabs = (
            f'<div class="reader-tabs" role="tablist" aria-label="正文语言" data-translation-url="{escape(translation_url)}">'
            '<button type="button" role="tab" id="tab-original" aria-controls="body-original" aria-selected="false" tabindex="-1">原文</button>'
            '<button type="button" role="tab" id="tab-chinese" aria-controls="body-chinese" aria-selected="true" tabindex="0">中文</button></div>'
        )
    original = (
        '<div id="body-original"' + (' role="tabpanel" aria-labelledby="tab-original" tabindex="0" hidden' if tabs else '') + '>'
        '<div class="article-body">' + (body_html or '<p class="empty">正文暂不可用，可通过操作区的原文链接阅读。</p>') + '</div></div>'
    )
    if tabs:
        original += '<div id="body-chinese" role="tabpanel" aria-labelledby="tab-chinese" tabindex="0" lang="zh-CN"><p class="translation-status" role="status">正在准备中文全文…</p></div>'
    meta = (f'<div class="meta"><span>{escape(source_name or entry.channel_id)}</span>'
            f'<time datetime="{escape(entry.timestamp.isoformat())}">{escape(entry.timestamp.strftime("%Y-%m-%d %H:%M"))}'
            f'{" · 发布" if entry.published_from_entry else " · 采集（来源未提供发布时间）"}</time></div>')
    title = (f'<h2 class="article-title">{escape(entry.title)}</h2>'
             + (f'<p class="title-zh" lang="zh-CN">{escape(entry.title_zh)}</p>' if entry.title_zh else ''))
    intro = ('<section class="article-intro"><h3>摘要 / 简介</h3>'
             f'<p>{escape(entry.summary) if entry.summary else "暂无简介。"}</p>'
             + (f'<span class="summary-origin">{escape(entry.summary_origin)}</span>' if entry.summary_origin and not entry.summary else '')
             + '</section>')
    actions = ('<div class="actions" aria-label="文章打标">'
               + _valid_action(entry, actor, return_to, LABEL_VALUE_VALID, "标记有效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_VALID)
               + _valid_action(entry, actor, return_to, LABEL_VALUE_INVALID, "标记无效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_INVALID)
               + _source_action(entry.link or entry.endpoint)
               + _tag_editor(entry, labels, industries, actor, return_to) + '</div>')
    heading = title + meta + _tag_row(entry, labels)
    content = ('<section class="reader-content"><h3>正文</h3>'
               + (f'<p class="body-note">{escape(body_note)}</p>' if body_note else '')
               + original + '</section>')
    back = ('' if embedded else f'<a class="reader-back" href="{escape(back_to)}" aria-label="返回信息流">'
            '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 6-6 6 6 6"/></svg>'
            '<span>返回信息流</span></a>')
    content = ('<div class="reader-toolbar">' + back + tabs + actions + _menu_button() + _LABEL_STATUS + '</div>'
               + '<div class="reader-paper"><header class="reader-heading">' + heading + '</header>'
               + intro + content
               + f'<footer class="reader-footer"><span>来源 {escape(source_name or entry.channel_id)}</span>'
               + f'<span>{escape(entry.timestamp.strftime("%Y-%m-%d %H:%M"))}</span>'
               + f'<a href="{_safe_href(entry.link or entry.endpoint)}" target="_blank" rel="noreferrer noopener">阅读原文 ↗</a></footer></div>')
    body = (
        f'<article class="item article-detail" id="item-{escape(entry.raw_id)}" data-tracking-detail {_tracking_identity(entry)}>'
        + content + _sheet_open()
        + f'<p>渠道：{escape(entry.channel_id)}</p><p>来源：{escape(entry.link or entry.endpoint)}</p>'
        f'<p>采集时间：{escape(entry.fetched_at.isoformat())}</p>'
        f'<p>HTTP：{escape(entry.http_status)} · {entry.byte_length} 字节</p>'
        f'<p><code class="raw-id">{escape(entry.raw_id)}</code></p>'
        f'<p>SHA-256：<code>{escape(entry.raw_sha256)}</code></p>'
        f'<p>条目区间：[{entry.char_start}, {entry.char_end})</p>'
        + (f'<p>条目 ID：<code>{escape(entry.entry_id)}</code></p>' if entry.entry_id else '')
        + (f'<p>分类模型：{escape(entry.classification_model)}</p>' if entry.classification_model else '')
        + f'<p><a href="{escape(entry_anchor_url(entry))}">查看归档原文与条目锚点</a></p>'
        '</details></div></dialog></article>' + _LABEL_SCRIPT + _READER_SCRIPT
    )
    return _shell(title=entry.title + " · Atlas", heading="文章详情", body=body,
                  body_class="embedded-detail" if embedded else "article-page")


def _render_item(
    item: FeedItem,
    labels: Mapping[str, str],
    *,
    industries: Sequence[str],
    actor: str,
    return_to: str,
    source_name: str = "",
) -> str:
    status = "" if item.http_status is None else f" · HTTP {item.http_status}"
    return (
        f'<article class="item" id="item-{escape(item.raw_id)}" data-tracking-list-item {_tracking_identity(item)}>'
        '<div class="head">'
        f'<code class="raw-id">{escape(item.raw_id)}</code>'
        "</div>"
        f'<div class="endpoint"><a href="{_safe_href(item.endpoint)}" rel="noreferrer noopener">'
        f"{escape(item.endpoint)}</a></div>"
        f'<div class="meta">{escape(source_name or item.channel_id)} · {escape(item.fetched_at.isoformat())} · '
        f"{escape(item.byte_length)} 字节{escape(status)}</div>"
        + _tag_row(item, labels)
        + '<div class="actions">'
        + _valid_action(item, actor, return_to, LABEL_VALUE_VALID, "标记有效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_VALID)
        + _valid_action(item, actor, return_to, LABEL_VALUE_INVALID, "标记无效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_INVALID)
        + _source_action(item.endpoint)
        + _tag_editor(item, labels, industries, actor, return_to)
        + "</div>" + _LABEL_STATUS + "</article>"
    )


def _anchor_fields(item: Any) -> str:
    """条目表单的锚点隐藏字段；**文档级**（`FeedItem`）返回空串。

    锚点是三件套 `raw_sha256` / `char_start` / `char_end`（`raw_id` 另有字段）。
    `entry_id` **不在这里**：它是派生量，不作锚点（SPEC §6.3 / T-130）。
    """
    if not isinstance(item, FeedEntry):
        return ""
    return _ANCHOR_FIELDS.substitute(
        raw_sha256=escape(item.raw_sha256),
        char_start=escape(item.char_start),
        char_end=escape(item.char_end),
    )


def _valid_action(
    item: Any, actor: str, return_to: str, value: str, caption: str, *, selected: bool = False
) -> str:
    # 两枚拇指共用轮廓；向下图标绕中心旋转，保持线宽和尺寸一致。
    icon = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
        + ('<g transform="rotate(180 12 12)">' if value == LABEL_VALUE_INVALID else '<g>')
        + '<path d="M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3Z"/>'
        '<path d="m7 10 4-7a2 2 0 0 1 2 2v5h6a2 2 0 0 1 2 2l-1 7a2 2 0 0 1-2 2H7"/>'
        '</g></svg>'
    )
    return _ACTION_FORM.substitute(
        raw_id=escape(item.raw_id),
        label_key=escape(LABEL_KEY_VALID),
        anchor_fields=_anchor_fields(item),
        label_value=escape(value),
        actor=escape(actor),
        return_to=escape(return_to),
        caption=escape(caption),
        pressed="true" if selected else "false",
        icon=icon,
    )


def _industry_action(
    item: Any,
    labels: Mapping[str, str],
    industries: Sequence[str],
    actor: str,
    return_to: str,
) -> str:
    return _ACTION_SELECT_FORM.substitute(
        raw_id=escape(item.raw_id), label_key="tag", anchor_fields=_anchor_fields(item),
        select='<label>标签名称 <input name="label_value" maxlength="80" required></label>',
        actor=escape(actor), return_to=escape(return_to), caption="添加标签",
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
