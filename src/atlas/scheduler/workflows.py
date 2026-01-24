"""
工作流编排模块

提供轻量级的DAG（有向无环图）工作流编排功能。
基于Celery任务实现任务依赖和流程控制。

设计原则：
- 简单优先：不引入Airflow等重型框架
- 本地优先：基于现有Celery任务
- 可扩展：支持自定义工作流
"""

from typing import Dict, List, Optional, Callable, Any
from datetime import datetime
from enum import Enum
import logging

from celery import chain, chord, group
from celery.canvas import Signature

from .celery_app import celery_app


logger = logging.getLogger(__name__)


class WorkflowStatus(str, Enum):
    """工作流状态"""
    PENDING = "pending"       # 等待执行
    RUNNING = "running"       # 执行中
    SUCCESS = "success"       # 成功
    FAILED = "failed"         # 失败
    PARTIAL = "partial"       # 部分成功


class TaskNode:
    """工作流任务节点"""

    def __init__(
        self,
        task_id: str,
        task_func: Callable,
        task_name: str,
        depends_on: Optional[List[str]] = None,
        params: Optional[Dict[str, Any]] = None,
        retry_on_failure: bool = True,
        max_retries: int = 3
    ):
        """
        初始化任务节点

        Args:
            task_id: 任务ID（在工作流内唯一）
            task_func: Celery任务函数
            task_name: 任务名称
            depends_on: 依赖的任务ID列表
            params: 任务参数
            retry_on_failure: 失败是否重试
            max_retries: 最大重试次数
        """
        self.task_id = task_id
        self.task_func = task_func
        self.task_name = task_name
        self.depends_on = depends_on or []
        self.params = params or {}
        self.retry_on_failure = retry_on_failure
        self.max_retries = max_retries
        self.status = WorkflowStatus.PENDING
        self.result = None
        self.error = None

    def __repr__(self) -> str:
        return f"<TaskNode(id={self.task_id}, name={self.task_name}, status={self.status})>"


class Workflow:
    """工作流定义"""

    def __init__(
        self,
        workflow_id: str,
        workflow_name: str,
        description: Optional[str] = None
    ):
        """
        初始化工作流

        Args:
            workflow_id: 工作流ID
            workflow_name: 工作流名称
            description: 工作流描述
        """
        self.workflow_id = workflow_id
        self.workflow_name = workflow_name
        self.description = description
        self.tasks: Dict[str, TaskNode] = {}
        self.status = WorkflowStatus.PENDING
        self.created_at = datetime.now()
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None

    def add_task(self, task: TaskNode) -> 'Workflow':
        """
        添加任务到工作流

        Args:
            task: 任务节点

        Returns:
            Workflow: 返回自身，支持链式调用
        """
        if task.task_id in self.tasks:
            raise ValueError(f"任务ID {task.task_id} 已存在")

        # 验证依赖的任务是否存在
        for dep_id in task.depends_on:
            if dep_id not in self.tasks:
                raise ValueError(f"依赖的任务 {dep_id} 不存在")

        self.tasks[task.task_id] = task
        return self

    def validate(self) -> bool:
        """
        验证工作流定义

        检查：
        - 是否有循环依赖
        - 是否有孤立的节点
        - 是否有至少一个入口节点

        Returns:
            bool: 验证是否通过
        """
        if not self.tasks:
            raise ValueError("工作流没有任务")

        # 检查循环依赖
        visited = set()
        rec_stack = set()

        def has_cycle(task_id: str) -> bool:
            visited.add(task_id)
            rec_stack.add(task_id)

            task = self.tasks[task_id]
            for dep_id in task.depends_on:
                if dep_id not in visited:
                    if has_cycle(dep_id):
                        return True
                elif dep_id in rec_stack:
                    return True

            rec_stack.remove(task_id)
            return False

        for task_id in self.tasks:
            if task_id not in visited:
                if has_cycle(task_id):
                    raise ValueError(f"工作流存在循环依赖")

        # 检查是否有入口节点（无依赖的节点）
        entry_nodes = [
            task_id for task_id, task in self.tasks.items()
            if not task.depends_on
        ]

        if not entry_nodes:
            raise ValueError("工作流没有入口节点（所有任务都有依赖）")

        return True

    def build_celery_dag(self) -> Signature:
        """
        构建Celery DAG

        根据任务依赖关系构建Celery的canvas（chain/chord/group）

        Returns:
            Signature: Celery任务签名
        """
        self.validate()

        # 找出所有任务层级
        levels = self._topological_sort()

        # 构建Celery任务链
        task_chain = []
        for level in levels:
            if len(level) == 1:
                # 单个任务
                task_node = self.tasks[level[0]]
                sig = self._build_task_signature(task_node)
                task_chain.append(sig)
            else:
                # 多个并行任务
                group_tasks = []
                for task_id in level:
                    task_node = self.tasks[task_id]
                    sig = self._build_task_signature(task_node)
                    group_tasks.append(sig)

                # 使用group实现并行执行
                task_chain.append(group(*group_tasks))

        # 使用chain串联所有层级
        if len(task_chain) == 1:
            return task_chain[0]
        else:
            return chain(*task_chain)

    def _topological_sort(self) -> List[List[str]]:
        """
        拓扑排序，返回任务层级

        Returns:
            List[List[str]]: 每层可以并行执行的任务ID列表
        """
        levels = []
        remaining_tasks = set(self.tasks.keys())
        completed_tasks = set()

        while remaining_tasks:
            # 找出当前可执行的任务（依赖都已满足）
            current_level = []
            for task_id in list(remaining_tasks):
                task = self.tasks[task_id]
                if all(dep_id in completed_tasks for dep_id in task.depends_on):
                    current_level.append(task_id)

            if not current_level:
                # 没有可执行的任务，说明有循环依赖
                raise ValueError("无法进行拓扑排序，可能存在循环依赖")

            levels.append(current_level)
            completed_tasks.update(current_level)
            remaining_tasks -= set(current_level)

        return levels

    def _build_task_signature(self, task: TaskNode) -> Signature:
        """
        构建Celery任务签名

        Args:
            task: 任务节点

        Returns:
            Signature: Celery任务签名
        """
        # 构建任务选项
        options = {}
        if task.retry_on_failure:
            options.update({
                'retry': True,
                'retry_policy': {
                    'max_retries': task.max_retries,
                    'interval_start': 0,
                    'interval_step': 60,
                    'interval_max': 600,
                }
            })

        # 创建任务签名
        sig = task.task_func.signature(
            kwargs=task.params,
            options=options,
            task_id=f"{self.workflow_id}.{task.task_id}"
        )

        return sig

    def run(self) -> str:
        """
        执行工作流

        Returns:
            str: Celery任务ID
        """
        logger.info(f"启动工作流: {self.workflow_name} ({self.workflow_id})")

        # 构建并执行Celery DAG
        dag = self.build_celery_dag()
        result = dag.apply_async()

        self.status = WorkflowStatus.RUNNING
        self.started_at = datetime.now()

        logger.info(f"工作流已提交: {result.id}")
        return result.id

    def __repr__(self) -> str:
        return f"<Workflow(id={self.workflow_id}, name={self.workflow_name}, tasks={len(self.tasks)})>"


# =============================================================================
# 预定义工作流
# =============================================================================

def create_collection_workflow(source_name: str) -> Workflow:
    """
    创建数据采集工作流

    工作流步骤：
    1. 触发采集任务
    2. 等待采集完成
    3. 触发文档处理

    Args:
        source_name: 数据源名称

    Returns:
        Workflow: 工作流实例
    """
    from .celery_tasks import collect_from_source, process_documents

    workflow_id = f"collection.{source_name}.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    workflow = Workflow(
        workflow_id=workflow_id,
        workflow_name=f"数据采集工作流-{source_name}",
        description=f"从{source_name}采集并处理数据"
    )

    # 步骤1: 触发采集
    task_collect = TaskNode(
        task_id="collect",
        task_func=collect_from_source,
        task_name="采集数据",
        params={"source_name": source_name},
        retry_on_failure=True,
        max_retries=3
    )
    workflow.add_task(task_collect)

    # 步骤2: 处理文档（依赖采集完成）
    task_process = TaskNode(
        task_id="process",
        task_func=process_documents,
        task_name="处理文档",
        depends_on=["collect"],
        params={"source_name": source_name},
        retry_on_failure=True,
        max_retries=2
    )
    workflow.add_task(task_process)

    return workflow


def create_batch_processing_workflow(
    source_names: List[str]
) -> Workflow:
    """
    创建批量数据处理工作流

    工作流步骤：
    1. 并行采集多个数据源
    2. 等待所有采集完成
    3. 批量处理所有文档
    4. 生成处理报告

    Args:
        source_names: 数据源名称列表

    Returns:
        Workflow: 工作流实例
    """
    from .celery_tasks import (
        collect_from_source,
        process_documents,
        generate_report
    )

    workflow_id = f"batch.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    workflow = Workflow(
        workflow_id=workflow_id,
        workflow_name="批量数据处理工作流",
        description=f"批量处理{len(source_names)}个数据源"
    )

    # 第一层: 并行采集多个数据源
    for idx, source_name in enumerate(source_names):
        task_collect = TaskNode(
            task_id=f"collect_{idx}",
            task_func=collect_from_source,
            task_name=f"采集{source_name}",
            params={"source_name": source_name},
            retry_on_failure=True,
            max_retries=3
        )
        workflow.add_task(task_collect)

    # 第二层: 批量处理（依赖所有采集任务完成）
    for idx, source_name in enumerate(source_names):
        task_process = TaskNode(
            task_id=f"process_{idx}",
            task_func=process_documents,
            task_name=f"处理{source_name}",
            depends_on=[f"collect_{i}" for i in range(len(source_names))],
            params={"source_name": source_name},
            retry_on_failure=True,
            max_retries=2
        )
        workflow.add_task(task_process)

    # 第三层: 生成报告
    task_report = TaskNode(
        task_id="report",
        task_func=generate_report,
        task_name="生成报告",
        depends_on=[f"process_{i}" for i in range(len(source_names))],
        params={"source_names": source_names},
        retry_on_failure=False
    )
    workflow.add_task(task_report)

    return workflow


def create_full_pipeline_workflow(source_name: str) -> Workflow:
    """
    创建完整数据处理流水线工作流

    工作流步骤：
    1. 采集数据
    2. 处理文档
    3. 内容提取
    4. 向量化
    5. 索引构建

    Args:
        source_name: 数据源名称

    Returns:
        Workflow: 工作流实例
    """
    from .celery_tasks import (
        collect_from_source,
        process_documents,
        extract_content,
        vectorize_documents,
        build_index
    )

    workflow_id = f"pipeline.{source_name}.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    workflow = Workflow(
        workflow_id=workflow_id,
        workflow_name=f"完整流水线-{source_name}",
        description=f"从采集到索引的完整数据处理流程"
    )

    # 步骤1: 采集
    task_collect = TaskNode(
        task_id="collect",
        task_func=collect_from_source,
        task_name="采集数据",
        params={"source_name": source_name}
    )
    workflow.add_task(task_collect)

    # 步骤2: 处理
    task_process = TaskNode(
        task_id="process",
        task_func=process_documents,
        task_name="处理文档",
        depends_on=["collect"],
        params={"source_name": source_name}
    )
    workflow.add_task(task_process)

    # 步骤3: 内容提取
    task_extract = TaskNode(
        task_id="extract",
        task_func=extract_content,
        task_name="内容提取",
        depends_on=["process"],
        params={"source_name": source_name}
    )
    workflow.add_task(task_extract)

    # 步骤4: 向量化
    task_vectorize = TaskNode(
        task_id="vectorize",
        task_func=vectorize_documents,
        task_name="向量化",
        depends_on=["extract"],
        params={"source_name": source_name}
    )
    workflow.add_task(task_vectorize)

    # 步骤5: 构建索引
    task_index = TaskNode(
        task_id="index",
        task_func=build_index,
        task_name="构建索引",
        depends_on=["vectorize"],
        params={"source_name": source_name}
    )
    workflow.add_task(task_index)

    return workflow


# =============================================================================
# 工作流管理器
# =============================================================================

class WorkflowManager:
    """工作流管理器"""

    _instance = None
    _workflows: Dict[str, Workflow] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def register_workflow(cls, workflow: Workflow) -> None:
        """
        注册工作流

        Args:
            workflow: 工作流实例
        """
        cls._workflows[workflow.workflow_id] = workflow
        logger.info(f"注册工作流: {workflow.workflow_id}")

    @classmethod
    def get_workflow(cls, workflow_id: str) -> Optional[Workflow]:
        """
        获取工作流

        Args:
            workflow_id: 工作流ID

        Returns:
            Optional[Workflow]: 工作流实例或None
        """
        return cls._workflows.get(workflow_id)

    @classmethod
    def list_workflows(cls) -> List[Workflow]:
        """
        列出所有工作流

        Returns:
            List[Workflow]: 工作流列表
        """
        return list(cls._workflows.values())

    @classmethod
    def remove_workflow(cls, workflow_id: str) -> bool:
        """
        移除工作流

        Args:
            workflow_id: 工作流ID

        Returns:
            bool: 是否移除成功
        """
        if workflow_id in cls._workflows:
            del cls._workflows[workflow_id]
            logger.info(f"移除工作流: {workflow_id}")
            return True
        return False
