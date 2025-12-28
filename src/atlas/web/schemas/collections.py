"""
采集任务相关的Pydantic模型

定义采集任务API的请求和响应数据结构。
"""

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


# =============================================================================
# 基础模型
# =============================================================================

class CollectionTaskBase(BaseModel):
    """采集任务基础模型"""
    source_name: str = Field(..., description="数据源名称")
    task_type: str = Field("manual", description="任务类型（manual/scheduled）")


class CollectionTaskCreate(CollectionTaskBase):
    """创建采集任务请求"""
    pass


class BatchCollectionCreate(BaseModel):
    """批量采集请求"""
    source_names: List[str] = Field(..., description="数据源名称列表", min_length=1, max_length=10)

    @field_validator('source_names')
    @classmethod
    def validate_sources(cls, v: List[str]) -> List[str]:
        if len(v) == 0:
            raise ValueError('至少需要一个数据源')
        # 去重
        return list(set(v))


# =============================================================================
# 响应模型
# =============================================================================

class CollectionTaskResponse(BaseModel):
    """采集任务响应"""
    id: int
    source_id: str = Field(..., description="数据源名称")
    task_type: str
    status: str

    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    items_collected: int = Field(default=0, description="采集项目数")
    items_processed: int = Field(default=0, description="处理项目数")
    items_failed: int = Field(default=0, description="失败项目数")

    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class CollectionTaskListItem(BaseModel):
    """采集任务列表项"""
    id: int
    source_id: str
    task_type: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    items_collected: int
    items_processed: int
    items_failed: int
    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class TaskLogEntry(BaseModel):
    """任务日志条目"""
    timestamp: datetime
    level: str = Field(..., description="日志级别（INFO/WARNING/ERROR）")
    message: str


class TaskLogsResponse(BaseModel):
    """任务日志响应"""
    task_id: int
    source_name: str
    status: str
    logs: List[TaskLogEntry]
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# =============================================================================
# 查询参数
# =============================================================================

class CollectionTaskQuery(BaseModel):
    """采集任务查询参数"""
    source_name: Optional[str] = Field(None, description="数据源名称")
    task_type: Optional[str] = Field(None, description="任务类型")
    status: Optional[str] = Field(None, description="任务状态")
    page: int = Field(1, ge=1, description="页码")
    page_size: int = Field(20, ge=1, le=100, description="每页数量")


class BatchCollectionResponse(BaseModel):
    """批量采集响应"""
    total_tasks: int = Field(..., description="总任务数")
    success_count: int = Field(..., description="成功创建任务数")
    failed_count: int = Field(..., description="失败任务数")
    tasks: List[CollectionTaskResponse] = Field(default_factory=list, description="创建的任务列表")
    errors: List[dict] = Field(default_factory=list, description="错误信息列表")
