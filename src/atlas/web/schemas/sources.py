"""
数据源相关的Pydantic模型

定义数据源API的请求和响应数据结构。
"""

from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, field_serializer


# =============================================================================
# 基础模型
# =============================================================================

class DataSourceBase(BaseModel):
    """数据源基础模型"""
    name: str = Field(..., description="数据源名称（唯一标识）")
    source_type: str = Field(..., description="数据源类型（rss,api,web等）")
    url: Optional[str] = Field(None, description="数据源URL")
    enabled: bool = Field(True, description="是否启用")
    config: Optional[dict] = Field(default=None, description="配置参数")
    category: Optional[str] = Field(None, description="分类")
    language: str = Field("zh-CN", description="语言")
    collection_interval: int = Field(3600, description="采集间隔（秒）")
    max_items_per_run: int = Field(100, description="单次最大采集数量")
    retry_count: int = Field(3, description="重试次数")
    timeout: int = Field(30, description="超时时间（秒）")


class DataSourceCreate(DataSourceBase):
    """创建数据源请求"""
    tags: Optional[List[str]] = Field(default=None, description="标签列表")

    @field_validator('collection_interval', 'max_items_per_run', 'retry_count', 'timeout')
    @classmethod
    def validate_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError('必须大于0')
        return v


class DataSourceUpdate(BaseModel):
    """更新数据源请求"""
    url: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[dict] = None
    tags: Optional[List[str]] = None
    category: Optional[str] = None
    language: Optional[str] = None
    collection_interval: Optional[int] = None
    max_items_per_run: Optional[int] = None
    retry_count: Optional[int] = None
    timeout: Optional[int] = None

    @field_validator('collection_interval', 'max_items_per_run', 'retry_count', 'timeout')
    @classmethod
    def validate_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError('必须大于0')
        return v


# =============================================================================
# 响应模型
# =============================================================================

class DataSourceResponse(DataSourceBase):
    """数据源响应"""
    id: int
    tags: List[str] = Field(default_factory=list, description="标签列表")
    collection_count: int = Field(default=0, description="采集次数")
    success_count: int = Field(default=0, description="成功次数")
    error_count: int = Field(default=0, description="错误次数")
    last_collected_at: Optional[datetime] = Field(None, description="最后采集时间")
    last_success_at: Optional[datetime] = Field(None, description="最后成功时间")
    last_error: Optional[str] = Field(None, description="最后错误信息")
    created_at: datetime
    updated_at: datetime

    @field_validator('tags', mode='before')
    @classmethod
    def parse_tags(cls, v: Any) -> List[str]:
        """解析tags字段（字符串转列表）"""
        if isinstance(v, str):
            return v.split(",") if v else []
        return v

    class Config:
        from_attributes = True


class DataSourceListItem(BaseModel):
    """数据源列表项"""
    id: int
    name: str
    source_type: str
    enabled: bool
    category: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    collection_count: int
    success_count: int
    error_count: int
    last_collected_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    created_at: datetime

    @field_validator('tags', mode='before')
    @classmethod
    def parse_tags(cls, v: Any) -> List[str]:
        """解析tags字段（字符串转列表）"""
        if isinstance(v, str):
            return v.split(",") if v else []
        return v

    class Config:
        from_attributes = True


class DataSourceStats(BaseModel):
    """数据源统计"""
    source_id: int
    source_name: str
    collection_count: int
    success_count: int
    error_count: int
    success_rate: float = Field(description="成功率")
    last_collected_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None

    class Config:
        from_attributes = True


# =============================================================================
# 查询参数
# =============================================================================

class DataSourceQuery(BaseModel):
    """数据源查询参数"""
    enabled: Optional[bool] = Field(None, description="筛选启用状态")
    source_type: Optional[str] = Field(None, description="筛选数据源类型")
    category: Optional[str] = Field(None, description="筛选分类")
    search: Optional[str] = Field(None, description="搜索关键词")
    page: int = Field(1, ge=1, description="页码")
    page_size: int = Field(20, ge=1, le=100, description="每页数量")


class CollectionTaskCreate(BaseModel):
    """创建采集任务请求"""
    source_name: str = Field(..., description="数据源名称")
    task_type: str = Field("manual", description="任务类型（manual/scheduled）")
