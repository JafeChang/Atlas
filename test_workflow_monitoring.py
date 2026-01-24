"""
工作流和监控功能测试脚本
"""

import sys
from pathlib import Path

# 添加src到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from atlas.scheduler.workflows import (
    Workflow,
    TaskNode,
    WorkflowManager,
    create_collection_workflow
)

print("=" * 80)
print("Atlas 工作流和监控功能测试")
print("=" * 80)

# 测试1: 工作流定义和验证
print("\n1. 测试工作流定义和验证...")
try:
    workflow = Workflow(
        workflow_id="test_workflow_1",
        workflow_name="测试工作流",
        description="用于测试的工作流"
    )

    # 添加任务节点（使用lambda作为占位符）
    task1 = TaskNode(
        task_id="task1",
        task_func=lambda: "Task 1 completed",
        task_name="任务1",
        params={}
    )
    workflow.add_task(task1)

    task2 = TaskNode(
        task_id="task2",
        task_func=lambda: "Task 2 completed",
        task_name="任务2",
        depends_on=["task1"],
        params={}
    )
    workflow.add_task(task2)

    # 验证工作流
    is_valid = workflow.validate()
    print(f"   ✓ 工作流验证: {is_valid}")
    print(f"   ✓ 工作流包含 {len(workflow.tasks)} 个任务")

except Exception as e:
    print(f"   ✗ 工作流定义失败: {e}")

# 测试2: 工作流管理器
print("\n2. 测试工作流管理器...")
try:
    # 注册工作流
    WorkflowManager.register_workflow(workflow)

    # 获取工作流
    retrieved_workflow = WorkflowManager.get_workflow("test_workflow_1")
    print(f"   ✓ 工作流注册和检索成功")

    # 列出所有工作流
    all_workflows = WorkflowManager.list_workflows()
    print(f"   ✓ 当前有 {len(all_workflows)} 个工作流")

    # 移除工作流
    WorkflowManager.remove_workflow("test_workflow_1")
    print(f"   ✓ 工作流移除成功")

except Exception as e:
    print(f"   ✗ 工作流管理器测试失败: {e}")

# 测试3: 预定义工作流
print("\n3. 测试预定义工作流...")
try:
    # 创建数据采集工作流（不实际执行）
    collection_workflow = create_collection_workflow("test_source")
    print(f"   ✓ 数据采集工作流创建成功")
    print(f"   ✓ 工作流包含 {len(collection_workflow.tasks)} 个任务")

    # 验证工作流
    is_valid = collection_workflow.validate()
    print(f"   ✓ 工作流验证: {is_valid}")

except Exception as e:
    print(f"   ✗ 预定义工作流测试失败: {e}")

# 测试4: 工作流拓扑排序
print("\n4. 测试工作流拓扑排序...")
try:
    complex_workflow = Workflow(
        workflow_id="test_complex",
        workflow_name="复杂工作流测试"
    )

    # 创建多层依赖关系
    # Level 1
    complex_workflow.add_task(TaskNode(
        task_id="a", task_func=lambda: "A", task_name="A", params={}
    ))
    complex_workflow.add_task(TaskNode(
        task_id="b", task_func=lambda: "B", task_name="B", params={}
    ))

    # Level 2
    complex_workflow.add_task(TaskNode(
        task_id="c", task_func=lambda: "C", task_name="C",
        depends_on=["a"], params={}
    ))
    complex_workflow.add_task(TaskNode(
        task_id="d", task_func=lambda: "D", task_name="D",
        depends_on=["a", "b"], params={}
    ))

    # Level 3
    complex_workflow.add_task(TaskNode(
        task_id="e", task_func=lambda: "E", task_name="E",
        depends_on=["c", "d"], params={}
    ))

    levels = complex_workflow._topological_sort()
    print(f"   ✓ 拓扑排序成功，共 {len(levels)} 层")
    for i, level in enumerate(levels):
        print(f"     Level {i + 1}: {', '.join(level)}")

    complex_workflow.validate()
    print("   ✓ 复杂工作流验证通过")

except Exception as e:
    print(f"   ✗ 拓扑排序测试失败: {e}")

# 测试5: 循环依赖检测
print("\n5. 测试循环依赖检测...")
try:
    bad_workflow = Workflow(
        workflow_id="test_circular",
        workflow_name="循环依赖测试"
    )

    # 创建循环依赖: A -> B -> C -> A
    bad_workflow.add_task(TaskNode(
        task_id="a", task_func=lambda: "A", task_name="A",
        depends_on=["c"], params={}
    ))
    bad_workflow.add_task(TaskNode(
        task_id="b", task_func=lambda: "B", task_name="B",
        depends_on=["a"], params={}
    ))
    bad_workflow.add_task(TaskNode(
        task_id="c", task_func=lambda: "C", task_name="C",
        depends_on=["b"], params={}
    ))

    bad_workflow.validate()
    print("   ✗ 应该检测到循环依赖但没有")

except ValueError as e:
    print(f"   ✓ 正确检测到循环依赖")
except Exception as e:
    print(f"   ✗ 意外错误: {e}")

# 测试6: API端点注册
print("\n6. 测试API端点注册...")
try:
    from atlas.web.fastapi_app import app

    # 检查监控相关路由
    monitoring_routes = [
        route for route in app.routes
        if hasattr(route, 'path') and '/monitoring' in route.path
    ]

    print(f"   ✓ 监控API端点: {len(monitoring_routes)} 个")
    for route in monitoring_routes[:5]:
        if hasattr(route, 'methods') and hasattr(route, 'path'):
            for method in route.methods:
                print(f"     {method} {route.path}")

except Exception as e:
    print(f"   ✗ API端点测试失败: {e}")

print("\n" + "=" * 80)
print("测试完成!")
print("=" * 80)
