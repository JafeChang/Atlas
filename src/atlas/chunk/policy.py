"""分块策略：参数化 + 版本化（SPEC §2.7"能力层做全"，T-206）。

**为什么策略要参数化、还要记进产物**

SPEC §3 要求任务是 `f(输入快照, 配置快照) → 输出`，且**可重算**。
分块的"配置快照"就是 `ChunkPolicy`：只要它被完整记录下来，任何人拿着
同一份 raw 就能重建出**逐字节相同**的分块集合。因此策略不是调用现场的
临时参数，而是**产物的一部分**（`ChunkSet.policy`）。

**分隔符优先级（对齐可读边界，不硬切）**

`separators` 是**有序**的 `SeparatorSpec` 序列，索引即优先级。每个 spec 用正则
描述一类可读边界：

- `after`  —— 在匹配**之后**断开（段落 `\\n\\s*\\n`、句末、换行）
- `before` —— 在匹配**之前**断开（列表项标记）
- `soft`   —— 与显式分隔符同级：只在窗口内没有任何显式分隔符时才使用

命中规则：**先按优先级取"窗口内最后一个命中"**；窗口内该优先级没有命中才下降到
下一优先级。全部落空 → 才允许硬切（硬切仍尽量退到空白处，避免切断单词）。

**非法参数必须在构造时就抛错**

`overlap_chars >= target_chars` 会让分块循环永不前进（死循环）；
非法正则会等到运行时才炸。两者都必须在 `ChunkPolicy(...)` **构造期**拒绝。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ChunkPolicyError

__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_SEPARATORS",
    "MIN_FILL_RATIO",
    "SEPARATOR_KINDS",
    "ChunkPolicy",
    "SeparatorSpec",
]

#: 合法的分隔符类型。
SEPARATOR_KINDS = frozenset({"after", "before", "soft"})

#: 窗口内允许的最小填充比例：低于它就不认这个边界（否则会切出碎块）。
MIN_FILL_RATIO = 0.5


@dataclass(frozen=True, slots=True)
class SeparatorSpec:
    """一类可读边界：`kind` 决定断点取匹配之前还是之后，`pattern` 是正则。

    构造即校验：`name` 非空、`kind` 在 `SEPARATOR_KINDS` 内、`pattern` 能被 `re`
    编译且**不命中空串**（非法正则在构造期就**响亮失败**，不会拖到运行时）。
    """

    name: str
    kind: str
    pattern: str
    _regex: "re.Pattern[str]" = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ChunkPolicyError("分隔符 name 不得为空")
        if self.kind not in SEPARATOR_KINDS:
            raise ChunkPolicyError(
                f"未知分隔符类型 {self.kind!r}（必须是 {sorted(SEPARATOR_KINDS)} 之一）"
            )
        if not isinstance(self.pattern, str) or not self.pattern:
            raise ChunkPolicyError(f"分隔符 {self.name!r} 的 pattern 不得为空")
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise ChunkPolicyError(
                f"分隔符 {self.name!r} 的正则非法（{self.pattern!r}）：{exc}"
            ) from exc
        if compiled.search("") is not None:
            raise ChunkPolicyError(
                f"分隔符 {self.name!r} 的正则会命中空串（{self.pattern!r}）：无法作为边界"
            )
        object.__setattr__(self, "_regex", compiled)

    @property
    def regex(self) -> "re.Pattern[str]":
        return self._regex

    def positions(self, text: str, lower: int, upper: int) -> list[int]:
        """返回 `[lower, upper]` 闭区间内全部断点位置（升序、去重、去掉 0）。"""
        if upper < lower:
            return []
        found: set[int] = set()
        for match in self.regex.finditer(text, lower, upper + 1):
            position = match.end() if self.kind != "before" else match.start()
            if lower <= position <= upper and position > 0:
                found.add(position)
        return sorted(found)


#: 默认分隔符优先级：段落 > 句末 > 换行 > 列表项标记。
DEFAULT_SEPARATORS: tuple[SeparatorSpec, ...] = (
    SeparatorSpec(name="paragraph", kind="after", pattern=r"\n\s*\n"),
    SeparatorSpec(name="sentence", kind="after", pattern=r"[.!?。！？]+[\x22\x27)\]]*\s+"),
    SeparatorSpec(name="newline", kind="after", pattern=r"\n+"),
    SeparatorSpec(name="list-item", kind="before", pattern=r"\n(?=[-*\u2022]\s|\d{1,3}[.)]\s)"),
)


@dataclass(frozen=True, slots=True)
class ChunkPolicy:
    """分块参数快照（冻结、可哈希、可记录进产物）。

    | 字段 | 默认 | 含义 |
    |---|---|---|
    | `version` | `"chunk-policy-v1"` | **策略版本**，参与 `chunk_id` 计算 |
    | `target_chars` | `1000` | 目标长度；分块尽量落在 `[target/2, target]` |
    | `max_chars` | `1600` | 硬上限；任何分块不得超过（重叠不计入） |
    | `overlap_chars` | `0` | 相邻分块重叠字符数**下界**（对齐边界只会更多） |
    | `separators` | `DEFAULT_SEPARATORS` | 有序分隔符（索引即优先级） |

    非法参数在构造期抛 `ChunkPolicyError`：

    - `target_chars <= 0` / `max_chars <= 0`
    - `max_chars < target_chars`
    - `overlap_chars < 0` 或 `overlap_chars >= target_chars`（后者会导致死循环）
    - `version` 为空
    - `separators` 为空、含非法 spec、或名称重复
    """

    version: str = "chunk-policy-v1"
    target_chars: int = 1000
    max_chars: int = 1600
    overlap_chars: int = 0
    separators: tuple[SeparatorSpec, ...] = DEFAULT_SEPARATORS

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise ChunkPolicyError("policy.version 不得为空：ID 必须携带策略版本")
        for label in ("target_chars", "max_chars", "overlap_chars"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ChunkPolicyError(
                    f"{label} 必须是 int，得到 {type(value).__name__}（{value!r}）"
                )
        if self.target_chars <= 0:
            raise ChunkPolicyError(f"target_chars 必须 > 0，得到 {self.target_chars}")
        if self.max_chars <= 0:
            raise ChunkPolicyError(f"max_chars 必须 > 0，得到 {self.max_chars}")
        if self.max_chars < self.target_chars:
            raise ChunkPolicyError(
                f"max_chars({self.max_chars}) 不得小于 target_chars({self.target_chars})"
            )
        if self.overlap_chars < 0:
            raise ChunkPolicyError(f"overlap_chars 不得为负：{self.overlap_chars}")
        if self.overlap_chars >= self.target_chars:
            raise ChunkPolicyError(
                f"overlap_chars({self.overlap_chars}) 必须 < target_chars({self.target_chars})："
                "否则分块循环永不前进（死循环）"
            )
        if not isinstance(self.separators, tuple) or not self.separators:
            raise ChunkPolicyError("separators 必须是非空 tuple（索引即优先级）")
        for index, spec in enumerate(self.separators):
            if not isinstance(spec, SeparatorSpec):
                raise ChunkPolicyError(
                    f"separators[{index}] 必须是 SeparatorSpec，得到 {type(spec).__name__}"
                )
        names = [spec.name for spec in self.separators]
        if len(set(names)) != len(names):
            raise ChunkPolicyError(f"分隔符名称必须唯一（否则无法归属命中）：{names}")

    # -- 派生参数 -------------------------------------------------------------------

    @property
    def min_fill_chars(self) -> int:
        """窗口内允许的最小填充长度（低于它就继续找更大的边界）。"""
        return max(1, int(self.target_chars * MIN_FILL_RATIO))

    # -- 边界搜索 -------------------------------------------------------------------

    def break_positions(self, text: str, lower: int, upper: int) -> list[int]:
        """`[lower, upper]` 内全部**显式**分隔符断点（各优先级取并集，升序去重）。"""
        found: set[int] = set()
        for spec in self.separators:
            if spec.kind == "soft":
                continue
            found.update(spec.positions(text, lower, upper))
        return sorted(found)

    def find_break(self, text: str, start: int, target_end: int, max_end: int) -> tuple[int, str]:
        """在 `(start, max_end]` 内找断点，返回 `(断点, 命中说明)`。

        规则（**确定性、无随机**）：

        对每个分隔符**按优先级从高到低**依次尝试：

        1. 先看目标窗口 `W = [start + min_fill, target_end]`：有命中就取 `W` 内
           **最后一个**位置（这一段/这一句尽量长，但仍对齐可读边界）；
        2. `W` 内没有命中，就把窗口放大到 `[start + min_fill, max_end]` 再看一次
           —— **`min_fill` 仍然生效**，否则会为了对齐一个紧挨着起点的段末而
           切出碎块；
        3. 所有分隔符在所有窗口内都落空 → 硬切：只切在 **token 边界**
           （断点两侧至少一侧是空白）；窗口内一个空白都没有（单个超长 token）
           → 只能切在 `max_end`。

        **为什么"更高优先级可以更早"**：可读性优先于长度。目标窗口内若存在段末，
        就在段末切，而不是为了凑满 `target_chars` 去切句末。这保证"边界对齐
        可读边界，不按固定字符硬切"，同时 `min_fill` 兜住"碎块"。

        **返回值契约**：断点恒在 `(start, max_end]` 内。它可能落在空白上
        （窗口内既无分隔符、又正好停在空白串里）；"下一块以内容开头"由
        `snap_start` 与 `chunk_normalized` 的主循环负责，而不是靠这里保证。

        入参按 `min(x, len(text))` 夹紧，因此调用方可以直接传
        `start + target_chars` / `start + max_chars` 而不必先夹一遍。
        """
        max_end = min(max_end, len(text))
        target_end = min(target_end, max_end)
        if not 0 <= start < target_end <= max_end:
            raise ChunkPolicyError(
                f"非法搜索窗口：start={start} target_end={target_end} "
                f"max_end={max_end} len={len(text)}"
            )

        fill_lower = min(start + self.min_fill_chars, target_end)
        for spec in self.separators:
            positions = spec.positions(text, fill_lower, target_end)
            if not positions and max_end > target_end:
                positions = spec.positions(text, fill_lower, max_end)
            if positions:
                return positions[-1], spec.name

        # 硬切：**只能切在 token 边界**（不切断单词）。
        #
        # 关键判断不是"窗口末尾是不是空白"，而是"断点两侧是不是同一个 token 的
        # 两半"（`…word3|5…` 就属于后者）。因此只有当 `text[断点-1]` 或
        # `text[断点]` 是空白时才算合法边界；否则退到窗口内最后一个空白之后。
        # 窗口内一个空白都没有（单个超长 token，例如 5000 个 "A"）→ 只能切在
        # `max_end`，因为没有别的选择。
        if max_end == len(text) or text[max_end - 1].isspace() or text[max_end].isspace():
            return max_end, "hard-break"
        window = text[start:max_end]
        last_space = max(
            (index for index, char in enumerate(window) if char.isspace()),
            default=-1,
        )
        if last_space < 0:
            return max_end, "hard-break"
        return start + last_space + 1, "hard-break"

    def snap_start(self, text: str, lower: int, upper: int, candidate: int) -> int:
        """为下一块选起点：返回 `[lower, upper]` 内的位置（`lower = start + 1`）。

        取值规则：

        1. 把 `candidate` 夹到 `[lower, upper]`，保证**必然前进**且**不产生空隙**；
        2. 若夹取后的位置落在空白上（重叠回退落进了分隔符里），**向后对齐到最近的
           断点**（`[lower, position]` 内最后一个），让下一块从可读边界开始；
        3. 该区间内没有断点就取夹取后的位置本身（调用方会跳过其前导空白）。

        因此返回值恒在 `[lower, upper]` 内 —— 重叠只会**多于**请求值，绝不少于。
        """
        if upper <= lower:
            return upper
        position = min(max(candidate, lower), upper)
        if not text[position].isspace():
            return position
        positions = self.break_positions(text, lower, position)
        return positions[-1] if positions else position

    # -- 快照（用于重建自检与报告） ---------------------------------------------------

    def snapshot_fields(self) -> tuple[str, ...]:
        """策略的**有序**字段（重建时用来断言"两次用的策略完全一致"）。"""
        separators = tuple(f"{s.name}:{s.kind}:{s.pattern}" for s in self.separators)
        return (
            self.version,
            str(self.target_chars),
            str(self.max_chars),
            str(self.overlap_chars),
            *(separators or ("<none>",)),
        )


#: 默认策略（代码里的默认值就是文档里的默认值；SPEC §2.12 的同一条纪律）。
DEFAULT_POLICY = ChunkPolicy()


def policy_from(**overrides: object) -> ChunkPolicy:
    """按默认值构造策略并覆盖若干字段（便捷入口，非法参数同样在构造期抛错）。"""
    allowed = {"version", "target_chars", "max_chars", "overlap_chars", "separators"}
    unknown = set(overrides) - allowed
    if unknown:
        raise ChunkPolicyError(f"未知策略字段：{sorted(unknown)}")
    if "separators" in overrides:
        specs = overrides["separators"]
        if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes)):
            raise ChunkPolicyError("separators 必须是 SeparatorSpec 序列")
        overrides["separators"] = tuple(specs)
    return ChunkPolicy(**overrides)  # type: ignore[arg-type]
