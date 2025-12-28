"""
认证API路由

提供用户认证和授权相关的API端点：
- POST /api/v1/auth/login       - 用户登录
- POST /api/v1/auth/refresh     - 刷新令牌
- GET  /api/v1/auth/me          - 获取当前用户信息
- POST /api/v1/auth/apikey      - 创建API Key
- GET  /api/v1/auth/apikeys     - API Key列表
- DELETE /api/v1/auth/apikeys/{id} - 撤销API Key
"""

from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.models.schema import User, APIKey, AuditLog
from atlas.web.dependencies.auth import get_current_active_user, get_async_db
from atlas.web.core.security import (
    verify_password,
    create_access_token,
    generate_api_key,
    hash_api_key
)
from atlas.web.config.settings import get_security_settings
from atlas.web.utils.response import success_response
from atlas.web.schemas.auth import (
    LoginRequest,
    TokenResponse,
    UserResponse,
    APIKeyCreateRequest,
    APIKeyResponse,
    APIKeyListItem
)


router = APIRouter(prefix="/auth", tags=["认证授权"])
security_settings = get_security_settings()


# =============================================================================
# 辅助函数
# =============================================================================

async def create_audit_log(
    db: AsyncSession,
    user_id: Optional[int],
    action: str,
    request: Request,
    status: str = "success",
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[dict] = None
):
    """
    创建审计日志

    Args:
        db: 数据库会话
        user_id: 用户ID
        action: 动作类型
        request: FastAPI请求对象
        status: 状态
        resource_type: 资源类型
        resource_id: 资源ID
        details: 详细信息
    """
    log = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        status=status
    )
    db.add(log)


# =============================================================================
# 认证端点
# =============================================================================

@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_db)
):
    """
    用户登录

    验证用户名和密码，返回JWT访问令牌。

    Args:
        credentials: 登录凭证
        request: FastAPI请求对象
        db: 数据库会话

    Returns:
        TokenResponse: JWT令牌和用户信息

    Raises:
        HTTPException: 登录失败
    """
    # 查询用户
    result = await db.execute(
        select(User).where(User.username == credentials.username)
    )
    user = result.scalar_one_or_none()

    # 验证用户存在性和密码
    if not user or not verify_password(credentials.password, user.password_hash):
        # 记录失败的登录尝试
        await create_audit_log(
            db=db,
            user_id=None,
            action="login",
            request=request,
            status="failure",
            details={"username": credentials.username}
        )
        await db.commit()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )

    # 检查用户是否激活
    if not user.is_active:
        await create_audit_log(
            db=db,
            user_id=user.id,
            action="login",
            request=request,
            status="failure",
            details={"reason": "user_disabled"}
        )
        await db.commit()

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用"
        )

    # 创建JWT令牌
    access_token = create_access_token(
        data={"sub": user.username},
        expires_delta=timedelta(minutes=security_settings.access_token_expire_minutes)
    )

    # 更新最后登录时间
    from sqlalchemy import update
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(last_login_at=datetime.utcnow())
    )

    # 记录成功的登录
    await create_audit_log(
        db=db,
        user_id=user.id,
        action="login",
        request=request,
        status="success"
    )
    await db.commit()

    # 刷新用户对象
    await db.refresh(user)

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=security_settings.access_token_expire_minutes * 60,
        user=UserResponse.model_validate(user)
    )


@router.post("/refresh")
async def refresh_token(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    刷新JWT令牌

    Args:
        current_user: 当前用户
        db: 数据库会话

    Returns:
        TokenResponse: 新的JWT令牌
    """
    # 创建新的JWT令牌
    access_token = create_access_token(
        data={"sub": current_user.username},
        expires_delta=timedelta(minutes=security_settings.access_token_expire_minutes)
    )

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=security_settings.access_token_expire_minutes * 60,
        user=UserResponse.model_validate(current_user)
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user)
):
    """
    获取当前用户信息

    Args:
        current_user: 当前用户

    Returns:
        UserResponse: 用户信息
    """
    return UserResponse.model_validate(current_user)


# =============================================================================
# API Key管理
# =============================================================================

@router.post("/apikey", response_model=APIKeyResponse)
async def create_api_key(
    key_data: APIKeyCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    创建API Key

    Args:
        key_data: API Key创建请求
        request: FastAPI请求对象
        current_user: 当前用户
        db: 数据库会话

    Returns:
        APIKeyResponse: 创建的API Key信息
    """
    # 生成API Key
    api_key, key_prefix = generate_api_key()
    key_hash = hash_api_key(api_key)

    # 计算过期时间
    expires_at = None
    if key_data.expires_days:
        expires_at = datetime.utcnow() + timedelta(days=key_data.expires_days)

    # 创建API Key记录
    scopes_json = ",".join(key_data.scopes or ["read", "write"])
    api_key_record = APIKey(
        user_id=current_user.id,
        key_prefix=key_prefix,
        key_hash=key_hash,
        name=key_data.name,
        scopes=scopes_json,
        expires_at=expires_at
    )

    db.add(api_key_record)
    await db.commit()
    await db.refresh(api_key_record)

    # 记录审计日志
    await create_audit_log(
        db=db,
        user_id=current_user.id,
        action="create_api_key",
        request=request,
        status="success",
        resource_type="api_key",
        resource_id=str(api_key_record.id),
        details={"name": key_data.name}
    )
    await db.commit()

    # 返回响应（包含完整API Key，仅此一次）
    return APIKeyResponse(
        id=api_key_record.id,
        key_prefix=api_key_record.key_prefix,
        key=api_key,  # 仅在创建时返回完整key
        name=api_key_record.name,
        is_active=api_key_record.is_active,
        scopes=api_key_record.scopes_list,
        expires_at=api_key_record.expires_at,
        last_used_at=api_key_record.last_used_at,
        created_at=api_key_record.created_at
    )


@router.get("/apikeys", response_model=List[APIKeyListItem])
async def list_api_keys(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取API Key列表

    Args:
        current_user: 当前用户
        db: 数据库会话

    Returns:
        List[APIKeyListItem]: API Key列表（不包含完整key）
    """
    result = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == current_user.id)
        .order_by(APIKey.created_at.desc())
    )
    api_keys = result.scalars().all()

    return [
        APIKeyListItem(
            id=key.id,
            key_prefix=key.key_prefix,
            name=key.name,
            is_active=key.is_active,
            scopes=key.scopes_list,
            expires_at=key.expires_at,
            last_used_at=key.last_used_at,
            created_at=key.created_at
        )
        for key in api_keys
    ]


@router.delete("/apikeys/{api_key_id}")
async def revoke_api_key(
    api_key_id: int,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    撤销API Key

    Args:
        api_key_id: API Key ID
        request: FastAPI请求对象
        current_user: 当前用户
        db: 数据库会话

    Returns:
        APIResponse: 操作结果
    """
    # 查询API Key
    result = await db.execute(
        select(APIKey)
        .where(APIKey.id == api_key_id)
        .where(APIKey.user_id == current_user.id)
    )
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API Key不存在"
        )

    # 标记为不活跃（软删除）
    api_key.is_active = False

    # 记录审计日志
    await create_audit_log(
        db=db,
        user_id=current_user.id,
        action="revoke_api_key",
        request=request,
        status="success",
        resource_type="api_key",
        resource_id=str(api_key_id),
        details={"name": api_key.name}
    )

    await db.commit()

    return success_response(
        data={"id": api_key_id},
        message="API Key已撤销"
    )
