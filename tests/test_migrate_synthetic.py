"""T-131 旧语料导入 —— **先定义、后实现**的验收判据（SPEC §4.1 T-002 的写法）。

本文件是 T-131 的判据正本。实现写在 `src/atlas/migrate/`；**判据先于实现写下**
（SPEC §4.1 对 T-002 的要求，本任务沿用同一节奏）。每条判据都有对应的测试函数，
并用**合成旧语料**（`tmp_path`）验证 —— 因此干净 worktree 里也会真的跑（不需要 `data/`）。

背景：这条路为什么必须存在
--------------------------

SPEC §6.3 裁决 B 记下的事实：新结构此前把**整个 feed** 归档成一篇文档，
10 条归档记录里含 830 篇文章，**没有一篇成为独立文档**（§1.4 的成功信号因此只能
"给整份 feed 打标"）。旧系统 `data/raw/<频道>/*.json` 反倒是**按篇**存的。
T-131 就是把旧语料的**逐篇**导入新归档，让 `raw_id` 重新等于"一篇文章"。

判据（1–18）
------------

1. **按篇，`raw_id` 逐篇**：同一频道下两篇**不同 URL**的文章 → **不同** `raw_id`；
   且 `raw_id == raw_id_for(channel_id, source_url, sha256(raw_content字节))`
   （`endpoint` = 旧记录 `source_url`，不是 feed 地址）。
2. **只导入真正的文档**：`document_type` 缺失（旧系统的 `empty_*` / `summary_*`）
   与 `raw_content` 为空的记录一律**跳过并记账**，不进归档、不计失败。
3. **不静默跳过**：每个扫描到的文件都必须在报告里有归宿；对账表必须加得起来
   （`扫描 = 跳过 + 文档`、`文档 = 新建 + 收敛 + 失败`），且 `ledger_problems() == []`。
4. **幂等**：同一输入跑两次 → `raw_records` 行数不变、第二次 `imported == 0`、
   所有文档记为"收敛"，报告指纹相同。
5. **不得迁移任何人工标签**（§2.4）：标签锚在 `raw_id` 上，而导入会**改变条目身份**
   （旧 uuid → 内容寻址 `raw_id`），迁移标签等于把打标数据锚到错误对象。
   本包没有任何写标签的代码路径，且**以导入侧拦截**证明（见 `test_migrate_no_label_migration.py`）。
6. **`data/raw/` 只读**：进出各算一次整树 sha256 并断言相等。
7. **`fetched_at` 来自旧记录自己的抓取时间**（`collected_at`），**不是"现在"**。
8. **零新增依赖**：只用标准库与已装的 `pydantic`（经 `atlas.contracts`）。
9. **不变量不退化**：导入后 `verify() == []`；`raw_records` 仍受 append-only 触发器保护
   （**否定性断言配活对照**：先证明合法 `SELECT` / `INSERT` 通过，再断言 `UPDATE` / `DELETE` 被拒）。
10. **失败响亮**：字段缺失 / 时间无法解析 / 编码异常 / 频道映射不到注册表，
    都要有明确的失败记录与原因码；`strict=True` 时直接抛异常，
    且**非数据类异常（接线错误）不被 `except` 吞掉**。
11. **行业归属保住**（§2.5 的 C8 闭环）：导入记录的 `channel_id` 必须是注册表里
    真实存在的渠道，否则行业筛选会静默落空。
12. **字节即事实**：`content.bin` 的 sha256 与 `raw_records.content_sha256` 一致，
    且等于旧记录 `raw_content` 的 UTF-8 字节指纹。
13. **`data/raw/` 不改动** —— 同 6（真实数据上量测，见 `test_migrate_realdata.py`）。
14. **不退化 `raw_records` 的追加性**：见 9。
15. **`pytest` 全绿**：提交前退出码 0，且提交后用独立 worktree 复核；真实数据缺失时
    真实数据用例 `skip`（照 `tests/test_search_realdata.py` 的先例），判据 1–12 仍全跑。
16. **对账表可断言**：`MigrateReport.digest()` 在"同输入同归档状态"下稳定，
    可用于证明"第二次运行真的是同一件事"。
17. **不猜时间**：全部时间字段都缺失/不可解析时**不产生任何记录**（记失败），
    更**不得**回退到 `datetime.now()`。
18. **`raw_id` 不含旧 uuid**：旧 uuid 只作溯源（`legacy_id` 字段），
    不参与指纹 —— 否则同一篇文章的 10 次重复采集会得到 10 个身份，去重不可能收敛。

真实语料上的量测（判据 1–18 的落地证据）见 `tests/test_migrate_realdata.py`。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from atlas.archive import open_archive
from atlas.contracts.ids import content_sha256, raw_id_for
from atlas.migrate import (
    CONTENT_FIELD,
    MOMENT_FIELDS,
    REASON_MISSING_TIMESTAMP,
    REASON_NESTED_LAYOUT,
    REASON_NOT_AN_OBJECT,
    REASON_UNPARSEABLE_JSON,
    REASON_UNPARSEABLE_TIMESTAMP,
    REASON_UNREADABLE_BYTES,
    SKIP_EMPTY_CONTENT,
    SKIP_NON_DOCUMENT,
    STATUS_DEDUPLICATED,
    STATUS_FAILED,
    STATUS_IMPORTED,
    STATUS_SKIPPED,
    ChannelMappingError,
    Migrate,
    MigrateOptions,
    MigrateReport,
    build_channel_map,
    channel_map_resolver,
    classify_legacy,
    empty_mapper,
    migrate_legacy,
    nested_relative_paths,
    pick_moment_field,
    resolve_nothing,
)
from tests._migrate_fixtures import (
    directory_stats,
    fake_channels,
    legacy_record,
    record_dirs,
    tree_digest,
    utc,
    write_legacy,
    write_raw_bytes,
)

#: 合成本用例的渠道注册表：`chan-a` / `chan-b` 属同一行业 `industry-x`。
REGISTRY = fake_channels((("chan-a", "industry-x"), ("chan-b", "industry-x")))
CHANNEL_MAP = build_channel_map(REGISTRY)

#: 旧记录里那条确定的时间（实测形状：无时区、微秒精度）。
COLLECTED_AT = "2025-12-21T11:41:02.772123"
COLLECTED_DT = datetime(2025, 12, 21, 11, 41, 2, 772123, tzinfo=timezone.utc)


@pytest.fixture
def legacy_root(tmp_path: Path) -> Path:
    """合成旧语料根（**不碰仓库 `data/`**）。"""
    root = tmp_path / "legacy" / "raw"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "store"


def _run(legacy_root: Path, store_root: Path, **kwargs: Any) -> MigrateReport:
    options = MigrateOptions(
        legacy_root=legacy_root,
        archive_root=store_root,
        channel_map=kwargs.pop("channel_map", CHANNEL_MAP),
        **kwargs,
    )
    with Migrate(options) as migrator:
        report = migrator.run()
        assert migrator.archive.verify() == [], "合成导入后归档自检必须为空"
    return report


# --------------------------------------------------------------------------- #
# 判据 1：按篇，raw_id 逐篇
# --------------------------------------------------------------------------- #
def test_criterion_1_raw_id_is_per_article_and_from_source_url(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 1：`raw_id` 逐篇，且由 `(channel_id, source_url, sha256(正文字节))` 决定。"""
    write_legacy(
        legacy_root,
        "chan-a",
        "one.json",
        legacy_record(id="uuid-1", source_url="https://example.invalid/a/1", raw_content="alpha"),
    )
    write_legacy(
        legacy_root,
        "chan-a",
        "two.json",
        legacy_record(id="uuid-2", source_url="https://example.invalid/a/2", raw_content="beta"),
    )
    report = _run(legacy_root, store_root)

    assert report.imported == 2, "两篇不同文章必须是两条新 Raw"
    assert report.distinct_raw_ids == 2
    expected_one = raw_id_for(
        "chan-a", "https://example.invalid/a/1", content_sha256("alpha".encode("utf-8"))
    )
    expected_two = raw_id_for(
        "chan-a", "https://example.invalid/a/2", content_sha256("beta".encode("utf-8"))
    )
    assert sorted(report.raw_ids) == sorted([expected_one, expected_two])

    archive = open_archive(store_root)
    try:
        for raw_id, url, body in (
            (expected_one, "https://example.invalid/a/1", "alpha"),
            (expected_two, "https://example.invalid/a/2", "beta"),
        ):
            record = archive.get(raw_id)
            assert record.channel_id == "chan-a"
            assert record.endpoint == url, "endpoint 必须是文章地址（source_url）"
            assert archive.get_content(raw_id) == body.encode("utf-8")
    finally:
        archive.close()


def test_criterion_1_endpoint_is_article_url_not_feed_url(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 1（语义差异）：导入记录的 `endpoint` 是**文章地址**；新采集的是 feed 地址。

    这条不是"测试实现"，而是把 §5 登记 #12 需要登记的**语义差异**钉住：
    `raw_records.endpoint` 这一列现在有两种含义，读它的代码不得假设是 feed 地址。
    """
    write_legacy(
        legacy_root,
        "chan-a",
        "one.json",
        legacy_record(source_url="https://example.invalid/article/x", raw_content="body"),
    )
    report = _run(legacy_root, store_root)
    archive = open_archive(store_root)
    try:
        record = archive.get(report.raw_ids[0])
        assert record.endpoint == "https://example.invalid/article/x"
        assert not record.endpoint.endswith(
            ("/feed", "/rss", "atom.xml")
        ), "导入的 endpoint 不该长得像 feed 地址"
    finally:
        archive.close()


def test_criterion_1_same_body_different_article_is_two_raws(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 1：正文相同但**文章不同**（URL 不同）→ 两条 Raw（去重不合并身份，§2.4）。"""
    write_legacy(
        legacy_root, "chan-a", "one.json", legacy_record(source_url="https://x.invalid/1", raw_content="same")
    )
    write_legacy(
        legacy_root, "chan-a", "two.json", legacy_record(source_url="https://x.invalid/2", raw_content="same")
    )
    report = _run(legacy_root, store_root)
    assert report.imported == 2
    assert report.distinct_raw_ids == 2
    assert report.ledger_problems() == []


def test_criterion_1_same_article_in_two_channel_dirs_is_two_raws(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 1：同一篇文章出现在两个频道目录 → 两条 Raw（`channel_id` 是指纹输入）。

    这也是 `raw_id` **逐频道**的直接后果：内容寻址的收敛只在同一频道内发生。
    """
    payload = legacy_record(source_url="https://x.invalid/9", raw_content="shared body")
    write_legacy(legacy_root, "chan-a", "one.json", payload)
    write_legacy(legacy_root, "chan-b", "one.json", payload)
    report = _run(legacy_root, store_root)
    assert report.imported == 2
    archive = open_archive(store_root)
    try:
        channels = {archive.get(raw_id).channel_id for raw_id in report.raw_ids}
        assert channels == {"chan-a", "chan-b"}
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 判据 2 / 3：只导入真正的文档；不静默跳过
# --------------------------------------------------------------------------- #
def test_criterion_2_non_documents_are_skipped_with_reason(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 2：`document_type` 缺失（`empty_*` / `summary_*`）→ 跳过并记账。"""
    write_legacy(
        legacy_root,
        "chan-a",
        "summary_20251221.json",
        {"source_name": "test-rss", "url": "https://example.invalid/feed", "items": []},
    )
    write_legacy(
        legacy_root,
        "chan-a",
        "empty_20251221.json",
        {"source_name": "test-rss", "url": "https://example.invalid/feed", "items": [], "count": 0},
    )
    write_legacy(
        legacy_root, "chan-a", "doc.json", legacy_record(source_url="https://x.invalid/1")
    )
    report = _run(legacy_root, store_root)

    assert report.scanned_files == 3
    assert report.skipped == 2 and report.imported == 1
    assert report.skip_counts() == {SKIP_NON_DOCUMENT: 2}
    assert all(entry.reason == SKIP_NON_DOCUMENT for entry in report.skips)
    assert all(entry.raw_id is None for entry in report.skips)
    assert report.ledger_problems() == []


def test_criterion_2_document_type_without_content_is_skipped_not_failed(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 2：有 `document_type` 但正文为空 → 跳过（空抓取），**不是失败**。"""
    write_legacy(
        legacy_root,
        "chan-a",
        "empty_body.json",
        legacy_record(raw_content="   ", source_url="https://x.invalid/1"),
    )
    write_legacy(legacy_root, "chan-a", "ok.json", legacy_record(raw_content="body"))
    report = _run(legacy_root, store_root)
    assert report.skip_counts() == {SKIP_EMPTY_CONTENT: 1}
    assert report.failed == 0
    assert report.imported == 1


def test_criterion_3_every_scanned_file_has_a_home(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 3：每个文件都有归宿，两本账都加得起来（含未知频道目录）。"""
    write_legacy(legacy_root, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))
    write_legacy(legacy_root, "chan-a", "b.json", legacy_record(source_url="https://x.invalid/1"))
    write_legacy(legacy_root, "chan-a", "summary.json", {"items": []})
    write_legacy(legacy_root, "unknown-channel", "c.json", legacy_record(source_url="https://x.invalid/2"))
    write_legacy(legacy_root, "indexes", "junk.json", {"whatever": True})
    report = _run(legacy_root, store_root)

    assert report.scanned_files == 5
    assert report.scanned_files == report.skipped + report.articles
    assert report.articles == report.imported + report.deduplicated + report.failed
    assert report.imported == 1 and report.deduplicated == 1
    assert report.skipped == 2  # summary.json + indexes/junk.json
    assert report.failed == 1  # unknown-channel 映射不到
    assert report.ledger_problems() == []
    assert len(report.entries) == 5
    assert {entry.channel for entry in report.entries} == {
        "chan-a",
        "unknown-channel",
        "indexes",
    }


def test_criterion_3_nested_directories_are_scanned_and_refused(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 3：`rglob` 覆盖子目录 —— 子目录里的文件也要**进账**。

    布局规则是写死的：**频道 = `data/raw/` 下的第一段目录名**，文件必须直接位于
    频道目录下。真实语料 534/534 都是这样（嵌套文件 0 个）。真出现嵌套就**记失败**
    而不是按子目录名编造一个新频道（那会让 `raw_id` 与行业归属双错）。
    """
    assert nested_relative_paths(legacy_root) == []  # 空目录：活对照的基线

    write_legacy(
        legacy_root,
        "chan-a",
        "deep.json",
        legacy_record(source_url="https://x.invalid/deep"),
        subdir="2025/12",
    )
    assert nested_relative_paths(legacy_root) == ["chan-a/2025/12/deep.json"]

    report = _run(legacy_root, store_root)
    assert report.scanned_files == 1, "嵌套文件不能凭空消失（不静默跳过）"
    assert report.failure_counts() == {REASON_NESTED_LAYOUT: 1}
    assert report.entries[0].channel == "chan-a", "频道仍取第一段目录名，不是子目录名"
    assert report.ledger_problems() == []

    # 活对照：同一篇文章直接放在频道目录下必须成功
    write_legacy(
        legacy_root, "chan-a", "flat.json", legacy_record(source_url="https://x.invalid/flat")
    )
    flat = _run(legacy_root, store_root)
    assert flat.imported == 1
    assert flat.failed == 1


# --------------------------------------------------------------------------- #
# 判据 4 / 16：幂等
# --------------------------------------------------------------------------- #
def test_criterion_4_idempotent_rerun_creates_no_new_rows(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 4：跑两次，`raw_records` 行数不变、第二次 `imported == 0`。"""
    for index in range(5):
        write_legacy(
            legacy_root,
            "chan-a",
            f"{index}.json",
            legacy_record(
                id=f"uuid-{index}",
                source_url="https://x.invalid/same",
                raw_content="the very same body",
            ),
        )
    options = MigrateOptions(
        legacy_root=legacy_root, archive_root=store_root, channel_map=CHANNEL_MAP
    )
    with Migrate(options) as migrator:
        first = migrator.run()
        assert migrator.archive.verify() == []
        rows_after_first = migrator.archive.records.count()
        second = migrator.run()
        rows_after_second = migrator.archive.records.count()
        assert migrator.archive.verify() == []

    assert first.imported == 1 and first.deduplicated == 4
    assert rows_after_first == 1
    assert second.imported == 0 and second.deduplicated == 5
    assert rows_after_second == rows_after_first == 1
    assert second.bytes_archived == 0
    assert second.ledger_problems() == []
    # 判据 16：**归档状态相同**的两次运行（第一次之后 vs 第二次之后）报告逐条相同。
    # 指纹含 status，所以"首次（新建）"与"重跑（幂等命中）"必然不同 —— 这本身证明
    # 指纹对归宿敏感（不是恒等函数）。
    assert first.digest() != second.digest(), "首次新建与重跑命中不应有相同指纹"
    third = _run(legacy_root, store_root)
    assert third.digest() == second.digest(), "同输入 + 同归档状态 ⇒ 报告指纹必须相同"
    assert third.imported == 0 and third.deduplicated == 5


def test_criterion_16_report_digest_changes_when_input_changes(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 16 的活对照：输入变了，指纹必须**变**（否则该断言什么也没验证）。"""
    write_legacy(legacy_root, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))
    first = _run(legacy_root, store_root)
    write_legacy(legacy_root, "chan-a", "b.json", legacy_record(source_url="https://x.invalid/2"))
    second = _run(legacy_root, store_root)
    assert second.digest() != first.digest()


# --------------------------------------------------------------------------- #
# 判据 6：data/raw 只读（合成根上先钉住机制）
# --------------------------------------------------------------------------- #
def test_criterion_6_legacy_tree_is_untouched(legacy_root: Path, store_root: Path) -> None:
    """判据 6：导入前后 `data/raw/` 整树 sha256 相同（含未导入的垃圾文件）。"""
    write_legacy(legacy_root, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))
    write_legacy(legacy_root, "indexes", "junk.json", {"x": 1})
    write_raw_bytes(legacy_root, "chan-a", "broken.json", b"\xff\xfe not json")
    before = tree_digest(legacy_root)
    files_before, bytes_before = directory_stats(legacy_root)

    _run(legacy_root, store_root)

    assert tree_digest(legacy_root) == before
    assert directory_stats(legacy_root) == (files_before, bytes_before)


# --------------------------------------------------------------------------- #
# 判据 7 / 17：fetched_at 来自旧记录，且不猜时间
# --------------------------------------------------------------------------- #
def test_criterion_7_fetched_at_comes_from_legacy_record_not_now(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 7：`fetched_at` == 旧记录的 `collected_at`（UTC），**不是"现在"**。"""
    write_legacy(
        legacy_root,
        "chan-a",
        "a.json",
        legacy_record(source_url="https://x.invalid/1", collected_at=COLLECTED_AT),
    )
    report = _run(legacy_root, store_root)
    archive = open_archive(store_root)
    try:
        record = archive.get(report.raw_ids[0])
    finally:
        archive.close()
    assert record.fetched_at == COLLECTED_DT
    assert record.fetched_at.tzinfo is not None, "必须是 aware（否则与库里的 aware 值不可比）"
    now = datetime.now(timezone.utc)
    assert now - record.fetched_at > timedelta(days=30), (
        "fetched_at 落在最近 30 天内，说明用了「现在」而不是旧记录的时间"
    )
    assert report.entries[0].moment_field == "collected_at"


def test_criterion_7_moment_field_priority_is_documented_order(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 7：`collected_at` → `created_at` → `stored_at` → `updated_at` 的优先级。

    活对照：**有** `collected_at` 时必须选它（上一条测试）；这里删掉它，验证回退。
    """
    assert MOMENT_FIELDS == ("collected_at", "created_at", "stored_at", "updated_at")

    # 活对照（正向）：四个都在 → 选 collected_at
    picked = pick_moment_field(legacy_record())
    assert picked is not None and picked[0] == "collected_at"

    # 回退链：逐个删掉首选，验证顺序
    variants = [
        ({"collected_at": None}, "created_at"),
        ({"collected_at": None, "created_at": None}, "stored_at"),
        ({"collected_at": None, "created_at": None, "stored_at": None}, "updated_at"),
    ]
    for overrides, expected in variants:
        picked = pick_moment_field(legacy_record(**overrides))
        assert picked is not None and picked[0] == expected, f"应回退到 {expected}"

    write_legacy(
        legacy_root,
        "chan-a",
        "a.json",
        legacy_record(source_url="https://x.invalid/1", collected_at=None),
    )
    report = _run(legacy_root, store_root)
    assert report.entries[0].moment_field == "created_at"
    assert report.entries[0].fetched_at == "2025-12-21T11:41:02.772099+00:00"


def test_criterion_17_missing_every_timestamp_is_a_recorded_failure(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 17：没有任何时间字段 → 记失败，**一个 Raw 都不写**（不猜、不用现在）。"""
    write_legacy(
        legacy_root,
        "chan-a",
        "a.json",
        legacy_record(
            source_url="https://x.invalid/1",
            collected_at=None,
            created_at=None,
            stored_at=None,
            updated_at=None,
        ),
    )
    write_legacy(
        legacy_root,
        "chan-a",
        "ok.json",
        legacy_record(source_url="https://x.invalid/2", collected_at="2025-01-02T03:04:05"),
    )
    report = _run(legacy_root, store_root)

    assert report.failed == 1, "时间全缺必须是失败"
    assert report.failure_counts() == {REASON_MISSING_TIMESTAMP: 1}
    assert report.imported == 1, "活对照：同一批里时间正常的文档仍然导入成功"
    assert report.distinct_raw_ids == 1
    assert report.ledger_problems() == []


def test_criterion_17_unparseable_timestamp_is_a_recorded_failure(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 17：时间解析不了 → 记失败（原因码 `unparseable_timestamp`），不编造。"""
    write_legacy(
        legacy_root,
        "chan-a",
        "bad.json",
        legacy_record(source_url="https://x.invalid/1", collected_at="21/12/2025 11:41"),
    )
    write_legacy(
        legacy_root,
        "chan-a",
        "good.json",
        legacy_record(source_url="https://x.invalid/2", collected_at="2025-12-21T11:41:02"),
    )
    report = _run(legacy_root, store_root)
    assert report.failure_counts() == {REASON_UNPARSEABLE_TIMESTAMP: 1}
    assert report.imported == 1
    failure = report.failures[0]
    assert failure.reason == REASON_UNPARSEABLE_TIMESTAMP
    assert "21/12/2025" in failure.message


def test_criterion_7_aware_offsets_are_normalised_to_utc(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 7：带偏移的时间戳归一化成 UTC；`Z` 结尾也认。"""
    write_legacy(
        legacy_root,
        "chan-a",
        "a.json",
        legacy_record(source_url="https://x.invalid/1", collected_at="2025-12-21T19:41:02+08:00"),
    )
    write_legacy(
        legacy_root,
        "chan-b",
        "b.json",
        legacy_record(source_url="https://x.invalid/2", collected_at="2025-12-21T11:41:02Z"),
    )
    report = _run(legacy_root, store_root)
    archive = open_archive(store_root)
    try:
        by_channel = {
            archive.get(raw_id).channel_id: archive.get(raw_id) for raw_id in report.raw_ids
        }
    finally:
        archive.close()
    # `19:41:02+08:00` 与 `11:41:02Z` 是**同一个时刻**，归一化后必须相等
    assert by_channel["chan-a"].fetched_at == by_channel["chan-b"].fetched_at
    assert by_channel["chan-a"].fetched_at == datetime(
        2025, 12, 21, 11, 41, 2, tzinfo=timezone.utc
    )
    # 活对照：微秒精度的无时区串保真到微秒（没有被截断或补零）
    write_legacy(
        legacy_root,
        "chan-a",
        "c.json",
        legacy_record(source_url="https://x.invalid/3", collected_at=COLLECTED_AT),
    )
    more = _run(legacy_root, store_root)
    precision = [
        entry.fetched_at for entry in more.entries if entry.relative.endswith("chan-a/c.json")
    ]
    assert precision == [COLLECTED_AT + "+00:00"]


# --------------------------------------------------------------------------- #
# 判据 10：失败响亮（可记账 + 不吞接线错误）
# --------------------------------------------------------------------------- #
def test_criterion_10_broken_json_is_a_recorded_failure(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 10：JSON 坏 → 记失败（`unparseable_json`），不静默跳过。"""
    legacy_root.joinpath("chan-a").mkdir(parents=True, exist_ok=True)
    legacy_root.joinpath("chan-a", "broken.json").write_text("{not json", encoding="utf-8")
    write_legacy(legacy_root, "chan-a", "ok.json", legacy_record(source_url="https://x.invalid/1"))
    report = _run(legacy_root, store_root)
    assert report.failure_counts() == {REASON_UNPARSEABLE_JSON: 1}
    assert report.imported == 1
    assert report.ledger_problems() == []


def test_criterion_10_non_object_json_is_a_recorded_failure(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 10：顶层是数组 → 记失败（`not_an_object`）。"""
    legacy_root.joinpath("chan-a").mkdir(parents=True, exist_ok=True)
    legacy_root.joinpath("chan-a", "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    write_legacy(legacy_root, "chan-a", "ok.json", legacy_record(source_url="https://x.invalid/1"))
    report = _run(legacy_root, store_root)
    assert report.failure_counts() == {REASON_NOT_AN_OBJECT: 1}
    assert report.imported == 1


def test_criterion_10_undecodable_bytes_is_a_recorded_failure(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 10：字节不是合法 UTF-8 → 记失败（`unreadable_bytes`），不静默跳过。"""
    write_raw_bytes(legacy_root, "chan-a", "broken.json", b'{"raw_content": "\xff\xfe"}')
    write_legacy(legacy_root, "chan-a", "ok.json", legacy_record(source_url="https://x.invalid/1"))
    report = _run(legacy_root, store_root)
    assert report.failure_counts() == {REASON_UNREADABLE_BYTES: 1}
    assert report.imported == 1


def test_criterion_10_missing_source_url_is_a_recorded_failure(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 10：没有 `source_url` 就没有逐篇身份 → 记失败，**不用文件路径兜底**。"""
    write_legacy(
        legacy_root, "chan-a", "no-url.json", legacy_record(source_url=None, raw_content="body")
    )
    write_legacy(legacy_root, "chan-a", "ok.json", legacy_record(source_url="https://x.invalid/1"))
    report = _run(legacy_root, store_root)
    assert report.failed == 1
    assert report.imported == 1
    assert "source_url" in report.failures[0].message


def test_criterion_10_strict_mode_raises_with_the_ledger(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 10：`strict=True` 直接抛，异常文本里带对账表（不留下"半数导入"的错觉）。"""
    write_legacy(legacy_root, "unknown", "a.json", legacy_record(source_url="https://x.invalid/1"))
    from atlas.migrate import MigrateError

    with pytest.raises(MigrateError) as info:
        _run(legacy_root, store_root, strict=True)
    message = str(info.value)
    assert "strict" in message
    assert "文件账" in message and "导入账" in message, "异常里必须能看出对账结果"


def test_criterion_10_wiring_errors_are_not_swallowed(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 10（硬规则 2）：解析器返回未知状态是**接线错误** → 直接冒泡，不被吞成失败。

    活对照：同一调用路径在合法映射下必须成功（否则"抛异常"什么也没证明）。
    """
    from atlas.migrate import MigrateError

    write_legacy(legacy_root, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))

    good = _run(legacy_root, store_root)
    assert good.imported == 1

    def broken_resolver(_channel: str):
        return ("chan-a",), "totally-unknown-status"

    with pytest.raises(MigrateError) as info:
        _run(legacy_root, store_root, channel_map=broken_resolver)
    assert "未知的映射状态" in str(info.value)


# --------------------------------------------------------------------------- #
# 判据 11：行业归属保住（channel_id 必须真实存在于注册表）
# --------------------------------------------------------------------------- #
def test_criterion_11_imported_channel_is_a_registered_channel(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 11：导入记录的 `channel_id` 必须**精确**等于注册表里的渠道 id。"""
    registered = {channel.id for channel in REGISTRY}
    write_legacy(legacy_root, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))
    write_legacy(legacy_root, "chan-b", "b.json", legacy_record(source_url="https://x.invalid/2"))
    report = _run(legacy_root, store_root)
    archive = open_archive(store_root)
    try:
        channels = {archive.get(raw_id).channel_id for raw_id in report.raw_ids}
    finally:
        archive.close()
    assert channels == registered
    assert report.per_registry_channel() == {"chan-a": 1, "chan-b": 1}


def test_criterion_11_unmapped_channel_is_never_guessed(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 11：映射不到的频道 → 失败，**绝不**编造 channel_id。

    活对照：同一批里映射得到的频道照常导入。
    """
    write_legacy(legacy_root, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))
    write_legacy(legacy_root, "chan-z", "z.json", legacy_record(source_url="https://x.invalid/2"))
    report = _run(legacy_root, store_root)
    assert report.failed == 1 and report.imported == 1
    assert report.per_registry_channel() == {"chan-a": 1}
    archive = open_archive(store_root)
    try:
        assert all(archive.get(raw_id).channel_id != "chan-z" for raw_id in report.raw_ids)
    finally:
        archive.close()


def test_criterion_11_default_channel_map_fails_loudly(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 11：默认（不注入）映射 **一律失败** —— 忘记接线必须响亮可见。

    两种默认形态都要验：`resolve_nothing`（记进对账表）与 `empty_mapper`（直接抛）。
    """
    write_legacy(legacy_root, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))

    # 默认值（不传 channel_map）→ 每条记录都失败，账仍然平
    report = _run(legacy_root, store_root, channel_map=resolve_nothing)
    assert report.imported == 0 and report.failed == 1
    assert report.ledger_problems() == []

    default_report = MigrateOptions(legacy_root=legacy_root, archive_root=store_root)
    assert default_report.channel_map is resolve_nothing, "默认必须是「一律未映射」"

    # 显式调用 mapper 形态 → 响亮抛（附对账表之外的直接原因）
    with pytest.raises(ChannelMappingError) as info:
        empty_mapper("chan-a")
    assert "没有注入渠道映射" in str(info.value)


def test_criterion_11_ambiguous_mapping_is_refused_not_guessed(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 11：一个旧频道名对应两个注册表渠道 → 失败，**不任选**。"""
    write_legacy(legacy_root, "dup", "a.json", legacy_record(source_url="https://x.invalid/1"))
    ambiguous = channel_map_resolver({"dup": ["chan-a", "chan-b"]})
    report = _run(legacy_root, store_root, channel_map=ambiguous)
    assert report.imported == 0 and report.failed == 1
    assert "多个注册表渠道" in report.failures[0].message
    # 活对照：把候选收敛成一个，同一路径必须成功
    unambiguous = channel_map_resolver({"dup": ["chan-a"]})
    ok = _run(legacy_root, store_root, channel_map=unambiguous)
    assert ok.imported == 1


# --------------------------------------------------------------------------- #
# 判据 12：字节即事实
# --------------------------------------------------------------------------- #
def test_criterion_12_disk_bytes_match_legacy_raw_content(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 12：`content.bin` 的 sha256 == `raw_records.content_sha256` == 旧正文指纹。"""
    body = "中文与 English 混排的正文 —— 实体 &amp; 与省略号…"
    write_legacy(
        legacy_root,
        "chan-a",
        "a.json",
        legacy_record(source_url="https://x.invalid/1", raw_content=body),
    )
    report = _run(legacy_root, store_root)
    archive = open_archive(store_root)
    try:
        raw_id = report.raw_ids[0]
        record = archive.get(raw_id)
        expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert record.content_sha256 == expected
        assert report.entries[0].content_sha256 == expected
        on_disk = hashlib.sha256(archive.blobs.content_path(raw_id).read_bytes()).hexdigest()
        assert on_disk == expected, "磁盘字节必须与元数据指纹一致"
        meta = json.loads(archive.blobs.meta_path(raw_id).read_text(encoding="utf-8"))
        assert meta["content_sha256"] == expected
        assert meta["endpoint"] == "https://x.invalid/1"
        assert meta["fetched_at"] == COLLECTED_AT + "+00:00"
        assert record.byte_length == len(body.encode("utf-8"))
    finally:
        archive.close()


def test_criterion_12_ledger_bytes_equal_archived_bytes(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 12：对账表的正文字节数 == 归档目录里的实际字节数。"""
    for index in range(4):
        write_legacy(
            legacy_root,
            "chan-a",
            f"{index}.json",
            legacy_record(source_url=f"https://x.invalid/{index}", raw_content="x" * (10 + index)),
        )
    report = _run(legacy_root, store_root)
    files, total = directory_stats(store_root / "raw")
    # 每个记录目录有 content.bin + meta.json
    assert len(record_dirs(store_root / "raw")) == report.imported == 4
    archived_bytes = sum(
        (store_root / "raw" / raw_id / "content.bin").stat().st_size for raw_id in report.raw_ids
    )
    assert archived_bytes == report.bytes_archived == 10 + 11 + 12 + 13
    assert files == 8 and total > archived_bytes  # meta.json 的字节也在盘上


# --------------------------------------------------------------------------- #
# 判据 18：raw_id 不含旧 uuid
# --------------------------------------------------------------------------- #
def test_criterion_18_legacy_uuid_is_not_part_of_the_fingerprint(
    legacy_root: Path, store_root: Path
) -> None:
    """判据 18：同一篇文章的 10 次采集（旧 uuid 各不同）必须收敛到 1 条 —— 因此
    `raw_id` 不能含旧 uuid。"""
    for index in range(10):
        write_legacy(
            legacy_root,
            "chan-a",
            f"{index}.json",
            legacy_record(
                id=f"uuid-{index}",
                source_url="https://x.invalid/same",
                raw_content="identical body",
            ),
        )
    report = _run(legacy_root, store_root)
    assert report.imported == 1 and report.deduplicated == 9
    assert report.distinct_raw_ids == 1
    legacy_ids = {entry.legacy_id for entry in report.entries}
    assert len(legacy_ids) == 10, "旧 uuid 仍逐条保留在报告里（可溯源）"
    raw_id = report.raw_ids[0]
    assert not any(uuid in raw_id for uuid in legacy_ids)


# --------------------------------------------------------------------------- #
# 判据 8：零新增依赖
# --------------------------------------------------------------------------- #
def test_criterion_8_no_new_dependencies() -> None:
    """判据 8：`atlas.migrate` 只 import 标准库 + `atlas.*`（不新增任何第三方依赖）。"""
    import ast
    import sys

    package = Path(__file__).resolve().parents[1] / "src" / "atlas" / "migrate"
    allowed_third_party: set[str] = set()
    stdlib = set(sys.stdlib_module_names)
    imported: set[str] = set()
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    external = {
        name
        for name in imported
        if name not in stdlib and name != "atlas" and name not in allowed_third_party
    }
    assert external == set(), f"atlas.migrate 引入了非标准库依赖：{sorted(external)}"


def test_criterion_8_migrate_does_not_import_registry_or_labels() -> None:
    """判据 8（§4.0 跨包规则）：`atlas.migrate` 不 import `atlas.registry` / `atlas.labels`。

    行业归属是**注入**的（`channel_map`），标签根本不许迁移（§2.4）。
    """
    import ast

    package = Path(__file__).resolve().parents[1] / "src" / "atlas" / "migrate"
    forbidden = {"atlas.registry", "atlas.labels", "atlas.feed", "atlas.search", "atlas.normalize"}
    found: List[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names if alias.name in forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module in forbidden:
                found.append(node.module)
    assert found == [], f"atlas.migrate 出现了禁止的跨包 import：{found}"


# --------------------------------------------------------------------------- #
# 分类函数本身（不经过文件系统）
# --------------------------------------------------------------------------- #
def test_classify_is_the_single_decision_point() -> None:
    """`classify_legacy` 是"是不是文档"的唯一判定点（活对照 + 否定）。"""
    assert classify_legacy(legacy_record()) is None
    assert classify_legacy(legacy_record(document_type="html")) is None
    assert classify_legacy(legacy_record(document_type=None)) == SKIP_NON_DOCUMENT
    assert classify_legacy(legacy_record(document_type="")) == SKIP_NON_DOCUMENT
    assert classify_legacy(legacy_record(raw_content="")) == SKIP_EMPTY_CONTENT
    assert classify_legacy(legacy_record(raw_content=None)) == SKIP_EMPTY_CONTENT


def test_migrate_legacy_convenience_entry_point_is_idempotent(store_root: Path, tmp_path: Path) -> None:
    """便捷入口 `migrate_legacy(...)` 与 `Migrate` 同语义（第二次 imported == 0）。"""
    legacy = tmp_path / "legacy"
    write_legacy(legacy, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))
    first = migrate_legacy(
        legacy_root=legacy, archive_root=store_root, channel_map=CHANNEL_MAP
    )
    second = migrate_legacy(
        legacy_root=legacy, archive_root=store_root, channel_map=CHANNEL_MAP
    )
    assert first.imported == 1 and first.records_after == 1
    assert second.imported == 0 and second.deduplicated == 1
    assert second.records_before == second.records_after == 1
    assert second.ledger_problems() == []


def test_empty_legacy_root_is_an_empty_ledger_not_an_error(store_root: Path, tmp_path: Path) -> None:
    """空目录（或不存在）不是错误：0 文件 → 0 归宿，账仍然平。"""
    report = migrate_legacy(
        legacy_root=tmp_path / "does-not-exist", archive_root=store_root, channel_map=CHANNEL_MAP
    )
    assert report.scanned_files == 0
    assert report.entries == []
    assert report.ledger_problems() == []
    assert (store_root / "raw").exists() is False or record_dirs(store_root / "raw") == []
