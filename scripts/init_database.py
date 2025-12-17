#!/usr/bin/env python3
"""
Atlas 数据库初始化脚本

用于初始化数据库、创建表结构和基础数据
"""

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from atlas.core.database import get_database, AtlasDatabase
from atlas.core.storage import get_storage_manager
from atlas.core.config import get_config
from atlas.models.documents import DataSource, SourceType

import argparse
from datetime import datetime
from loguru import logger


def setup_logging(verbose: bool = False):
    """设置日志"""
    level = "DEBUG" if verbose else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=level, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")


def init_database(force_recreate: bool = False):
    """初始化数据库"""
    logger.info("开始初始化数据库...")

    config = get_config()
    db_path = config.data_dir / "atlas.db"

    # 如果强制重建且数据库已存在，则删除
    if force_recreate and db_path.exists():
        logger.warning(f"删除现有数据库: {db_path}")
        db_path.unlink()

    # 获取数据库实例（会自动初始化表结构）
    try:
        database = get_database()

        # 验证表结构 - 只检查是否存在，不查询计数
        tables = ['raw_documents', 'processed_documents', 'data_sources',
                 'collection_tasks', 'system_metrics', 'duplicate_groups']

        for table in tables:
            try:
                info = database.get_table_info(table)
                logger.info(f"表 {table}: 已创建，{len(info)} 个字段")
            except Exception as e:
                logger.warning(f"表 {table} 检查失败: {e}")

        logger.info("数据库初始化完成")
        return True

    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        return False


def init_storage(force_recreate: bool = False):
    """初始化文件存储"""
    logger.info("开始初始化文件存储...")

    config = get_config()

    # 如果强制重建，删除存储目录
    if force_recreate and config.data_dir.exists():
        logger.warning(f"删除现有存储目录: {config.data_dir}")
        import shutil
        shutil.rmtree(config.data_dir)

    try:
        storage = get_storage_manager(config.data_dir)

        logger.info("文件存储初始化完成")
        logger.info(f"存储目录: {storage.base_dir}")
        logger.info(f"压缩功能: {'启用' if storage.enable_compression else '禁用'}")

        return True

    except Exception as e:
        logger.error(f"文件存储初始化失败: {e}")
        return False


import asyncio


def create_sample_data():
    """创建示例数据"""
    logger.info("开始创建示例数据...")

    async def _create_sources():
        try:
            from atlas.core.operations import get_repository
            repository = get_repository()

            # 创建示例数据源
            sample_sources = [
                DataSource(
                    id="rss_example_1",
                    name="示例RSS源1",
                    description="技术新闻RSS源",
                    source_type=SourceType.RSS_FEED,
                    url="https://feeds.example.com/tech-news",
                    config={
                        "update_interval": 3600,
                        "max_items": 50
                    },
                    tags=["technology", "news"],
                    category="technology",
                    language="en"
                ),
                DataSource(
                    id="web_example_1",
                    name="示例网站1",
                    description="新闻网站首页",
                    source_type=SourceType.WEBSITE,
                    url="https://news.example.com",
                    config={
                        "selector": "article",
                        "update_interval": 7200
                    },
                    tags=["news", "general"],
                    category="general",
                    language="en"
                ),
            ]

            for source in sample_sources:
                await repository.create_data_source(source)
                logger.info(f"创建数据源: {source.name} ({source.id})")

            logger.info("示例数据创建完成")
            return True

        except Exception as e:
            logger.error(f"创建示例数据失败: {e}")
            return False

    # 运行异步函数
    return asyncio.run(_create_sources())


def show_status():
    """显示系统状态"""
    logger.info("获取系统状态...")

    async def _get_status():
        try:
            from atlas.core.operations import get_repository
            repository = get_repository()

            # 获取统计信息
            stats = await repository.get_statistics()

            logger.info("=== 系统状态 ===")
            logger.info(f"原始文档: {stats.get('raw_documents', 0)} 条")
            logger.info(f"处理后文档: {stats.get('processed_documents', 0)} 条")
            logger.info(f"数据源总数: {stats.get('total_sources', 0)} 个")
            logger.info(f"活跃数据源: {stats.get('active_sources', 0)} 个")
            logger.info(f"任务总数: {stats.get('total_tasks', 0)} 个")
            logger.info(f"待处理任务: {stats.get('pending_tasks', 0)} 个")

            # 存储状态
            if 'storage' in stats:
                storage = stats['storage']
                logger.info(f"存储目录: {storage.get('base_directory')}")
                if 'total' in storage:
                    logger.info(f"总文件数: {storage['total']['file_count']} 个")
                    logger.info(f"总存储大小: {storage['total']['size_mb']} MB")

            return True

        except Exception as e:
            logger.error(f"获取系统状态失败: {e}")
            return False

    # 运行异步函数
    return asyncio.run(_get_status())


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="Atlas 数据库初始化工具")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    parser.add_argument("--force-recreate", action="store_true", help="强制重建数据库和存储")
    parser.add_argument("--db-only", action="store_true", help="仅初始化数据库")
    parser.add_argument("--storage-only", action="store_true", help="仅初始化文件存储")
    parser.add_argument("--sample-data", action="store_true", help="创建示例数据")
    parser.add_argument("--status", action="store_true", help="显示系统状态")

    args = parser.parse_args()

    setup_logging(args.verbose)

    success = True

    # 初始化数据库
    if not args.storage_only:
        if not init_database(args.force_recreate):
            success = False

    # 初始化文件存储
    if not args.db_only:
        if not init_storage(args.force_recreate):
            success = False

    # 创建示例数据
    if args.sample_data:
        if not create_sample_data():
            success = False

    # 显示状态
    if args.status or (not args.db_only and not args.storage_only):
        if success:
            show_status()

    if success:
        logger.info("✅ Atlas 数据库初始化完成!")
        return 0
    else:
        logger.error("❌ Atlas 数据库初始化失败!")
        return 1


if __name__ == "__main__":
    sys.exit(main())