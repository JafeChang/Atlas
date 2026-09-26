"""逐篇导入主流程：旧 JSON → 新 Raw（只增不改，幂等）。

一篇文章的转换（**没有一步是猜的**）
-----------------------------------

```
旧 JSON 文件
  ├─ channel      = 文件所在目录名  → 经注入的映射 → 注册表 channel_id
  ├─ endpoint     = 旧记录 source_url（**文章地址**，不是 feed 地址）
  ├─ content      = 旧记录 raw_content 的 UTF-8 字节
  ├─ fetched_at   = 旧记录 collected_at（→ created_at → stored_at → updated_at）
  └─ raw_id       = raw_id_for(channel_id, endpoint, content_sha256)
                 ⇒ **逐篇**，不是逐 feed（§6.3 裁决 B 的落地）
```

然后 `ArchiveStore.put(record, content)`（T-103）：

- 新建 → `imported`（对账表里的"新建"）
- 已存在同指纹 → `deduplicated`（**幂等命中**；旧系统未去重，实测 474 篇 → 65 条）
- 已存在不同指纹 → `ImmutabilityError` 冒泡（**不是**可记账失败：同 `raw_id` 不同内容
  只可能是 SHA-256 碰撞或代码写错，两者都必须让人看到）

写完之后**回读校验**：`get_content(raw_id)` 的 sha256 必须与记录里的一致 ——
"写完就算成功"正是归档基线里"字段存在但永为空"的同一类自欺。

本包**不写**任何非 `raw_records` 的表（§2.4：不得迁移任何人工标签）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from atlas.archive import ArchiveStore, open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256, raw_id_for

from .errors import (
    REASON_CHANNEL_MAPPING_CONFLICT,
    REASON_CHANNEL_NOT_MAPPED,
    REASON_MISSING_TIMESTAMP,
    REASON_NESTED_LAYOUT,
    REASON_NO_CHANNEL_DIR,
    ChannelMappingError,
    MigrateError,
    TimestampError,
)
from .mapping import (
    ChannelMapResolver,
    channel_map_resolver,
    resolve_nothing,
)
from .moment import parse_legacy_moment, pick_moment_field
from .report import (
    STATUS_DEDUPLICATED,
    STATUS_FAILED,
    STATUS_IMPORTED,
    STATUS_SKIPPED,
    MigrateEntry,
    MigrateReport,
)
from .source import LegacyDocument, classify_legacy, scan_legacy_files, to_document

__all__ = [
    "DEFAULT_ARCHIVE_ROOT",
    "DEFAULT_LEGACY_ROOT",
    "LEGACY_HTTP_STATUS",
    "REPO_ROOT",
    "Migrate",
    "MigrateOptions",
    "load_channel_map_file",
    "migrate_legacy",
]

#: 仓库根：默认路径以此为准，避免"从哪个 cwd 跑"改变写到哪里。
REPO_ROOT = Path(__file__).resolve().parents[3]

#: 旧语料根（SPEC §7.1 的 `data/raw/`，**只读**）。
DEFAULT_LEGACY_ROOT = REPO_ROOT / "data" / "raw"

#: 新归档根（SPEC §2.10 的 `data/store/`，本任务**故意写入**这里）。
DEFAULT_ARCHIVE_ROOT = REPO_ROOT / "data" / "store"

#: 正文与新 Raw 之间的状态码。旧记录没有 HTTP 响应体本身，只有抓取结果。
#: 用 `200` 与 T-205 的既有读法一致（`tests/test_search_realdata.py` 也是这么写的）。
LEGACY_HTTP_STATUS = 200


@dataclass(frozen=True)
class MigrateOptions:
    """导入选项（**全部显式**，没有隐式默认源）。"""

    legacy_root: Path = DEFAULT_LEGACY_ROOT
    archive_root: Optional[Path] = None
    archive: Optional[ArchiveStore] = None
    channel_map: Union[Mapping[str, object], ChannelMapResolver] = resolve_nothing
    #: `True`：第一条可记账失败就抛 `MigrateError`（不留下"半数导入"的错觉）。
    strict: bool = False


class Migrate:
    """把旧语料根下的**逐篇文章**导入归档。

    构造即持有归档实例（不重新打开），因此同一实例重复调用是幂等的，
    且调用方能在同一个连接/事务视野里观察前后状态。
    """

    def __init__(self, options: Optional[MigrateOptions] = None) -> None:
        self._options = options or MigrateOptions()
        target = self._options.archive_root
        self._archive = (
            self._options.archive
            if self._options.archive is not None
            else open_archive(DEFAULT_ARCHIVE_ROOT if target is None else target)
        )
        self._owns_archive = self._options.archive is None
        self._resolve = _as_resolver(self._options.channel_map)

    # ------------------------------------------------------------------
    # 访问
    # ------------------------------------------------------------------
    @property
    def archive(self) -> ArchiveStore:
        """归档存储（调用方可用它做只读校验，例如 `verify()` 与行数）。"""
        return self._archive

    @property
    def options(self) -> MigrateOptions:
        return self._options

    @property
    def legacy_root(self) -> Path:
        return Path(self._options.legacy_root)

    def close(self) -> None:
        """只在**自己开**了归档时关闭（外部传入的归档由调用方负责）。"""
        if self._owns_archive:
            self._archive.close()

    def __enter__(self) -> "Migrate":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self) -> MigrateReport:
        """跑一次导入，返回对账表。**不吞任何非预期异常。**"""
        report = MigrateReport(
            legacy_root=str(self.legacy_root),
            archive_root=str(self._archive.raw_dir),
            records_before=len(self._archive.all_raw_ids()),
        )
        for scanned in scan_legacy_files(self.legacy_root):
            relative = scanned.file.relative
            channel = scanned.file.channel
            if scanned.error is not None:
                self._record_failure(report, scanned.error, relative, channel)
                continue
            if not channel:
                self._record_failure(
                    report,
                    MigrateError(
                        "文件直接位于旧语料根下，没有频道目录 ⇒ 无法判定行业归属"
                        "（不猜，见 SPEC §2.5 的 C8 闭环）",
                        path=scanned.file.path,
                        reason=REASON_NO_CHANNEL_DIR,
                    ),
                    relative,
                    channel,
                )
                continue
            if scanned.file.nested:
                # 真实语料里 0 个嵌套文件（534/534 深度 1）。布局变了必须让人看到：
                # 按子目录名当频道会编造一个注册表里不存在的渠道，`raw_id` 与行业归属双错。
                self._record_failure(
                    report,
                    MigrateError(
                        f"文件嵌在频道目录的子目录里（{relative}）；真实语料里嵌套文件为 0，"
                        "布局已变，请先确认频道归属规则",
                        path=scanned.file.path,
                        reason=REASON_NESTED_LAYOUT,
                    ),
                    relative,
                    channel,
                )
                continue

            payload = scanned.payload or {}
            skip_reason = classify_legacy(payload)
            if skip_reason is not None:
                report.entries.append(
                    MigrateEntry(
                        relative=relative,
                        channel=channel,
                        status=STATUS_SKIPPED,
                        reason=skip_reason,
                        message=(
                            "document_type 缺失 ⇒ 旧系统的非文档产物（empty_* / summary_*）"
                            if skip_reason == "non_document"
                            else "raw_content 为空 ⇒ 空抓取，没有可归档的正文"
                        ),
                        legacy_id=_legacy_id(payload),
                    )
                )
                continue

            registry_channel = self._map_channel(relative, channel, report)
            if registry_channel is None:
                continue

            picked = pick_moment_field(payload)
            if picked is None:
                error = TimestampError(
                    "旧记录没有任何可用的抓取时间字段；不用「现在」兜底"
                    "（那会把历史文章伪装成刚采到的，破坏 feed 时间序）",
                    path=scanned.file.path,
                    reason=REASON_MISSING_TIMESTAMP,
                )
                self._record_failure(report, error, relative, channel)
                continue
            moment_field, moment_value = picked
            try:
                fetched_at = parse_legacy_moment(
                    moment_field, moment_value, path=scanned.file.path
                )
            except MigrateError as error:
                self._record_failure(report, error, relative, channel)
                continue
            try:
                document = to_document(scanned, (moment_field, moment_value))
            except MigrateError as error:
                self._record_failure(report, error, relative, channel)
                continue

            entry = self._write_one(
                report,
                relative=relative,
                channel=channel,
                registry_channel=registry_channel,
                document=document,
                fetched_at=fetched_at,
                moment_field=moment_field,
                moment_value=moment_value,
            )
            report.entries.append(entry)

        report.records_after = len(self._archive.all_raw_ids())
        return report

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _map_channel(
        self, relative: str, channel: str, report: MigrateReport
    ) -> Optional[str]:
        targets, status = self._resolve(channel)
        if status == "":
            return targets[0]
        if status == "not_found":
            error = ChannelMappingError(
                f"旧频道 {channel!r} 在注册表里没有对应渠道（行业归属会丢，不猜）；"
                "请由组合根注入映射",
                reason=REASON_CHANNEL_NOT_MAPPED,
            )
        elif status == "ambiguous":
            error = ChannelMappingError(
                f"旧频道 {channel!r} 对应多个注册表渠道 {list(targets)}；不任选",
                reason=REASON_CHANNEL_MAPPING_CONFLICT,
            )
        else:  # pragma: no cover - 解析器契约被违反，属于接线错误
            raise MigrateError(f"未知的映射状态 {status!r}（旧频道 {channel!r}）")
        self._record_failure(report, error, relative, channel)
        return None

    def _write_one(
        self,
        report: MigrateReport,
        *,
        relative: str,
        channel: str,
        registry_channel: str,
        document: LegacyDocument,
        fetched_at: datetime,
        moment_field: str,
        moment_value: str,
    ) -> MigrateEntry:
        content = document.content
        sha = content_sha256(content)
        raw_id = raw_id_for(registry_channel, document.url, sha)
        expected_byte_length = document.content_length
        record = RawRecord(
            raw_id=raw_id,
            channel_id=registry_channel,
            endpoint=document.url,
            content_sha256=sha,
            byte_length=len(content),
            fetched_at=fetched_at,
            http_status=LEGACY_HTTP_STATUS,
        )
        existed = self._archive.records.get_optional(raw_id) is not None
        stored = self._archive.put(record, content)
        if stored.content_sha256 != sha or stored.byte_length != len(content):
            raise MigrateError(
                f"归档返回的记录与写入内容不符：raw_id={raw_id} "
                f"（行 {stored.content_sha256[:12]}…/{stored.byte_length}，"
                f"写入 {sha[:12]}…/{len(content)}）"
            )
        # 回读校验：写完必须能从磁盘读回**同样的字节**（硬规则 1）。
        readback = self._archive.get_content(raw_id)
        actual = content_sha256(readback)
        if actual != sha:
            raise MigrateError(
                f"回读校验失败：raw_id={raw_id} 磁盘字节指纹 {actual[:12]}… ≠ 写入 {sha[:12]}…"
            )
        if len(readback) != expected_byte_length:
            raise MigrateError(
                f"回读校验失败：raw_id={raw_id} 磁盘字节数 {len(readback)} "
                f"≠ 写入 {expected_byte_length}"
            )
        report.raw_ids.append(raw_id)
        return MigrateEntry(
            relative=relative,
            channel=channel,
            status=STATUS_DEDUPLICATED if existed else STATUS_IMPORTED,
            reason="",
            message="raw_id 已存在且指纹相同（内容寻址的幂等命中）" if existed else "",
            raw_id=raw_id,
            registry_channel=registry_channel,
            endpoint=record.endpoint,
            content_sha256=sha,
            byte_length=len(content),
            moment_field=moment_field,
            moment_value=moment_value,
            fetched_at=fetched_at.isoformat(),
            legacy_id=document.raw_id_legacy,
        )

    def _record_failure(
        self,
        report: MigrateReport,
        error: MigrateError,
        relative: str,
        channel: str,
    ) -> None:
        entry = MigrateEntry(
            relative=relative,
            channel=channel,
            status=STATUS_FAILED,
            reason=error.reason,
            message=str(error),
        )
        # 先记账再（可选）抛：strict 模式下异常消息里带着完整的对账表，
        # 操作者一眼能看到"已经发生了什么"，而不是只知道"第一条就炸了"。
        report.entries.append(entry)
        if self._options.strict:
            report.records_after = len(self._archive.all_raw_ids())
            raise _StrictFailure(entry, report) from error


class _StrictFailure(MigrateError):
    """`strict=True` 时的失败：异常本身携带对账表（便于一次看清全局）。"""

    def __init__(self, entry: MigrateEntry, report: MigrateReport) -> None:
        super().__init__(
            f"strict 模式下导入中止于 {entry.relative}：[{entry.reason}] {entry.message}\n"
            f"{report.render()}",
            path=Path(entry.relative),
            reason=entry.reason,
        )
        self.entry = entry
        self.report = report


def _as_resolver(
    channel_map: Union[Mapping[str, object], ChannelMapResolver]
) -> ChannelMapResolver:
    if callable(channel_map) and not isinstance(channel_map, Mapping):
        return channel_map
    return channel_map_resolver(channel_map)


def _legacy_id(payload: Mapping[str, object]) -> str:
    value = payload.get("id")
    return value if isinstance(value, str) else ""


def migrate_legacy(
    *,
    legacy_root: Optional[Path] = None,
    archive_root: Optional[Path] = None,
    archive: Optional[ArchiveStore] = None,
    channel_map: Union[Mapping[str, object], ChannelMapResolver, None] = None,
    strict: bool = False,
) -> MigrateReport:
    """一次性导入（便捷入口；重复调用幂等）。

    不传 `channel_map` 时**每条记录都会失败**（`empty_mapper` 一律抛）——
    这是刻意的默认值：忘记注入映射必须响亮可见，而不是静默把文档挂到编造的渠道上。
    """
    options = MigrateOptions(
        legacy_root=DEFAULT_LEGACY_ROOT if legacy_root is None else Path(legacy_root),
        archive_root=None if archive_root is None else Path(archive_root),
        archive=archive,
        channel_map=resolve_nothing if channel_map is None else channel_map,
        strict=strict,
    )
    migrator = Migrate(options)
    try:
        return migrator.run()
    finally:
        migrator.close()


def load_channel_map_file(path: Path) -> Dict[str, Tuple[str, ...]]:
    """读一个 JSON 格式的显式映射文件（`{旧名: id | [id, …]}`）。

    给操作者一条"不靠启发式"的路：注册表里没有同名渠道时，写一份显式映射即可。
    文件内容非法 → `ChannelMappingError`（可记账），不静默当成空映射。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ChannelMappingError(f"渠道映射文件读取失败：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise ChannelMappingError(f"渠道映射文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ChannelMappingError(f"渠道映射文件顶层必须是对象：{path}")
    out: Dict[str, Tuple[str, ...]] = {}
    for name, targets in payload.items():
        if isinstance(targets, str):
            out[str(name)] = (targets,)
        elif isinstance(targets, Sequence):
            out[str(name)] = tuple(str(item) for item in targets)
        else:
            raise ChannelMappingError(f"渠道映射 {name!r} 的值必须是字符串或字符串数组")
    return out
