"""Atlas Web UI — T-109 打标前端（服务端渲染，SPEC §2.11）。

- `pages` —— stdlib `string.Template` + `html.escape` 的服务端渲染，**无构建步骤**；
- `app` —— stdlib `ThreadingHTTPServer`：feed 浏览（只读，复用 `atlas.feed` 的查询层）
  + 文档级打标提交（唯一写入口 `atlas.labels.LabelStore`）。

本包只用标准库与已提交的 `atlas.contracts` / `atlas.feed` / `atlas.labels`：
不引入 Web 框架、无 JS 框架、零新增依赖、不自己写 SQL。
"""

from atlas.webui.app import (
    DEFAULT_ACTOR,
    DEFAULT_RETURN_TO,
    MAX_BODY_BYTES,
    WEBUI_CONTRACT_VERSION,
    LabelAccess,
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
    escape,
    feed_query_string,
    render_error_page,
    render_feed_page,
)

__all__ = [
    # 服务
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
    "LabelSession",
    "StoreLabelAccess",
    # 页面
    "LABEL_KEY_INDUSTRY",
    "LABEL_KEY_VALID",
    "LABEL_VALUE_INVALID",
    "LABEL_VALUE_VALID",
    "escape",
    "feed_query_string",
    "render_error_page",
    "render_feed_page",
]
