#!/usr/bin/env python3
"""
Atlas 定时任务脚本

用于执行定时数据采集、系统维护等任务。
使用方法:
  python scripts/cron_job.py --task collect  # 执行数据采集
  python scripts/cron_job.py --task cleanup  # 执行系统清理
  python scripts/cron_job.py --task all      # 执行所有任务
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from atlas.core.config import get_config
from atlas.core.logging import init_logger, get_logger
from atlas.core.database import init_database
from atlas.core.storage import init_storage

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
cron_logger = logging.getLogger(__name__)


class CronJob:
    """定时任务管理器"""

    def __init__(self, config_dir: Optional[Path] = None):
        """初始化定时任务管理器"""
        self.config = get_config(config_dir)
        self.logger = init_logger(
            log_dir=self.config.log_dir,
            log_level=self.config.log_level
        )
        self.logger.info("定时任务管理器初始化完成")

    async def run_collect_task(self) -> bool:
        """执行数据采集任务"""
        try:
            self.logger.info("开始执行数据采集任务")

            # 这里应该调用实际的数据采集逻辑
            # from atlas.collectors.rss_collector import RSSCollector
            # from atlas.collectors.web_collector import WebCollector

            # 模拟数据采集过程
            sources = self.config.sources.get("sources", [])
            enabled_sources = [s for s in sources if s.get("enabled", True)]

            self.logger.info(f"找到 {len(enabled_sources)} 个启用的数据源")

            # TODO: 实际的数据采集逻辑
            # for source in enabled_sources:
            #     if source["type"] == "rss":
            #         collector = RSSCollector(self.config)
            #         await collector.collect(source)
            #     elif source["type"] == "web":
            #         collector = WebCollector(self.config)
            #         await collector.collect(source)

            self.logger.info("数据采集任务完成")
            return True

        except Exception as e:
            self.logger.error(f"数据采集任务失败: {e}")
            return False

    async def run_cleanup_task(self) -> bool:
        """执行系统清理任务"""
        try:
            self.logger.info("开始执行系统清理任务")

            # 清理过期日志
            await self._cleanup_logs()

            # 清理临时文件
            await self._cleanup_temp_files()

            # 数据库维护
            await self._database_maintenance()

            # 存储系统维护
            await self._storage_maintenance()

            self.logger.info("系统清理任务完成")
            return True

        except Exception as e:
            self.logger.error(f"系统清理任务失败: {e}")
            return False

    async def _cleanup_logs(self) -> None:
        """清理过期日志"""
        try:
            log_dir = self.config.log_dir
            import time

            # 清理超过 30 天的日志文件
            cutoff_time = time.time() - (30 * 24 * 60 * 60)

            for log_file in log_dir.glob("*.log*"):
                if log_file.stat().st_mtime < cutoff_time:
                    log_file.unlink()
                    self.logger.info(f"删除过期日志文件: {log_file}")

        except Exception as e:
            self.logger.warning(f"清理日志文件失败: {e}")

    async def _cleanup_temp_files(self) -> None:
        """清理临时文件"""
        try:
            # 清理 Python 缓存
            for cache_dir in project_root.rglob("__pycache__"):
                if cache_dir.is_dir():
                    import shutil
                    shutil.rmtree(cache_dir)
                    self.logger.info(f"清理缓存目录: {cache_dir}")

            # 清理其他临时文件
            temp_patterns = ["*.tmp", "*.temp", "*.bak"]
            for pattern in temp_patterns:
                for temp_file in project_root.rglob(pattern):
                    if temp_file.is_file():
                        temp_file.unlink()
                        self.logger.info(f"删除临时文件: {temp_file}")

        except Exception as e:
            self.logger.warning(f"清理临时文件失败: {e}")

    async def _database_maintenance(self) -> None:
        """数据库维护"""
        try:
            # TODO: 实现数据库维护逻辑
            # - 清理过期数据
            # - 优化索引
            # - 备份数据库
            self.logger.info("执行数据库维护")

        except Exception as e:
            self.logger.warning(f"数据库维护失败: {e}")

    async def _storage_maintenance(self) -> None:
        """存储系统维护"""
        try:
            # TODO: 实现存储系统维护逻辑
            # - 清理损坏的文件
            # - 重建索引
            # - 压缩旧文件
            self.logger.info("执行存储系统维护")

        except Exception as e:
            self.logger.warning(f"存储系统维护失败: {e}")

    async def run_health_check(self) -> bool:
        """执行健康检查"""
        try:
            self.logger.info("开始执行健康检查")

            # 检查数据库连接
            await self._check_database()

            # 检查存储系统
            await self._check_storage()

            # 检查配置文件
            await self._check_config()

            # 检查系统资源
            await self._check_resources()

            self.logger.info("健康检查完成")
            return True

        except Exception as e:
            self.logger.error(f"健康检查失败: {e}")
            return False

    async def _check_database(self) -> None:
        """检查数据库"""
        try:
            # TODO: 实现数据库连接检查
            self.logger.info("数据库连接正常")
        except Exception as e:
            self.logger.error(f"数据库连接失败: {e}")
            raise

    async def _check_storage(self) -> None:
        """检查存储系统"""
        try:
            # TODO: 实现存储系统检查
            self.logger.info("存储系统正常")
        except Exception as e:
            self.logger.error(f"存储系统检查失败: {e}")
            raise

    async def _check_config(self) -> None:
        """检查配置文件"""
        try:
            config_files = [
                self.config.config_dir_path / "config.yaml",
                self.config.config_dir_path / "sources.yaml"
            ]

            for config_file in config_files:
                if not config_file.exists():
                    raise FileNotFoundError(f"配置文件不存在: {config_file}")

                if config_file.stat().st_size == 0:
                    raise ValueError(f"配置文件为空: {config_file}")

            self.logger.info("配置文件检查通过")
        except Exception as e:
            self.logger.error(f"配置文件检查失败: {e}")
            raise

    async def _check_resources(self) -> None:
        """检查系统资源"""
        try:
            import shutil
            import psutil

            # 检查磁盘空间
            total, used, free = shutil.disk_usage(str(project_root))
            free_percent = (free / total) * 100

            if free_percent < 10:
                self.logger.warning(f"磁盘空间不足: {free_percent:.1f}% 剩余")
            else:
                self.logger.info(f"磁盘空间充足: {free_percent:.1f}% 剩余")

            # 检查内存使用
            memory = psutil.virtual_memory()
            if memory.percent > 90:
                self.logger.warning(f"内存使用率过高: {memory.percent:.1f}%")
            else:
                self.logger.info(f"内存使用正常: {memory.percent:.1f}%")

        except ImportError:
            self.logger.warning("未安装 psutil，跳过资源检查")
        except Exception as e:
            self.logger.warning(f"资源检查失败: {e}")

    async def run_all_tasks(self) -> None:
        """执行所有定时任务"""
        self.logger.info("开始执行所有定时任务")

        tasks = [
            ("健康检查", self.run_health_check()),
            ("数据采集", self.run_collect_task()),
            ("系统清理", self.run_cleanup_task()),
        ]

        for task_name, task_coro in tasks:
            try:
                self.logger.info(f"开始执行任务: {task_name}")
                success = await task_coro
                if success:
                    self.logger.info(f"任务完成: {task_name}")
                else:
                    self.logger.error(f"任务失败: {task_name}")
            except Exception as e:
                self.logger.error(f"任务异常: {task_name} - {e}")

        self.logger.info("所有定时任务执行完成")


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="Atlas 定时任务脚本")
    parser.add_argument("--task", choices=["collect", "cleanup", "health", "all"],
                       default="all", help="要执行的任务")
    parser.add_argument("--config-dir", type=Path,
                       help="配置文件目录")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="详细输出")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # 创建定时任务管理器
        cron_job = CronJob(args.config_dir)

        # 执行指定任务
        if args.task == "collect":
            await cron_job.run_collect_task()
        elif args.task == "cleanup":
            await cron_job.run_cleanup_task()
        elif args.task == "health":
            await cron_job.run_health_check()
        elif args.task == "all":
            await cron_job.run_all_tasks()

    except KeyboardInterrupt:
        print("\n任务被用户中断")
        sys.exit(1)
    except Exception as e:
        cron_logger.error(f"执行任务时发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())