"""人工打标的导出 / 导入 / 备份（T-108，B7「数据一定要可流转」）。

**为什么必须有这个模块**：SPEC §2.3 把 Confirmed 层定义为**不可重建**的人工产物。
Raw 能从数据源重抓，归一化与 Proposed 能重算，人工标签**丢了就是丢了**。
因此"可导出 + 可回流"不是锦上添花的功能，而是这一层能不能被信任的前提
（§4.2 T-108 的验收词就是「导出与备份」）。

两条格式，同一份字段集（缺一不可）：

```
label_id / raw_id / label_key / label_value / actor / created_at / from_claim_id / anchor（若有）
```

- **JSON**：权威格式。结构化、带 `format_version`，用于备份与迁移。
- **CSV**：流转格式（表格工具可读可改）。锚点摊平成 4 列。

三条设计要点：

1. **导出可再导入，且逐字段等值**。`export_json()` 的产物被 `import_json()`
   读回后，每条 `ConfirmedLabel` 与原对象 `model_dump()` 完全一致
   （含 `created_at` 的时区、`from_claim_id`、`anchor`），**写入顺序也一致**
   （顺序是 `latest_value` 并列判定的输入，见 `_labels_of`）。测试对此有断言。
2. **导入是严格的**：字段缺失、格式版本不认识、`label_id` 与内容不符（被人手改过）
   一律抛 `atlas.contracts` 的领域异常，**不**跳过坏行、**不**补默认值。
   静默丢数据正是不可重建层最不能接受的失败模式。
3. **导入走 `store.add()`**，因此幂等：同一份导出重复导入不会产生重复行
   （`label_id` 是内容寻址的，重复 `add` 返回库里那条）。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from atlas.contracts import (
    ConfirmedLabel,
    IdError,
    InvalidContractStateError,
    VersionError,
    label_id_for,
)

__all__ = [
    "CSV_COLUMNS",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "backup",
    "dumps_json",
    "export_csv",
    "export_json",
    "import_csv",
    "import_json",
    "label_from_dict",
    "label_to_dict",
    "load_csv",
    "load_json",
    "restore",
    "verify_label_id",
]

#: 导出文件自识别标记（迁移到 PostgreSQL 时用来认领数据）。
EXPORT_FORMAT = "atlas.confirmed_labels"

#: 导出格式版本。字段集变化必须递增，导入侧对不认识的版本响亮失败。
EXPORT_FORMAT_VERSION = 1

#: CSV 表头（顺序即列序，稳定以便 diff）。
CSV_COLUMNS: Sequence[str] = (
    "label_id",
    "raw_id",
    "label_key",
    "label_value",
    "actor",
    "created_at",
    "from_claim_id",
    "anchor_raw_id",
    "anchor_raw_sha256",
    "anchor_char_start",
    "anchor_char_end",
)

_ANCHOR_COLUMNS = CSV_COLUMNS[7:]

#: 接受"导出源"的两种形态：一串标签，或任何带 `all_labels()` 的 store。
LabelSource = Union[Iterable[ConfirmedLabel], object]

_LabelSink = Callable[[ConfirmedLabel], ConfirmedLabel]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _labels_of(source: LabelSource) -> List[ConfirmedLabel]:
    """把导出源统一成标签序列。

    - 序列 / 迭代器 → 原样使用；
    - 带 `all_labels()` 的 store（本包的 `SqliteConfirmedStore` / `LabelStore`）→ 调它
      （它按写入顺序读出）。

    **顺序必须保序，不排序**：写入顺序是语义的一部分 —— `created_at` 并列时
    `latest_value` 取**先写入**的那条。若导出按 `label_id` 排序，备份回流后的
    并列语义就可能翻转，那这份备份就**不等价**于原库了。
    保序同时让"同一库连续两次导出"逐字节相同（追加写 → 末尾追加行，diff 干净）。
    """
    if isinstance(source, (str, bytes, Mapping)) or isinstance(source, ConfirmedLabel):
        raise InvalidContractStateError(
            f"导出源必须是一串 ConfirmedLabel 或带 all_labels() 的 store，"
            f"收到 {type(source).__name__}"
        )
    if hasattr(source, "all_labels"):
        labels = list(source.all_labels())  # type: ignore[attr-defined]
    else:
        try:
            labels = list(source)  # type: ignore[arg-type]
        except TypeError as exc:
            raise InvalidContractStateError(
                f"导出源既不是 store 也不可迭代：{type(source).__name__}（{exc}）"
            ) from exc
    for label in labels:
        if not isinstance(label, ConfirmedLabel):
            raise InvalidContractStateError(
                f"导出源里混入了非 ConfirmedLabel 元素：{type(label).__name__}"
            )
        # 出口也校验一次：导出的东西必须能原样回来（回流时同样会校验）。
        verify_label_id(label)
    return labels


# --------------------------------------------------------------------------- #
# 记录 ↔ 字典
# --------------------------------------------------------------------------- #
def label_to_dict(label: ConfirmedLabel) -> Dict[str, Any]:
    """一条标签 → 可 JSON 化的完整字典（字段集见模块 docstring）。"""
    anchor = label.anchor
    return {
        "label_id": label.label_id,
        "raw_id": label.raw_id,
        "label_key": label.label_key,
        "label_value": label.label_value,
        "actor": label.actor,
        "created_at": label.created_at.isoformat(),
        "from_claim_id": label.from_claim_id,
        "anchor": (
            None
            if anchor is None
            else {
                "raw_id": anchor.raw_id,
                "raw_sha256": anchor.raw_sha256,
                "char_start": anchor.char_start,
                "char_end": anchor.char_end,
            }
        ),
    }


def verify_label_id(label: ConfirmedLabel) -> None:
    """确认 `label_id` 与内容一致（内容寻址，SPEC §4.1 T-002 的 ID 策略）。

    导出文件是**可被人工编辑**的载体。若 `label_id` 与内容对不上，
    回流后库里就会出现"标识与内容不符"的行 —— 那比丢一条更糟（后续无法去重）。
    """
    expected = label_id_for(label.raw_id, label.label_key, label.label_value, label.actor)
    if label.label_id != expected:
        raise IdError(
            f"label_id 与内容不符：声明 {label.label_id}，按 (raw_id, label_key, "
            f"label_value, actor) 重算应为 {expected} —— 拒绝导入被改写过的记录"
        )


def label_from_dict(payload: Mapping[str, Any]) -> ConfirmedLabel:
    """字典 → 契约记录（严格：缺字段即抛，不补默认值）。"""
    if not isinstance(payload, Mapping):
        raise InvalidContractStateError(f"标签条目必须是对象，收到 {type(payload).__name__}")
    required = (
        "label_id",
        "raw_id",
        "label_key",
        "label_value",
        "actor",
        "created_at",
        "from_claim_id",
        "anchor",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise InvalidContractStateError(f"标签条目缺字段：{sorted(missing)}")
    label = ConfirmedLabel.model_validate(
        {
            "label_id": payload["label_id"],
            "raw_id": payload["raw_id"],
            "label_key": payload["label_key"],
            "label_value": payload["label_value"],
            "actor": payload["actor"],
            "created_at": payload["created_at"],
            "from_claim_id": payload["from_claim_id"],
            "anchor": payload["anchor"],
        }
    )
    verify_label_id(label)
    return label


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def dumps_json(source: LabelSource, *, exported_at: Optional[datetime] = None) -> str:
    """导出为 JSON 文本（不落盘，便于嵌入备份包或走 HTTP 下载）。"""
    labels = _labels_of(source)
    document = {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": (exported_at or _utcnow()).isoformat(),
        "count": len(labels),
        "labels": [label_to_dict(label) for label in labels],
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def export_json(
    source: LabelSource,
    path: str | Path,
    *,
    exported_at: Optional[datetime] = None,
) -> Path:
    """导出 JSON 到 `path`（先写临时文件再原子替换，避免半截备份文件）。"""
    target = Path(path)
    _ensure_parent(target)
    _atomic_write(target, dumps_json(source, exported_at=exported_at))
    return target


def _document_labels(document: Any) -> List[Any]:
    """校验导出文档的外壳，取出 `labels` 列表。"""
    if not isinstance(document, Mapping):
        raise InvalidContractStateError(
            f"导出文件顶层必须是对象，收到 {type(document).__name__}"
        )
    fmt = document.get("format")
    if fmt != EXPORT_FORMAT:
        raise InvalidContractStateError(
            f"不是 Atlas 人工标签导出文件：format={fmt!r}，预期 {EXPORT_FORMAT!r}"
        )
    version = document.get("format_version")
    if version != EXPORT_FORMAT_VERSION:
        raise VersionError(
            f"导出文件的 format_version={version!r}，本代码只认 "
            f"{EXPORT_FORMAT_VERSION}；需要显式迁移，不做静默兼容"
        )
    labels = document.get("labels")
    if not isinstance(labels, list):
        raise InvalidContractStateError(
            f"导出文件的 labels 必须是数组，收到 {type(labels).__name__}"
        )
    declared = document.get("count")
    if declared != len(labels):
        raise InvalidContractStateError(
            f"导出文件自述 count={declared!r}，实际有 {len(labels)} 条 —— 文件已损坏"
        )
    return labels


def load_json(path: str | Path) -> List[ConfirmedLabel]:
    """读取 JSON 导出文件 → 标签列表（按文件内顺序）。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidContractStateError(f"{path} 不是合法 JSON：{exc}") from exc
    return [label_from_dict(item) for item in _document_labels(document)]


def import_json(store: Any, path: str | Path) -> int:
    """把 JSON 导出文件回流到 `store`（幂等）；返回处理的记录条数。"""
    sink = _sink_of(store)
    labels = load_json(path)
    for label in labels:
        sink(label)
    return len(labels)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def _csv_row(label: ConfirmedLabel) -> Dict[str, Any]:
    anchor = label.anchor
    return {
        "label_id": label.label_id,
        "raw_id": label.raw_id,
        "label_key": label.label_key,
        "label_value": label.label_value,
        "actor": label.actor,
        "created_at": label.created_at.isoformat(),
        "from_claim_id": label.from_claim_id or "",
        "anchor_raw_id": "" if anchor is None else anchor.raw_id,
        "anchor_raw_sha256": "" if anchor is None else anchor.raw_sha256,
        "anchor_char_start": "" if anchor is None else anchor.char_start,
        "anchor_char_end": "" if anchor is None else anchor.char_end,
    }


def export_csv(source: LabelSource, path: str | Path) -> Path:
    """导出 CSV（表格工具可直接阅读/编辑；回流用 `import_csv`）。"""
    target = Path(path)
    _ensure_parent(target)
    labels = _labels_of(source)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for label in labels:
        writer.writerow(_csv_row(label))
    _atomic_write(target, buffer.getvalue())
    return target


def load_csv(path: str | Path) -> List[ConfirmedLabel]:
    """读取 CSV 导出文件 → 标签列表。"""
    text = Path(path).read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames or []
    missing = [column for column in CSV_COLUMNS if column not in fieldnames]
    if missing:
        raise InvalidContractStateError(f"{path} 的 CSV 表头缺列：{missing}")
    labels: List[ConfirmedLabel] = []
    for line_no, row in enumerate(reader, start=2):
        labels.append(_label_from_csv_row(row, path=path, line_no=line_no))
    return labels


def _label_from_csv_row(row: Mapping[str, Any], *, path: str | Path, line_no: int) -> ConfirmedLabel:
    where = f"{path}:{line_no}"
    present = [column for column in _ANCHOR_COLUMNS if str(row.get(column) or "").strip()]
    if present and len(present) != len(_ANCHOR_COLUMNS):
        raise InvalidContractStateError(
            f"{where} 的锚点列不完整（只有 {present}）—— 不允许半截锚点"
        )
    anchor: Optional[Dict[str, Any]] = None
    if present:
        try:
            char_start = int(str(row["anchor_char_start"]))
            char_end = int(str(row["anchor_char_end"]))
        except ValueError as exc:
            raise InvalidContractStateError(f"{where} 的锚点区间不是整数：{exc}") from exc
        anchor = {
            "raw_id": str(row["anchor_raw_id"]).strip(),
            "raw_sha256": str(row["anchor_raw_sha256"]).strip(),
            "char_start": char_start,
            "char_end": char_end,
        }
    from_claim = str(row.get("from_claim_id") or "").strip()
    payload = {
        "label_id": str(row.get("label_id") or "").strip(),
        "raw_id": str(row.get("raw_id") or "").strip(),
        "label_key": str(row.get("label_key") or "").strip(),
        "label_value": "" if row.get("label_value") is None else str(row["label_value"]),
        "actor": str(row.get("actor") or "").strip(),
        "created_at": str(row.get("created_at") or "").strip(),
        "from_claim_id": from_claim or None,
        "anchor": anchor,
    }
    return label_from_dict(payload)


def import_csv(store: Any, path: str | Path) -> int:
    """把 CSV 导出文件回流到 `store`（幂等）；返回处理的记录条数。"""
    sink = _sink_of(store)
    labels = load_csv(path)
    for label in labels:
        sink(label)
    return len(labels)


# --------------------------------------------------------------------------- #
# 备份 / 恢复
# --------------------------------------------------------------------------- #
def backup(
    source: LabelSource,
    directory: str | Path,
    *,
    stem: str = "confirmed_labels",
    now: Optional[datetime] = None,
) -> Path:
    """把当前全部人工标签备份成 `directory/<stem>-<UTC 时间戳>.json`，返回备份路径。

    用 UTC 时间戳命名，因此重复备份不会互相覆盖（备份是不可重建层唯一的退路）。
    """
    stamp = (now or _utcnow()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(directory) / f"{stem}-{stamp}.json"
    return export_json(source, target, exported_at=now)


def restore(store: Any, path: str | Path) -> int:
    """从备份文件回流（按扩展名分派 `.json` / `.csv`）；返回处理的记录条数。"""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".json":
        return import_json(store, target)
    if suffix == ".csv":
        return import_csv(store, target)
    raise InvalidContractStateError(
        f"不认识的备份格式 {suffix!r}（只支持 .json / .csv）：{target}"
    )


def _sink_of(store: Any) -> _LabelSink:
    """取出 store 的 `add`（本包与 T-002 契约的 store 都有）。"""
    sink = getattr(store, "add", None)
    if not callable(sink):
        raise InvalidContractStateError(
            f"导入目标必须提供 add(label)（atlas.contracts.ConfirmedStore 的契约方法），"
            f"收到 {type(store).__name__}"
        )
    return sink  # type: ignore[no-any-return]


def _ensure_parent(target: Path) -> None:
    parent = target.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)


def _atomic_write(target: Path, text: str) -> None:
    """先写 `<name>.tmp` 再 `replace`：读者永远看不到半截导出文件。"""
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
