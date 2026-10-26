"""T-131 测试共用的小工具（**纯 stdlib**，无 pytest fixture 魔法）。

为什么单独一个模块而不是在测试里重复
------------------------------------

三个测试文件（判据 / 防线 / 真实数据）都要造"旧语料目录"，并且都要做同一件事：
**整树 sha256**（证明 `data/raw/` 一个字节都没被改，硬规则 1 / 判据 13）。
把这两件事放一处，避免三份实现悄悄分叉（归档基线里"3 份重复去重"就是这么来的）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "LEGACY_TEMPLATE",
    "directory_stats",
    "fake_channels",
    "legacy_record",
    "record_dirs",
    "tree_digest",
    "utc",
    "write_legacy",
    "write_raw_bytes",
]

#: 一条真实形状的旧记录（字段取自实测：`data/raw/<频道>/*.json`）。
LEGACY_TEMPLATE: Dict[str, Any] = {
    "id": "00000000-0000-0000-0000-000000000000",
    "source_id": "unknown",
    "source_url": "https://example.invalid/article/1",
    "source_type": "rss_feed",
    "document_type": "rss",
    "raw_content": "body text",
    "title": "title",
    "collected_at": "2025-12-21T11:41:02.772123",
    "created_at": "2025-12-21T11:41:02.772099",
    "stored_at": "2025-12-21T11:41:02.772193",
    "updated_at": "2025-12-21T11:41:02.772100",
    "published_at": None,
}


def legacy_record(**overrides: Any) -> Dict[str, Any]:
    """造一条旧记录；`overrides` 里给 `None` 表示**删掉该字段**（模拟字段缺失）。"""
    record = dict(LEGACY_TEMPLATE)
    for key, value in overrides.items():
        if value is None and key in record:
            del record[key]
        else:
            record[key] = value
    return record


def write_legacy(
    root: Path,
    channel: str,
    name: str,
    payload: Mapping[str, Any],
    *,
    subdir: str = "",
) -> Path:
    """把一条记录写进 `<root>/<channel>/<subdir>/<name>`，返回文件路径。"""
    directory = root / channel
    if subdir:
        directory = directory / subdir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def write_raw_bytes(root: Path, channel: str, name: str, payload: bytes) -> Path:
    """写**原始字节**（用于构造"不是合法 UTF-8"这类文件）。"""
    directory = root / channel
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(payload)
    return path


def tree_digest(root: Path) -> str:
    """整树 sha256（逐文件：相对路径 + 文件内容指纹），与 T-205 的做法一致。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


def directory_stats(root: Path) -> Tuple[int, int]:
    """`(文件数, 字节数)`；根不存在时返回 `(0, 0)`（不抛错，便于 before/after 对比）。"""
    if not root.is_dir():
        return 0, 0
    files = 0
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files += 1
            total += path.stat().st_size
    return files, total


def record_dirs(root: Path) -> List[str]:
    """`root` 下的记录目录名（排序）——`data/store/raw/<raw_id>/` 的形态。"""
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def utc(*args: int) -> datetime:
    """便捷构造 aware UTC 时间（测试里写期望值用）。"""
    return datetime(*args, tzinfo=timezone.utc)  # type: ignore[arg-type]


def fake_channels(spec: Iterable[Tuple[str, str]]) -> List[Any]:
    """造 `build_channel_map` 需要的鸭子类型渠道对象（`id` + `industry_id`）。"""

    class _Channel:
        def __init__(self, channel_id: str, industry_id: str) -> None:
            self.id = channel_id
            self.industry_id = industry_id

    return [_Channel(channel_id, industry_id) for channel_id, industry_id in spec]
