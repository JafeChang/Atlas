"""Atlas 证据校验与高亮展示（SPEC §2.2 方案 2C / §4.2 T-107）。

职责边界（§4.0 包布局）：本包**只**负责把机器产出的 quote 变成经过确定性校验的
原文锚点，并把它落成 `evidence_spans`（§2.10 表归属）+ 产出 T-109 渲染高亮所需的载荷。

```python
from atlas.evidence import SqliteEvidenceStore, verify_claim, build_highlight

outcome = verify_claim(claim, raw_bytes, content_type="text/html")   # 坐标只能由这里算出
if outcome.status is VerificationStatus.VERIFIED:
    verified = outcome.apply_to(claim)          # 校验后的 claim 才能进 Confirmed（T-108）
    payload = build_highlight(outcome.verification)   # T-109 用的高亮载荷

store = SqliteEvidenceStore(db_path)            # evidence_spans：只增不改
store.record(outcome)                           # FAILED → 不写任何东西
```

不变量（在代码层强制，不是约定）：

- 坐标**只能**由 `atlas.normalize` + `atlas.contracts.build_anchor` 的确定性匹配产出；
  本包没有任何"接收坐标"的入口，声明锚点只用于比对。
- 匹配失败 = `FAILED`，没有锚点、不落库，不存在"大概位置"。
- `evidence_spans` 只增不改：`(claim_id, claim_version)` 唯一，UPDATE / DELETE 由触发器拒绝。
"""

from .display import (
    DEFAULT_CONTEXT_CHARS,
    HighlightPayload,
    HighlightRegion,
    build_highlight,
    highlight_for_span,
)
from .store import (
    DEFAULT_DB_PATH,
    SqliteEvidenceStore,
    open_evidence_store,
    verify_and_record,
)
from .verify import (
    EvidenceSpan,
    QuoteVerification,
    VerificationOutcome,
    verify_claim,
    verify_quote,
)

__all__ = [
    # display
    "DEFAULT_CONTEXT_CHARS",
    "HighlightPayload",
    "HighlightRegion",
    "build_highlight",
    "highlight_for_span",
    # store
    "DEFAULT_DB_PATH",
    "SqliteEvidenceStore",
    "open_evidence_store",
    "verify_and_record",
    # verify
    "EvidenceSpan",
    "QuoteVerification",
    "VerificationOutcome",
    "verify_claim",
    "verify_quote",
]
