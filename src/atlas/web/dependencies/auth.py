"""
认证依赖模块

提供FastAPI依赖注入的认证功能：
- JWT令牌认证
- API Key认证
- 用户获取
- 权限检查
"""

from typing import Optional
from datetime import datetime
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from atlas.models.schema import User, APIKey
from atlas.core.database_async import get_async_db_manager
from atlas.web.core.security import decode_access_token, verify_api_key
from atlas.web.config.settings import get_security_settings


security_settings = get_security_settings()


# HTTP Bearer认证方案（用于JWT）
bearer_security = HTTPBearer(auto_error=False)


async def get_async_db():
    """
    获取异步数据库会话

    Returns:
        AsyncSession: 数据库会话
    """
    db_manager = await get_async_db_manager()
    async with db_manager.get_session() as session:
        yield session


async def get_current_user_jwt(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_security),
    db: AsyncSession = Depends(get_async_db)
) -> User:
    """
    通过JWT令牌获取当前用户

    Args:
        credentials: HTTP Bearer凭证
        db: 数据库会话

    Returns:
        User: 当前用户

    Raises:
        HTTPException: 认证失败
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username: str = payload.get("sub")
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 从数据库获取用户
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用"
        )

    return user


async def get_current_user_api_key(
    x_api_key: Optional[str] = Depends(None),
    db: AsyncSession = Depends(get_async_db)
) -> User:
    """
    通过API Key获取当前用户

    Args:
        x_api_key: API Key (从header获取)
        db: 数据库会话

    Returns:
        User: 当前用户

    Raises:
        HTTPException: 认证失败
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供API Key"
        )

    # 哈希API Key
    from atlas.web.core.security import hash_api_key
    key_hash = hash_api_key(x_api_key)

    # 查询API Key
    result = await db.execute(
        select(APIKey, User)
        .join(User, APIKey.user_id == User.id)
        .where(APIKey.key_hash == key_hash)
        .where(APIKey.is_active == True)
    )
    row = result.first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的API Key"
        )

    api_key, user = row

    # 检查API Key是否过期
    if api_key.expires_at and datetime.utcnow() > api_key.expires_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key已过期"
        )

    # 更新最后使用时间
    await db.execute(
        update(APIKey)
        .where(APIKey.id == api_key.id)
        .values(last_used_at=datetime.utcnow())
    )
    await db.commit()

    return user


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_async_db)
) -> User:
    """
    获取当前用户（支持JWT和API Key两种认证方式）

    优先级：
    1. Authorization header (JWT)
    2. X-API-Key header (API Key)

    Args:
        request: FastAPI请求对象
        db: 数据库会话

    Returns:
        User: 当前用户

    Raises:
        HTTPException: 认证失败
    """
    # 尝试JWT认证
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        payload = decode_access_token(token)

        if payload:
            username: str = payload.get("sub")
            if username:
                result = await db.execute(select(User).where(User.username == username))
                user = result.scalar_one_or_none()

                if user and user.is_active:
                    return user

    # 尝试API Key认证
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return await get_current_user_api_key(api_key, db)

    # 两种认证方式都失败
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="需要提供认证凭证（JWT或API Key）",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    获取当前活跃用户

    Args:
        current_user: 当前用户

    Returns:
        User: 活跃用户

    Raises:
        HTTPException: 用户未激活
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用"
        )
    return current_user


def require_superuser(*, require_superuser: bool = True):
    """
    超级用户权限检查装饰器工厂

    Args:
        require_superuser: 是否需要超级用户权限

    Returns:
        依赖函数
    """
    async def superuser_check(
        current_user: User = Depends(get_current_active_user)
    ) -> User:
        if require_superuser and not current_user.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="需要超级用户权限"
            )
        return current_user

    return superuser_check
