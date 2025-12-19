"""
Atlas 命令行接口

提供系统的主要命令行工具，包括数据采集、系统管理、配置等功能。
"""

import sys
import os
import click
from pathlib import Path
from typing import Optional, TextIO
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from . import __version__
from .core.config import get_config
from .core.logging import init_logger, get_logger

# Rich console for better output formatting
console = Console()


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

    try:
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

        ctx.obj['console'] = console

    except Exception as e:
        console.print(f"[red]❌ 启动失败: {e}[/red]")
        sys.exit(1)


@main.command()
@click.pass_context
def check(ctx: click.Context) -> None:
    """检查系统状态和配置"""
    console = ctx.obj['console']
    config = ctx.obj['config']
    logger = ctx.obj['logger']

    console.print("[bold blue]🔍 检查 Atlas 系统状态...[/bold blue]")

    # 创建状态表格
    table = Table(title="系统状态检查结果")
    table.add_column("状态", style="bold", no_wrap=True)
    table.add_column("检查项", style="cyan")
    table.add_column("详情", style="dim")

    checks = []

    # 检查配置
    try:
        config.ensure_directories()
        checks.append(("✅", "配置目录", str(config.config_dir_path)))
        table.add_row("[green]✅[/green]", "配置目录", str(config.config_dir_path))
    except Exception as e:
        checks.append(("❌", "配置目录", str(e)))
        table.add_row("[red]❌[/red]", "配置目录", str(e))

    # 检查数据目录
    if config.data_dir.exists():
        checks.append(("✅", "数据目录", str(config.data_dir)))
        table.add_row("[green]✅[/green]", "数据目录", str(config.data_dir))
    else:
        checks.append(("❌", "数据目录", f"目录不存在: {config.data_dir}"))
        table.add_row("[red]❌[/red]", "数据目录", f"目录不存在: {config.data_dir}")

    # 检查日志目录
    if config.log_dir.exists():
        checks.append(("✅", "日志目录", str(config.log_dir)))
        table.add_row("[green]✅[/green]", "日志目录", str(config.log_dir))
    else:
        checks.append(("❌", "日志目录", f"目录不存在: {config.log_dir}"))
        table.add_row("[red]❌[/red]", "日志目录", f"目录不存在: {config.log_dir}")

    # 检查配置文件
    config_file = config.config_dir_path / "config.yaml"
    if config_file.exists():
        checks.append(("✅", "主配置文件", str(config_file)))
        table.add_row("[green]✅[/green]", "主配置文件", str(config_file))
    else:
        checks.append(("⚠️", "主配置文件", f"文件不存在: {config_file}"))
        table.add_row("[yellow]⚠️[/yellow]", "主配置文件", f"文件不存在: {config_file}")

    sources_file = config.config_dir_path / "sources.yaml"
    if sources_file.exists():
        checks.append(("✅", "数据源配置", str(sources_file)))
        table.add_row("[green]✅[/green]", "数据源配置", str(sources_file))
    else:
        checks.append(("⚠️", "数据源配置", f"文件不存在: {sources_file}"))
        table.add_row("[yellow]⚠️[/yellow]", "数据源配置", f"文件不存在: {sources_file}")

    # 检查Python版本
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 13):
        checks.append(("✅", "Python版本", python_version))
        table.add_row("[green]✅[/green]", "Python版本", python_version)
    else:
        checks.append(("⚠️", "Python版本", f"{python_version} (推荐3.13+)"))
        table.add_row("[yellow]⚠️[/yellow]", "Python版本", f"{python_version} (推荐3.13+)")

    # 输出表格
    console.print(table)

    # 统计
    success_count = sum(1 for status, _, _ in checks if "✅" in status)
    warning_count = sum(1 for status, _, _ in checks if "⚠️" in status)
    error_count = sum(1 for status, _, _ in checks if "❌" in status)

    # 创建统计面板
    stats_text = f"""[bold green]✅ 成功: {success_count}[/bold green]
[yellow]⚠️ 警告: {warning_count}[/yellow]
[bold red]❌ 错误: {error_count}[/bold red]"""

    panel = Panel(stats_text, title="📈 检查统计", border_style="blue")
    console.print(panel)

    if error_count > 0:
        logger.error("系统检查失败", error_count=error_count)
        console.print("[red]❌ 系统检查失败，请修复错误后重试[/red]")
        sys.exit(1)
    else:
        logger.info("系统检查完成", success=success_count, warnings=warning_count)
        console.print("[green]✅ 系统检查完成[/green]")


@main.command()
@click.option('--force', '-f', is_flag=True, help='强制重新初始化')
@click.pass_context
def init(ctx: click.Context, force: bool) -> None:
    """初始化系统（创建目录、数据库等）"""
    console = ctx.obj['console']
    config = ctx.obj['config']
    logger = ctx.obj['logger']

    console.print("[bold blue]🚀 初始化 Atlas 系统...[/bold blue]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:

        try:
            # 创建必要目录
            task = progress.add_task("创建目录结构...", total=None)
            config.ensure_directories()
            progress.update(task, description="✅ 目录结构创建完成")

            # 数据库初始化
            progress.update(task, description="初始化数据库...")
            try:
                from .core.database import init_database
                init_database(config.database.url)
                progress.update(task, description="✅ 数据库初始化完成")
            except ImportError:
                progress.update(task, description="⚠️ 数据库模块未实现")
            except Exception as e:
                progress.update(task, description=f"❌ 数据库初始化失败: {e}")
                if not force:
                    raise

            # 检查配置文件
            progress.update(task, description="检查配置文件...")
            config_files = ["config.yaml", "sources.yaml"]
            missing_files = []

            for filename in config_files:
                config_file = config.config_dir_path / filename
                if config_file.exists():
                    progress.update(task, description=f"✅ {filename} 存在")
                else:
                    missing_files.append(filename)
                    progress.update(task, description=f"⚠️ {filename} 不存在")

            # 初始化完成
            progress.update(task, description="🎉 系统初始化完成")

            console.print("\n[bold green]🎉 Atlas 系统初始化完成！[/bold green]")

            # 显示创建的目录
            directories_panel = Panel(
                f"📁 数据目录: {config.data_dir}\n"
                f"📝 日志目录: {config.log_dir}\n"
                f"⚙️  配置目录: {config.config_dir_path}",
                title="创建的目录",
                border_style="green"
            )
            console.print(directories_panel)

            if missing_files:
                console.print(f"[yellow]⚠️  缺少配置文件: {', '.join(missing_files)}[/yellow]")

            logger.info("系统初始化完成", data_dir=str(config.data_dir), force=force)

        except Exception as e:
            console.print(f"[red]❌ 初始化失败: {e}[/red]")
            logger.exception("系统初始化失败", error=str(e))
            if not force:
                sys.exit(1)
            else:
                console.print("[yellow]⚠️ 强制初始化，继续执行...[/yellow]")


@main.command()
@click.option('--detailed', '-d', is_flag=True, help='显示详细信息')
@click.pass_context
def status(ctx: click.Context, detailed: bool) -> None:
    """显示系统状态信息"""
    console = ctx.obj['console']
    config = ctx.obj['config']
    logger = ctx.obj['logger']

    # 基本信息表格
    basic_table = Table(title="📋 Atlas 系统状态", show_header=False, box=None)
    basic_table.add_column("项目", style="cyan")
    basic_table.add_column("值", style="white")

    basic_table.add_row("📦 版本", __version__)
    basic_table.add_row("🌍 环境", config.env)
    basic_table.add_row("🔧 调试模式", "✅ 开启" if config.debug else "❌ 关闭")
    basic_table.add_row("📊 日志级别", config.log_level)
    basic_table.add_row("💾 数据目录", str(config.data_dir))
    basic_table.add_row("📁 配置目录", str(config.config_dir_path))
    basic_table.add_row("📝 日志目录", str(config.log_dir))

    console.print(basic_table)

    if detailed:
        # 环境信息表格
        env_info = config.get_env_info()
        env_table = Table(title="🔧 环境配置")
        env_table.add_column("配置项", style="cyan")
        env_table.add_column("值", style="white")

        env_table.add_row("当前环境", env_info['current_env'])
        env_table.add_row("加载的配置文件", "\n".join(f"• {file}" for file in env_info['loaded_files']))

        console.print(env_table)

        # 数据库信息表格
        db_table = Table(title="🗄️ 数据库配置")
        db_table.add_column("配置项", style="cyan")
        db_table.add_column("值", style="white")

        db_info = config.database.url.split(':')
        db_table.add_row("类型", db_info[0].upper())
        db_table.add_row("连接字符串", config.database.url)
        db_table.add_row("连接池大小", str(config.database.pool_size))

        console.print(db_table)

        # LLM 配置表格
        llm_table = Table(title="🤖 LLM 配置")
        llm_table.add_column("配置项", style="cyan")
        llm_table.add_column("值", style="white")

        llm_table.add_row("提供商", config.llm.provider)
        llm_table.add_row("模型", config.llm.model)
        if hasattr(config.llm, 'base_url') and config.llm.base_url:
            llm_table.add_row("服务地址", config.llm.base_url)

        console.print(llm_table)

    # 数据源统计
    sources = config.sources.get("sources", [])
    enabled_sources = [s for s in sources if s.get("enabled", True)]

    sources_table = Table(title="📡 数据源统计")
    sources_table.add_column("统计项", style="cyan")
    sources_table.add_column("数量", style="white", justify="right")

    sources_table.add_row("总数", str(len(sources)))
    sources_table.add_row("已启用", f"[green]{len(enabled_sources)}[/green]")
    sources_table.add_row("已禁用", f"[red]{len(sources) - len(enabled_sources)}[/red]")

    console.print(sources_table)

    logger.info("查看系统状态", sources_count=len(sources), detailed=detailed)


@main.command()
@click.option('--lines', '-n', default=20, help='显示的行数')
@click.option('--follow', '-f', is_flag=True, help='实时跟踪日志')
@click.option('--level', '-l',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']),
              help='过滤日志级别')
@click.pass_context
def logs(ctx: click.Context, lines: int, follow: bool, level: Optional[str]) -> None:
    """查看日志文件"""
    console = ctx.obj['console']
    config = ctx.obj['config']

    log_file = config.log_dir / "atlas.log"
    error_file = config.log_dir / "errors.log"

    # 显示日志文件信息
    files_table = Table(title="📝 日志文件信息", show_header=True)
    files_table.add_column("文件类型", style="cyan")
    files_table.add_column("路径", style="dim")
    files_table.add_column("大小", style="white", justify="right")
    files_table.add_column("存在", justify="center")

    for file_type, file_path in [("应用日志", log_file), ("错误日志", error_file)]:
        exists = "✅" if file_path.exists() else "❌"
        size = f"{file_path.stat().st_size} bytes" if file_path.exists() else "N/A"
        files_table.add_row(file_type, str(file_path), size, exists)

    console.print(files_table)

    if log_file.exists():
        console.print(f"\n📊 [bold]应用日志 (最近 {lines} 行):[/bold]")

        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_lines = f.readlines()

                # 过滤日志级别
                if level:
                    log_lines = [line for line in log_lines if level in line]

                # 显示最后N行
                for line in log_lines[-lines:]:
                    # 根据日志级别着色
                    line_lower = line.lower()
                    if 'error' in line_lower or 'critical' in line_lower:
                        console.print(f"[red]{line.strip()}[/red]")
                    elif 'warning' in line_lower:
                        console.print(f"[yellow]{line.strip()}[/yellow]")
                    elif 'debug' in line_lower:
                        console.print(f"[dim]{line.strip()}[/dim]")
                    else:
                        console.print(line.strip())

        except Exception as e:
            console.print(f"[red]❌ 读取日志文件失败: {e}[/red]")
    else:
        console.print("[yellow]⚠️ 应用日志文件不存在[/yellow]")

    if follow:
        console.print("\n[dim]🔄 实时跟踪功能需要额外实现...[/dim]")


@main.group()
def config() -> None:
    """配置管理命令组"""
    pass


@config.command('show')
@click.option('--format', 'output_format',
              type=click.Choice(['yaml', 'json']),
              default='yaml',
              help='输出格式')
@click.pass_context
def config_show(ctx: click.Context, output_format: str) -> None:
    """显示当前配置"""
    config = ctx.obj['config']

    if output_format == 'json':
        import json
        config_dict = {
            "env": config.env,
            "debug": config.debug,
            "log_level": config.log_level,
            "data_dir": str(config.data_dir),
            "log_dir": str(config.log_dir),
        }
        console.print(json.dumps(config_dict, indent=2, ensure_ascii=False))
    else:
        import yaml
        config_dict = {
            "env": config.env,
            "debug": config.debug,
            "log_level": config.log_level,
            "data_dir": str(config.data_dir),
            "log_dir": str(config.log_dir),
        }
        console.print(yaml.dump(config_dict, default_flow_style=False, allow_unicode=True))


@config.command('validate')
@click.pass_context
def config_validate(ctx: click.Context) -> None:
    """验证配置文件"""
    console = ctx.obj['console']
    config = ctx.obj['config']

    console.print("[blue]🔍 验证配置文件...[/blue]")

    validation_results = []

    # 验证主配置文件
    main_config = config.config_dir_path / "config.yaml"
    if main_config.exists():
        try:
            with open(main_config, 'r', encoding='utf-8') as f:
                import yaml
                yaml.safe_load(f)
            validation_results.append(("✅", "主配置文件", "语法正确"))
        except Exception as e:
            validation_results.append(("❌", "主配置文件", f"语法错误: {e}"))
    else:
        validation_results.append(("❌", "主配置文件", "文件不存在"))

    # 验证数据源配置文件
    sources_config = config.config_dir_path / "sources.yaml"
    if sources_config.exists():
        try:
            with open(sources_config, 'r', encoding='utf-8') as f:
                import yaml
                sources_data = yaml.safe_load(f)
                sources_count = len(sources_data.get("sources", []))
            validation_results.append(("✅", "数据源配置", f"包含 {sources_count} 个数据源"))
        except Exception as e:
            validation_results.append(("❌", "数据源配置", f"语法错误: {e}"))
    else:
        validation_results.append(("❌", "数据源配置", "文件不存在"))

    # 显示验证结果
    table = Table(title="配置验证结果")
    table.add_column("状态", style="bold")
    table.add_column("文件", style="cyan")
    table.add_column("结果", style="white")

    for status, file_name, result in validation_results:
        table.add_row(status, file_name, result)

    console.print(table)


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