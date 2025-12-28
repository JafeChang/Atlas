#!/usr/bin/env python3
"""
Redis环境检查脚本

检查Redis服务状态和Python依赖，为Celery集成做准备。
TASK-003-1: Redis配置和部署
"""

import subprocess
import sys
from pathlib import Path


class Colors:
    """终端颜色"""
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_header(text: str) -> None:
    """打印标题"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text:^70}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.END}\n")


def print_success(text: str) -> None:
    """打印成功信息"""
    print(f"{Colors.GREEN}✓ {text}{Colors.END}")


def print_warning(text: str) -> None:
    """打印警告信息"""
    print(f"{Colors.YELLOW}⚠ {text}{Colors.END}")


def print_error(text: str) -> None:
    """打印错误信息"""
    print(f"{Colors.RED}✗ {text}{Colors.END}")


def print_info(text: str) -> None:
    """打印信息"""
    print(f"{Colors.BLUE}ℹ {text}{Colors.END}")


def check_redis_cli() -> bool:
    """检查Redis CLI是否安装"""
    print_info("检查Redis CLI...")
    try:
        result = subprocess.run(
            ["redis-cli", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            print_success(f"Redis CLI已安装: {version}")
            return True
    except FileNotFoundError:
        print_error("Redis CLI未找到")
    except subprocess.TimeoutExpired:
        print_error("Redis CLI命令超时")
    except Exception as e:
        print_error(f"检查Redis CLI时出错: {e}")

    print_info("请安装Redis: sudo apt-get install redis-server")
    return False


def check_redis_server() -> bool:
    """检查Redis服务器是否运行"""
    print_info("检查Redis服务器状态...")

    # 方式1: 使用service命令
    try:
        result = subprocess.run(
            ["service", "redis-server", "status"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if "active (running)" in result.stdout or "is running" in result.stdout:
            print_success("Redis服务器正在运行")
            return True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        pass

    # 方式2: 使用redis-cli ping
    try:
        result = subprocess.run(
            ["redis-cli", "ping"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and "PONG" in result.stdout:
            print_success("Redis服务器正在运行 (PONG)")
            return True
    except FileNotFoundError:
        print_error("Redis CLI未找到")
        return False
    except subprocess.TimeoutExpired:
        print_error("Redis连接超时")
        return False
    except Exception as e:
        print_error(f"检查Redis服务器时出错: {e}")
        return False

    print_error("Redis服务器未运行")
    print_info("请启动Redis: sudo service redis-server start")
    return False


def check_redis_info() -> dict:
    """获取Redis服务器信息"""
    print_info("获取Redis服务器信息...")

    try:
        result = subprocess.run(
            ["redis-cli", "info", "server"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            info = {}
            for line in result.stdout.strip().split('\n'):
                if ':' in line and not line.startswith('#'):
                    key, value = line.split(':', 1)
                    info[key.strip()] = value.strip()

            # 显示关键信息
            if 'redis_version' in info:
                print_success(f"Redis版本: {info['redis_version']}")
            if 'os' in info:
                print_info(f"操作系统: {info['os']}")

            return info
    except Exception as e:
        print_error(f"获取Redis信息时出错: {e}")

    return {}


def check_redis_memory() -> bool:
    """检查Redis内存使用情况"""
    print_info("检查Redis内存使用...")

    try:
        result = subprocess.run(
            ["redis-cli", "info", "memory"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            info = {}
            for line in result.stdout.strip().split('\n'):
                if ':' in line and not line.startswith('#'):
                    key, value = line.split(':', 1)
                    info[key.strip()] = value.strip()

            if 'used_memory_human' in info:
                print_success(f"已用内存: {info['used_memory_human']}")
            if 'maxmemory_human' in info:
                max_mem = info['maxmemory_human']
                if max_mem == '0B':
                    print_warning("未设置最大内存限制 (maxmemory)")
                else:
                    print_info(f"最大内存: {max_mem}")

            return True
    except Exception as e:
        print_error(f"检查Redis内存时出错: {e}")

    return False


def check_redis_persistence() -> bool:
    """检查Redis持久化配置"""
    print_info("检查Redis持久化配置...")

    try:
        result = subprocess.run(
            ["redis-cli", "config", "get", "save"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                save_config = lines[1]
                if save_config and save_config != '""':
                    print_success(f"RDB持久化: {save_config}")
                else:
                    print_warning("RDB持久化未启用")

        # 检查AOF
        result = subprocess.run(
            ["redis-cli", "config", "get", "appendonly"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                aof_enabled = lines[1]
                if aof_enabled == 'yes':
                    print_success("AOF持久化: 已启用")
                else:
                    print_info("AOF持久化: 未启用")

        return True
    except Exception as e:
        print_error(f"检查Redis持久化时出错: {e}")

    return False


def check_python_redis() -> bool:
    """检查Python Redis库"""
    print_info("检查Python Redis库...")

    try:
        import redis
        print_success(f"Python Redis库已安装: redis.__version__")

        # 测试连接
        try:
            r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            r.ping()
            print_success("Python Redis连接测试成功")
        except Exception as e:
            print_warning(f"Python Redis连接测试失败: {e}")
            print_info("请确保Redis服务器正在运行")

        return True
    except ImportError:
        print_error("Python Redis库未安装")
        print_info("请安装: uv add redis")
        return False


def check_celery() -> bool:
    """检查Celery库"""
    print_info("检查Celery库...")

    try:
        import celery
        print_success(f"Celery库已安装: {celery.__version__}")
        return True
    except ImportError:
        print_error("Celery库未安装")
        print_info("请安装: uv add celery")
        return False


def check_docker_redis() -> bool:
    """检查Docker中的Redis"""
    print_info("检查Docker Redis容器...")

    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=atlas-redis", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and "atlas-redis" in result.stdout:
            print_success("Docker Redis容器正在运行")
            return True
    except FileNotFoundError:
        print_info("Docker未安装或不在PATH中")
    except subprocess.TimeoutExpired:
        print_error("Docker命令超时")
    except Exception as e:
        print_warning(f"检查Docker Redis时出错: {e}")

    return False


def print_redis_docker_example() -> None:
    """打印Docker Redis启动示例"""
    print_info("\nDocker Redis启动示例:")
    print("""
# 从.env.local读取配置（推荐）
source .env.local 2>/dev/null || true
docker run -d \\
  --name atlas-redis \\
  -p 6379:6379 \\
  -v redis-data:/data \\
  -e REDIS_PASSWORD=${ATLAS_REDIS_PASSWORD:-} \\
  redis:7-alpine

# 验证运行
docker ps | grep atlas-redis
docker exec -it atlas-redis redis-cli ping

# 查看日志
docker logs -f atlas-redis

# 停止容器
docker stop atlas-redis

# 启动容器
docker start atlas-redis

# 删除容器
docker rm -f atlas-redis
    """)


def main() -> int:
    """主函数"""
    print_header("Atlas Redis环境检查")

    checks = [
        ("Redis CLI", check_redis_cli),
        ("Redis服务器", check_redis_server),
        ("Redis信息", check_redis_info),
        ("Redis内存", check_redis_memory),
        ("Redis持久化", check_redis_persistence),
        ("Python Redis", check_python_redis),
        ("Celery", check_celery),
        ("Docker Redis", check_docker_redis),
    ]

    results = []
    for name, check_func in checks:
        try:
            result = check_func()
            results.append((name, result))
        except Exception as e:
            print_error(f"{name}检查时出错: {e}")
            results.append((name, False))

    # 打印总结
    print_header("检查总结")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        if result:
            print_success(f"{name}: 通过")
        else:
            print_warning(f"{name}: 未通过")

    print(f"\n通过率: {passed}/{total} ({passed * 100 // total}%)")

    if passed >= total * 0.75:  # 75%通过
        print_success("\nRedis环境基本就绪，可以开始Celery集成")
        return 0
    else:
        print_error("\nRedis环境未就绪，请完成上述检查项")
        print_info("\n建议操作:")
        print("1. 安装Redis: sudo apt-get install redis-server")
        print("2. 启动服务: sudo service redis-server start")
        print("3. 验证连接: redis-cli ping")
        print("4. 安装Python库: uv sync")
        print("\n详细指南: docs/guidelines/redis-setup-guide.md")

        # 显示Docker选项
        print_redis_docker_example()
        return 1


if __name__ == "__main__":
    sys.exit(main())
