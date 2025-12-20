"""
告警管理模块

提供系统告警、通知和规则管理功能。
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any, Callable, Union
from pathlib import Path

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertStatus(Enum):
    """告警状态"""
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


@dataclass
class Alert:
    """告警对象"""
    id: str
    rule_name: str
    level: AlertLevel
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    status: AlertStatus = AlertStatus.ACTIVE
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    acknowledgment: Optional[str] = None

    def resolve(self, resolved_by: str = "system") -> None:
        """解决告警"""
        self.status = AlertStatus.RESOLVED
        self.resolved_at = datetime.now()
        self.resolved_by = resolved_by

    def acknowledge(self, user: str, message: str = "") -> None:
        """确认告警"""
        self.acknowledgment = f"{user}: {message}" if message else user


@dataclass
class AlertRule:
    """告警规则"""
    name: str
    description: str
    condition: Callable[[Dict[str, Any]], bool]
    level: AlertLevel
    message: str
    enabled: bool = True
    cooldown_seconds: int = 300  # 5分钟冷却
    last_triggered: Optional[datetime] = None
    suppression_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def should_trigger(self, metrics: Dict[str, Any]) -> bool:
        """检查是否应该触发告警"""
        if not self.enabled:
            return False

        # 检查冷却时间
        if self.last_triggered:
            cooldown_end = self.last_triggered + timedelta(seconds=self.cooldown_seconds)
            if datetime.now() < cooldown_end:
                self.suppression_count += 1
                return False

        # 检查条件
        try:
            return self.condition(metrics)
        except Exception as e:
            logger.error(f"告警规则 {self.name} 条件检查失败: {e}")
            return False

    def trigger(self) -> None:
        """触发告警"""
        self.last_triggered = datetime.now()


class AlertNotifier(ABC):
    """告警通知器抽象基类"""

    @abstractmethod
    async def send_alert(self, alert: Alert) -> bool:
        """发送告警通知

        Args:
            alert: 告警对象

        Returns:
            是否发送成功
        """
        pass


class ConsoleNotifier(AlertNotifier):
    """控制台通知器"""

    async def send_alert(self, alert: Alert) -> bool:
        """发送告警到控制台"""
        level_icons = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.ERROR: "❌",
            AlertLevel.CRITICAL: "🚨"
        }

        icon = level_icons.get(alert.level, "📢")
        timestamp = alert.timestamp.strftime("%H:%M:%S")

        print(f"\n{icon} [{alert.level.value.upper()}] {alert.message}")
        print(f"   时间: {timestamp}")
        print(f"   规则: {alert.rule_name}")
        print(f"   ID: {alert.id}")

        if alert.details:
            print("   详情:")
            for key, value in alert.details.items():
                print(f"     - {key}: {value}")

        return True


class FileNotifier(AlertNotifier):
    """文件通知器"""

    def __init__(self, log_file: Path):
        """初始化文件通知器

        Args:
            log_file: 日志文件路径
        """
        self.log_file = log_file

    async def send_alert(self, alert: Alert) -> bool:
        """发送告警到文件"""
        try:
            # 确保目录存在
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

            alert_data = {
                "id": alert.id,
                "rule_name": alert.rule_name,
                "level": alert.level.value,
                "message": alert.message,
                "details": alert.details,
                "timestamp": alert.timestamp.isoformat(),
                "status": alert.status.value
            }

            with open(self.log_file, 'a', encoding='utf-8') as f:
                import json
                f.write(json.dumps(alert_data) + '\n')

            return True

        except Exception as e:
            logger.error(f"写入告警文件失败: {e}")
            return False


class EmailNotifier(AlertNotifier):
    """邮件通知器"""

    def __init__(self, smtp_config: Dict[str, Any]):
        """初始化邮件通知器

        Args:
            smtp_config: SMTP配置
        """
        self.smtp_config = smtp_config

    async def send_alert(self, alert: Alert) -> bool:
        """发送告警邮件"""
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            # 创建邮件
            msg = MIMEMultipart()
            msg['From'] = self.smtp_config['from']
            msg['To'] = ', '.join(self.smtp_config['to'])
            msg['Subject'] = f"[Atlas Alert] {alert.level.value.upper()}: {alert.message}"

            # 邮件内容
            body = f"""
告警详情:
- 级别: {alert.level.value}
- 消息: {alert.message}
- 规则: {alert.rule_name}
- 时间: {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}
- ID: {alert.id}

详细信息:
{chr(10).join(f'- {k}: {v}' for k, v in alert.details.items())}
"""

            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            # 发送邮件
            server = smtplib.SMTP(self.smtp_config['host'], self.smtp_config['port'])
            if self.smtp_config.get('use_tls', True):
                server.starttls()

            if self.smtp_config.get('username') and self.smtp_config.get('password'):
                server.login(self.smtp_config['username'], self.smtp_config['password'])

            server.send_message(msg)
            server.quit()

            return True

        except Exception as e:
            logger.error(f"发送告警邮件失败: {e}")
            return False


class WebhookNotifier(AlertNotifier):
    """Webhook通知器"""

    def __init__(self, webhook_url: str, headers: Optional[Dict[str, str]] = None):
        """初始化Webhook通知器

        Args:
            webhook_url: Webhook URL
            headers: HTTP请求头
        """
        self.webhook_url = webhook_url
        self.headers = headers or {}

    async def send_alert(self, alert: Alert) -> bool:
        """发送告警到Webhook"""
        try:
            from atlas.collectors.http_client import HTTPClient

            http_client = HTTPClient()

            payload = {
                "id": alert.id,
                "rule_name": alert.rule_name,
                "level": alert.level.value,
                "message": alert.message,
                "details": alert.details,
                "timestamp": alert.timestamp.isoformat(),
                "status": alert.status.value
            }

            response = await http_client.post(
                self.webhook_url,
                json=payload,
                headers=self.headers
            )

            return response and response.status_code == 200

        except Exception as e:
            logger.error(f"发送Webhook告警失败: {e}")
            return False


class AlertManager:
    """告警管理器"""

    def __init__(self, check_interval: float = 60.0):
        """初始化告警管理器

        Args:
            check_interval: 检查间隔（秒）
        """
        self.check_interval = check_interval
        self.rules: Dict[str, AlertRule] = {}
        self.notifiers: List[AlertNotifier] = []
        self.active_alerts: Dict[str, Alert] = {}
        self.alert_history: List[Alert] = []
        self._running = False
        self._check_task: Optional[asyncio.Task] = None

        # 统计信息
        self.stats = {
            "total_alerts": 0,
            "alerts_by_level": {level.value: 0 for level in AlertLevel},
            "alerts_by_rule": {},
            "resolved_alerts": 0
        }

    def register_rule(self, rule: AlertRule) -> None:
        """注册告警规则

        Args:
            rule: 告警规则
        """
        self.rules[rule.name] = rule
        logger.info(f"注册告警规则: {rule.name}")

    def unregister_rule(self, rule_name: str) -> None:
        """注销告警规则

        Args:
            rule_name: 规则名称
        """
        if rule_name in self.rules:
            del self.rules[rule_name]
            logger.info(f"注销告警规则: {rule_name}")

    def add_notifier(self, notifier: AlertNotifier) -> None:
        """添加告警通知器

        Args:
            notifier: 通知器
        """
        self.notifiers.append(notifier)
        logger.info(f"添加告警通知器: {type(notifier).__name__}")

    def remove_notifier(self, notifier: AlertNotifier) -> None:
        """移除告警通知器

        Args:
            notifier: 通知器
        """
        if notifier in self.notifiers:
            self.notifiers.remove(notifier)

    async def start(self) -> None:
        """启动告警管理器"""
        if self._running:
            logger.warning("告警管理器已经在运行")
            return

        self._running = True
        logger.info(f"启动告警管理器，检查间隔: {self.check_interval}秒")

        self._check_task = asyncio.create_task(self._monitoring_loop())

    async def stop(self) -> None:
        """停止告警管理器"""
        if not self._running:
            return

        self._running = False
        logger.info("停止告警管理器")

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

    async def _monitoring_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                # 这里需要从性能监控器获取指标
                # 为了简化，我们跳过实际的指标检查
                pass
            except Exception as e:
                logger.error(f"告警监控循环失败: {e}")

            await asyncio.sleep(self.check_interval)

    async def check_alerts(self, metrics: Dict[str, Any]) -> List[Alert]:
        """检查告警规则

        Args:
            metrics: 系统指标

        Returns:
            触发的告警列表
        """
        triggered_alerts = []

        for rule_name, rule in self.rules.items():
            try:
                if rule.should_trigger(metrics):
                    # 创建告警
                    alert_id = f"{rule_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    alert = Alert(
                        id=alert_id,
                        rule_name=rule_name,
                        level=rule.level,
                        message=rule.message,
                        details={
                            "metrics": metrics,
                            "rule_description": rule.description
                        }
                    )

                    # 更新规则状态
                    rule.trigger()

                    # 添加到活动告警
                    self.active_alerts[alert_id] = alert
                    self.alert_history.append(alert)

                    # 更新统计信息
                    self._update_stats(alert)

                    # 发送通知
                    await self._send_notifications(alert)

                    triggered_alerts.append(alert)

                    logger.warning(f"触发告警: [{alert.level.value}] {alert.message}")

            except Exception as e:
                logger.error(f"检查告警规则 {rule_name} 失败: {e}")

        return triggered_alerts

    async def _send_notifications(self, alert: Alert) -> None:
        """发送告警通知

        Args:
            alert: 告警对象
        """
        if not self.notifiers:
            return

        # 并行发送通知
        notification_tasks = [
            notifier.send_alert(alert) for notifier in self.notifiers
        ]

        results = await asyncio.gather(*notification_tasks, return_exceptions=True)

        # 统计发送结果
        success_count = sum(1 for r in results if r is True)
        logger.info(f"告警 {alert.id} 通知发送完成: {success_count}/{len(self.notifiers)} 成功")

    def _update_stats(self, alert: Alert) -> None:
        """更新统计信息

        Args:
            alert: 告警对象
        """
        self.stats["total_alerts"] += 1
        self.stats["alerts_by_level"][alert.level.value] += 1
        self.stats["alerts_by_rule"][alert.rule_name] = \
            self.stats["alerts_by_rule"].get(alert.rule_name, 0) + 1

    def resolve_alert(self, alert_id: str, resolved_by: str = "user") -> bool:
        """解决告警

        Args:
            alert_id: 告警ID
            resolved_by: 解决者

        Returns:
            是否成功解决
        """
        if alert_id in self.active_alerts:
            alert = self.active_alerts[alert_id]
            alert.resolve(resolved_by)

            # 从活动告警中移除
            del self.active_alerts[alert_id]

            # 更新统计信息
            self.stats["resolved_alerts"] += 1

            logger.info(f"告警已解决: {alert_id}")
            return True

        return False

    def acknowledge_alert(self, alert_id: str, user: str, message: str = "") -> bool:
        """确认告警

        Args:
            alert_id: 告警ID
            user: 确认用户
            message: 确认消息

        Returns:
            是否成功确认
        """
        if alert_id in self.active_alerts:
            alert = self.active_alerts[alert_id]
            alert.acknowledge(user, message)

            logger.info(f"告警已确认: {alert_id} by {user}")
            return True

        return False

    def get_active_alerts(self, level: Optional[AlertLevel] = None) -> List[Alert]:
        """获取活动告警

        Args:
            level: 告警级别过滤

        Returns:
            活动告警列表
        """
        alerts = list(self.active_alerts.values())

        if level:
            alerts = [a for a in alerts if a.level == level]

        return sorted(alerts, key=lambda x: x.timestamp, reverse=True)

    def get_recent_alerts(
        self,
        hours: int = 24,
        level: Optional[AlertLevel] = None,
        status: Optional[AlertStatus] = None
    ) -> List[Alert]:
        """获取最近的告警

        Args:
            hours: 时间范围（小时）
            level: 告警级别过滤
            status: 告警状态过滤

        Returns:
            告警列表
        """
        cutoff_time = datetime.now() - timedelta(hours=hours)

        alerts = [
            alert for alert in self.alert_history
            if alert.timestamp >= cutoff_time
        ]

        if level:
            alerts = [a for a in alerts if a.level == level]

        if status:
            alerts = [a for a in alerts if a.status == status]

        return sorted(alerts, key=lambda x: x.timestamp, reverse=True)

    def get_statistics(self) -> Dict[str, Any]:
        """获取告警统计信息"""
        active_alerts_by_level = {
            level.value: len([a for a in self.active_alerts.values() if a.level == level])
            for level in AlertLevel
        }

        return {
            **self.stats,
            "active_alerts_count": len(self.active_alerts),
            "active_alerts_by_level": active_alerts_by_level,
            "active_rules": len([r for r in self.rules.values() if r.enabled]),
            "notifiers_count": len(self.notifiers)
        }

    def get_rule_status(self) -> Dict[str, Dict[str, Any]]:
        """获取规则状态"""
        return {
            name: {
                "enabled": rule.enabled,
                "last_triggered": rule.last_triggered.isoformat() if rule.last_triggered else None,
                "suppression_count": rule.suppression_count,
                "description": rule.description
            }
            for name, rule in self.rules.items()
        }


# 预定义告警规则
class AlertRules:
    """预定义的告警规则集合"""

    @staticmethod
    def high_cpu_usage(threshold: float = 80.0) -> AlertRule:
        """高CPU使用率告警规则"""
        return AlertRule(
            name="high_cpu_usage",
            description=f"CPU使用率超过{threshold}%",
            condition=lambda metrics: metrics.get("cpu_percent", 0) > threshold,
            level=AlertLevel.WARNING,
            message=f"CPU使用率过高: {{cpu_percent:.1f}}%",
            cooldown_seconds=300
        )

    @staticmethod
    def high_memory_usage(threshold: float = 85.0) -> AlertRule:
        """高内存使用率告警规则"""
        return AlertRule(
            name="high_memory_usage",
            description=f"内存使用率超过{threshold}%",
            condition=lambda metrics: metrics.get("memory_percent", 0) > threshold,
            level=AlertLevel.WARNING,
            message=f"内存使用率过高: {{memory_percent:.1f}}%",
            cooldown_seconds=300
        )

    @staticmethod
    def low_disk_space(threshold: float = 90.0) -> AlertRule:
        """低磁盘空间告警规则"""
        return AlertRule(
            name="low_disk_space",
            description=f"磁盘使用率超过{threshold}%",
            condition=lambda metrics: metrics.get("disk_usage_percent", 0) > threshold,
            level=AlertLevel.CRITICAL,
            message=f"磁盘空间不足: {{disk_usage_percent:.1f}}%",
            cooldown_seconds=600
        )

    @staticmethod
    def high_error_rate(threshold: float = 10.0) -> AlertRule:
        """高错误率告警规则"""
        return AlertRule(
            name="high_error_rate",
            description=f"错误率超过{threshold}%",
            condition=lambda metrics: metrics.get("error_rate", 0) > threshold,
            level=AlertLevel.ERROR,
            message=f"系统错误率过高: {{error_rate:.1f}}%",
            cooldown_seconds=180
        )

    @staticmethod
    def service_unavailable() -> AlertRule:
        """服务不可用告警规则"""
        return AlertRule(
            name="service_unavailable",
            description="关键服务不可用",
            condition=lambda metrics: not metrics.get("service_available", True),
            level=AlertLevel.CRITICAL,
            message="关键服务不可用",
            cooldown_seconds=60
        )


# 全局告警管理器实例
_global_alert_manager: Optional[AlertManager] = None


def get_global_alert_manager() -> Optional[AlertManager]:
    """获取全局告警管理器实例"""
    return _global_alert_manager


def set_global_alert_manager(manager: AlertManager) -> None:
    """设置全局告警管理器实例"""
    global _global_alert_manager
    _global_alert_manager = manager