"""
统一API响应格式

提供标准化的API响应格式，包括：
- 成功响应
- 错误响应
- 分页响应
"""

from typing import Generic, TypeVar, Optional, Any
from datetime import datetime
from pydantic import BaseModel, Field


T = TypeVar('T')


class APIResponse(BaseModel, Generic[T]):
    """
    统一API响应格式

    Args:
        success: 请求是否成功
        message: 响应消息
        data: 响应数据
        error: 错误信息
        timestamp: 响应时间戳
    """

    success: bool = Field(description="请求是否成功")
    message: Optional[str] = Field(None, description="响应消息")
    data: Optional[T] = Field(None, description="响应数据")
    error: Optional[str] = Field(None, description="错误信息")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="响应时间戳"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "success": True,
                    "message": "操作成功",
                    "data": {"id": 1, "name": "示例"},
                    "error": None,
                    "timestamp": "2025-12-28T12:00:00"
                }
            ]
        }
    }


class PaginatedResponse(BaseModel, Generic[T]):
    """
    分页响应格式

    Args:
        success: 请求是否成功
        data: 分页数据
        total: 总记录数
        page: 当前页码
        page_size: 每页大小
        pages: 总页数
        message: 响应消息
        timestamp: 响应时间戳
    """

    success: bool = Field(description="请求是否成功")
    data: list[T] = Field(description="分页数据列表")
    total: int = Field(description="总记录数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页大小")
    pages: int = Field(description="总页数")
    message: Optional[str] = Field(None, description="响应消息")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="响应时间戳"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "success": True,
                    "data": [{"id": 1}, {"id": 2}],
                    "total": 100,
                    "page": 1,
                    "page_size": 20,
                    "pages": 5,
                    "message": "查询成功",
                    "timestamp": "2025-12-28T12:00:00"
                }
            ]
        }
    }


def success_response(
    data: Any = None,
    message: str = "操作成功"
) -> APIResponse:
    """
    创建成功响应

    Args:
        data: 响应数据
        message: 响应消息

    Returns:
        APIResponse: 成功响应对象
    """
    return APIResponse(success=True, data=data, message=message)


def error_response(
    error: str,
    message: str = "操作失败"
) -> APIResponse:
    """
    创建错误响应

    Args:
        error: 错误信息
        message: 响应消息

    Returns:
        APIResponse: 错误响应对象
    """
    return APIResponse(success=False, error=error, message=message)


def paginated_response(
    data: list[Any],
    total: int,
    page: int,
    page_size: int,
    message: str = "查询成功"
) -> PaginatedResponse:
    """
    创建分页响应

    Args:
        data: 数据列表
        total: 总记录数
        page: 当前页码
        page_size: 每页大小
        message: 响应消息

    Returns:
        PaginatedResponse: 分页响应对象
    """
    pages = (total + page_size - 1) // page_size if page_size > 0 else 0

    return PaginatedResponse(
        success=True,
        data=data,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
        message=message
    )
