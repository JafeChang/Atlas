#!/usr/bin/env python3
"""
MinIO服务器启动脚本
简化版本，使用Python实现基础MinIO功能
"""

import os
import sys
import json
import time
import hashlib
import mimetypes
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import socket

class MinIODataStore:
    """简化的MinIO兼容存储实现"""

    def __init__(self, data_dir="/tmp/minio-data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.buckets = {}
        self.load_metadata()

    def load_metadata(self):
        """加载元数据"""
        metadata_file = self.data_dir / "buckets.json"
        if metadata_file.exists():
            try:
                with open(metadata_file, 'r') as f:
                    self.buckets = json.load(f)
            except Exception as e:
                print(f"加载元数据失败: {e}")
                self.buckets = {}

    def save_metadata(self):
        """保存元数据"""
        metadata_file = self.data_dir / "buckets.json"
        try:
            with open(metadata_file, 'w') as f:
                json.dump(self.buckets, f, indent=2)
        except Exception as e:
            print(f"保存元数据失败: {e}")

    def create_bucket(self, bucket_name):
        """创建存储桶"""
        bucket_path = self.data_dir / bucket_name
        bucket_path.mkdir(parents=True, exist_ok=True)
        self.buckets[bucket_name] = {
            "created_at": datetime.now().isoformat(),
            "objects": {}
        }
        self.save_metadata()
        return True

    def bucket_exists(self, bucket_name):
        """检查存储桶是否存在"""
        return bucket_name in self.buckets

    def upload_file(self, bucket_name, object_name, content, content_type=None):
        """上传文件"""
        if not self.bucket_exists(bucket_name):
            if not self.create_bucket(bucket_name):
                return False

        object_path = self.data_dir / bucket_name / object_name
        object_path.parent.mkdir(parents=True, exist_ok=True)

        with open(object_path, 'wb') as f:
            f.write(content)

        # 更新元数据
        file_hash = hashlib.sha256(content).hexdigest()
        file_size = len(content)

        self.buckets[bucket_name]["objects"][object_name] = {
            "size": file_size,
            "hash": file_hash,
            "content_type": content_type or mimetypes.guess_type(object_name, 'application/octet-stream'),
            "created_at": datetime.now().isoformat()
        }
        self.save_metadata()
        return True

    def download_file(self, bucket_name, object_name):
        """下载文件"""
        if not self.bucket_exists(bucket_name):
            return None

        object_path = self.data_dir / bucket_name / object_name
        if not object_path.exists():
            return None

        with open(object_path, 'rb') as f:
            return f.read()

    def list_objects(self, bucket_name, prefix=None):
        """列出对象"""
        if not self.bucket_exists(bucket_name):
            return []

        objects = []
        bucket_path = self.data_dir / bucket_name

        for obj_file in bucket_path.rglob("*"):
            if obj_file.is_file():
                rel_path = obj_file.relative_to(bucket_path)
                obj_name = str(rel_path).replace("\\", "/")

                if prefix and not obj_name.startswith(prefix):
                    continue

                objects.append(obj_name)

        return objects

class MinIORequestHandler(BaseHTTPRequestHandler):
    """MinIO API请求处理器"""

    def __init__(self, store, *args, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """处理GET请求"""
        path = self.path.strip('/')

        if path == "health/live":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return

        # 解析路径: /bucket/object
        parts = path.split("/", 2)
        if len(parts) < 1:
            self.send_error(404, "Not Found")
            return

        bucket_name = parts[0]
        if len(parts) > 1:
            object_name = "/".join(parts[1:])

            # 下载文件
            content = self.store.download_file(bucket_name, object_name)
            if content is None:
                self.send_error(404, "Object Not Found")
                return

            self.send_response(200)
            content_type = mimetypes.guess_type(object_name, 'application/octet-stream')
            self.send_header("Content-type", content_type)
            self.end_headers()
            self.wfile.write(content)
        else:
            # 列出存储桶或对象
            if self.store.bucket_exists(bucket_name):
                objects = self.store.list_objects(bucket_name)
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"objects": objects}).encode())
            else:
                # 列出所有存储桶
                buckets = list(self.store.buckets.keys())
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"buckets": buckets}).encode())

    def do_PUT(self):
        """处理PUT请求"""
        path = self.path.strip('/')

        # 解析路径: /bucket/object
        parts = path.split("/", 2)
        if len(parts) < 2:
            self.send_error(400, "Bad Request")
            return

        bucket_name = parts[0]
        object_name = "/".join(parts[1:])

        if not object_name:
            self.send_error(400, "Bad Request")
            return

        # 读取请求体
        content_length = int(self.headers.get('content-length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request")
            return

        content = self.rfile.read(content_length)

        # 上传文件
        content_type = self.headers.get('content-type', 'application/octet-stream')
        if self.store.upload_file(bucket_name, object_name, content, content_type):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode())
        else:
            self.send_error(500, "Upload Failed")

def start_minio_server():
    """启动MinIO服务器"""
    print("🚀 启动简化版MinIO服务器")
    print("=" * 50)

    # 检查端口
    port = 9000
    print(f"🔍 检查端口 {port}...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('localhost', port))
        print(f"✅ 端口 {port} 可用")
    except OSError:
        print(f"❌ 端口 {port} 已被占用")
        return

    # 初始化存储
    data_dir = "/tmp/minio-data"
    store = MinIODataStore(data_dir)

    # 创建默认存储桶
    default_bucket = "atlas-raw-data"
    if not store.bucket_exists(default_bucket):
        print(f"📁 创建默认存储桶: {default_bucket}")
        store.create_bucket(default_bucket)
    else:
        print(f"✅ 存储桶已存在: {default_bucket}")

    # 启动HTTP服务器
    server_address = ('localhost', port)
    def handler(*args, **kwargs):
        return MinIORequestHandler(store, *args, **kwargs)
    httpd = HTTPServer(server_address, handler)

    print(f"🌐 MinIO服务器启动成功！")
    print(f"📋 服务地址: http://localhost:{port}")
    print(f"📁 数据目录: {data_dir}")
    print(f"🪣 存储桶: {list(store.buckets.keys())}")
    print("按 Ctrl+C 停止服务")
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 收到停止信号，正在关闭服务器...")
    finally:
        httpd.server_close()
        print("✅ MinIO服务器已停止")

if __name__ == "__main__":
    start_minio_server()