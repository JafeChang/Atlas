"""
LLM 提供商工厂模式实现

支持多种LLM提供商：本地模型、OpenAI、Anthropic等
"""

import os
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum
import json

# 设置日志
logger = logging.getLogger(__name__)


class LLMProviderType(Enum):
    """LLM提供商类型"""
    LOCAL = "local"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass
class LLMConfig:
    """LLM配置"""
    provider: str
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    timeout: int = 60
    max_workers: int = 1
    temperature: float = 0.7
    max_tokens: Optional[int] = None


class BaseLLMProvider(ABC):
    """LLM提供商基类"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def simple_chat(self, message: str, **kwargs) -> str:
        """简单对话接口"""
        pass

    @abstractmethod
    async def async_simple_chat(self, message: str, **kwargs) -> str:
        """异步简单对话接口"""
        pass

    @abstractmethod
    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """多轮对话接口"""
        pass


class OpenAIProvider(BaseLLMProvider):
    """OpenAI兼容的LLM提供商"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = None
        self._init_client()

    def _init_client(self):
        """初始化OpenAI客户端"""
        try:
            import openai
            self.client = openai.OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url
            )
            self.logger.info(f"OpenAI客户端初始化成功: {self.config.base_url}")
        except ImportError:
            self.logger.error("openai包未安装，请运行: uv add openai")
            raise
        except Exception as e:
            self.logger.error(f"OpenAI客户端初始化失败: {e}")
            raise

    def simple_chat(self, message: str, **kwargs) -> str:
        """简单对话"""
        try:
            messages = [{"role": "user", "content": message}]
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=kwargs.get('temperature', self.config.temperature),
                max_tokens=kwargs.get('max_tokens', self.config.max_tokens)
            )
            return response.choices[0].message.content
        except Exception as e:
            self.logger.error(f"OpenAI对话失败: {e}")
            raise

    async def async_simple_chat(self, message: str, **kwargs) -> str:
        """异步简单对话"""
        try:
            import openai
            messages = [{"role": "user", "content": message}]
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=kwargs.get('temperature', self.config.temperature),
                max_tokens=kwargs.get('max_tokens', self.config.max_tokens)
            )
            return response.choices[0].message.content
        except Exception as e:
            self.logger.error(f"OpenAI异步对话失败: {e}")
            raise

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """多轮对话"""
        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=kwargs.get('temperature', self.config.temperature),
                max_tokens=kwargs.get('max_tokens', self.config.max_tokens)
            )
            return response.choices[0].message.content
        except Exception as e:
            self.logger.error(f"OpenAI多轮对话失败: {e}")
            raise


class LocalLLMProvider(BaseLLMProvider):
    """本地LLM提供商（Ollama等）"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = None
        self._init_client()

    def _init_client(self):
        """初始化本地LLM客户端"""
        try:
            from .local_client import LocalLLMClient, ModelConfig

            model_config = ModelConfig(
                model_name=self.config.model,
                base_url=self.config.base_url or "http://localhost:11434",
                timeout=self.config.timeout
            )

            self.client = LocalLLMClient(model_config)
            self.logger.info(f"本地LLM客户端初始化成功: {model_config.base_url}")
        except Exception as e:
            self.logger.error(f"本地LLM客户端初始化失败: {e}")
            raise

    def simple_chat(self, message: str, **kwargs) -> str:
        """简单对话"""
        try:
            from .local_client import GenerationConfig

            gen_config = GenerationConfig(
                temperature=kwargs.get('temperature', self.config.temperature),
                max_tokens=kwargs.get('max_tokens', self.config.max_tokens)
            )

            response = self.client.generate(message, gen_config)
            return response.text
        except Exception as e:
            self.logger.error(f"本地LLM对话失败: {e}")
            raise

    async def async_simple_chat(self, message: str, **kwargs) -> str:
        """异步简单对话"""
        try:
            from .local_client import GenerationConfig

            gen_config = GenerationConfig(
                temperature=kwargs.get('temperature', self.config.temperature),
                max_tokens=kwargs.get('max_tokens', self.config.max_tokens)
            )

            response = await self.client.generate_async(message, gen_config)
            return response.text
        except Exception as e:
            self.logger.error(f"本地LLM异步对话失败: {e}")
            raise

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """多轮对话"""
        # 对于本地LLM，将多轮对话合并为单个提示
        conversation = "\n".join([f"{msg['role']}: {msg['content']}" for msg in messages])
        return self.simple_chat(conversation, **kwargs)


class LLMProviderFactory:
    """LLM提供商工厂"""

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._providers = {
            LLMProviderType.LOCAL: LocalLLMProvider,
            LLMProviderType.OPENAI: OpenAIProvider,
            # LLMProviderType.ANTHROPIC: AnthropicProvider,  # 暂未实现
        }

    def create_provider(self, config: Optional[LLMConfig] = None) -> BaseLLMProvider:
        """创建LLM提供商实例"""
        if config is None:
            config = self._load_config_from_env()

        provider_type = LLMProviderType(config.provider)

        if provider_type not in self._providers:
            raise ValueError(f"不支持的LLM提供商: {provider_type}")

        provider_class = self._providers[provider_type]
        return provider_class(config)

    def _load_config_from_env(self) -> LLMConfig:
        """从环境变量加载LLM配置"""
        provider = os.getenv('ATLAS_LLM_PROVIDER', 'local')
        model = os.getenv('ATLAS_LLM_MODEL', 'qwen2.5:7b')
        base_url = os.getenv('ATLAS_LLM_BASE_URL')
        timeout = int(os.getenv('ATLAS_LLM_TIMEOUT', '60'))
        max_workers = int(os.getenv('ATLAS_LLM_MAX_WORKERS', '1'))
        temperature = float(os.getenv('ATLAS_LLM_TEMPERATURE', '0.7'))

        api_key = None
        if provider == 'openai':
            api_key = os.getenv('ATLAS_OPENAI_API_KEY')
            if not base_url:
                base_url = os.getenv('ATLAS_OPENAI_BASE_URL', 'https://api.openai.com/v1')
        elif provider == 'anthropic':
            api_key = os.getenv('ATLAS_ANTHROPIC_API_KEY')

        if not api_key and provider in ['openai', 'anthropic']:
            self.logger.warning(f"{provider}提供商未设置API密钥")

        return LLMConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_workers=max_workers,
            temperature=temperature
        )

    def get_supported_providers(self) -> List[str]:
        """获取支持的提供商列表"""
        return [provider.value for provider in LLMProviderType]

    def validate_config(self, config: LLMConfig) -> Dict[str, Any]:
        """验证配置"""
        result = {
            "valid": True,
            "errors": [],
            "warnings": []
        }

        # 验证提供商
        if config.provider not in self.get_supported_providers():
            result["valid"] = False
            result["errors"].append(f"不支持的提供商: {config.provider}")

        # 验证API密钥
        if config.provider in ['openai', 'anthropic'] and not config.api_key:
            result["valid"] = False
            result["errors"].append(f"{config.provider}提供商需要API密钥")

        # 验证URL
        if config.provider == 'local' and not config.base_url:
            result["warnings"].append("本地LLM未设置base_url，将使用默认值")

        return result


# 便捷函数
def create_llm_provider(config: Optional[LLMConfig] = None) -> BaseLLMProvider:
    """创建LLM提供商的便捷函数"""
    factory = LLMProviderFactory()
    return factory.create_provider(config)