"""T-003 认知层：PI 隔离与最小结构化调用（SPEC §2.14 / §4.7 / §5 登记 #4）。

**本包做什么**

1. 定义 `CognitionPort` —— 认知层的**稳定接口**（只输出 quote，不输出坐标）；
2. 提供它的一个适配器 `PiSidecarCognitionPort` —— 经 **pi-ai Node 边车**调用远端模型，
   **注册零工具**，因此没有执行面（§4.7 硬门槛**由构造满足**，不需要容器/微VM）；
3. 把 provider / model / 凭据做成**配置**（默认 DeepSeek 官方端点，可切到环境内兼容凭据）；
4. 把结构化输出做成**冻结契约**（`extra="forbid"`），并把三种**已实测**的失败
   （围栏包裹 / 模型下架 / 端点不可达）全部纳入显式的降级或响亮失败路径。

**降级 = "未分类"，绝不是猜测**（§2.14 决策四）：`CognitionResult.is_unclassified`
为真时 `claims` 恒为空，且 `record.reason` 必非空。

**零新增 Python 依赖**：本包只用 stdlib + 已声明的 pydantic。Node 依赖被关在
`sidecar/node_modules`（不进 git，不在 Python 依赖图里）。
"""

from __future__ import annotations

from .adapter import FRAME_PREFIX, PROTOCOL, PiSidecarCognitionPort
from .config import (
    CONFIG_VERSION,
    DEFAULT_NO_PROXY,
    DEFAULT_ROUTE,
    DEFAULT_TIMEOUT_SECONDS,
    DS_API_KEY_ENV,
    DS_BASE_URL,
    DS_MODEL,
    DS_PROVIDER,
    DS_ROUTE,
    OA_API_KEY_ENV,
    OA_BASE_URL,
    OA_MODEL,
    OA_PROVIDER,
    OA_ROUTE,
    PROMPT_VERSION,
    CognitionConfig,
    available_routes,
    load_env_file,
    repo_root,
)
from .contracts import (
    SCHEMA_VERSION,
    CallStatus,
    CognitionCallRecord,
    CognitionOutput,
    CognitionRequest,
    CognitionResult,
    CognitionUsage,
    DegradeReason,
    ExtractedClaim,
)
from .errors import (
    CognitionError,
    CognitionPortError,
    ConfigError,
    IsolationViolationError,
    ModelEnvelopeError,
    ModelOutputError,
    ProtocolError,
    SidecarUnavailableError,
)
from .node import MINIMUM_NODE_VERSION, NodeBinary, resolve_node
from .parse import Extracted, extract_single_json, normalize_model_text
from .port import CognitionPort
from .prompt import build_system_prompt, build_user_content, truncate_content

__all__ = [
    "CONFIG_VERSION",
    "CallStatus",
    "CognitionCallRecord",
    "CognitionConfig",
    "CognitionError",
    "CognitionOutput",
    "CognitionPort",
    "CognitionPortError",
    "CognitionRequest",
    "CognitionResult",
    "CognitionUsage",
    "ConfigError",
    "DEFAULT_NO_PROXY",
    "DEFAULT_ROUTE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DS_API_KEY_ENV",
    "DS_BASE_URL",
    "DS_MODEL",
    "DS_PROVIDER",
    "DS_ROUTE",
    "DegradeReason",
    "Extracted",
    "ExtractedClaim",
    "FRAME_PREFIX",
    "IsolationViolationError",
    "MINIMUM_NODE_VERSION",
    "ModelEnvelopeError",
    "ModelOutputError",
    "NodeBinary",
    "OA_API_KEY_ENV",
    "OA_BASE_URL",
    "OA_MODEL",
    "OA_PROVIDER",
    "OA_ROUTE",
    "PROMPT_VERSION",
    "PROTOCOL",
    "PiSidecarCognitionPort",
    "ProtocolError",
    "SCHEMA_VERSION",
    "SidecarUnavailableError",
    "available_routes",
    "build_system_prompt",
    "build_user_content",
    "extract_single_json",
    "load_env_file",
    "normalize_model_text",
    "repo_root",
    "resolve_node",
    "truncate_content",
]
