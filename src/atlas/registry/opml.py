"""OPML 导入导出（SPEC §2.9「目录交换」）。

用 **stdlib `xml.etree.ElementTree`**，不引入任何新依赖。

两条硬约束：

1. **导入必须走同一套校验，不得有旁路**：解析出的每条记录都经
   `Channel(**payload)` / `Industry(**payload)` 构造（单对象校验），
   再统一交给 `ConfigStore.commit()`（跨对象校验 + 版本化）。
   OPML 路径没有任何"直接塞进配置"的捷径。
2. **不静默降级**：外部 OPML 缺少 `interval_seconds` 之类的必填信息时，
   不是替你猜一个值，而是要求调用方**显式**给出默认值，否则拒绝导入。

格式约定
--------

- 标准 OPML 字段照旧使用：`text` / `title` / `type` / `xmlUrl`。
  `type="rss"` + `xmlUrl` 这一类外部清单（`awesome-rss-feeds` 等）可直接导入。
- Atlas 自有字段用 **`atlas-` 前缀的普通属性**（不是 XML namespace），
  这样 `xml.etree` 无需命名空间登记即可读写，文件对普通 OPML 阅读器也仍然合法：
  `atlas-object` / `atlas-id` / `atlas-industry-id` / `atlas-type` / `atlas-endpoint` /
  `atlas-fetch-spec`（JSON）/ `atlas-interval-seconds` / `atlas-rate-limit-seconds` /
  `atlas-user-agent` / `atlas-user-agent-justification` / `atlas-enabled` / `atlas-tags`。
- 行业以**分组 outline** 形式承载（嵌套渠道），导出即完整目录；导入可复原同一份配置。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from pydantic import Field

from atlas.contracts import (
    ContractModel,
    IdError,
    InvalidContractStateError,
)

from .schema import (
    MIN_INTERVAL_SECONDS,
    Channel,
    FetchType,
    Industry,
    validate_id,
)
from .versioning import ChangeKind, ConfigStore, ConfigVersion, RegistryMutation

__all__ = [
    "OPML_ATLAS_NAMESPACE_NOTE",
    "ATLAS_ATTR_PREFIX",
    "OpmlDocument",
    "OpmlOutline",
    "channels_to_opml",
    "registry_to_opml",
    "parse_opml",
    "import_opml",
]

ATLAS_ATTR_PREFIX = "atlas-"

OPML_ATLAS_NAMESPACE_NOTE = (
    "Atlas 自有字段使用 'atlas-' 前缀的普通 OPML 属性（非 XML namespace），"
    "以保持文件对通用 OPML 阅读器合法，并避免 xml.etree 的命名空间登记负担。"
)

_TRUE = frozenset({"true", "1", "yes"})
_FALSE = frozenset({"false", "0", "no"})

_SLUG_CHARS = re.compile(r"[a-z0-9]+")


# --- 导出 ---------------------------------------------------------------------


def _attr(name: str) -> str:
    return f"{ATLAS_ATTR_PREFIX}{name}"


def _outline_attributes(record: Channel) -> Dict[str, str]:
    attrs: Dict[str, str] = {
        # 标准 OPML 字段：让外部阅读器仍能识别这是一个 feed
        "text": record.id,
        "title": record.id,
        "type": record.type.value if record.type in (FetchType.RSS, FetchType.ATOM) else "link",
        _attr("object"): "channel",
        _attr("id"): record.id,
        _attr("industry-id"): record.industry_id,
        _attr("type"): record.type.value,
        _attr("endpoint"): record.endpoint,
        _attr("fetch-spec"): json.dumps(
            record.fetch_spec.payload(), sort_keys=True, ensure_ascii=False
        ),
        _attr("interval-seconds"): str(record.interval_seconds),
        _attr("enabled"): "true" if record.enabled else "false",
    }
    if record.rate_limit_seconds is not None:
        attrs[_attr("rate-limit-seconds")] = str(record.rate_limit_seconds)
    if record.user_agent is not None:
        attrs[_attr("user-agent")] = record.user_agent
    if record.user_agent_justification is not None:
        attrs[_attr("user-agent-justification")] = record.user_agent_justification
    if record.tags:
        attrs[_attr("tags")] = ",".join(record.tags)
    if record.endpoint.lower().startswith("http"):
        attrs["xmlUrl"] = record.endpoint
    return attrs


def _industry_attributes(record: Industry) -> Dict[str, str]:
    attrs: Dict[str, str] = {
        "text": record.name,
        "title": record.name,
        _attr("object"): "industry",
        _attr("id"): record.id,
        _attr("name"): record.name,
        _attr("enabled"): "true" if record.enabled else "false",
    }
    if record.parent_id is not None:
        attrs[_attr("parent-id")] = record.parent_id
    if record.keywords:
        attrs[_attr("keywords")] = ",".join(record.keywords)
    return attrs


def _render(
    industries: Sequence[Industry],
    channels: Sequence[Channel],
    *,
    title: str,
    owner: Optional[str],
    created_at: Optional[datetime],
) -> str:
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    if owner:
        ET.SubElement(head, "ownerName").text = owner
    stamp = created_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise InvalidContractStateError("created_at 必须带时区（避免导出结果不可比）")
    ET.SubElement(head, "dateCreated").text = format_datetime(stamp)
    body = ET.SubElement(root, "body")

    grouped: Dict[str, List[Channel]] = {}
    for channel in channels:
        grouped.setdefault(channel.industry_id, []).append(channel)

    known_industries = {ind.id for ind in industries}
    for industry in sorted(industries, key=lambda i: i.id):
        node = ET.SubElement(body, "outline", _industry_attributes(industry))
        for channel in sorted(grouped.get(industry.id, []), key=lambda c: c.id):
            ET.SubElement(node, "outline", _outline_attributes(channel))

    # 引用了未知行业的渠道不能消失（那会变成静默丢数据），单独挂在顶层。
    orphans = sorted(
        (c for c in channels if c.industry_id not in known_industries),
        key=lambda c: c.id,
    )
    for channel in orphans:
        ET.SubElement(body, "outline", _outline_attributes(channel))

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def channels_to_opml(
    channels: Iterable[Channel],
    *,
    industries: Iterable[Industry] = (),
    title: str = "Atlas 渠道目录",
    owner: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> str:
    """把渠道列表导出为 OPML。

    给出 `industries` 时按行业分组（可无损往返）；不给则全部平铺在 body 下。
    """
    return _render(
        tuple(industries),
        tuple(channels),
        title=title,
        owner=owner,
        created_at=created_at,
    )


def registry_to_opml(
    industries: Iterable[Industry],
    channels: Iterable[Channel],
    *,
    title: str = "Atlas 行业与渠道目录",
    owner: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> str:
    """导出完整目录（行业 + 渠道），用于备份与交换。"""
    return _render(
        tuple(industries),
        tuple(channels),
        title=title,
        owner=owner,
        created_at=created_at,
    )


# --- 解析 ---------------------------------------------------------------------


class OpmlOutline(ContractModel):
    """一个未被识别为 Atlas 记录的 outline（用于显式报告，而不是丢弃）。"""

    text: str
    attributes: Dict[str, Any] = Field(default_factory=dict)


class OpmlDocument(ContractModel):
    """解析结果：已识别的 Atlas 记录 + 需调用方补默认值的外部 outline + 被跳过的条目。"""

    title: Optional[str] = None
    industries: Tuple[Industry, ...] = ()
    channels: Tuple[Channel, ...] = ()
    legacy: Tuple[OpmlOutline, ...] = ()
    skipped: Tuple[Tuple[str, str], ...] = ()


def _text_of(element: ET.Element) -> str:
    return (element.get("title") or element.get("text") or "").strip()


def _parse_bool(raw: str, *, what: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise InvalidContractStateError(f"{what}={raw!r} 不是布尔值（true/false）")


def _parse_csv(raw: str) -> Tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_int(raw: str, *, what: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise InvalidContractStateError(f"{what}={raw!r} 不是整数") from exc


def slug_from_endpoint(endpoint: str) -> str:
    """从 URL 确定性派生渠道 id（仅用于外部 OPML 里没有 `atlas-id` 的条目）。"""
    parsed = urlsplit(endpoint)
    raw = f"{parsed.hostname or ''}{parsed.path or ''}"
    slug = "-".join(_SLUG_CHARS.findall(raw.lower()))[:64].strip("-")
    if not slug:
        raise IdError(f"无法从 endpoint={endpoint!r} 派生合法 id，请显式提供 atlas-id")
    return validate_id(slug, what="derived channel.id")


def _fetch_spec_payload(attributes: Dict[str, str], *, fetch_type: str, what: str) -> Dict[str, Any]:
    raw = attributes.get(_attr("fetch-spec"))
    if raw is None:
        return {"type": fetch_type}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidContractStateError(
            f"{what} 的 atlas-fetch-spec 不是合法 JSON：{exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidContractStateError(f"{what} 的 atlas-fetch-spec 必须是 JSON 对象")
    declared = payload.get("type")
    if declared is not None and declared != fetch_type:
        raise InvalidContractStateError(
            f"{what} 的 atlas-type={fetch_type!r} 与 atlas-fetch-spec.type={declared!r} 不一致"
        )
    return {**payload, "type": fetch_type}


def _known_fetch_types() -> Tuple[str, ...]:
    return tuple(t.value for t in FetchType)


def _resolve_type(attributes: Dict[str, str], *, what: str) -> str:
    """OPML `type` 是自由字符串，这里映射到 Atlas 抓取方式枚举（枚举外即拒绝）。"""
    raw = (attributes.get(_attr("type")) or attributes.get("type") or "").strip().lower()
    if not raw:
        raise InvalidContractStateError(
            f"{what} 缺少抓取方式：标准 OPML 需 type=\"rss\"/\"atom\"，"
            f"其余类型需 atlas-type（已知：{_known_fetch_types()}）"
        )
    if raw in ("rss", "rss2", "feed"):
        return FetchType.RSS.value
    if raw == "atom":
        return FetchType.ATOM.value
    if raw in (FetchType.JSON_API.value, FetchType.HTML_XPATH.value):
        return raw
    raise InvalidContractStateError(
        f"{what} 的抓取方式 {raw!r} 不在已知枚举 {_known_fetch_types()} 内："
        "新增协议类型需要 adapter 插件（SPEC §2.6），T-004 不提供插件机制"
    )


def _channel_from_outline(
    element: ET.Element,
    *,
    fallback_industry_id: Optional[str],
    default_interval_seconds: Optional[int],
) -> Channel:
    attributes = dict(element.attrib)
    what = f"outline {_text_of(element)!r}"
    endpoint = (
        attributes.get(_attr("endpoint"))
        or attributes.get("xmlUrl")
        or attributes.get("url")
        or ""
    ).strip()
    if not endpoint:
        raise InvalidContractStateError(f"{what} 缺少 endpoint（xmlUrl 或 atlas-endpoint）")
    fetch_type = _resolve_type(attributes, what=what)

    raw_id = (attributes.get(_attr("id")) or "").strip()
    channel_id = validate_id(raw_id, what="channel.id") if raw_id else slug_from_endpoint(endpoint)

    industry_id = (attributes.get(_attr("industry-id")) or "").strip() or fallback_industry_id
    if not industry_id:
        raise InvalidContractStateError(
            f"{what} 没有 industry_id：外部 OPML 导入必须显式给出 default_industry_id，"
            "不得替你猜行业归属（SPEC §2.9 校验规则 2）"
        )

    raw_interval = attributes.get(_attr("interval-seconds"))
    if raw_interval is None:
        if default_interval_seconds is None:
            raise InvalidContractStateError(
                f"{what} 缺少 atlas-interval-seconds：导入外部 OPML 必须显式给出 "
                "default_interval_seconds，不得替你猜采集间隔（SPEC §2.9 校验规则 5）"
            )
        interval = default_interval_seconds
    else:
        interval = _parse_int(raw_interval, what=f"{what}.interval-seconds")

    payload: Dict[str, Any] = {
        "id": channel_id,
        "industry_id": industry_id,
        "type": fetch_type,
        "endpoint": endpoint,
        "fetch_spec": _fetch_spec_payload(attributes, fetch_type=fetch_type, what=what),
        "interval_seconds": interval,
        "enabled": True,
        "tags": (),
    }
    raw_rate = attributes.get(_attr("rate-limit-seconds"))
    if raw_rate is not None:
        payload["rate_limit_seconds"] = _parse_int(raw_rate, what=f"{what}.rate-limit-seconds")
    raw_user_agent = attributes.get(_attr("user-agent"))
    if raw_user_agent is not None:
        payload["user_agent"] = raw_user_agent
    raw_reason = attributes.get(_attr("user-agent-justification"))
    if raw_reason is not None:
        payload["user_agent_justification"] = raw_reason
    raw_enabled = attributes.get(_attr("enabled"))
    if raw_enabled is not None:
        payload["enabled"] = _parse_bool(raw_enabled, what=f"{what}.enabled")
    raw_tags = attributes.get(_attr("tags"))
    if raw_tags is not None:
        payload["tags"] = _parse_csv(raw_tags)

    return Channel(**payload)


def _industry_from_outline(element: ET.Element) -> Industry:
    attributes = dict(element.attrib)
    raw_id = (attributes.get(_attr("id")) or "").strip()
    if not raw_id:
        raise InvalidContractStateError(
            f"行业 outline {_text_of(element)!r} 缺少 atlas-id"
        )
    name = (attributes.get(_attr("name")) or _text_of(element)).strip()
    payload: Dict[str, Any] = {"id": raw_id, "name": name}
    raw_parent = (attributes.get(_attr("parent-id")) or "").strip()
    if raw_parent:
        payload["parent_id"] = raw_parent
    raw_keywords = attributes.get(_attr("keywords"))
    if raw_keywords is not None:
        payload["keywords"] = _parse_csv(raw_keywords)
    raw_enabled = attributes.get(_attr("enabled"))
    # 外部 OPML 的行业 outline 通常不带 atlas-enabled；这里只在**导入边界**上
    # 显式落一个默认值（模型本身不给默认值，避免"默认启用"被静默决定）。
    payload["enabled"] = (
        _parse_bool(raw_enabled, what=f"industry {raw_id!r}.enabled")
        if raw_enabled is not None
        else True
    )
    return Industry(**payload)


def _walk(
    element: ET.Element,
    *,
    ancestor_industry_id: Optional[str],
    industries: List[Industry],
    channels: List[Channel],
    legacy: List[OpmlOutline],
    skipped: List[Tuple[str, str]],
    allow_legacy: bool,
) -> None:
    for child in list(element):
        if child.tag != "outline":
            continue
        attributes = dict(child.attrib)
        object_kind = (attributes.get(_attr("object")) or "").strip()
        has_children = any(grand.tag == "outline" for grand in list(child))

        if object_kind == "industry":
            industry = _industry_from_outline(child)
            industries.append(industry)
            _walk(
                child,
                ancestor_industry_id=industry.id,
                industries=industries,
                channels=channels,
                legacy=legacy,
                skipped=skipped,
                allow_legacy=allow_legacy,
            )
            continue

        if object_kind == "channel":
            channels.append(
                _channel_from_outline(
                    child,
                    fallback_industry_id=ancestor_industry_id,
                    default_interval_seconds=None,
                )
            )
            if has_children:
                skipped.append((_text_of(child), "channel outline 下的嵌套 outline 已忽略"))
            continue

        if object_kind:
            raise InvalidContractStateError(
                f"outline {_text_of(child)!r} 的 atlas-object={object_kind!r} 未知"
                "（只支持 industry / channel）"
            )

        # 无 atlas-object 标记：分组节点 or 外部清单条目
        if "xmlUrl" in attributes or "url" in attributes:
            if allow_legacy:
                legacy.append(OpmlOutline(text=_text_of(child), attributes=attributes))
            else:  # pragma: no cover - 由调用方传入 allow_legacy=True
                skipped.append((_text_of(child), "外部 outline 未在本次导入范围内"))
            continue

        if has_children:
            _walk(
                child,
                ancestor_industry_id=ancestor_industry_id,
                industries=industries,
                channels=channels,
                legacy=legacy,
                skipped=skipped,
                allow_legacy=allow_legacy,
            )
            continue

        skipped.append((_text_of(child), "既非渠道（无 xmlUrl）也无子条目"))


def parse_opml(text: str) -> OpmlDocument:
    """解析 OPML 文本。

    只做"读到什么就是什么"的解析；需要补默认值的外部 outline 放在 `legacy` 里，
    由 `import_opml()` 用调用方显式给出的默认值补齐后再走校验。
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidContractStateError("OPML 内容不得为空")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise InvalidContractStateError(f"OPML 解析失败：{exc}") from exc
    if root.tag != "opml":
        raise InvalidContractStateError(f"根元素必须是 <opml>，收到 <{root.tag}>")

    body = root.find("body")
    if body is None:
        raise InvalidContractStateError("OPML 缺少 <body>")
    head = root.find("head")
    title_element = head.find("title") if head is not None else None
    title = title_element.text.strip() if title_element is not None and title_element.text else None

    industries: List[Industry] = []
    channels: List[Channel] = []
    legacy: List[OpmlOutline] = []
    skipped: List[Tuple[str, str]] = []
    _walk(
        body,
        ancestor_industry_id=None,
        industries=industries,
        channels=channels,
        legacy=legacy,
        skipped=skipped,
        allow_legacy=True,
    )
    return OpmlDocument(
        title=title,
        industries=tuple(industries),
        channels=tuple(channels),
        legacy=tuple(legacy),
        skipped=tuple(skipped),
    )


# --- 导入到仓储（同一套校验，无旁路）-----------------------------------------


def import_opml(
    store: ConfigStore,
    text: str,
    *,
    author: Optional[str] = None,
    default_industry_id: Optional[str] = None,
    default_interval_seconds: Optional[int] = None,
    note: Optional[str] = None,
) -> Optional[ConfigVersion]:
    """把 OPML 导入 `ConfigStore`。

    - 已识别的行业/渠道经模型构造（单对象校验）后，与外部 outline 补齐出的渠道一起
      **一次性提交**，由 `ConfigStore.commit()` 做跨对象校验并产生新版本。
    - 已存在且内容相同的记录视为幂等重复，跳过；已存在但内容不同则明确报错，
      要求调用方用 update 显式改（不在导入时静默覆盖）。
    - 返回 `None` 表示明确的"无变化"（重复导入同一份文件）。

    外部 OPML（无 `atlas-*` 字段）必须显式给出 `default_industry_id` 与
    `default_interval_seconds`，否则拒绝——不猜值。
    """
    document = parse_opml(text)
    current = store.current
    existing_industries = {i.id: i for i in current.industries}
    existing_channels = {c.id: c for c in current.channels}

    mutations: List[RegistryMutation] = []
    unchanged: List[str] = []

    def queue_industry(record: Industry) -> None:
        existing = existing_industries.get(record.id)
        if existing is not None:
            if existing.payload() != record.payload():
                raise InvalidContractStateError(
                    f"行业 {record.id!r} 已存在且内容不同；导入不覆盖既有配置，"
                    "请显式调用 update_industry()"
                )
            unchanged.append(record.id)
            return
        mutations.append(
            RegistryMutation(kind="create", object_kind="industry", payload=record.payload())
        )

    def queue_channel(record: Channel) -> None:
        existing = existing_channels.get(record.id)
        if existing is not None:
            if existing.payload() != record.payload():
                raise InvalidContractStateError(
                    f"渠道 {record.id!r} 已存在且内容不同；导入不覆盖既有配置，"
                    "请显式调用 update_channel()"
                )
            unchanged.append(record.id)
            return
        mutations.append(
            RegistryMutation(kind="create", object_kind="channel", payload=record.payload())
        )

    # 父行业可能出现在子行业之后；按层级拓扑排序，保证提交后的 candidate 一致。
    for record in _industries_parents_first(document.industries):
        queue_industry(record)
    for record in document.channels:
        queue_channel(record)
    for outline in document.legacy:
        # 复用与 atlas outline 完全相同的解析/构造路径，避免出现第二套校验。
        element = ET.Element("outline", {str(k): str(v) for k, v in outline.attributes.items()})
        record = _channel_from_outline(
            element,
            fallback_industry_id=default_industry_id,
            default_interval_seconds=default_interval_seconds,
        )
        queue_channel(record)

    if not mutations:
        return None

    summary = [note] if note else []
    if document.skipped:
        rendered = "; ".join(f"{text}: {reason}" for text, reason in document.skipped)
        summary.append(f"跳过 {len(document.skipped)} 条非渠道 outline（{rendered}）")
    if unchanged:
        summary.append(f"幂等跳过 {len(unchanged)} 条已存在且相同的记录")

    return store.commit(
        mutations,
        author=author,
        kind=ChangeKind.IMPORT,
        note="；".join(summary) if summary else None,
    )


def _industries_parents_first(records: Iterable[Industry]) -> Tuple[Industry, ...]:
    remaining = {record.id: record for record in records}
    ordered: List[Industry] = []
    while remaining:
        progressed = False
        for record_id in sorted(remaining):
            record = remaining[record_id]
            if record.parent_id is None or record.parent_id not in remaining:
                ordered.append(record)
                del remaining[record_id]
                progressed = True
                break
        if not progressed:  # 成环：交给 validate_registry 报错，但不要死循环
            ordered.extend(remaining.values())
            break
    return tuple(ordered)
