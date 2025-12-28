"""
认证相关的Pydantic模型

提供API请求和响应的数据验证：
- 用户登录
- JWT令牌响应
- API Key管理
"""

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field, EmailStr, field_validator


# =============================================================================
# 请求模型
# =============================================================================

class LoginRequest(BaseModel):
    """用户登录请求"""
    username: str = Field(..., min_length=1, max_length=255, description="用户名")
    password: str = Field(..., min_length=1, description="密码")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "username": "admin",
                    "password": "admin123"
                }
            ]
        }
    }


class APIKeyCreateRequest(BaseModel):
    """创建API Key请求"""
    name: str = Field(..., min_length=1, max_length=255, description="API Key名称")
    scopes: Optional[List[str]] = Field(
        default=["read", "write"],
        description="权限范围"
    )
    expires_days: Optional[int] = Field(
        None,
        ge=1,
        le=365,
        description="有效期（天），不设置则永不过期"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "开发测试Key",
                    "scopes": ["read", "write"],
                    "expires_days": 90
                }
            ]
        }
    }


# =============================================================================
# 响应模型
# =============================================================================

class TokenResponse(BaseModel):
    """JWT令牌响应"""
    access_token: str = Field(description="访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    expires_in: int = Field(description="过期时间（秒）")
    user: "UserResponse" = Field(description="用户信息")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "token_type": "bearer",
                    "expires_in": 86400,
                    "user": {
                        "id": 1,
                        "username": "admin",
                        "email": "admin@atlas.local",
                        "is_active": True,
                        "is_superuser": True
                    }
                }
            ]
        }
    }


class UserResponse(BaseModel):
    """用户信息响应"""
    id: int = Field(description="用户ID")
    username: str = Field(description="用户名")
    email: Optional[str] = Field(None, description="邮箱")
    is_active: bool = Field(description="是否激活")
    is_superuser: bool = Field(description="是否超级用户")
    created_at: datetime = Field(description="创建时间")
    last_login_at: Optional[datetime] = Field(None, description="最后登录时间")

    model_config = {
        "from_attributes": True  # Pydantic v2
    }


class APIKeyResponse(BaseModel):
    """API Key响应"""
    id: int = Field(description="API Key ID")
    key_prefix: str = Field(description="API Key前缀（用于显示）")
    key: str = Field(description="完整的API Key（仅在创建时返回）")
    name: str = Field(description="API Key名称")
    is_active: bool = Field(description="是否激活")
    scopes: List[str] = Field(description="权限范围")
    expires_at: Optional[datetime] = Field(None, description="过期时间")
    last_used_at: Optional[datetime] = Field(None, description="最后使用时间")
    created_at: datetime = Field(description="创建时间")

    model_config = {
        "from_attributes": True
    }


class APIKeyListItem(BaseModel):
    """API Key列表项（不包含完整key）"""
    id: int = Field(description="API Key ID")
    key_prefix: str = Field(description="API Key前缀")
    name: str = Field(description="API Key名称")
    is_active: bool = Field(description="是否激活")
    scopes: List[str] = Field(description="权限范围")
    expires_at: Optional[datetime] = Field(None, description="过期时间")
    last_used_at: Optional[datetime] = Field(None, description="最后使用时间")
    created_at: datetime = Field(description="创建时间")

    model_config = {
        "from_attributes": True
    }


# 更新前向引用
TokenResponse.model_rebuild()
