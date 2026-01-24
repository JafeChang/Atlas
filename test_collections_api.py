#!/usr/bin/env python3
"""测试采集任务API"""

import requests
import json

BASE_URL = "http://localhost:8000"

# 1. 登录获取token
print("=== 1. 登录 ===")
response = requests.post(
    f"{BASE_URL}/api/v1/auth/login",
    json={"username": "admin", "password": "admin123"}
)
data = response.json()
TOKEN = data["access_token"]
print(f"Token obtained: {TOKEN[:30]}...")

headers = {"Authorization": f"Bearer {TOKEN}"}

# 2. 创建采集任务
print("\n=== 2. 创建采集任务 ===")
response = requests.post(
    f"{BASE_URL}/api/v1/collections",
    json={"source_name": "example_rss", "task_type": "manual"},
    headers=headers
)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))
task_id = response.json()["id"]

# 3. 获取任务列表
print("\n=== 3. 获取任务列表 ===")
response = requests.get(f"{BASE_URL}/api/v1/collections", headers=headers)
result = response.json()
print(f"Total tasks: {result['data']['total']}")

# 4. 获取任务详情
print("\n=== 4. 获取任务详情 ===")
response = requests.get(f"{BASE_URL}/api/v1/collections/{task_id}", headers=headers)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))

# 5. 获取任务日志
print("\n=== 5. 获取任务日志 ===")
response = requests.get(f"{BASE_URL}/api/v1/collections/{task_id}/logs", headers=headers)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))

# 6. 批量创建任务
print("\n=== 6. 批量创建任务 ===")
response = requests.post(
    f"{BASE_URL}/api/v1/collections/batch",
    json={"source_names": ["example_rss", "tech_news"]},
    headers=headers
)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))

# 7. 取消任务
print("\n=== 7. 取消任务 ===")
response = requests.delete(f"{BASE_URL}/api/v1/collections/{task_id}", headers=headers)
print(json.dumps(response.json(), indent=2, ensure_ascii=False))

print("\n✅ 所有测试完成！")
