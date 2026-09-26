"""旧记录的抓取时间 → **aware UTC datetime**（绝不编造时间）。

为什么要专门一个模块
--------------------

§7.1 的判据是"**采集有时间价值——分析可补做，历史补不回来**"。因此导入必须保留
旧记录**自己的**抓取时间：

- **不得**用"现在"当 `fetched_at` —— 那会把 9 个月前的文章伪装成刚采到的，
  直接破坏 §1.5 feed 的时间序（`fetched_at` 是排序键的第一维，见 §2.13）。
- **不得**在解析失败时回退到任何一个"看起来合理"的值 —— 宁可**响亮失败**，
  让那条记录进对账表的失败栏（硬规则 2）。

字段优先级（实测口径）
----------------------

旧记录里同时有 `collected_at` / `created_at` / `stored_at` / `updated_at` 四个
时间戳（实测 474 篇有正文的记录**四个字段全部非空**），而 `published_at` 恒为
`null`（0/474）。它们的语义：

| 字段 | 语义 | 用不用 |
|---|---|---|
| `collected_at` | **抓取时刻**（采集器写完记录时打的时间） | ✅ 首选 —— 就是 `fetched_at` |
| `created_at` | 记录创建时刻（比 `collected_at` 早几十微秒） | 回退 |
| `stored_at` | 落库时刻（比 `collected_at` 晚几十微秒） | 回退 |
| `updated_at` | 记录更新时刻 | 回退（最后手段） |
| `published_at` | **文章发布时间** | ❌ 语义不同（是内容时间，不是抓取时间），实测恒空，不用 |

**这个顺序是量出来的，不是猜的**：实测首条记录
`created_at=…02.772099` < `collected_at=…02.772123` < `stored_at=…02.772193`。

时区规则（与 T-106 一致，写死一处）
-----------------------------------

旧时间戳是**无时区的**本地串（`2025-12-21T11:41:02.772123`）。项目里已有明文规则：
"**naive 按 UTC 解释**"（`atlas.feed.query._ensure_aware` 与 §2.15 的索引时间键）。
本模块沿用同一条规则，并顺手把带偏移的时间戳**归一化成 UTC**，
使 `raw_records.fetched_at` 里读回来的比较恒为 aware 对 aware。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Tuple

from .errors import REASON_MISSING_TIMESTAMP, REASON_UNPARSEABLE_TIMESTAMP, TimestampError

__all__ = ["MOMENT_FIELDS", "parse_legacy_moment", "pick_moment_field"]

#: 旧记录里抓取时间的字段优先级（**实测口径**，见模块 docstring）。第一个非空者胜出。
MOMENT_FIELDS: Tuple[str, ...] = ("collected_at", "created_at", "stored_at", "updated_at")


def _looks_like_moment(value: object) -> bool:
    """`datetime` 实例与形如 ISO-8601 的字符串都接受；其余一律视为"缺失"。"""
    if isinstance(value, datetime):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    return False


def pick_moment_field(payload: Mapping[str, object]) -> Optional[Tuple[str, str]]:
    """按 `MOMENT_FIELDS` 的优先级挑出 `(字段名, 值)`；都没有则返回 `None`。

    返回字段名是为了让报告能说清"这条记录的时间来自哪个字段"（可审计），
    而不是只留一个孤立的时间。
    """
    for field in MOMENT_FIELDS:
        value = payload.get(field)
        if isinstance(value, datetime):
            return field, value.isoformat()
        if _looks_like_moment(value):
            return field, str(value).strip()
    return None


def parse_legacy_moment(field: str, value: object, *, path: Optional[Path] = None) -> datetime:
    """把旧时间字段解释成 **aware UTC** datetime。

    - `datetime` 实例：naive 按 UTC、aware 转 UTC；
    - 字符串：`Z` 结尾按 UTC 处理，其余走 `datetime.fromisoformat`；
    - **缺失** → `TimestampError(reason=missing_timestamp)`；
    - **解析不了** → `TimestampError(reason=unparseable_timestamp)`。

    两种失败都**不返回任何回退值**。
    """
    where = path
    if isinstance(value, datetime):
        return _to_utc(value)
    if not isinstance(value, str) or not value.strip():
        raise TimestampError(
            f"抓取时间字段 {field!r} 缺失或为空（可用字段：{', '.join(MOMENT_FIELDS)}）",
            path=where,
            reason=REASON_MISSING_TIMESTAMP,
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimestampError(
            f"抓取时间字段 {field!r} 无法解析为 ISO-8601：{text!r}（{exc}）",
            path=where,
            reason=REASON_UNPARSEABLE_TIMESTAMP,
        ) from exc
    return _to_utc(parsed)


def _to_utc(moment: datetime) -> datetime:
    """naive 按 UTC 解释（项目既有规则），aware 归一化到 UTC。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
