"""T-109 打标前端应用：stdlib `ThreadingHTTPServer` 上的**条目浏览** + 打标。

SPEC §6.5 的结论（T-109 的补完）
-------------------------------

T-109 的验收是"**feed 浏览** + 一条一次点击打标"。此前前端只显示 `raw_id` /
渠道 / 字节数，**连"这是什么"都看不出来**——那不叫浏览。T-130 落地之后可以按
**条目**渲染了，本模块因此从"文档粒度"切到"条目粒度"：

- `GET /feed` 走 `granularity="entry"`（容器 feed **本身不出现**，它的派生条目出现；
  本身即条目的 Raw 整篇作为一条出现，见 SPEC §6.3 裁决 B）；
- `GET /entry` 打开原文并**高亮该条目区间**；
- `POST /label` 提交 `(raw_id, label_key, label_value, actor, anchor)`，
  anchor = 该条目的字符区间（**绝不是 `entry_id`**，T-130 已用测试钉死）。

**条目派生层是注入的**：条目化归 T-130（`atlas.entries`），而本包的 import 白名单
只允许 `atlas` 根（`tests/test_webui_app.py::test_webui_uses_only_stdlib_and_the_atlas_package`）。
因此 `/feed` 的条目由 `entries_lookup` 注入；**未注入时 `/feed` 响亮失败（500）**，
绝不退回"把容器当条目"的旧行为。`/entry` 在注入 `locate_entry` 时用它定位真实条目，
否则按"整篇就是一个条目"渲染（此时不高亮，页面会明说）。真实装配见 `atlas.webapp`。

SPEC §2.11 的硬边界
-------------------

- 只用标准库：`http.server` / `html` / `urllib.parse` / `json` / `logging` / `threading`；
- **`ThreadingHTTPServer`**（不是单线程 `HTTPServer`），慢请求不阻塞其它请求；
- **不引入任何 Web 框架、无 JS 框架、无构建步骤**；页面由 `atlas.webui.pages` 服务端渲染。

路由
----

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/` `/feed` | 可筛选、可排序、可翻页的 feed（**条目**粒度）；只读 |
| GET | `/entry` | 原文 + 高亮该条目区间（`raw_id` / `char_start` / `char_end`）；只读 |
| POST | `/label` | 提交**一个**判断（一条一次点击）；成功后 `303` 回到 feed |
| GET | `/health` | 存活探测（不碰数据源、不碰标签库） |

其它路径 → `404`；方法不匹配 → `405` + `Allow`。

打标为什么一定走唯一写入口
--------------------------

打标写入全部经过 `LabelAccess.record()`，其**唯一实现** `StoreLabelAccess.record()`
只做一件事：`atlas.labels.open_store(db_path).add(label)`，其中 `label` 由
`ConfirmedLabel.human(...)` 构造（带不带 `anchor` 决定文档级 / 条目级）。

- 本包**不 import `sqlite3`**、**不写任何 SQL**、**不建表**、不 import 其它域的持久化实现；
  `confirmed_labels` 表、`Confirmed` 只增不改的触发器、内容寻址的 `label_id` 全归 T-108；
- 幂等由存储层裁决：同一 `(raw_id, label_key, label_value, actor)` 得到同一个 `label_id`，
  `add` 走 `ON CONFLICT DO NOTHING`，重复提交同一判断**不会**产生第二条；
- 改判（同维度换值）**不是覆盖**，而是新记录——`latest_value` 取最新（SPEC §2.3 只增不改）。

**`label_id` 与锚点的关系（必须知道的一条）**：`label_id` 的口径是
`(raw_id, label_key, label_value, actor)`，**不含锚点**（见 `atlas.contracts.ids`）。
因此"同一文档 + 同一人 + 同维度 + 同取值 + 不同区间"会撞同一个 `label_id`，
存储层按幂等处理、**不会**写入第二条。本模块在写之前**主动检测**这种撞击：
若库里已有的那条锚点与本次不同，**响亮拒绝**（`409`）并指出已有的区间——
静默丢弃一条人工判断是 SPEC §7.3 记的"静默失效"，不能接受。

浏览路径为什么是只读的
----------------------

GET 路径只拿到 `LabelAccess.read_session()` 产出的**只读会话**（只有
`keys_for` / `latest_value` / `values_for` / `rates_for`，**没有** `add`），
结构上不存在写入口；`record()` 只在 POST 分支被调用。另外
`StoreLabelAccess.read_session()` 在**库文件不存在时直接返回空会话，不创建库文件**
—— 纯浏览不会在磁盘上留下任何东西。

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
from atlas.contracts.anchors import EvidenceAnchor
from atlas.feed import (
    ENTRY_GRANULARITY,
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
    render_entry_page,
    render_error_page,
    render_feed_page,
)

__all__ = [
    "ANCHOR_FIELDS",
    "DEFAULT_ACTOR",
    "DEFAULT_RETURN_TO",
    "ENTRY_PARAMS",
    "MAX_ANCHOR_CHAR",
    "MAX_BODY_BYTES",
    "WEBUI_CONTRACT_VERSION",
    "LabelAccess",
    "LabelRecordConflict",
    "LabelSession",
    "StoreLabelAccess",
    "WebUIApplication",
    "WebUIHTTPServer",
    "WebUIRequestHandler",
    "build_application",
    "raw_exists_from_source",
]

#: webui 自身的对外契约版本（T-106 的 feed JSON 契约与本版号互不影响）。
#:
#: 版本历史：
#:
#: - **1**：文档粒度浏览 + 文档级打标。
#: - **2**（T-109 补完）：`/feed` 改为**条目粒度**、新增 `GET /entry`（高亮条目区间）、
#:   `POST /label` 新增**可选**的锚点字段（`raw_sha256` / `char_start` / `char_end`）。
#:   不带锚点的提交仍然等价于文档级打标 —— 既有调用方不受影响。
WEBUI_CONTRACT_VERSION = 2

#: 打标人的默认署名；也可由页面上的 `actor` 字段覆盖。
DEFAULT_ACTOR = "me"

#: 表单体上限：打标是一次一条，正常远小于此。
MAX_BODY_BYTES = 64 * 1024

#: 锚点字符偏移的防御性上界（真正是否落在原文内由服务端按原文长度判定）。
MAX_ANCHOR_CHAR = 10**9

#: 打标成功后默认回到的地址。
DEFAULT_RETURN_TO = "/feed"

#: `/label` 表单允许出现的字段（其余一律拒绝；重复亦拒绝）。
#: 三个锚点字段**可选**：全给 ⇒ 条目级；全不给 ⇒ 文档级；给一半 ⇒ 拒绝。
FORM_FIELDS = frozenset(
    {
        "raw_id",
        "label_key",
        "label_value",
        "actor",
        "return_to",
        "raw_sha256",
        "char_start",
        "char_end",
    }
)

#: 锚点字段名（要么全给、要么全不给）。
ANCHOR_FIELDS = ("raw_sha256", "char_start", "char_end")

#: `/entry` 的查询参数（只有 id 与整数，没有可用来推算坐标的输入）。
ENTRY_PARAMS = frozenset({"raw_id", "char_start", "char_end", "entry_id"})

#: 只允许回到 feed（防开放重定向：不接受绝对 URL、不接受任意路径）。
_RETURN_TO_PREFIX = "/feed"

_log = logging.getLogger("atlas.webui.app")

_ALLOWED_METHODS = ("GET", "POST")


# ---------------------------------------------------------------------- #
# 标签访问接缝（读 / 写分离）
# ---------------------------------------------------------------------- #
class LabelRecordConflict(Exception):
    """写入会与库里已有的一条标签**冲突**（同 `label_id` 但锚点不同）。

    为什么必须响亮失败：`label_id = f(raw_id, label_key, label_value, actor)` 不含锚点，
    所以"同一个人对同一份原文的同一个维度给出同一个取值、但锚在不同区间"会撞 id。
    存储层按幂等处理（`ON CONFLICT DO NOTHING`），**静默丢弃**后写入的那条人工判断——
    那是 SPEC §7.3 的"静默失效"。本异常把它变成可见的 `409`。
    """

    def __init__(self, message: str, *, existing: ConfirmedLabel) -> None:
        super().__init__(message)
        self.existing = existing


class _WholeDocumentLocation:
    """没有条目层时的 `/entry` 定位结果（与 `atlas.webapp.LocatedEntry` 同形）。

    刻意用普通类而不是 `dataclass`：`atlas.webui` 的 import 白名单里没有 `dataclasses`
    （`tests/test_webui_app.py::test_webui_uses_only_stdlib_and_the_atlas_package`），
    而为一个四字段的结果对象去改那条边界不值得。
    """

    __slots__ = ("entry", "content", "sliced", "notice")

    def __init__(
        self, entry: Any, content: str, sliced: bool, notice: str = ""
    ) -> None:
        self.entry = entry
        self.content = content
        self.sliced = sliced
        self.notice = notice


class LabelSession(Protocol):
    """**只读**标签会话。刻意不含 `add`：GET 路径拿不到写入口。"""

    def keys_for(self, raw_id: str) -> Sequence[str]: ...

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]: ...

    def values_for(self, raw_id: str) -> Dict[str, str]: ...

    def rates_for(self, raw_id: str) -> list[tuple[ConfirmedLabel, str]]: ...


class LabelAccess(Protocol):
    """打标存储的唯一接缝：读走 `read_session()`，写走 `record()`。"""

    def read_session(self) -> Any: ...

    def record(
        self,
        *,
        raw_id: str,
        label_key: str,
        label_value: str,
        actor: str,
        anchor: Optional[EvidenceAnchor] = None,
    ) -> ConfirmedLabel: ...


class _ReadOnlySession:
    """`LabelSession` 的公共实现：`values_for` 由另外两个读方法派生。"""

    def keys_for(self, raw_id: str) -> Sequence[str]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def rates_for(self, raw_id: str) -> list[tuple[ConfirmedLabel, str]]:  # pragma: no cover
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

    def rates_for(self, raw_id: str) -> list[tuple[ConfirmedLabel, str]]:
        return []


class _StoreSession(_ReadOnlySession):
    """把打开的 `LabelStore` 包成只读会话；退出时关闭连接。"""

    def __init__(self, store: LabelStore) -> None:
        self._store = store

    def keys_for(self, raw_id: str) -> Sequence[str]:
        return tuple(self._store.keys_for(raw_id))

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:
        return self._store.latest_value(raw_id, label_key)

    def rates_for(self, raw_id: str) -> list[tuple[ConfirmedLabel, str]]:
        """该文档的全部标签 + **锚点区间串**（文档级为 `"document"`）。

        用 `all_for` 而不是 `keys_for`：条目级标签的身份包含它的区间，
        只拿键名分不出"这条标签标在哪一段"。
        """
        return [(label, _anchor_key(label)) for label in self._store.all_for(raw_id)]

    def __exit__(self, *exc_info: object) -> None:
        self._store.close()
        return None


def _anchor_key(label: ConfirmedLabel) -> str:
    """标签锚点的规范化字符串：文档级 = `"document"`，条目级 = `"start-end"`。"""
    if label.anchor is None:
        return "document"
    return f"{label.anchor.char_start}-{label.anchor.char_end}"


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
        每次请求各开一条只读会话（会话本身持有游标/缓存，**不该跨请求复用**）。
        注意：这**不是**因为 `sqlite3` 连接线程亲和——归档层已用
        `check_same_thread=False` + 锁解除了该限制（T-103，`70c5a68`），
        单个 store 实例可以安全地跨线程共享。这里按请求开会话是为了会话状态隔离。
        """
        if not self._db_path.exists():
            return _EmptySession()
        return _StoreSession(open_store(self._db_path))

    def record(
        self,
        *,
        raw_id: str,
        label_key: str,
        label_value: str,
        actor: str,
        anchor: Optional[EvidenceAnchor] = None,
    ) -> ConfirmedLabel:
        """写入一条人工标签 —— 本包的**唯一**写入路径。

        `ConfirmedLabel.human()` 是人工直判（不要求 AI 证据）；带 `anchor`
        就是**条目级**（2C 字符区间），不带就是**文档级**（1A）。
        `LabelStore.add()` 负责内容寻址的 `label_id`、`ON CONFLICT DO NOTHING`
        的幂等、以及触发器保护的只增不改。

        Raises:
            LabelRecordConflict: 库里有同 `label_id` 但**锚点不同**的记录 ——
                存储层会静默幂等，因此这里先查再写，把静默丢弃变成可见的拒绝。
        """
        label = ConfirmedLabel.human(
            raw_id=raw_id,
            label_key=label_key,
            label_value=label_value,
            actor=actor,
            anchor=anchor,
        )
        with open_store(self._db_path) as store:
            existing = store.get(label.label_id)
            if existing is not None and _anchor_key(existing) != _anchor_key(label):
                raise LabelRecordConflict(
                    f"同一条判断已存在但锚在不同位置：{label.raw_id} / "
                    f"{label.label_key}={label.label_value} / actor={label.actor}；"
                    f"库里已有锚点 {_anchor_key(existing)}，本次锚点 {_anchor_key(label)}。"
                    "label_id 不含锚点（内容寻址口径见 atlas.contracts.ids），"
                    "因此这两条判断无法共存 —— 拒绝静默丢弃本次判断",
                    existing=existing,
                )
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
        if path == "/entry":
            self._render_entry(target.query)
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
        if path in ("/", "/feed", "/entry"):
            self._reject_method(allow="GET", detail="feed / 条目页是只读投影，只接受 GET")
            return
        self._not_found(path)

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_method(allow=", ".join(_ALLOWED_METHODS), detail="不支持该方法")

    do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_PUT

    def _not_found(self, path: str) -> None:
        self._send_error(
            HTTPStatus.NOT_FOUND,
            "路径不存在",
            f"未知路径：{path}；可用：/feed（GET）、/entry（GET）、/label（POST）、/health（GET）",
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
        params = parse_qs(query_string, keep_blank_values=True)
        try:
            query = self._entry_query(params)
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
                    server.resolve_source(),
                    query,
                    label_lookup=_memoized_lookup(session),
                    entries_of=server.entries_lookup,
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
            notices=_entry_notices(result),
        )
        self._send_html(HTTPStatus.OK, document)

    @staticmethod
    def _entry_query(params: Mapping[str, Sequence[str]]) -> FeedQuery:
        """把浏览页的查询固定成**条目粒度**（SPEC §6.5：浏览 = 按条目看）。

        调用方显式传了 `granularity` 就尊重它（否则传 `document` 会被静默改写，
        那种"看着接受了其实没用"正是要避免的）；缺省时用条目粒度。
        """
        if "granularity" not in params:
            params = {**params, "granularity": [ENTRY_GRANULARITY]}
        return FeedQuery.from_params(params)

    # -- GET /entry ---------------------------------------------------------
    def _render_entry(self, query_string: str) -> None:
        """打开原文并高亮该条目区间。

        区间是**整数**参数，直接用来切片（`text[char_start:char_end]`）；
        本路由不做任何坐标推算。切片来自 `EntrySet.content_slice()` 的产物
        （去标签与 CDATA 外壳、**不解实体**），因此字符数与偏移保持一致；
        渲染时一律经 `escape()`。
        """
        server = self.server
        params = parse_qs(query_string, keep_blank_values=True)
        unknown = sorted(set(params) - ENTRY_PARAMS)
        if unknown:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "查询参数非法",
                f"未知参数 {unknown}；允许：{sorted(ENTRY_PARAMS)}",
            )
            return
        try:
            raw_id = _require_query_text(params, "raw_id", max_length=MAX_RAW_ID_LENGTH)
            char_start = _require_query_int(params, "char_start", maximum=MAX_ANCHOR_CHAR)
            char_end = _require_query_int(params, "char_end", maximum=MAX_ANCHOR_CHAR)
            if char_end <= char_start:
                raise _FormRejected(
                    HTTPStatus.BAD_REQUEST,
                    "区间非法",
                    f"char_end({char_end}) 必须大于 char_start({char_start})",
                )
            requested_entry_id = _optional_query_text(params, "entry_id")
        except _FormRejected as exc:
            self._send_error(exc.status, exc.title, exc.message)
            return

        record = None
        try:
            source = server.resolve_source()
            record = _record_of(source, raw_id)
            located = server.locate_entry(record, char_start=char_start, char_end=char_end)
        except _FormRejected as exc:
            self._send_error(exc.status, exc.title, exc.message)
            return
        except Exception as exc:  # noqa: BLE001 - 兜底但不吞
            _log.exception("条目页渲染内部错误")
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "内部错误",
                f"{type(exc).__name__}: {exc}",
            )
            return

        # `entry_id` 是可选的：给了就必须对得上，否则说明链接是旧的（条目层换了版本）。
        # 不接受"看不懂就忽略"——那正是"静默忽略参数"。
        if requested_entry_id is not None and requested_entry_id != located.entry.entry_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "条目 ID 与原文对不上",
                f"链接里的 entry_id={requested_entry_id!r}，而按该区间定位到的是 "
                f"{located.entry.entry_id!r}。条目 ID 是**派生**量（掺入解析器版本），"
                "换解析器后会整体漂移；请从 feed 页面重新点进来。",
            )
            return

        try:
            with server.labels.read_session() as session:
                labels = session.values_for(raw_id)
        except Exception as exc:  # noqa: BLE001
            _log.exception("读取标签失败")
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "读取标签失败",
                f"{type(exc).__name__}: {exc}",
            )
            return

        document = render_entry_page(
            located.entry,
            content=located.content,
            sliced=located.sliced,
            labels=labels,
            notice=located.notice,
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

        # 锚点必须与服务端认定的原文与区间一致 —— 客户端说了不算。
        try:
            anchor = self._anchor_from_fields(
                fields, raw_id=raw_id, label_value=label_value, label_key=label_key
            )
        except _FormRejected as exc:
            self._send_error(exc.status, exc.title, exc.message)
            return

        try:
            label = server.labels.record(
                raw_id=raw_id,
                label_key=label_key,
                label_value=label_value,
                actor=actor,
                anchor=anchor,
            )
        except LabelRecordConflict as exc:
            _log.warning("打标被拒绝（锚点冲突）：%s", exc)
            self._send_error(
                HTTPStatus.CONFLICT,
                "同一条判断已存在但锚在不同位置",
                str(exc),
            )
            return
        except Exception as exc:  # noqa: BLE001 - 兜底但不吞：写明真实原因
            _log.exception("打标写入失败")
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "打标写入失败",
                f"{type(exc).__name__}: {exc}",
            )
            return

        _log.info(
            "打标：raw_id=%s %s=%s actor=%s anchor=%s label_id=%s",
            label.raw_id,
            label.label_key,
            label.label_value,
            label.actor,
            _anchor_key(label),
            label.label_id,
        )
        self._redirect(HTTPStatus.SEE_OTHER, return_to)

    def _anchor_from_fields(
        self,
        fields: Mapping[str, str],
        *,
        raw_id: str,
        label_key: str,
        label_value: str,
    ) -> Optional[EvidenceAnchor]:
        """从表单读出可选锚点；**给一半就拒绝**，全给就按原文校验。

        校验三项：① `raw_sha256` 必须等于该 Raw 的真实内容指纹；
        ② `char_start >= 0` 且 `char_end > char_start`；
        ③ 区间必须落在这份原文里（按解码后字符数判定）。
        """
        present = [name for name in ANCHOR_FIELDS if _optional_text(fields, name)]
        if not present:
            return None
        if len(present) != len(ANCHOR_FIELDS):
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "锚点字段不完整",
                f"要带锚点就必须给全 {list(ANCHOR_FIELDS)}，只收到 {present}；"
                "半截锚点没有意义（SPEC §2.1 的存储约束同样拒绝半截锚点）",
            )

        raw_sha256 = _require_text(fields, "raw_sha256", max_length=64)
        if len(raw_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in raw_sha256):
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "锚点指纹非法",
                f"raw_sha256 必须是 64 位小写十六进制，收到 {raw_sha256!r}",
            )
        char_start = _require_form_int(fields, "char_start")
        char_end = _require_form_int(fields, "char_end")
        if char_start < 0 or char_end > MAX_ANCHOR_CHAR:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "锚点区间非法",
                f"要求 0 ≤ char_start 且 char_end ≤ {MAX_ANCHOR_CHAR}，"
                f"收到 [{char_start}, {char_end})",
            )
        if char_end <= char_start:
            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "锚点区间非法",
                f"char_end({char_end}) 必须大于 char_start({char_start})",
            )

        record = _record_of(self.server.resolve_source(), raw_id)
        if record is None:  # pragma: no cover - raw_exists 已经先查过
            raise _FormRejected(
                HTTPStatus.NOT_FOUND, "文档不存在", f"raw_id {raw_id!r} 不在归档里"
            )
        if raw_sha256 != record.content_sha256:
            raise _FormRejected(
                HTTPStatus.CONFLICT,
                "锚点指纹与原文不符",
                f"表单声明的 raw_sha256={raw_sha256[:12]}… 与归档里 {raw_id} 的指纹 "
                f"{record.content_sha256[:12]}… 不一致；拒绝把锚点钉在不匹配的原文上",
            )
        limit = self.server.text_length(record)
        if char_end > limit:            raise _FormRejected(
                HTTPStatus.BAD_REQUEST,
                "锚点区间越界",
                f"[{char_start}, {char_end}) 超出该原文的长度 {limit}（解码后字符数）",
            )
        return EvidenceAnchor.create(
            raw_id=raw_id,
            raw_sha256=raw_sha256,
            char_start=char_start,
            char_end=char_end,
        )

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


def _require_form_int(fields: Mapping[str, str], name: str) -> int:
    """表单里的整数字段：缺失/非十进制一律**响亮拒绝**。"""
    text = fields.get(name)
    if text is None:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "表单字段缺失", f"缺少必填字段 {name!r}"
        )
    try:
        return int(text.strip(), 10)
    except ValueError as exc:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "表单字段非法", f"{name} 必须是十进制整数，收到 {text!r}"
        ) from exc


def _require_query_text(
    params: Mapping[str, Sequence[str]], name: str, *, max_length: int
) -> str:
    values = params.get(name)
    if not values:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "查询参数缺失", f"缺少必填参数 {name!r}"
        )
    if len(values) > 1:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "查询参数重复", f"{name!r} 只允许出现一次"
        )
    text = values[0].strip()
    if not text:
        raise _FormRejected(HTTPStatus.BAD_REQUEST, "查询参数为空", f"{name!r} 不得为空")
    if len(text) > max_length:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST,
            "查询参数过长",
            f"{name!r} 上限 {max_length} 字符，收到 {len(text)}",
        )
    return text


def _require_query_int(
    params: Mapping[str, Sequence[str]], name: str, *, maximum: int
) -> int:
    text = _require_query_text(params, name, max_length=12)
    try:
        value = int(text, 10)
    except ValueError as exc:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "查询参数非法", f"{name} 必须是十进制整数，收到 {text!r}"
        ) from exc
    if value < 0 or value > maximum:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST,
            "查询参数非法",
            f"{name} 必须落在 [0, {maximum}]，收到 {value}",
        )
    return value


def _optional_query_text(
    params: Mapping[str, Sequence[str]], name: str
) -> Optional[str]:
    """可选的单值查询参数：缺省返回 `None`；出现多次即拒绝。"""
    values = params.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST, "查询参数重复", f"{name!r} 只允许出现一次"
        )
    text = values[0].strip()
    return text or None


def _record_of(source: Any, raw_id: str) -> Any:
    """从数据源取一条 RawRecord（只读）。取不到返回 `None`。"""
    getter = getattr(source, "get_raw", None)
    if callable(getter):
        try:
            return getter(raw_id)
        except Exception as exc:  # noqa: BLE001 - 不存在就是不存在，但理由要留下
            _log.debug("get_raw(%s) 失败：%s", raw_id, exc)
            return None
    by_id = getattr(source, "by_id", None)
    if callable(by_id):
        return by_id(raw_id)
    offset = 0
    while True:
        page = list(source.list_raw(SOURCE_PAGE_SIZE, offset))
        for record in page:
            if record.raw_id == raw_id:
                return record
        if len(page) < SOURCE_PAGE_SIZE:
            return None
        offset += len(page)


def _entry_notices(result: Any) -> tuple[str, ...]:
    """把条目层的 `problems` 聚合成页面顶部的显式提示（**绝不静默**）。"""
    seen: list[str] = []
    for item in result.items:
        for problem in getattr(item, "problems", ()):
            if problem not in seen:
                seen.append(problem)
    return tuple(seen)


def _derive_entries_lookup(source: Any) -> Any:
    """`entries_lookup` 缺省时的派生：**整篇即条目**（每个 Raw 一个条目）。

    为什么不"缺省就报错"：`entries_lookup` 描述的是"**这份原文的条目层结论**"，
    而 `RawRecord.entry_kind` 已经承载了"是不是容器"这个决定。没有条目层可用时，
    `entry_kind` 一律不是 `"feed"`（`None`），于是"整篇即条目"正是**契约本身**
    在那种输入下的答案，而不是一句编造的兜底。

    真正把"容器展开成派生条目"的实现由 `atlas.webapp` 注入（T-130）。
    未注入时**不会**有人把容器说成条目：本函数的输出里 `from_feed` 恒为 `False`。
    """
    entries_of = getattr(source, "entries_of", None)
    if callable(entries_of):
        return entries_of
    content_reader = getattr(source, "content", None)

    class _WholeDocumentEntries:
        """`RawEntriesView` 的结构实现：零条目 + 解码后原文的字符数。"""

        def __init__(self, entries: Sequence[Any], text_length: int) -> None:
            self.entries = entries
            self.text_length = text_length

    def lookup(record: Any) -> Any:
        text_length = 0
        if callable(content_reader):
            try:
                raw_bytes = content_reader(record.raw_id)
            except Exception as exc:  # noqa: BLE001 - 读不到就报清晰理由，不静默
                raise FeedQueryError(
                    f"无法读取 {record.raw_id} 的原文，无法给出条目层的字符长度："
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            try:
                text_length = len(bytes(raw_bytes).decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise FeedQueryError(
                    f"{record.raw_id} 的字节不是合法 UTF-8，拒绝把一个假的字符长度"
                    f"当作区间基准：{exc}"
                ) from exc
        return _WholeDocumentEntries(entries=(), text_length=text_length)

    return lookup


def _default_text_length(record: Any) -> int:
    """锚点范围校验用的原文长度。

    真实装配（`atlas.webapp`）注入 `EntrySet.feed_text` 的长度（与 T-130 的解码链
    完全一致）。这里没有原文可读，只能用 `byte_length` 作为**上界**：
    它可能比真实字符数大（UTF-8 多字节），因此只会让校验**更宽松**，
    不会误拒合法区间 —— 而下游的 `ConfirmedLabel` / 存储层仍会独立校验锚点形状。
    """
    value = getattr(record, "byte_length", None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    # 必须是 `_FormRejected`（而不是别的异常类型）：本函数只在表单校验路径被调用，
    # 抛别的类型会穿过 `except _FormRejected` 变成连接被重置，而不是一个可读的 4xx。
    raise _FormRejected(
        HTTPStatus.BAD_REQUEST,
        "无法判定原文长度",
        f"{getattr(record, 'raw_id', record)!r} 没有 byte_length，无法校验锚点区间",
    )


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
    """带 feed 数据源、条目派生层、标签访问、行业配置与「文档存在」判定的服务器。"""

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
        entries_lookup: Any = None,
        locate_entry: Any = None,
        text_length: Any = None,
    ) -> None:
        self._source = source
        self.labels = labels
        self.industry_provider = industry_provider
        self.raw_exists = raw_exists
        self.actor = actor
        #: 条目派生层（T-130）注入点。缺省时按"整篇即条目"派生（见 `_derive_entries_lookup`），
        #: 绝不把容器本身当成条目——真正的容器判定在注入的实现里（`atlas.webapp`）。
        self.entries_lookup = entries_lookup or _derive_entries_lookup(source)
        self.locate_entry = locate_entry or _whole_document_entry
        self.text_length = text_length or _default_text_length
        super().__init__(server_address, WebUIRequestHandler)

    def resolve_source(self) -> Any:
        """`source` 可以是 `FeedSource`，也可以是零参工厂（工厂每次调用新建一个实例）。"""
        if hasattr(self._source, "list_raw"):
            return self._source
        if callable(self._source):
            return self._source()
        raise TypeError(
            "source 必须是 FeedSource（有 list_raw）或返回 FeedSource 的零参工厂，"
            f"收到 {type(self._source).__name__}"
        )


def _whole_document_entry(record: Any, *, char_start: int, char_end: int) -> Any:
    """没有注入 `locate_entry` 时的整篇回退：把原文当作一个整篇条目。

    仅在**没有条目层**的装配下使用（自定义数据源 / 单元测试）。
    真实装配（`atlas.webapp`）注入基于 `atlas.entries` 的定位器。
    `sliced=False` 会让页面**明说**"没有可高亮的子区间"，不假装高亮过。
    """
    from atlas.feed.query import FeedEntry  # 局部 import：避免模块级循环

    if char_start != 0:
        raise _FormRejected(
            HTTPStatus.BAD_REQUEST,
            "区间非法",
            "当前装配没有条目派生层，只有整篇区间 [0, N) 可用；"
            f"收到 char_start={char_start}",
        )
    entry = FeedEntry(
        entry_id=None,
        raw_id=record.raw_id,
        raw_sha256=record.content_sha256,
        title=record.endpoint.rsplit("/", 1)[-1] or record.endpoint,
        link=record.endpoint,
        published_at=None,
        char_start=0,
        char_end=char_end,
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
    return _WholeDocumentLocation(entry=entry, content="", sliced=False, notice="")


class WebUIApplication:
    """在后台线程里跑 `WebUIHTTPServer` 的小包装（测试与本地运行共用）。

    ``with WebUIApplication(source, labels=..., industry_provider=...) as app:``
    → ``app.base_url`` 形如 ``http://127.0.0.1:<port>``。

    `on_close` 是给**组合根**用的收尾钩子（例如关闭注入的归档句柄）：
    本类不 import `atlas.archive`，因此只能由调用方告诉它"该关什么"。
    """

    def __init__(
        self,
        source: Any,
        *,
        labels: LabelAccess,
        industry_provider: Callable[[], Sequence[str]],
        raw_exists: Optional[Callable[[str], bool]] = None,
        actor: str = DEFAULT_ACTOR,
        entries_lookup: Any = None,
        locate_entry: Any = None,
        text_length: Any = None,
        on_close: Optional[Callable[[], None]] = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        resolved_exists = raw_exists if raw_exists is not None else _derive_raw_exists(source)
        self._on_close = on_close
        self._httpd = WebUIHTTPServer(
            (host, port),
            source=source,
            labels=labels,
            industry_provider=industry_provider,
            raw_exists=resolved_exists,
            actor=actor,
            entries_lookup=entries_lookup,
            locate_entry=locate_entry,
            text_length=text_length,
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
        if self._on_close is not None:
            self._on_close()

    def __enter__(self) -> "WebUIApplication":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"WebUIApplication(base_url={self.base_url!r})"


def _derive_raw_exists(source: Any) -> Callable[[str], bool]:
    """`raw_exists` 缺省时从数据源派生：工厂则每次新开（工厂是可选形态，不是线程约束）。"""
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
    entries_lookup: Any = None,
    locate_entry: Any = None,
    text_length: Any = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> WebUIApplication:
    """便捷装配：`StoreLabelAccess` + `WebUIApplication`（已启动）。

    `db_path` **必填**：本包不提供任何指向仓库 `data/` 的默认值。
    启动时显式初始化一次标签库（建表 + 触发器，归 T-108），此后浏览路径只读。

    条目派生层由调用方注入（本包不 import `atlas.entries`，见模块 docstring）：
    `entries_lookup` / `locate_entry` / `text_length` 三个可选接缝。
    真实装配见 `atlas.webapp.build_webapp`。
    """
    labels = StoreLabelAccess(db_path)
    labels.initialize()
    return WebUIApplication(
        source,
        labels=labels,
        industry_provider=industry_provider,
        raw_exists=raw_exists,
        actor=actor,
        entries_lookup=entries_lookup,
        locate_entry=locate_entry,
        text_length=text_length,
        host=host,
        port=port,
    )
