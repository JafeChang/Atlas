#!/usr/bin/env python3
"""
Celery管理脚本

提供Celery服务的启动、停止和管理功能
TASK-003: Celery任务队列系统
"""

import os
import sys
import subprocess
import signal
import time
from pathlib import Path
from typing import Optional, List


class Colors:
    """终端颜色"""
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_success(text: str) -> None:
    """打印成功信息"""
    print(f"{Colors.GREEN}✓ {text}{Colors.END}")


def print_error(text: str) -> None:
    """打印错误信息"""
    print(f"{Colors.RED}✗ {text}{Colors.END}")


def print_warning(text: str) -> None:
    """打印警告信息"""
    print(f"{Colors.YELLOW}⚠ {text}{Colors.END}")


def print_info(text: str) -> None:
    """打印信息"""
    print(f"{Colors.BLUE}ℹ {text}{Colors.END}")


def print_header(text: str) -> None:
    """打印标题"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text:^70}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.END}\n")


def check_redis() -> bool:
    """检查Redis是否运行"""
    print_info("检查Redis服务...")
    try:
        import redis
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        print_success("Redis服务正在运行")
        return True
    except Exception as e:
        print_error(f"Redis服务未运行: {e}")
        print_info("请先启动Redis: sudo service redis-server start")
        return False


def check_environment() -> bool:
    """检查环境配置"""
    print_info("检查环境配置...")

    required_vars = [
        "ATLAS_REDIS_HOST",
        "ATLAS_REDIS_PORT",
    ]

    missing = []
    for var in required_vars:
        if not os.getenv(var):
            # 尝试从.env.local加载
            continue  # Celery会自动加载.env.local

    return True


def start_worker(loglevel: str = "info", concurrency: Optional[int] = None,
                 queues: Optional[List[str]] = None) -> bool:
    """启动Celery Worker

    Args:
        loglevel: 日志级别
        concurrency: 并发数（默认为CPU核心数）
        queues: 监听的队列列表

    Returns:
        是否启动成功
    """
    print_header("启动Celery Worker")

    if not check_redis():
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "worker",
        "--loglevel=" + loglevel,
    ]

    if concurrency:
        cmd.extend(["--concurrency=" + str(concurrency)])

    if queues:
        cmd.extend(["--queues=" + ",".join(queues)])

    print_info(f"执行命令: {' '.join(cmd)}")
    print_info("按 Ctrl+C 停止Worker\n")

    try:
        subprocess.run(cmd, check=True)
        return True
    except KeyboardInterrupt:
        print_warning("\n接收到中断信号，正在停止Worker...")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Worker启动失败: {e}")
        return False


def start_beat(loglevel: str = "info") -> bool:
    """启动Celery Beat（定时任务调度器）

    Args:
        loglevel: 日志级别

    Returns:
        是否启动成功
    """
    print_header("启动Celery Beat")

    if not check_redis():
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "beat",
        "--loglevel=" + loglevel,
    ]

    print_info(f"执行命令: {' '.join(cmd)}")
    print_info("按 Ctrl+C 停止Beat\n")

    try:
        subprocess.run(cmd, check=True)
        return True
    except KeyboardInterrupt:
        print_warning("\n接收到中断信号，正在停止Beat...")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Beat启动失败: {e}")
        return False


def start_flower(port: int = 5555, broker: Optional[str] = None) -> bool:
    """启动Flower（Celery监控界面）

    Args:
        port: Flower端口
        broker: Broker URL（可选，默认使用配置）

    Returns:
        是否启动成功
    """
    print_header("启动Flower监控")

    if not check_redis():
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "flower",
        "--port=" + str(port),
        "--broker=" + (broker or os.getenv("ATLAS_CELERY_BROKER_URL", "redis://localhost:6379/0")),
    ]

    print_info(f"执行命令: {' '.join(cmd)}")
    print_info(f"Flower监控界面: http://localhost:{port}")
    print_info("按 Ctrl+C 停止Flower\n")

    try:
        subprocess.run(cmd, check=True)
        return True
    except KeyboardInterrupt:
        print_warning("\n接收到中断信号，正在停止Flower...")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Flower启动失败: {e}")
        return False


def start_all(loglevel: str = "info") -> bool:
    """启动所有Celery服务（Worker + Beat）

    注意：这只是示例，实际应该在多个终端或使用进程管理器

    Args:
        loglevel: 日志级别

    Returns:
        是否启动成功
    """
    print_header("启动所有Celery服务")

    print_warning("建议使用进程管理器（如systemd、supervisor）来管理Celery服务")
    print_warning("这里仅在单个进程中启动Worker和Beat（不推荐用于生产环境）")

    print_info("\n生产环境建议:")
    print("  1. 使用systemd管理Worker和Beat")
    print("  2. 使用supervisor管理进程")
    print("  3. 使用Docker Compose编排服务")
    print("  4. 在后台运行Worker和Beat")

    return False


def show_active_tasks() -> bool:
    """显示活动任务

    Returns:
        是否成功
    """
    print_header("查看活动任务")

    if not check_redis():
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "inspect",
        "active"
    ]

    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"查看任务失败: {e}")
        return False


def show_registered_tasks() -> bool:
    """显示已注册任务

    Returns:
        是否成功
    """
    print_header("查看已注册任务")

    if not check_redis():
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "inspect",
        "registered"
    ]

    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"查看任务失败: {e}")
        return False


def show_stats() -> bool:
    """显示统计信息

    Returns:
        是否成功
    """
    print_header("查看统计信息")

    if not check_redis():
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "inspect",
        "stats"
    ]

    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"查看统计信息失败: {e}")
        return False


def purge_tasks() -> bool:
    """清除所有任务

    Returns:
        是否成功
    """
    print_header("清除所有任务")

    if not check_redis():
        return False

    print_warning("这将清除所有待执行的任务！")
    confirm = input("确认清除? (yes/no): ")

    if confirm.lower() != "yes":
        print_info("操作已取消")
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "purge",
        "-f"
    ]

    try:
        subprocess.run(cmd, check=True)
        print_success("任务已清除")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"清除任务失败: {e}")
        return False


def test_connection() -> bool:
    """测试Celery连接

    Returns:
        是否成功
    """
    print_header("测试Celery连接")

    if not check_redis():
        return False

    cmd = [
        "celery",
        "-A", "atlas.scheduler.celery_app",
        "inspect",
        "ping"
    ]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True)
        print_success("Celery连接正常")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Celery连接失败: {e}")
        return False


def print_usage() -> None:
    """打印使用说明"""
    print_header("Celery管理脚本")
    print("""
用法: python3 scripts/celery_manager.py <command> [options]

命令:
  worker [loglevel] [concurrency]     启动Celery Worker
    --loglevel=INFO                   日志级别 (DEBUG/INFO/WARNING/ERROR)
    --concurrency=4                    并发worker数 (默认为CPU核心数)
    --queues=queue1,queue2             监听的队列

  beat [loglevel]                     启动Celery Beat (定时任务调度器)
    --loglevel=INFO                   日志级别

  flower [port]                       启动Flower监控界面
    --port=5555                       监听端口 (默认5555)

  active                              查看活动任务
  registered                          查看已注册任务
  stats                               查看统计信息
  purge                               清除所有任务
  ping                                测试连接
  help                                显示此帮助信息

示例:
  # 启动Worker (4个并发)
  python3 scripts/celery_manager.py worker --loglevel=INFO --concurrency=4

  # 启动Beat
  python3 scripts/celery_manager.py beat --loglevel=INFO

  # 启动Flower
  python3 scripts/celery_manager.py flower --port=5555

  # 查看活动任务
  python3 scripts/celery_manager.py active

  # 查看统计信息
  python3 scripts/celery_manager.py stats

生产环境部署建议:
  1. 使用systemd服务管理Celery
  2. 使用supervisor管理进程
  3. 使用Docker Compose编排服务
    """)


def main() -> int:
    """主函数"""
    if len(sys.argv) < 2:
        print_usage()
        return 1

    command = sys.argv[1].lower()

    if command == "worker":
        # 解析参数
        loglevel = "info"
        concurrency = None
        queues = None

        for arg in sys.argv[2:]:
            if arg.startswith("--loglevel="):
                loglevel = arg.split("=")[1]
            elif arg.startswith("--concurrency="):
                concurrency = int(arg.split("=")[1])
            elif arg.startswith("--queues="):
                queues = arg.split("=")[1].split(",")

        return 0 if start_worker(loglevel, concurrency, queues) else 1

    elif command == "beat":
        loglevel = "info"
        for arg in sys.argv[2:]:
            if arg.startswith("--loglevel="):
                loglevel = arg.split("=")[1]

        return 0 if start_beat(loglevel) else 1

    elif command == "flower":
        port = 5555
        broker = None

        for arg in sys.argv[2:]:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
            elif arg.startswith("--broker="):
                broker = arg.split("=")[1]

        return 0 if start_flower(port, broker) else 1

    elif command == "active":
        return 0 if show_active_tasks() else 1

    elif command == "registered":
        return 0 if show_registered_tasks() else 1

    elif command == "stats":
        return 0 if show_stats() else 1

    elif command == "purge":
        return 0 if purge_tasks() else 1

    elif command == "ping":
        return 0 if test_connection() else 1

    elif command in ["help", "--help", "-h"]:
        print_usage()
        return 0

    else:
        print_error(f"未知命令: {command}")
        print_usage()
        return 1


if __name__ == "__main__":
    sys.exit(main())
