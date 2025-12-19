"""
Atlas HTTP 客户端

提供增强的 HTTP 请求功能，包括重试、代理、缓存等。
"""

import asyncio
import time
import hashlib
import json
from typing import Any, Dict, List, Optional, Union, Callable
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass, field
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import httpx

from ..core.config import CollectionConfig
from ..core.logging import get_logger


@dataclass
class RequestConfig:
    """请求配置"""
    timeout: int = 30
    max_retries: int = 3
    retry_backoff_factor: float = 1.0
    retry_status_codes: List[int] = field(default_factory=lambda: [500, 502, 503, 504])
    verify_ssl: bool = True
    allow_redirects: bool = True
    max_redirects: int = 5
    use_cache: bool = True
    cache_ttl: int = 3600  # 缓存时间（秒）
    proxy: Optional[str] = None
    custom_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class Response:
    """HTTP 响应封装"""
    url: str
    status_code: int
    headers: Dict[str, str]
    content: bytes
    text: str
    encoding: str
    elapsed_time: float
    from_cache: bool = False
    request_info: Dict[str, Any] = field(default_factory=dict)


class CacheManager:
    """缓存管理器"""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or Path.home() / ".cache" / "atlas"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger()

    def _get_cache_key(self, url: str, method: str = "GET", data: Optional[Dict] = None) -> str:
        """生成缓存键"""
        key_data = f"{url}:{method}"
        if data:
            key_data += f":{json.dumps(data, sort_keys=True)}"
        return hashlib.md5(key_data.encode()).hexdigest()

    def _get_cache_path(self, cache_key: str) -> Path:
        """获取缓存文件路径"""
        return self.cache_dir / f"{cache_key}.cache"

    def get(self, url: str, method: str = "GET", data: Optional[Dict] = None) -> Optional[Response]:
        """获取缓存的响应"""
        cache_key = self._get_cache_key(url, method, data)
        cache_path = self._get_cache_path(cache_key)

        if not cache_path.exists():
            return None

        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # 检查是否过期
            if time.time() > cache_data['expires_at']:
                cache_path.unlink()  # 删除过期缓存
                return None

            return Response(
                url=cache_data['url'],
                status_code=cache_data['status_code'],
                headers=cache_data['headers'],
                content=cache_data['content'].encode('utf-8'),
                text=cache_data['text'],
                encoding=cache_data['encoding'],
                elapsed_time=cache_data['elapsed_time'],
                from_cache=True,
                request_info=cache_data.get('request_info', {})
            )
        except Exception as e:
            self.logger.warning(f"读取缓存失败", cache_key=cache_key, error=str(e))
            try:
                cache_path.unlink()  # 删除损坏的缓存文件
            except:
                pass
            return None

    def set(self, response: Response, url: str, method: str = "GET",
            data: Optional[Dict] = None, ttl: int = 3600) -> None:
        """设置缓存"""
        cache_key = self._get_cache_key(url, method, data)
        cache_path = self._get_cache_path(cache_key)

        cache_data = {
            'url': response.url,
            'status_code': response.status_code,
            'headers': response.headers,
            'content': response.text,  # 存储文本而不是二进制
            'text': response.text,
            'encoding': response.encoding,
            'elapsed_time': response.elapsed_time,
            'expires_at': time.time() + ttl,
            'request_info': response.request_info
        }

        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"写入缓存失败", cache_key=cache_key, error=str(e))

    def clear(self) -> None:
        """清空缓存"""
        try:
            for cache_file in self.cache_dir.glob("*.cache"):
                cache_file.unlink()
            self.logger.info("缓存已清空")
        except Exception as e:
            self.logger.warning(f"清空缓存失败", error=str(e))

    def cleanup_expired(self) -> None:
        """清理过期缓存"""
        current_time = time.time()
        cleaned_count = 0

        try:
            for cache_file in self.cache_dir.glob("*.cache"):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)

                    if current_time > cache_data['expires_at']:
                        cache_file.unlink()
                        cleaned_count += 1
                except:
                    cache_file.unlink()  # 删除损坏的文件
                    cleaned_count += 1

            if cleaned_count > 0:
                self.logger.info(f"清理了 {cleaned_count} 个过期缓存文件")
        except Exception as e:
            self.logger.warning(f"清理过期缓存失败", error=str(e))


class RateLimiter:
    """频率限制器"""

    def __init__(self, requests_per_second: float = 1.0):
        self.requests_per_second = requests_per_second
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time = 0.0
        self.domain_limits = {}  # 按域名单独限制
        self.logger = get_logger()

    def set_domain_limit(self, domain: str, requests_per_second: float) -> None:
        """设置特定域名的频率限制"""
        self.domain_limits[domain] = 1.0 / requests_per_second

    def wait(self, domain: str = None) -> None:
        """等待直到可以发送下一个请求"""
        current_time = time.time()

        # 使用域名特定的限制或全局限制
        min_interval = self.domain_limits.get(domain, self.min_interval)

        time_since_last = current_time - self.last_request_time
        if time_since_last < min_interval:
            sleep_time = min_interval - time_since_last
            self.logger.debug(f"频率限制等待", sleep_time=sleep_time, domain=domain)
            time.sleep(sleep_time)

        self.last_request_time = time.time()

    async def wait_async(self, domain: str = None) -> None:
        """异步等待"""
        current_time = time.time()

        min_interval = self.domain_limits.get(domain, self.min_interval)

        time_since_last = current_time - self.last_request_time
        if time_since_last < min_interval:
            sleep_time = min_interval - time_since_last
            self.logger.debug(f"频率限制等待（异步）", sleep_time=sleep_time, domain=domain)
            await asyncio.sleep(sleep_time)

        self.last_request_time = time.time()


class HTTPClient:
    """增强的 HTTP 客户端"""

    def __init__(self, config: CollectionConfig, request_config: Optional[RequestConfig] = None):
        self.config = config
        self.request_config = request_config or RequestConfig()
        self.logger = get_logger()
        self.session = None
        self.async_client = None
        self.cache_manager = CacheManager()
        self.rate_limiter = RateLimiter(1.0 / self.config.rate_limit_delay)

        # 统计信息
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'cached_requests': 0,
            'total_bytes': 0,
            'total_time': 0.0
        }

    def _setup_session(self) -> None:
        """设置 requests 会话"""
        if self.session is not None:
            return

        self.session = requests.Session()

        # 设置重试策略
        retry_strategy = Retry(
            total=self.request_config.max_retries,
            backoff_factor=self.request_config.retry_backoff_factor,
            status_forcelist=self.request_config.retry_status_codes,
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"]
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # 设置默认请求头
        self.session.headers.update({
            'User-Agent': self.config.default_user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })

        # 添加自定义请求头
        self.session.headers.update(self.request_config.custom_headers)

    def _setup_async_client(self) -> None:
        """设置异步客户端"""
        if self.async_client is not None:
            return

        headers = {
            'User-Agent': self.config.default_user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
        }
        headers.update(self.request_config.custom_headers)

        self.async_client = httpx.AsyncClient(
            timeout=self.request_config.timeout,
            verify=self.request_config.verify_ssl,
            follow_redirects=self.request_config.allow_redirects,
            headers=headers
        )

    def _get_domain(self, url: str) -> str:
        """提取域名"""
        parsed = urlparse(url)
        return parsed.netloc.lower()

    def request(self, method: str, url: str, **kwargs) -> Optional[Response]:
        """发送 HTTP 请求"""
        self._setup_session()

        domain = self._get_domain(url)
        start_time = time.time()

        # 频率限制
        self.rate_limiter.wait(domain)

        # 检查缓存
        if self.request_config.use_cache and method.upper() == 'GET':
            cached_response = self.cache_manager.get(url, method, kwargs.get('json'))
            if cached_response:
                self.stats['cached_requests'] += 1
                self.logger.debug(f"使用缓存响应", url=url, method=method)
                return cached_response

        try:
            self.stats['total_requests'] += 1
            self.logger.debug(f"发送请求", url=url, method=method)

            # 设置代理
            if self.request_config.proxy:
                kwargs['proxies'] = {'http': self.request_config.proxy, 'https': self.request_config.proxy}

            # 发送请求
            response = self.session.request(
                method=method,
                url=url,
                timeout=self.request_config.timeout,
                verify=self.request_config.verify_ssl,
                allow_redirects=self.request_config.allow_redirects,
                **kwargs
            )

            elapsed_time = time.time() - start_time

            # 封装响应
            wrapped_response = Response(
                url=response.url,
                status_code=response.status_code,
                headers=dict(response.headers),
                content=response.content,
                text=response.text,
                encoding=response.encoding or 'utf-8',
                elapsed_time=elapsed_time,
                from_cache=False,
                request_info={
                    'method': method,
                    'url': url,
                    'kwargs': kwargs
                }
            )

            # 更新统计
            self.stats['successful_requests'] += 1
            self.stats['total_bytes'] += len(response.content)
            self.stats['total_time'] += elapsed_time

            # 缓存响应
            if (self.request_config.use_cache and
                method.upper() == 'GET' and
                response.status_code == 200):
                self.cache_manager.set(
                    wrapped_response, url, method,
                    kwargs.get('json'), self.request_config.cache_ttl
                )

            self.logger.debug(f"请求成功", url=url, status_code=response.status_code,
                            elapsed_time=f"{elapsed_time:.3f}s")
            return wrapped_response

        except requests.exceptions.Timeout as e:
            self.stats['failed_requests'] += 1
            self.logger.warning(f"请求超时", url=url, method=method,
                            timeout=self.request_config.timeout, error=str(e))
            return None

        except requests.exceptions.ConnectionError as e:
            self.stats['failed_requests'] += 1
            self.logger.warning(f"连接错误", url=url, method=method, error=str(e))
            return None

        except requests.exceptions.HTTPError as e:
            self.stats['failed_requests'] += 1
            self.logger.warning(f"HTTP错误", url=url, method=method,
                            status_code=e.response.status_code, error=str(e))
            return None

        except Exception as e:
            self.stats['failed_requests'] += 1
            self.logger.exception(f"请求异常", url=url, method=method, error=str(e))
            return None

    def get(self, url: str, **kwargs) -> Optional[Response]:
        """发送 GET 请求"""
        return self.request('GET', url, **kwargs)

    def post(self, url: str, **kwargs) -> Optional[Response]:
        """发送 POST 请求"""
        return self.request('POST', url, **kwargs)

    def put(self, url: str, **kwargs) -> Optional[Response]:
        """发送 PUT 请求"""
        return self.request('PUT', url, **kwargs)

    def delete(self, url: str, **kwargs) -> Optional[Response]:
        """发送 DELETE 请求"""
        return self.request('DELETE', url, **kwargs)

    async def arequest(self, method: str, url: str, **kwargs) -> Optional[Response]:
        """发送异步 HTTP 请求"""
        self._setup_async_client()

        domain = self._get_domain(url)
        start_time = time.time()

        # 频率限制
        await self.rate_limiter.wait_async(domain)

        try:
            self.stats['total_requests'] += 1
            self.logger.debug(f"发送异步请求", url=url, method=method)

            response = await self.async_client.request(method, url, **kwargs)
            elapsed_time = time.time() - start_time

            wrapped_response = Response(
                url=str(response.url),
                status_code=response.status_code,
                headers=dict(response.headers),
                content=response.content,
                content=response.text,
                encoding=response.encoding or 'utf-8',
                elapsed_time=elapsed_time,
                from_cache=False,
                request_info={
                    'method': method,
                    'url': url,
                    'kwargs': kwargs
                }
            )

            self.stats['successful_requests'] += 1
            self.stats['total_bytes'] += len(response.content)
            self.stats['total_time'] += elapsed_time

            self.logger.debug(f"异步请求成功", url=url, status_code=response.status_code,
                            elapsed_time=f"{elapsed_time:.3f}s")
            return wrapped_response

        except httpx.TimeoutException as e:
            self.stats['failed_requests'] += 1
            self.logger.warning(f"异步请求超时", url=url, method=method, error=str(e))
            return None

        except httpx.ConnectError as e:
            self.stats['failed_requests'] += 1
            self.logger.warning(f"异步连接错误", url=url, method=method, error=str(e))
            return None

        except Exception as e:
            self.stats['failed_requests'] += 1
            self.logger.exception(f"异步请求异常", url=url, method=method, error=str(e))
            return None

    def set_user_agent(self, user_agent: str) -> None:
        """设置 User-Agent"""
        if self.session:
            self.session.headers['User-Agent'] = user_agent
        if self.async_client:
            self.async_client.headers['User-Agent'] = user_agent
        self.logger.debug(f"User-Agent 已更新", user_agent=user_agent)

    def set_proxy(self, proxy: str) -> None:
        """设置代理"""
        self.request_config.proxy = proxy
        self.logger.info(f"代理已设置", proxy=proxy)

    def clear_cache(self) -> None:
        """清空缓存"""
        self.cache_manager.clear()

    def cleanup_cache(self) -> None:
        """清理过期缓存"""
        self.cache_manager.cleanup_expired()

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self.stats.copy()
        if stats['total_requests'] > 0:
            stats['success_rate'] = stats['successful_requests'] / stats['total_requests']
            stats['average_response_time'] = stats['total_time'] / stats['total_requests']
            stats['cache_hit_rate'] = stats['cached_requests'] / stats['total_requests']
        else:
            stats['success_rate'] = 0.0
            stats['average_response_time'] = 0.0
            stats['cache_hit_rate'] = 0.0
        return stats

    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'cached_requests': 0,
            'total_bytes': 0,
            'total_time': 0.0
        }
        self.logger.info("统计信息已重置")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            self.session.close()
        if self.async_client:
            asyncio.create_task(self.async_client.aclose())

    async def aclose(self) -> None:
        """异步关闭客户端"""
        if self.async_client:
            await self.async_client.aclose()