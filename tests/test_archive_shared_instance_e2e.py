"""端到端回归：**单个** `ArchiveStore` 实例接进真实 HTTP 服务（T-120 的复现路径）。

`tests/test_feed_http.py` / `tests/test_webui_app.py` 都传"每请求新建归档"的**工厂**
（`archive_source_factory`），所以它们**结构上无法**发现原缺陷；服务端也能靠调用方
纪律绕开。本文件故意反过来：把**同一个** `ArchiveStore` 交给
`atlas.webui.build_application(source=...)`（内部是 `ThreadingHTTPServer`，每请求一线程），
然后打真实 HTTP 请求。

修复前的失败形态::

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be used
    in that same thread.   → GET /feed 返回 500
"""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.archive import ArchiveStore, open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256
from atlas.feed import ArchiveFeedSource
from atlas.webui import build_application

UTC = timezone.utc
BASE = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)
INDUSTRY_MAP = {"chan-0": "cs.LG", "chan-1": "stat.ML"}
DEMO_RECORDS = (
    ("arch-0", "chan-0", BASE, b"payload-0"),
    ("arch-1", "chan-1", BASE + timedelta(minutes=1), b"payload-1"),
    ("arch-2", "chan-2", BASE + timedelta(minutes=2), b"payload-2"),
)


def make_record(raw_id: str, channel_id: str, fetched_at: datetime, payload: bytes) -> RawRecord:
    return RawRecord(
        raw_id=raw_id,
        channel_id=channel_id,
        endpoint=f"https://example.test/{raw_id}",
        content_sha256=content_sha256(payload),
        byte_length=len(payload),
        fetched_at=fetched_at,
        http_status=200,
    )


def seed_shared_archive(tmp_path: Path) -> ArchiveStore:
    """写入三条原文并**返回仍打开的那个实例**（刻意不复用"每请求新建"的工厂）。"""
    archive = open_archive(tmp_path / "store")
    for raw_id, channel_id, fetched_at, payload in DEMO_RECORDS:
        archive.put(make_record(raw_id, channel_id, fetched_at, payload), payload)
    assert archive.verify() == []
    return archive


def get(base_url: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base_url + path, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也作为观测值返回
        return exc.code, exc.read()


def test_shared_archive_instance_serves_feed_end_to_end(tmp_path: Path) -> None:
    """复现路径：共享实例 → `build_application` → GET /feed 必须是 200 且渲染出记录。"""
    archive = seed_shared_archive(tmp_path)
    try:
        app = build_application(
            ArchiveFeedSource(archive, industry_of=INDUSTRY_MAP),
            db_path=tmp_path / "labels.db",
            industry_provider=lambda: ("cs.LG", "stat.ML"),
            raw_exists=lambda raw_id: raw_id in set(archive.all_raw_ids()),
        )
        with app:
            status, body = get(app.base_url, "/feed")
            assert status == 200, (
                "GET /feed 失败（共享 ArchiveStore 跨线程使用）："
                f"{status} {body[:400]!r}"
            )
            # webui 的 /feed 是服务端渲染的 HTML（不是 JSON）；断言三条记录都渲染出来
            html = body.decode("utf-8")
            for raw_id, _channel, _fetched, _payload in DEMO_RECORDS:
                assert raw_id in html, f"/feed 页面缺少 {raw_id}（服务端渲染不完整）"
            assert "cs.LG" in html and "stat.ML" in html

            # 带筛选与翻页参数的请求同样走归档读路径，也不能炸
            status, body = get(app.base_url, "/feed?industry=cs.LG&limit=2&order=asc")
            assert status == 200, f"{status} {body[:400]!r}"
            filtered = body.decode("utf-8")
            assert "arch-0" in filtered
            assert "arch-2" not in filtered, "筛选参数没有被真正应用"
    finally:
        archive.close()


def test_shared_archive_instance_under_concurrent_requests(tmp_path: Path) -> None:
    """多个请求线程**同时**打 /feed：共享实例必须全部 200，且页面一致。"""
    archive = seed_shared_archive(tmp_path)
    try:
        app = build_application(
            ArchiveFeedSource(archive, industry_of=INDUSTRY_MAP),
            db_path=tmp_path / "labels.db",
            industry_provider=lambda: ("cs.LG", "stat.ML"),
            raw_exists=lambda raw_id: raw_id in set(archive.all_raw_ids()),
        )
        with app:
            def hit(_index: int) -> tuple[int, tuple[str, ...]]:
                status, body = get(app.base_url, "/feed")
                if status != 200:
                    return status, (body.decode("utf-8", "replace")[:200],)
                html = body.decode("utf-8")
                return status, tuple(
                    raw_id for raw_id, *_ in DEMO_RECORDS if raw_id in html
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = [pool.submit(hit, index) for index in range(24)]
                observed = [future.result() for future in results]

            statuses = {status for status, _ in observed}
            assert statuses == {200}, f"并发请求出现非 200：{observed}"
            assert {ids for _, ids in observed} == {
                tuple(raw_id for raw_id, *_ in DEMO_RECORDS)
            }
    finally:
        archive.close()


def test_shared_archive_instance_serves_health(tmp_path: Path) -> None:
    """`/health` 必须在服务线程里可用（不需要碰归档也能证实服务真的起来了）。"""
    archive = seed_shared_archive(tmp_path)
    try:
        app = build_application(
            ArchiveFeedSource(archive, industry_of=INDUSTRY_MAP),
            db_path=tmp_path / "labels.db",
            industry_provider=lambda: ("cs.LG", "stat.ML"),
        )
        with app:
            status, body = get(app.base_url, "/health")
        assert status == 200
        assert json.loads(body.decode("utf-8"))["status"] == "ok"
    finally:
        archive.close()


def test_archive_store_directly_usable_from_request_like_threads(tmp_path: Path) -> None:
    """不经过 HTTP 的最小复现：服务线程会做的调用，换个线程做同样不能炸。"""
    archive = seed_shared_archive(tmp_path)
    try:
        # `atlas.webui` / `atlas.feed` 在请求线程里实际调用的就是这几个方法
        def request_thread() -> tuple:
            return (
                tuple(archive.all_raw_ids()),
                archive.get("arch-0"),
                archive.get_content("arch-0"),
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = [pool.submit(request_thread) for _ in range(18)]
            observed = {future.result() for future in results}

        assert len(observed) == 1
        ids, record, content = next(iter(observed))
        assert ids == ("arch-0", "arch-1", "arch-2")
        assert record == make_record("arch-0", "chan-0", BASE, b"payload-0")
        assert content == b"payload-0"
    finally:
        archive.close()
