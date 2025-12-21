"""
Atlas 任务调度器

提供定时任务调度功能，支持 cron表达式配置。
"""

import asyncio
import schedule
import threading
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path
import json

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Atlas 任务调度器"""

    def __init__(self):
        self.tasks = []
        self.running = False
        self.scheduler_thread = None

    def add_task(self, name: str, func, cron_expression: str, description: str = ""):
        """添加定时任务"""
        try:
            # 简单的cron表达式解析 (分钟/小时/日/月/周)
            parts = cron_expression.split()
            if len(parts) != 5:
                raise ValueError("cron表达式格式错误，应为: 分钟/小时/日/月/周")

            minute, hour, day, month, weekday = parts
            task_func = None

            # 创建调度函数 - 简化处理，支持常见的cron格式
            if minute.startswith("*/"):
                # 每N分钟格式
                minutes = int(minute[2:])
                task_func = schedule.every(minutes).minutes
            elif minute == "*":
                if hour.startswith("*/"):
                    # 每N小时格式
                    hours = int(hour[2:])
                    task_func = schedule.every(hours).hours
                elif hour == "*":
                    # 每分钟执行
                    task_func = schedule.every().minute
                else:
                    # 特定小时，每分钟执行
                    task_func = schedule.every().hour.at(f"{hour.zfill(2)}:00")
            else:
                # 特定分钟
                if hour == "*":
                    task_func = schedule.every().hour.at(f":{minute.zfill(2)}")
                else:
                    task_func = schedule.every().day.at(f"{hour.zfill(2)}:{minute.zfill(2)}")

            if day != "*" and hour == "*" and minute == "*":
                task_func = schedule.every().day.at(f"00:00")

            task_func.do(func)

            task = {
                "name": name,
                "func": func,
                "cron": cron_expression,
                "description": description,
                "last_run": None,
                "next_run": None,
                "enabled": True
            }

            self.tasks.append(task)
            logger.info(f"添加定时任务: {name} - {cron_expression}")

        except Exception as e:
            logger.error(f"添加任务失败: {e}")
            raise

    def remove_task(self, name: str):
        """移除任务"""
        self.tasks = [t for t in self.tasks if t["name"] != name]
        logger.info(f"移除定时任务: {name}")

    def get_tasks(self) -> List[Dict]:
        """获取任务列表"""
        tasks = []
        for task in self.tasks:
            next_run = self._get_next_run(task)
            tasks.append({
                "name": task["name"],
                "cron": task["cron"],
                "description": task["description"],
                "enabled": task["enabled"],
                "last_run": task["last_run"],
                "next_run": next_run
            })
        return tasks

    def _get_next_run(self, task: Dict) -> Optional[str]:
        """获取下次运行时间"""
        try:
            # 计算下次运行时间
            now = datetime.now()
            parts = task["cron"].split()
            minute, hour, day, month, weekday = parts

            next_run = now

            # 解析分钟字段
            if minute.startswith("*/"):
                # 每N分钟格式，如 */30
                interval = int(minute[2:])
                # 计算下一个间隔时间点
                minutes_past_hour = now.minute
                next_interval = ((minutes_past_hour // interval) + 1) * interval
                if next_interval >= 60:
                    # 下一个小时
                    next_run = next_run.replace(second=0, microsecond=0)
                    next_run += timedelta(hours=1, minutes=next_interval - 60)
                else:
                    next_run = next_run.replace(minute=next_interval, second=0, microsecond=0)
            elif minute == "*":
                # 每分钟执行
                next_run = next_run.replace(second=0, microsecond=0)
                next_run += timedelta(minutes=1)
            else:
                # 特定分钟
                target_minute = int(minute)
                if hour == "*":
                    # 每小时的特定分钟
                    if now.minute < target_minute:
                        next_run = next_run.replace(minute=target_minute, second=0, microsecond=0)
                    else:
                        # 下个小时
                        next_run = next_run.replace(minute=target_minute, second=0, microsecond=0)
                        next_run += timedelta(hours=1)
                else:
                    # 特定小时和分钟
                    target_hour = int(hour)
                    next_run = next_run.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                    if next_run <= now:
                        next_run += timedelta(days=1)

            # 确保下次运行时间在未来
            while next_run <= now:
                if minute.startswith("*/"):
                    interval = int(minute[2:])
                    next_run += timedelta(minutes=interval)
                elif minute == "*":
                    next_run += timedelta(minutes=1)
                else:
                    next_run += timedelta(days=1)

            return next_run.isoformat()

        except Exception as e:
            logger.error(f"计算下次运行时间失败: {e}")
            return None

    def run_pending_tasks(self):
        """运行待执行的任务"""
        now = datetime.now()
        for task in self.tasks:
            if task["enabled"]:
                next_run = self._get_next_run(task)
                if next_run and datetime.fromisoformat(next_run) <= now:
                    try:
                        logger.info(f"执行定时任务: {task['name']}")
                        task["func"]()
                        task["last_run"] = now.isoformat()
                    except Exception as e:
                        logger.error(f"执行任务 {task['name']} 失败: {e}")

    def start(self):
        """启动调度器"""
        if self.running:
            logger.warning("调度器已在运行中")
            return

        self.running = True
        logger.info("启动任务调度器")

        def run_scheduler():
            while self.running:
                schedule.run_pending()
                time.sleep(1)

        self.scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        self.scheduler_thread.start()

        logger.info("任务调度器已启动")

    def stop(self):
        """停止调度器"""
        if not self.running:
            return

        self.running = False
        logger.info("正在停止任务调度器...")

        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5)

        logger.info("任务调度器已停止")

    def get_schedule_status(self) -> Dict:
        """获取调度器状态"""
        return {
            "running": self.running,
            "tasks_count": len(self.tasks),
            "enabled_tasks": len([t for t in self.tasks if t["enabled"]]),
            "next_run_times": [
                task["name"] + ": " + (self._get_next_run(task) or "N/A")
                for task in self.tasks if task["enabled"]
            ]
        }


# 全局调度器实例
scheduler = TaskScheduler()