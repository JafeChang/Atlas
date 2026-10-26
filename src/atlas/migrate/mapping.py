"""旧频道名 → 注册表 `channel_id` 的映射（**注入式**，本包不 import `atlas.registry`）。

为什么要注入而不是直接查注册表
------------------------------

SPEC §4.0：跨包 import 只允许指向自己在 DAG 里的**上游**（生产者）。
`atlas.registry`（T-004 / T-101）不在 T-103 的上游，`atlas.archive` 也不 import 它
（`atlas.feed` 出于同一理由刻意不 import registry，§2.5）。因此本包同样**不 import**
`atlas.registry`，由**组合根**（`tools/migrate_legacy.py`，或测试）把映射注入进来。

保行业归属的具体做法（§2.5 的 C8 闭环）
---------------------------------------

实测旧语料的 5 个真频道目录名与注册表渠道 id **1:1 相同**
（`ai-techpark` / `google-ai-blog` / `kdnuggets` / `marktechpost` / `synced-review`
都在 `data/store/atlas.db` 的 `channels` 表里），因此导入后
`channel_id` 直接沿用目录名即可，行业归属由注册表的 `channels.industry_id` 给出 ——
导入的每一篇都能被 `feed --industry` 筛出来。**只有映射成功才写入**：
一个映射不到的频道是"行业归属会丢"的严重问题，必须记账或直接失败，不能猜。

一对多 = 响亮失败
-----------------

若一个旧目录名同时匹配注册表里多个渠道（例如别名 `google-ai-blog` 与
`google-ai` 都建成了渠道），本模块返回 `MAPPER_AMBIGUOUS` 而不是任选一个 ——
任选会让一部分记录静默挂到错误的渠道上，而 `raw_id` 里已把 `channel_id`
作为指纹输入，选错就是永久错。
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import (
    REASON_CHANNEL_MAPPING_CONFLICT,
    REASON_CHANNEL_NOT_MAPPED,
    ChannelMappingError,
)

__all__ = [
    "MAPPER_AMBIGUOUS",
    "MAPPER_NOT_FOUND",
    "ChannelMapResolver",
    "ChannelMapper",
    "build_channel_map",
    "channel_map_resolver",
    "empty_mapper",
    "known_channels",
    "normalize_channel_map",
    "resolve_nothing",
    "unmapped_names",
]

#: 旧频道名在注册表里**没有**对应渠道。
MAPPER_NOT_FOUND = "not_found"
#: 旧频道名在注册表里对应**多个**渠道（别名冲突）—— 不任选。
MAPPER_AMBIGUOUS = "ambiguous"

#: 解析器：旧频道名 → `(映射结果, 说明)`。
#: - `(("chan-a",), "")` → 成功
#: - `((), MAPPER_NOT_FOUND)` / `(("a", "b"), MAPPER_AMBIGUOUS)` → 失败（原因见常量）
ChannelMapResolver = Callable[[str], Tuple[Sequence[str], str]]

#: 映射器：旧频道名 → 注册表 `channel_id`；失败时抛 `ChannelMappingError`。
ChannelMapper = Callable[[str], str]


def normalize_channel_map(mapping: Mapping[str, object]) -> Dict[str, Tuple[str, ...]]:
    """把 `{旧名: id}` 或 `{旧名: [id, …]}` 统一成 `{旧名: (id, …)}`（**只读副本**）。

    重复值去重保序；空字符串是配置错误 → `ChannelMappingError`（不静默丢弃）。
    """
    normalized: Dict[str, Tuple[str, ...]] = {}
    for legacy_name, targets in mapping.items():
        if isinstance(targets, str):
            values: Iterable[object] = (targets,)
        else:
            values = targets  # type: ignore[assignment]
        cleaned: List[str] = []
        for target in values:
            if not isinstance(target, str) or not target.strip():
                raise ChannelMappingError(
                    f"渠道映射 {legacy_name!r} 含非法目标 {target!r}（必须是非空字符串）"
                )
            text = target.strip()
            if text not in cleaned:
                cleaned.append(text)
        if not cleaned:
            raise ChannelMappingError(f"渠道映射 {legacy_name!r} 的目标列表为空")
        normalized[legacy_name] = tuple(cleaned)
    return normalized


def build_channel_map(
    registry_channels: Iterable[object],
    *,
    aliases: Optional[Mapping[str, Iterable[str]]] = None,
    derive_industry_prefixes: bool = False,
) -> Dict[str, Tuple[str, ...]]:
    """由注册表渠道列表构造"旧频道名 → 注册表渠道 id"的候选映射（**纯函数**）。

    `registry_channels` 的元素只需有 `.id` 与 `.industry_id`（`atlas.registry.Channel`
    满足，任何鸭子类型对象也满足 —— 本包因此不需要 import registry）。

    规则（**确定、可解释**）：

    1. 渠道自己的 id 是**精确候选**（实测 5 个真频道走的就是这条）；
    2. `aliases` 给出的别名**追加**为精确候选（`{注册表 id: [旧名, …]}`）；
    3. `derive_industry_prefixes=True` 时，额外把"渠道 id 以旧名 + `-` 开头"的渠道
       作为**低置信度候选** —— 它让 `industry_id` **前缀**出现在 `raw_id` 的输入里
       （见 `channel_map_resolver`），因此**默认关闭**，只有操作者显式打开
       （组合根在"精确匹配不到的旧目录名"上重试）才会用到。

    一个旧名对应多个候选时，结果里是多个 id，解析器会返回 `MAPPER_AMBIGUOUS`。
    """
    entries: List[Tuple[str, str]] = []
    for channel in registry_channels:
        channel_id = getattr(channel, "id", None)
        industry_id = getattr(channel, "industry_id", None)
        if not isinstance(channel_id, str) or not channel_id:
            raise ChannelMappingError(f"注册表渠道缺少 id：{channel!r}")
        if not isinstance(industry_id, str) or not industry_id:
            raise ChannelMappingError(f"注册表渠道 {channel_id!r} 缺少 industry_id")
        entries.append((channel_id, industry_id))

    alias_pairs: List[Tuple[str, str]] = []
    for channel_id, names in (aliases or {}).items():
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise ChannelMappingError(f"别名列表 {channel_id!r} 含非法名 {name!r}")
            alias_pairs.append((name.strip(), channel_id))

    candidates: Dict[str, List[str]] = {}

    def add(name: str, channel_id: str) -> None:
        bucket = candidates.setdefault(name, [])
        if channel_id not in bucket:
            bucket.append(channel_id)

    # 1) 精确 id
    for channel_id, _ in entries:
        add(channel_id, channel_id)
    # 2) 显式别名
    for name, channel_id in alias_pairs:
        add(name, channel_id)

    if derive_industry_prefixes:
        # 3) 低置信度：旧名是渠道 id 的前缀（如 `google-ai-blog` → `google-ai-blog-1`）
        for old_name in sorted(candidates):
            if any(channel_id == old_name for channel_id in candidates[old_name]):
                continue  # 已有精确候选，不掺入低置信度候选
            for channel_id, _ in entries:
                if channel_id.startswith(f"{old_name}-"):
                    add(old_name, channel_id)

    return {name: tuple(ids) for name, ids in sorted(candidates.items())}


def channel_map_resolver(channel_map: Mapping[str, object]) -> ChannelMapResolver:
    """把静态映射包成 `ChannelMapResolver`（`Migrate` 的默认形态）。"""
    normalized = normalize_channel_map(channel_map)

    def resolve(legacy_channel: str) -> Tuple[Sequence[str], str]:
        targets = normalized.get(legacy_channel)
        if not targets:
            return (), MAPPER_NOT_FOUND
        if len(targets) > 1:
            return targets, MAPPER_AMBIGUOUS
        return targets, ""

    return resolve


def unmapped_names(channel_map: Mapping[str, object], names: Iterable[str]) -> List[str]:
    """`names` 里没有出现在映射中的部分（排序）—— 供组合根决定是否启用前缀推导。"""
    known = {str(key) for key in channel_map}
    return sorted({name for name in names if name not in known})


def resolve_nothing(legacy_channel: str) -> Tuple[Sequence[str], str]:
    """默认解析器：**一律未映射**（结果码 `MAPPER_NOT_FOUND`）。

    默认值必须让"忘记注入映射"这件事**响亮失败**：每条记录都进对账表的失败栏，
    而不是静默把所有文档挂到一个编造的渠道上（那会同时污染 `raw_id` 与行业归属）。
    """
    return (), MAPPER_NOT_FOUND


def empty_mapper(legacy_channel: str) -> str:
    """`ChannelMapper` 形态的"一律失败"（供只想看到异常、不要对账表的调用方）。

    `MigrateOptions.channel_map` 应传 `mapping.ChannelMapResolver`（例如
    `resolve_nothing`）或直接传映射；传本函数会在第一条记录上抛异常。
    """
    raise ChannelMappingError(
        f"没有注入渠道映射，无法导入频道 {legacy_channel!r}；"
        "请由组合根提供旧频道名 → 注册表 channel_id 的映射（本包不 import atlas.registry）",
        reason=REASON_CHANNEL_NOT_MAPPED,
    )


def known_channels(channel_map: Mapping[str, object]) -> Tuple[str, ...]:
    """映射覆盖的旧频道名（排序），供工具打印与测试断言。"""
    return tuple(sorted(str(name) for name in channel_map))


def _reason_for_conflict(legacy_channel: str, targets: Sequence[str]) -> ChannelMappingError:
    return ChannelMappingError(
        f"频道 {legacy_channel!r} 在注册表里对应多个渠道 {list(targets)}；"
        "不任选（raw_id 与行业归属都会永久错），请先消歧",
        reason=REASON_CHANNEL_MAPPING_CONFLICT,
    )
