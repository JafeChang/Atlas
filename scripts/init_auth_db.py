#!/usr/bin/env python3
"""
初始化认证数据库

创建默认用户和管理员账户
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到path
sys.path.insert(0, str(Path(__file__).parent.parent))

from atlas.core.database_async import get_async_db_manager
from atlas.models.schema import User
from atlas.web.core.security import get_password_hash, create_default_user_password_hash
from atlas.web.config.settings import get_security_settings


async def create_default_user():
    """
    创建默认用户

    Returns:
        User: 创建的默认用户
    """
    db_manager = await get_async_db_manager()
    security_settings = get_security_settings()

    async with db_manager.get_session() as session:
        # 检查是否已存在用户
        from sqlalchemy import select
        result = await session.execute(
            select(User).where(User.username == security_settings.default_username)
        )
        existing_user = result.scalar_one_or_none()

        if existing_user:
            print(f"✓ 默认用户已存在: {existing_user.username}")
            return existing_user

        # 创建默认用户
        password_hash = get_password_hash(security_settings.default_password)

        user = User(
            username=security_settings.default_username,
            password_hash=password_hash,
            email="admin@atlas.local",
            is_active=True,
            is_superuser=True
        )

        session.add(user)
        await session.commit()
        await session.refresh(user)

        print(f"✓ 默认用户创建成功")
        print(f"  用户名: {user.username}")
        print(f"  密码: {security_settings.default_password}")
        print(f"  邮箱: {user.email}")
        print(f"  超级用户: {user.is_superuser}")
        print()
        print("⚠️  请在生产环境中立即修改默认密码！")

        return user


async def init_database():
    """
    初始化认证数据库

    创建所有必要的表和默认数据
    """
    print("=" * 60)
    print("Atlas 认证数据库初始化")
    print("=" * 60)
    print()

    # 获取数据库管理器
    db_manager = await get_async_db_manager()

    # 创建所有表
    print("正在创建数据库表...")
    await db_manager.create_tables()
    print("✓ 数据库表创建完成")
    print()

    # 创建默认用户
    print("正在创建默认用户...")
    await create_default_user()
    print()

    print("=" * 60)
    print("认证数据库初始化完成！")
    print("=" * 60)
    print()
    print("你可以使用以下凭据登录:")
    print(f"  用户名: {get_security_settings().default_username}")
    print(f"  密码: {get_security_settings().default_password}")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(init_database())
    except Exception as e:
        print(f"✗ 初始化失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
