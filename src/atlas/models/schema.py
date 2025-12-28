"""
Atlas 数据库模型定义

支持SQLite和PostgreSQL双后端的ORM模型。
使用SQLAlchemy 2.0异步API。
"""

from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, JSON, Enum as SQLEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    """所有模型的基类"""
    pass


class DataSource(Base):
    """数据源表"""
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 逗号分隔的标签，SQLite兼容
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="zh-CN")

    collection_interval: Mapped[int] = mapped_column(Integer, default=3600)
    max_items_per_run: Mapped[int] = mapped_column(Integer, default=100)
    retry_count: Mapped[int] = mapped_column(Integer, default=3)
    timeout: Mapped[int] = mapped_column(Integer, default=30)

    collection_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)

    last_collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关系
    raw_documents: Mapped[list["RawDocument"]] = relationship(
        "RawDocument", back_populates="source", cascade="all, delete-orphan"
    )
    collection_tasks: Mapped[list["CollectionTask"]] = relationship(
        "CollectionTask", back_populates="source", cascade="all, delete-orphan"
    )

    @property
    def tags_list(self) -> List[str]:
        """获取标签列表"""
        if self.tags:
            return self.tags.split(",")
        return []

    def __repr__(self) -> str:
        return f"<DataSource(id={self.id}, name='{self.name}', type='{self.source_type}')>"


class RawDocument(Base):
    """原始文档表"""
    __tablename__ = "raw_documents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[str] = mapped_column(String(255), ForeignKey("data_sources.name", ondelete="CASCADE"), nullable=False, index=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    document_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    raw_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # SQLite兼容

    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    collector_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    processing_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0)

    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关系
    source: Mapped["DataSource"] = relationship("DataSource", back_populates="raw_documents")
    processed_documents: Mapped[list["ProcessedDocument"]] = relationship(
        "ProcessedDocument", back_populates="raw_document", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<RawDocument(id={self.id}, source_id='{self.source_id}', title='{self.title}')>"


class ProcessedDocument(Base):
    """处理后文档表"""
    __tablename__ = "processed_documents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    raw_document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("raw_documents.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    structured_content: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # SQLite兼容
    extracted_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # SQLite兼容
    entities: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # SQLite兼容

    keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 逗号分隔的关键词，SQLite兼容
    categories: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 逗号分隔的分类，SQLite兼容

    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processor_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    processing_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    similarity_group_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)
    similarity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    relevance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 关系
    raw_document: Mapped["RawDocument"] = relationship("RawDocument", back_populates="processed_documents")

    def __repr__(self) -> str:
        return f"<ProcessedDocument(id={self.id}, title='{self.title}')>"


class CollectionTask(Base):
    """采集任务表"""
    __tablename__ = "collection_tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(255), ForeignKey("data_sources.name", ondelete="CASCADE"), nullable=False, index=True)

    task_type: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    items_collected: Mapped[int] = mapped_column(Integer, default=0)
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 关系
    source: Mapped["DataSource"] = relationship("DataSource", back_populates="collection_tasks")

    def __repr__(self) -> str:
        return f"<CollectionTask(id={self.id}, source_id='{self.source_id}', status='{self.status}')>"


# =============================================================================
# 认证和授权模型 (TASK-004)
# =============================================================================

class User(Base):
    """
    用户表

    用于系统用户认证，支持单用户和多用户模式。
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # 关系
    api_keys: Mapped[list["APIKey"]] = relationship(
        "APIKey",
        back_populates="user",
        cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        "AuditLog",
        back_populates="user",
        cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username='{self.username}', is_active={self.is_active})>"


class APIKey(Base):
    """
    API Key表

    用于API认证，支持多API Key管理。
    """
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    key_prefix: Mapped[str] = mapped_column(String(10), nullable=False)  # 用于显示 (e.g., "atk_xxx")
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)   # SHA-256哈希
    name: Mapped[str] = mapped_column(String(255), nullable=False)       # Key名称

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    # 权限范围 (SQLite兼容，使用JSON存储为字符串)
    scopes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 关系
    user: Mapped["User"] = relationship("User", back_populates="api_keys")

    @property
    def scopes_list(self) -> List[str]:
        """获取权限范围列表"""
        if self.scopes:
            return self.scopes.split(",")
        return []

    def __repr__(self) -> str:
        return f"<APIKey(id={self.id}, key_prefix='{self.key_prefix}', name='{self.name}', is_active={self.is_active})>"


class AuditLog(Base):
    """
    审计日志表

    记录所有用户操作，用于安全审计和问题排查。
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # 动作类型
    resource_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # 资源类型
    resource_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)    # 资源ID
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)              # 详细信息

    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)     # IPv6兼容
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="success")  # success, failure
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    # 关系
    user: Mapped[Optional["User"]] = relationship("User", back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, action='{self.action}', status='{self.status}')>"
