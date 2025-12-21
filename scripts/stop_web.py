#!/usr/bin/env python3
"""
Atlas Web后台停止脚本
"""

import subprocess
import sys
import time


def stop_web_service():
    """停止Web服务"""
    try:
        # 查找正在运行的Flask进程
        result = subprocess.run([
            'pkill', '-f', 'python.*atlas.*web.*app'
        ], capture_output=True, text=True)

        if result.returncode == 0:
            print("✅ Atlas Web后台服务已停止")
        else:
            print("ℹ️  没有找到正在运行的Web服务")

        return 0

    except Exception as e:
        print(f"❌ 停止服务失败: {e}")
        return 1


def main():
    print("🛑 停止 Atlas Web后台服务")
    print("=" * 40)

    return stop_web_service()


if __name__ == "__main__":
    exit(main())