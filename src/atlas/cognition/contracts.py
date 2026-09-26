"""T-003 认知层冻结契约（SPEC §2.2 / §2.14 / §3）。

三件事在这里被**代码层**强制，而不是靠约定：

1. **PI 只输出 `quote`，不输出坐标**（SPEC §2.2）。
   `ExtractedClaim` 继承 `ContractModel`（`extra="forbid"`），因此
   `ExtractedClaim(..., char_start=3)` 在类型层根本构造不出来——这正是
   `atlas.contracts.base.ContractModel` 存在的理由。
2. **每次调用携带 `(code_version, config_version, model_version)`**（SPEC §3 / §5 登记 #4），
   以及 `model` / `prompt_version`。
3. **降级是显式的一等状态**（SPEC §2.14 决策四）：`CognitionResult.status` 只有
   `ok` / `unclassified` 两种取值，`unclassified` 必须带 `reason`，**没有**"猜一个"的路径。

`CognitionResult` / `CognitionCallRecord` 为普通冻结 dataclass（而非 pydantic 模型）：
它们是本包的**内部调用记录**，不是需要 `json_schema` 的对外契约；
用 dataclass 可以让密钥相关字段永不进入序列化路径。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pydantic import Field, ValidationError, model_validator

from atlas.contracts.base import ContractModel

from .errors import ModelEnvelopeError

__all__ = [
    "CallStatus",
    "CognitionCallRecord",
    "CognitionOutput",
    "CognitionRequest",
    "CognitionResult",
    "CognitionUsage",
    "DegradeReason",
    "ExtractedClaim",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = "cognition-output/1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DegradeReason(str, Enum):
    """降级原因码（`status == "unclassified"` 时**必有**，且必须来自这个闭集）。

    `tests/test_cognition_degrade.py` 断言每个取值都被至少一条测试覆盖——
    防止"加了一个静默吞掉的失败分支"。
    """

    #: 端点不可达 / 连接被拒 / DNS 失败（SPEC §2.14 决策三第 2 类）
    UNREACHABLE_MODEL = "unreachable_model"
    #: 超时（含边车自身看门狗超时）
    TIMEOUT = "timeout"
    #: 任意非 2xx（含 402 余额、429 限流、5xx 服务端错误、404 模型下架）
    HTTP_ERROR = "http_error"
    #: 模型下架（HTTP 404 且响应体明确说 deprecated；SPEC §2.14 决策三第 1 类）
    MODEL_DEPRECATED = "model_deprecated"
    #: 2xx 但没有任何可用文本（例如只返回 reasoning）
    EMPTY_COMPLETION = "empty_completion"
    #: 有文本，但抽不出"恰好一个完整 JSON 值"（围栏 / 说明文字 / 截断 / 多段）
    UNPARSEABLE_OUTPUT = "unparseable_output"
    #: 边车报告自身错误（协议帧内 error）——只在**没有**更具体原因时使用
    SIDECAR_ERROR = "sidecar_error"


class CallStatus(str, Enum):
    """调用状态。只有两种，且 `UNCLASSIFIED` **必须**带原因。"""

    OK = "ok"
    UNCLASSIFIED = "unclassified"


# --------------------------------------------------------------------------- #
# 模型输出契约（冻结、字段闭合）
# --------------------------------------------------------------------------- #


class ExtractedClaim(ContractModel):
    """PI 的一条抽取结果：**只带 quote（文字），不带坐标**（SPEC §2.2）。

    `char_start` / `char_end` / `block_id` 之类的字段在这里**不存在**，
    因此模型或代码都无法把坐标塞进来——坐标只能由 T-107 的确定性匹配产生。
    """

    kind: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)


class CognitionOutput(ContractModel):
    """模型必须在一次调用里返回的**唯一** JSON 对象。

    形状刻意只有一个键：`{"claims": [...]}`。多一个键即 `extra="forbid"` 拒绝，
    少一个键即缺字段拒绝——两者都**响亮失败**（`ModelEnvelopeError`），
    不允许"宽容地只取认识的字段"。
    """

    schema_version: str = SCHEMA_VERSION
    claims: List[ExtractedClaim]

    @model_validator(mode="after")
    def _check_schema_version(self) -> "CognitionOutput":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version 必须是 {SCHEMA_VERSION!r}，收到 {self.schema_version!r}"
            )
        return self

    @classmethod
    def parse(cls, payload: Any) -> "CognitionOutput":
        """把已解析的 JSON 值收进冻结契约；结构非法时**响亮失败**。"""
        if not isinstance(payload, Mapping):
            raise ModelEnvelopeError(
                f"模型输出必须是 JSON 对象，收到 {type(payload).__name__}"
            )
        # 兼容"schema_version 省略"的常见形态？不——省了就缺字段，响亮失败。
        try:
            return cls.model_validate(dict(payload))
        except ValidationError as exc:
            raise ModelEnvelopeError(
                "模型输出违反 output 契约（缺字段 / 多余字段 / 类型或取值非法）："
                + _summarize(exc)
            ) from exc


def _summarize(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{location or '<root>'}: {error.get('msg')}")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# 调用记录
# --------------------------------------------------------------------------- #


def canonical_digest(payload: Any) -> str:
    """规范化 JSON 的 sha256：键序无关，用于幂等键。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CognitionRequest:
    """一次认知层调用的输入快照（SPEC §3：输入快照 + 配置快照）。

    `external_content` 是**不可信外部内容**——它只被当作数据送进模型，
    永不进入命令行、永不 `eval`、永不作为路径。边车侧注册零工具，
    因此即便模型被这段文字说服，也没有任何可调用的执行面。
    """

    raw_id: str
    external_content: str
    instruction: str = ""
    candidate_labels: Tuple[str, ...] = ()
    kind: str = "industry"

    def __post_init__(self) -> None:
        if not self.raw_id:
            raise ValueError("raw_id 不得为空")
        if not self.external_content:
            raise ValueError("external_content 不得为空（没有内容就没有可抽取的证据）")

    def digest(self) -> str:
        return canonical_digest(
            {
                "raw_id": self.raw_id,
                "external_content": self.external_content,
                "instruction": self.instruction,
                "candidate_labels": list(self.candidate_labels),
                "kind": self.kind,
            }
        )


@dataclass(frozen=True)
class CognitionUsage:
    """token 计数（来自 pi-ai 的 `Usage`；provider 不报则为 `None`）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_total: Optional[float] = None
    cost_currency: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cost_total": self.cost_total,
            "cost_currency": self.cost_currency,
        }


@dataclass(frozen=True)
class CognitionCallRecord:
    """一次调用的可审计记录（SPEC §3 / §5 登记 #4）。

    记录里**没有**密钥，也**没有**原始外部内容（只留摘要），因此可以安全落库 / 打日志。
    """

    status: CallStatus
    reason: Optional[DegradeReason]
    detail: str
    provider: str
    model: str
    response_model: str
    credential_route: str
    sidecar_code_version: str
    code_version: str
    config_version: str
    model_version: str
    config_digest: str
    idempotency_key: str
    input_digest: str
    elapsed_ms: int
    usage: CognitionUsage
    parse_strategy: Optional[str]
    degraded: bool
    tool_calls: int
    tools_declared: int
    thinking_chars: int
    called_at: datetime = field(default_factory=_utcnow)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "provider": self.provider,
            "model": self.model,
            "response_model": self.response_model,
            "credential_route": self.credential_route,
            "sidecar_code_version": self.sidecar_code_version,
            "code_version": self.code_version,
            "config_version": self.config_version,
            "model_version": self.model_version,
            "config_digest": self.config_digest,
            "idempotency_key": self.idempotency_key,
            "input_digest": self.input_digest,
            "elapsed_ms": self.elapsed_ms,
            "usage": self.usage.as_dict(),
            "parse_strategy": self.parse_strategy,
            "degraded": self.degraded,
            "tool_calls": self.tool_calls,
            "tools_declared": self.tools_declared,
            "thinking_chars": self.thinking_chars,
            "called_at": self.called_at.isoformat(),
        }

    def versions_dict(self) -> Dict[str, str]:
        """SPEC §3 要求的版本三元组。"""
        return {
            "code_version": self.code_version,
            "config_version": self.config_version,
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class CognitionResult:
    """一次认知层调用的结果。

    不变量（构造期强制，见 `__post_init__`）：
    `ok` 必须带 output；`unclassified` 必须带 reason 且 **必须**没有 output
    ——"降级"永远不是"猜一个出来"。
    """

    record: CognitionCallRecord
    output: Optional[CognitionOutput] = None

    def __post_init__(self) -> None:
        if self.record.status is CallStatus.OK and self.output is None:
            raise ValueError("status=ok 必须携带 output")
        if self.record.status is CallStatus.UNCLASSIFIED:
            if self.record.reason is None:
                raise ValueError("status=unclassified 必须携带 reason")
            if self.output is not None:
                raise ValueError("status=unclassified 不得携带 output（降级不是猜测）")

    @property
    def status(self) -> CallStatus:
        return self.record.status

    @property
    def reason(self) -> Optional[DegradeReason]:
        return self.record.reason

    @property
    def is_unclassified(self) -> bool:
        return self.record.status is CallStatus.UNCLASSIFIED

    @property
    def claims(self) -> Tuple[ExtractedClaim, ...]:
        if self.output is None:
            return ()
        return tuple(self.output.claims)
