"""T-106 Feed HTTP 服务（SPEC §2.11：stdlib `http.server.ThreadingHTTPServer` + `json`）。

**零 Web 框架、零构建步骤、零新依赖。** 对外是**本模块定义的 JSON 契约**，
将来若换成 FastAPI，只允许换实现，**不得改动这里的 JSON 形状**（SPEC §2.11）。

路由
----

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/feed` | 筛选 / 排序 / 分页查询（唯一的数据出口，只读） |
| GET | `/health` | 存活探测（不触碰数据源） |

其它路径 → `404 not_found`；非 GET → `405 method_not_allowed` + `Allow: GET`。

成功响应（`200`）::

    {
      "contract_version": 1,
      "items": [
        {
          "raw_id": "...", "channel_id": "...", "industry": "ai",
          "endpoint": "https://...", "content_sha256": "<64 hex>",
          "byte_length": 1234, "fetched_at": "2026-01-01T00:00:00+00:00",
          "http_status": 200, "labels": ["valid"]
        }
      ],
      "page": {"limit": 50, "offset": 0, "total": 3, "returned": 3,
               "has_more": false, "next_offset": null},
      "sort": {"column": "fetched_at", "order": "desc", "tie_break": "raw_id asc"},
      "filters": {"industries": [], "channels": [], "since": null, "until": null,
                  "labels": [], "labeled": null}
    }

错误响应（`400` / `404` / `405` / `500`）::

    {"contract_version": 1,
     "error": {"code": "invalid_query", "parameter": "limit", "message": "..."}}

错误码：`invalid_query`（参数非法，**拒绝而不是静默忽略**）、`not_found`、
`method_not_allowed`、`feed_unavailable`（服务端未接线 / 数据源违约）、`internal_error`。

线程与只读
----------

- 用 `ThreadingHTTPServer`（SPEC §2.11 硬性要求），慢请求不阻塞其它请求；
- `source` 可以是 `FeedSource`，也可以是**零参工厂**（`sqlite3` 连接线程亲和，
  归档实现请传工厂，见 `atlas.feed.repository.ArchiveFeedSource` 的说明）；
- 本模块只调用 `list_raw` / `industry_of` / 注入的 `label_lookup`，没有任何写入路径。
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping
from urllib.parse import parse_qs, urlsplit

from .query import (
    FeedQuery,
    FeedQueryError,
    FeedResult,
    InvalidQueryError,
    LabelLookup,
    run_query,
)

__all__ = [
    "CONTRACT_VERSION",
    "ERROR_CODES",
    "FeedServer",
    "FeedRequestHandler",
    "error_payload",
    "feed_payload",
    "start_feed_server",
]

#: 对外 JSON 契约版本。**改形状必须升版本**（SPEC §2.11：换框架不得改契约）。
CONTRACT_VERSION = 1

ERROR_CODES = {
    HTTPStatus.BAD_REQUEST: "invalid_query",
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
    HTTPStatus.INTERNAL_SERVER_ERROR: "internal_error",
}

_log = logging.getLogger("atlas.feed.http")

#: 只有数据出口，没有数据入口。
_ALLOWED_METHODS = ("GET",)


# ---------------------------------------------------------------------- #
# JSON 契约
# ---------------------------------------------------------------------- #
def feed_payload(result: FeedResult) -> Dict[str, Any]:
    """把查询结果渲染成对外 JSON（**契约的唯一出处**）。"""
    return {
        "contract_version": CONTRACT_VERSION,
        "items": [_item_payload(item) for item in result.items],
        "page": {
            "limit": result.limit,
            "offset": result.offset,
            "total": result.total,
            "returned": len(result.items),
            "has_more": result.has_more,
            "next_offset": result.next_offset,
        },
        "sort": result.query.sort_description(),
        "filters": result.query.filters_description(),
    }


def _item_payload(item: Any) -> Dict[str, Any]:
    return {
        "raw_id": item.raw_id,
        "channel_id": item.channel_id,
        "industry": item.industry,
        "endpoint": item.endpoint,
        "content_sha256": item.content_sha256,
        "byte_length": item.byte_length,
        "fetched_at": item.fetched_at.isoformat(),
        "http_status": item.http_status,
        "labels": list(item.labels),
    }


def error_payload(
    code: str, message: str, *, parameter: str | None = None
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if parameter is not None:
        error["parameter"] = parameter
    return {"contract_version": CONTRACT_VERSION, "error": error}


# ---------------------------------------------------------------------- #
# 处理
# ---------------------------------------------------------------------- #
class FeedRequestHandler(BaseHTTPRequestHandler):
    """只读 GET 处理器。所有业务逻辑都在 `query.py`，这里只做 HTTP 翻译。"""

    server_version = "AtlasFeed/" + str(CONTRACT_VERSION)
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- 日志：走 logging，不往 stderr 乱喷 ---------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _log.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        _log.warning("%s - %s", self.address_string(), format % args)

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self) -> None:
        target = urlsplit(self.path)
        path = target.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"

        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "contract_version": CONTRACT_VERSION})
            return
        if path != "/feed":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                error_payload("not_found", f"未知路径：{path}（只有 /feed 与 /health）"),
            )
            return

        try:
            query = FeedQuery.from_params(parse_qs(target.query, keep_blank_values=True))
        except InvalidQueryError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                error_payload("invalid_query", exc.message, parameter=exc.parameter),
            )
            return

        label_lookup: LabelLookup | None = getattr(self.server, "label_lookup", None)
        source: Any = None
        try:
            source = self.server.resolve_source()
            result = run_query(source, query, label_lookup=label_lookup)
        except InvalidQueryError as exc:  # 参数问题（例如区间矛盾）
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                error_payload("invalid_query", exc.message, parameter=exc.parameter),
            )
            return
        except FeedQueryError as exc:
            # 未注入 label_lookup / 数据源违约：服务端未接线，响亮失败（不是 200 空结果）
            _log.error("feed 查询失败：%s", exc)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                error_payload("feed_unavailable", str(exc)),
            )
            return
        except Exception as exc:  # noqa: BLE001 - 兜底但不能吞：记录堆栈后如实报 500
            _log.exception("feed 查询内部错误")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                error_payload("internal_error", f"{type(exc).__name__}: {exc}"),
            )
            return

        self._send_json(HTTPStatus.OK, feed_payload(result))

    def _reject_method(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            error_payload(
                "method_not_allowed",
                f"{self.command} 不被支持：feed 是只读投影，只允许 {', '.join(_ALLOWED_METHODS)}",
            ),
            extra_headers={"Allow": ", ".join(_ALLOWED_METHODS)},
            close=True,
        )

    do_POST = _reject_method
    do_PUT = _reject_method
    do_PATCH = _reject_method
    do_DELETE = _reject_method
    do_HEAD = _reject_method
    do_OPTIONS = _reject_method

    # -- 输出 ---------------------------------------------------------------
    def _send_json(
        self,
        status: HTTPStatus,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
        close: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class FeedHTTPServer(ThreadingHTTPServer):
    """带上数据源解析与标签查询的 `ThreadingHTTPServer`。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        source: Any,
        label_lookup: LabelLookup | None = None,
    ) -> None:
        self._source = source
        self.label_lookup = label_lookup
        super().__init__(server_address, FeedRequestHandler)

    def resolve_source(self) -> Any:
        """`source` 可以是 `FeedSource`，也可以是零参工厂（每请求新建，线程安全）。"""
        if hasattr(self._source, "list_raw"):
            return self._source
        if callable(self._source):
            return self._source()
        raise TypeError(
            "source 必须是 FeedSource（有 list_raw）或返回 FeedSource 的零参工厂，"
            f"收到 {type(self._source).__name__}"
        )


class FeedServer:
    """在后台线程里跑 `FeedHTTPServer` 的小包装（测试与本地运行共用）。

    ``with FeedServer(source) as server: server.base_url`` → ``http://127.0.0.1:<port>``

    `port=0` 表示由内核分配临时端口（测试默认）；`close()` 会 `shutdown()` +
    `server_close()` 并 join 线程，测试结束必须调用（或用 `with`）。
    """

    def __init__(
        self,
        source: Any,
        *,
        label_lookup: LabelLookup | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._httpd = FeedHTTPServer((host, port), source=source, label_lookup=label_lookup)
        self._thread = threading.Thread(
            target=self._serve,
            name="atlas-feed-http",
            daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        # 轮询间隔调小：`shutdown()` 最多等一个 poll_interval，默认 0.5s 会让关服务很慢。
        self._httpd.serve_forever(poll_interval=0.05)

    @property
    def host(self) -> str:
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def server(self) -> FeedHTTPServer:
        return self._httpd

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FeedServer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"FeedServer(base_url={self.base_url!r})"


def start_feed_server(
    source: Any,
    *,
    label_lookup: LabelLookup | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> FeedServer:
    """便捷入口（语义同 `FeedServer(...)`，已启动）。"""
    return FeedServer(source, label_lookup=label_lookup, host=host, port=port)
