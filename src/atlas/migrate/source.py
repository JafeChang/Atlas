"""只读扫描旧语料 `data/raw/**/*.json`（绝不写入）。

扫描口径
--------

- **`rglob("*.json")`**：`data/raw/` 下既有真频道目录，也有垃圾目录
  （`indexes` / `processed` / `raw` / `temp` 实测 0 个 JSON，`test` 1 个非文档产物）。
  本模块**不做目录白名单过滤**——每个扫到的文件都必须进入对账表，
  否则"垃圾目录被静默跳过"就不可审计。真正的过滤发生在导入层（按 `document_type`
  与 `raw_content` 判定），原因逐条记录。
- 文件按**相对路径字典序**处理，因此对账表与"哪一条重复先到"都是确定的、可复现的。
- 解析失败或字节不可解码 → `LegacyScanError`（带原因码），调用方记一条失败，**不静默跳过**。

`raw_content` 是旧系统里正文所在的字段（实测 474 篇非空的记录全部有它），
`source_url` 是**文章地址**（不是 feed 地址）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Mapping, Optional, Tuple

from .errors import (
    REASON_NOT_AN_OBJECT,
    REASON_UNPARSEABLE_JSON,
    REASON_UNREADABLE_BYTES,
    SKIP_EMPTY_CONTENT,
    SKIP_NON_DOCUMENT,
    LegacyScanError,
)

__all__ = [
    "CONTENT_FIELD",
    "DOCUMENT_TYPE_FIELD",
    "URL_FIELD",
    "LegacyDocument",
    "LegacyFile",
    "LegacyJson",
    "classify_legacy",
    "iter_legacy_files",
    "nested_relative_paths",
    "scan_legacy_files",
    "to_document",
]

#: 正文所在字段（旧系统实测口径）。
CONTENT_FIELD = "raw_content"
#: 文档类型字段；**缺失**表示该文件不是文档产物。
DOCUMENT_TYPE_FIELD = "document_type"
#: 文章地址字段（用作新 Raw 的 `endpoint`）。
URL_FIELD = "source_url"

#: 旧语料里"正文的编码"。旧系统写出的是 UTF-8 JSON。
CONTENT_ENCODING = "utf-8"


@dataclass(frozen=True)
class LegacyFile:
    """扫描到的**一个** JSON 文件（还没判定是不是文档）。"""

    path: Path
    #: 频道名 = `data/raw/<频道>/` 的**第一段目录名**。
    #:
    #: **实测口径**：534 个 JSON 全部位于"频道目录/文件.json"（`depth_under_channel == 1`），
    #: 没有任何一个嵌在更深层，因此"第一段"与"直接父目录"在真实语料上**完全一致**，
    #: 本判据不改变任何一个真实文件的频道归属。
    #: 取第一段而不是父目录名，是为了让"旧语料真的出现了嵌套"时**响亮的失败**（见
    #: `import_guard`），而不是安静地按子目录名编造一个新频道。
    channel: str
    #: 相对频道目录的路径深度（直接位于频道目录下的文件为 1）。
    depth_under_channel: int
    #: 相对旧语料根的路径（报告与对账表里的稳定标识）。
    relative: str

    @property
    def nested(self) -> bool:
        """`True` 表示这个文件嵌在频道目录的子目录里（真实语料里为 0 个）。"""
        return self.depth_under_channel != 1


@dataclass(frozen=True)
class LegacyJson:
    """扫描到的文件 + 解析结果（二选一：`payload` 或 `error`）。"""

    file: LegacyFile
    payload: Optional[Mapping[str, object]]
    error: Optional[LegacyScanError]


@dataclass(frozen=True)
class LegacyDocument:
    """**逐篇文章**：旧系统记录到新 Raw 输入的全部信息。

    一篇文章 = 一份 JSON（旧系统按篇存储）。`content` 是**原始字节**：
    旧系统的 `raw_content` 经 UTF-8 编码 —— 这是新 Raw 的字节本体。
    """

    raw_id_legacy: str
    channel: str
    url: str
    content: bytes
    #: 旧记录里的时间字段（原样保留 `(字段名, 值)`，供 `moment.parse_legacy_moment` 解释）。
    moment_field: Tuple[str, str]
    source_path: Path
    relative: str

    @property
    def content_length(self) -> int:
        return len(self.content)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - root 一定是 path 的祖先
        return path.as_posix()


def iter_legacy_files(root: Path) -> Iterator[LegacyFile]:
    """按相对路径字典序遍历 `root` 下的全部 `*.json`（**只读**）。"""
    base = Path(root)
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*.json")):
        if not path.is_file():
            continue
        relative = _relative(path, base)
        parts = relative.split("/")
        # 频道 = 第一段目录名；直接位于根下的文件没有频道可言（用 "" 表示，导入时失败）。
        channel = parts[0] if len(parts) > 1 else ""
        yield LegacyFile(
            path=path,
            channel=channel,
            depth_under_channel=len(parts) - 1,
            relative=relative,
        )


def scan_legacy_files(root: Path) -> List[LegacyJson]:
    """扫描并解析全部 `*.json`；解析失败成为**带原因的条目**，不是异常。

    返回顺序 = 相对路径字典序（确定、可复现）。
    """
    out: List[LegacyJson] = []
    for entry in iter_legacy_files(root):
        out.append(_load(entry))
    return out


def nested_relative_paths(root: Path) -> List[str]:
    """嵌在频道目录子目录里的文件（相对路径，排序）。

    **实测为 0**（534/534 都直接位于频道目录下）。本函数存在的意义是让"布局变了"
    变成一条**可测量的**事实，而不是靠"第一段目录名"的启发式安静地猜过去。
    """
    return [entry.relative for entry in iter_legacy_files(root) if entry.nested]


def _load(entry: LegacyFile) -> LegacyJson:
    try:
        raw = entry.path.read_bytes()
    except OSError as exc:  # 权限 / 消失的文件：可记账，不吞
        return LegacyJson(
            file=entry,
            payload=None,
            error=LegacyScanError(
                f"读取失败：{exc.__class__.__name__}: {exc}",
                path=entry.path,
            ),
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return LegacyJson(
            file=entry,
            payload=None,
            error=LegacyScanError(
                f"字节不是合法 UTF-8：{exc}", path=entry.path, reason=REASON_UNREADABLE_BYTES
            ),
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return LegacyJson(
            file=entry,
            payload=None,
            error=LegacyScanError(
                f"JSON 解析失败：{exc}", path=entry.path, reason=REASON_UNPARSEABLE_JSON
            ),
        )
    if not isinstance(payload, dict):
        return LegacyJson(
            file=entry,
            payload=None,
            error=LegacyScanError(
                f"顶层不是 JSON 对象，而是 {type(payload).__name__}",
                path=entry.path,
                reason=REASON_NOT_AN_OBJECT,
            ),
        )
    return LegacyJson(file=entry, payload=payload, error=None)


def classify_legacy(payload: Mapping[str, object]) -> Optional[str]:
    """判定一份旧 JSON 是**文档**还是**跳过**，返回 `None` 表示是文档。

    判定顺序（原因必须能对上账）：

    1. `document_type` 缺失/空 → `SKIP_NON_DOCUMENT`
       （实测 60 个：`empty_*.json` / `summary_*.json`，旧系统的非文档产物，§6.2 明确点过名）
    2. `raw_content` 不是非空字符串 → `SKIP_EMPTY_CONTENT`
       （有 `document_type` 却没有正文 = 空抓取，没有可归档的字节）
    """
    document_type = payload.get(DOCUMENT_TYPE_FIELD)
    if not isinstance(document_type, str) or not document_type.strip():
        return SKIP_NON_DOCUMENT
    content = payload.get(CONTENT_FIELD)
    if not isinstance(content, str) or not content.strip():
        return SKIP_EMPTY_CONTENT
    return None


def to_document(entry: LegacyJson, moment_field: Tuple[str, str]) -> LegacyDocument:
    """把已解析的旧记录变成 `LegacyDocument`（**假定** `classify_legacy` 已判定是文档）。

    时间字段的选择（优先级与回退）由 `moment.pick_moment_field` 决定，本函数只承载结果。
    调用方必须先判定是文档，否则这里会拿到空的 `raw_content` —— 因此本函数在
    `raw_content` 不是非空字符串时**响亮失败**（`AssertionError` 是不对的，
    这里用 `LegacyScanError`，因为它是可记账的数据问题）。
    """
    payload = entry.payload
    if payload is None:  # pragma: no cover - 调用方保证
        raise LegacyScanError("payload 为空，不能转成文档", path=entry.file.path)
    content = payload.get(CONTENT_FIELD)
    if not isinstance(content, str) or not content.strip():
        raise LegacyScanError(
            f"{CONTENT_FIELD} 不是非空字符串，不能转成文档", path=entry.file.path
        )
    url = payload.get(URL_FIELD)
    if not isinstance(url, str) or not url.strip():
        # 没有文章地址 ⇒ 无法构造逐篇 raw_id。用文件路径兜底会让 raw_id
        # 静默变成"按文件"而不是"按篇"，与本任务的目的相反 ⇒ 响亮失败。
        raise LegacyScanError(
            f"{URL_FIELD} 缺失或为空，无法构造逐篇 raw_id", path=entry.file.path
        )
    raw_id_legacy = payload.get("id")
    return LegacyDocument(
        raw_id_legacy=raw_id_legacy if isinstance(raw_id_legacy, str) else "",
        channel=entry.file.channel,
        url=url,
        content=content.encode(CONTENT_ENCODING),
        moment_field=moment_field,
        source_path=entry.file.path,
        relative=entry.file.relative,
    )
