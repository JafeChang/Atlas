"""T-003 / T-105 认知层（SPEC §2.14 / §4.2）。

**T-003 做什么**

1. 定义 `CognitionPort` —— 认知层的**稳定接口**（只输出 quote，不输出坐标）；
2. 提供它的一个适配器 `PiSidecarCognitionPort` —— 经 **pi-ai Node 边车**调用远端模型，
   **注册零工具**，因此没有执行面（§4.7 硬门槛**由构造满足**，不需要容器/微VM）；
3. 把 provider / model / 凭据做成**配置**（默认 DeepSeek 官方端点，可切到环境内兼容凭据）；
4. 把结构化输出做成**冻结契约**（`extra="forbid"`），并把三种**已实测**的失败
   （围栏包裹 / 模型下架 / 端点不可达）全部纳入显式的降级或响亮失败路径。

**T-105 做什么**（本包的新增部分，见 `classify` / `propose` / `store` 三个模块）

1. `classify` —— **前端分流**：raw 是异质的（SPEC §6.3），先判种类再决定
   "分类单元是条目还是整篇文档"；不是 feed 的纯文本当整篇分类，HTML/JSON **跳过并给理由**；
2. `propose` —— **分类与提议**：批次调用（摊薄边车启动开销）、quote 确定性归属、
   "没返回"逐单元记账、降级 = 未分类；`LabelSpace` 是**注入**参数（§2.9 闭环，
   本包**不 import** `atlas.registry`）；
3. `store` —— `proposed_claims` + `proposal_runs` 两张表的 SQLite 持久化
   （可覆写 = 追加新版本 + 保留版本链；只增不改由触发器强制）。

**降级 = "未分类"，绝不是猜测**（§2.14 决策四）：`CognitionResult.is_unclassified`
为真时 `claims` 恒为空，且 `record.reason` 必非空；T-105 的 `proposed_claims` 上
同样有 `CHECK` 强制"未分类行没有取值/引用且必有理由"。

**零新增 Python 依赖**：本包只用 stdlib + 已声明的 pydantic。Node 依赖被关在
`sidecar/node_modules`（不进 git，不在 Python 依赖图里）。
"""

from __future__ import annotations

from .adapter import FRAME_PREFIX, PROTOCOL, PiSidecarCognitionPort
from .classify import (
    ARTICLE_ID_PREFIX,
    BATCH_MAX_CHARS,
    BATCH_MAX_UNITS,
    UNIT_TEXT_MAX_CHARS,
    ClassificationError,
    ClassifiedUnit,
    DocumentKind,
    DocumentPlan,
    LabelSpace,
    SkipReason,
    Unit,
    UnitKind,
    batch_key_for,
    classify_document,
    plan_batches,
    reduce_text,
    unit_id_for_article,
)
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
from .propose import (
    ATTRIBUTION_STATUS_ATTRIBUTED,
    ATTRIBUTION_STATUS_MULTIPLE,
    ATTRIBUTION_STATUS_NONE,
    REASON_LABEL_OUT_OF_SPACE,
    REASON_NO_CLAIM,
    REASON_UNATTRIBUTED_QUOTE,
    UNATTRIBUTED_UNIT_PREFIX,
    AttributedClaim,
    Attribution,
    BatchOutcome,
    BatchRequest,
    CognitionPortLike,
    OutcomeCounters,
    ProposalOutcome,
    ProposalPolicy,
    UnattributedClaim,
    attribute_claims,
    build_batch_content,
    build_batch_instruction,
    build_batches,
    propose_document,
    propose_documents,
    propose_units,
    run_batch,
)
from .store import (
    CLAIM_KEY_PREFIX,
    CLAIM_STATUS_CLASSIFIED,
    CLAIM_STATUS_OUT_OF_SPACE,
    CLAIM_STATUS_UNATTRIBUTED,
    CLAIM_STATUS_UNCLASSIFIED,
    CLAIM_STATUSES,
    KIND_INDUSTRY,
    ProposalRunRow,
    ProposedClaimRow,
    ProposedStoreError,
    SqliteProposedStore,
    batch_id_for,
    claim_key_for,
    label_space_version,
    open_proposed_store,
    output_digest_for,
    plan_digest_for,
    unit_row_fields,
)

__all__ = [
    "ARTICLE_ID_PREFIX",
    "ATTRIBUTION_STATUS_ATTRIBUTED",
    "ATTRIBUTION_STATUS_MULTIPLE",
    "ATTRIBUTION_STATUS_NONE",
    "BATCH_MAX_CHARS",
    "BATCH_MAX_UNITS",
    "CLAIM_KEY_PREFIX",
    "CLAIM_STATUSES",
    "CLAIM_STATUS_CLASSIFIED",
    "CLAIM_STATUS_OUT_OF_SPACE",
    "CLAIM_STATUS_UNATTRIBUTED",
    "CLAIM_STATUS_UNCLASSIFIED",
    "CONFIG_VERSION",
    "CallStatus",
    "ClassificationError",
    "ClassifiedUnit",
    "CognitionCallRecord",
    "CognitionConfig",
    "CognitionError",
    "CognitionOutput",
    "CognitionPort",
    "CognitionPortError",
    "CognitionPortLike",
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
    "DocumentKind",
    "DocumentPlan",
    "Extracted",
    "ExtractedClaim",
    "FRAME_PREFIX",
    "IsolationViolationError",
    "KIND_INDUSTRY",
    "LabelSpace",
    "MINIMUM_NODE_VERSION",
    "ModelEnvelopeError",
    "ModelOutputError",
    "NodeBinary",
    "OA_API_KEY_ENV",
    "OA_BASE_URL",
    "OA_MODEL",
    "OA_PROVIDER",
    "OA_ROUTE",
    "OutcomeCounters",
    "PROMPT_VERSION",
    "PROTOCOL",
    "PiSidecarCognitionPort",
    "ProposalOutcome",
    "ProposalPolicy",
    "ProposalRunRow",
    "ProposedClaimRow",
    "ProposedStoreError",
    "ProtocolError",
    "REASON_LABEL_OUT_OF_SPACE",
    "REASON_NO_CLAIM",
    "REASON_UNATTRIBUTED_QUOTE",
    "SCHEMA_VERSION",
    "SidecarUnavailableError",
    "SkipReason",
    "SqliteProposedStore",
    "UNIT_TEXT_MAX_CHARS",
    "UNATTRIBUTED_UNIT_PREFIX",
    "Unit",
    "UnitKind",
    "AttributedClaim",
    "Attribution",
    "BatchOutcome",
    "BatchRequest",
    "UnattributedClaim",
    "attribute_claims",
    "available_routes",
    "batch_id_for",
    "batch_key_for",
    "build_batch_content",
    "build_batch_instruction",
    "build_batches",
    "build_system_prompt",
    "build_user_content",
    "claim_key_for",
    "classify_document",
    "extract_single_json",
    "label_space_version",
    "load_env_file",
    "normalize_model_text",
    "open_proposed_store",
    "output_digest_for",
    "plan_batches",
    "plan_digest_for",
    "propose_document",
    "propose_documents",
    "propose_units",
    "reduce_text",
    "repo_root",
    "resolve_node",
    "run_batch",
    "truncate_content",
    "unit_id_for_article",
    "unit_row_fields",
]
