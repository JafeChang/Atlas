"""
错误处理中间件

提供统一的错误处理和格式化：
- 捕获所有异常
- 格式化错误响应
- 记录错误日志
"""

from typing import Any
from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging

logger = logging.getLogger(__name__)


class ErrorResponse:
    """错误响应模型"""

    @staticmethod
    def create(
        status_code: int,
        error: str,
        detail: Any = None,
        path: str = None
    ) -> JSONResponse:
        """
        创建标准化错误响应

        Args:
            status_code: HTTP状态码
            error: 错误类型
            detail: 错误详情
            path: 请求路径

        Returns:
            JSONResponse: 错误响应
        """
        content = {
            "success": False,
            "error": error,
            "detail": str(detail) if detail else None,
            "path": path,
            "status_code": status_code
        }

        return JSONResponse(status_code=status_code, content=content)


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException
) -> JSONResponse:
    """
    处理HTTP异常

    Args:
        request: 请求对象
        exc: HTTP异常

    Returns:
        JSONResponse: 错误响应
    """
    logger.error(
        f"HTTP异常: {exc.status_code} - {exc.detail} - {request.url.path}"
    )

    return ErrorResponse.create(
        status_code=exc.status_code,
        error="HTTP_ERROR",
        detail=exc.detail,
        path=request.url.path
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError
) -> JSONResponse:
    """
    处理请求验证异常

    Args:
        request: 请求对象
        exc: 验证异常

    Returns:
        JSONResponse: 错误响应
    """
    errors = []
    for error in exc.errors():
        errors.append({
            "field": ".".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
            "type": error["type"]
        })

    logger.error(
        f"验证失败: {request.url.path} - {errors}"
    )

    return ErrorResponse.create(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="VALIDATION_ERROR",
        detail=errors,
        path=request.url.path
    )


async def general_exception_handler(
    request: Request,
    exc: Exception
) -> JSONResponse:
    """
    处理所有未捕获的异常

    Args:
        request: 请求对象
        exc: 异常对象

    Returns:
        JSONResponse: 错误响应
    """
    logger.exception(
        f"未捕获的异常: {type(exc).__name__} - {str(exc)} - {request.url.path}"
    )

    return ErrorResponse.create(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error="INTERNAL_SERVER_ERROR",
        detail=str(exc) if logger.isEnabledFor(logging.DEBUG) else "服务器内部错误",
        path=request.url.path
    )
