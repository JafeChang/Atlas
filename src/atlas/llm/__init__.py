"""
Atlas LLM 集成模块

提供本地 LLM 客户端、智能去重、任务队列管理等功能。
支持 Ollama 集成、语义分析、自适应控制等。
"""

from .local_client import LocalLLMClient, ModelConfig, GenerationConfig
from .queue_manager import LLMQueueManager, LLMTask, LLMTaskPriority
from .adaptive_controller import AdaptiveController, ControllerConfig, ControlAction
from .semantic_dedup import SemanticDeduplicator, SemanticConfig
from .factory import LLMProviderFactory, LLMConfig, BaseLLMProvider, create_llm_provider

__all__ = [
    # Factory
    "LLMProviderFactory",
    "LLMConfig",
    "BaseLLMProvider",
    "create_llm_provider",

    # Local Client
    "LocalLLMClient",
    "ModelConfig",
    "GenerationConfig",

    # Queue Manager
    "LLMQueueManager",
    "LLMTask",
    "LLMTaskPriority",

    # Adaptive Controller
    "AdaptiveController",
    "ControllerConfig",
    "ControlAction",

    # Semantic Deduplication
    "SemanticDeduplicator",
    "SemanticConfig",
]

__version__ = "1.0.0"