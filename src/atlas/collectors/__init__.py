"""
Atlas 数据采集模块

提供各种数据源的采集功能。
"""

from .base import BaseCollector, CollectorFactory
from .rss_collector import RSSCollector
from .web_collector import WebCollector

__all__ = [
    "BaseCollector",
    "CollectorFactory",
    "RSSCollector",
    "WebCollector"
]