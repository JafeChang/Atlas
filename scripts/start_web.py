#!/usr/bin/env python3
"""
Atlas Web后台启动脚本

提供便捷的Web服务启动功能。
"""

import os
import sys
import argparse
import threading
import time
import webbrowser
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from atlas.web.app import main as start_web_service


def open_browser(url, delay=2):
    """延迟打开浏览器"""
    time.sleep(delay)
    try:
        webbrowser.open(url)
        print(f"🌐 已在浏览器中打开: {url}")
    except Exception as e:
        print(f"⚠️ 无法自动打开浏览器: {e}")
        print(f"请手动访问: {url}")


def start_server(host='localhost', port=5000, open_browser_flag=False):
    """启动Web服务器"""
    print(f"🚀 正在启动 Atlas Web后台服务...")
    print(f"📍 服务地址: http://{host}:{port}")
    print(f"🔧 工作目录: {Path.cwd()}")

    # 如果需要打开浏览器，在新线程中延迟打开
    if open_browser_flag:
        browser_thread = threading.Thread(
            target=open_browser,
            args=(f"http://{host}:{port}",)
        )
        browser_thread.daemon = True
        browser_thread.start()

    # 设置环境变量
    os.environ['FLASK_ENV'] = 'development'

    try:
        # 启动Web服务
        start_web_service()
    except KeyboardInterrupt:
        print("\n🛑 Web服务已停止")
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        return 1

    return 0


def main():
    parser = argparse.ArgumentParser(description="Atlas Web后台启动工具")
    parser.add_argument('--host', default='localhost', help='服务主机地址')
    parser.add_argument('--port', type=int, default=5000, help='服务端口')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--debug', action='store_true', help='启用调试模式')

    args = parser.parse_args()

    # 创建必要的目录
    directories = ['data', 'logs', 'src/atlas/web/static']
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)

    # 检查依赖
    try:
        import flask
        import flask_cors
        print("✅ Flask依赖检查通过")
    except ImportError as e:
        print(f"❌ 缺少依赖: {e}")
        print("请运行: uv add flask flask-cors")
        return 1

    # 启动服务
    return start_server(
        host=args.host,
        port=args.port,
        open_browser_flag=not args.no_browser
    )


if __name__ == "__main__":
    exit(main())