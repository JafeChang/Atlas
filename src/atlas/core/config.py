"""
Atlas 配置管理模块

提供统一的配置管理功能，支持多种配置源：
1. 系统环境变量
2. 本地 .env 文件
3. 项目级 .env 文件 (支持多环境)
4. 配置文件 (YAML)
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional, Union, List
from dotenv import load_dotenv
from pydantic import BaseSettings, Field, validator


class DatabaseConfig(BaseSettings):
    """数据库配置"""

    url: str = Field(default="sqlite:///data/atlas.db", description="数据库连接URL")
    pool_size: int = Field(default=5, description="连接池大小")
    max_overflow: int = Field(default=10, description="最大溢出连接数")
    echo: bool = Field(default=False, description="是否打印SQL语句")

    class Config:
        env_prefix = "ATLAS_DATABASE_"


class CollectionConfig(BaseSettings):
    """数据采集配置"""

    default_user_agent: str = Field(
        default="Atlas/0.1.0 (Information Aggregation System; +https://github.com/your-username/atlas)",
        description="默认HTTP请求User-Agent"
    )
    request_timeout: int = Field(default=30, description="请求超时时间(秒)")
    max_concurrent_requests: int = Field(default=3, description="最大并发请求数")
    rate_limit_delay: int = Field(default=300, description="访问频率限制(秒)")
    use_random_user_agent: bool = Field(default=False, description="是否使用随机User-Agent")
    rotate_user_agent: bool = Field(default=False, description="是否轮换User-Agent")

    class Config:
        env_prefix = "ATLAS_"


class LLMConfig(BaseSettings):
    """LLM配置"""

    provider: str = Field(default="local", description="LLM提供商: local, openai, anthropic")
    model: str = Field(default="qwen2.5:7b", description="模型名称")
    base_url: str = Field(default="http://localhost:11434", description="API基础URL")
    timeout: int = Field(default=60, description="请求超时时间(秒)")
    max_workers: int = Field(default=1, description="最大工作线程数")

    # OpenAI 配置
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI API密钥")
    openai_base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI API URL")
    openai_model: str = Field(default="gpt-4", description="OpenAI模型名称")

    # Anthropic 配置
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic API密钥")
    anthropic_model: str = Field(default="claude-3-sonnet-20240229", description="Anthropic模型名称")

    @validator('provider')
    def validate_provider(cls, v):
        """验证LLM提供商"""
        if v not in ['local', 'openai', 'anthropic']:
            raise ValueError(f"不支持的LLM提供商: {v}")
        return v

    class Config:
        env_prefix = "ATLAS_LLM_"


class SchedulerConfig(BaseSettings):
    """调度器配置"""

    enabled: bool = Field(default=True, description="是否启用调度器")
    timezone: str = Field(default="UTC", description="时区")
    max_workers: int = Field(default=3, description="最大工作线程数")

    class Config:
        env_prefix = "ATLAS_SCHEDULER_"


class MonitoringConfig(BaseSettings):
    """监控配置"""

    enabled: bool = Field(default=True, description="是否启用监控")
    metrics_port: int = Field(default=8080, description="监控端口")
    health_check_interval: int = Field(default=60, description="健康检查间隔(秒)")

    class Config:
        env_prefix = "ATLAS_MONITORING_"


class LocalEnvLoader:
    """本地环境变量加载器"""

    def __init__(self, config_dir: Path, env_name: Optional[str] = None):
        """初始化环境变量加载器

        Args:
            config_dir: 配置文件目录
            env_name: 环境名称 (development, production, test等)
        """
        self.config_dir = config_dir
        self.env_name = env_name or self._detect_env()
        self._loaded_envs = set()

    def _detect_env(self) -> str:
        """自动检测环境类型"""
        # 优先从系统环境变量读取
        env = os.getenv("ATLAS_ENV")
        if env:
            return env

        # 从项目根目录的 .env 文件读取
        root_env_file = Path(".env")
        if root_env_file.exists():
            try:
                with open(root_env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ATLAS_ENV="):
                            return line.split("=", 1)[1].strip().strip('"\'')
            except Exception:
                pass

        # 默认为开发环境
        return "development"

    def load_env_files(self) -> None:
        """按优先级加载环境变量文件"""
        env_files = self._get_env_file_paths()

        for env_file in env_files:
            if env_file.exists() and str(env_file) not in self._loaded_envs:
                load_dotenv(env_file, override=True)
                self._loaded_envs.add(str(env_file))

    def _get_env_file_paths(self) -> List[Path]:
        """获取环境变量文件路径列表（按优先级排序）"""
        env_files = []

        # 1. 系统环境变量 (已在os.environ中，不需要加载)

        # 2. 项目根目录的 .env 文件 (全局默认配置)
        root_env = Path(".env")
        if root_env.exists():
            env_files.append(root_env)

        # 3. 配置目录的环境特定 .env 文件
        config_specific_env = self.config_dir / f".env.{self.env_name}"
        if config_specific_env.exists():
            env_files.append(config_specific_env)

        # 4. 配置目录的 .env.local 文件 (本地覆盖配置)
        local_env = self.config_dir / ".env.local"
        if local_env.exists():
            env_files.append(local_env)

        # 5. 配置目录的 .env 文件 (默认配置)
        config_env = self.config_dir / ".env"
        if config_env.exists():
            env_files.append(config_env)

        return env_files

    def get_env_info(self) -> Dict[str, Any]:
        """获取当前环境信息"""
        return {
            "env_name": self.env_name,
            "loaded_files": list(self._loaded_envs),
            "config_dir": str(self.config_dir)
        }


class Config:
    """Atlas 系统配置管理器"""

    def __init__(self, config_dir: Optional[Union[str, Path]] = None, env_name: Optional[str] = None):
        """初始化配置管理器

        Args:
            config_dir: 配置文件目录路径
            env_name: 环境名称 (development, production, test等)
        """
        self.config_dir = Path(config_dir) if config_dir else Path("config")
        self.env_name = env_name
        self._config_data: Dict[str, Any] = {}
        self._database: Optional[DatabaseConfig] = None
        self._collection: Optional[CollectionConfig] = None
        self._llm: Optional[LLMConfig] = None
        self._scheduler: Optional[SchedulerConfig] = None
        self._monitoring: Optional[MonitoringConfig] = None

        # 初始化环境变量加载器
        self._env_loader = LocalEnvLoader(self.config_dir, env_name)

        # 加载环境变量
        self._load_env_vars()

        # 加载配置文件
        self._load_config_files()

    def _load_env_vars(self) -> None:
        """加载环境变量"""
        # 加载本地 .env 文件
        self._env_loader.load_env_files()

    def _load_config_files(self) -> None:
        """加载配置文件"""
        # 加载主配置文件
        main_config_file = self.config_dir / "config.yaml"
        if main_config_file.exists():
            with open(main_config_file, 'r', encoding='utf-8') as f:
                self._config_data.update(yaml.safe_load(f) or {})

        # 加载数据源配置文件
        sources_config_file = self.config_dir / "sources.yaml"
        if sources_config_file.exists():
            self._config_data['sources'] = yaml.safe_load(sources_config_file)

        # 加载环境特定配置文件（如果存在）
        env_config_file = self.config_dir / f"config.{self.env_name}.yaml"
        if env_config_file.exists():
            with open(env_config_file, 'r', encoding='utf-8') as f:
                env_config = yaml.safe_load(f) or {}
                # 深度合并环境特定配置
                self._deep_merge(self._config_data, env_config)

    def _deep_merge(self, base_dict: Dict, update_dict: Dict) -> None:
        """深度合并字典"""
        for key, value in update_dict.items():
            if key in base_dict and isinstance(base_dict[key], dict) and isinstance(value, dict):
                self._deep_merge(base_dict[key], value)
            else:
                base_dict[key] = value

    @property
    def env(self) -> str:
        """环境类型"""
        return os.getenv("ATLAS_ENV", self._config_data.get("env", self.env_name or "development"))

    @property
    def debug(self) -> bool:
        """调试模式"""
        return os.getenv("ATLAS_DEBUG", "false").lower() == "true"

    @property
    def log_level(self) -> str:
        """日志级别"""
        return os.getenv("ATLAS_LOG_LEVEL", self._config_data.get("log_level", "INFO"))

    @property
    def data_dir(self) -> Path:
        """数据目录"""
        path_str = os.getenv("ATLAS_DATA_DIR", self._config_data.get("data_dir", "./data"))
        return Path(path_str).expanduser().absolute()

    @property
    def config_dir_path(self) -> Path:
        """配置目录"""
        return self.config_dir.expanduser().absolute()

    @property
    def log_dir(self) -> Path:
        """日志目录"""
        path_str = os.getenv("ATLAS_LOG_DIR", self._config_data.get("log_dir", "./logs"))
        return Path(path_str).expanduser().absolute()

    @property
    def database(self) -> DatabaseConfig:
        """数据库配置"""
        if self._database is None:
            self._database = DatabaseConfig()
        return self._database

    @property
    def collection(self) -> CollectionConfig:
        """数据采集配置"""
        if self._collection is None:
            self._collection = CollectionConfig()
        return self._collection

    @property
    def llm(self) -> LLMConfig:
        """LLM配置"""
        if self._llm is None:
            self._llm = LLMConfig()
        return self._llm

    @property
    def scheduler(self) -> SchedulerConfig:
        """调度器配置"""
        if self._scheduler is None:
            self._scheduler = SchedulerConfig()
        return self._scheduler

    @property
    def monitoring(self) -> MonitoringConfig:
        """监控配置"""
        if self._monitoring is None:
            self._monitoring = MonitoringConfig()
        return self._monitoring

    @property
    def sources(self) -> Dict[str, Any]:
        """数据源配置"""
        return self._config_data.get("sources", {})

    def get_env_info(self) -> Dict[str, Any]:
        """获取环境信息"""
        return {
            **self._env_loader.get_env_info(),
            "current_env": self.env,
            "debug": self.debug,
            "log_level": self.log_level
        }

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值

        Args:
            key: 配置键，支持点分隔的嵌套键
            default: 默认值

        Returns:
            配置值
        """
        keys = key.split('.')
        value = self._config_data

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value: Any) -> None:
        """设置配置值

        Args:
            key: 配置键，支持点分隔的嵌套键
            value: 配置值
        """
        keys = key.split('.')
        config = self._config_data

        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        config[keys[-1]] = value

    def reload(self) -> None:
        """重新加载配置"""
        self._config_data.clear()
        self._database = None
        self._collection = None
        self._llm = None
        self._scheduler = None
        self._monitoring = None

        # 重新初始化环境变量加载器
        self._env_loader = LocalEnvLoader(self.config_dir, self.env_name)
        self._load_env_vars()
        self._load_config_files()

    def save(self) -> None:
        """保存配置到文件"""
        config_file = self.config_dir / "config.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)

        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(self._config_data, f, default_flow_style=False, allow_unicode=True)

    def ensure_directories(self) -> None:
        """确保必要的目录存在"""
        directories = [
            self.data_dir,
            self.log_dir,
            self.config_dir_path,
            self.data_dir / "raw",
            self.data_dir / "processed",
            self.data_dir / "indexes"
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"Config(env={self.env}, data_dir={self.data_dir}, debug={self.debug})"


# 全局配置实例
config: Optional[Config] = None


def get_config(config_dir: Optional[Union[str, Path]] = None, env_name: Optional[str] = None) -> Config:
    """获取全局配置实例

    Args:
        config_dir: 配置文件目录路径
        env_name: 环境名称

    Returns:
        配置实例
    """
    global config
    if config is None:
        config = Config(config_dir, env_name)
        config.ensure_directories()
    return config


def reload_config(config_dir: Optional[Union[str, Path]] = None, env_name: Optional[str] = None) -> Config:
    """重新加载配置

    Args:
        config_dir: 配置文件目录路径
        env_name: 环境名称

    Returns:
        新的配置实例
    """
    global config
    config = Config(config_dir, env_name)
    config.ensure_directories()
    return config