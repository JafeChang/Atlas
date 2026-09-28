"""T-003 认知层配置：provider / model / 凭据**是配置，不是代码**（SPEC §2.14 决策二）。

设计要点

1. **`frozen=True` + `repr=False` 的密钥字段**：`api_key` 标了 `field(repr=False)`，
   因此配置对象即使被打进日志、异常或 `pytest` 的断言输出，也**不会**泄露密钥。
2. **凭据只从环境变量读**：`CognitionConfig.from_env(...)` 按 `api_key_env` 指定的变量名
   取值，绝不硬编码、绝不读 harness 的 `~/.dsh/.credentials.yaml`（那不是项目凭据）。
3. **切官方 DeepSeek 端点 = 纯配置变更**：改 `provider` / `base_url` / `model` /
   `api_key_env` 四个值即可，不需要改任何代码路径。
4. `config_version` / `model_version` 参与幂等键（SPEC §3），因此"同输入 + 同配置 → 同输出"
   在配置或版本变化时**必然**产生新的幂等键。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import ConfigError

__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_ROUTE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_NO_PROXY",
    "DEFAULT_HOST_ENV_ALLOWLIST",
    "DS_API_KEY_ENV",
    "DS_BASE_URL",
    "DS_MODEL",
    "DS_PROVIDER",
    "DS_ROUTE",
    "OA_API_KEY_ENV",
    "OA_BASE_URL",
    "OA_MODEL",
    "OA_PROVIDER",
    "OA_ROUTE",
    "PROMPT_VERSION",
    "CognitionConfig",
    "available_routes",
    "load_env_file",
    "repo_root",
]

CONFIG_VERSION = "cognition-config/1"
PROMPT_VERSION = "cognition-extract-prompt/1"

#: 路由 A（**默认**）：DeepSeek 官方端点。目标模型 `deepseek-flash` 是官方合法 id。
#: 需要独立的 `DEEPSEEK_API_KEY`，**不**从 harness 的凭据库借用（SPEC §2.14 决策二）。
DS_ROUTE = "deepseek"
DS_PROVIDER = "deepseek"
DS_MODEL = "deepseek-flash"
DS_BASE_URL = "https://api.deepseek.com"
DS_API_KEY_ENV = "DEEPSEEK_API_KEY"

#: 路由 B（回退）：环境内已有的 OpenAI 兼容凭据。
#: 实测可达 `deepseek/deepseek-v4-flash`（HTTP 200 + 合法 JSON）。
#: ⚠️ 环境里的 `ATLAS_LLM_MODEL` 目前指向 `xiaomi/mimo-v2-flash:free`，该模型已被下架
#: （HTTP 404 `This model has been deprecated`）——这正是 §2.14 决策三第 1 类的实测形态。
#: 因此**代码默认值**用一个确认可用的模型；若环境变量显式指定了已下架的模型，
#: 调用会**如实降级**为 `model_deprecated`（不静默换模型）。
OA_ROUTE = "openai-compatible"
OA_PROVIDER = "atlas-openai-compatible"
OA_MODEL = "deepseek/deepseek-v4-flash"
OA_BASE_URL = "https://openrouter.ai/api/v1"
OA_API_KEY_ENV = "ATLAS_OPENAI_API_KEY"

DEFAULT_ROUTE = DS_ROUTE
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_NO_PROXY = "127.0.0.1,localhost,::1"
DEFAULT_HOST_ENV_ALLOWLIST: tuple[str, ...] = ()

#: 显式覆盖路由的变量名。
_ROUTE_ENV = ("ATLAS_COGNITION_ROUTE",)
_DS_BASE_URL_ENV = ("DEEPSEEK_BASE_URL",)
_DS_MODEL_ENV = ("DEEPSEEK_MODEL",)
_DS_KEY_ENV = ("DEEPSEEK_API_KEY",)
_OA_BASE_URL_ENV = ("ATLAS_LLM_BASE_URL", "ATLAS_OPENAI_BASE_URL")
_OA_MODEL_ENV = ("ATLAS_LLM_MODEL", "ATLAS_OPENAI_MODEL")
_OA_KEY_ENV = ("ATLAS_OPENAI_API_KEY",)
_TIMEOUT_ENV = ("ATLAS_LLM_TIMEOUT",)
_PROXY_ENV = ("ATLAS_COGNITION_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")

#: 官方端点的主机名（用于判别路由敏感默认值，例如官方用 `DEEPSEEK_API_KEY`）。
_OFFICIAL_HOSTS = ("api.deepseek.com",)


def repo_root() -> Optional[Path]:
    """从本文件向上找到含 `pyproject.toml` 的目录；找不到返回 `None`。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """解析 KEY=VALUE 文件；**不写回** `os.environ`（调用方显式取用）。

    支持：`#` 注释行、`export ` 前缀、成对引号、行尾 `#` 注释（引号内的 `#` 保留）。
    这是本仓库 `.env.local` 的实际格式（值带行尾中文注释）。
    """
    values: dict[str, str] = {}
    file = Path(path)
    if not file.is_file():
        return values
    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        quote: str | None = None
        cut = len(value)
        for index, char in enumerate(value):
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "#":
                cut = index
                break
        value = value[:cut].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _env_lookup(env: Mapping[str, str], env_file: Mapping[str, str], *names: str) -> Optional[str]:
    """先看进程环境，再看 `.env.local`（进程环境优先，符合 12-factor 惯例）。"""
    for name in names:
        value = env.get(name)
        if value:
            return value
        value = env_file.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class CognitionConfig:
    """一次认知层调用的**全部**外部依赖。

    冻结：改配置必须用 `with_*` 造新实例，因此幂等键不会因就地修改而失效。
    """

    provider: str = DS_PROVIDER
    model: str = DS_MODEL
    base_url: str = DS_BASE_URL
    route_name: str = DEFAULT_ROUTE
    api_key: str = field(default="", repr=False)
    api_key_env: str = DS_API_KEY_ENV
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    temperature: float = 0.0
    #: 单次调用的输出预算（经 `max_completion_tokens` 真正发到 HTTP 请求体）。
    #:
    #: **4096 是实测标定的，不是拍的**（SPEC §2.17 / `tools/t105_yield_probe.py`）：
    #: `deepseek-flash` 的 reasoning token 与 JSON 答案**共用**这一份预算，
    #: 实测 reasoning 占 output 的 **87–92%**。在 **2048** 下 25 次真实调用里有
    #: **4 次**被截断（`output_tokens == reasoning_tokens == 2048`、
    #: `stopReason=length`、内容为 0 ⇒ 降级 `empty_completion`），
    #: 单元级产出率 **10/25 = 40%**；抬到 **4096** 后同样 25 个单元
    #: **0 次截断**、产出率 **14/25 = 56%**；抬到 **8192** 是 **15/25 = 60%**
    #: （只多 1 个单元，单轮 25 个样本里不构成差异）。
    #: 允许跑完的调用里最大一次 output 是 **2384** token ⇒ 4096 留了约 1.7× 余量，
    #: 因此取**最小且实测零截断**的那个值，而不是越大越好。
    #: ⚠️ 这个字段**不进** `plan_digest`（见 `versions()`），改它**不会**让已跑过的
    #: 单元重跑；改它必须重跑 `tools/t105_real_evidence.py`。
    max_output_tokens: int = 4096
    reasoning_enabled: bool = False
    proxy_url: str = field(default="", repr=False)
    no_proxy: str = DEFAULT_NO_PROXY
    node_bin: str = ""
    host_env_allowlist: tuple[str, ...] = DEFAULT_HOST_ENV_ALLOWLIST
    config_version: str = CONFIG_VERSION
    model_version: str = DS_MODEL
    prompt_version: str = PROMPT_VERSION
    max_context_chars: int = 12000

    def __post_init__(self) -> None:
        if not self.provider:
            raise ConfigError("provider 不得为空")
        if not self.model:
            raise ConfigError("model 不得为空")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base_url 必须是 http(s)：{self.base_url!r}")
        if self.timeout_seconds <= 0:
            raise ConfigError(f"timeout_seconds 必须为正：{self.timeout_seconds!r}")
        if not 0.0 <= self.temperature <= 2.0:
            raise ConfigError(f"temperature 必须在 [0, 2]：{self.temperature!r}")
        if self.max_output_tokens <= 0:
            raise ConfigError(f"max_output_tokens 必须为正：{self.max_output_tokens!r}")
        if self.max_context_chars <= 0:
            raise ConfigError(f"max_context_chars 必须为正：{self.max_context_chars!r}")

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def from_env(
        cls,
        *,
        env: Optional[Mapping[str, str]] = None,
        env_file: Optional[str | os.PathLike[str]] = None,
        route: Optional[str] = None,
        require_key: bool = True,
        **overrides: Any,
    ) -> "CognitionConfig":
        """从进程环境（可选叠加 `.env.local`）构造配置。

        路由解析（`route` 显式参数 > `ATLAS_COGNITION_ROUTE` > 默认）：

        * `"deepseek"`（**默认**）→ `api.deepseek.com` + `DEEPSEEK_API_KEY` + `deepseek-flash`
        * `"openai-compatible"` → `.env.local` 里的 `ATLAS_OPENAI_API_KEY` + `ATLAS_LLM_BASE_URL`

        `require_key=False` 用于"只想知道配置长什么样"的场景（例如测试里验证路由切换）；
        真正调用模型时缺 key 会**响亮失败**（`ConfigError`）。
        """
        environ = dict(os.environ if env is None else env)
        file_values: dict[str, str] = {}
        if env_file is not None:
            file_values = load_env_file(env_file)
        elif env is None:
            root = repo_root()
            if root is not None:
                file_values = load_env_file(root / ".env.local")

        selected = (
            route
            or overrides.pop("route_name", None)
            or _env_lookup(environ, file_values, *_ROUTE_ENV)
            or DEFAULT_ROUTE
        ).strip()
        if selected not in (DS_ROUTE, OA_ROUTE):
            raise ConfigError(
                f"未知的认知层路由 {selected!r}；可用：{DS_ROUTE!r} / {OA_ROUTE!r}"
                f"（通过参数 route= 或环境变量 {_ROUTE_ENV[0]} 指定）"
            )

        if selected == DS_ROUTE:
            base_url = (
                _env_lookup(environ, file_values, *_DS_BASE_URL_ENV) or DS_BASE_URL
            )
            model = _env_lookup(environ, file_values, *_DS_MODEL_ENV) or DS_MODEL
            default_key_env = DS_API_KEY_ENV
            default_provider = DS_PROVIDER
        else:
            base_url = (
                _env_lookup(environ, file_values, *_OA_BASE_URL_ENV) or OA_BASE_URL
            )
            model = _env_lookup(environ, file_values, *_OA_MODEL_ENV) or OA_MODEL
            default_key_env = OA_API_KEY_ENV
            default_provider = OA_PROVIDER

        api_key_env = overrides.pop("api_key_env", None)
        raw_timeout = _env_lookup(environ, file_values, *_TIMEOUT_ENV)
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        proxy = _env_lookup(environ, file_values, *_PROXY_ENV) or ""

        values: dict[str, Any] = {
            "route_name": selected,
            "base_url": base_url,
            "model": model,
            "timeout_seconds": timeout,
            "proxy_url": proxy,
        }
        if api_key_env:
            values["api_key_env"] = api_key_env
        override_provider = overrides.pop("provider", None)
        values["provider"] = override_provider or default_provider

        values.update(overrides)

        # 凭据变量名在**最终** base_url 确定之后才判定：显式 override 到官方端点时
        # 必须自动改用 `DEEPSEEK_API_KEY`，否则会拿错钥匙。
        if "api_key_env" not in values:
            values["api_key_env"] = cls._default_key_env(values["base_url"])
        # `model_version` 默认跟随最终 model（除非调用方显式指定）。
        if "model_version" not in overrides:
            values["model_version"] = values["model"]

        final_key_env = values["api_key_env"]
        values["api_key"] = (
            environ.get(final_key_env) or file_values.get(final_key_env) or ""
        )

        config = cls(**values)
        if require_key and not config.api_key:
            raise ConfigError(
                f"缺少 API key：环境变量 {config.api_key_env} 为空。"
                f"（当前路由 {config.route_name!r} → {config.base_url}）"
                "凭据必须通过环境变量提供（绝不写入代码 / 提交 / 日志）。"
                "官方端点用 DEEPSEEK_API_KEY；环境内兼容凭据用 ATLAS_OPENAI_API_KEY。"
            )
        return config

    @staticmethod
    def _default_key_env(base_url: str) -> str:
        """官方 DeepSeek 端点用**独立的** `DEEPSEEK_API_KEY`，不与环境内兼容凭据混用。"""
        host = base_url.lower()
        if any(official in host for official in _OFFICIAL_HOSTS):
            return DS_API_KEY_ENV
        return OA_API_KEY_ENV

    def with_overrides(self, **overrides: Any) -> "CognitionConfig":
        return replace(self, **overrides)

    # ------------------------------------------------------------------ #
    # 版本与幂等
    # ------------------------------------------------------------------ #

    def versions(self) -> dict[str, str]:
        """`(code_version, config_version, model_version)` 三元组（SPEC §3 / §5 登记 #4）。

        `code_version` 由 `prompt_version` 承载——本包唯一的"代码即契约"部分是 prompt 模板。
        """
        return {
            "code_version": self.prompt_version,
            "config_version": self.config_version,
            "model_version": self.model_version,
        }

    def credential_route(self) -> str:
        """**不含密钥**的凭据路由标识，写进调用记录，使"这条结果走的是哪条路"可审计。

        形如 `deepseek@api.deepseek.com#<key 指纹前 16 位>`；指纹不可反推密钥。
        """
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0]
        if not self.api_key:
            return f"{self.route_name}@{host}#no-key"
        return f"{self.route_name}@{host}#{self.api_key_fingerprint()}"

    def public_digest(self) -> str:
        """不含密钥的配置指纹（规范化 JSON 的 sha256）。

        **密钥不进摘要**：摘要会被写进调用记录与幂等键，密钥一旦进入就等于二次泄露面。
        密钥变化通过 `api_key_fingerprint` 单独体现。
        """
        payload = {
            "route_name": self.route_name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_enabled": self.reasoning_enabled,
            "no_proxy": self.no_proxy,
            "host_env_allowlist": list(self.host_env_allowlist),
            "max_context_chars": self.max_context_chars,
            **self.versions(),
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def api_key_fingerprint(self) -> str:
        """密钥的 sha256 前 16 位（可比较"是不是同一把钥匙"，不可反推）。"""
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:16]


def available_routes(
    *,
    env: Optional[Mapping[str, str]] = None,
    env_file: Optional[str | os.PathLike[str]] = None,
) -> dict[str, dict[str, Any]]:
    """列出两条路由的当前配置以及凭据是否就位（**不含密钥**）。

    给运维/测试用：一眼看出"官方端点是否可用、将回退到哪条路"。
    """
    out: dict[str, dict[str, Any]] = {}
    for name in (DS_ROUTE, OA_ROUTE):
        try:
            config = CognitionConfig.from_env(
                env=env, env_file=env_file, route=name, require_key=False
            )
        except ConfigError as exc:  # pragma: no cover - 仅当路由常量被改坏
            out[name] = {"error": str(exc)}
            continue
        out[name] = {
            "provider": config.provider,
            "model": config.model,
            "base_url": config.base_url,
            "api_key_env": config.api_key_env,
            "credential_available": bool(config.api_key),
        }
    return out
