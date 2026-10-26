"""T-004 行业/渠道配置外部契约（SPEC §2.9）。

本模块是**唯一**的配置校验权威：`Industry` / `Channel` / `FetchSpec` 三个数据模型，
加上一组**跨对象**不变量检查函数。持久化与版本化（`versioning.py`）、OPML 交换
（`opml.py`）都必须经过这里，不允许旁路校验。

校验原则（SPEC §2.9「非法配置必须在保存时拒绝，不得静默降级」）：

- 单对象规则在 pydantic validator 里强制，违例抛**领域异常**（继承 `Exception`，
  不被 pydantic 包装成 `ValidationError`，见 `atlas.contracts.errors` 的说明）。
- 跨对象规则（`industry_id` 必须存在、`id` 唯一）由 `validate_registry()` 承担：
  它在**每次提交前**对整份候选快照跑一遍。
- 未知抓取协议**不静默忽略**：`FetchType` 是闭合枚举，枚举外的类型根本构造不出来；
  真正的协议扩展点由 `require_adapter_plugin()` 响亮失败地标记（T-004 不实现插件机制，
  只留边界，见 SPEC §2.6 / §2.9）。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from atlas.contracts import ContractModel, IdError, InvalidContractStateError

__all__ = [
    "MIN_INTERVAL_SECONDS",
    "MIN_RATE_LIMIT_SECONDS",
    "BROWSER_MASQUERADE_USER_AGENTS",
    "ID_PATTERN",
    "KNOWN_FETCH_TYPES",
    "REQUIRED_SPEC_FIELDS",
    "SPEC_FIELDS_BY_FAMILY",
    "FetchType",
    "FetchSpec",
    "Industry",
    "Channel",
    "require_adapter_plugin",
    "validate_id",
    "validate_endpoint",
    "validate_registry",
]

# --- 下限保护（SPEC §2.9 校验规则 5）-----------------------------------------
# 采集间隔下限：低于 60s 的轮询既无信息价值，也会给源站造成压力。
MIN_INTERVAL_SECONDS = 60
# 同域最小间隔：允许 0（表示不做额外节流），但不得为负。
MIN_RATE_LIMIT_SECONDS = 0

# 小写短横线标识：`a` / `openai-blog` / `arxiv-cs-lg`；不允许前导/尾随/连续短横线。
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# SPEC §2.9 / §7.1：默认禁止浏览器伪装。这里的取值是**UA 选项名**而非 UA 字符串本身
# （SPEC 字段说明："引用 UA 选项名"），因此枚举的是伪装档位名。
BROWSER_MASQUERADE_USER_AGENTS = frozenset(
    {
        "browser",
        "chrome",
        "chrome-desktop",
        "firefox",
        "firefox-desktop",
        "safari",
        "safari-ios",
        "edge",
        "headless-chrome",
    }
)

_HTTP_SCHEMES = frozenset({"http", "https"})


class FetchType(str, Enum):
    """抓取方式枚举（SPEC §2.9「抓取方式枚举与扩展边界」）。

    **闭合枚举**：四种已知类型零代码可配；枚举外的协议需要 adapter 插件，
    在 T-004 里表现为"根本构造不出来"，而不是"能构造但没人处理"。
    """

    RSS = "rss"
    ATOM = "atom"
    JSON_API = "json_api"
    HTML_XPATH = "html_xpath"


KNOWN_FETCH_TYPES: Tuple[str, ...] = tuple(t.value for t in FetchType)

#: 每种类型**必须**提供、且**只允许**提供的 fetch_spec 字段。
REQUIRED_SPEC_FIELDS: Mapping[FetchType, Tuple[str, ...]] = {
    FetchType.RSS: (),
    FetchType.ATOM: (),
    FetchType.JSON_API: ("list_path", "title_path"),
    FetchType.HTML_XPATH: ("list_selector", "title_selector"),
}

#: 字段族：json_api 用"字段路径"，html_xpath 用"选择器"。混用即配置错误
#: （adapter 会静默忽略不认识的字段——这里在保存时就拒绝，避免静默降级）。
SPEC_FIELDS_BY_FAMILY: Mapping[FetchType, Tuple[str, ...]] = {
    FetchType.RSS: (),
    FetchType.ATOM: (),
    FetchType.JSON_API: (
        "list_path",
        "title_path",
        "content_path",
        "time_path",
        "url_path",
    ),
    FetchType.HTML_XPATH: (
        "list_selector",
        "title_selector",
        "content_selector",
        "time_selector",
        "url_selector",
    ),
}

_ALL_SPEC_FIELDS: Tuple[str, ...] = tuple(
    sorted({name for names in SPEC_FIELDS_BY_FAMILY.values() for name in names})
)


# --- 基础校验函数 -------------------------------------------------------------


def validate_id(value: str, *, what: str = "id") -> str:
    """校验小写短横线标识。非法一律抛 `IdError`（不静默纠正大小写）。"""
    if not isinstance(value, str) or not value:
        raise IdError(f"{what} 不得为空")
    if not ID_PATTERN.match(value):
        raise IdError(
            f"{what}={value!r} 不合法：只允许小写字母/数字，段间用单个短横线连接"
            "（例：openai-blog、arxiv-cs-lg）"
        )
    if len(value) > 64:
        raise IdError(f"{what}={value!r} 过长（上限 64 字符）")
    return value


def validate_endpoint(url: str) -> str:
    """校验 `http(s)` URL（SPEC §2.9 校验规则 6）。"""
    if not isinstance(url, str) or not url:
        raise InvalidContractStateError("endpoint 不得为空")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in _HTTP_SCHEMES:
        raise InvalidContractStateError(
            f"endpoint={url!r} 必须以 http:// 或 https:// 开头，收到 scheme={parsed.scheme!r}"
        )
    if not parsed.netloc or not parsed.hostname:
        raise InvalidContractStateError(f"endpoint={url!r} 缺少主机名")
    return url


def require_adapter_plugin(fetch_type: str) -> None:
    """未知协议类型的扩展边界（SPEC §2.6）。

    T-004 只**留出**插件边界，不实现插件机制：调用即响亮失败。
    """
    raise NotImplementedError(
        f"抓取协议 {fetch_type!r} 不在已知枚举 {KNOWN_FETCH_TYPES} 内；"
        "新增协议类型需要 adapter 插件（核心不改，见 SPEC §2.6 / §2.9）。"
        "T-004 不含插件机制，本函数是显式的扩展边界标记。"
    )


# --- FetchSpec ---------------------------------------------------------------


class FetchSpec(ContractModel):
    """随 `type` 变化的抓取参数（选择器 / 字段路径）。

    只声明"该类型可能需要"的字段；某类型**必须**哪些字段、**禁止**哪些字段
    由 `REQUIRED_SPEC_FIELDS` / `SPEC_FIELDS_BY_FAMILY` 决定，见 `_check_type_contract`。
    """

    type: FetchType

    # json_api：字段路径映射
    list_path: Optional[str] = None
    title_path: Optional[str] = None
    content_path: Optional[str] = None
    time_path: Optional[str] = None
    url_path: Optional[str] = None

    # html_xpath：选择器
    list_selector: Optional[str] = None
    title_selector: Optional[str] = None
    content_selector: Optional[str] = None
    time_selector: Optional[str] = None
    url_selector: Optional[str] = None

    @field_validator(*_ALL_SPEC_FIELDS)
    @classmethod
    def _reject_blank(cls, value: Optional[str], info: Any) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise InvalidContractStateError(
                f"fetch_spec.{info.field_name} 不得为空白字符串（要么省略，要么给值）"
            )
        return stripped

    @model_validator(mode="after")
    def _check_type_contract(self) -> "FetchSpec":
        allowed = set(SPEC_FIELDS_BY_FAMILY[self.type])
        provided = {
            name for name in _ALL_SPEC_FIELDS if getattr(self, name) is not None
        }

        required = set(REQUIRED_SPEC_FIELDS[self.type])
        missing = sorted(required - provided)
        if missing:
            raise InvalidContractStateError(
                f"fetch_spec.type={self.type.value} 缺少必填项 {missing}"
                f"（该类型的必填项：{sorted(required)}）"
            )

        stray = sorted(provided - allowed)
        if stray:
            family = (
                "字段路径"
                if self.type is FetchType.JSON_API
                else "选择器"
                if self.type is FetchType.HTML_XPATH
                else "无"
            )
            raise InvalidContractStateError(
                f"fetch_spec.type={self.type.value} 不接受字段 {stray}"
                f"（该类型允许的{family}字段：{sorted(allowed)}）；"
                "不认识的字段会被 adapter 静默忽略，因此在保存时拒绝"
            )

        if self.type in (FetchType.RSS, FetchType.ATOM) and provided:
            raise InvalidContractStateError(
                f"{self.type.value} 无需 fetch_spec 内容，收到 {sorted(provided)}"
            )
        return self

    def payload(self) -> Dict[str, Any]:
        """稳定可比的字典表示（用于 diff 与 OPML 序列化）。"""
        return self.model_dump(mode="json", exclude_none=True)


# --- Industry ----------------------------------------------------------------


class Industry(ContractModel):
    """行业 = AI 分类的标签空间 = feed 筛选维度 = 打标修正对象（SPEC §2.5）。"""

    id: str
    name: str
    keywords: Tuple[str, ...] = ()
    parent_id: Optional[str] = None
    # SPEC §2.9 把 enabled 标为必填：停用后不再采集，历史数据保留。
    # 这里不给默认值——"默认启用"是一个会影响采集行为的策略决定，必须由配置显式表达。
    enabled: bool

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return validate_id(value, what="industry.id")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise InvalidContractStateError("industry.name 不得为空")
        return value.strip()

    @field_validator("parent_id")
    @classmethod
    def _check_parent_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        validate_id(value, what="industry.parent_id")
        return value

    @field_validator("keywords")
    @classmethod
    def _check_keywords(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        cleaned = tuple(k.strip() for k in value)
        if any(not k for k in cleaned):
            raise InvalidContractStateError("industry.keywords 不得含空白词")
        if len(set(cleaned)) != len(cleaned):
            raise InvalidContractStateError(f"industry.keywords 含重复项：{cleaned}")
        return cleaned

    @model_validator(mode="after")
    def _check_self_parent(self) -> "Industry":
        if self.parent_id == self.id:
            raise InvalidContractStateError(f"industry.id={self.id!r} 不能以自己为父行业")
        return self

    def payload(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")


# --- Channel -----------------------------------------------------------------


class Channel(ContractModel):
    """渠道 = 一个具体可采集的端点（SPEC §2.9）。

    `user_agent` 是 UA **选项名**；默认禁止浏览器伪装（SPEC §7.1 合规底线）。
    需要例外时必须显式给出 `user_agent_justification`，否则保存即拒绝。
    """

    id: str
    industry_id: str
    type: FetchType
    endpoint: str
    fetch_spec: FetchSpec
    interval_seconds: int
    rate_limit_seconds: Optional[int] = None
    user_agent: Optional[str] = None
    user_agent_justification: Optional[str] = None
    enabled: bool
    tags: Tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return validate_id(value, what="channel.id")

    @field_validator("industry_id")
    @classmethod
    def _check_industry_id(cls, value: str) -> str:
        return validate_id(value, what="channel.industry_id")

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        return validate_endpoint(value)

    @field_validator("interval_seconds")
    @classmethod
    def _check_interval(cls, value: int) -> int:
        if value < MIN_INTERVAL_SECONDS:
            raise InvalidContractStateError(
                f"channel.interval_seconds={value} 低于下限 {MIN_INTERVAL_SECONDS}"
            )
        return value

    @field_validator("rate_limit_seconds")
    @classmethod
    def _check_rate_limit(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if value < MIN_RATE_LIMIT_SECONDS:
            raise InvalidContractStateError(
                f"channel.rate_limit_seconds={value} 低于下限 {MIN_RATE_LIMIT_SECONDS}"
            )
        return value

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        cleaned = tuple(t.strip() for t in value)
        if any(not t for t in cleaned):
            raise InvalidContractStateError("channel.tags 不得含空白标签")
        if len(set(cleaned)) != len(cleaned):
            raise InvalidContractStateError(f"channel.tags 含重复项：{cleaned}")
        return cleaned

    @model_validator(mode="after")
    def _check_type_consistency(self) -> "Channel":
        if self.fetch_spec.type is not self.type:
            raise InvalidContractStateError(
                f"channel.type={self.type.value} 与 "
                f"fetch_spec.type={self.fetch_spec.type.value} 不一致"
            )
        return self

    @model_validator(mode="after")
    def _check_user_agent(self) -> "Channel":
        if self.user_agent is None:
            if self.user_agent_justification is not None:
                raise InvalidContractStateError(
                    "未指定 user_agent 时不需要 user_agent_justification"
                )
            return self
        name = self.user_agent.strip()
        if not name:
            raise InvalidContractStateError("channel.user_agent 不得为空白")
        if name in BROWSER_MASQUERADE_USER_AGENTS:
            reason = (self.user_agent_justification or "").strip()
            if not reason:
                raise InvalidContractStateError(
                    f"user_agent={name!r} 属于浏览器伪装，默认禁止（SPEC §7.1）；"
                    "如确需例外，必须显式给出 user_agent_justification"
                )
        return self

    def payload(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")


# --- 跨对象不变量 -------------------------------------------------------------


def validate_registry(
    industries: Iterable[Industry],
    channels: Iterable[Channel],
    *,
    referenced_ids: Iterable[str] = (),
) -> None:
    """对**整份候选快照**跑跨对象校验；任何一条不满足即抛领域异常。

    规则（SPEC §2.9 校验规则 1 / 2）：

    1. `id` 唯一（行业与渠道各自命名空间内，且两类之间也不允许撞名，避免标签空间歧义）
    2. `industry_id` 必须指向已存在的行业
    3. `parent_id` 必须指向已存在的行业；不允许层级成环
    4. 被已存在标签引用的行业 `id` 必须仍然存在（不得删除；改名由版本层介入）

    它接受"候选快照"而不是"变更"，因此**保存前跑一次即可覆盖所有写入路径**
    （前端 CRUD、批量应用、OPML 导入全部收敛到这一处）。
    """
    industry_list = list(industries)
    channel_list = list(channels)

    industry_by_id: Dict[str, Industry] = {}
    for ind in industry_list:
        if ind.id in industry_by_id:
            raise InvalidContractStateError(f"行业 id 重复：{ind.id!r}")
        industry_by_id[ind.id] = ind

    channel_ids: Dict[str, Channel] = {}
    for ch in channel_list:
        if ch.id in channel_ids:
            raise InvalidContractStateError(f"渠道 id 重复：{ch.id!r}")
        if ch.id in industry_by_id:
            raise InvalidContractStateError(
                f"id={ch.id!r} 同时被行业与渠道占用：标签空间与采集空间会混淆"
            )
        channel_ids[ch.id] = ch

    for ind in industry_list:
        if ind.parent_id is not None and ind.parent_id not in industry_by_id:
            raise InvalidContractStateError(
                f"行业 {ind.id!r} 的 parent_id={ind.parent_id!r} 不存在"
            )
        # 层级成环检测（沿 parent 链走，路径长度上限 = 行业数）
        seen = {ind.id}
        cursor = ind.parent_id
        while cursor is not None:
            if cursor in seen:
                raise InvalidContractStateError(
                    f"行业层级成环：{ind.id!r} → … → {cursor!r}"
                )
            seen.add(cursor)
            parent = industry_by_id.get(cursor)
            if parent is None:
                break
            cursor = parent.parent_id

    for ch in channel_list:
        if ch.industry_id not in industry_by_id:
            raise InvalidContractStateError(
                f"渠道 {ch.id!r} 的 industry_id={ch.industry_id!r} 不存在"
            )

    missing_referenced = sorted(set(referenced_ids) - set(industry_by_id))
    if missing_referenced:
        raise InvalidContractStateError(
            f"行业 {missing_referenced} 已被标签引用，不得删除"
            "（SPEC §2.9 校验规则 1：被引用的 id 不得修改或删除）"
        )
