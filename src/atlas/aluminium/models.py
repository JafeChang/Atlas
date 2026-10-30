"""输入与模型输出严格校验；未披露数据为 null，模型不能指定来源身份。"""

from __future__ import annotations

import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .rules import BusinessType, EventType

INTERVALS = {"P0": 21600, "P1": 43200, "P2": 86400}
INGESTIONS = {
    "RSS",
    "web/data",
    "web/download",
    "web",
    "web/PDF",
    "RSS directory",
    "RSS search",
    "email",
    "web/API subject to SEC policy",
    "licensed web/API subject to contract",
}


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    region: str = Field(min_length=1)
    type: str = Field(min_length=1)
    url: str
    ingestion: str
    priority: Literal["P0", "P1", "P2"]
    status: Literal["verified", "constructed-unfetched", "domain-only"]
    focus: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("信源必须为不含凭据的 HTTP(S) URL")
        return value

    @field_validator("ingestion")
    @classmethod
    def valid_ingestion(cls, value):
        if value not in INGESTIONS:
            raise ValueError(f"未知 ingestion: {value}")
        return value

    @field_validator("name", "region", "type", "focus")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("字段不得为空白")
        return value

    @property
    def mode(self):
        if self.type == "paid intelligence" or self.ingestion.startswith("licensed"):
            return "paid"
        if self.ingestion == "email":
            return "email"
        if self.ingestion == "RSS directory":
            return "rss_directory"
        if "API" in self.ingestion:
            return "api_pending"
        if self.ingestion.startswith("RSS"):
            return "rss"
        return "web"

    @property
    def pending_reason(self):
        return {
            "paid": "需要授权：缺少有效订阅合同和接口权限；不采集付费内容",
            "email": "待配置：系统没有邮件订阅接收器",
            "rss_directory": "待配置：RSS 目录不是订阅流，需选择具体 feed",
            "api_pending": "待配置：未提供已验证的 SEC API 端点、发行人及合规身份配置",
        }.get(self.mode) or (
            "待配置：domain-only 仅为网站入口" if self.status == "domain-only" else None
        )


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    as_of: date
    notes: list[str]
    sources: list[Source]

    @model_validator(mode="after")
    def unique(self):
        from .store import canonical_url

        urls = [canonical_url(s.url) for s in self.sources]
        if len(set(urls)) != len(urls):
            raise ValueError("JSON 中有重复信源 URL")
        return self


def collection_source(source: Source, connection=None) -> Source:
    """解析已单独配置的目录订阅；原始清单及其验证状态保持不变。"""
    if not connection:
        return source
    if source.mode != "rss_directory":
        raise ValueError("仅 RSS 目录支持配置公开订阅；授权和邮件渠道不能由此启用")
    return Source.model_validate(
        {
            **source.model_dump(),
            "url": connection["endpoint"],
            "ingestion": "RSS",
            # 配置地址本身不构成验证证据；通过 probe 才可启用。
            "status": "constructed-unfetched",
        }
    )


def probe_verified(probe, connection=None, channel=None):
    """旧目录探测或其他端点的成功记录不能验证新订阅地址。"""
    if not probe or not probe.get("collectible"):
        return False
    if (
        channel
        and "chromium" in channel.get("tags", ())
        and (
            probe.get("transport") != "chromium"
            or probe.get("user_agent") != channel.get("user_agent")
        )
    ):
        return False
    if not connection:
        return True
    from .store import canonical_url

    return bool(
        probe.get("endpoint")
        and canonical_url(probe["endpoint"]) == canonical_url(connection["endpoint"])
    )


def capacity_tpy(
    value: str | int | float | Decimal | None, unit: str = "tpy"
) -> float | None:
    """仅转换明确的年产能单位；不把月产量、实际产量当作年产能。"""
    if value is None:
        return None
    units = {
        "tpy": 1,
        "tpa": 1,
        "tonnes/year": 1,
        "tonnes per year": 1,
        "吨/年": 1,
        "ktpa": 1000,
        "ktpy": 1000,
        "kt/y": 1000,
        "thousand tonnes/year": 1000,
        "mtpa": 1000000,
        "mtpy": 1000000,
        "million tonnes/year": 1000000,
        "tonnes per annum": 1,
        "tonnes/year capacity": 1,
        "million tonnes per year": 1000000,
        "million tonnes per annum": 1000000,
        "thousand tonnes per year": 1000,
        "thousand tonnes per annum": 1000,
        "万吨/年": 10000,
        "short tons/year": 0.90718474,
    }
    factor = units.get(unit.casefold().strip())
    if factor is None or isinstance(value, bool):
        raise ValueError(f"不明确的年产能单位或数值: {unit}")
    try:
        number = str(value).replace(",", "").strip()
        suffix = re.fullmatch(r"(\d+(?:\.\d+)?)([km])", number, re.I)
        multiplier = 1
        if suffix:
            if factor != 1:
                raise ValueError("缩写数值不能与已放大的单位重复换算")
            number = suffix[1]
            multiplier = 1000 if suffix[2].lower() == "k" else 1000000
        result = float(Decimal(number) * Decimal(str(factor)) * multiplier)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("产能数值无效") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("产能必须为有限非负数")
    return result


def explicit_date_in_quote(value: date, quote: str) -> bool:
    """只接受完整年月日；季度、月份或月末不能自动补成某一天。"""
    if (
        value.isoformat() in quote
        or f"{value.year}年{value.month}月{value.day}日" in quote
    ):
        return True
    months = (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )
    month = months[value.month - 1]
    name = f"(?:{month}|{month[:3]}\\.?)"
    day = f"0?{value.day}(?:st|nd|rd|th)?"
    return bool(
        re.search(
            rf"\b(?:{day}\s+{name}[,\s]+{value.year}|{name}\s+{day}[,\s]+{value.year})\b",
            quote,
            re.I,
        )
    )


CAPACITY_FIELDS = (
    "announced_capacity_tpy",
    "added_capacity_tpy",
    "restarted_capacity_tpy",
    "curtailed_capacity_tpy",
    "operating_capacity_tpy",
)


class NumericEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_value: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class Intelligence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    country: str | None = None
    company: str | None = None
    project_name: str | None = None
    smelter_name: str | None = None
    event_type: EventType | None = None
    event_date: date | None = None
    announced_capacity_tpy: float | None = Field(default=None, ge=0)
    added_capacity_tpy: float | None = Field(default=None, ge=0)
    restarted_capacity_tpy: float | None = Field(default=None, ge=0)
    curtailed_capacity_tpy: float | None = Field(default=None, ge=0)
    operating_capacity_tpy: float | None = Field(default=None, ge=0)
    actual_production_tonnes: float | None = Field(default=None, ge=0)
    expected_full_capacity_date: date | None = None
    project_status: str | None = None
    source_url: str | None = None
    source_type: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: str | None = None
    numeric_evidence: dict[str, NumericEvidence] = Field(default_factory=dict)
    business_type: BusinessType = BusinessType.UNKNOWN
    # 计划日期单独存放；event_date 只记录实际发生日期。
    event_status: Literal["planned", "actual", "unknown"] = "unknown"
    planned_event_date: date | None = None
    country_is_overseas: bool | None = None
    primary_capacity_explicit: bool = False
    corrects_source_url: str | None = None
    title_zh: str = Field(min_length=1, max_length=150)
    summary_zh: str = Field(min_length=1, max_length=600)

    @field_validator(
        *CAPACITY_FIELDS, "actual_production_tonnes", "confidence", mode="before"
    )
    @classmethod
    def numeric(cls, value):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError("数值必须为 JSON number 或 null")
        return value

    @field_validator("title_zh", "summary_zh")
    @classmethod
    def chinese(cls, value):
        import re

        if not re.search(r"[\u3400-\u9fff]", value):
            raise ValueError("必须输出中文译题和摘要")
        return value

    @model_validator(mode="after")
    def evidence_required(self):
        if (
            any(
                getattr(self, f) is not None
                for f in (*CAPACITY_FIELDS, "actual_production_tonnes")
            )
            and not self.evidence
        ):
            raise ValueError("产能/产量数据必须附原文证据")
        if self.event_status != "actual" and self.event_date is not None:
            raise ValueError("计划或未知事件不得填写实际事件日期")
        for field in (
            "event_date",
            "expected_full_capacity_date",
            "planned_event_date",
        ):
            value = getattr(self, field)
            if value and not explicit_date_in_quote(value, self.evidence or ""):
                raise ValueError(
                    f"{field} 必须有完整年月日的原文引用；月份/季度/月末不能补日，日期应为 null"
                )
        if (
            self.primary_capacity_explicit
            and self.business_type != BusinessType.PRIMARY
        ):
            raise ValueError("只有明确拆分的原铝事件才能确认原铝产能")
        if self.event_type == EventType.FULL_CAPACITY and self.event_status == "actual":
            import re

            if not re.search(
                r"full (?:capacity|production)|(?:reached|at) (?:the )?nameplate|达产",
                self.evidence or "",
                re.I,
            ):
                raise ValueError("全面达产事件必须有达产原文证据")
        return self

    @property
    def eligible_for_capacity(self):
        return (
            self.business_type == BusinessType.PRIMARY
            and self.primary_capacity_explicit
            and self.country_is_overseas is True
            and self.event_status == "actual"
            and bool(self.country)
            and self.country.casefold().strip()
            not in (
                "china",
                "prc",
                "中国",
                "中华人民共和国",
                "people's republic of china",
            )
        )
