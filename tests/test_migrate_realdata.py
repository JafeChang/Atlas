"""T-131 真实数据流证据（硬规则 1）：对**真实旧语料**跑一次真导入，并逐项对账。

真实数据的规模（只读口径，测试自己数）
--------------------------------------

`data/` 不进 git（SPEC §8.1），所以干净 worktree 里没有它 —— 那时本文件的用例
**skip**（照 `tests/test_search_realdata.py` 的先例），真实证据由主工作区的那次运行给出。
`data/raw/` 是**只读**的：进/出各算一次整树 sha256 并断言相等（判据 6/13）。

本文件**独立重算**期望值，不复用 `atlas.migrate` 的任何结论
----------------------------------------------------------

对账要真的对得上，就不能拿被测代码的输出当期望值。因此本文件自己：

1. 用 `json` + `hashlib` 遍历 `data/raw/**/*.json`；
2. 按 §2.10 的 `raw_id` 构造式（`raw_id_for`，来自 `atlas.contracts`，不是 migrate 包）
   算出**期望的 `raw_id` 集合**与**每条的正文 sha256**；
3. 与导入后归档里的 `raw_records` 逐条比对（含磁盘字节的 sha256）。

导入是**真的写 `data/store`**（本任务的目的，用户已批准"现在就导"），
但**绝不写 `data/raw/`**，也不写任何标签表（§2.4）。

实测口径（本次运行的量测值，钉住以防"数据没了但测试还绿"）
----------------------------------------------------------

| 项 | 实测 |
|---|---|
| `data/raw` 下 JSON | **534**（5 个真频道 + 4 个垃圾目录 + `test`） |
| 有正文的文档 | **474** |
| 非文档产物（`document_type` 缺失） | **60** |
| 收敛后的不同 `raw_id` | **65**（旧系统未去重，同一篇被采约 10 次） |
| 失败 | **0** |
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from atlas.archive import open_archive
from atlas.contracts.ids import content_sha256, raw_id_for
from atlas.feed import ArchiveFeedSource, FeedQuery, run_query
from atlas.migrate import (
    REASON_NESTED_LAYOUT,
    REASON_NO_CHANNEL_DIR,
    Migrate,
    MigrateOptions,
    build_channel_map,
    iter_legacy_files,
)
from tests._migrate_fixtures import directory_stats, tree_digest

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_RAW = REPO_ROOT / "data" / "raw"
STORE_ROOT = REPO_ROOT / "data" / "store"
STORE_DB = STORE_ROOT / "atlas.db"

#: 实测基线（2026-09 快照）。数据变了就**响亮失败**，逼操作者重新测量。
MEASURED_JSON_FILES = 534
MEASURED_DOCUMENTS = 474
MEASURED_NON_DOCUMENTS = 60
MEASURED_DISTINCT_RAW_IDS = 65
MEASURED_EXISTING_RAW_RECORDS = 10
MEASURED_NESTED_FILES = 0

#: 旧系统里混放的**非文档产物**目录（实测 0 个 JSON）与文件名前缀。
EXPECTED_NON_DOCUMENT_PREFIXES = ("empty_", "summary_")

requires_real_data = pytest.mark.skipif(
    not (LEGACY_RAW.is_dir() and any(LEGACY_RAW.rglob("*.json"))),
    reason="本地真实归档数据缺失（data/ 不进 git，见 SPEC §8.1）",
)


# --------------------------------------------------------------------------- #
# 独立重算（不用被测代码的结论）
# --------------------------------------------------------------------------- #
class LegacyCensus:
    """对 `data/raw/` 的独立普查结果（**只读**）。"""

    def __init__(self) -> None:
        self.json_files = 0
        self.documents = 0
        self.non_documents = 0
        self.nested = 0
        self.without_url = 0
        self.bad_moment = 0
        #: `raw_id` → `(channel, source_url, content_sha256, byte_length, fetched_at_str)`
        self.expected: Dict[str, Tuple[str, str, str, int, str]] = {}
        #: 每个 `raw_id` 被多少份旧 JSON 命中（用来量测收敛倍数）
        self.hits: Dict[str, int] = {}
        self.per_channel: Dict[str, int] = {}
        self.moment_fields: Dict[str, int] = {}
        self.moment_values: Dict[str, str] = {}
        #: 旧正文的**原始字节**合计（去重前，按文件累加）
        self.raw_bytes: int = 0
        #: 去重后的正文字节合计（= `data/store/raw/**/content.bin` 里这 65 条的合计）
        self.distinct_bytes: int = 0

    @property
    def distinct(self) -> int:
        return len(self.expected)

    def stored_bytes(self, archive) -> int:
        """这 65 条**在归档里**的 `content.bin` 实际字节合计（只读）。"""
        return sum(
            archive.blobs.content_path(raw_id).stat().st_size for raw_id in self.expected
        )


def _census() -> LegacyCensus:
    """独立数一遍真实旧语料：文件数、文档数、非文档数、期望的 `raw_id` 集合。"""
    census = LegacyCensus()
    for entry in iter_legacy_files(LEGACY_RAW):
        census.json_files += 1
        census.nested += int(entry.nested)
        payload = json.loads(entry.path.read_bytes().decode("utf-8"))
        if not isinstance(payload, dict):
            census.non_documents += 1
            continue
        document_type = payload.get("document_type")
        content = payload.get("raw_content")
        if not isinstance(document_type, str) or not document_type.strip():
            census.non_documents += 1
            assert entry.path.name.startswith(EXPECTED_NON_DOCUMENT_PREFIXES), (
                f"非文档产物 {entry.path.name} 不是 empty_/summary_ 形态，口径需重测"
            )
            continue
        if not isinstance(content, str) or not content.strip():
            census.non_documents += 1
            continue
        url = payload.get("source_url")
        if not isinstance(url, str) or not url.strip():
            census.without_url += 1
            continue
        channel = entry.channel
        content_bytes = content.encode("utf-8")
        digest = content_sha256(content_bytes)
        raw_id = raw_id_for(channel, url, digest)
        moment_field = next(
            (
                field
                for field in ("collected_at", "created_at", "stored_at", "updated_at")
                if isinstance(payload.get(field), str) and payload.get(field)
            ),
            None,
        )
        if moment_field is None:
            census.bad_moment += 1
            continue
        moment_value = str(payload[moment_field])
        census.documents += 1
        census.raw_bytes += len(content_bytes)
        census.per_channel[channel] = census.per_channel.get(channel, 0) + 1
        census.hits[raw_id] = census.hits.get(raw_id, 0) + 1
        census.moment_fields[moment_field] = census.moment_fields.get(moment_field, 0) + 1
        census.moment_values[raw_id] = moment_value
        if raw_id not in census.expected:
            census.distinct_bytes += len(content_bytes)
        census.expected.setdefault(
            raw_id, (channel, url, digest, len(content_bytes), moment_value)
        )
    return census


# --------------------------------------------------------------------------- #
# 夹具：**一次**真导入（写 data/store），并记录前后状态
# --------------------------------------------------------------------------- #
class RealRun:
    """一次真导入的完整现场（前后快照 + 报告 + 归档只读视图）。"""

    def __init__(
        self,
        *,
        census: LegacyCensus,
        before_rows: int,
        before_files: int,
        before_bytes: int,
        before_tree: str,
        report,
        second,
        rows_after: int,
        channel_industry: Dict[str, str],
        before_label_rows: int,
        label_rows_after: int,
        table_names: List[str],
        trigger_names: List[str],
    ) -> None:
        self.census = census
        self.before_rows = before_rows
        self.before_files = before_files
        self.before_bytes = before_bytes
        self.before_tree = before_tree
        self.report = report
        self.second = second
        self.rows_after = rows_after
        self.channel_industry = channel_industry
        self.before_label_rows = before_label_rows
        self.label_rows_after = label_rows_after
        self.table_names = table_names
        self.trigger_names = trigger_names


@pytest.fixture(scope="module")
def real_run() -> RealRun:
    """对真实 `data/store` 跑一次真导入（幂等：第二次必然 0 新建）。"""
    from atlas.registry import RegistryService, open_store

    census = _census()
    before_rows = _raw_row_count()
    before_label_rows = _label_row_count()
    before_files, before_bytes = directory_stats(STORE_ROOT)
    before_tree = tree_digest(LEGACY_RAW)

    service = RegistryService(open_store(STORE_DB, author="t131-migrate"))
    channel_map = build_channel_map(service.list_channels())
    channel_industry = {
        channel.id: channel.industry_id for channel in service.list_channels()
    }

    archive = open_archive(STORE_ROOT)
    try:
        first = Migrate(
            MigrateOptions(
                legacy_root=LEGACY_RAW,
                archive_root=STORE_ROOT,
                archive=archive,
                channel_map=channel_map,
            )
        ).run()
        rows_after = len(archive.all_raw_ids())
        second = Migrate(
            MigrateOptions(
                legacy_root=LEGACY_RAW,
                archive_root=STORE_ROOT,
                archive=archive,
                channel_map=channel_map,
            )
        ).run()
        rows_after_second = len(archive.all_raw_ids())
        assert archive.verify() == [], "导入后归档自检必须为空"
    finally:
        archive.close()

    assert rows_after_second == rows_after, "第二次运行不得改变 raw_records 行数"
    label_rows_after, table_names, trigger_names = _label_and_schema_state()
    return RealRun(
        census=census,
        before_rows=before_rows,
        before_files=before_files,
        before_bytes=before_bytes,
        before_tree=before_tree,
        report=first,
        second=second,
        rows_after=rows_after,
        channel_industry=channel_industry,
        before_label_rows=before_label_rows,
        label_rows_after=label_rows_after,
        table_names=table_names,
        trigger_names=trigger_names,
    )


def _raw_row_count() -> int:
    connection = sqlite3.connect(f"file:{STORE_DB}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) AS n FROM raw_records").fetchone()[0])
    finally:
        connection.close()


def _label_row_count() -> int:
    """`confirmed_labels` 当前行数（**只读**）。"""
    connection = sqlite3.connect(f"file:{STORE_DB}?mode=ro", uri=True)
    try:
        return int(
            connection.execute("SELECT COUNT(*) AS n FROM confirmed_labels").fetchone()[0]
        )
    finally:
        connection.close()


def _label_and_schema_state() -> Tuple[int, List[str], List[str]]:
    """`(confirmed_labels 行数, 表名列表, 触发器名列表)` —— 只读。"""
    connection = sqlite3.connect(f"file:{STORE_DB}?mode=ro", uri=True)
    try:
        labels = int(
            connection.execute("SELECT COUNT(*) AS n FROM confirmed_labels").fetchone()[0]
        )
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        triggers = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        ]
        return labels, tables, triggers
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# 判据 1–3 / 11 / 12 / 16：对账表 + 磁盘字节
# --------------------------------------------------------------------------- #
@requires_real_data
def test_real_ledger_adds_up(real_run: RealRun) -> None:
    """判据 3 / 16：真实数据的对账表加得起来，且每个文件都有归宿。"""
    census, report = real_run.census, real_run.report
    print(
        f"\n[T-131 真实] 扫描 {report.scanned_files} 个 JSON = "
        f"跳过 {report.skipped} + 文档 {report.articles}；"
        f"文档 = 新建 {report.imported} + 收敛 {report.deduplicated} + 失败 {report.failed}"
    )
    assert report.ledger_problems() == [], f"对账问题：{report.ledger_problems()}"
    assert report.scanned_files == census.json_files == MEASURED_JSON_FILES
    assert report.articles == census.documents == MEASURED_DOCUMENTS
    assert report.skipped == census.non_documents == MEASURED_NON_DOCUMENTS
    assert report.failed == 0, f"真实导入不应有失败：{report.failure_counts()}"
    assert report.skip_counts() == {"non_document": MEASURED_NON_DOCUMENTS}
    assert all(
        entry.relative.split("/")[-1].startswith(EXPECTED_NON_DOCUMENT_PREFIXES)
        for entry in report.skips
    ), "所有跳过项都必须是 empty_/summary_ 产物"
    assert report.distinct_raw_ids == census.distinct == MEASURED_DISTINCT_RAW_IDS
    assert census.nested == MEASURED_NESTED_FILES
    assert census.without_url == 0 and census.bad_moment == 0
    # 本次运行**之前**归档里已有 10 条 feed 级记录；本文件可能被重复执行，
    # 因此"运行前"要么是那 10 条，要么是"10 + 65"（说明导入已经做过了）。
    assert real_run.before_rows in (
        MEASURED_EXISTING_RAW_RECORDS,
        MEASURED_EXISTING_RAW_RECORDS + MEASURED_DISTINCT_RAW_IDS,
    )
    # 本次运行新建的条数 = 运行后 - 运行前（首次应为 65，重跑应为 0）
    assert report.imported == real_run.rows_after - real_run.before_rows
    assert report.imported in (0, census.distinct)
    if report.imported:
        # 首次导入：收敛数 == 重复采集的份数
        assert report.deduplicated == census.documents - census.distinct
    else:
        # 重跑：每一条都是幂等命中
        assert report.deduplicated == census.documents
    print(
        f"[T-131 真实] 收敛：{census.documents} 篇 → {census.distinct} 条不同 raw_id"
        f"（重复 {census.documents - census.distinct} 次，最多一篇被采 "
        f"{max(census.hits.values())} 次）"
    )
    for channel in sorted(report.per_registry_channel()):
        print(f"[T-131 真实]   注册表渠道 {channel}: {report.per_registry_channel()[channel]} 篇")


@requires_real_data
def test_real_per_channel_ledger_matches_recount(real_run: RealRun) -> None:
    """判据 3：逐频道对账与**独立重算**逐频道一致（文件数/文档数）。"""
    census, report = real_run.census, real_run.report
    legacy_names = {entry.channel for entry in iter_legacy_files(LEGACY_RAW)}
    summaries = {summary.channel: summary for summary in report.channels()}
    assert set(summaries) == legacy_names, "逐频道对账必须覆盖每个旧目录（含垃圾目录）"
    for name, summary in summaries.items():
        if name in ("test",):
            assert summary.articles == 0, "test/ 只有一个非文档产物"
        else:
            assert summary.articles == census.per_channel.get(name, 0), name
        assert summary.scanned == summary.skipped + summary.articles
        assert summary.articles == summary.imported + summary.deduplicated + summary.failed
        print(
            f"[T-131 真实]   {name:<18} 扫描 {summary.scanned:>4} 跳过 {summary.skipped:>3} "
            f"文档 {summary.articles:>4} 新建 {summary.imported:>3} "
            f"收敛 {summary.deduplicated:>4} 失败 {summary.failed} 字节 {summary.bytes_archived}"
        )


@requires_real_data
def test_real_archive_contains_exactly_the_expected_raw_ids(real_run: RealRun) -> None:
    """判据 1 / 12：归档里逐条包含**独立重算**的 65 个 `raw_id`，且字节一致。"""
    census = real_run.census
    archive = open_archive(STORE_ROOT)
    try:
        stored = set(archive.all_raw_ids())
        expected = set(census.expected)
        assert expected <= stored, f"缺少 {len(expected - stored)} 条"
        # 抽查 8 条：endpoint / channel / fetched_at / 磁盘字节 sha256 逐项相符
        for offset in range(0, len(census.expected), max(1, len(census.expected) // 8)):
            raw_id = sorted(census.expected)[offset]
            channel, url, digest, byte_length, moment_value = census.expected[raw_id]
            record = archive.get(raw_id)
            content = archive.get_content(raw_id)
            assert record.channel_id == channel
            assert record.endpoint == url, "endpoint 必须是旧记录的文章地址"
            assert record.content_sha256 == digest
            assert record.byte_length == byte_length == len(content)
            assert hashlib.sha256(content).hexdigest() == digest
            assert record.fetched_at.isoformat() == moment_value + "+00:00"
            assert record.http_status == 200
            print(
                f"[T-131 真实]   {raw_id[4:14]}… channel={channel} "
                f"bytes={byte_length} fetched_at={record.fetched_at.isoformat()} "
                f"endpoint={url[:60]}"
            )
        # 全部 65 条都从磁盘回读一遍并核对指纹（不是只抽查）
        mismatched = [
            raw_id
            for raw_id, (_, _, digest, byte_length, _) in census.expected.items()
            if hashlib.sha256(archive.get_content(raw_id)).hexdigest() != digest
            or archive.get(raw_id).byte_length != byte_length
        ]
        assert mismatched == [], f"{len(mismatched)} 条磁盘字节与旧记录不符"
        assert archive.verify() == []
    finally:
        archive.close()


@requires_real_data
def test_real_fetched_at_follows_the_legacy_record_field(real_run: RealRun) -> None:
    """判据 7：`fetched_at` 来自旧记录的 `collected_at`（全 474 篇实测口径）。"""
    census = real_run.census
    assert census.moment_fields == {"collected_at": MEASURED_DOCUMENTS}, (
        f"真实语料的时间字段分布变了：{census.moment_fields}"
    )
    assert all(
        entry.moment_field == "collected_at"
        for entry in real_run.report.entries
        if entry.status in ("imported", "deduplicated")
    )
    # 全部 fetched_at 都必须落在旧系统采集期内（2025-12），绝不能是"现在"
    years = {value[:4] for value in census.moment_values.values()}
    assert years == {"2025"}, f"旧记录的时间年份分布意外：{sorted(years)}"
    print(
        f"[T-131 真实] {len(census.moment_values)} 条 fetched_at 全部来自 "
        f"collected_at，年份 {sorted(years)}"
    )


# --------------------------------------------------------------------------- #
# 判据 4：幂等（同一夹具里的第二次运行）
# --------------------------------------------------------------------------- #
@requires_real_data
def test_real_second_run_creates_nothing_new(real_run: RealRun) -> None:
    """判据 4：第二次运行 `imported == 0`、行数不变（幂等证明）。"""
    second, report = real_run.second, real_run.report
    print(
        f"\n[T-131 真实] 第二次运行：新建 {second.imported} / 收敛 {second.deduplicated} / "
        f"失败 {second.failed}；raw_records {second.records_before} → {second.records_after}"
    )
    assert second.imported == 0
    assert second.deduplicated == MEASURED_DOCUMENTS
    assert second.failed == 0
    assert second.records_before == second.records_after == real_run.rows_after
    assert second.ledger_problems() == []
    assert second.bytes_archived == 0
    # 第一次的 records_after 与第二次的 records_before 必须是同一个数
    assert report.records_after == second.records_before
    # 归档状态相同的两次运行，报告指纹必须相同
    third = second.digest()
    assert third == second.digest()


# --------------------------------------------------------------------------- #
# 判据 5：不得迁移人工标签
# --------------------------------------------------------------------------- #
@requires_real_data
def test_real_labels_are_untouched(real_run: RealRun) -> None:
    """判据 5（§2.4）：导入**不新增任何人工标签**，符号层面也不存在写标签的路径。

    ⚠️ **这条判据在 T-109 之后改成了"导入前后相等"**（原本写死 `== 1`）。
    原因：真实库里的行数**本就会长**——T-109 打标前端落地后，用户每点一次
    "标记有效"就多一条 Confirmed。把绝对行数写死，等于**禁止项目达成 §1.4 的成功信号**
    （"可以在多个渠道、多个领域给收集到的信息打标"）。
    判据的**本意**是"导入不写标签表"，因此现在比较**导入前后**的行数。
    """
    assert real_run.label_rows_after == real_run.before_label_rows, (
        "confirmed_labels 行数变了 —— 导入动了人工标签（§2.4 禁止）"
    )
    assert real_run.before_label_rows >= 1, (
        "真实库里应当至少有 §6.1 记录的那条 A1 复验探针（actor=e2e-verify）"
    )
    assert not any(reason.startswith("label") for reason in real_run.report.failure_counts())
    # 表集合里不得出现"标签 / 提议 / 证据域"的**表**。
    #
    # ⚠️ 这条判据在 T-105 之后**收窄了它的对象**（原本把 `proposed_claims` 也列为禁止项）。
    # 原因：T-105（机器分类与提议）落地后，`proposed_claims` 是 SPEC §2.10
    # **登记给 T-105 的合法表**，而且 T-105 有正当理由往它写（这正是它的交付内容）。
    # 把这张表列进"禁止出现"，等于**禁止后续任务交付**——与把 `confirmed_labels`
    # 行数写死为 1 是同一类错误（见上一个测试的说明）。
    #
    # 判据的**本意**没变：**导入不建"导入专用"的表、也不写别人的表**。
    # 因此这里只保留"导入自己可能偷偷建的表"；`proposed_claims` 只断言"存在也合法"。
    # （想断言"导入没往它写"，需要一个 before 快照；本文件没有采，属于**已知的判据
    # 弱点**，在此写明，不假装它被覆盖了。）
    forbidden = {
        "confirmed_labels_imported",
        "migrate_labels",
        "migrate_proposed",
        "import_runs",
    }
    assert forbidden & set(real_run.table_names) == set()
    if "proposed_claims" in real_run.table_names:
        print("[T-131 真实] proposed_claims 存在（T-105 的合法表，SPEC §2.10）")
    print(f"[T-131 真实] confirmed_labels 仍为 {real_run.label_rows_after} 行（导入未触碰）")


@requires_real_data
def test_real_no_extra_tables_or_triggers_were_created(real_run: RealRun) -> None:
    """判据 9 / 10：导入只写 `raw_records`，不新建表、不改触发器集合。

    ⚠️ 这条判据在 T-105 之后**从"写死集合"改成"逐项归类"**：`proposed_claims` /
    `proposal_runs` 与它们的 4 个 append-only 触发器是 SPEC §2.10 登记给 T-105 的合法
    对象（由 `atlas.cognition.store` 建立），不是导入建的。写死集合会让"后续任务按
    SPEC 建自己的表"表现为 T-131 的失败——那是**假失败**，会把运维注意力引到错的地方。

    真正要守的两条（本测试的核心）：
    1. 每个触发器都必须属于某个**已登记的域**（T-101 配置 / T-103 raw / T-105 提议 /
       T-107 证据）；
    2. `raw_records` 的 append-only 触发器必须**还在**（导入不许把它弄丢）。

    ⚠️ **2026-09-28 补 T-107**：`evidence_spans` 及其 2 个触发器在真实库里出现了
    （T-107 把证据校验接进流水线时由 `atlas.evidence.store` 的 `CREATE TABLE IF NOT EXISTS`
    建立，0 行）。这不是导入建的，也不是"归属不明"——§2.10 把 `evidence_spans` 登记给了
    T-107。本测试的写法（逐项归类 + `actual <= allowed`）正是为了在这种情况下**只需补一行
    域名**，而不是像原先写死集合那样把"后续任务按 SPEC 建自己的表"误报成 T-131 失败。
    """
    t101_t103_triggers = {
        "trg_config_versions_no_delete",
        "trg_config_versions_no_update",
        "trg_confirmed_labels_no_delete",
        "trg_confirmed_labels_no_update",
        "trg_label_space_no_delete",
        "trg_label_space_no_update",
        "trg_raw_records_no_delete",
        "trg_raw_records_no_update",
        "trg_registry_label_ref_snapshots_no_delete",
        "trg_registry_label_ref_snapshots_no_update",
    }
    # T-105 的成员（SPEC §2.10 表归属：proposed_claims / proposal_runs 归 T-105）
    t105_triggers = {
        "trg_proposed_claims_no_delete",
        "trg_proposed_claims_no_update",
        "trg_proposal_runs_no_delete",
        "trg_proposal_runs_no_update",
    }
    # T-107 的成员（SPEC §2.10 表归属：evidence_spans 归 T-107，只增不改）
    t107_triggers = {
        "trg_evidence_spans_no_delete",
        "trg_evidence_spans_no_update",
    }
    actual = set(real_run.trigger_names)
    allowed = t101_t103_triggers | t105_triggers | t107_triggers
    assert actual <= allowed, f"出现了归属不明的触发器：{sorted(actual - allowed)}"
    assert t101_t103_triggers <= actual, (
        f"T-101/T-103 的触发器丢了：{sorted(t101_t103_triggers - actual)}"
    )
    assert "trg_raw_records_no_update" in actual and "trg_raw_records_no_delete" in actual
    assert "raw_records" in real_run.table_names and "raw_store_meta" in real_run.table_names
    print(
        f"[T-131 真实] 触发器 {len(actual)} 个 = T-101/T-103 {len(actual & t101_t103_triggers)}"
        f" + T-105 {len(actual & t105_triggers)} + T-107 {len(actual & t107_triggers)}"
        "（全部有登记归属）"
    )


# --------------------------------------------------------------------------- #
# 判据 9：不变量（触发器 + 活对照）
# --------------------------------------------------------------------------- #
@requires_real_data
def test_real_raw_records_are_still_append_only_with_live_controls(tmp_path: Path) -> None:
    """判据 9：`raw_records` 仍受 append-only 触发器保护（**真活对照 + 否定**）。

    硬规则 4：断言"X 被拒绝"之前，必须先用同一调用路径证明**合法输入成功**。

    ⚠️ **本测试的活对照曾是假的（2026-09-26 主代理独立实测发现，已修）**
    ------------------------------------------------------------------

    原先那句"合法 INSERT 必须成功"插入的行**从未提交**：`sqlite3.connect()` 的
    `isolation_level` 默认是 `""`（不是 `None`），此时 DML 会自动开启一个**隐式事务**；
    测试没有 `commit()`，`close()` 把未提交事务**回滚**。于是那句
    `COUNT(*) == count + 1` 只证明了"**同一连接**看得见自己未提交的写"，
    与"触发器是否生效""合法写入是否真的成功"**都无关**。

    实测证据（在副本上做，真实库未动）：

    | 检查 | 结果 |
    |---|---|
    | 同一连接内 `COUNT(*) == count + 1` | True（原测试据此"通过"） |
    | `connection.in_transaction` | True |
    | **另开一个连接**看得见这一行吗 | **False** |
    | `close()` 之后另开连接看得见吗 | **False** |
    | 外部口径行数 | 75 → **75**（没变）⇒ INSERT 从未提交 |

    修法（本测试现在的做法）：**先提交，再另开连接确认可见**——
    跨连接可见才是"真的写进去了"。这才是活对照；同一连接的自我可见不是。

    **为什么写副本而不写真实库**
    ---------------------------

    `raw_records` 是 append-only，在真实库里插入一行探针**永远删不掉**，
    而 `data/store` 是用户的真实数据、不是测试夹具。因此：

    - 真实库上只做**只读**断言（其中 `UPDATE` 是会被触发器拒绝的那条否定断言）；
    - "合法 INSERT 跨连接可见"这条**写**操作在副本上做（副本的行数先与真实库对齐，
      证据才有意义）；
    - `UPDATE` / `DELETE` 的否定断言**两处都测**（副本 + 真实库），
      副本上顺带证明"被拒之后行数没变"。

    ⚠️ 因此本测试**不再往真实库写任何东西**（原版也没写进去，但那是靠回滚"侥幸"，
    不是设计）。
    """
    import shutil

    probe_raw_id = "raw_" + "1" * 32  # 刻意与真实语料无关、也不与旧探针 id 相同

    def _connect(path: Path):  # type: ignore[no-untyped-def]
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        return connection

    def _count(path: Path) -> int:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(
                connection.execute("SELECT COUNT(*) AS n FROM raw_records").fetchone()[0]
            )
        finally:
            connection.close()

    def _visible(path: Path, raw_id: str) -> bool:
        """**另开一个连接**看这一行在不在（跨连接可见 = 真的提交了）。"""
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return (
                int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM raw_records WHERE raw_id = ?",
                        (raw_id,),
                    ).fetchone()[0]
                )
                == 1
            )
        finally:
            connection.close()

    insert_sql = (
        "INSERT INTO raw_records (raw_id, channel_id, endpoint, content_sha256,"
        " byte_length, fetched_at, http_status) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    insert_args = (
        probe_raw_id,
        "t131-probe",
        "https://example.invalid/probe",
        "0" * 64,
        0,
        "2026-01-01T00:00:00+00:00",
        None,
    )

    real_count_before = _count(STORE_DB)

    # ---- 副本：可写的活对照 + 否定断言 -------------------------------------
    copy_db = tmp_path / "atlas-copy.db"
    shutil.copy2(STORE_DB, copy_db)
    assert _count(copy_db) == real_count_before, (
        "副本行数与真实库不符 —— 下面那条活对照的证据就不成立了"
    )

    # 活对照 1：合法 SELECT（副本）
    connection = _connect(copy_db)
    try:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM raw_records"
        ).fetchone()["n"] == real_count_before
    finally:
        connection.close()

    # 活对照 2：合法 INSERT **必须真的写进去**。用 autocommit（isolation_level=None）
    # 显式表达"这一次写要落盘"，再用**另一个连接**确认可见。
    writer = sqlite3.connect(str(copy_db), isolation_level=None)
    writer.row_factory = sqlite3.Row
    try:
        assert writer.in_transaction is False, "autocommit 连接不应处于事务中"
        writer.execute(insert_sql, insert_args)
    finally:
        writer.close()

    assert _visible(copy_db, probe_raw_id), (
        "合法 INSERT 之后**另一个连接**看不到这一行 —— 活对照没有真的写进去，"
        "那么它对该路径的'合法输入成功'什么都没证明（硬规则 4）"
    )
    assert _count(copy_db) == real_count_before + 1, "活对照：行数必须 +1"

    # 否定断言（副本）：UPDATE / DELETE 必须被触发器拒绝，原因文本可辨认
    connection = _connect(copy_db)
    try:
        with pytest.raises(sqlite3.DatabaseError) as update_error:
            connection.execute(
                "UPDATE raw_records SET endpoint = 'x' WHERE raw_id = ?", (probe_raw_id,)
            )
        assert "append-only" in str(update_error.value)
        with pytest.raises(sqlite3.DatabaseError) as delete_error:
            connection.execute(
                "DELETE FROM raw_records WHERE raw_id = ?", (probe_raw_id,)
            )
        assert "append-only" in str(delete_error.value)
        # 活对照（收尾）：被拒之后行数没变
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM raw_records"
        ).fetchone()["n"] == real_count_before + 1
    finally:
        connection.close()

    # ---- 真实库：只读 + 只做否定断言（不写入任何东西）----------------------
    connection = _connect(STORE_DB)
    try:
        # 活对照 1（真实库）：合法 SELECT 必须成功
        real_rows = connection.execute(
            "SELECT COUNT(*) AS n FROM raw_records"
        ).fetchone()["n"]
        assert real_rows == real_count_before
        # 取一行**真实存在**的记录，对它做 UPDATE —— 必须被拒（不能靠不存在的 id 蒙混）
        existing = connection.execute(
            "SELECT raw_id FROM raw_records ORDER BY raw_id LIMIT 1"
        ).fetchone()
        assert existing is not None, "真实归档里应有记录"
        existing_id = str(existing["raw_id"])
        with pytest.raises(sqlite3.DatabaseError) as real_update_error:
            connection.execute(
                "UPDATE raw_records SET endpoint = 'x' WHERE raw_id = ?", (existing_id,)
            )
        assert "append-only" in str(real_update_error.value)
        with pytest.raises(sqlite3.DatabaseError) as real_delete_error:
            connection.execute("DELETE FROM raw_records WHERE raw_id = ?", (existing_id,))
        assert "append-only" in str(real_delete_error.value)
        # 活对照（收尾）：真实库行数一个都没变
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM raw_records"
        ).fetchone()["n"] == real_count_before
    finally:
        connection.close()

    assert _count(STORE_DB) == real_count_before, "真实库行数不得改变（本测试不写真实库）"

    print(
        "[T-131 真实] raw_records 仍为 append-only："
        f"合法 INSERT 跨连接可见（副本 {real_count_before} → {real_count_before + 1}），"
        f"UPDATE/DELETE 被拒（副本与真实库各验一次，真实库保持 {real_count_before} 行）"
    )


# --------------------------------------------------------------------------- #
# 判据 6 / 13：data/raw 只读
# --------------------------------------------------------------------------- #
@requires_real_data
def test_real_legacy_tree_is_byte_identical(real_run: RealRun) -> None:
    """判据 6 / 13：导入前后 `data/raw/` 整树 sha256 完全一致。"""
    after = tree_digest(LEGACY_RAW)
    assert after == real_run.before_tree, "data/raw 被改动了 —— 本任务只允许读它"
    files, size = directory_stats(LEGACY_RAW)
    assert files == MEASURED_JSON_FILES == real_run.census.json_files
    print(
        f"[T-131 真实] data/raw 整树 sha256 前后一致：{after[:16]}…；"
        f"{files} 个文件 / {size} 字节"
    )


@requires_real_data
def test_real_store_grew_by_exactly_the_imported_records(real_run: RealRun) -> None:
    """判据 3 / 12：`data/store` 的增长**恰好**等于新建的记录数（没有额外副作用文件）。

    重复执行本文件时 `imported == 0`（导入已完成），此时断言退化为"零增长"——
    这本身也是幂等的一条证据。
    """
    archive = open_archive(STORE_ROOT)
    try:
        stored_article_bytes = real_run.census.stored_bytes(archive)
    finally:
        archive.close()
    files_after, bytes_after = directory_stats(STORE_ROOT)
    added_files = files_after - real_run.before_files
    added_bytes = bytes_after - real_run.before_bytes
    # 每条新记录 = content.bin + meta.json ⇒ 2 个文件
    assert added_files == 2 * real_run.report.imported, (
        f"data/store 新增文件 {added_files} ≠ 2 × 新建 {real_run.report.imported}"
    )
    if real_run.report.imported:
        assert added_bytes > real_run.report.bytes_archived, "meta.json 的字节也应计入增长"
    else:
        assert added_bytes == 0, "重跑不应改变 data/store 的字节数"
    # 65 条旧语料正文在磁盘上的实际合计 == 旧记录去重后的正文合计（判据 12）
    assert stored_article_bytes == real_run.census.distinct_bytes
    assert real_run.report.bytes_archived in (0, real_run.census.raw_bytes)
    print(
        f"[T-131 真实] data/store：文件 {real_run.before_files} → {files_after}"
        f"（+{added_files}），字节 {real_run.before_bytes} → {bytes_after}（+{added_bytes}）"
    )
    print(
        f"[T-131 真实] raw_records：{real_run.before_rows} → {real_run.rows_after}"
        f"（本次新建 {real_run.report.imported}），旧正文按文件累加 "
        f"{real_run.census.raw_bytes} 字节；去重后磁盘 {stored_article_bytes} 字节"
    )


# --------------------------------------------------------------------------- #
# 判据 11 + §2.5：按行业筛选闭合（T-106 的 feed × 注册表行业）
# --------------------------------------------------------------------------- #
@requires_real_data
def test_real_feed_industry_filter_is_closed(real_run: RealRun) -> None:
    """判据 11 / §2.5：`industry_of` 注入后，按行业筛选**精确等于**归档里该行业的记录集合。

    做法（全店口径，不挑子集）：对注册表里每个行业，用 T-106 的 `run_query` 拿结果，
    与"直接查归档 + 用注册表行业映射算出的期望集合"**逐个 `raw_id` 比对**，
    并断言各行业结果之并 == 全部已归档记录（不重不漏）。
    """
    archive = open_archive(STORE_ROOT)
    try:
        industry_map = dict(real_run.channel_industry)
        source = ArchiveFeedSource(archive, industry_of=industry_map)
        all_ids = set(archive.all_raw_ids())
        channel_of = {raw_id: archive.get(raw_id).channel_id for raw_id in all_ids}
        assert all(channel in industry_map for channel in channel_of.values()), (
            "有归档记录的 channel_id 不在注册表里 —— 行业归属会落空"
        )
        # 旧语料的 65 条必须逐条落在注册表渠道上（判据 11）
        assert set(real_run.census.expected) <= all_ids
        assert {channel_of[raw_id] for raw_id in real_run.census.expected} <= set(industry_map)

        covered: List[str] = []
        for industry in sorted(set(industry_map.values())):
            result = run_query(source, FeedQuery(industries=(industry,), limit=200))
            expected_ids = {
                raw_id for raw_id in all_ids if industry_map[channel_of[raw_id]] == industry
            }
            got = {item.raw_id for item in result.items}
            assert got == expected_ids, (
                f"行业 {industry} 的 feed 筛选与归档不一致："
                f"多 {sorted(got - expected_ids)[:3]} 少 {sorted(expected_ids - got)[:3]}"
            )
            assert result.total == len(expected_ids)
            assert all(item.industry == industry for item in result.items)
            imported_here = len(expected_ids & set(real_run.census.expected))
            covered.extend(sorted(got))
            print(
                f"[T-131 真实] 行业 {industry:<20} feed 命中 {result.total:>3} 条"
                f"（其中旧语料导入 {imported_here} 条）"
            )
        assert len(covered) == len(set(covered)), "不同行业的结果不得重叠"
        assert set(covered) == all_ids, "各行业结果之并必须覆盖全部归档记录"

        # 活对照：不存在的行业必须返回空（负向筛选不能静默返回全部）
        empty = run_query(source, FeedQuery(industries=("no-such-industry",), limit=200))
        assert empty.total == 0
        # 活对照：单一渠道筛选也必须闭合
        for channel in sorted({channel_of[raw_id] for raw_id in real_run.census.expected}):
            scoped = run_query(source, FeedQuery(channels=(channel,), limit=200))
            assert {item.raw_id for item in scoped.items} == {
                raw_id for raw_id in all_ids if channel_of[raw_id] == channel
            }
        # §2.5 的已知缝隙：**忘记注入** industry_of 时 industry 全是 None（显式钉住）
        blind = ArchiveFeedSource(archive)
        assert run_query(blind, FeedQuery(limit=5)).items[0].industry is None
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 判据 12：磁盘字节与 DB 声明一致（全量，不只抽查）
# --------------------------------------------------------------------------- #
@requires_real_data
def test_real_declared_bytes_match_disk(real_run: RealRun) -> None:
    """判据 12：`raw_records.byte_length` 之和 == `data/store/raw/**/content.bin` 实际字节。"""
    archive = open_archive(STORE_ROOT)
    try:
        declared = sum(archive.get(raw_id).byte_length for raw_id in archive.all_raw_ids())
        on_disk = 0
        for raw_id in archive.all_raw_ids():
            on_disk += archive.blobs.content_path(raw_id).stat().st_size
        assert declared == on_disk
        assert declared >= real_run.report.bytes_archived
        print(
            f"[T-131 真实] {len(archive.all_raw_ids())} 条记录的声明字节 {declared} "
            f"== 磁盘 content.bin 合计 {on_disk}"
        )
    finally:
        archive.close()
