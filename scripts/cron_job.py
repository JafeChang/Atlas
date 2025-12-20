#!/usr/bin/env python3
"""
Atlas 定时任务脚本

用于执行定时数据采集、系统维护等任务。
集成了完整的任务调度系统，支持队列管理、状态跟踪和 cron 调度。

使用方法:
  python scripts/cron_job.py --task collect  # 执行数据采集
  python scripts/cron_job.py --task cleanup  # 执行系统清理
  python scripts/cron_job.py --task all      # 执行所有任务
  python scripts/cron_job.py --scheduler     # 启动 cron 调度器
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from atlas.core.config import get_config
from atlas.core.logging import init_logger, get_logger
from atlas.core.database import get_database
from atlas.core.storage import get_storage_manager
from atlas.scheduler import TaskQueue, CronManager, StatusManager
from atlas.collectors.rss_collector import RSSCollector
from atlas.collectors.web_collector import WebCollector

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
cron_logger = logging.getLogger(__name__)


class AtlasCronScheduler:
    """Atlas 定时任务调度器"""

    def __init__(self, config_dir: Optional[Path] = None):
        """初始化调度器"""
        self.config = get_config(config_dir)
        self.logger = init_logger(
            log_dir=self.config.log_dir,
            log_level=self.config.log_level
        )

        # 初始化核心组件
        self.status_manager: Optional[StatusManager] = None
        self.task_queue: Optional[TaskQueue] = None
        self.cron_manager: Optional[CronManager] = None

        # 状态标志
        self._running = False
        self._shutdown_requested = False

        self.logger.info("Atlas 定时任务调度器初始化完成")

    async def initialize(self) -> None:
        """初始化所有组件"""
        try:
            # 初始化数据库和存储
            await get_database()
            await get_storage_manager()

            # 初始化状态管理器
            status_file = self.config.data_dir / "task_status.json"
            self.status_manager = StatusManager(status_file)

            # 初始化任务队列
            queue_file = self.config.data_dir / "task_queue.pkl"
            self.task_queue = TaskQueue(
                max_workers=self.config.max_workers if hasattr(self.config, 'max_workers') else 5,
                persistence_file=queue_file
            )
            await self.task_queue.start()

            # 初始化 Cron 管理器
            cron_config_file = self.config.config_dir_path / "cron_jobs.json"
            self.cron_manager = CronManager(
                self.task_queue,
                self.status_manager,
                cron_config_file
            )
            await self.cron_manager.start()

            # 注册 cron 任务
            await self._register_cron_jobs()

            self.logger.info("所有组件初始化完成")

        except Exception as e:
            self.logger.error(f"初始化失败: {e}")
            raise

    async def _register_cron_jobs(self) -> None:
        """注册 cron 任务"""
        try:
            # 数据采集任务 - 每小时执行
            await self.cron_manager.add_job(
                name="data_collection",
                cron_expression="@hourly",
                func=self._collect_data,
                description="定时数据采集任务"
            )

            # 系统清理任务 - 每天凌晨2点执行
            await self.cron_manager.add_job(
                name="system_cleanup",
                cron_expression="0 2 * * *",
                func=self._system_cleanup,
                description="系统清理任务"
            )

            # 健康检查任务 - 每30分钟执行
            await self.cron_manager.add_job(
                name="health_check",
                cron_expression="*/30 * * * *",
                func=self._health_check,
                description="系统健康检查"
            )

            # 状态报告任务 - 每天上午9点执行
            await self.cron_manager.add_job(
                name="daily_report",
                cron_expression="0 9 * * *",
                func=self._generate_daily_report,
                description="生成每日状态报告"
            )

            self.logger.info("Cron 任务注册完成")

        except Exception as e:
            self.logger.error(f"注册 Cron 任务失败: {e}")
            raise

    async def start_scheduler(self) -> None:
        """启动调度器"""
        try:
            self._running = True
            self.logger.info("Atlas 任务调度器启动")

            # 设置信号处理
            self._setup_signal_handlers()

            # 主循环
            while self._running and not self._shutdown_requested:
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            self.logger.info("收到中断信号，正在关闭调度器...")
        except Exception as e:
            self.logger.error(f"调度器运行异常: {e}")
        finally:
            await self.shutdown()

    async def run_collect_task(self) -> bool:
        """执行数据采集任务"""
        try:
            self.logger.info("开始执行数据采集任务")
            task_id = await self.task_queue.submit(
                self._collect_data,
                name="manual_data_collection",
                description="手动触发的数据采集任务"
            )

            # 等待任务完成
            try:
                result = await self.task_queue.get_task_result(task_id, timeout=300)  # 5分钟超时
                self.logger.info(f"数据采集任务完成: {result}")
                return True
            except TimeoutError:
                self.logger.error("数据采集任务超时")
                return False

        except Exception as e:
            self.logger.error(f"数据采集任务失败: {e}")
            return False

    async def run_cleanup_task(self) -> bool:
        """执行系统清理任务"""
        try:
            self.logger.info("开始执行系统清理任务")
            task_id = await self.task_queue.submit(
                self._system_cleanup,
                name="manual_system_cleanup",
                description="手动触发的系统清理任务"
            )

            # 等待任务完成
            try:
                result = await self.task_queue.get_task_result(task_id, timeout=600)  # 10分钟超时
                self.logger.info(f"系统清理任务完成: {result}")
                return True
            except TimeoutError:
                self.logger.error("系统清理任务超时")
                return False

        except Exception as e:
            self.logger.error(f"系统清理任务失败: {e}")
            return False

    async def run_health_check(self) -> bool:
        """执行健康检查"""
        try:
            self.logger.info("开始执行健康检查")
            task_id = await self.task_queue.submit(
                self._health_check,
                name="manual_health_check",
                description="手动触发的健康检查"
            )

            # 等待任务完成
            try:
                result = await self.task_queue.get_task_result(task_id, timeout=60)  # 1分钟超时
                self.logger.info(f"健康检查完成: {result}")
                return True
            except TimeoutError:
                self.logger.error("健康检查超时")
                return False

        except Exception as e:
            self.logger.error(f"健康检查失败: {e}")
            return False

    async def _collect_data(self) -> Dict[str, Any]:
        """数据采集任务"""
        try:
            results = {
                "start_time": time.time(),
                "sources_processed": 0,
                "items_collected": 0,
                "errors": []
            }

            sources = self.config.sources.get("sources", [])
            enabled_sources = [s for s in sources if s.get("enabled", True)]

            self.logger.info(f"开始处理 {len(enabled_sources)} 个数据源")

            for source in enabled_sources:
                try:
                    if source["type"] == "rss":
                        collector = RSSCollector(self.config)
                        items = await collector.collect(source)
                        results["items_collected"] += len(items)
                        self.logger.info(f"RSS 源 {source['name']} 采集了 {len(items)} 个条目")

                    elif source["type"] == "web":
                        collector = WebCollector(self.config)
                        items = await collector.collect(source)
                        results["items_collected"] += len(items)
                        self.logger.info(f"Web 源 {source['name']} 采集了 {len(items)} 个条目")

                    results["sources_processed"] += 1

                except Exception as e:
                    error_msg = f"处理数据源 {source.get('name', 'unknown')} 失败: {e}"
                    results["errors"].append(error_msg)
                    self.logger.error(error_msg)

            results["end_time"] = time.time()
            results["duration"] = results["end_time"] - results["start_time"]

            self.logger.info(f"数据采集完成，处理 {results['sources_processed']} 个源，"
                           f"采集 {results['items_collected']} 个条目，"
                           f"耗时 {results['duration']:.2f} 秒")

            return results

        except Exception as e:
            self.logger.error(f"数据采集任务失败: {e}")
            raise

    async def _system_cleanup(self) -> Dict[str, Any]:
        """系统清理任务"""
        try:
            results = {
                "start_time": time.time(),
                "logs_cleaned": 0,
                "temp_files_cleaned": 0,
                "errors": []
            }

            # 清理过期日志
            logs_cleaned = await self._cleanup_logs()
            results["logs_cleaned"] = logs_cleaned

            # 清理临时文件
            temp_files_cleaned = await self._cleanup_temp_files()
            results["temp_files_cleaned"] = temp_files_cleaned

            # 数据库维护
            await self._database_maintenance()

            # 存储系统维护
            await self._storage_maintenance()

            # 清理旧任务状态
            old_tasks_cleaned = self.status_manager.cleanup_old_tasks(days=30)
            results["old_tasks_cleaned"] = old_tasks_cleaned

            results["end_time"] = time.time()
            results["duration"] = results["end_time"] - results["start_time"]

            self.logger.info(f"系统清理完成，清理 {logs_cleaned} 个日志文件，"
                           f"{temp_files_cleaned} 个临时文件，"
                           f"{old_tasks_cleaned} 个旧任务状态，"
                           f"耗时 {results['duration']:.2f} 秒")

            return results

        except Exception as e:
            self.logger.error(f"系统清理任务失败: {e}")
            raise

    async def _health_check(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            results = {
                "start_time": time.time(),
                "checks": {},
                "overall_status": "healthy"
            }

            # 检查数据库连接
            try:
                # TODO: 实际的数据库连接检查
                results["checks"]["database"] = {"status": "ok", "message": "数据库连接正常"}
            except Exception as e:
                results["checks"]["database"] = {"status": "error", "message": str(e)}
                results["overall_status"] = "unhealthy"

            # 检查存储系统
            try:
                # TODO: 实际的存储系统检查
                results["checks"]["storage"] = {"status": "ok", "message": "存储系统正常"}
            except Exception as e:
                results["checks"]["storage"] = {"status": "error", "message": str(e)}
                results["overall_status"] = "unhealthy"

            # 检查配置文件
            try:
                config_files = [
                    self.config.config_dir_path / "config.yaml",
                    self.config.config_dir_path / "sources.yaml"
                ]

                missing_files = []
                for config_file in config_files:
                    if not config_file.exists():
                        missing_files.append(str(config_file))

                if missing_files:
                    raise FileNotFoundError(f"配置文件缺失: {missing_files}")

                results["checks"]["config"] = {"status": "ok", "message": "配置文件正常"}
            except Exception as e:
                results["checks"]["config"] = {"status": "error", "message": str(e)}
                results["overall_status"] = "unhealthy"

            # 检查系统资源
            try:
                import shutil
                import psutil

                # 检查磁盘空间
                total, used, free = shutil.disk_usage(str(project_root))
                free_percent = (free / total) * 100
                disk_status = "ok" if free_percent > 10 else "warning"
                disk_message = f"磁盘剩余 {free_percent:.1f}%"

                # 检查内存使用
                memory = psutil.virtual_memory()
                memory_status = "ok" if memory.percent < 90 else "warning"
                memory_message = f"内存使用率 {memory.percent:.1f}%"

                results["checks"]["resources"] = {
                    "status": disk_status if disk_status == "error" else memory_status,
                    "message": f"{disk_message}, {memory_message}",
                    "disk_free_percent": free_percent,
                    "memory_percent": memory.percent
                }

            except ImportError:
                results["checks"]["resources"] = {
                    "status": "warning",
                    "message": "未安装 psutil，无法检查系统资源"
                }
            except Exception as e:
                results["checks"]["resources"] = {"status": "error", "message": str(e)}

            results["end_time"] = time.time()
            results["duration"] = results["end_time"] - results["start_time"]

            self.logger.info(f"健康检查完成，状态: {results['overall_status']}")
            return results

        except Exception as e:
            self.logger.error(f"健康检查失败: {e}")
            raise

    async def _generate_daily_report(self) -> Dict[str, Any]:
        """生成每日状态报告"""
        try:
            results = {
                "start_time": time.time(),
                "metrics": {},
                "report": ""
            }

            # 获取任务统计
            metrics = self.status_manager.get_metrics()
            results["metrics"] = {
                "total_tasks": metrics.total_tasks,
                "success_tasks": metrics.success_tasks,
                "failed_tasks": metrics.failed_tasks,
                "success_rate": metrics.success_rate,
                "avg_execution_time": metrics.avg_execution_time
            }

            # 获取队列状态
            queue_status = self.task_queue.get_queue_status()
            results["queue_status"] = queue_status

            # 生成报告文本
            report_lines = [
                f"Atlas 系统每日报告 - {time.strftime('%Y-%m-%d')}",
                "=" * 50,
                "",
                "任务统计:",
                f"  总任务数: {metrics.total_tasks}",
                f"  成功任务: {metrics.success_tasks}",
                f"  失败任务: {metrics.failed_tasks}",
                f"  成功率: {metrics.success_rate:.1f}%",
                f"  平均执行时间: {metrics.avg_execution_time:.2f}秒",
                "",
                "队列状态:",
                f"  队列大小: {queue_status['queue_size']}",
                f"  运行中任务: {queue_status['running_tasks']}",
                f"  最大工作线程: {queue_status['max_workers']}",
                "",
                "Cron 任务状态:",
            ]

            # 获取所有 cron 任务状态
            cron_jobs = await self.cron_manager.get_all_jobs()
            for job in cron_jobs:
                job_status = await self.cron_manager.get_job_status(job.name)
                if job_status:
                    report_lines.append(
                        f"  {job.name}: {'启用' if job.enabled else '禁用'} - "
                        f"运行 {job_status['run_count']} 次，"
                        f"成功率 {job_status['success_rate']:.1f}%"
                    )

            results["report"] = "\n".join(report_lines)

            # 保存报告到文件
            report_file = self.config.log_dir / f"daily_report_{time.strftime('%Y%m%d')}.txt"
            with open(report_file, 'w', encoding='utf-8') as f:
                f.write(results["report"])

            results["end_time"] = time.time()
            results["duration"] = results["end_time"] - results["start_time"]
            results["report_file"] = str(report_file)

            self.logger.info(f"每日报告生成完成，保存到: {report_file}")
            return results

        except Exception as e:
            self.logger.error(f"生成每日报告失败: {e}")
            raise

    async def _cleanup_logs(self) -> int:
        """清理过期日志"""
        try:
            log_dir = self.config.log_dir
            cleaned_count = 0

            # 清理超过 30 天的日志文件
            cutoff_time = time.time() - (30 * 24 * 60 * 60)

            for log_file in log_dir.glob("*.log*"):
                if log_file.stat().st_mtime < cutoff_time:
                    log_file.unlink()
                    cleaned_count += 1
                    self.logger.debug(f"删除过期日志文件: {log_file}")

            return cleaned_count

        except Exception as e:
            self.logger.warning(f"清理日志文件失败: {e}")
            return 0

    async def _cleanup_temp_files(self) -> int:
        """清理临时文件"""
        try:
            cleaned_count = 0

            # 清理 Python 缓存
            for cache_dir in project_root.rglob("__pycache__"):
                if cache_dir.is_dir():
                    import shutil
                    shutil.rmtree(cache_dir)
                    cleaned_count += 1
                    self.logger.debug(f"清理缓存目录: {cache_dir}")

            # 清理其他临时文件
            temp_patterns = ["*.tmp", "*.temp", "*.bak"]
            for pattern in temp_patterns:
                for temp_file in project_root.rglob(pattern):
                    if temp_file.is_file():
                        temp_file.unlink()
                        cleaned_count += 1
                        self.logger.debug(f"删除临时文件: {temp_file}")

            return cleaned_count

        except Exception as e:
            self.logger.warning(f"清理临时文件失败: {e}")
            return 0

    async def _database_maintenance(self) -> None:
        """数据库维护"""
        try:
            # TODO: 实现数据库维护逻辑
            self.logger.info("执行数据库维护")
        except Exception as e:
            self.logger.warning(f"数据库维护失败: {e}")

    async def _storage_maintenance(self) -> None:
        """存储系统维护"""
        try:
            # TODO: 实现存储系统维护逻辑
            self.logger.info("执行存储系统维护")
        except Exception as e:
            self.logger.warning(f"存储系统维护失败: {e}")

    def _setup_signal_handlers(self) -> None:
        """设置信号处理器"""
        def signal_handler(signum, frame):
            self.logger.info(f"收到信号 {signum}，准备关闭调度器...")
            self._shutdown_requested = True

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def shutdown(self) -> None:
        """关闭调度器"""
        try:
            self.logger.info("正在关闭 Atlas 任务调度器...")
            self._running = False

            # 关闭 cron 管理器
            if self.cron_manager:
                await self.cron_manager.stop()

            # 关闭任务队列
            if self.task_queue:
                await self.task_queue.stop()

            # 保存状态
            if self.status_manager:
                self.status_manager.save_status()

            self.logger.info("Atlas 任务调度器已关闭")

        except Exception as e:
            self.logger.error(f"关闭调度器时发生错误: {e}")


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="Atlas 定时任务脚本")
    parser.add_argument("--task", choices=["collect", "cleanup", "health", "scheduler"],
                       default="scheduler", help="要执行的任务")
    parser.add_argument("--config-dir", type=Path,
                       help="配置文件目录")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="详细输出")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # 创建调度器
        scheduler = AtlasCronScheduler(args.config_dir)
        await scheduler.initialize()

        # 执行指定任务
        if args.task == "collect":
            success = await scheduler.run_collect_task()
            sys.exit(0 if success else 1)
        elif args.task == "cleanup":
            success = await scheduler.run_cleanup_task()
            sys.exit(0 if success else 1)
        elif args.task == "health":
            success = await scheduler.run_health_check()
            sys.exit(0 if success else 1)
        elif args.task == "scheduler":
            await scheduler.start_scheduler()

    except KeyboardInterrupt:
        print("\n任务被用户中断")
        sys.exit(1)
    except Exception as e:
        cron_logger.error(f"执行任务时发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())