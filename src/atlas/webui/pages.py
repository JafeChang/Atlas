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
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlencode

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
.feed-notices { font-size: 0.875rem; color: var(--muted); overflow-wrap: anywhere; }
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
@media (prefers-color-scheme: dark) {
  :root { --bg: #101720; --surface: #18212d; --text: #e5ebf3; --muted: #a8b6c8;
          --border: #354357; --accent: #8dbaff; --tint: #233653;
          --valid: #7cdbac; --valid-bg: #183c2d; --invalid: #ffa0b3; --invalid-bg: #45232d;
          --manual: #ccb0ff; --manual-bg: #342743; --neutral-bg: #25303d;
          --history: #efca83; --history-bg: #3c3220; }
  button, .quick a[aria-current=page] { color: #101720; }
  .vote-button, .icon-action { color: var(--muted); }
}
"""

_LABEL_SCRIPT = """<script>
document.addEventListener('submit', async (event) => {
  const form = event.target;
  if (!form.matches('form.act[action="/label"]')) return;
  const card = form.closest('article.item');
  if (!card || !window.fetch) return;
  event.preventDefault();
  if (card.getAttribute('aria-busy') === 'true') return;

  const body = new URLSearchParams(new FormData(form));
  const status = card.querySelector('.label-status');
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
    if (body.get('label_key') === 'tag') { window.location.reload(); return; }
    const labels = card.querySelector('.labels');
    const keys = Object.keys(result.values).filter((key) => key !== 'industry' && key !== 'valid' && !key.startsWith('tag:')).sort();
    labels.replaceChildren();
    keys.forEach((key) => {
      const tag = document.createElement('span');
      const value = result.values[key];
      tag.className = 'label-tag tag-neutral';
      tag.dataset.labelKey = key;
      tag.dataset.labelValue = value;
      tag.textContent = key + '=' + result.values[key];
      labels.append(tag);
    });
    card.querySelector('.tag-row').classList.toggle('is-empty',
      !card.querySelector('.industry-badges').children.length && !labels.children.length);
    card.querySelectorAll('.vote-button').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.form.elements.label_value.value === result.values.valid));
    });
    status.textContent = '已保存';
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
document.addEventListener('click', (event) => {
  let dismissed = false;
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
  if (event.target.closest('a,button,input,select,textarea,summary,details,form,dialog') || window.getSelection().toString()) return;
  window.location.assign(card.dataset.detailUrl);
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || event.defaultPrevented) return;
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
    status.textContent = '正在生成中文全文…';
    translated.append(status);
    try {
      let response = await fetch(tabs.dataset.translationUrl, {method: 'POST', headers: {Accept: 'application/json'}});
      let result = await response.json();
      if (!response.ok) throw new Error(result.message || '翻译请求失败');
      while (result.status === 'running') {
        status.textContent = '正在生成中文全文… ' + result.progress + '%';
        await new Promise((resolve) => setTimeout(resolve, 1200));
        response = await fetch('/translation?key=' + encodeURIComponent(result.key), {headers: {Accept: 'application/json'}});
        result = await response.json();
        if (!response.ok) throw new Error(result.message || '读取翻译进度失败');
      }
      if (result.status !== 'ready') throw new Error(result.message || '全文翻译尚未完成，请重试。');
      const body = document.createElement('div');
      body.className = 'article-body';
      // 此字段仅由服务端的正文清洗器与逐文本节点翻译产生。
      body.innerHTML = result.html;
      translated.replaceChildren(body);
      const origin = document.createElement('p');
      origin.className = 'translation-status';
      origin.textContent = 'AI 中文译文';
      translated.prepend(origin);
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
}
</script>"""

from atlas.analytics.tracking import TRACKING_SCRIPT

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


def _shell(*, title: str, heading: str, body: str) -> str:
    return _SHELL.substitute(
        title=escape(title), heading=escape(heading), style=_STYLE, body=body, tracking=TRACKING_SCRIPT
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
    notices: Sequence[str] = (),
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
        notices: 需要显式告诉用户的事实（例如"若干条目由容器回落而来"）。
    """
    page_url = return_to or feed_query_string(result.query, offset=result.offset)
    parts: list[str] = [
        _render_source_tabs(result.query, result.channels),
        _render_filters(result.query, industries),
        _render_quick_switches(result.query),
        _render_pager(result),
    ]
    if notices:
        parts.append(
            '<details class="feed-notices"><summary>'
            f'订阅与浏览提示（{len(notices)}）</summary>'
            + "".join(f'<p>{escape(notice)}</p>' for notice in notices)
            + '</details>'
        )
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
                )
            )
    parts.append(_render_pager(result))
    return _shell(title="Atlas Feed", heading="Atlas Feed", body="\n".join(parts) + _LABEL_SCRIPT + _READER_SCRIPT)


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
        '<details class="filter-panel"><summary>筛选与排序</summary>'
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


def _render_source_tabs(query: FeedQuery, channels: Sequence[str]) -> str:
    def link(label: str, selected_channels: tuple[str, ...]) -> str:
        href = feed_query_string(query, offset=0, channels=selected_channels)
        current = ' aria-current="page"' if query.channels == selected_channels else ''
        return f'<a href="{escape(href)}"{current}>{escape(label)}</a>'

    # 显式查询不存在的渠道时也保留当前选择，避免页面没有选中状态。
    available = sorted(set(channels) | set(query.channels))
    return (
        '<nav class="source-tabs" aria-label="来源">'
        + link("全部来源", ())
        + "".join(link(channel, (channel,)) for channel in available)
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
        + f'<div class="meta"><span>{escape(entry.channel_id)}</span>'
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
        f"渠道 {escape(entry.channel_id)} · 区间 "
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
    native_chinese: bool, body_note: str = "",
) -> str:
    """正文 HTML 仅接受阅读服务的清洗结果；其余字段继续逐项转义。"""
    return_to = article_url(entry, back_to=back_to)
    translation_url = return_to.replace("/article?", "/translate?", 1)
    tabs = ""
    if body_html and not native_chinese:
        tabs = (
            f'<div class="reader-tabs" role="tablist" aria-label="正文语言" data-translation-url="{escape(translation_url)}">'
            '<button type="button" role="tab" id="tab-original" aria-controls="body-original" aria-selected="true" tabindex="0">原文</button>'
            '<button type="button" role="tab" id="tab-chinese" aria-controls="body-chinese" aria-selected="false" tabindex="-1">中文</button></div>'
        )
    original = (
        '<div id="body-original"' + (' role="tabpanel" aria-labelledby="tab-original" tabindex="0"' if tabs else '') + '>'
        '<div class="article-body">' + (body_html or '<p class="empty">正文暂不可用，可通过操作区的原文链接阅读。</p>') + '</div></div>'
    )
    if tabs:
        original += '<div id="body-chinese" role="tabpanel" aria-labelledby="tab-chinese" tabindex="0" lang="zh-CN" hidden></div>'
    body = (
        f'<nav class="pager"><a href="{escape(back_to)}">返回信息流</a></nav>'
        f'<article class="item article-detail" id="item-{escape(entry.raw_id)}" data-tracking-detail {_tracking_identity(entry)}>'
        + _menu_button()
        + f'<h2 class="article-title">{escape(entry.title)}</h2>'
        + (f'<p class="title-zh" lang="zh-CN">{escape(entry.title_zh)}</p>' if entry.title_zh else '')
        + f'<div class="meta"><span>{escape(entry.channel_id)}</span>'
        f'<time datetime="{escape(entry.timestamp.isoformat())}">{escape(entry.timestamp.strftime("%Y-%m-%d %H:%M"))}'
        f'{" · 发布" if entry.published_from_entry else " · 采集（来源未提供发布时间）"}</time></div>'
        + _tag_row(entry, labels)
        + '<section class="article-intro"><h3>摘要 / 简介</h3>'
        f'<p>{escape(entry.summary) if entry.summary else "暂无简介。"}</p>'
        + (f'<span class="summary-origin">{escape(entry.summary_origin)}</span>' if entry.summary_origin and not entry.summary else '')
        + '</section><section class="reader-content"><h3>正文</h3>'
        + (f'<p class="body-note">{escape(body_note)}</p>' if body_note else '')
        + tabs + original + '</section><div class="actions" aria-label="文章打标">'
        + _valid_action(entry, actor, return_to, LABEL_VALUE_VALID, "标记有效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_VALID)
        + _valid_action(entry, actor, return_to, LABEL_VALUE_INVALID, "标记无效", selected=labels.get(LABEL_KEY_VALID) == LABEL_VALUE_INVALID)
        + _source_action(entry.link or entry.endpoint)
        + _tag_editor(entry, labels, industries, actor, return_to)
        + '</div>' + _LABEL_STATUS
        + _sheet_open()
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
    return _shell(title=entry.title + " · Atlas", heading="文章详情", body=body)


def _render_item(
    item: FeedItem,
    labels: Mapping[str, str],
    *,
    industries: Sequence[str],
    actor: str,
    return_to: str,
) -> str:
    status = "" if item.http_status is None else f" · HTTP {item.http_status}"
    return (
        f'<article class="item" id="item-{escape(item.raw_id)}" data-tracking-list-item {_tracking_identity(item)}>'
        '<div class="head">'
        f'<code class="raw-id">{escape(item.raw_id)}</code>'
        "</div>"
        f'<div class="endpoint"><a href="{_safe_href(item.endpoint)}" rel="noreferrer noopener">'
        f"{escape(item.endpoint)}</a></div>"
        f'<div class="meta">{escape(item.channel_id)} · {escape(item.fetched_at.isoformat())} · '
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
