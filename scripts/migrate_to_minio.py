#!/usr/bin/env python3
"""
Atlas 数据迁移脚本 - 从文件系统迁移到MinIO

将现有的文件系统存储的文档数据迁移到MinIO对象存储。

使用方法:
    # 预演模式（不实际迁移）
    uv run python -m scripts.migrate_to_minio --dry-run

    # 完整迁移
    uv run python -m scripts.migrate_to_minio

    # 详细日志
    uv run python -m scripts.migrate_to_minio --verbose

    # 仅迁移指定数据源
    uv run python -m scripts.migrate_to_minio --source google-ai-blog
"""

import os
import sys
import json
import asyncio
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple
from loguru import logger

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.atlas.core.config import get_config
from src.atlas.core.unified_storage import get_unified_storage


class DataMigration:
    """数据迁移管理器"""

    def __init__(self, dry_run: bool = False, verbose: bool = False):
        """初始化迁移管理器

        Args:
            dry_run: 预演模式，不实际迁移数据
            verbose: 详细日志
        """
        self.dry_run = dry_run
        self.verbose = verbose

        # 统计信息
        self.stats = {
            'total_files': 0,
            'successful': 0,
            'failed': 0,
            'skipped': 0,
            'total_size_bytes': 0,
            'start_time': None,
            'end_time': None
        }

        # 失败记录
        self.failures: List[Tuple[str, str]] = []

        # 配置日志
        self._setup_logging()

    def _setup_logging(self):
        """配置日志"""
        log_level = "DEBUG" if self.verbose else "INFO"
        logger.remove()
        logger.add(sys.stderr, level=log_level)
        logger.add(
            "logs/migration_{time}.log",
            rotation="10 MB",
            level="DEBUG",
            encoding="utf-8"
        )

    def discover_files(self, base_dir: Path, source_filter: str = None) -> List[Path]:
        """发现需要迁移的文件

        Args:
            base_dir: 基础目录
            source_filter: 数据源过滤器

        Returns:
            文件路径列表
        """
        files = []

        if source_filter:
            # 只迁移指定数据源
            source_dir = base_dir / source_filter
            if source_dir.exists():
                files = list(source_dir.glob("*.json"))
                logger.info(f"发现数据源 '{source_filter}' 的 {len(files)} 个文件")
            else:
                logger.warning(f"数据源目录不存在: {source_dir}")
        else:
            # 迁移所有数据源
            for source_dir in base_dir.glob("*/"):
                if source_dir.is_dir():
                    source_files = list(source_dir.glob("*.json"))
                    files.extend(source_files)
                    logger.info(f"发现数据源 '{source_dir.name}' 的 {len(source_files)} 个文件")

        # 按文件路径排序
        files.sort(key=str)

        return files

    def validate_document(self, doc_data: Dict) -> bool:
        """验证文档数据

        Args:
            doc_data: 文档数据

        Returns:
            是否有效
        """
        # 检查必需字段
        required_fields = ['id', 'source_id', 'raw_content']
        for field in required_fields:
            if field not in doc_data:
                logger.warning(f"文档缺少必需字段 '{field}': {doc_data.get('id', 'unknown')}")
                return False

        # 检查内容长度
        content = doc_data.get('raw_content', '')
        if len(content) < 10:
            logger.warning(f"文档内容过短: {doc_data['id']}")
            return False

        return True

    async def migrate_file(self, file_path: Path, storage) -> bool:
        """迁移单个文件

        Args:
            file_path: 文件路径
            storage: 统一存储接口

        Returns:
            是否成功
        """
        try:
            # 读取文件
            with open(file_path, 'r', encoding='utf-8') as f:
                doc_data = json.load(f)

            # 验证文档
            if not self.validate_document(doc_data):
                self.stats['skipped'] += 1
                return False

            # 获取文件大小
            file_size = file_path.stat().st_size
            self.stats['total_size_bytes'] += file_size

            # 预演模式
            if self.dry_run:
                logger.info(f"[DRY-RUN] 将迁移: {file_path.name}")
                self.stats['successful'] += 1
                return True

            # 实际迁移
            await storage.store_raw_document(doc_data)

            if self.verbose:
                logger.debug(f"已迁移: {file_path}")

            self.stats['successful'] += 1
            return True

        except Exception as e:
            error_msg = f"{file_path}: {str(e)}"
            logger.error(f"迁移失败: {error_msg}")
            self.failures.append((str(file_path), str(e)))
            self.stats['failed'] += 1
            return False

    async def migrate(self, source_filter: str = None, batch_size: int = 10):
        """执行迁移

        Args:
            source_filter: 数据源过滤器
            batch_size: 批处理大小
        """
        self.stats['start_time'] = datetime.now()

        logger.info("=" * 60)
        logger.info("Atlas 数据迁移 - 文件系统 -> MinIO")
        logger.info("=" * 60)

        if self.dry_run:
            logger.info("⚠️  预演模式：不会实际迁移数据")
        else:
            logger.warning("⚠️  即将开始数据迁移！")
            logger.warning("⚠️  建议先备份现有数据！")

        # 获取配置
        config = get_config()
        base_dir = Path(config.data_dir) / "raw"

        logger.info(f"数据源目录: {base_dir}")
        logger.info(f"存储类型: {config.storage.type}")

        if config.storage.type == 'filesystem':
            logger.warning("当前配置为文件系统存储，数据将复制到同一文件系统")
        elif config.storage.type == 'minio':
            logger.info(f"MinIO端点: {config.storage.minio_endpoint}")
            logger.info(f"存储桶: {config.storage.minio_bucket_name}")

        # 发现文件
        logger.info("\n正在扫描文件...")
        files = self.discover_files(base_dir, source_filter)
        self.stats['total_files'] = len(files)

        if not files:
            logger.warning("未发现需要迁移的文件")
            return

        logger.info(f"共发现 {len(files)} 个文件")
        logger.info(f"总大小: {sum(f.stat().st_size for f in files) / 1024 / 1024:.2f} MB")

        # 获取存储接口
        storage = get_unified_storage()

        # 执行迁移
        logger.info("\n开始迁移...")
        logger.info("-" * 60)

        for idx, file_path in enumerate(files, 1):
            # 显示进度
            progress = f"[{idx}/{len(files)}] {file_path.relative_to(base_dir)}"
            logger.info(f"处理中: {progress}")

            # 迁移文件
            await self.migrate_file(file_path, storage)

            # 定期显示统计
            if idx % batch_size == 0:
                self._print_progress()

        self.stats['end_time'] = datetime.now()

        # 最终统计
        logger.info("\n" + "=" * 60)
        logger.info("迁移完成！")
        logger.info("=" * 60)
        self._print_final_stats()

        # 显示失败列表
        if self.failures:
            logger.warning(f"\n失败文件 ({len(self.failures)}):")
            for file_path, error in self.failures[:10]:  # 只显示前10个
                logger.warning(f"  - {file_path}")
                logger.warning(f"    错误: {error}")

            if len(self.failures) > 10:
                logger.warning(f"  ... 还有 {len(self.failures) - 10} 个失败文件")

    def _print_progress(self):
        """显示进度统计"""
        logger.info(
            f"进度: 成功={self.stats['successful']}, "
            f"失败={self.stats['failed']}, "
            f"跳过={self.stats['skipped']}"
        )

    def _print_final_stats(self):
        """显示最终统计"""
        elapsed = (self.stats['end_time'] - self.stats['start_time']).total_seconds()

        logger.info(f"总文件数: {self.stats['total_files']}")
        logger.info(f"成功: {self.stats['successful']}")
        logger.info(f"失败: {self.stats['failed']}")
        logger.info(f"跳过: {self.stats['skipped']}")
        logger.info(f"总大小: {self.stats['total_size_bytes'] / 1024 / 1024:.2f} MB")
        logger.info(f"耗时: {elapsed:.2f} 秒")

        if self.stats['total_files'] > 0:
            success_rate = (self.stats['successful'] / self.stats['total_files']) * 100
            logger.info(f"成功率: {success_rate:.1f}%")

            if elapsed > 0:
                speed = self.stats['total_size_bytes'] / elapsed / 1024 / 1024
                logger.info(f"平均速度: {speed:.2f} MB/s")


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Atlas数据迁移工具 - 从文件系统迁移到MinIO"
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='预演模式，不实际迁移数据'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='详细日志'
    )
    parser.add_argument(
        '--source', '-s',
        type=str,
        help='仅迁移指定数据源（例如: google-ai-blog）'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=10,
        help='批处理大小（默认: 10）'
    )

    args = parser.parse_args()

    # 确认迁移
    if not args.dry_run:
        print("\n⚠️  即将开始数据迁移！")
        print("⚠️  建议先备份现有数据！")
        print("\n建议先使用 --dry-run 预演模式测试\n")

        response = input("确定要继续吗？(yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("迁移已取消")
            return

    # 执行迁移
    migration = DataMigration(
        dry_run=args.dry_run,
        verbose=args.verbose
    )

    try:
        await migration.migrate(
            source_filter=args.source,
            batch_size=args.batch_size
        )
    except KeyboardInterrupt:
        logger.warning("\n迁移被用户中断")
    except Exception as e:
        logger.error(f"迁移失败: {e}")
        raise


if __name__ == '__main__':
    # 创建日志目录
    Path("logs").mkdir(exist_ok=True)

    # 运行迁移
    asyncio.run(main())
