"""归一化文本层：字节 → 归一化文本 + 精确偏移映射（SPEC §2.2 / §2.10，T-104）。

本模块是**纯函数内核**：只接受 `bytes` / `str`，返回契约对象；
不读文件、不写文件、不碰数据库（不可变原文归档由 T-103 负责，本模块只产出可被它保存的结果）。

偏移映射的实现方式（关键）
-------------------------
**不使用** `str.find` 事后猜位置。文本与原文**逐段增量拼接**，同时记录段表
`segments: [(norm_start, raw_start, length, raw_length), ...]`，其中：

- `norm_start`  该段在归一化文本中的起点；
- `raw_start`   该段对应的原文起点（`raw_text` 的字符下标）；
- `length`      该段在归一化文本中的字符数；
- `raw_length`  该段消耗的原文字符数（折叠空白时 = 空白串全长；实体 = 实体字面量全长）。

`to_raw_offset(i)` 返回包含归一化偏移 `i` 的段的映射位置：

- 常规段（`raw_length >= length`）：`raw_start + (i - norm_start)` —— **逐字符精确**；
- 折叠空白：整段长度 1（或 2，用于空行），映射到**该空白串在原文中的起点**；
- 退化段（`raw_length == 0`，仅用于 HTML 块边界插入的**合成换行**）：整段恒定映射到插入点；
- 段内超出 `raw_length` 的位置被夹紧到 `raw_start + raw_length`（保证单调、不越界）。

`SegmentTable.__post_init__` **用代码校验单调性**，而不是在文档里承诺：

    对相邻两段 a、b：b.raw_start >= a.raw_start + min(a.length - 1, a.raw_length)

这条不变量保证 `to_raw_offset` 对递增输入返回不减的输出（判据 2），
也让「区间 [ns, ne) 的原文切片包含该区间首尾非空白字符」（判据 1）成为可证明的性质：
非空白字符永远落在 `raw_length >= length` 的常规段里，映射是精确的。
"""

from __future__ import annotations

import bisect
import codecs
import re
from dataclasses import dataclass
from typing import NamedTuple, Sequence

from pydantic import Field

from atlas.contracts import ContractModel, DerivedLocator
from atlas.contracts.anchors import OffsetMap

__all__ = [
    "CHARSET_RE",
    "HTML_MIME_TYPES",
    "META_CHARSET_RE",
    "Atom",
    "NormalizeError",
    "NormalizedText",
    "Segment",
    "SegmentTable",
    "assemble",
    "decode_bytes",
    "detect_encoding",
    "mime_type",
    "normalize",
    "plain_atoms",
]


class NormalizeError(Exception):
    """归一化层的显式失败（解码失败 / 段表不变量被破坏 / HTML 标记无法定位）。

    这里不吞异常：任何定位失败都必须响亮失败（CLAUDE.md 硬规则 2）。
    """


# --------------------------------------------------------------------------------------
# 字符集探测与解码
# --------------------------------------------------------------------------------------

CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:\-+]+)", re.IGNORECASE)

META_CHARSET_RE = re.compile(
    rb"""<meta[^>]{0,400}?charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-+]+)""",
    re.IGNORECASE | re.DOTALL,
)

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

# 允许的回退链：只用标准库编解码器，**不引入 chardet**（SPEC §2.10 零新依赖）。
_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030")

HTML_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml", "application/xhtml"})

_UNSUPPORTED_MIME: dict[str, str] = {
    "application/pdf": "PDF 解析未实现",
    "application/msword": "Office 二进制格式解析未实现",
    "application/epub+zip": "EPUB 解析未实现",
    "application/x-mobipocket-ebook": "MOBI 解析未实现",
}
_UNSUPPORTED_MIME_PREFIXES: tuple[str, ...] = (
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.oasis.opendocument",
    "application/vnd.ms-",
)


def mime_type(content_type: str) -> str:
    """从 `Content-Type` 头取出小写 MIME 类型（忽略参数）。"""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def detect_encoding(raw_bytes: bytes, content_type: str = "", *, is_html: bool = False) -> str | None:
    """按 BOM → `Content-Type` charset → `<meta charset>` 的顺序探测编码。

    返回 `None` 表示没有任何显式声明（调用方走回退链）。
    """
    for bom, enc in _BOMS:
        if raw_bytes.startswith(bom):
            return enc

    match = CHARSET_RE.search(content_type or "")
    if match:
        return match.group(1)

    if is_html or b"<meta" in raw_bytes[:4096].lower():
        head = raw_bytes[:4096]
        meta = META_CHARSET_RE.search(head)
        if meta:
            return meta.group(1).decode("ascii", errors="ignore") or None
    return None


def decode_bytes(raw_bytes: bytes, content_type: str = "", *, is_html: bool = False) -> tuple[str, str]:
    """原始字节 → `(raw_text, encoding)`。

    先按声明的编码严格解码；失败时按 `utf-8` → `gb18030` 回退链严格重试；
    全部失败则抛 `NormalizeError`（**不**用 `errors="replace"` 编造文本）。
    """
    declared = detect_encoding(raw_bytes, content_type, is_html=is_html)

    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    for enc in _FALLBACK_ENCODINGS:
        if enc.lower() not in {c.lower() for c in candidates}:
            candidates.append(enc)

    tried: list[str] = []
    for enc in candidates:
        try:
            return raw_bytes.decode(enc), enc
        except LookupError as exc:  # 未知字符集名：响亮失败，不静默降级
            raise NormalizeError(f"未知字符集 {enc!r}（来自 content_type={content_type!r}）") from exc
        except UnicodeDecodeError as exc:
            tried.append(f"{enc}@{exc.start}({exc.reason})")
    raise NormalizeError(
        "原始字节无法解码；已严格尝试 " + ", ".join(tried) + f"（共 {len(raw_bytes)} 字节）"
    )


# --------------------------------------------------------------------------------------
# 段表
# --------------------------------------------------------------------------------------


class Segment(ContractModel):
    """段表的一段：归一化区间 `[norm_start, norm_start+length)` ↔ 原文起点 `raw_start`。"""

    norm_start: int = Field(ge=0)
    raw_start: int = Field(ge=0)
    length: int = Field(gt=0)
    raw_length: int = Field(ge=0)

    @property
    def norm_end(self) -> int:
        return self.norm_start + self.length

    @property
    def raw_end(self) -> int:
        return self.raw_start + self.raw_length

    def map_offset(self, normalized_offset: int) -> int:
        """段内映射：`raw_length == 0` 的退化段恒定映射到插入点，其余线性并夹紧。"""
        if self.raw_length == 0:
            return self.raw_start
        return self.raw_start + min(normalized_offset - self.norm_start, self.raw_length)


@dataclass(frozen=True, slots=True)
class SegmentTable:
    """段表 + 偏移映射，满足 `atlas.contracts.anchors.OffsetMap`（`Callable[[int], int]`）。

    构造即校验（不变量在代码层强制）：
    归一化空间连续且完整覆盖 `[0, text_length)`；原文区间不越过 `raw_text_length`；
    相邻段满足单调性下界。
    """

    segments: tuple[Segment, ...]
    text_length: int
    raw_text_length: int
    _starts: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.text_length < 0:
            raise NormalizeError(f"text_length 不得为负：{self.text_length}")
        if self.raw_text_length < 0:
            raise NormalizeError(f"raw_text_length 不得为负：{self.raw_text_length}")

        starts: list[int] = []
        cursor = 0
        previous: Segment | None = None
        for seg in self.segments:
            if seg.norm_start != cursor:
                raise NormalizeError(
                    f"段表在归一化空间不连续：期望 norm_start={cursor}，实际 {seg.norm_start}"
                )
            if seg.raw_end > self.raw_text_length:
                raise NormalizeError(
                    f"段表越过原文末尾：raw_end={seg.raw_end} > raw_text_length={self.raw_text_length}"
                )
            if previous is not None:
                floor = previous.raw_start + min(previous.length - 1, previous.raw_length)
                if seg.raw_start < floor:
                    raise NormalizeError(
                        "段表违反偏移单调性："
                        f"seg.raw_start={seg.raw_start} < {floor}"
                        f"（上一段 norm=[{previous.norm_start},{previous.norm_end}) "
                        f"raw=[{previous.raw_start},{previous.raw_end})）"
                    )
            starts.append(seg.norm_start)
            cursor = seg.norm_end
            previous = seg

        if cursor != self.text_length:
            raise NormalizeError(
                f"段表未覆盖归一化文本：覆盖到 {cursor}，text_length={self.text_length}"
            )
        object.__setattr__(self, "_starts", tuple(starts))

    # -- 映射 ---------------------------------------------------------------------------

    def to_raw_offset(self, normalized_offset: int) -> int:
        """归一化偏移 → 原文偏移；越界抛 `IndexError`（不返回编造的值）。"""
        if isinstance(normalized_offset, bool) or not isinstance(normalized_offset, int):
            raise TypeError(f"归一化偏移必须是 int，得到 {type(normalized_offset).__name__}")
        if normalized_offset < 0 or normalized_offset > self.text_length:
            raise IndexError(
                f"归一化偏移越界：[0, {self.text_length}] 之外：{normalized_offset}"
            )
        if not self.segments:
            return 0
        if normalized_offset == self.text_length:
            return self.segments[-1].map_offset(normalized_offset)
        index = bisect.bisect_right(self._starts, normalized_offset) - 1
        return self.segments[index].map_offset(normalized_offset)

    def __call__(self, normalized_offset: int) -> int:
        return self.to_raw_offset(normalized_offset)

    def to_raw_range(self, start: int, end: int) -> tuple[int, int]:
        """归一化区间 `[start, end)` → 原文区间 `[raw_start, raw_end)`（同一映射）。"""
        if end < start:
            raise ValueError(f"非法区间：[{start}, {end})")
        return self.to_raw_offset(start), self.to_raw_offset(end)

    def is_monotonic(self) -> bool:
        """判据 2 的自检：对 `[0, text_length]` 逐点验证单调不减。"""
        previous = -1
        for offset in range(self.text_length + 1):
            current = self.to_raw_offset(offset)
            if current < previous:
                return False
            previous = current
        return True


# `to_raw_offset` 的契约签名（T-002 冻结）
_TO_RAW_OFFSET_ANNOTATION = OffsetMap


class NormalizedText(ContractModel):
    """归一化文本层的产出：文本 + 偏移映射 + 派生块。

    - `raw_text`：**解码后的原文**，是 `to_raw_offset` 返回值的参照系
      （例如 `raw_text[nt.to_raw_offset(3):nt.to_raw_offset(9)]`）。
    - `to_raw_offset`：`SegmentTable` 实例（`frozen` 契约字段，同时也是 `OffsetMap`）。
    - `blocks`：**派生物**，见 `atlas.normalize.blocks` —— 可重建，且**不得**作为人工产物的锚点。
    """

    text: str
    raw_text: str
    encoding: str
    content_type: str
    source_kind: str
    to_raw_offset: _TO_RAW_OFFSET_ANNOTATION
    blocks: tuple[DerivedLocator, ...] = ()

    @property
    def text_length(self) -> int:
        return len(self.text)

    @property
    def segments(self) -> tuple[Segment, ...]:
        """段表（`atlas.normalize` 的 `.map` 派生物即由此序列化）。"""
        return self.to_raw_offset.segments  # type: ignore[union-attr]

    def raw_slice(self, normalized_start: int, normalized_end: int) -> str:
        """归一化区间 → 对应的原文切片（判据 1 的实现依据）。"""
        start, end = self.to_raw_offset.to_raw_range(normalized_start, normalized_end)  # type: ignore[union-attr]
        return self.raw_text[start:end]

    def with_blocks(self, blocks: Sequence[DerivedLocator]) -> "NormalizedText":
        """附上派生块，返回**新**记录（契约记录不可就地修改）。"""
        return NormalizedText(
            text=self.text,
            raw_text=self.raw_text,
            encoding=self.encoding,
            content_type=self.content_type,
            source_kind=self.source_kind,
            to_raw_offset=self.to_raw_offset,
            blocks=tuple(blocks),
        )


# --------------------------------------------------------------------------------------
# 原子 → 段表（增量拼接）
# --------------------------------------------------------------------------------------


class Atom(NamedTuple):
    """归一化流水线的输入单位：`text` 来自原文 `[raw_start, raw_start+raw_length)`。

    `raw_length == 0` 表示**合成内容**（HTML 块边界插入的换行），原文中无对应字符。
    """

    text: str
    raw_start: int
    raw_length: int


class _Char(NamedTuple):
    ch: str
    raw_start: int
    next_start: int


class _Chunk(NamedTuple):
    text: str
    raw_start: int
    raw_length: int


def expand_atoms(atoms: Sequence[Atom]) -> list[_Char]:
    """把原子展开为逐字符项，并给出每个字符消耗的原文区间。"""
    chars: list[_Char] = []
    for atom in atoms:
        text = atom.text
        if not text:
            continue
        if atom.raw_length <= 0:
            chars.extend(_Char(ch, atom.raw_start, atom.raw_start) for ch in text)
        elif len(text) == 1:
            chars.append(_Char(text, atom.raw_start, atom.raw_start + atom.raw_length))
        else:
            for index, ch in enumerate(text):
                start = atom.raw_start + min(index, atom.raw_length - 1)
                end = atom.raw_start + min(index + 1, atom.raw_length)
                chars.append(_Char(ch, start, end))
    return chars


_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def _collapse_chars(chars: Sequence[_Char]) -> list[_Chunk]:
    """合并空白串：0 个换行 → 一个空格；1 个换行 → 一个换行；2+ 个换行 → 空行。

    每个空白串整体映射到**该空白串在原文中的起点**。
    """
    chunks: list[_Chunk] = []
    index = 0
    total = len(chars)
    while index < total:
        char = chars[index]
        if char.ch.isspace():
            end = index
            while end < total and chars[end].ch.isspace():
                end += 1
            run = chars[index:end]
            breaks = len(_LINE_BREAK_RE.findall("".join(c.ch for c in run)))
            raw_start = run[0].raw_start
            raw_length = max(run[-1].next_start - raw_start, 0)
            if breaks >= 2:
                chunks.append(_Chunk("\n\n", raw_start, raw_length))
            elif breaks == 1:
                chunks.append(_Chunk("\n", raw_start, raw_length))
            else:
                chunks.append(_Chunk(" ", raw_start, raw_length))
            index = end
        else:
            chunks.append(_Chunk(char.ch, char.raw_start, max(char.next_start - char.raw_start, 0)))
            index += 1
    return chunks


def _build_segments(chunks: Sequence[_Chunk]) -> list[Segment]:
    """块 → 段；只有「逐字符 1:1 且原文连续」的相邻块才合并，保证映射仍精确。"""
    segments: list[Segment] = []
    cursor = 0
    for chunk in chunks:
        last = segments[-1] if segments else None
        chunk_length = len(chunk.text)
        if (
            last is not None
            and last.length == last.raw_length
            and chunk_length == chunk.raw_length
            and last.raw_end == chunk.raw_start
        ):
            segments[-1] = Segment(
                norm_start=last.norm_start,
                raw_start=last.raw_start,
                length=last.length + chunk_length,
                raw_length=last.raw_length + chunk.raw_length,
            )
        else:
            segments.append(
                Segment(
                    norm_start=cursor,
                    raw_start=chunk.raw_start,
                    length=chunk_length,
                    raw_length=chunk.raw_length,
                )
            )
        cursor += chunk_length
    return segments


def assemble(
    atoms: Sequence[Atom],
    *,
    raw_text: str,
    encoding: str,
    content_type: str,
    source_kind: str,
) -> NormalizedText:
    """原子序列 → 归一化文本 + 段表（纯函数，HTML 与纯文本共用）。"""
    chunks = _collapse_chars(expand_atoms(atoms))
    while chunks and chunks[0].text.isspace():
        chunks.pop(0)
    while chunks and chunks[-1].text.isspace():
        chunks.pop()

    segments = _build_segments(chunks)
    text = "".join(chunk.text for chunk in chunks)

    if len(text) != sum(seg.length for seg in segments):
        raise NormalizeError("内部错误：段表长度与归一化文本长度不一致")

    table = SegmentTable(
        segments=tuple(segments),
        text_length=len(text),
        raw_text_length=len(raw_text),
    )
    return NormalizedText(
        text=text,
        raw_text=raw_text,
        encoding=encoding,
        content_type=content_type,
        source_kind=source_kind,
        to_raw_offset=table,
        blocks=(),
    )


# --------------------------------------------------------------------------------------
# 纯文本路径 + 入口
# --------------------------------------------------------------------------------------


def plain_atoms(raw_text: str) -> list[Atom]:
    """纯文本 → 原子：换行统一为 `\\n`，其余字符原样搬运（空白折叠交给 `assemble`）。"""
    atoms: list[Atom] = []
    index = 0
    total = len(raw_text)
    while index < total:
        ch = raw_text[index]
        if ch == "\r":
            if raw_text[index + 1 : index + 2] == "\n":
                atoms.append(Atom("\n", index, 2))
                index += 2
            else:
                atoms.append(Atom("\n", index, 1))
                index += 1
        elif ch.isspace():
            end = index
            while end < total and raw_text[end].isspace() and raw_text[end] != "\r":
                end += 1
            atoms.append(Atom(raw_text[index:end], index, end - index))
            index = end
        else:
            end = index
            while end < total and not raw_text[end].isspace():
                end += 1
            atoms.append(Atom(raw_text[index:end], index, end - index))
            index = end
    return atoms


def normalize_plain_text(
    raw_text: str,
    *,
    encoding: str = "utf-8",
    content_type: str = "text/plain",
) -> NormalizedText:
    """已解码文本 → `NormalizedText`（纯文本路径；`blocks` 为空，见 `blocks.attach_blocks`）。"""
    return assemble(
        plain_atoms(raw_text),
        raw_text=raw_text,
        encoding=encoding,
        content_type=content_type,
        source_kind="text",
    )


def _reject_unimplemented(raw_bytes: bytes, mime: str) -> None:
    reason = _UNSUPPORTED_MIME.get(mime)
    if reason is None:
        for prefix in _UNSUPPORTED_MIME_PREFIXES:
            if mime.startswith(prefix):
                reason = "Office/ODF 文档解析未实现"
                break
    if reason is None and raw_bytes[:5] == b"%PDF-":
        reason = "PDF 解析未实现"
    if reason is None and raw_bytes[:4] == b"PK\x03\x04":
        reason = "ZIP 容器（Office / EPUB）解析未实现"
    if reason is not None:
        raise NotImplementedError(
            f"{reason}（T-104 只覆盖纯文本与 HTML 文本层；content_type={mime!r}）"
        )


def normalize(raw_bytes: bytes, content_type: str = "") -> NormalizedText:
    """字节 → 归一化文本 + 偏移映射（HTML 先行；PDF/Office 显式 `NotImplementedError`）。

    返回值的 `blocks` 为空元组；T-104 的完整入口（含派生块）见 `atlas.normalize.normalize`。
    """
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise TypeError(f"raw_bytes 必须是 bytes-like，得到 {type(raw_bytes).__name__}")
    payload = bytes(raw_bytes)
    mime = mime_type(content_type)
    _reject_unimplemented(payload, mime)

    is_html = mime in HTML_MIME_TYPES
    if not is_html and not mime:
        # 真实抓取里 Content-Type 可能缺失（旧采集器/异常响应）。用结构嗅探兜底，
        # 否则 HTML 标签会被当成正文进入归一化文本。
        head = payload[:4096].lower()
        is_html = any(
            marker in head
            for marker in (b"<html", b"<body", b"<div", b"<p>", b"<br", b"<table", b"<!doctype html")
        )
    raw_text, encoding = decode_bytes(payload, content_type, is_html=is_html)

    if is_html:
        from .html import normalize_html  # 局部导入：html.py 依赖本模块，避免循环导入

        return normalize_html(
            raw_text, encoding=encoding, content_type=content_type or "text/html"
        )

    return normalize_plain_text(
        raw_text, encoding=encoding, content_type=content_type or "text/plain"
    )
