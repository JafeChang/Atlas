"""对账表：**每个文件都有归宿**，数字必须加得起来（硬规则 1 / 3）。

`MigrateReport` 的全部价值在于 `ledger_problems()`：它把"扫描到的文件数 = 各归宿之和"
变成一个**可断言的不变量**。归档基线里"字段存在但永为空"的教训是：只记数字、
不校验数字之间关系，失配就会一直悄悄躺着。

两套账（都能加得起来）
----------------------

```text
文件账：scanned_files == skipped + articles            （articles = 可供导入的文档数）
导入账：articles      == imported + deduplicated + failed
```

`imported` / `deduplicated` 的口径（内容寻址的必然结果，§6.2）
-------------------------------------------------------------

- **imported**：本次调用**新建**的 `raw_id`（此前归档里没有）。
- **deduplicated**：`raw_id` 已存在**且内容指纹相同** —— 旧系统未去重，
  同一篇文章被重复采集约 10 次（实测 474 篇 → 65 条不同文档）。
  这是**幂等命中**，不是错误，也不是"丢数据"。

因此**第二次运行**的 `imported == 0`、`deduplicated == 全部文档数` —— 这正是幂等证明。

失败的每一种原因都计数（`failure_counts`），原因码见 `errors` 模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = [
    "FailureReason",
    "MigrateEntry",
    "MigrateReport",
    "PerChannelSummary",
    "SkipReason",
]

#: 条目的归宿：跳过（非文档）/ 新建 / 幂等命中 / 失败。
STATUS_SKIPPED = "skipped"
STATUS_IMPORTED = "imported"
STATUS_DEDUPLICATED = "deduplicated"
STATUS_FAILED = "failed"

STATUSES: Tuple[str, ...] = (
    STATUS_SKIPPED,
    STATUS_IMPORTED,
    STATUS_DEDUPLICATED,
    STATUS_FAILED,
)

#: `(原因码, 人类可读说明)`。
FailureReason = Tuple[str, str]
#: `(原因码, 人类可读说明)`。
SkipReason = Tuple[str, str]


@dataclass(frozen=True)
class MigrateEntry:
    """**一个旧 JSON 文件**的归宿（每一条都可追溯到文件）。"""

    relative: str
    channel: str
    status: str
    reason: str = ""
    message: str = ""
    raw_id: Optional[str] = None
    #: 映射后的注册表渠道 id（跳过/失败时可能为空）。
    registry_channel: str = ""
    endpoint: str = ""
    content_sha256: str = ""
    byte_length: int = 0
    #: 旧记录里被抓取时间的**字段名**（可审计：这条记录的时间是哪来的）。
    moment_field: str = ""
    moment_value: str = ""
    fetched_at: Optional[str] = None
    #: 旧记录自己的 uuid（仅作溯源，**不**参与新 `raw_id`）。
    legacy_id: str = ""


@dataclass
class PerChannelSummary:
    """逐频道对账（`ledger()` 与报告打印都用它）。"""

    channel: str
    scanned: int = 0
    skipped: int = 0
    articles: int = 0
    imported: int = 0
    deduplicated: int = 0
    failed: int = 0
    bytes_archived: int = 0

    def row(self) -> Tuple[str, int, int, int, int, int, int, int]:
        return (
            self.channel,
            self.scanned,
            self.skipped,
            self.articles,
            self.imported,
            self.deduplicated,
            self.failed,
            self.bytes_archived,
        )


@dataclass
class MigrateReport:
    """一次导入运行的完整结果（**每次运行一份**，不是累计值）。

    累计事实看归档本身（`raw_records` 行数 / `data/store/raw` 目录数），
    两者在真实数据测试里互相印证。
    """

    legacy_root: str = ""
    archive_root: str = ""
    entries: List[MigrateEntry] = field(default_factory=list)
    #: 运行**前**归档里的 `raw_id` 数（累计快照）。
    records_before: int = 0
    #: 运行**后**归档里的 `raw_id` 数（累计快照）。
    records_after: int = 0
    #: 本次运行碰过的全部旧文章 `raw_id`（含重复，按处理顺序）。
    raw_ids: List[str] = field(default_factory=list)
    archived_bytes: int = 0

    # ------------------------------------------------------------------
    # 计数
    # ------------------------------------------------------------------
    def _count(self, status: str) -> int:
        return sum(1 for entry in self.entries if entry.status == status)

    @property
    def scanned_files(self) -> int:
        return len(self.entries)

    @property
    def skipped(self) -> int:
        return self._count(STATUS_SKIPPED)

    @property
    def imported(self) -> int:
        return self._count(STATUS_IMPORTED)

    @property
    def deduplicated(self) -> int:
        return self._count(STATUS_DEDUPLICATED)

    @property
    def failed(self) -> int:
        return self._count(STATUS_FAILED)

    @property
    def articles(self) -> int:
        """可供导入的文档条数（有 `document_type` 且有正文）。

        **按文件计**（旧系统未去重）：`imported + deduplicated + failed`。
        """
        return self.scanned_files - self.skipped

    @property
    def distinct_raw_ids(self) -> int:
        """本次运行涉及的**不同** `raw_id` 数（内容寻址收敛后的文档数）。"""
        return len(set(self.raw_ids))

    @property
    def bytes_archived(self) -> int:
        """本次运行写入/命中的正文总字节（**按文件累加**，未去重）。"""
        return sum(entry.byte_length for entry in self.entries if entry.status == STATUS_IMPORTED)

    @property
    def failures(self) -> List[MigrateEntry]:
        return [entry for entry in self.entries if entry.status == STATUS_FAILED]

    @property
    def skips(self) -> List[MigrateEntry]:
        return [entry for entry in self.entries if entry.status == STATUS_SKIPPED]

    # ------------------------------------------------------------------
    # 分类计数
    # ------------------------------------------------------------------
    def skip_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self.entries:
            if entry.status == STATUS_SKIPPED:
                counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return dict(sorted(counts.items()))

    def failure_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self.entries:
            if entry.status == STATUS_FAILED:
                counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return dict(sorted(counts.items()))

    def channels(self) -> List[PerChannelSummary]:
        """逐频道对账（按频道名字典序）。"""
        summaries: Dict[str, PerChannelSummary] = {}
        for entry in self.entries:
            summary = summaries.setdefault(entry.channel, PerChannelSummary(channel=entry.channel))
            summary.scanned += 1
            if entry.status == STATUS_SKIPPED:
                summary.skipped += 1
            else:
                summary.articles += 1
            if entry.status == STATUS_IMPORTED:
                summary.imported += 1
                summary.bytes_archived += entry.byte_length
            elif entry.status == STATUS_DEDUPLICATED:
                summary.deduplicated += 1
            elif entry.status == STATUS_FAILED:
                summary.failed += 1
        return [summaries[name] for name in sorted(summaries)]

    def per_registry_channel(self) -> Dict[str, int]:
        """映射后的注册表渠道 → 本次导入/命中的文章数（去重前）。"""
        counts: Dict[str, int] = {}
        for entry in self.entries:
            if entry.status not in (STATUS_IMPORTED, STATUS_DEDUPLICATED):
                continue
            key = entry.registry_channel or entry.channel
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def samples(self, status: str, limit: int = 3) -> List[MigrateEntry]:
        return [entry for entry in self.entries if entry.status == status][:limit]

    # ------------------------------------------------------------------
    # 不变量
    # ------------------------------------------------------------------
    def ledger_problems(self) -> List[str]:
        """对账不成立时的**问题清单**（空 = 账能对上）。

        断言这件事而不是只打印数字：归档基线的失败模式正是"数字都在、关系不对"。
        """
        problems: List[str] = []
        if self.scanned_files != self.skipped + self.articles:
            problems.append(
                f"文件账不平：扫描 {self.scanned_files} ≠ 跳过 {self.skipped} + 文档 {self.articles}"
            )
        if self.articles != self.imported + self.deduplicated + self.failed:
            problems.append(
                f"导入账不平：文档 {self.articles} ≠ 新建 {self.imported} + "
                f"收敛 {self.deduplicated} + 失败 {self.failed}"
            )
        unknown = sorted({entry.status for entry in self.entries} - set(STATUSES))
        if unknown:
            problems.append(f"出现未知归宿 {unknown}")
        if self.records_after < self.records_before:
            problems.append(
                f"归档行数减少：{self.records_before} → {self.records_after}（Raw 只增不改，不该发生）"
            )
        expected_after = self.records_before + self.imported
        if self.records_after != expected_after:
            problems.append(
                f"归档行数与对账不一致：运行前 {self.records_before} + 新建 {self.imported} "
                f"≠ 运行后 {self.records_after}"
            )
        for entry in self.entries:
            if entry.status in (STATUS_IMPORTED, STATUS_DEDUPLICATED) and not entry.raw_id:
                problems.append(f"{entry.relative}: {entry.status} 却没有 raw_id")
            if entry.status == STATUS_FAILED and not entry.reason:
                problems.append(f"{entry.relative}: 失败却没有原因码")
            if entry.status == STATUS_SKIPPED and not entry.reason:
                problems.append(f"{entry.relative}: 跳过却没有原因码")
        return problems

    def digest(self) -> str:
        """本报告内容指纹（同一输入重复运行必须相同 —— 幂等的可断言形态）。"""
        import hashlib

        hasher = hashlib.sha256()
        for entry in self.entries:
            hasher.update(
                "\x1f".join(
                    (
                        entry.relative,
                        entry.status,
                        entry.reason,
                        entry.raw_id or "",
                        entry.content_sha256,
                        entry.fetched_at or "",
                    )
                ).encode("utf-8")
            )
            hasher.update(b"\x1e")
        return hasher.hexdigest()

    def render(self) -> str:
        """人类可读的对账表（工具与失败信息都用它）。"""
        lines: List[str] = []
        lines.append(f"旧语料根: {self.legacy_root}")
        lines.append(f"归档根  : {self.archive_root}")
        lines.append(
            f"文件账  : 扫描 {self.scanned_files} = 跳过 {self.skipped} + 文档 {self.articles}"
        )
        lines.append(
            f"导入账  : 文档 {self.articles} = 新建 {self.imported} + "
            f"收敛 {self.deduplicated} + 失败 {self.failed}"
        )
        lines.append(
            f"归档    : raw_records {self.records_before} → {self.records_after}；"
            f"本次正文 {self.bytes_archived} 字节；不同 raw_id {self.distinct_raw_ids}"
        )
        lines.append("")
        header = (
            f"{'频道':<18}{'扫描':>6}{'跳过':>6}{'文档':>6}"
            f"{'新建':>6}{'收敛':>6}{'失败':>6}{'字节':>12}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for summary in self.channels():
            lines.append(
                f"{summary.channel:<18}{summary.scanned:>6}{summary.skipped:>6}"
                f"{summary.articles:>6}{summary.imported:>6}{summary.deduplicated:>6}"
                f"{summary.failed:>6}{summary.bytes_archived:>12}"
            )
        if self.skip_counts():
            lines.append("")
            lines.append("跳过原因: " + ", ".join(
                f"{reason}={count}" for reason, count in self.skip_counts().items()
            ))
        if self.failure_counts():
            lines.append("失败原因: " + ", ".join(
                f"{reason}={count}" for reason, count in self.failure_counts().items()
            ))
        else:
            lines.append("失败原因: 无")
        problems = self.ledger_problems()
        lines.append("对账问题: " + ("无" if not problems else "; ".join(problems)))
        return "\n".join(lines)
