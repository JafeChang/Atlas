"""
FastAPI应用测试脚本

测试TASK-004的所有API端点
"""

import sys
from pathlib import Path

# 添加src到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from atlas.web.fastapi_app import app
from fastapi.testclient import TestClient

# 创建测试客户端
client = TestClient(app)

print("=" * 80)
print("Atlas FastAPI 应用测试")
print("=" * 80)

# 测试1: 健康检查
print("\n1. 测试健康检查...")
response = client.get("/health")
print(f"   状态码: {response.status_code}")
print(f"   响应: {response.json()}")

# 测试2: API概览
print("\n2. 测试API概览...")
response = client.get("/api/v1/overview")
print(f"   状态码: {response.status_code}")
print(f"   响应: {response.json()}")

# 测试3: 系统概览（无需认证的测试端点）
print("\n3. 测试系统监控API...")
try:
    # 注意：系统监控API需要认证，这里只测试是否注册成功
    response = client.get("/api/v1/system/overview")
    if response.status_code == 401:
        print("   ✓ API端点已注册（需要认证）")
    else:
        print(f"   状态码: {response.status_code}")
except Exception as e:
    print(f"   错误: {e}")

# 测试4: 检查路由注册
print("\n4. 检查路由注册...")
routes_by_tag = {}
for route in app.routes:
    if hasattr(route, 'tag') and hasattr(route, 'path'):
        tag = route.tag
        if tag not in routes_by_tag:
            routes_by_tag[tag] = []
        routes_by_tag[tag].append(route.path)

for tag, routes in sorted(routes_by_tag.items()):
    print(f"   {tag}: {len(routes)} 个端点")

# 测试5: OpenAPI文档
print("\n5. 测试OpenAPI文档...")
response = client.get("/openapi.json")
print(f"   状态码: {response.status_code}")
if response.status_code == 200:
    openapi_data = response.json()
    print(f"   API标题: {openapi_data['info']['title']}")
    print(f"   API版本: {openapi_data['info']['version']}")
    print(f"   端点总数: {len(openapi_data['paths'])}")

print("\n" + "=" * 80)
print("测试完成!")
print("=" * 80)
print("\nAPI端点列表:")
for route in app.routes:
    if hasattr(route, 'methods') and hasattr(route, 'path'):
        for method in route.methods:
            print(f"  {method:6} {route.path}")
