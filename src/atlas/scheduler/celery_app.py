"""
Celery应用模块

TASK-003: Celery任务队列系统
提供Celery应用实例和配置
"""

from celery import Celery
from typing import Dict, Any

from atlas.core.logging import get_logger
from .celery_config import get_celery_config

logger = get_logger(__name__)


def create_celery_app() -> Celery:
    """创建Celery应用实例

    Returns:
        Celery应用实例
    """
    # 获取配置
    config = get_celery_config()

    # 创建Celery应用
    app = Celery('atlas')

    # 应用配置
    app.config_from_object(config.to_celery_config())

    # 自动发现任务
    app.autodiscover_tasks(['atlas.scheduler'], related_name='celery_tasks')

    logger.info(f"Celery应用初始化完成 - Broker: {config.broker_url}")

    return app


# 全局Celery应用实例
celery_app = create_celery_app()


# 配置定时任务 (Beat Schedule)
@celery_app.on_after_configure.connect
def setup_periodic_tasks(sender: Celery, **kwargs: Dict[str, Any]) -> None:
    """配置定时任务

    Args:
        sender: Celery应用实例
        **kwargs: 其他参数
    """
    from .celery_tasks import register_periodic_tasks

    # 注册定时任务
    register_periodic_tasks(sender)

    logger.info("定时任务配置完成")


# 导出Celery应用
__all__ = ["celery_app", "create_celery_app"]
