"""
核心安全模块

提供JWT令牌管理和密码加密功能：
- JWT令牌创建和验证
- 密码哈希和验证（bcrypt）
- API Key生成和验证
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from jose import JWTError, jwt
import bcrypt
import secrets
import hashlib

from atlas.web.config.settings import get_security_settings


# 获取安全配置
security_settings = get_security_settings()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证密码

    Args:
        plain_password: 明文密码
        hashed_password: 哈希后的密码

    Returns:
        bool: 密码是否匹配
    """
    # bcrypt限制密码最长72字节
    password_bytes = plain_password.encode('utf-8')[:72]
    return bcrypt.checkpw(password_bytes, hashed_password.encode('utf-8'))


def get_password_hash(password: str) -> str:
    """
    对密码进行哈希

    Args:
        password: 明文密码

    Returns:
        str: 哈希后的密码
    """
    # bcrypt限制密码最长72字节
    password_bytes = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None
) -> str:
    """
    创建JWT访问令牌

    Args:
        data: 要编码的数据（通常包含username等用户信息）
        expires_delta: 过期时间增量

    Returns:
        str: JWT令牌
    """
    to_encode = data.copy()

    # 设置过期时间
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=security_settings.access_token_expire_minutes
        )

    to_encode.update({"exp": expire})

    # 编码JWT
    encoded_jwt = jwt.encode(
        to_encode,
        security_settings.secret_key,
        algorithm=security_settings.algorithm
    )

    return encoded_jwt


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """
    解码JWT访问令牌

    Args:
        token: JWT令牌

    Returns:
        Optional[Dict[str, Any]]: 解码后的数据，失败返回None
    """
    try:
        payload = jwt.decode(
            token,
            security_settings.secret_key,
            algorithms=[security_settings.algorithm]
        )
        return payload
    except JWTError:
        return None


def generate_api_key() -> tuple[str, str]:
    """
    生成API Key

    Returns:
        tuple[str, str]: (api_key, key_prefix)
            - api_key: 完整的API Key (格式: atk_xxxxxxxxx...)
            - key_prefix: 用于显示的前缀 (格式: atk_xxx)
    """
    # 生成32字节随机数据
    random_bytes = secrets.token_bytes(32)

    # 转换为十六进制字符串
    api_key = f"atk_{random_bytes.hex()}"

    # 生成前缀（用于显示）
    key_prefix = f"atk_{random_bytes[:6].hex()}"

    return api_key, key_prefix


def hash_api_key(api_key: str) -> str:
    """
    对API Key进行哈希（用于存储）

    Args:
        api_key: 原始API Key

    Returns:
        str: 哈希后的API Key
    """
    # 使用SHA-256哈希
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(api_key: str, hashed_key: str) -> bool:
    """
    验证API Key

    Args:
        api_key: 原始API Key
        hashed_key: 存储的哈希值

    Returns:
        bool: 是否匹配
    """
    return hash_api_key(api_key) == hashed_key


def create_default_user_password_hash() -> str:
    """
    创建默认用户的密码哈希

    Returns:
        str: 默认密码的哈希值
    """
    return get_password_hash(security_settings.default_password)
