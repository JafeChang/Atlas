"""
FastAPI应用入口

Atlas个人信息聚合系统的FastAPI异步API应用
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging

from .config.settings import (
    get_fastapi_settings,
    get_security_settings,
    get_cors_settings
)
from .middleware.error_handler import (
    http_exception_handler,
    validation_exception_handler,
    general_exception_handler
)
from .utils.response import success_response
from .api.v1 import auth as auth_api
from .api.v1 import sources as sources_api
from .api.v1 import collections as collections_api


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    启动时初始化资源，关闭时清理资源
    """
    # 启动
    logger.info("Atlas FastAPI应用启动中...")
    logger.info(f"版本: {get_fastapi_settings().app_version}")

    # 这里将来可以添加：
    # - 初始化数据库连接池
    # - 初始化Redis连接
    # - 健康检查

    yield

    # 关闭
    logger.info("Atlas FastAPI应用关闭中...")


# 创建FastAPI应用实例
settings = get_fastapi_settings()
security_settings = get_security_settings()
cors_settings = get_cors_settings()

app = FastAPI(
    title=settings.app_name,
    description=settings.app_description,
    version=settings.app_version,
    docs_url=settings.docs_url,
    redoc_url=settings.redoc_url,
    openapi_url=settings.openapi_url,
    lifespan=lifespan,
)


# =============================================================================
# 中间件配置
# =============================================================================

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_settings.origins,
    allow_credentials=cors_settings.allow_credentials,
    allow_methods=cors_settings.allow_methods,
    allow_headers=cors_settings.allow_headers,
    expose_headers=cors_settings.expose_headers,
)


# =============================================================================
# 异常处理器
# =============================================================================

app.add_exception_handler(
    StarletteHTTPException,
    http_exception_handler
)

app.add_exception_handler(
    RequestValidationError,
    validation_exception_handler
)

app.add_exception_handler(
    Exception,
    general_exception_handler
)


# =============================================================================
# 静态文件和模板
# =============================================================================

# 挂载静态文件目录
try:
    app.mount("/static", StaticFiles(directory="src/atlas/web/static"), name="static")
    logger.info("静态文件目录已挂载: /static")
except Exception as e:
    logger.warning(f"静态文件目录挂载失败: {e}")

# 配置Jinja2模板
try:
    templates = Jinja2Templates(directory="src/atlas/web/templates")
    logger.info("Jinja2模板已配置")
except Exception as e:
    logger.warning(f"Jinja2模板配置失败: {e}")
    templates = None


# =============================================================================
# 基础路由
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """
    主页 - 管理后台界面
    """
    if templates:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "app_name": settings.app_name}
        )
    else:
        return HTMLResponse("""
        <html>
            <head><title>Atlas API</title></head>
            <body>
                <h1>Atlas API</h1>
                <p>版本: {version}</p>
                <p>文档: <a href="{docs_url}">Swagger UI</a></p>
                <p>状态: <a href="/health">健康检查</a></p>
            </body>
        </html>
        """.format(
            version=settings.app_version,
            docs_url=settings.docs_url
        ))


@app.get("/health")
async def health_check():
    """
    健康检查端点

    Returns:
        JSONResponse: 健康状态
    """
    return success_response(
        data={
            "status": "healthy",
            "version": settings.app_version,
            "service": "atlas-api"
        },
        message="服务运行正常"
    )


@app.get("/api/v1/overview")
async def overview():
    """
    API概览

    Returns:
        APIResponse: API基本信息
    """
    return success_response(
        data={
            "name": settings.app_name,
            "version": settings.app_version,
            "description": settings.app_description,
            "docs_url": settings.docs_url,
            "redoc_url": settings.redoc_url,
            "endpoints": {
                "health": "/health",
                "overview": "/api/v1/overview",
                "docs": settings.docs_url,
            }
        },
        message="Atlas API v2.0.0"
    )


# =============================================================================
# API路由（后续添加）
# =============================================================================

# 认证API
app.include_router(auth_api.router, prefix="/api/v1")

# 数据源API
app.include_router(sources_api.router, prefix="/api/v1")

# 采集任务API
app.include_router(collections_api.router, prefix="/api/v1")

# 这里将在后续阶段添加：
# - /api/v1/documents - 文档管理
# - /api/v1/storage - 存储管理
# - /api/v1/scheduler - 调度器管理
# - /api/v1/system - 系统监控


# =============================================================================
# 启动说明
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "fastapi_app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        log_level=settings.log_level,
    )
