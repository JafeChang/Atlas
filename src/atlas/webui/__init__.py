"""Atlas Web UI — T-109 打标前端（服务端渲染，SPEC §2.11）。

- `pages` —— stdlib `string.Template` + `html.escape` 的服务端渲染，**无构建步骤**；
- `app` —— stdlib `ThreadingHTTPServer`：**按条目**浏览 feed（只读，复用 `atlas.feed`
  的查询层）+ `GET /entry`（打开原文并高亮条目区间）+ 打标提交（唯一写入口
  `atlas.labels.LabelStore`）。

本包只用标准库与已提交的 `atlas.contracts` / `atlas.feed` / `atlas.labels`：
不引入 Web 框架、无 JS 框架、零新增依赖、不自己写 SQL。

**条目派生层不在这里**：条目化归 T-130（`atlas.entries`），本包的 import 白名单
不允许它进来，因此 `entries_lookup` / `locate_entry` / `text_length` 都是**注入**的。
真实装配见 `atlas.webapp`（组合根 + 命令行入口 `python -m atlas.webapp`）。
"""

from atlas.webui.app import (
    ANCHOR_FIELDS,
    DEFAULT_ACTOR,
    DEFAULT_RETURN_TO,
    MAX_ANCHOR_CHAR,
    MAX_BODY_BYTES,
    WEBUI_CONTRACT_VERSION,
    LabelAccess,
    LabelRecordConflict,
    LabelSession,
    StoreLabelAccess,
    WebUIApplication,
    WebUIHTTPServer,
    WebUIRequestHandler,
    build_application,
    raw_exists_from_source,
)
from atlas.webui.pages import (
    LABEL_KEY_INDUSTRY,
    LABEL_KEY_VALID,
    LABEL_VALUE_INVALID,
    LABEL_VALUE_VALID,
    entry_anchor_url,
    escape,
    feed_query_string,
    render_entry_page,
    render_error_page,
    render_feed_page,
)

__all__ = [
    # 服务
    "ANCHOR_FIELDS",
    "MAX_ANCHOR_CHAR",
    "MAX_BODY_BYTES",
    "WEBUI_CONTRACT_VERSION",
    "DEFAULT_ACTOR",
    "DEFAULT_RETURN_TO",
    "WebUIApplication",
    "WebUIHTTPServer",
    "WebUIRequestHandler",
    "build_application",
    "raw_exists_from_source",
    # 标签访问接缝
    "LabelAccess",
    "LabelRecordConflict",
    "LabelSession",
    "StoreLabelAccess",
    # 页面
    "LABEL_KEY_INDUSTRY",
    "LABEL_KEY_VALID",
    "LABEL_VALUE_INVALID",
    "LABEL_VALUE_VALID",
    "entry_anchor_url",
    "escape",
    "feed_query_string",
    "render_entry_page",
    "render_error_page",
    "render_feed_page",
]
