"""专用关键词预筛选；OR 组间 AND，不把预筛选当成最终语义分类。"""

from __future__ import annotations

import re
from enum import StrEnum


class BusinessType(StrEnum):
    PRIMARY = "PRIMARY_ALUMINIUM_SMELTING"
    ALUMINA = "ALUMINA_REFINING"
    SECONDARY = "SECONDARY_ALUMINIUM_RECYCLING"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class EventType(StrEnum):
    NEW_PROJECT = "NEW_PROJECT"
    CONSTRUCTION = "CONSTRUCTION"
    ENERGIZATION = "ENERGIZATION"
    FIRST_METAL = "FIRST_METAL"
    COMMERCIAL_PRODUCTION = "COMMERCIAL_PRODUCTION"
    RAMP_UP = "RAMP_UP"
    FULL_CAPACITY = "FULL_CAPACITY"
    CAPACITY_EXPANSION = "CAPACITY_EXPANSION"
    CURTAILMENT = "CURTAILMENT"
    SHUTDOWN = "SHUTDOWN"
    RESTART = "RESTART"
    PERMANENT_CLOSURE = "PERMANENT_CLOSURE"
    CAPACITY_UPDATE = "CAPACITY_UPDATE"


INDUSTRY_KEYWORDS = (
    "aluminium smelter",
    "aluminum smelter",
    "primary aluminium",
    "primary aluminum",
    "potline",
    "reduction cell",
    "electrolysis",
    "smelter aluminium",
    "produksi aluminium primer",
    "电解铝",
    "原铝",
)
EVENT_KEYWORDS = (
    "commissioning",
    "first hot metal",
    "first metal",
    "commercial production",
    "capacity expansion",
    "ramp-up",
    "ramp up",
    "full production",
    "nameplate capacity",
    "restart",
    "resumption",
    "curtailment",
    "shutdown",
    "production suspension",
    "permanent closure",
    "power outage",
    "kapasitas produksi",
    "mulai beroperasi",
    "peresmian smelter",
    "new project",
    "construction",
    "energization",
    "full capacity",
    "capacity",
    "production",
    "投产",
    "扩产",
    "停产",
    "复产",
    "产能",
    "产量",
)


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold().replace("–", "-").replace("‑", "-"))


def keyword_matches(text: str) -> dict[str, list[str] | bool]:
    value = normalized(text)

    # 词边界避免 electrolysis 在不相关单词内部碰撞；复数 smelters 仍可命中词组。
    def hits(words):
        return [
            w
            for w in words
            if re.search(r"(?<!\w)" + re.escape(w) + r"(?:s)?(?!\w)", value)
        ]

    industry, events = hits(INDUSTRY_KEYWORDS), hits(EVENT_KEYWORDS)
    return {
        "industry": industry,
        "events": events,
        "candidate": bool(industry and events),
    }


def business_hint(text: str) -> BusinessType:
    value = normalized(text)
    primary = any(w in value for w in INDUSTRY_KEYWORDS)
    alumina = any(w in value for w in ("alumina", "氧化铝", "bauksit"))
    secondary = any(
        w in value
        for w in (
            "recycled aluminium",
            "recycled aluminum",
            "secondary aluminium",
            "secondary aluminum",
            "recycling",
            "再生铝",
        )
    )
    if sum((primary, alumina, secondary)) > 1:
        return BusinessType.MIXED
    if primary:
        return BusinessType.PRIMARY
    if alumina:
        return BusinessType.ALUMINA
    if secondary:
        return BusinessType.SECONDARY
    return BusinessType.UNKNOWN
