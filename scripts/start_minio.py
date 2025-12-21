#!/usr/bin/env python3
"""
Atlas MinIO对象存储启动脚本
"""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path

def main():
    """启动MinIO服务器"""
    print("🚀 Atlas MinIO对象存储启动脚本")
    print("=" * 50)

    # 检查当前目录
    if not Path("config/minio/minio.env").exists():
        print("❌ 错误: MinIO配置文件不存在")
        print("请确保 config/minio/minio.env 文件存在")
        sys.exit(1)

    print("📍 当前目录:", Path.cwd().absolute())
    print("📁 配置文件:", Path("config/minio/minio.env").absolute())

    # 创建数据目录
    data_dir = Path("/tmp/minio-data")
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"📁 数据目录: {data_dir}")

    # 检查MinIO是否已经运行
    try:
        result = subprocess.run(
            ["curl", "-s", "http://localhost:9000/minio/health/live"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print("✅ MinIO服务器已在运行")
            print("📋 MinIO控制台: http://localhost:9001")
            print("👤 用户名: minioadmin")
            print("🔑 密码: minioadmin123456")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    print("\n🔧 正在启动MinIO服务器...")
    print("📋 MinIO控制台: http://localhost:9001")
    print("👤 用户名: minioadmin")
    print("🔑 密码: minioadmin123456")
    print()

    # 启动MinIO服务器
    try:
        # 设置环境变量
        env = os.environ.copy()
        env_file = Path("config/minio/minio.env")

        # 读取.env文件
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    env[key] = value

        # 启动MinIO
        cmd = [
            "minio", "server",
            "--address", ":9000",
            "--console-address", ":9001",
            "/tmp/minio-data"
        ]

        print("🚀 启动命令:", " ".join(cmd))
        print("按 Ctrl+C 停止服务")
        print()

        # 启动MinIO服务
        process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # 等待服务启动
        time.sleep(3)

        # 检查服务是否成功启动
        try:
            result = subprocess.run(
                ["curl", "-s", "http://localhost:9000/minio/health/live"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                print("✅ MinIO服务启动成功！")
                print("📊 实时监控:")
                print("   - 服务地址: http://localhost:9000")
                print("   - 控制台地址: http://localhost:9001")
                print("   - 存储桶: atlas-raw-data")
            else:
                print("❌ MinIO服务启动失败")
                print("错误信息:", result.stderr)
        except Exception as e:
            print(f"⚠️ 检查服务状态时出错: {e}")

        # 监控服务输出
        print("\n📋 MinIO服务日志:")
        print("-" * 40)

        # 使用非阻塞方式读取输出
        import select
        while process.poll() is None:
            # 检查是否有输出可读
            if select.select([process.stdout, process.stderr], [], [], 0.1)[0]:
                if process.stdout in select.select([process.stdout], [], [], 0.1)[0]:
                    line = process.stdout.readline()
                    if line:
                        print(line.strip())
                if process.stderr in select.select([process.stderr], [], [], 0.1)[0]:
                    line = process.stderr.readline()
                    if line:
                        print(line.strip())

        process.wait()

    except KeyboardInterrupt:
        print("\n👋 收到停止信号，正在关闭MinIO服务...")
        if 'process' in locals():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        print("✅ MinIO服务已停止")

    except Exception as e:
        print(f"❌ 启动MinIO时发生错误: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()