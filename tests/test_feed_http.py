"""T-106 HTTP 层的端到端测试（真实服务、本地临时端口、**不出网**）。

覆盖（对应任务书判据）：
- 真实 `ThreadingHTTPServer` 起在 `127.0.0.1:0`，用 stdlib `urllib.request` 打真实请求；
- 每个用例**关掉**服务（`with FeedServer(...)`），并单独验证端口已释放；
- 非法参数 → `400` 且**明确指出是哪个参数**（不是静默忽略）；
- 并发请求不被串行化（`ThreadingHTTPServer` 的硬性要求，SPEC §2.11）；
- **只读性**：真归档 + HTTP 全流程前后做文件快照比对，`ArchiveStore.verify()` 无问题；
  外加 AST 静态检查（本包无写入调用、不 import `atlas.labels`）；
- 不访问外部网络（全部 `127.0.0.1`）。
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

import pytest

from atlas.archive import open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256
from atlas.feed import (
    CONTRACT_VERSION,
    FeedServer,
    StaticFeedSource,
    archive_source_factory,
    labels_from_mapping,
)

BASE = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
FEED_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "atlas" / "feed"


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #
def make_record(
    raw_id: str,
    *,
    channel_id: str = "chan-1",
    fetched_at: datetime = BASE,
    payload: bytes = b"hello atlas",
) -> RawRecord:
    return RawRecord(
        raw_id=raw_id,
        channel_id=channel_id,
        endpoint=f"https://example.test/{raw_id}",
        content_sha256=content_sha256(payload),
        byte_length=len(payload),
        fetched_at=fetched_at,
        http_status=200,
    )


def demo_records() -> List[RawRecord]:
    return [
        make_record("r-1", channel_id="chan-1", fetched_at=BASE + timedelta(hours=3)),
        make_record("r-2", channel_id="chan-2", fetched_at=BASE + timedelta(hours=2)),
        make_record("r-3", channel_id="chan-1", fetched_at=BASE + timedelta(hours=1)),
        make_record("r-4", channel_id="chan-3", fetched_at=BASE),
    ]


def demo_source(labels: Dict[str, Sequence[str]] | None = None) -> StaticFeedSource:
    return StaticFeedSource(
        demo_records(),
        industry_of={"chan-1": "ai", "chan-2": "web", "chan-3": "ai"},
    )


@contextmanager
def running_server(
    source: Any,
    *,
    label_lookup: Any = None,
) -> Iterator[FeedServer]:
    server = FeedServer(source, label_lookup=label_lookup)
    try:
        assert server.port != 0
        yield server
    finally:
        server.close()


def get(server: FeedServer, path: str, *, timeout: float = 5.0) -> tuple[int, Any, Dict[str, str]]:
    """返回 `(status, json_body, headers)`；4xx/5xx 不抛异常，便于断言错误契约。"""
    url = server.base_url + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body), dict(exc.headers)


def snapshot(root: Path) -> Dict[str, str]:
    """目录内容快照：相对路径 → sha256（用于证明"读操作没动过数据"）。"""
    out: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------- #
# 基本契约
# ---------------------------------------------------------------------- #
def test_health_endpoint() -> None:
    with running_server(demo_source()) as server:
        status, body, headers = get(server, "/health")
    assert status == 200
    assert body == {"status": "ok", "contract_version": CONTRACT_VERSION}
    assert headers["Content-Type"].startswith("application/json")


def test_feed_json_contract_shape() -> None:
    with running_server(demo_source()) as server:
        status, body, _ = get(server, "/feed?limit=2")

    assert status == 200
    assert body["contract_version"] == CONTRACT_VERSION
    assert set(body) == {"contract_version", "items", "page", "sort", "filters"}

    assert body["page"] == {
        "limit": 2,
        "offset": 0,
        "total": 4,
        "returned": 2,
        "has_more": True,
        "next_offset": 2,
    }
    assert body["sort"] == {"column": "fetched_at", "order": "desc", "tie_break": "raw_id asc"}
    assert body["filters"] == {
        "industries": [],
        "channels": [],
        "since": None,
        "until": None,
        "labels": [],
        "labeled": None,
    }

    first = body["items"][0]
    assert set(first) == {
        "raw_id",
        "channel_id",
        "industry",
        "endpoint",
        "content_sha256",
        "byte_length",
        "fetched_at",
        "http_status",
        "labels",
    }
    assert [item["raw_id"] for item in body["items"]] == ["r-1", "r-2"]
    assert first["industry"] == "ai"
    assert first["http_status"] == 200
    assert first["byte_length"] == len(b"hello atlas")
    assert first["labels"] == []
    # 时间必须是带时区的 ISO-8601（契约的一部分，前端据此排序/展示）
    assert datetime.fromisoformat(first["fetched_at"]).tzinfo is not None


def test_filter_sort_and_pagination_over_http() -> None:
    with running_server(demo_source()) as server:
        status, full, _ = get(server, "/feed?limit=200&order=asc")
        assert status == 200
        assert [item["raw_id"] for item in full["items"]] == ["r-4", "r-3", "r-2", "r-1"]

        collected: List[str] = []
        offset = 0
        while True:
            status, page, _ = get(server, f"/feed?order=asc&limit=2&offset={offset}")
            assert status == 200
            collected.extend(item["raw_id"] for item in page["items"])
            if not page["page"]["has_more"]:
                break
            offset = page["page"]["next_offset"]
        assert collected == ["r-4", "r-3", "r-2", "r-1"]  # 不重不漏

        status, industry, _ = get(server, "/feed?industry=web")
        assert status == 200
        assert [item["raw_id"] for item in industry["items"]] == ["r-2"]
        assert industry["filters"]["industries"] == ["web"]

        status, chan, _ = get(server, "/feed?channel=chan-1&order=asc")
        assert [item["raw_id"] for item in chan["items"]] == ["r-3", "r-1"]

        since = urllib.parse.quote((BASE + timedelta(hours=1)).isoformat())
        status, window, _ = get(server, f"/feed?since={since}&order=asc")
        assert status == 200
        assert [item["raw_id"] for item in window["items"]] == ["r-3", "r-2", "r-1"]


def test_empty_result_is_200_with_empty_items() -> None:
    with running_server(demo_source()) as server:
        status, body, _ = get(server, "/feed?industry=nonexistent")
    assert status == 200
    assert body["items"] == []
    assert body["page"]["total"] == 0
    assert body["page"]["has_more"] is False
    assert body["page"]["next_offset"] is None


def test_offset_beyond_total_over_http() -> None:
    with running_server(demo_source()) as server:
        status, body, _ = get(server, "/feed?offset=100")
    assert status == 200
    assert body["items"] == []
    assert body["page"]["total"] == 4


# ---------------------------------------------------------------------- #
# 非法参数必须被拒绝
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query_string, parameter",
    [
        ("limit=0", "limit"),
        ("limit=-1", "limit"),
        ("limit=999", "limit"),          # 超过 MAX_LIMIT：拒绝而不是截断
        ("limit=abc", "limit"),
        ("offset=-1", "offset"),
        ("offset=1.5", "offset"),
        ("order=sideways", "order"),
        ("order=asc&order=desc", "order"),
        ("since=2026-01-01", "since"),                     # naive → 拒绝
        ("since=not-a-time", "since"),
        ("since=2026-03-01T09:00:00%2000:00", "since"),     # '+' 未编码 → 拒绝并提示
        ("since=2026-02-01T00:00:00Z&until=2026-01-01T00:00:00Z", "since"),
        ("labeled=maybe", "labeled"),
        ("labeled=false&labels=valid", "labeled"),
        ("industry=", "industry"),
        ("sector=ai", "sector"),                            # 未知参数
        ("limit=5&limit=6", "limit"),
    ],
)
def test_invalid_parameters_are_rejected_with_400(query_string: str, parameter: str) -> None:
    with running_server(demo_source()) as server:
        status, body, headers = get(server, f"/feed?{query_string}")

    assert status == 400, f"{query_string} 应当被拒绝（不是静默忽略）"
    assert headers["Content-Type"].startswith("application/json")
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["error"]["code"] == "invalid_query"
    assert body["error"]["parameter"] == parameter
    assert body["error"]["message"]
    assert "items" not in body  # 拒绝时不得顺带返回一页数据


def test_limit_upper_bound_is_accepted_over_http() -> None:
    with running_server(demo_source()) as server:
        status, body, _ = get(server, "/feed?limit=200")
    assert status == 200
    assert body["page"]["limit"] == 200


def test_unknown_path_returns_404() -> None:
    with running_server(demo_source()) as server:
        status, body, _ = get(server, "/raw/r-1")
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_non_get_methods_are_rejected_405() -> None:
    """feed 是只读投影：写方法在协议层就没有入口。"""
    with running_server(demo_source()) as server:
        request = urllib.request.Request(
            server.base_url + "/feed", data=b'{"raw_id":"r-1"}', method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 405
        assert excinfo.value.headers["Allow"] == "GET"
        body = json.loads(excinfo.value.read().decode("utf-8"))
    assert body["error"]["code"] == "method_not_allowed"


# ---------------------------------------------------------------------- #
# 标签筛选走注入的 label_lookup（不依赖 T-108）
# ---------------------------------------------------------------------- #
def test_label_filters_use_injected_lookup() -> None:
    lookup = labels_from_mapping({"r-1": ("valid",), "r-3": ("spam", "valid")})
    with running_server(demo_source(), label_lookup=lookup) as server:
        status, labeled, _ = get(server, "/feed?labeled=true&order=asc")
        assert status == 200
        assert [item["raw_id"] for item in labeled["items"]] == ["r-3", "r-1"]
        assert labeled["items"][0]["labels"] == ["spam", "valid"]  # 去重且有序
        assert labeled["filters"]["labeled"] is True

        status, unlabeled, _ = get(server, "/feed?labeled=false&order=asc")
        assert [item["raw_id"] for item in unlabeled["items"]] == ["r-4", "r-2"]

        status, by_label, _ = get(server, "/feed?labels=spam&order=asc")
        assert [item["raw_id"] for item in by_label["items"]] == ["r-3"]


def test_label_filter_without_lookup_fails_loudly() -> None:
    """未接线就必须 500，绝不返回"看起来正常"的空列表（CLAUDE.md 硬规则 2）。"""
    with running_server(demo_source()) as server:
        status, body, _ = get(server, "/feed?labeled=true")
    assert status == 500
    assert body["error"]["code"] == "feed_unavailable"
    assert "label_lookup" in body["error"]["message"]


# ---------------------------------------------------------------------- #
# 线程化与生命周期
# ---------------------------------------------------------------------- #
class SlowSource(StaticFeedSource):
    """每次查询慢 0.4s，并记录处理线程 —— 用来证明服务确实是多线程的。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.threads: List[int] = []
        self._lock = threading.Lock()

    def list_raw(self, limit: int, offset: int) -> Sequence[RawRecord]:
        with self._lock:
            self.threads.append(threading.get_ident())
        time.sleep(0.4)
        return super().list_raw(limit, offset)


def test_server_is_threading_and_does_not_serialize_requests() -> None:
    source = SlowSource(demo_records(), industry_of={"chan-1": "ai"})
    with running_server(source) as server:
        assert isinstance(server.server, ThreadingHTTPServer)

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(get, server, "/feed?limit=1") for _ in range(4)]
            statuses = [future.result()[0] for future in futures]
        elapsed = time.monotonic() - started

    assert statuses == [200, 200, 200, 200]
    assert len(set(source.threads)) >= 2, "并发请求应落在不同线程上"
    assert elapsed < 1.2, f"4 个 0.4s 请求被串行化了（{elapsed:.2f}s）"


def test_server_close_releases_port() -> None:
    with running_server(demo_source()) as server:
        base_url = server.base_url
        assert get(server, "/health")[0] == 200
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(base_url + "/health", timeout=2)


def test_source_factory_is_resolved_per_request() -> None:
    """SQLite 连接线程亲和 ⇒ 允许传零参工厂，每请求新建（归档适配的推荐用法）。"""
    created: List[int] = []

    def factory() -> StaticFeedSource:
        created.append(threading.get_ident())
        return demo_source()

    with running_server(factory) as server:
        assert get(server, "/feed?limit=1")[0] == 200
        assert get(server, "/feed?limit=1")[0] == 200

    assert len(created) == 2


# ---------------------------------------------------------------------- #
# 只读性：真归档 + HTTP 全流程
# ---------------------------------------------------------------------- #
def test_archive_backed_feed_end_to_end(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    archive = open_archive(store_root)
    for index in range(3):
        record = make_record(
            f"arch-{index}",
            channel_id=f"chan-{index}",
            fetched_at=BASE + timedelta(minutes=index),
            payload=f"payload-{index}".encode(),
        )
        archive.put(record, f"payload-{index}".encode())
    assert archive.verify() == []

    before = snapshot(store_root)
    with running_server(
        # sqlite 连接线程亲和 ⇒ 每请求新开归档（仓库提供的工厂用法）
        archive_source_factory(store_root, industry_of={"chan-0": "ai", "chan-1": "ai"})
    ) as server:
        status, body, _ = get(server, "/feed?order=asc")
        assert status == 200
        assert [item["raw_id"] for item in body["items"]] == ["arch-0", "arch-1", "arch-2"]
        assert [item["industry"] for item in body["items"]] == ["ai", "ai", None]
        assert [item["byte_length"] for item in body["items"]] == [9, 9, 9]

        status, filtered, _ = get(server, "/feed?industry=ai&order=asc")
        assert status == 200
        assert [item["raw_id"] for item in filtered["items"]] == ["arch-0", "arch-1"]

    assert snapshot(store_root) == before, "HTTP 只读查询不得改动任何归档文件"
    assert archive.verify() == []
    archive.close()


def test_archive_is_untouched_by_http_reads(tmp_path: Path) -> None:
    """把归档暴露给全部对外入口（含非法请求与写方法）后，数据必须一字未动。"""
    store_root = tmp_path / "store"
    archive = open_archive(store_root)
    record = make_record("arch-0", channel_id="chan-0", payload=b"immutable")
    archive.put(record, b"immutable")
    before = snapshot(store_root)

    with running_server(
        archive_source_factory(store_root, industry_of={"chan-0": "ai"})
    ) as server:
        requests = [
            "/feed",
            "/feed?limit=0",
            "/feed?labeled=true",
            "/feed?sector=ai",
            "/feed?industry=ai&order=asc&limit=1",
            "/health",
            "/nope",
        ]
        statuses = [get(server, path)[0] for path in requests]

    assert statuses == [200, 400, 500, 400, 200, 200, 404]

    assert snapshot(store_root) == before
    assert archive.all_raw_ids() == ["arch-0"]
    assert archive.get_content("arch-0") == b"immutable"
    assert archive.verify() == []
    archive.close()


# ---------------------------------------------------------------------- #
# 静态检查：无写入路径 / 不与 T-108 耦合
# ---------------------------------------------------------------------- #
FEED_MODULES = sorted(FEED_PACKAGE.glob("*.py"))

#: 允许 import 的 stdlib 根模块白名单（没有 os / shutil / sqlite3 / pathlib / io，
#: 因此结构上不存在文件或数据库写入手段）。
ALLOWED_STDLIB_ROOTS = frozenset(
    {"__future__", "dataclasses", "datetime", "http", "json", "logging", "threading", "typing", "urllib"}
)

#: 绝不允许出现的调用名（写入语义）。`replace` 不在此列是有意的：`datetime.replace`
#: 与写入无关，而 `os.replace` 已经被 import 白名单挡在门外（本包不允许 import os）。
FORBIDDEN_CALLS = frozenset(
    {
        "put",
        "insert",
        "add",
        "delete",
        "update",
        "write",
        "write_new",
        "rewrite_meta",
        "execute",
        "executescript",
        "commit",
        "connect",
        "unlink",
        "rmtree",
        "mkdir",
        "makedirs",
        "rename",
        "dump",
    }
)

#: 唯一允许的 `write` 调用：HTTP 响应体（不是数据写入）。
ALLOWED_WRITE_TARGETS = frozenset({"self.wfile.write"})


def test_feed_package_files_exist() -> None:
    assert [path.name for path in FEED_MODULES] == [
        "__init__.py",
        "http.py",
        "query.py",
        "repository.py",
    ]


def test_feed_package_has_no_write_path() -> None:
    """静态证明本包没有写入路径：白名单 import + 禁用调用名。"""
    for path in FEED_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])

        unexpected = {
            name
            for name in imported
            if name != "atlas" and name not in ALLOWED_STDLIB_ROOTS
        }
        assert not unexpected, f"{path.name} 引入了白名单外的模块（可能是写入通道）：{unexpected}"

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                if ast.unparse(func) in ALLOWED_WRITE_TARGETS:
                    continue
                assert func.attr not in FORBIDDEN_CALLS, (
                    f"{path.name}:{node.lineno} 调用了写入语义的方法 {func.attr}()"
                )
            elif isinstance(func, ast.Name):
                assert func.id != "open", f"{path.name}:{node.lineno} 打开了文件"


def test_feed_package_does_not_import_labels_or_registry_impl() -> None:
    """与在建任务解耦：只允许依赖 atlas.contracts（类型）与 atlas.archive（只读适配）。"""
    allowed_atlas = {"atlas.contracts", "atlas.archive", "atlas.contracts.ids", "atlas.feed"}
    for path in FEED_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("atlas.labels"), (
                        f"{path.name} 不得 import atlas.labels（T-108 在建）"
                    )
            if module is None or not module.startswith("atlas"):
                continue
            assert module in allowed_atlas, f"{path.name} 不得 import {module}（SPEC §4.0）"
