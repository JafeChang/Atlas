"""
文档管理相关的Pydantic模型
"""

from datetime import datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field


# =============================================================================
# RawDocument 相关模型
# =============================================================================

class RawDocumentBase(BaseModel):
    """原始文档基础模型"""
    source_id: str = Field(..., description="数据源ID/名称")
    source_url: Optional[str] = Field(None, description="来源URL")
    source_type: Optional[str] = Field(None, description="来源类型")
    document_type: Optional[str] = Field(None, description="文档类型")
    raw_content: Optional[str] = Field(None, description="原始内容")
    raw_metadata: Optional[dict] = Field(None, description="原始元数据")
    title: Optional[str] = Field(None, description="标题")
    author: Optional[str] = Field(None, description="作者")
    published_at: Optional[datetime] = Field(None, description="发布时间")
    language: Optional[str] = Field(None, description="语言")


class RawDocumentCreate(RawDocumentBase):
    """创建原始文档"""
    content_hash: Optional[str] = Field(None, description="内容哈希")
    collector_version: Optional[str] = Field(None, description="采集器版本")


class RawDocumentUpdate(BaseModel):
    """更新原始文档"""
    processing_status: Optional[str] = Field(None, description="处理状态")
    processing_error: Optional[str] = Field(None, description="处理错误")
    processing_attempts: Optional[int] = Field(None, description="处理尝试次数")
    title: Optional[str] = Field(None, description="标题")
    author: Optional[str] = Field(None, description="作者")


class RawDocumentResponse(RawDocumentBase):
    """原始文档响应"""
    id: UUID = Field(..., description="文档ID")
    processing_status: str = Field(..., description="处理状态")
    processing_error: Optional[str] = Field(None, description="处理错误")
    processing_attempts: int = Field(..., description="处理尝试次数")
    content_hash: Optional[str] = Field(None, description="内容哈希")
    collected_at: datetime = Field(..., description="采集时间")
    collector_version: Optional[str] = Field(None, description="采集器版本")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    class Config:
        from_attributes = True


class RawDocumentListItem(BaseModel):
    """原始文档列表项"""
    id: UUID = Field(..., description="文档ID")
    source_id: str = Field(..., description="数据源ID")
    title: Optional[str] = Field(None, description="标题")
    document_type: Optional[str] = Field(None, description="文档类型")
    processing_status: str = Field(..., description="处理状态")
    collected_at: datetime = Field(..., description="采集时间")

    class Config:
        from_attributes = True


# =============================================================================
# ProcessedDocument 相关模型
# =============================================================================

class ProcessedDocumentBase(BaseModel):
    """处理后文档基础模型"""
    title: str = Field(..., description="标题")
    summary: Optional[str] = Field(None, description="摘要")
    content: Optional[str] = Field(None, description="内容")
    structured_content: Optional[dict] = Field(None, description="结构化内容")
    extracted_metadata: Optional[dict] = Field(None, description="提取的元数据")
    entities: Optional[dict] = Field(None, description="实体")
    keywords: Optional[str] = Field(None, description="关键词（逗号分隔）")
    categories: Optional[str] = Field(None, description="分类（逗号分隔）")


class ProcessedDocumentCreate(ProcessedDocumentBase):
    """创建处理后文档"""
    raw_document_id: UUID = Field(..., description="原始文档ID")
    processor_version: Optional[str] = Field(None, description="处理器版本")


class ProcessedDocumentUpdate(BaseModel):
    """更新处理后文档"""
    title: Optional[str] = Field(None, description="标题")
    summary: Optional[str] = Field(None, description="摘要")
    content: Optional[str] = Field(None, description="内容")
    keywords: Optional[str] = Field(None, description="关键词")
    categories: Optional[str] = Field(None, description="分类")


class ProcessedDocumentResponse(ProcessedDocumentBase):
    """处理后文档响应"""
    id: UUID = Field(..., description="文档ID")
    raw_document_id: UUID = Field(..., description="原始文档ID")
    processed_at: datetime = Field(..., description="处理时间")
    processor_version: Optional[str] = Field(None, description="处理器版本")

    class Config:
        from_attributes = True


class ProcessedDocumentListItem(BaseModel):
    """处理后文档列表项"""
    id: UUID = Field(..., description="文档ID")
    raw_document_id: UUID = Field(..., description="原始文档ID")
    title: str = Field(..., description="标题")
    summary: Optional[str] = Field(None, description="摘要")
    processed_at: datetime = Field(..., description="处理时间")

    class Config:
        from_attributes = True


# =============================================================================
# 批量操作模型
# =============================================================================

class DocumentBatchOperation(BaseModel):
    """文档批量操作"""
    document_ids: List[UUID] = Field(..., description="文档ID列表")
    operation: str = Field(..., description="操作类型: reprocess, delete")


class DocumentBatchResponse(BaseModel):
    """批量操作响应"""
    success_count: int = Field(..., description="成功数量")
    failed_count: int = Field(..., description="失败数量")
    errors: List[str] = Field(default_factory=list, description="错误信息")
