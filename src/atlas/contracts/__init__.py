"""Atlas 领域契约（SPEC §2、§3、§4.1 T-002）。

这一层只定义**契约与不变量**，不含 I/O：

- 三态：`RawRecord` / `ProposedClaim` / `ConfirmedLabel`（含契约级 Store）
- 证据锚点：`EvidenceAnchor`（真值）/ `DerivedLocator`（派生）
- 任务规范：`AtlasTask` / `TaskVersions` / `Snapshot` / `TaskOutput`
- ID 与版本策略：`ids`

存储实现在 T-103 / T-108（文件系统 + stdlib `sqlite3`，见 SPEC §2.10）。
"""

from .anchors import (
    DerivedLocator,
    EvidenceAnchor,
    VerificationStatus,
    build_anchor,
    match_quote,
)
from .base import ContractModel
from .errors import (
    AnchorError,
    ContractError,
    EvidenceMismatchError,
    IdError,
    ImmutabilityError,
    InvalidContractStateError,
    NotFoundError,
    TaskContractError,
    UnverifiedEvidenceError,
    VersionError,
)
from .ids import (
    claim_id_for,
    content_sha256,
    label_id_for,
    normalize_for_id,
    raw_id_for,
)
from .states import (
    ConfirmedLabel,
    ConfirmedStore,
    ProposedClaim,
    ProposedStore,
    RawRecord,
    RawStore,
)
from .tasks import AtlasTask, Snapshot, TaskOutput, TaskVersions

__all__ = [
    # anchors
    "DerivedLocator",
    "EvidenceAnchor",
    "VerificationStatus",
    "build_anchor",
    "match_quote",
    # base
    "ContractModel",
    # errors
    "AnchorError",
    "ContractError",
    "EvidenceMismatchError",
    "IdError",
    "ImmutabilityError",
    "InvalidContractStateError",
    "NotFoundError",
    "TaskContractError",
    "UnverifiedEvidenceError",
    "VersionError",
    # ids
    "claim_id_for",
    "content_sha256",
    "label_id_for",
    "normalize_for_id",
    "raw_id_for",
    # states
    "ConfirmedLabel",
    "ConfirmedStore",
    "ProposedClaim",
    "ProposedStore",
    "RawRecord",
    "RawStore",
    # tasks
    "AtlasTask",
    "Snapshot",
    "TaskOutput",
    "TaskVersions",
]
