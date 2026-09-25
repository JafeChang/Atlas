"""T-109 打标前端应用：stdlib `ThreadingHTTPServer` 上的 feed 浏览 + 文档级打标。

SPEC §2.11 的硬边界
-------------------

- 只用标准库：`http.server` / `html` / `urllib.parse` / `json` / `logging` / `threading`；
- **`ThreadingHTTPServer`**（不是单线程 `HTTPServer`），慢请求不阻塞其它请求；
- **不引入任何 Web 框架、无 JS 框架、无构建步骤**；页面由 `atlas.webui.pages` 服务端渲染。

路由
----

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/` `/feed` | 可筛选、可排序、可翻页的 feed；**只读** |
| POST | `/label` | 提交**一个**文档级判断（一条一次点击）；成功后 `303` 回到 feed |
| GET | `/health` | 存活探测（不碰数据源、不碰标签库） |

其它路径 → `404`；方法不匹配 → `405` + `Allow`。

打标为什么一定走唯一写入口
--------------------------

打标写入全部经过 `LabelAccess.record()`，其**唯一实现** `StoreLabelAccess.record()`
只做一件事：`atlas.labels.open_store(db_path).add(ConfirmedLabel.human(...))`。

- 本包**不 import `sqlite3`**、**不写任何 SQL**、**不建表**、不 import 其它域的持久化实现；
  `confirmed_labels` 表、`Confirmed` 只增不改的触发器、内容寻址的 `label_id` 全归 T-108；
- 幂等由存储层裁决：同一 `(raw_id, label_key, label_value, actor)` 得到同一个 `label_id`，
  `add` 走 `ON CONFLICT DO NOTHING`，重复提交同一判断**不会**产生第二条；
- 改判（同维度换值）**不是覆盖**，而是新记录——`latest_value` 取最新（SPEC §2.3 只增不改）。

浏览路径为什么是只读的
----------------------

GET 路径只拿到 `LabelAccess.read_session()` 产出的**只读会话**（只有
`keys_for` / `latest_value` / `values_for`，**没有** `add`），结构上不存在写入口；
`record()` 只在 POST 分支被调用。另外 `StoreLabelAccess.read_session()` 在
**库文件不存在时直接返回空会话，不创建库文件** —— 纯浏览不会在磁盘上留下任何东西。

（打开一个已存在的库会执行 `CREATE TABLE IF NOT EXISTS` + `commit`，这是存储层
T-108 的行为；库已存在时不改变任何字节，`tests/test_webui_app.py` 用库文件 sha256 +
`count()` 前后一致做了行为性验证，并用「`record()` 一被调用就抛错」的探针证明
GET 路径**根本没有尝试过**写入。）

行业为什么没有硬编码
--------------------

行业的唯一来源是注入的 `industry_provider`（生产上接 `atlas.registry` 的
`RegistryService.label_space`）。它同时喂给三个地方，构成 SPEC §2.5 的 C8 闭环：
**筛选维度**（feed 表单）、**打标修正对象**（行业下拉框 + 服务端校验）、**页面展示**。
本模块里没有任何行业名字面量。
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qs, urlsplit

from atlas.contracts import ConfirmedLabel
from atlas.feed import (
    FeedQuery,
    FeedQueryError,
    InvalidQueryError,
    LabelLookup,
    run_query,
)
from atlas.feed.query import SOURCE_PAGE_SIZE
from atlas.labels import LabelStore, open_store

from .pages import (
    LABEL_KEY_INDUSTRY,
    LABEL_KEY_VALID,
    LABEL_VALUE_INVALID,
    LABEL_VALUE_VALID,
    MAX_ACTOR_LENGTH,
    MAX_LABEL_VALUE_LENGTH,
    MAX_RAW_ID_LENGTH,
    render_error_page,
    render_feed_page,
)

__all__ = [
    "DEFAULT_ACTOR",
    "DEFAULT_RETURN_TO",
    "MAX_BODY_BYTES",
    "WEBUI_CONTRACT_VERSION",
    "LabelAccess",
    "LabelSession",
    "StoreLabelAccess",
    "WebUIApplication",
    "WebUIHTTPServer",
    "WebUIRequestHandler",
    "build_application",
    "raw_exists_from_source",
]

#: webui 自身的对外契约版本（T-106 的 feed JSON 契约与本版号互不影响）。
WEBUI_CONTRACT_VERSION = 1

#: 打标人的默认署名；也可由页面上的 `actor` 字段覆盖。
DEFAULT_ACTOR = "me"

#: 表单体上限：打标是一次一条，正常远小于此。
MAX_BODY_BYTES = 64 * 1024

#: 打标成功后默认回到的地址。
DEFAULT_RETURN_TO = "/feed"

#: `/label` 表单允许出现的字段（其余一律拒绝；重复亦拒绝）。
FORM_FIELDS = frozenset({"raw_id", "label_key", "label_value", "actor", "return_to"})

#: 只允许回到 feed（防开放重定向：不接受绝对 URL、不接受任意路径）。
_RETURN_TO_PREFIX = "/feed"

_log = logging.getLogger("atlas.webui.app")

_ALLOWED_METHODS = ("GET", "POST")


# ---------------------------------------------------------------------- #
# 标签访问接缝（读 / 写分离）
# ---------------------------------------------------------------------- #
class LabelSession(Protocol):
    """**只读**标签会话。刻意不含 `add`：GET 路径拿不到写入口。"""

    def keys_for(self, raw_id: str) -> Sequence[str]: ...

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]: ...

    def values_for(self, raw_id: str) -> Dict[str, str]: ...


class LabelAccess(Protocol):
    """打标存储的唯一接缝：读走 `read_session()`，写走 `record()`。"""

    def read_session(self) -> Any: ...

    def record(
        self, *, raw_id: str, label_key: str, label_value: str, actor: str
    ) -> ConfirmedLabel: ...


class _ReadOnlySession:
    """`LabelSession` 的公共实现：`values_for` 由另外两个读方法派生。"""

    def keys_for(self, raw_id: str) -> Sequence[str]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def values_for(self, raw_id: str) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for key in self.keys_for(raw_id):
            value = self.latest_value(raw_id, key)
            if value is not None:
                values[key] = value
        return values

    def __enter__(self) -> "_ReadOnlySession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _EmptySession(_ReadOnlySession):
    """库文件还不存在时的会话：没有标签，且**不创建**任何东西。"""

    def keys_for(self, raw_id: str) -> Sequence[str]:
        return ()

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:
        return None


class _StoreSession(_ReadOnlySession):
    """把打开的 `LabelStore` 包成只读会话；退出时关闭连接。"""

    def __init__(self, store: LabelStore) -> None:
        self._store = store

    def keys_for(self, raw_id: str) -> Sequence[str]:
        return tuple(self._store.keys_for(raw_id))

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:
        return self._store.latest_value(raw_id, label_key)

    def __exit__(self, *exc_info: object) -> None:
        self._store.close()
        return None


class StoreLabelAccess:
    """默认实现：唯一持久化入口是 `atlas.labels` 的 `LabelStore`。

    Args:
        db_path: Confirmed 库文件路径。**必须显式给出**（没有默认值），
            避免任何"不小心写到仓库 `data/`"的路径。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def initialize(self) -> Path:
        """显式的一次性初始化：建库、建表、装触发器（全部归 T-108）。

        应用启动时调用一次。此后运行时对库的每一次打开都只是"打开已存在的库"，
        浏览路径不会引入任何 schema 或数据行。
        """
        with open_store(self._db_path):
            pass
        return self._db_path

    def read_session(self) -> _ReadOnlySession:
        """打开只读会话。

        库文件**不存在**时返回空会话：浏览路径绝不创建库文件、绝不写盘。
        `sqlite3` 连接是线程亲和的，因此每次请求各自开一条，不做跨线程共享。
        """
        if not self._db_path.exists():
            return _EmptySession()
        return _StoreSession(open_store(self._db_path))

    def record(
        self, *, raw_id: str, label_key: str, label_value: str, actor: str
    ) -> ConfirmedLabel:
        """写入一条人工标签 —— 本包的**唯一**写入路径。

        `ConfirmedLabel.human()` 是 1A 文档级的人工直判（不要求 AI 证据）；
        `LabelStore.add()` 负责内容寻址的 `label_id`、`ON CONFLICT DO NOTHING`
        的幂等、以及触发器保护的只增不改。
        """
        label = ConfirmedLabel.human(
            raw_id=raw_id, label_key=label_key, label_value=label_value, actor=actor
        )
        with open_store(self._db_path) as store:
            return store.add(label)


# ---------------------------------------------------------------------- #
# 「这条 raw 存在吗」（打标必须锚在真实文档上）
# ---------------------------------------------------------------------- #
def raw_exists_from_source(source: Any) -> Callable[[str], bool]:
    """从已注入的 `FeedSource` 派生"文档存在"判定（**只读**）。

    优先用 `by_id`（`atlas.feed.StaticFeedSource` 提供）；没有就按 `FeedSource`
    契约的 `list_raw(limit, offset)` 分页扫一遍 —— 不做筛选、不做排序、不自己写存储查询。
    """
    by_id = getattr(source, "by_id", None)
    if callable(by_id):
        return lambda raw_id: by_id(raw_id) is not None

    def paged(raw_id: str) -> bool:
        offset = 0
        while True:
            page = list(source.list_raw(SOURCE_PAGE_SIZE, offset))
            if any(record.raw_id == raw_id for record in page):
                return True
            if len(page) < SOURCE_PAGE_SIZE:
                return False
            offset += len(page)

    return paged


# ---------------------------------------------------------------------- #
# HTTP
# ---------------------------------------------------------------------- #
class _FormRejected(Exception):
    """表单层面的拒绝：带上要回给用户的状态码与可读理由。"""

    def __init__(self, status: HTTPStatus, title: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.title = title
        self.message = message
        #: 请求体没被完整读走时必须关连接，否则残留字节会被当成下一个请求。
        self.close = False


def _normalize_path(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/") or "/"
    return path


def _single(params: Mapping[str, Sequence[str]], name: str) -> Optional[str]:
    """取表单里的单值字段：缺省返回 `None`（由调用方决定是否算"空参数"）。"""
    values = params.get(name)
    if not values:
        return None
    return values[0]


def _memoized_lookup(session: LabelSession) -> LabelLookup:
    """把会话的 `keys_for` 包成 `run_query` 需要的 `LabelLookup`（每请求一次缓存）。"""
    cache: Dict[str, Sequence[str]] = {}

    def lookup(raw_id: str) -> Sequence[str]:
        if raw_id not in cache:
            cache[raw_id] = tuple(session.keys_for(raw_id))
        return cache[raw_id]

    return lookup


class WebUIRequestHandler(BaseHTTPRequestHandler):
    """GET 浏览 / POST 打标。业务逻辑复用 `atlas.feed` 与 `atlas.labels`，这里只做 HTTP 翻译。"""

    server_version = "AtlasWebUI/" + str(WEBUI_CONTRACT_VERSION)
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- 日志：走 logging，不往 stderr 乱喷 ---------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _log.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        _log.warning("%s - %s", self.address_string(), format % args)

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        target = urlsplit(self.path)
        path = _normalize_path(target.path)
        if path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "atlas-webui",
                    "contract_version": WEBUI_CONTRACT_VERSION,
                },
            )
            return
        if path in ("/", "/feed"):
            self._render_feed(target.query)
            return
        if path == "/label":
            self._reject_method(allow="POST", detail="打标是写操作，只接受 POST")
            return
        self._not_found(path)

    def do_POST(self) -> None:  # noqa: N802
        path = _normalize_path(urlsplit(self.path).path)
        if path == "/label":
            self._submit_label()
            return
        if path in ("/", "/feed"):
            self._reject_method(allow="GET", detail="feed 是只读投影，只接受 GET")
            return
        self._not_found(path)

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_method(allow=", ".join(_ALLOWED_METHODS), detail="不支持该方法")

    do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_PUT

    def _not_found(self, path: str) -> None:
        self._send_error(
            HTTPStatus.NOT_FOUND,
            "路径不存在",
            f"未知路径：{path}；可用：/feed（GET）、/label（POST）、/health（GET）",
        )

    def _reject_method(self, *, allow: str, detail: str) -> None:
        self._send_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "方法不被允许",
            f"{detail}（本路径允许：{allow}）",
            extra_headers={"Allow": allow},
        )

    # -- GET /feed ----------------------------------------------------------
    def _render_feed(self, query_string: str) -> None:
        server = self.server
        try:
            query = FeedQuery.from_params(parse_qs(query_string, keep_blank_values=True))
        except InvalidQueryError as exc:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "查询参数非法",
                f"参数 {exc.parameter!r}：{exc.message}",
            )
            return

        try:
            with server.labels.read_session() as session:
                result = run_query(
                    server.resolve_source(), query, label_lookup=_memoized_lookup(session)
                )
                values = {
                    item.raw_id: session.values_for(item.raw_id) for item in result.items
                }
        except FeedQueryError as exc:
            _log.error("feed 查询失败：%s", exc)
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "feed 不可用", str(exc)
            )
            return
        except Exception as exc:  # noqa: BLE001 - 兜底但不吞：记录堆栈后如实报 500
            _log.exception("feed 页面渲染内部错误")
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "内部错误",
                f"{type(exc).__name__}: {exc}",
            )
            return

        document = render_feed_page(
            result,
            industries=tuple(server.industry_provider()),
            values=values,
            actor=server.actor,
        )
        self._send_html(HTTPStatus.OK, document)

    # -- POST /label --------------------------------------------------------
    def _submit_label(self) -> None:
        server = self.server
        try:
            fields = self._parse_form()
        except _FormRejected as exc:
            # 请求体可能没被完整读走：关连接，避免残留字节被当成下一个请求。
            self._send_error(exc.status, exc.title, exc.message, close=True)
            return

        try:
            raw_id = _require_text(fields, "raw_id", max_length=MAX_RAW_ID_LENGTH)
            label_key = _require_text(fields, "label_key", max_length=MAX_LABEL_VALUE_LENGTH)
            label_value = _require_text(
                fields, "label_value", max_length=MAX_LABEL_VALUE_LENGTH
            )
            actor = _require_text(fields, "actor", max_length=MAX_ACTOR_LENGTH)
            return_to = _optional_text(fields, "return_to") or DEFAULT_RETURN_TO
            _validate_label(label_key, label_value, server.industry_provider())
            _validate_return_to(return_to)
        except _FormRejected as exc:
            self._send_error(exc.status, exc.title, exc.message)
            return

        if not server.raw_exists(raw_id):
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "文档不存在",
                f"raw_id {raw_id!r} 不在归档里；打标必须锚在真实文档上（SPEC §2.1）",
            )
            return

        try:
            label = server.labels.record(
                raw_id=raw_id, label_key=label_key, label_value=label_value, actor=actor
            )
        except Exception as exc:  # noqa: BLE001 - 兜底但不吞：写明真实原因
            _log.exception("打标写入失败")
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "打标写入失败",
                f"{type(exc).__name__}: {exc}",
            )
            return

        _log.info(
            "打标：raw_id=%s %s=%s actor=%s label_id=%s",
            label.raw_id,
            label.label_key,
            label.label_value,
            label.actor,
            label.label_id,
        )
        self._redirect(HTTPStatus.SEE_OTHER, return_to)

    def _parse_form(self) -> Dict[str, str]:
        """读并校验表单体：类型 / 长度 / 字段白名单 / 单值。"""
        media = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media != "application/x-www-form-urlencoded":
            raise _FormRejected(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "请求体类型不支持",
                "只接受 application/x-www-form-urlencoded，收到 "
                f"{self.headers.get('Content-Type')!r}",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _FormRejected(
                HTTPStatus.LENGTH_REQUIRED, "缺少 Content-Length", "打标表单必须带长度"
            )
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST, "Content-Length 非法", f"收到 {raw_length!r}"
            ) from exc
        if length <= 0:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST, "请求体为空", "打标表单不得为空，必须带 raw_id 等字段"
            )
        if length > MAX_BODY_BYTES:
            raise _FormRejected(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "请求体过大",
                f"上限 {MAX_BODY_BYTES} 字节，收到 {length}",
            )
        body = self.rfile.read(length)
        if len(body) != length:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST, "请求体不完整", f"声明 {length} 字节，实收 {len(body)}"
            )
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST, "请求体不是 UTF-8", str(exc)
            ) from exc

        params = parse_qs(text, keep_blank_values=True)
        unknown = sorted(set(params) - FORM_FIELDS)
        if unknown:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "未知表单字段",
                f"收到 {unknown}；允许：{sorted(FORM_FIELDS)}",
            )
        duplicated = sorted(name for name, values in params.items() if len(values) > 1)
        if duplicated:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "字段重复提交",
                f"{duplicated} 只允许出现一次（重复即语义不明）",
            )
        return {name: values[0] for name, values in params.items()}

    # -- 输出 ---------------------------------------------------------------
    def _send_html(
        self,
        status: HTTPStatus,
        document: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
        close: bool = False,
    ) -> None:
        self._send_bytes(
            status,
            document.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            extra_headers=extra_headers,
            close=close,
        )

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    def _send_error(
        self,
        status: HTTPStatus,
        title: str,
        message: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
        close: bool = False,
    ) -> None:
        if self.command == "HEAD":
            self._send_bytes(
                status,
                b"",
                content_type="text/html; charset=utf-8",
                extra_headers=extra_headers,
                close=close,
            )
            return
        self._send_html(
            status,
            render_error_page(int(status), title, message),
            extra_headers=extra_headers,
            close=close,
        )

    def _redirect(self, status: HTTPStatus, location: str) -> None:
        self._send_bytes(
            status,
            b"",
            content_type="text/html; charset=utf-8",
            extra_headers={"Location": location},
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        extra_headers: Mapping[str, str] | None = None,
        close: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)


def _require_text(fields: Mapping[str, str], name: str, *, max_length: int) -> str:
    """必填文本字段：缺失、空白、超长一律**响亮拒绝**。"""
    if name not in fields:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "表单字段缺失", f"缺少必填字段 {name!r}"
        )
    value = fields[name].strip()
    if not value:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "表单字段为空", f"字段 {name!r} 不得为空"
        )
    if len(value) > max_length:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST,
            "表单字段过长",
            f"字段 {name!r} 上限 {max_length} 字符，收到 {len(value)}",
        )
    return value


def _optional_text(fields: Mapping[str, str], name: str) -> Optional[str]:
    value = fields.get(name)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _validate_label(label_key: str, label_value: str, industries: Sequence[str]) -> None:
    """标签值校验。行业取值集合**只来自配置**，代码里没有行业名。"""
    if label_key == LABEL_KEY_VALID:
        allowed = (LABEL_VALUE_VALID, LABEL_VALUE_INVALID)
        if label_value not in allowed:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "标签值非法",
                f"{LABEL_KEY_VALID} 只接受 {' / '.join(allowed)}，收到 {label_value!r}",
            )
        return
    if label_key == LABEL_KEY_INDUSTRY:
        allowed = tuple(industries)
        if not allowed:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "行业配置为空",
                "当前配置里没有任何行业，无法修正行业（行业来自 atlas.registry 的配置）",
            )
        if label_value not in allowed:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "标签值非法",
                f"行业 {label_value!r} 不在当前配置的行业列表里：{list(allowed)}",
            )
        return
    raise _FormRejected(
        HTTPStatus.BAD_REQUEST,
        "标签维度非法",
        f"未知 label_key {label_key!r}；允许：{LABEL_KEY_VALID} / {LABEL_KEY_INDUSTRY}",
    )


def _validate_return_to(return_to: str) -> None:
    """只允许回到 feed（防开放重定向），且查询参数必须是合法的 feed 查询。"""
    if not (return_to == _RETURN_TO_PREFIX or return_to.startswith(_RETURN_TO_PREFIX + "?")):
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST,
            "return_to 非法",
            f"只允许 {_RETURN_TO_PREFIX} 开头的站内地址，收到 {return_to!r}",
        )
    query_string = return_to.split("?", 1)[1] if "?" in return_to else ""
    try:
        FeedQuery.from_params(parse_qs(query_string, keep_blank_values=True))
    except InvalidQueryError as exc:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST,
            "return_to 非法",
            f"参数 {exc.parameter!r}：{exc.message}",
        ) from exc


# ---------------------------------------------------------------------- #
# 服务
# ---------------------------------------------------------------------- #
class WebUIHTTPServer(ThreadingHTTPServer):
    """带 feed 数据源、标签访问、行业配置与「文档存在」判定的 `ThreadingHTTPServer`。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        source: Any,
        labels: LabelAccess,
        industry_provider: Callable[[], Sequence[str]],
        raw_exists: Callable[[str], bool],
        actor: str,
    ) -> None:
        self._source = source
        self.labels = labels
        self.industry_provider = industry_provider
        self.raw_exists = raw_exists
        self.actor = actor
        super().__init__(server_address, WebUIRequestHandler)

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


class WebUIApplication:
    """在后台线程里跑 `WebUIHTTPServer` 的小包装（测试与本地运行共用）。

    ``with WebUIApplication(source, labels=..., industry_provider=...) as app:``
    → ``app.base_url`` 形如 ``http://127.0.0.1:<port>``。
    """

    def __init__(
        self,
        source: Any,
        *,
        labels: LabelAccess,
        industry_provider: Callable[[], Sequence[str]],
        raw_exists: Optional[Callable[[str], bool]] = None,
        actor: str = DEFAULT_ACTOR,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        resolved_exists = raw_exists if raw_exists is not None else _derive_raw_exists(source)
        self._httpd = WebUIHTTPServer(
            (host, port),
            source=source,
            labels=labels,
            industry_provider=industry_provider,
            raw_exists=resolved_exists,
            actor=actor,
        )
        self._thread = threading.Thread(
            target=self._serve, name="atlas-webui-http", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        # 轮询间隔调小：`shutdown()` 最多等一个 poll_interval。
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
    def server(self) -> WebUIHTTPServer:
        return self._httpd

    @property
    def labels(self) -> LabelAccess:
        return self._httpd.labels

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "WebUIApplication":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"WebUIApplication(base_url={self.base_url!r})"


def _derive_raw_exists(source: Any) -> Callable[[str], bool]:
    """`raw_exists` 缺省时从数据源派生：工厂则每次新开（`sqlite3` 线程亲和）。"""
    if hasattr(source, "list_raw") or hasattr(source, "by_id"):
        return raw_exists_from_source(source)
    if callable(source):
        return lambda raw_id: raw_exists_from_source(source())(raw_id)
    raise TypeError(
        "source 必须是 FeedSource 或返回 FeedSource 的零参工厂，"
        f"收到 {type(source).__name__}"
    )


def build_application(
    source: Any,
    *,
    db_path: str | Path,
    industry_provider: Callable[[], Sequence[str]],
    raw_exists: Optional[Callable[[str], bool]] = None,
    actor: str = DEFAULT_ACTOR,
    host: str = "127.0.0.1",
    port: int = 0,
) -> WebUIApplication:
    """便捷装配：`StoreLabelAccess` + `WebUIApplication`（已启动）。

    `db_path` **必填**：本包不提供任何指向仓库 `data/` 的默认值。
    启动时显式初始化一次标签库（建表 + 触发器，归 T-108），此后浏览路径只读。
    """
    labels = StoreLabelAccess(db_path)
    labels.initialize()
    return WebUIApplication(
        source,
        labels=labels,
        industry_provider=industry_provider,
        raw_exists=raw_exists,
        actor=actor,
        host=host,
        port=port,
    )
