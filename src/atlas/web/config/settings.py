"""
FastAPI配置管理模块

该模块负责管理FastAPI应用的所有配置，包括：
- 应用基础配置
- CORS配置
- JWT认证配置
- API文档配置
"""

from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class FastAPISettings(BaseSettings):
    """FastAPI应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ATLAS_FASTAPI_",
        extra="ignore",
    )

    # 应用配置
    app_name: str = "Atlas API"
    app_version: str = "2.0.0"
    app_description: str = "Atlas 个人信息聚合系统 API"
    debug: bool = False

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    workers: int = 1

    # API文档配置
    docs_url: str = "/api/docs"
    redoc_url: str = "/api/redoc"
    openapi_url: str = "/api/openapi.json"

    # 日志配置
    log_level: str = "info"

    # 请求限制
    max_upload_size: int = 100 * 1024 * 1024  # 100MB


class SecuritySettings(BaseSettings):
    """安全配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ATLAS_",
        extra="ignore",
    )

    # JWT配置
    secret_key: str = "your-secret-key-please-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24小时

    # API Key配置
    api_key_expire_days: int = 90

    # 密码配置
    default_username: str = "admin"
    default_password: str = "admin123"
    bcrypt_rounds: int = 12

    # 速率限制
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60


class CorsSettings(BaseSettings):
    """CORS配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ATLAS_CORS_",
        extra="ignore",
    )

    # CORS配置
    origins: List[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
    ]
    allow_credentials: bool = True
    allow_methods: List[str] = ["*"]
    allow_headers: List[str] = ["*"]

    # 暴露的响应头
    expose_headers: List[str] = [
        "X-Total-Count",
        "X-Page",
        "X-Page-Size",
    ]


@lru_cache
def get_fastapi_settings() -> FastAPISettings:
    """获取FastAPI配置（缓存）"""
    return FastAPISettings()


@lru_cache
def get_security_settings() -> SecuritySettings:
    """获取安全配置（缓存）"""
    return SecuritySettings()


@lru_cache
def get_cors_settings() -> CorsSettings:
    """获取CORS配置（缓存）"""
    return CorsSettings()
