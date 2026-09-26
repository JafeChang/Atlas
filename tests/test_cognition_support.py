"""T-003 测试支撑（**刻意不是 `conftest.py`**）。

为什么不放进 `conftest.py`：同一分支上有多个代理并行工作，`tests/conftest.py` 是共享文件，
改动它会互相踩（本项目刚发生过一次：我覆盖了别人的未跟踪 `conftest.py`，已如实报告）。
因此本任务的夹具全部以**可显式 import 的普通函数/上下文管理器**形式提供，
每个测试文件自己构造所需对象——不依赖任何自动夹具注入。

提供三件事：

1. **真 node 的解析**与"边车依赖是否已安装"的门（缺依赖时 `pytest.skip`，而不是假装通过）；
2. **凭据检查**：只从进程环境 / `.env.local` 读，**绝不打印**；缺失时 `skip` 真实调用类测试；
3. **本地 mock OpenAI 兼容服务器**：给降级 / 协议类测试一个不依赖外网的确定性对端。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from atlas.cognition import (
    DS_ROUTE,
    OA_ROUTE,
    CognitionConfig,
    PiSidecarCognitionPort,
    available_routes,
    load_env_file,
    repo_root,
)

REPO_ROOT = repo_root() or Path(__file__).resolve().parents[1]
SIDECAR_DIR = REPO_ROOT / "src" / "atlas" / "cognition" / "sidecar"

#: 测试用假密钥。**不是**真凭据，写死也无所谓；真凭据一律从环境读。
FAKE_KEY = "test-key-not-a-secret"


def require_node():
    """返回可用的 node；没有就 `skip`（而不是 fail——环境问题不是缺陷）。"""
    from atlas.cognition import resolve_node
    from atlas.cognition.errors import SidecarUnavailableError

    try:
        return resolve_node()
    except SidecarUnavailableError as exc:  # pragma: no cover - 环境缺失时
        pytest.skip(f"本机没有可用的 node：{exc}")


def require_sidecar_installed() -> None:
    """边车依赖没装就 `skip`（安装需要外网，不能当作测试缺陷）。"""
    if not (SIDECAR_DIR / "node_modules").is_dir():  # pragma: no cover - 环境缺失时
        pytest.skip(
            f"边车依赖未安装（{SIDECAR_DIR / 'node_modules'} 不存在）："
            "先在该目录执行 npm install --no-audit --no-fund"
        )


def env_file_values() -> Dict[str, str]:
    return load_env_file(REPO_ROOT / ".env.local")


def route_credential_available(route: str) -> bool:
    entry = available_routes().get(route, {})
    return bool(entry.get("credential_available"))


def live_routes() -> List[str]:
    return [route for route in (DS_ROUTE, OA_ROUTE) if route_credential_available(route)]


def require_live_route(route: str) -> None:
    if not route_credential_available(route):
        pytest.skip(f"路由 {route} 的凭据不可用（环境变量未设置）")


def fake_config(**overrides: Any) -> CognitionConfig:
    """不带真凭据的配置；`base_url` 默认指向必然拒绝连接的端口。"""
    values: Dict[str, Any] = {
        "provider": "test-provider",
        "model": "test-model",
        "base_url": "http://127.0.0.1:1/v1",
        "route_name": "test",
        "api_key": FAKE_KEY,
        "timeout_seconds": 10.0,
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    values.update(overrides)
    if "model" in overrides and "model_version" not in overrides:
        values["model_version"] = overrides["model"]
    return CognitionConfig(**values)


def live_config(route: str = DS_ROUTE) -> CognitionConfig:
    return CognitionConfig.from_env(route=route)


def live_port(route: str = DS_ROUTE) -> PiSidecarCognitionPort:
    require_live_route(route)
    require_sidecar_installed()
    return PiSidecarCognitionPort(live_config(route))


def fake_port(**overrides: Any) -> PiSidecarCognitionPort:
    require_sidecar_installed()
    return PiSidecarCognitionPort(fake_config(**overrides))


# --------------------------------------------------------------------------- #
# 本地 mock OpenAI 兼容服务器
# --------------------------------------------------------------------------- #


class MockModel:
    """可编程的 OpenAI 兼容对端。

    `script` 里的每一项可以是：

    * `str`  —— 作为 assistant 的 `content` 返回（最常见的用法）；
    * `dict` —— 整份响应体原样返回；
    * `Callable[[dict], Tuple[int, dict]]` —— 自定义 `(状态码, 响应体)`。
    """

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.script: List[Any] = []
        self.default_content = '{"schema_version": "cognition-output/1", "claims": []}'
        self._lock = threading.Lock()

    def push(self, *items: Any) -> "MockModel":
        with self._lock:
            self.script.extend(items)
        return self

    def next_response(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            self.requests.append(payload)
            item = self.script.pop(0) if self.script else self.default_content
        if callable(item):
            return item(payload)
        if isinstance(item, dict):
            return 200, item
        return 200, completion_body(str(item))

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def last_request(self) -> Dict[str, Any]:
        return self.requests[-1]

    def system_prompt(self) -> str:
        for message in reversed(self.requests[-1].get("messages", [])):
            if message.get("role") == "system":
                return str(message.get("content") or "")
        return ""

    def user_prompt(self) -> str:
        texts: List[str] = []
        for message in self.requests[-1].get("messages", []):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    str(part.get("text") or "") for part in content if isinstance(part, dict)
                )
        return "\n".join(texts)

    def tools_field(self) -> Optional[List[Any]]:
        """请求体里的 `tools` 字段：`None` 或空列表都表示**零工具**。"""
        return self.requests[-1].get("tools")


def completion_body(content: str) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib 命名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {"_raw": raw.decode("utf-8", errors="replace")}
        mock: MockModel = self.server.mock  # type: ignore[attr-defined]
        status, body = mock.next_response(payload)

        if status == 200 and payload.get("stream"):
            # OpenAI SDK 在流式模式下要求 SSE：即使只有一条 chunk，也必须按流返回，
            # 否则客户端会以 "Stream ended without finish_reason" 结束。
            self._send_stream(body)
            return

        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_stream(self, body: Dict[str, Any]) -> None:
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        model = body.get("model", "mock-model")
        completion_id = body.get("id", "chatcmpl-mock")

        chunks = [
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": message.get("content") or ""},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
            },
        ]
        if body.get("usage") is not None:
            chunks.append(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [],
                    "usage": body["usage"],
                }
            )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: Any) -> None:
        return


class MockServer:
    """`with MockServer() as server: ...` —— 用完即关，端口由内核分配。"""

    def __init__(self) -> None:
        self.mock = MockModel()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.mock = self.mock  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.port = self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> "MockServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def mock_port(
    server: MockServer, *, cache: bool = False, **overrides: Any
) -> PiSidecarCognitionPort:
    """指向本地 mock 的端口；不发真实外网请求。

    `cache` 是**端口**的行为（不是配置），因此在这里单独取出。
    """
    require_sidecar_installed()
    values: Dict[str, Any] = {"base_url": server.base_url, "model": "mock-model"}
    values.update(overrides)
    return PiSidecarCognitionPort(fake_config(**values), cache=cache)
