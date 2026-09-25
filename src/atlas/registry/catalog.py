"""预置渠道目录：勾选 + 覆盖默认值（T-101，SPEC §2.9「选配体验」）。

这是 C8 体验的核心：用户不是从零填表，而是**从一份可直接勾选的目录里选**，
再按需覆盖个别默认值。三条设计约束：

1. **目录是结构化数据，不是硬编码枚举**：`PrebuiltCatalog` 只是一组模板对象。
   行业同样来自配置数据 —— 代码里没有"行业枚举"这种东西（SPEC §2.5 闭环约束）。
2. **勾选 + 覆盖默认值**：`CatalogSelection` 一次勾选一个模板，可覆盖
   `interval_seconds` / `tags` / `enabled` / `user_agent` 等；未覆盖的字段走模板默认值。
3. **覆盖结果必须仍走 `schema.py` 的校验，不得有旁路**：`instantiate_channel()`
   的最后一步永远是 `Channel(**payload)`，随后由 `ConfigStore.commit()` 做跨对象校验。
   因此"勾选一个模板"与"手工写一条渠道"经过的是同一条校验路径。

预置目录的内容取自项目已有的真实来源（`config/sources.yaml` 里在用的 arXiv 分类与
RSS 源），不自行发明端点；目录的持续保鲜是 T-111 的职责。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import Field, field_validator

from atlas.contracts import ContractModel, InvalidContractStateError, NotFoundError

from .schema import Channel, FetchSpec, FetchType, Industry, validate_endpoint, validate_id

__all__ = [
    "CHANNEL_OVERRIDABLE_FIELDS",
    "INDUSTRY_OVERRIDABLE_FIELDS",
    "CatalogEntry",
    "CatalogSelection",
    "ChannelTemplate",
    "IndustryTemplate",
    "PrebuiltCatalog",
    "default_catalog",
    "instantiate_channel",
    "instantiate_industry",
]

#: 勾选后允许覆盖的字段。**不含** `id` / `type` / `endpoint` / `fetch_spec`：
#: 换端点或换协议等于换了一个渠道，应当新增而不是覆盖（避免目录里出现"同名不同源"）。
CHANNEL_OVERRIDABLE_FIELDS = frozenset(
    {
        "interval_seconds",
        "rate_limit_seconds",
        "tags",
        "enabled",
        "user_agent",
        "user_agent_justification",
    }
)

#: 行业模板允许覆盖的字段（同样不含 `id`：id 一旦被标签引用即不可改）。
INDUSTRY_OVERRIDABLE_FIELDS = frozenset({"name", "keywords", "parent_id", "enabled"})


class IndustryTemplate(ContractModel):
    """一条可直接勾选的行业模板。"""

    id: str
    name: str
    keywords: Tuple[str, ...] = ()
    parent_id: Optional[str] = None
    enabled: bool = True
    description: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return validate_id(value, what="industry template.id")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise InvalidContractStateError("行业模板 name 不得为空")
        return value.strip()

    def defaults(self) -> Dict[str, Any]:
        """模板默认值（展开成 `Industry` 的字段）。"""
        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "keywords": self.keywords,
            "enabled": self.enabled,
        }
        if self.parent_id is not None:
            payload["parent_id"] = self.parent_id
        else:
            payload["parent_id"] = None
        return payload


class ChannelTemplate(ContractModel):
    """一条可直接勾选的渠道模板 + 它的默认值。"""

    id: str
    industry_id: str
    type: FetchType
    endpoint: str
    fetch_spec: Dict[str, Any] = Field(default_factory=dict)
    interval_seconds: int = 3600
    rate_limit_seconds: Optional[int] = None
    user_agent: Optional[str] = None
    enabled: bool = True
    tags: Tuple[str, ...] = ()
    name: Optional[str] = None
    description: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return validate_id(value, what="channel template.id")

    @field_validator("industry_id")
    @classmethod
    def _check_industry_id(cls, value: str) -> str:
        return validate_id(value, what="channel template.industry_id")

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        return validate_endpoint(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> Any:
        return tuple(value)

    def defaults(self) -> Dict[str, Any]:
        """模板默认值（展开成 `Channel` 的完整字段）。

        这里**只给默认值**，不做校验决定：真正的合法性由 `Channel(**payload)` 判定。
        """
        spec = {"type": self.type.value, **self.fetch_spec}
        payload: Dict[str, Any] = {
            "id": self.id,
            "industry_id": self.industry_id,
            "type": self.type.value,
            "endpoint": self.endpoint,
            "fetch_spec": spec,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "tags": self.tags,
        }
        if self.rate_limit_seconds is not None:
            payload["rate_limit_seconds"] = self.rate_limit_seconds
        if self.user_agent is not None:
            payload["user_agent"] = self.user_agent
        return payload


class CatalogSelection(ContractModel):
    """一次勾选：选中一个模板，并可覆盖若干默认值。

    `industry_id` 允许覆盖归属（勾选后改挂到别的行业），它属于"结构归属"而非
    渠道身份，因此与 `id` / `endpoint` 区别对待。
    """

    template_id: str
    overrides: Dict[str, Any] = Field(default_factory=dict)
    industry_id: Optional[str] = None

    @field_validator("template_id")
    @classmethod
    def _check_template_id(cls, value: str) -> str:
        return validate_id(value, what="selection.template_id")

    @field_validator("overrides")
    @classmethod
    def _check_overridable(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        unknown = sorted(set(value) - CHANNEL_OVERRIDABLE_FIELDS)
        if unknown:
            raise InvalidContractStateError(
                f"模板覆盖字段 {unknown} 不被允许；可覆盖："
                f"{sorted(CHANNEL_OVERRIDABLE_FIELDS)}"
                "（换 id / endpoint / type 等于换了一个渠道，请改为新增）"
            )
        return value


class CatalogEntry(ContractModel):
    """目录里的一条可勾选条目：模板 + 它的默认值展开结果。"""

    template: ChannelTemplate
    defaults: Dict[str, Any]

    @property
    def id(self) -> str:
        return self.template.id


class PrebuiltCatalog(ContractModel):
    """一份预置目录：行业模板 + 渠道模板。

    它**不是**行业枚举：行业清单同样来自这里的数据，而这份数据可以被整体替换
    （T-111 聚合出来的目录就是另一个 `PrebuiltCatalog`）。
    """

    name: str = "预置目录"
    industries: Tuple[IndustryTemplate, ...] = ()
    channels: Tuple[ChannelTemplate, ...] = ()

    @field_validator("industries", "channels", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return tuple(value)

    # --- 查：行业模板 -------------------------------------------------------
    def industry_template(self, template_id: str) -> IndustryTemplate:
        for template in self.industries:
            if template.id == template_id:
                return template
        raise NotFoundError(
            f"目录里没有行业模板 {template_id!r}（现有：{[i.id for i in self.industries]}）"
        )

    # --- 查：渠道模板 -------------------------------------------------------
    def channel_template(self, template_id: str) -> ChannelTemplate:
        for template in self.channels:
            if template.id == template_id:
                return template
        raise NotFoundError(
            f"目录里没有渠道模板 {template_id!r}（现有：{[c.id for c in self.channels]}）"
        )

    def channel_templates(
        self,
        *,
        industry_id: Optional[str] = None,
        tag: Optional[str] = None,
        fetch_type: Optional[FetchType] = None,
    ) -> Tuple[ChannelTemplate, ...]:
        records = list(self.channels)
        if industry_id is not None:
            records = [t for t in records if t.industry_id == industry_id]
        if tag is not None:
            records = [t for t in records if tag in t.tags]
        if fetch_type is not None:
            records = [t for t in records if t.type is fetch_type]
        return tuple(records)

    def entries(self, **kwargs: Any) -> Tuple[CatalogEntry, ...]:
        """可直接展示给前端的条目列表（模板 + 默认值）。"""
        return tuple(
            CatalogEntry(template=template, defaults=template.defaults())
            for template in self.channel_templates(**kwargs)
        )

    def templates_by_industry(self) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
        """按行业分组，供前端渲染"勾选树"。"""
        grouped: Dict[str, List[str]] = {t.id: [] for t in self.industries}
        for template in self.channels:
            grouped.setdefault(template.industry_id, []).append(template.id)
        return tuple(
            (industry_id, tuple(sorted(ids))) for industry_id, ids in sorted(grouped.items())
        )

    # --- 勾选：实例化 -------------------------------------------------------
    def instantiate(self, selection: CatalogSelection) -> Channel:
        """勾选一条模板并套用覆盖，得到**已通过 schema 校验**的 `Channel`。"""
        template = self.channel_template(selection.template_id)
        return instantiate_channel(template, selection)

    def instantiate_industry(self, template_id: str, **overrides: Any) -> Industry:
        return instantiate_industry(self.industry_template(template_id), overrides)

    def select(self, selections: Iterable[CatalogSelection]) -> Tuple[Channel, ...]:
        """批量勾选；任何一条非法都整体失败（不产出半成品）。"""
        return tuple(self.instantiate(selection) for selection in selections)

    def required_industries(
        self, selections: Iterable[CatalogSelection]
    ) -> Tuple[Industry, ...]:
        """一组勾选所依赖的行业（含祖先行业），来自目录数据。

        必须把**祖先行业**一并带出：`validate_registry()` 要求 `parent_id` 指向已存在
        的行业，只建被直接引用的那个行业会让整次提交被拒。
        返回顺序是 `parent_id` 拓扑序，可直接用于一次提交。
        """
        selection_list = list(selections)
        needed: Dict[str, IndustryTemplate] = {}
        for selection in selection_list:
            channel = self.instantiate(selection)
            cursor: Optional[str] = channel.industry_id
            while cursor is not None and cursor not in needed:
                template = self.industry_template(cursor)
                needed[template.id] = template
                cursor = template.parent_id
        return _parents_first(
            tuple(instantiate_industry(t, {}) for t in needed.values())
        )


def instantiate_industry(
    template: IndustryTemplate, overrides: Dict[str, Any]
) -> Industry:
    """把行业模板 + 覆盖展开成 `Industry`（末尾必然经过 schema 校验）。"""
    unknown = sorted(set(overrides) - INDUSTRY_OVERRIDABLE_FIELDS)
    if unknown:
        raise InvalidContractStateError(
            f"行业模板 {template.id!r} 不支持覆盖 {unknown}；可覆盖："
            f"{sorted(INDUSTRY_OVERRIDABLE_FIELDS)}"
        )
    payload = template.defaults()
    payload.update(overrides)
    return Industry(**payload)


def instantiate_channel(
    template: ChannelTemplate, selection: CatalogSelection
) -> Channel:
    """模板默认值 + 勾选覆盖 → `Channel`。

    **没有旁路**：无论覆盖与否，返回的对象都是 `Channel(**payload)` 构造出来的，
    于是单对象校验（id 格式 / endpoint / interval 下限 / UA 合规 / fetch_spec 与 type
    一致）全部照跑；跨对象校验（industry_id 必须存在）由随后的 `commit()` 承担。
    """
    payload = template.defaults()
    if selection.industry_id is not None:
        payload["industry_id"] = validate_id(
            selection.industry_id, what="selection.industry_id"
        )
    for key, value in selection.overrides.items():
        payload[key] = _coerce_override(key, value)
    return Channel(**payload)


def _coerce_override(key: str, value: Any) -> Any:
    """把 JSON/YAML 友好的输入归一化（list → tuple）；不做校验。"""
    if key == "tags" and isinstance(value, list):
        return tuple(value)
    return value


def _parents_first(records: Sequence[Industry]) -> Tuple[Industry, ...]:
    """按父行业优先排序（父行业可能出现在子行业之后，提交时顺序要稳）。"""
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


def default_catalog() -> PrebuiltCatalog:
    """项目自带的一份预置目录（种子，不是全集；保鲜归 T-111）。

    行业 id 使用 `config/sources.yaml` 已在用的 arXiv 分类（`cs.LG` / `cs.CV` /
    `cs.CL` / `stat.ML`）的规范化短横线形式 —— SPEC §2.5 要求"主题本体的种子
    取自现成分类，不自行发明"。
    """
    industries = (
        IndustryTemplate(
            id="machine-learning",
            name="机器学习",
            keywords=("machine learning", "deep learning", "neural network"),
            description="arXiv cs.LG 分类对应的机器学习方向",
        ),
        IndustryTemplate(
            id="computer-vision",
            name="计算机视觉",
            keywords=("computer vision", "image", "vision"),
            description="arXiv cs.CV 分类对应的计算机视觉方向",
        ),
        IndustryTemplate(
            id="natural-language",
            name="自然语言处理",
            keywords=("nlp", "language model", "transformer"),
            description="arXiv cs.CL 分类对应的自然语言处理方向",
        ),
        IndustryTemplate(
            id="statistical-learning",
            name="统计机器学习",
            keywords=("statistics", "statistical learning", "probability"),
            description="arXiv stat.ML 分类对应的统计机器学习方向",
        ),
    )

    channels = (
        ChannelTemplate(
            id="arxiv-machine-learning",
            industry_id="machine-learning",
            type=FetchType.RSS,
            endpoint="https://arxiv.org/rss/cs.LG",
            interval_seconds=7200,
            tags=("research", "papers", "arxiv"),
            name="arXiv 机器学习最新论文",
        ),
        ChannelTemplate(
            id="arxiv-computer-vision",
            industry_id="computer-vision",
            type=FetchType.RSS,
            endpoint="https://arxiv.org/rss/cs.CV",
            interval_seconds=7200,
            tags=("research", "papers", "arxiv"),
            name="arXiv 计算机视觉最新论文",
        ),
        ChannelTemplate(
            id="arxiv-natural-language",
            industry_id="natural-language",
            type=FetchType.RSS,
            endpoint="https://arxiv.org/rss/cs.CL",
            interval_seconds=7200,
            tags=("research", "papers", "arxiv"),
            name="arXiv 自然语言处理最新论文",
        ),
        ChannelTemplate(
            id="arxiv-statistical-learning",
            industry_id="statistical-learning",
            type=FetchType.RSS,
            endpoint="https://arxiv.org/rss/stat.ML",
            interval_seconds=7200,
            tags=("research", "papers", "arxiv"),
            name="arXiv 统计机器学习最新论文",
        ),
        ChannelTemplate(
            id="openai-blog",
            industry_id="machine-learning",
            type=FetchType.RSS,
            endpoint="https://openai.com/blog/rss/",
            interval_seconds=3600,
            tags=("official", "llm", "research"),
            name="OpenAI 官方博客",
        ),
        ChannelTemplate(
            id="google-ai-blog",
            industry_id="machine-learning",
            type=FetchType.ATOM,
            endpoint="https://googleaiblog.blogspot.com/atom.xml",
            interval_seconds=3600,
            tags=("official", "research"),
            name="Google AI 官方博客",
        ),
        ChannelTemplate(
            id="synced-review",
            industry_id="machine-learning",
            type=FetchType.RSS,
            endpoint="https://syncedreview.com/feed",
            interval_seconds=1800,
            tags=("industry", "news"),
            name="Synced Review",
        ),
        ChannelTemplate(
            id="hacker-news-frontpage",
            industry_id="machine-learning",
            type=FetchType.JSON_API,
            endpoint="https://hn.algolia.com/api/v1/search_by_date?tags=story",
            fetch_spec={
                "list_path": "hits",
                "title_path": "title",
                "url_path": "url",
                "time_path": "created_at",
                "content_path": "story_text",
            },
            interval_seconds=3600,
            tags=("news", "community"),
            name="Hacker News 最新条目",
            description="演示 json_api 类型：字段路径随 type 变化，无需写代码",
        ),
    )

    return PrebuiltCatalog(
        name="Atlas 预置目录（种子）",
        industries=industries,
        channels=channels,
    )
