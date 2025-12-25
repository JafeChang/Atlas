#!/usr/bin/env python3
"""
Atlas 数据备份脚本

备份文件系统存储的数据到指定目录，支持压缩和增量备份。

使用方法:
    # 完整备份
    python scripts/backup_storage.py

    # 增量备份（仅备份修改过的文件）
    python scripts/backup_storage.py --incremental

    # 指定备份目录
    python scripts/backup_storage.py --output /path/to/backup

    # 启用压缩
    python scripts/backup_storage.py --compress
"""

import os
import sys
import gzip
import shutil
import hashlib
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Set
from loguru import logger

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.atlas.core.config import get_config


class StorageBackup:
    """存储备份管理器"""

    def __init__(self,
                 output_dir: Path = None,
                 incremental: bool = False,
                 compress: bool = False,
                 verbose: bool = False):
        """初始化备份管理器

        Args:
            output_dir: 输出目录
            incremental: 是否增量备份
            compress: 是否压缩
            verbose: 详细日志
        """
        self.output_dir = output_dir or Path("backups")
        self.incremental = incremental
        self.compress = compress
        self.verbose = verbose

        # 统计信息
        self.stats = {
            'total_files': 0,
            'backed_up': 0,
            'skipped': 0,
            'total_size_bytes': 0,
            'compressed_size_bytes': 0,
            'start_time': None,
            'end_time': None
        }

        # 文件哈希记录（用于增量备份）
        self.hash_cache: Dict[str, str] = {}

        # 配置日志
        self._setup_logging()

    def _setup_logging(self):
        """配置日志"""
        log_level = "DEBUG" if self.verbose else "INFO"
        logger.remove()
        logger.add(sys.stderr, level=log_level)

    def _calculate_file_hash(self, file_path: Path) -> str:
        """计算文件哈希值

        Args:
            file_path: 文件路径

        Returns:
            MD5哈希值
        """
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _load_hash_cache(self, cache_file: Path):
        """加载哈希缓存

        Args:
            cache_file: 缓存文件路径
        """
        if cache_file.exists():
            import json
            with open(cache_file, 'r') as f:
                self.hash_cache = json.load(f)
            logger.info(f"加载哈希缓存: {len(self.hash_cache)} 个文件")

    def _save_hash_cache(self, cache_file: Path):
        """保存哈希缓存

        Args:
            cache_file: 缓存文件路径
        """
        import json
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, 'w') as f:
            json.dump(self.hash_cache, f, indent=2)
        logger.info(f"保存哈希缓存: {len(self.hash_cache)} 个文件")

    def discover_files(self, base_dir: Path) -> list:
        """发现需要备份的文件

        Args:
            base_dir: 基础目录

        Returns:
            文件路径列表
        """
        files = []

        # 查找所有JSON文件
        for json_file in base_dir.rglob("*.json"):
            files.append(json_file)

        # 按文件路径排序
        files.sort(key=str)

        return files

    def backup_file(self, file_path: Path, relative_root: Path) -> bool:
        """备份单个文件

        Args:
            file_path: 源文件路径
            relative_root: 相对根目录

        Returns:
            是否成功
        """
        try:
            # 计算相对路径
            relative_path = file_path.relative_to(relative_root)
            dest_path = self.output_dir / relative_path

            # 创建目标目录
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # 增量备份：检查文件是否已修改
            if self.incremental:
                file_key = str(relative_path)
                current_hash = self._calculate_file_hash(file_path)

                if file_key in self.hash_cache:
                    if self.hash_cache[file_key] == current_hash:
                        if self.verbose:
                            logger.debug(f"跳过未修改文件: {relative_path}")
                        self.stats['skipped'] += 1
                        return False

                # 更新哈希缓存
                self.hash_cache[file_key] = current_hash

            # 获取文件大小
            file_size = file_path.stat().st_size
            self.stats['total_size_bytes'] += file_size

            # 复制文件
            if self.compress:
                # 压缩备份
                dest_path = dest_path.with_suffix(dest_path.suffix + '.gz')
                with open(file_path, 'rb') as f_in:
                    with gzip.open(dest_path, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)

                compressed_size = dest_path.stat().st_size
                self.stats['compressed_size_bytes'] += compressed_size

                if self.verbose:
                    ratio = (1 - compressed_size / file_size) * 100
                    logger.debug(
                        f"已压缩: {relative_path} "
                        f"({file_size / 1024:.1f} KB -> {compressed_size / 1024:.1f} KB, {ratio:.1f}% 压缩率)"
                    )
            else:
                # 直接复制
                shutil.copy2(file_path, dest_path)
                if self.verbose:
                    logger.debug(f"已备份: {relative_path}")

            self.stats['backed_up'] += 1
            return True

        except Exception as e:
            logger.error(f"备份失败: {file_path} - {e}")
            return False

    def backup(self):
        """执行备份"""
        self.stats['start_time'] = datetime.now()

        logger.info("=" * 60)
        logger.info("Atlas 数据备份")
        logger.info("=" * 60)

        # 获取配置
        config = get_config()
        source_dir = Path(config.data_dir) / "raw"

        logger.info(f"源目录: {source_dir}")
        logger.info(f"目标目录: {self.output_dir}")
        logger.info(f"增量模式: {'是' if self.incremental else '否'}")
        logger.info(f"压缩: {'是' if self.compress else '否'}")

        # 创建备份目录
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"atlas_backup_{timestamp}"
        self.output_dir = self.output_dir / backup_name
        self.output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"备份目录: {self.output_dir}")

        # 加载哈希缓存（增量模式）
        if self.incremental:
            cache_file = Path("backups") / "hash_cache.json"
            self._load_hash_cache(cache_file)

        # 发现文件
        logger.info("\n正在扫描文件...")
        files = self.discover_files(source_dir)
        self.stats['total_files'] = len(files)

        if not files:
            logger.warning("未发现需要备份的文件")
            return

        total_size = sum(f.stat().st_size for f in files)
        logger.info(f"共发现 {len(files)} 个文件")
        logger.info(f"总大小: {total_size / 1024 / 1024:.2f} MB")

        # 执行备份
        logger.info("\n开始备份...")
        logger.info("-" * 60)

        for idx, file_path in enumerate(files, 1):
            # 显示进度
            progress = f"[{idx}/{len(files)}] {file_path.relative_to(source_dir)}"
            logger.info(f"处理中: {progress}")

            # 备份文件
            self.backup_file(file_path, source_dir)

            # 定期显示统计
            if idx % 50 == 0:
                self._print_progress()

        self.stats['end_time'] = datetime.now()

        # 保存哈希缓存（增量模式）
        if self.incremental:
            cache_file = self.output_dir / "hash_cache.json"
            self._save_hash_cache(cache_file)

        # 创建备份清单
        self._create_manifest(source_dir)

        # 最终统计
        logger.info("\n" + "=" * 60)
        logger.info("备份完成！")
        logger.info("=" * 60)
        self._print_final_stats()

        logger.info(f"\n备份位置: {self.output_dir}")

    def _print_progress(self):
        """显示进度统计"""
        logger.info(
            f"进度: 已备份={self.stats['backed_up']}, "
            f"跳过={self.stats['skipped']}"
        )

    def _print_final_stats(self):
        """显示最终统计"""
        elapsed = (self.stats['end_time'] - self.stats['start_time']).total_seconds()

        logger.info(f"总文件数: {self.stats['total_files']}")
        logger.info(f"已备份: {self.stats['backed_up']}")
        logger.info(f"跳过: {self.stats['skipped']}")

        original_size = self.stats['total_size_bytes'] / 1024 / 1024
        logger.info(f"原始大小: {original_size:.2f} MB")

        if self.compress:
            compressed_size = self.stats['compressed_size_bytes'] / 1024 / 1024
            logger.info(f"压缩后大小: {compressed_size:.2f} MB")
            compression_ratio = (1 - compressed_size / original_size) * 100
            logger.info(f"压缩率: {compression_ratio:.1f}%")

        logger.info(f"耗时: {elapsed:.2f} 秒")

        if elapsed > 0:
            speed = self.stats['total_size_bytes'] / elapsed / 1024 / 1024
            logger.info(f"平均速度: {speed:.2f} MB/s")

    def _create_manifest(self, source_dir: Path):
        """创建备份清单

        Args:
            source_dir: 源目录
        """
        manifest = {
            'backup_time': self.stats['start_time'].isoformat(),
            'backup_type': 'incremental' if self.incremental else 'full',
            'compressed': self.compress,
            'source_directory': str(source_dir),
            'backup_directory': str(self.output_dir),
            'statistics': self.stats.copy(),
            'files': []
        }

        # 添加文件列表
        for file_path in source_dir.rglob("*.json"):
            relative_path = str(file_path.relative_to(source_dir))
            file_info = {
                'path': relative_path,
                'size': file_path.stat().st_size,
                'modified': datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
            }
            manifest['files'].append(file_info)

        # 保存清单
        manifest_file = self.output_dir / "manifest.json"
        import json
        with open(manifest_file, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        logger.info(f"备份清单: {manifest_file}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Atlas数据备份工具"
    )
    parser.add_argument(
        '--output', '-o',
        type=Path,
        default=Path("backups"),
        help='备份输出目录（默认: backups）'
    )
    parser.add_argument(
        '--incremental', '-i',
        action='store_true',
        help='增量备份（仅备份修改过的文件）'
    )
    parser.add_argument(
        '--compress', '-c',
        action='store_true',
        help='使用gzip压缩备份文件'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='详细日志'
    )

    args = parser.parse_args()

    # 执行备份
    backup = StorageBackup(
        output_dir=args.output,
        incremental=args.incremental,
        compress=args.compress,
        verbose=args.verbose
    )

    try:
        backup.backup()
    except KeyboardInterrupt:
        logger.warning("\n备份被用户中断")
    except Exception as e:
        logger.error(f"备份失败: {e}")
        raise


if __name__ == '__main__':
    main()
