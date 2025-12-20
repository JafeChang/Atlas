"""
本地 LLM 客户端

提供与 Ollama 服务的集成，支持多种模型的本地调用。
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
import aiohttp
import numpy as np

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class ModelType(Enum):
    """模型类型"""
    GENERAL = "general"          # 通用模型
    EMBEDDING = "embedding"      # 嵌入模型
    CHAT = "chat"               # 对话模型
    CODE = "code"               # 代码模型


@dataclass
class ModelConfig:
    """模型配置"""
    name: str
    model_type: ModelType
    context_length: int = 4096
    temperature: float = 0.7
    top_p: float = 0.9
    top_k: int = 40
    repeat_penalty: float = 1.1
    stop_tokens: List[str] = field(default_factory=list)
    max_tokens: Optional[int] = None

    # 性能配置
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0

    def __post_init__(self) -> None:
        """初始化后处理"""
        if self.model_type == ModelType.EMBEDDING:
            self.temperature = 0.0
            self.top_p = 1.0
            self.top_k = 1


@dataclass
class GenerationConfig:
    """生成配置"""
    prompt: str
    system_prompt: Optional[str] = None
    context: List[Dict[str, str]] = field(default_factory=list)
    stream: bool = False
    raw: bool = False
    format: Optional[str] = None  # "json" for JSON output

    # 临时覆盖模型配置
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_tokens: Optional[int] = None


@dataclass
class GenerationResult:
    """生成结果"""
    text: str
    model: str
    created_at: float
    done: bool
    total_duration: Optional[float] = None
    load_duration: Optional[float] = None
    prompt_eval_count: Optional[int] = None
    prompt_eval_duration: Optional[float] = None
    eval_count: Optional[int] = None
    eval_duration: Optional[float] = None
    context: Optional[List[int]] = None

    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)


class LocalLLMClient:
    """本地 LLM 客户端"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "llama2",
        timeout: float = 30.0,
        max_concurrent: int = 5
    ):
        """初始化本地 LLM 客户端

        Args:
            base_url: Ollama 服务地址
            default_model: 默认模型名称
            timeout: 请求超时时间
            max_concurrent: 最大并发请求数
        """
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_concurrent = max_concurrent

        # 连接池和信号量
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session: Optional[aiohttp.ClientSession] = None

        # 模型缓存
        self._models_cache: Dict[str, ModelConfig] = {}
        self._models_cache_time: float = 0
        self._models_cache_ttl: float = 300  # 5分钟

        # 统计信息
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_tokens": 0,
            "total_duration": 0.0,
            "cache_hits": 0,
        }

        logger.info(f"LLM客户端初始化完成: {base_url}, 默认模型: {default_model}")

    async def __aenter__(self) -> "LocalLLMClient":
        """异步上下文管理器入口"""
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.close()

    async def _ensure_session(self) -> None:
        """确保会话存在"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                connector=aiohttp.TCPConnector(limit=self.max_concurrent * 2)
            )

    async def close(self) -> None:
        """关闭客户端"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("LLM客户端已关闭")

    async def generate(
        self,
        config: GenerationConfig,
        model: Optional[str] = None,
        model_config: Optional[ModelConfig] = None
    ) -> GenerationResult:
        """生成文本

        Args:
            config: 生成配置
            model: 模型名称，默认使用客户端默认模型
            model_config: 模型配置，可选

        Returns:
            生成结果
        """
        model_name = model or self.default_model
        start_time = time.time()

        async with self._semaphore:
            await self._ensure_session()

            # 获取模型配置
            if not model_config:
                model_config = await self.get_model_config(model_name)

            # 构建请求数据
            request_data = self._build_request_data(config, model_config)

            # 记录统计
            self._stats["total_requests"] += 1

            # 重试机制
            for attempt in range(model_config.max_retries + 1):
                try:
                    if attempt > 0:
                        await asyncio.sleep(model_config.retry_delay * attempt)

                    result = await self._make_request(model_name, request_data, config.stream)

                    # 更新统计
                    self._stats["successful_requests"] += 1
                    if result.total_duration:
                        self._stats["total_duration"] += result.total_duration / 1000
                    if result.eval_count:
                        self._stats["total_tokens"] += result.eval_count

                    return result

                except Exception as e:
                    if attempt == model_config.max_retries:
                        self._stats["failed_requests"] += 1
                        logger.error(f"生成请求失败（重试{model_config.max_retries}次后）: {e}")
                        raise
                    else:
                        logger.warning(f"生成请求失败，重试{attempt + 1}/{model_config.max_retries}: {e}")

    async def generate_stream(
        self,
        config: GenerationConfig,
        model: Optional[str] = None,
        model_config: Optional[ModelConfig] = None
    ):
        """流式生成文本

        Args:
            config: 生成配置
            model: 模型名称
            model_config: 模型配置

        Yields:
            生成结果的片段
        """
        # 强制启用流式模式
        config.stream = True
        result = await self.generate(config, model, model_config)
        yield result

    async def embed(
        self,
        text: str,
        model: Optional[str] = None
    ) -> List[float]:
        """生成文本嵌入

        Args:
            text: 输入文本
            model: 嵌入模型名称

        Returns:
            嵌入向量
        """
        # 默认使用常见的嵌入模型
        embedding_model = model or "all-minilm"

        await self._ensure_session()

        request_data = {
            "model": embedding_model,
            "prompt": text
        }

        try:
            async with self._session.post(
                f"{self.base_url}/api/embeddings",
                json=request_data
            ) as response:
                response.raise_for_status()
                data = await response.json()

                return data.get("embedding", [])

        except Exception as e:
            logger.error(f"嵌入生成失败: {e}")
            raise

    async def list_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """列出可用模型

        Args:
            force_refresh: 是否强制刷新缓存

        Returns:
            模型列表
        """
        current_time = time.time()

        # 检查缓存
        if not force_refresh and self._models_cache_time > 0:
            if current_time - self._models_cache_time < self._models_cache_ttl:
                self._stats["cache_hits"] += 1
                return [{"name": name, "config": config} for name, config in self._models_cache.items()]

        await self._ensure_session()

        try:
            async with self._session.get(f"{self.base_url}/api/tags") as response:
                response.raise_for_status()
                data = await response.json()

                models = data.get("models", [])

                # 更新缓存
                self._models_cache.clear()
                for model in models:
                    name = model["name"]
                    size = model.get("size", 0)

                    # 根据模型特征推断类型
                    if "embed" in name.lower():
                        model_type = ModelType.EMBEDDING
                    elif "code" in name.lower() or "coder" in name.lower():
                        model_type = ModelType.CODE
                    else:
                        model_type = ModelType.GENERAL

                    self._models_cache[name] = ModelConfig(
                        name=name,
                        model_type=model_type,
                        context_length=4096  # 默认值，实际可能不同
                    )

                self._models_cache_time = current_time
                return models

        except Exception as e:
            logger.error(f"获取模型列表失败: {e}")
            raise

    async def get_model_config(self, model: str) -> ModelConfig:
        """获取模型配置

        Args:
            model: 模型名称

        Returns:
            模型配置
        """
        # 刷新模型列表
        await self.list_models()

        if model not in self._models_cache:
            # 创建默认配置
            self._models_cache[model] = ModelConfig(
                name=model,
                model_type=ModelType.GENERAL
            )

        return self._models_cache[model]

    def _build_request_data(
        self,
        config: GenerationConfig,
        model_config: ModelConfig
    ) -> Dict[str, Any]:
        """构建请求数据"""
        # 构建消息
        messages = []

        if config.system_prompt:
            messages.append({"role": "system", "content": config.system_prompt})

        # 添加上下文
        for msg in config.context:
            messages.append(msg)

        # 添加用户提示
        messages.append({"role": "user", "content": config.prompt})

        # 构建选项
        options = {
            "temperature": config.temperature or model_config.temperature,
            "top_p": config.top_p or model_config.top_p,
            "top_k": config.top_k or model_config.top_k,
            "repeat_penalty": model_config.repeat_penalty,
        }

        if config.max_tokens or model_config.max_tokens:
            options["num_predict"] = config.max_tokens or model_config.max_tokens

        if model_config.stop_tokens:
            options["stop"] = model_config.stop_tokens

        request_data = {
            "model": model_config.name,
            "messages": messages,
            "stream": config.stream,
            "options": options,
        }

        if config.format:
            request_data["format"] = config.format

        if config.raw:
            request_data["raw"] = True

        return request_data

    async def _make_request(
        self,
        model: str,
        request_data: Dict[str, Any],
        stream: bool = False
    ) -> GenerationResult:
        """发送请求"""
        if stream:
            return await self._make_stream_request(model, request_data)
        else:
            return await self._make_sync_request(model, request_data)

    async def _make_sync_request(
        self,
        model: str,
        request_data: Dict[str, Any]
    ) -> GenerationResult:
        """发送同步请求"""
        async with self._session.post(
            f"{self.base_url}/api/chat",
            json=request_data
        ) as response:
            response.raise_for_status()
            data = await response.json()

            message = data.get("message", {})
            content = message.get("content", "")

            return GenerationResult(
                text=content,
                model=model,
                created_at=data.get("created_at", time.time()),
                done=data.get("done", True),
                total_duration=data.get("total_duration"),
                load_duration=data.get("load_duration"),
                prompt_eval_count=data.get("prompt_eval_count"),
                prompt_eval_duration=data.get("prompt_eval_duration"),
                eval_count=data.get("eval_count"),
                eval_duration=data.get("eval_duration"),
                context=data.get("context"),
                metadata={
                    "model_details": data.get("model_details", {}),
                }
            )

    async def _make_stream_request(
        self,
        model: str,
        request_data: Dict[str, Any]
    ) -> GenerationResult:
        """发送流式请求（这里简化为同步处理）"""
        # 对于流式请求，我们在这里简化处理，收集所有片段
        content_parts = []

        async with self._session.post(
            f"{self.base_url}/api/chat",
            json=request_data
        ) as response:
            response.raise_for_status()

            async for line in response.content:
                if line:
                    try:
                        data = json.loads(line.decode('utf-8'))
                        if "message" in data and "content" in data["message"]:
                            content_parts.append(data["message"]["content"])

                        if data.get("done", False):
                            break
                    except json.JSONDecodeError:
                        continue

        return GenerationResult(
            text="".join(content_parts),
            model=model,
            created_at=time.time(),
            done=True,
            metadata={"stream": True}
        )

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._stats.copy()

        if stats["successful_requests"] > 0:
            stats["average_duration"] = stats["total_duration"] / stats["successful_requests"]
            stats["success_rate"] = stats["successful_requests"] / stats["total_requests"] * 100
        else:
            stats["average_duration"] = 0.0
            stats["success_rate"] = 0.0

        if stats["total_tokens"] > 0:
            stats["tokens_per_second"] = stats["total_tokens"] / stats["total_duration"] if stats["total_duration"] > 0 else 0
        else:
            stats["tokens_per_second"] = 0.0

        return stats

    def reset_stats(self) -> None:
        """重置统计信息"""
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_tokens": 0,
            "total_duration": 0.0,
            "cache_hits": 0,
        }

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            await self._ensure_session()

            # 检查Ollama服务是否可用
            async with self._session.get(f"{self.base_url}/api/tags") as response:
                if response.status == 200:
                    data = await response.json()
                    models = data.get("models", [])

                    return {
                        "status": "healthy",
                        "models_count": len(models),
                        "models": [model["name"] for model in models[:10]],  # 只返回前10个
                        "client_stats": self.get_stats(),
                    }
                else:
                    return {
                        "status": "unhealthy",
                        "error": f"HTTP {response.status}",
                    }

        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
            }


# 便利函数
async def create_llm_client(
    base_url: str = "http://localhost:11434",
    default_model: str = "llama2",
    **kwargs
) -> LocalLLMClient:
    """创建LLM客户端

    Args:
        base_url: Ollama服务地址
        default_model: 默认模型
        **kwargs: 其他参数

    Returns:
        LLM客户端实例
    """
    client = LocalLLMClient(base_url, default_model, **kwargs)
    await client._ensure_session()
    return client