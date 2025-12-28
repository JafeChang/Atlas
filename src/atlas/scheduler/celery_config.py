"""
Celery配置模块

TASK-003: Celery任务队列系统
提供Celery配置，支持从环境变量读取配置
"""

import os
from typing import Optional, Dict, Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class CeleryConfig(BaseSettings):
    """Celery配置类"""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Redis配置
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    redis_max_connections: int = 10

    # Celery Broker配置
    celery_broker_url: Optional[str] = None
    celery_result_backend: Optional[str] = None

    # 任务配置
    celery_task_track_started: bool = True
    celery_task_time_limit: int = 3600  # 1小时
    celery_task_soft_time_limit: int = 3000  # 50分钟
    celery_worker_prefetch_multiplier: int = 4
    celery_worker_max_tasks_per_child: int = 1000
    celery_task_acks_late: bool = True
    celery_task_reject_on_worker_lost: bool = True
    celery_result_expires: int = 86400  # 24小时
    celery_task_send_sent_event: bool = True

    # Beat调度器配置
    celery_beat_schedule_filename: str = "celerybeat-schedule"
    celery_beat_sync_every: int = 0  # 不强制同步

    # 日志配置
    celery_worker_log_format: str = "[%(asctime)s: %(levelname)s/%(processName)s] %(message)s"
    celery_worker_task_log_format: str = "[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s"
    celery_worker_log_color: bool = True

    @property
    def broker_url(self) -> str:
        """获取Broker URL"""
        if self.celery_broker_url:
            return self.celery_broker_url

        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        else:
            return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def result_backend(self) -> str:
        """获取Result Backend URL"""
        if self.celery_result_backend:
            return self.celery_result_backend

        # 使用不同的数据库存储结果
        result_db = self.redis_db + 1
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{result_db}"
        else:
            return f"redis://{self.redis_host}:{self.redis_port}/{result_db}"

    def to_celery_config(self) -> Dict[str, Any]:
        """转换为Celery配置字典"""
        return {
            # Broker配置
            "broker_url": self.broker_url,
            "result_backend": self.result_backend,

            # 任务配置
            "task_track_started": self.celery_task_track_started,
            "task_time_limit": self.celery_task_time_limit,
            "task_soft_time_limit": self.celery_task_soft_time_limit,
            "task_acks_late": self.celery_task_acks_late,
            "task_reject_on_worker_lost": self.celery_task_reject_on_worker_lost,
            "task_send_sent_event": self.celery_task_send_sent_event,
            "result_expires": self.celery_result_expires,

            # Worker配置
            "worker_prefetch_multiplier": self.celery_worker_prefetch_multiplier,
            "worker_max_tasks_per_child": self.celery_worker_max_tasks_per_child,

            # Beat配置
            "beat_schedule_filename": self.celery_beat_schedule_filename,
            "beat_sync_every": self.celery_beat_sync_every,

            # 日志配置
            "worker_log_format": self.celery_worker_log_format,
            "worker_task_log_format": self.celery_worker_task_log_format,
            "worker_log_color": self.celery_worker_log_color,

            # 时区配置
            "timezone": "UTC",
            "enable_utc": True,

            # 任务序列化
            "task_serializer": "json",
            "result_serializer": "json",
            "accept_content": ["json"],

            # 结果压缩
            "result_compression": "gzip",

            # 任务路由 (可根据需要自定义)
            "task_routes": {
                "atlas.scheduler.celery_tasks.data_collection": {"queue": "data_collection"},
                "atlas.scheduler.celery_tasks.data_processing": {"queue": "data_processing"},
                "atlas.scheduler.celery_tasks.periodic_tasks": {"queue": "periodic"},
            },

            # 任务优先级 (需要Redis或RabbitMQ支持)
            "task_default_priority": 5,
            "worker_disable_rate_limits": False,
        }


# 全局配置实例
_config: Optional[CeleryConfig] = None


def get_celery_config() -> CeleryConfig:
    """获取Celery配置（单例模式）"""
    global _config
    if _config is None:
        _config = CeleryConfig()
    return _config


def reload_celery_config() -> CeleryConfig:
    """重新加载Celery配置"""
    global _config
    _config = CeleryConfig()
    return _config
