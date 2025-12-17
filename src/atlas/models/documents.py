"""
Atlas 数据模型定义

定义系统中使用的所有数据结构，包括：
- 原始文档模型
- 处理后的内容模型
- 数据源配置模型
- 任务状态模型
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl, validator


class DocumentType(str, Enum):
    """文档类型枚举"""
    RSS = "rss"
    HTML = "html"
    JSON = "json"
    TEXT = "text"
    XML = "xml"
    PDF = "pdf"
    UNKNOWN = "unknown"


class ProcessingStatus(str, Enum):
    """处理状态枚举"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceType(str, Enum):
    """数据源类型枚举"""
    RSS_FEED = "rss_feed"
    WEBSITE = "website"
    API = "api"
    FILE = "file"
    DATABASE = "database"


class BaseDocument(BaseModel):
    """基础文档模型"""

    id: UUID = Field(default_factory=uuid4, description="文档唯一标识")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="更新时间")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }


class RawDocument(BaseDocument):
    """原始文档模型"""

    source_id: str = Field(description="数据源ID")
    source_url: Optional[HttpUrl] = Field(default=None, description="原始URL")
    source_type: SourceType = Field(description="数据源类型")
    document_type: DocumentType = Field(description="文档类型")

    # 原始内容
    raw_content: str = Field(description="原始内容")
    raw_metadata: Dict[str, Any] = Field(default_factory=dict, description="原始元数据")

    # 采集信息
    collected_at: datetime = Field(default_factory=datetime.utcnow, description="采集时间")
    collector_version: str = Field(default="0.1.0", description="采集器版本")

    # 处理状态
    processing_status: ProcessingStatus = Field(default=ProcessingStatus.PENDING, description="处理状态")
    processing_error: Optional[str] = Field(default=None, description="处理错误信息")
    processing_attempts: int = Field(default=0, description="处理尝试次数")

    # 内容特征
    content_hash: Optional[str] = Field(default=None, description="内容哈希值")
    title: Optional[str] = Field(default=None, description="文档标题")
    author: Optional[str] = Field(default=None, description="作者")
    published_at: Optional[datetime] = Field(default=None, description="发布时间")
    language: Optional[str] = Field(default=None, description="语言代码")

    @validator('content_hash', pre=True, always=True)
    def generate_content_hash(cls, v, values):
        """如果没有提供哈希值，根据内容生成"""
        if v is None and 'raw_content' in values:
            import hashlib
            content = values['raw_content']
            return hashlib.sha256(content.encode('utf-8')).hexdigest()
        return v


class ProcessedDocument(BaseDocument):
    """处理后文档模型"""

    raw_document_id: UUID = Field(description="原始文档ID")

    # 处理后的内容
    title: Optional[str] = Field(default=None, description="提取的标题")
    summary: Optional[str] = Field(default=None, description="内容摘要")
    content: Optional[str] = Field(default=None, description="清理后的内容")
    structured_content: Optional[Dict[str, Any]] = Field(default=None, description="结构化内容")

    # 提取的元数据
    extracted_metadata: Dict[str, Any] = Field(default_factory=dict, description="提取的元数据")
    entities: List[Dict[str, Any]] = Field(default_factory=list, description="识别的实体")
    keywords: List[str] = Field(default_factory=list, description="关键词")
    categories: List[str] = Field(default_factory=list, description="分类")

    # 处理信息
    processed_at: datetime = Field(default_factory=datetime.utcnow, description="处理时间")
    processor_version: str = Field(default="0.1.0", description="处理器版本")
    processing_time_ms: Optional[int] = Field(default=None, description="处理耗时(毫秒)")

    # 相似性分析
    content_hash: Optional[str] = Field(default=None, description="内容哈希值")
    similarity_group_id: Optional[str] = Field(default=None, description="相似组ID")
    similarity_score: Optional[float] = Field(default=None, description="与相似组的相似度分数")
    is_duplicate: bool = Field(default=False, description="是否为重复内容")

    # 质量评估
    quality_score: Optional[float] = Field(default=None, description="内容质量分数 (0-1)")
    relevance_score: Optional[float] = Field(default=None, description="相关性分数 (0-1)")

    @validator('content_hash', pre=True, always=True)
    def generate_content_hash(cls, v, values):
        """如果没有提供哈希值，根据处理后的内容生成"""
        if v is None:
            content_parts = []
            for field in ['title', 'summary', 'content']:
                if field in values and values[field]:
                    content_parts.append(values[field])

            if content_parts:
                combined_content = ' '.join(content_parts)
                import hashlib
                return hashlib.sha256(combined_content.encode('utf-8')).hexdigest()
        return v


class DataSource(BaseModel):
    """数据源配置模型"""

    id: str = Field(description="数据源唯一标识")
    name: str = Field(description="数据源名称")
    description: Optional[str] = Field(default=None, description="数据源描述")
    source_type: SourceType = Field(description="数据源类型")

    # 访问配置
    url: Optional[HttpUrl] = Field(default=None, description="数据源URL")
    config: Dict[str, Any] = Field(default_factory=dict, description="数据源特定配置")

    # 采集配置
    enabled: bool = Field(default=True, description="是否启用")
    collection_interval: int = Field(default=3600, description="采集间隔(秒)")
    max_items_per_run: int = Field(default=100, description="单次运行最大采集数量")
    retry_count: int = Field(default=3, description="失败重试次数")
    timeout: int = Field(default=30, description="请求超时时间(秒)")

    # 元数据
    tags: List[str] = Field(default_factory=list, description="标签")
    category: Optional[str] = Field(default=None, description="分类")
    language: Optional[str] = Field(default=None, description="主要语言")

    # 状态信息
    created_at: datetime = Field(default_factory=datetime.utcnow, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="更新时间")
    last_collected_at: Optional[datetime] = Field(default=None, description="最后采集时间")
    last_success_at: Optional[datetime] = Field(default=None, description="最后成功时间")
    collection_count: int = Field(default=0, description="采集次数")
    success_count: int = Field(default=0, description="成功次数")
    error_count: int = Field(default=0, description="错误次数")
    last_error: Optional[str] = Field(default=None, description="最后一次错误信息")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class CollectionTask(BaseModel):
    """采集任务模型"""

    id: UUID = Field(default_factory=uuid4, description="任务唯一标识")
    source_id: str = Field(description="数据源ID")
    task_type: str = Field(default="collection", description="任务类型")

    # 任务状态
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="任务状态")
    priority: int = Field(default=0, description="优先级 (数字越大优先级越高)")

    # 时间信息
    created_at: datetime = Field(default_factory=datetime.utcnow, description="创建时间")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    completed_at: Optional[datetime] = Field(default=None, description="完成时间")

    # 执行信息
    worker_id: Optional[str] = Field(default=None, description="执行器ID")
    retry_count: int = Field(default=0, description="重试次数")
    max_retries: int = Field(default=3, description="最大重试次数")

    # 结果信息
    items_collected: int = Field(default=0, description="采集的项目数量")
    items_processed: int = Field(default=0, description="处理的项目数量")
    items_failed: int = Field(default=0, description="失败的项目数量")

    # 错误信息
    error_message: Optional[str] = Field(default=None, description="错误信息")
    error_details: Optional[Dict[str, Any]] = Field(default=None, description="错误详情")

    # 执行配置
    config: Dict[str, Any] = Field(default_factory=dict, description="任务配置")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }


class SystemMetrics(BaseModel):
    """系统指标模型"""

    id: UUID = Field(default_factory=uuid4, description="指标唯一标识")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="记录时间")

    # 文档统计
    total_raw_documents: int = Field(default=0, description="原始文档总数")
    total_processed_documents: int = Field(default=0, description="处理后文档总数")
    documents_last_24h: int = Field(default=0, description="过去24小时文档数")

    # 数据源统计
    total_sources: int = Field(default=0, description="数据源总数")
    active_sources: int = Field(default=0, description="活跃数据源数")
    failed_sources: int = Field(default=0, description="失败数据源数")

    # 任务统计
    pending_tasks: int = Field(default=0, description="待处理任务数")
    running_tasks: int = Field(default=0, description="运行中任务数")
    completed_tasks_last_24h: int = Field(default=0, description="过去24小时完成任务数")

    # 系统资源
    cpu_usage_percent: Optional[float] = Field(default=None, description="CPU使用率")
    memory_usage_mb: Optional[float] = Field(default=None, description="内存使用量(MB)")
    disk_usage_mb: Optional[float] = Field(default=None, description="磁盘使用量(MB)")

    # 性能指标
    avg_processing_time_ms: Optional[float] = Field(default=None, description="平均处理时间(毫秒)")
    avg_collection_time_ms: Optional[float] = Field(default=None, description="平均采集时间(毫秒)")

    # 错误率
    error_rate_last_24h: Optional[float] = Field(default=None, description="过去24小时错误率")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }


class DuplicateGroup(BaseModel):
    """重复内容组模型"""

    group_id: str = Field(description="重复组唯一标识")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="更新时间")

    # 代表文档
    representative_document_id: UUID = Field(description="代表文档ID")

    # 组信息
    document_count: int = Field(default=0, description="组中文档数量")
    similarity_threshold: float = Field(description="相似度阈值")

    # 统计信息
    total_content_length: int = Field(default=0, description="内容总长度")
    avg_content_length: float = Field(default=0.0, description="平均内容长度")
    min_published_at: Optional[datetime] = Field(default=None, description="最早发布时间")
    max_published_at: Optional[datetime] = Field(default=None, description="最晚发布时间")

    # 检测信息
    detection_method: str = Field(description="检测方法")
    detection_config: Dict[str, Any] = Field(default_factory=dict, description="检测配置")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }