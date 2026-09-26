"""T-131 失败原因与异常：**逐条可解释**，不静默跳过（硬规则 2 / 3）。

两类"不可用"必须分开
--------------------

1. **跳过**（skip）：文件根本不是文档产物，不是失败。旧系统在同一个目录里混放了
   `empty_*.json` / `summary_*.json`（`document_type` 为空、无正文）。
   把它们计入"失败"会让对账表失真，计入"文档"则会把 feed 摘要当成文章。
2. **失败**（failure）：**本该能导入**的文档没能导入——时间无法解析、字段缺失、
   频道不在注册表、字节不是合法 UTF-8。这类必须逐条带原因出现在报告里。

`MigrateError` 是"**知道原因、可以记账**"的失败；其它异常（`ValueError` /
`OSError` / `ImmutabilityError` …）是**接线错误**，直接冒泡，
不允许被 `except` 吞成一条"失败"（硬规则 2）。
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "REASON_CHANNEL_MAPPING_CONFLICT",
    "REASON_CHANNEL_NOT_IN_REGISTRY",
    "REASON_CHANNEL_NOT_MAPPED",
    "REASON_FINGERPRINT_MISMATCH",
    "REASON_MISSING_TIMESTAMP",
    "REASON_NESTED_LAYOUT",
    "REASON_NOT_AN_OBJECT",
    "REASON_NO_CHANNEL_DIR",
    "REASON_UNPARSEABLE_JSON",
    "REASON_UNPARSEABLE_TIMESTAMP",
    "REASON_UNREADABLE_BYTES",
    "SKIP_EMPTY_CONTENT",
    "SKIP_NON_DOCUMENT",
    "ChannelMappingError",
    "LegacyScanError",
    "MigrateError",
    "RUN_ERRORS",
    "TimestampError",
]

# --------------------------------------------------------------------------- #
# 跳过原因（不是失败）
# --------------------------------------------------------------------------- #

#: `document_type` 缺失 ⇒ 旧系统的**非文档产物**（`empty_*` / `summary_*`）。
SKIP_NON_DOCUMENT = "non_document"
#: 有 `document_type` 但 `raw_content` 为空 ⇒ 空抓取，没有可归档的正文。
SKIP_EMPTY_CONTENT = "empty_content"

# --------------------------------------------------------------------------- #
# 失败原因（必须逐条出现在对账表里）
# --------------------------------------------------------------------------- #

REASON_UNPARSEABLE_JSON = "unparseable_json"
REASON_NOT_AN_OBJECT = "not_an_object"
REASON_UNREADABLE_BYTES = "unreadable_bytes"
REASON_CHANNEL_NOT_MAPPED = "channel_not_mapped"
REASON_CHANNEL_NOT_IN_REGISTRY = "channel_not_in_registry"
REASON_CHANNEL_MAPPING_CONFLICT = "channel_mapping_conflict"
REASON_MISSING_TIMESTAMP = "missing_timestamp"
REASON_UNPARSEABLE_TIMESTAMP = "unparseable_timestamp"
REASON_FINGERPRINT_MISMATCH = "fingerprint_mismatch"
#: 文件嵌在频道目录的子目录里 —— 真实语料里 0 个；出现即说明布局变了，
#: 必须让人看到，而不是按子目录名编造一个新频道（那会污染 `raw_id` 与行业归属）。
REASON_NESTED_LAYOUT = "nested_layout"
#: 文件直接躺在旧语料根下（没有频道目录）—— 无从判定行业归属，**不猜**。
REASON_NO_CHANNEL_DIR = "no_channel_directory"


class MigrateError(Exception):
    """**可记账**的导入失败：原因明确，调用方决定"记进对账表"还是"直接抛"。

    构造时带上出问题的文件（若已知）与机器可读的原因码，报告因此不需要从
    异常文本里反解原因。
    """

    #: 机器可读的原因码（见本模块的 `REASON_*` 常量）；子类可覆盖。
    reason = "migrate_error"

    def __init__(
        self, message: str, *, path: Path | None = None, reason: str | None = None
    ) -> None:
        where = "" if path is None else f"（{path}）"
        super().__init__(f"{message}{where}")
        self.path = path
        if reason is not None:
            self.reason = reason


class LegacyScanError(MigrateError):
    """扫描旧语料时单个文件不可用（JSON 坏 / 不是对象 / 字节不可解码）。"""

    reason = REASON_UNPARSEABLE_JSON


class TimestampError(MigrateError):
    """抓取时间不可用：字段缺失或无法解析。

    **绝不回退到"现在"**——那会把 9 个月前的文章伪装成刚采到的（§7.1：
    采集有时间价值，历史时间序不能乱）。宁可记一条失败。
    """

    reason = REASON_MISSING_TIMESTAMP


class ChannelMappingError(MigrateError):
    """渠道映射不可用：旧频道名没有对应注册表渠道，或对应多个。"""

    reason = REASON_CHANNEL_NOT_MAPPED


#: **可记账**的异常类型：`Migrate.run` 捕获它们并记一条失败；其它异常一律冒泡。
#: 这个元组是"`except` 只允许覆盖已知的数据问题"的具体化（硬规则 2）。
RUN_ERRORS: tuple[type, ...] = (LegacyScanError, TimestampError, ChannelMappingError)
