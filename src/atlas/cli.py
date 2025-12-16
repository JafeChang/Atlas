"""
Atlas 命令行接口

提供系统的主要命令行工具，包括数据采集、系统管理、配置等功能。
"""

import sys
import click
from pathlib import Path
from typing import Optional

from . import __version__
from .core.config import get_config
from .core.logging import init_logger, get_logger


@click.group()
@click.version_option(version=__version__, prog_name="Atlas")
@click.option('--config-dir', '-c',
              type=click.Path(exists=True, path_type=Path),
              help='配置文件目录路径')
@click.option('--log-level', '-l',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']),
              default='INFO',
              help='日志级别')
@click.pass_context
def main(ctx: click.Context, config_dir: Optional[Path], log_level: str) -> None:
    """Atlas 个人信息聚合系统命令行工具"""
    # 确保上下文对象存在
    ctx.ensure_object(dict)

    # 加载配置
    config = get_config(config_dir)
    ctx.obj['config'] = config

    # 初始化日志
    logger = init_logger(
        log_dir=config.log_dir,
        log_level=log_level or config.log_level
    )
    ctx.obj['logger'] = logger

    logger.info(f"Atlas v{__version__} 启动",
                env=config.env,
                config_dir=str(config.config_dir_path))


@main.command()
@click.pass_context
def check(ctx: click.Context) -> None:
    """检查系统状态和配置"""
    config = ctx.obj['config']
    logger = ctx.obj['logger']

    click.echo("🔍 检查 Atlas 系统状态...")

    checks = []

    # 检查配置
    try:
        config.ensure_directories()
        checks.append(("✅", "配置目录", str(config.config_dir_path)))
    except Exception as e:
        checks.append(("❌", "配置目录", str(e)))

    # 检查数据目录
    if config.data_dir.exists():
        checks.append(("✅", "数据目录", str(config.data_dir)))
    else:
        checks.append(("❌", "数据目录", f"目录不存在: {config.data_dir}"))

    # 检查日志目录
    if config.log_dir.exists():
        checks.append(("✅", "日志目录", str(config.log_dir)))
    else:
        checks.append(("❌", "日志目录", f"目录不存在: {config.log_dir}"))

    # 检查配置文件
    config_file = config.config_dir_path / "config.yaml"
    if config_file.exists():
        checks.append(("✅", "主配置文件", str(config_file)))
    else:
        checks.append(("⚠️", "主配置文件", f"文件不存在: {config_file}"))

    sources_file = config.config_dir_path / "sources.yaml"
    if sources_file.exists():
        checks.append(("✅", "数据源配置", str(sources_file)))
    else:
        checks.append(("⚠️", "数据源配置", f"文件不存在: {sources_file}"))

    # 输出检查结果
    click.echo("\n📊 系统状态检查结果:")
    for status, item, detail in checks:
        click.echo(f"  {status} {item}: {detail}")

    # 统计
    success_count = sum(1 for status, _, _ in checks if "✅" in status)
    warning_count = sum(1 for status, _, _ in checks if "⚠️" in status)
    error_count = sum(1 for status, _, _ in checks if "❌" in status)

    click.echo(f"\n📈 检查统计:")
    click.echo(f"  ✅ 成功: {success_count}")
    click.echo(f"  ⚠️ 警告: {warning_count}")
    click.echo(f"  ❌ 错误: {error_count}")

    if error_count > 0:
        logger.error("系统检查失败", error_count=error_count)
        sys.exit(1)
    else:
        logger.info("系统检查完成", success=success_count, warnings=warning_count)


@main.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """初始化系统（创建目录、数据库等）"""
    config = ctx.obj['config']
    logger = ctx.obj['logger']

    click.echo("🚀 初始化 Atlas 系统...")

    try:
        # 创建必要目录
        config.ensure_directories()
        click.echo("✅ 创建目录结构")

        # 这里后续会添加数据库初始化
        # from .core.database import init_database
        # init_database(config.database.url)
        # click.echo("✅ 初始化数据库")

        # 创建默认配置文件
        if not (config.config_dir_path / "config.yaml").exists():
            click.echo("⚠️  配置文件不存在，请检查配置目录")

        if not (config.config_dir_path / "sources.yaml").exists():
            click.echo("⚠️  数据源配置不存在，请检查配置目录")

        click.echo("\n🎉 Atlas 系统初始化完成！")
        logger.info("系统初始化完成", data_dir=str(config.data_dir))

    except Exception as e:
        click.echo(f"❌ 初始化失败: {e}")
        logger.exception("系统初始化失败", error=str(e))
        sys.exit(1)


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """显示系统状态信息"""
    config = ctx.obj['config']
    logger = ctx.obj['logger']

    click.echo("📋 Atlas 系统状态")
    click.echo("=" * 50)
    click.echo(f"📦 版本: {__version__}")
    click.echo(f"🌍 环境: {config.env}")
    click.echo(f"🔧 调试模式: {'开启' if config.debug else '关闭'}")
    click.echo(f"📊 日志级别: {config.log_level}")
    click.echo(f"💾 数据目录: {config.data_dir}")
    click.echo(f"📁 配置目录: {config.config_dir_path}")
    click.echo(f"📝 日志目录: {config.log_dir}")

    # 环境变量加载信息
    env_info = config.get_env_info()
    click.echo(f"\n🔧 环境配置:")
    click.echo(f"  当前环境: {env_info['current_env']}")
    click.echo(f"  加载的配置文件:")
    for loaded_file in env_info['loaded_files']:
        click.echo(f"    - {loaded_file}")

    # 数据库信息
    click.echo(f"\n🗄️  数据库:")
    click.echo(f"  类型: {config.database.url.split(':')[0].upper()}")
    click.echo(f"  连接池大小: {config.database.pool_size}")

    # LLM 配置
    click.echo(f"\n🤖 LLM 配置:")
    click.echo(f"  提供商: {config.llm.provider}")
    click.echo(f"  模型: {config.llm.model}")
    if config.llm.provider == "local":
        click.echo(f"  服务地址: {config.llm.base_url}")

    # 数据源统计
    sources = config.sources.get("sources", [])
    enabled_sources = [s for s in sources if s.get("enabled", True)]
    click.echo(f"\n📡 数据源:")
    click.echo(f"  总数: {len(sources)}")
    click.echo(f"  已启用: {len(enabled_sources)}")
    click.echo(f"  已禁用: {len(sources) - len(enabled_sources)}")

    logger.info("查看系统状态", sources_count=len(sources))


@main.command()
@click.option('--source', '-s', help='指定数据源名称')
@click.pass_context
def collect(ctx: click.Context, source: Optional[str]) -> None:
    """执行数据采集任务"""
    config = ctx.obj['config']
    logger = ctx.obj['logger']

    click.echo("🔄 执行数据采集任务...")

    # 这里后续会实现具体的数据采集逻辑
    click.echo(f"📡 数据源: {source or '所有启用的数据源'}")
    click.echo("⚠️  数据采集功能正在开发中...")

    logger.info("数据采集任务触发", source=source)


@main.command()
@click.pass_context
def logs(ctx: click.Context) -> None:
    """查看日志文件"""
    config = ctx.obj['config']

    log_file = config.log_dir / "atlas.log"
    error_file = config.log_dir / "errors.log"

    click.echo(f"📝 日志文件位置:")
    click.echo(f"  应用日志: {log_file}")
    click.echo(f"  错误日志: {error_file}")

    if log_file.exists():
        click.echo(f"\n📊 应用日志 (最近 10 行):")
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines[-10:]:
                click.echo(f"  {line.strip()}")
    else:
        click.echo("  应用日志文件不存在")


if __name__ == '__main__':
    main()