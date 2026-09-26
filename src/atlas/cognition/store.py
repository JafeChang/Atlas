"""T-105 Proposed 层持久化：`proposed_claims` + `proposal_runs`（SPEC §2.3 / §2.10 / §3）。

表归属与共用 DB 的边界
======================

`data/store/atlas.db` 由多个域共用，SPEC §2.10 已把 `proposed_claims` 登记给 **T-105**。
本模块只执行：

- `CREATE TABLE IF NOT EXISTS proposed_claims`
- `CREATE TABLE IF NOT EXISTS proposal_runs`（**需要登记**：见下）
- 自己的索引与触发器
- `CREATE TABLE IF NOT EXISTS cognition_store_meta`（本域的 schema 版本登记，
  与 T-103 的 `raw_store_meta` 同一做法 —— 不借用 T-101 的 `store_meta`）

**不做**任何破坏性 DDL，不碰任何其它域的表，也不 import 任何其它域的持久化模块
（`atlas.registry` / `atlas.labels` / `atlas.archive` 都没 import）。

> ⚠️ **`proposal_runs` 是一张新增表**，SPEC §2.10 的登记表里**还没有它**。
> 规则是"新增表必须先登记"，因此这张表的名字、列与用途**必须**由主代理登记进
> SPEC §2.10 之后才算生效。为什么不能省掉它（设计理由，不是偏好）：
> "已分类的单元不得重复调用模型"（§3 幂等）要求系统能回答"**这个单元在本配置下
> 是否已经跑过**"，而这与"有没有产出 claim"是**两件事** —— 一个降级为未分类的
> 单元**没有** claim，但它**已经**跑过了。把这件事塞进 `proposed_claims` 会让
> "提议"这张表同时承载"运行账"，两类语义混在一起。

两张表的分工
============

| 表 | 回答的问题 | 幂等键 |
|---|---|---|
| `proposed_claims` | "这个单元在本配置下**产出了什么**"（含"什么都没产出"的降级行） | 行身份 `claim_key` + 内容指纹 `output_digest` |
| `proposal_runs` | "这个单元在本配置下**跑过没有**、花了多少" | `(unit_id, plan_digest)` |

`proposed_claims` 的行身份 `claim_key` **确定性复算**（`claim_key_for`）：

```
claim_key = "pcl_" + sha256(raw_id ␟ unit_id ␟ kind ␟ value ␟ quote ␟ status ␟ reason)[:32]
```

- `kind` = `industry`（与 `confirmed_labels.label_key` 同一语义，SPEC §2.5 闭环）
- `status` ∈ {`classified`, `unclassified`}；降级行的 `value` / `quote` 为 `NULL`，
  `reason` **必非空** —— §2.14 决策四"降级 = 未分类，绝不是猜测"在**存储层**也成立
  （`CHECK` 强制，不是靠调用方自觉）
- `unit_id` **参与** `claim_key`：issue 是"同一条 quote 出现在同一 raw 的两个条目里"
  ⇒ 若不含 `unit_id`，两条**不同条目**的证据会塌成同一个 id（锚点跟着塌）。

**版本链怎么走**：`version` 是**每条 claim_key 自己的单调版本**，
`supersedes` 是它取代的那一版（首版为 0，不是 `NULL` —— `NULL` 在唯一索引里互不相等，
会让"同一身份只允许一个首版"这条约束失效）。不变量：

```
UNIQUE (claim_key, supersedes)   -- 同一身份、同一前驱只允许一行
UNIQUE (claim_key, version)      -- 版本号唯一
```

**幂等**（SPEC §3"同输入 + 同配置 → 同输出，或明确的'无变化'"）：
`claim_key` 只由**输入**（身份 + 配置决定的取值/引用）决定；`output_digest` 是
**输出**的规范摘要（`confidence` 等）。因此

- 重跑得到**同一个输出** → `(claim_key, output_digest)` 已存在 → **无变化**，
  返回既有行（版本**不**推进）；
- 重跑得到**不同输出** → 新版本（`supersedes` 指向上一版），旧版本保留；
- 降级原因变化 → 同上，是"内容变了"，会产生新版本（这正是可审计的）。

只增不改（SPEC §2.3"Proposed 可覆写"）
======================================

"可覆写"在这里的表达方式是**追加新版本 + 保留版本链**，而不是 `UPDATE`：
`proposed_claims` 上带 `BEFORE UPDATE` / `BEFORE DELETE` 触发器，直接用 `sqlite3`
也改不动。旧版本**永不**删除 —— 这与 T-107/T-108 的 `(claim_id, claim_version)`
同一纪律。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .classify import LabelSpace, Unit

__all__ = [
    "DEFAULT_DB_PATH",
    "CLAIM_KEY_PREFIX",
    "CLAIM_STATUS_CLASSIFIED",
    "CLAIM_STATUS_UNCLASSIFIED",
    "CLAIM_STATUS_UNATTRIBUTED",
    "CLAIM_STATUS_OUT_OF_SPACE",
    "CLAIM_STATUSES",
    "KIND_INDUSTRY",
    "ProposedClaimRow",
    "ProposalRunRow",
    "ProposedStoreError",
    "SqliteProposedStore",
    "claim_key_for",
    "output_digest_for",
    "plan_digest_for",
    "resolve_db_path",
]

#: SPEC §2.10 的目录布局：`proposed_claims` 与其它域共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

#: 本域物理 schema 版本。加列必须显式迁移，不静默兼容（与 T-103 同一纪律）。
SCHEMA_VERSION = 1

#: 表名（SPEC §2.10 登记表里的名字，**不得改动**）。
CLAIMS_TABLE = "proposed_claims"
RUNS_TABLE = "proposal_runs"
META_TABLE = "cognition_store_meta"

#: `claim_key` 的前缀（`pcl_` = proposed claim，与 T-002 的 `clm_` / T-130 的 `ent_`
#: 分属不同身份空间：`clm_` 是**契约**里的 claim_id，`pcl_` 是**本层表格**的行身份）。
CLAIM_KEY_PREFIX = "pcl_"

#: 分类维度。与 `confirmed_labels.label_key` 同一语义（SPEC §2.5 的 C8 闭环）。
KIND_INDUSTRY = "industry"

CLAIM_STATUS_CLASSIFIED = "classified"
CLAIM_STATUS_UNCLASSIFIED = "unclassified"
#: 归不到任何单元的 claim 的**记账行**（不是分类，也不是降级 —— 见 `propose.py`）。
CLAIM_STATUS_UNATTRIBUTED = "unattributed"
#: 模型给出了**当前标签空间之外**的取值：记下来（可审计），但**不得**当作分类结果。
#: 这是 SPEC §2.5 / §2.9 的 C8 闭环在存储层的落地：候选标签集合只来自配置。
CLAIM_STATUS_OUT_OF_SPACE = "out_of_space"

#: `status` 的**闭集**（测试断言"每一条落库的行都落在这个集合里"）。
CLAIM_STATUSES: Tuple[str, ...] = (
    CLAIM_STATUS_CLASSIFIED,
    CLAIM_STATUS_UNCLASSIFIED,
    CLAIM_STATUS_UNATTRIBUTED,
    CLAIM_STATUS_OUT_OF_SPACE,
)

#: 每列的期望顺序（重开时校验形状，避免"同名不同结构的表被静默复用"）。
_CLAIM_COLUMNS: Tuple[str, ...] = (
    "claim_key",
    "version",
    "supersedes",
    "raw_id",
    "unit_id",
    "unit_kind",
    "unit_char_start",
    "unit_char_end",
    "entry_index",
    "title",
    "kind",
    "value",
    "quote",
    "confidence",
    "status",
    "reason",
    "detail",
    "code_version",
    "config_version",
    "model_version",
    "label_space_version",
    "plan_digest",
    "output_digest",
    "batch_id",
    "batch_position",
    "batch_size",
    "retry_count",
    "provider",
    "model",
    "credential_route",
    "input_digest",
    "source",
    "created_at",
)

_RUN_COLUMNS: Tuple[str, ...] = (
    "run_id",
    "unit_id",
    "raw_id",
    "plan_digest",
    "status",
    "reason",
    "batch_id",
    "batch_size",
    "retry_count",
    "calls",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "elapsed_ms",
    "credential_route",
    "model",
    "code_version",
    "config_version",
    "model_version",
    "label_space_version",
    "created_at",
)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {META_TABLE} (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- SPEC §2.10 表归属：proposed_claims 属于 T-105（机器提议，可覆写，保留版本链）
CREATE TABLE IF NOT EXISTS {CLAIMS_TABLE} (
    claim_key         TEXT NOT NULL,
    version           INTEGER NOT NULL,
    supersedes        INTEGER NOT NULL,
    raw_id            TEXT NOT NULL,
    unit_id           TEXT NOT NULL,
    unit_kind         TEXT NOT NULL,
    unit_char_start   INTEGER NOT NULL,
    unit_char_end     INTEGER NOT NULL,
    entry_index       INTEGER,
    title             TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL,
    value             TEXT,
    quote             TEXT,
    confidence        REAL,
    status            TEXT NOT NULL,
    reason            TEXT,
    detail            TEXT NOT NULL DEFAULT '',
    code_version      TEXT NOT NULL,
    config_version    TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    label_space_version TEXT NOT NULL,
    plan_digest       TEXT NOT NULL,
    output_digest     TEXT NOT NULL,
    batch_id          TEXT NOT NULL,
    batch_position    INTEGER NOT NULL,
    batch_size        INTEGER NOT NULL,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    credential_route  TEXT NOT NULL DEFAULT '',
    input_digest      TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    PRIMARY KEY (claim_key, version),
    -- 同一身份、同一前驱只允许一行：这是"版本链是一条链、不是一棵树"的存储层强制。
    UNIQUE (claim_key, supersedes),
    CHECK (version >= 1),
    CHECK (supersedes >= 0),
    CHECK (supersedes < version),
    CHECK (retry_count >= 0),
    CHECK (unit_char_end > unit_char_start),
    CHECK (unit_char_start >= 0),
    CHECK (status IN ('{CLAIM_STATUS_CLASSIFIED}', '{CLAIM_STATUS_UNCLASSIFIED}',
                      '{CLAIM_STATUS_UNATTRIBUTED}', '{CLAIM_STATUS_OUT_OF_SPACE}')),
    -- SPEC §2.14 决策四：降级 = 未分类，绝不是猜测 —— 在存储层也成立。
    -- classified 行必须有取值与**逐字引用**；unclassified 行必须**没有**取值/引用
    -- 且**必有**理由；unattributed / out_of_space 是**审计行**：它们记下"模型说了
    -- 什么"，但值可以存在而引用必须为空（引用归不到原文区间，所以不能当证据）。
    CHECK (
        (status = '{CLAIM_STATUS_CLASSIFIED}'
         AND value IS NOT NULL AND length(value) > 0
         AND quote IS NOT NULL AND length(quote) > 0
         AND confidence IS NOT NULL AND confidence >= 0.0 AND confidence <= 1.0)
        OR
        (status = '{CLAIM_STATUS_UNCLASSIFIED}'
         AND value IS NULL AND quote IS NULL AND confidence IS NULL
         AND reason IS NOT NULL AND length(reason) > 0)
        OR
        (status IN ('{CLAIM_STATUS_UNATTRIBUTED}', '{CLAIM_STATUS_OUT_OF_SPACE}')
         AND quote IS NULL AND confidence IS NULL
         AND reason IS NOT NULL AND length(reason) > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_proposed_claims_raw
    ON {CLAIMS_TABLE}(raw_id);
CREATE INDEX IF NOT EXISTS idx_proposed_claims_unit
    ON {CLAIMS_TABLE}(unit_id);
CREATE INDEX IF NOT EXISTS idx_proposed_claims_status
    ON {CLAIMS_TABLE}(status);
CREATE INDEX IF NOT EXISTS idx_proposed_claims_versions
    ON {CLAIMS_TABLE}(code_version, config_version, model_version);
CREATE INDEX IF NOT EXISTS idx_proposed_claims_batch
    ON {CLAIMS_TABLE}(batch_id);

-- SPEC §3 幂等 / §2.14 边车启动成本：**运行账**。
-- "已分类的单元不得重复调用模型"要求能回答"这个单元在本配置下跑过没有"，
-- 而这与"有没有产出 claim"是两件事（降级单元没有 claim，但它跑过了）。
CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
    run_id            TEXT PRIMARY KEY,
    unit_id           TEXT NOT NULL,
    raw_id            TEXT NOT NULL,
    plan_digest       TEXT NOT NULL,
    status            TEXT NOT NULL,
    reason            TEXT,
    batch_id          TEXT NOT NULL DEFAULT '',
    batch_size        INTEGER NOT NULL DEFAULT 0,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    calls             INTEGER NOT NULL DEFAULT 0,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER,
    elapsed_ms        INTEGER NOT NULL DEFAULT 0,
    credential_route  TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    code_version      TEXT NOT NULL,
    config_version    TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    label_space_version TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (unit_id, plan_digest),
    CHECK (status IN ('{CLAIM_STATUS_CLASSIFIED}', '{CLAIM_STATUS_UNCLASSIFIED}',
                      '{CLAIM_STATUS_UNATTRIBUTED}', '{CLAIM_STATUS_OUT_OF_SPACE}',
                      'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_proposal_runs_unit ON {RUNS_TABLE}(unit_id);
CREATE INDEX IF NOT EXISTS idx_proposal_runs_batch ON {RUNS_TABLE}(batch_id);

-- SPEC §2.3：Proposed 的"覆写"= 追加新版本。已有行**永不** UPDATE / DELETE，
-- 由触发器强制（不是靠调用方约定）。
CREATE TRIGGER IF NOT EXISTS trg_proposed_claims_no_update
BEFORE UPDATE ON {CLAIMS_TABLE}
BEGIN
    SELECT RAISE(ABORT, 'proposed_claims is append-only: UPDATE is forbidden (SPEC 2.3/2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_proposed_claims_no_delete
BEFORE DELETE ON {CLAIMS_TABLE}
BEGIN
    SELECT RAISE(ABORT, 'proposed_claims is append-only: DELETE is forbidden (SPEC 2.3/2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_proposal_runs_no_update
BEFORE UPDATE ON {RUNS_TABLE}
BEGIN
    SELECT RAISE(ABORT, 'proposal_runs is append-only: UPDATE is forbidden (SPEC 2.3/2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_proposal_runs_no_delete
BEFORE DELETE ON {RUNS_TABLE}
BEGIN
    SELECT RAISE(ABORT, 'proposal_runs is append-only: DELETE is forbidden (SPEC 2.3/2.10)');
END;
"""

_PathLike = Union[str, Path]


class ProposedStoreError(Exception):
    """T-105 持久化层的契约违例（形状不符 / 内容与标识不符 / 读不到刚写的行）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_db_path(db_path: _PathLike | None) -> Path:
    """`db_path` 归一成 `Path`；`None` → SPEC §2.10 的默认库文件位置。

    单独暴露是为了能**不产生 I/O** 地断言默认路径策略（测试里尤其重要：
    仓库的 `data/` 不得被测试写入）。
    """
    if db_path is None:
        return DEFAULT_DB_PATH
    return db_path if isinstance(db_path, Path) else Path(db_path)


# --------------------------------------------------------------------------- #
# 确定性身份与摘要
# --------------------------------------------------------------------------- #


def _stable_digest(parts: Sequence[str]) -> str:
    """带字段分隔符的 sha256（`"ab"+""+"c"` 与 `"a"+""+"bc"` 不得相撞）。

    与 `atlas.contracts.ids` 里的 `_sha256_hex` 同一做法 —— 本模块**不 import**
    `atlas.contracts.ids` 的私有函数（那是实现细节），只沿用同一形状。
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def claim_key_for(
    *,
    raw_id: str,
    unit_id: str,
    kind: str,
    value: Optional[str],
    quote: Optional[str],
    status: str,
    reason: Optional[str],
) -> str:
    """`proposed_claims` 的**行身份**（确定性复算，SPEC §4.1 T-002 的 ID 策略同一形状）。

    **输入**（不含 `confidence` 等输出量）参与；`unit_id` 必须参与：
    否则同一条 quote 出现在同一 raw 的两个条目里时，两条**不同条目**的证据会塌成
    同一个 id，而它们指向**不同的原文区间**（锚点会跟着塌）。

    非法入参抛 `ProposedStoreError`（不返回空串、不静默纠正）。
    """
    if not raw_id or not unit_id or not kind:
        raise ProposedStoreError("raw_id / unit_id / kind 均不得为空")
    if status not in CLAIM_STATUSES:
        raise ProposedStoreError(
            f"未知 status：{status!r}（合法值 {list(CLAIM_STATUSES)}）"
        )
    if status == CLAIM_STATUS_CLASSIFIED:
        if not value or not quote:
            raise ProposedStoreError(
                "classified 行必须同时有 value 与 quote："
                f"value={value!r} quote={quote!r}"
            )
        if reason is not None:
            raise ProposedStoreError("classified 行不得携带降级理由")
    elif status in (CLAIM_STATUS_UNATTRIBUTED, CLAIM_STATUS_OUT_OF_SPACE):
        if quote is not None:
            raise ProposedStoreError(
                f"{status} 是审计行：它记下模型说了什么，但引用归不到原文区间，"
                "因此不得携带 quote（不得当证据用）"
            )
        if not reason:
            raise ProposedStoreError(f"{status} 行必须有理由（可审计）")
    else:
        if value is not None or quote is not None:
            raise ProposedStoreError("unclassified 行不得携带 value / quote（降级不是猜测）")
        if not reason:
            raise ProposedStoreError("unclassified 行必须有降级理由（§2.14 决策四）")
    return CLAIM_KEY_PREFIX + _stable_digest(
        [
            raw_id,
            unit_id,
            kind,
            value or "",
            quote or "",
            status,
            reason or "",
        ]
    )[:32]


def output_digest_for(
    *,
    value: Optional[str],
    quote: Optional[str],
    confidence: Optional[float],
    status: str,
    reason: Optional[str],
) -> str:
    """**输出**的规范摘要（幂等比对用）：只由"产出了什么"决定。

    刻意**不含** `created_at` / `elapsed_ms` / token 数：重跑时刻不同不代表产出不同。
    `confidence` 参与 —— 它是产出的一部分，模型给出不同的置信度就是不同的输出
    （会产生新版本，旧版本保留）。
    """
    canonical = json.dumps(
        {
            "value": value,
            "quote": quote,
            "confidence": None if confidence is None else round(float(confidence), 6),
            "status": status,
            "reason": reason,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_digest_for(
    *,
    unit_digest: str,
    code_version: str,
    config_version: str,
    model_version: str,
    label_space_version: str,
) -> str:
    """**运行计划**的摘要：回答"这个单元在本配置下跑过没有"的键（§3 幂等）。

    刻意不含模型延迟 / token：那些是运行观测，不是计划的一部分。
    """
    return _stable_digest(
        [
            unit_digest,
            code_version,
            config_version,
            model_version,
            label_space_version,
        ]
    )


# --------------------------------------------------------------------------- #
# 要写入的行
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProposedClaimRow:
    """`proposed_claims` 的一行（写入前构造，读回后同形）。

    | 组 | 字段 | 说明 |
    |---|---|---|
    | 身份 | `claim_key` / `version` / `supersedes` | `version` 由 store 在写入时定 |
    | 溯源 | `raw_id` / `unit_id` / `unit_kind` / `unit_char_start` / `unit_char_end` / `entry_index` | 单元与**真值区间**（SPEC §2.2） |
    | 内容 | `kind` / `value` / `quote` / `confidence` / `status` / `reason` / `detail` | 模型产出（`quote` 是**文字**，不是坐标） |
    | 版本 | 三元组 + `label_space_version` + `plan_digest` / `output_digest` | SPEC §3 |
    | 调用 | `batch_id` / `batch_position` / `batch_size` / `provider` / `model` / `credential_route` / `input_digest` | 批次账；`source` 标来源 |

    **它刻意不是 `atlas.contracts.ProposedClaim`**：契约那条**没有** `unit_id` /
    `status` 列，因此装不下"降级 = 未分类"与"单元级身份"这两件事。需要契约对象时用
    `as_proposed_claim()` 转换（只对 `classified` 行成立）。
    """

    raw_id: str
    unit_id: str
    unit_kind: str
    unit_char_start: int
    unit_char_end: int
    kind: str
    status: str
    plan_digest: str
    output_digest: str
    code_version: str
    config_version: str
    model_version: str
    label_space_version: str
    input_digest: str
    batch_id: str
    batch_position: int
    batch_size: int
    retry_count: int = 0
    value: Optional[str] = None
    quote: Optional[str] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    detail: str = ""
    entry_index: Optional[int] = None
    title: str = ""
    provider: str = ""
    model: str = ""
    credential_route: str = ""
    source: str = "t105-classify"
    claim_key: str = ""
    version: int = 0
    supersedes: int = 0
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.raw_id or not self.unit_id or not self.kind:
            raise ProposedStoreError("raw_id / unit_id / kind 均不得为空")
        if self.unit_char_start < 0 or self.unit_char_end <= self.unit_char_start:
            raise ProposedStoreError(
                f"非法单元区间：[{self.unit_char_start}, {self.unit_char_end})"
            )
        if self.batch_position < 0 or self.batch_size < 1:
            raise ProposedStoreError("batch_position ≥ 0 且 batch_size ≥ 1")
        if self.retry_count < 0:
            raise ProposedStoreError("retry_count 不得为负")
        if not self.batch_id:
            raise ProposedStoreError("batch_id 不得为空：每一行都要能追到那次调用")
        expected = claim_key_for(
            raw_id=self.raw_id,
            unit_id=self.unit_id,
            kind=self.kind,
            value=self.value,
            quote=self.quote,
            status=self.status,
            reason=self.reason,
        )
        if self.claim_key and self.claim_key != expected:
            raise ProposedStoreError(
                f"claim_key 与内容不符：声明 {self.claim_key}，按内容重算应为 {expected}"
            )
        object.__setattr__(self, "claim_key", expected)
        if not self.plan_digest or not self.output_digest or not self.input_digest:
            raise ProposedStoreError("plan_digest / output_digest / input_digest 均不得为空")
        for name in ("code_version", "config_version", "model_version", "label_space_version"):
            if not getattr(self, name):
                raise ProposedStoreError(f"{name} 不得为空（SPEC §3 的版本三元组）")

    @property
    def is_classified(self) -> bool:
        return self.status == CLAIM_STATUS_CLASSIFIED

    @property
    def is_unclassified(self) -> bool:
        return self.status == CLAIM_STATUS_UNCLASSIFIED

    @property
    def is_audit_only(self) -> bool:
        """审计行（`unattributed` / `out_of_space`）：**不是**分类结果，不参与标签统计。"""
        return self.status in (CLAIM_STATUS_UNATTRIBUTED, CLAIM_STATUS_OUT_OF_SPACE)

    def identity_fields(self) -> Tuple[object, ...]:
        """幂等比对用的**内容**字段（幂等键的另一半是 `claim_key`）。"""
        return (
            self.value,
            self.quote,
            self.confidence,
            self.status,
            self.reason,
        )

    def as_proposed_claim(self):  # type: ignore[no-untyped-def]
        """转成 SPEC §4.1 T-002 的契约对象（**只对分类行成立**）。

        这是给 T-107（证据校验）用的适配入口：契约那条带着 `versions: TaskVersions`
        与 `verification_status`，且 `extra="forbid"` 会把 `unit_id` 这类字段拒之门外
        —— 单元身份留在**本行**上，不进契约对象。未分类行调用它**响亮失败**：
        契约里根本没有"未分类的 claim"这个状态，硬塞就是编造。
        """
        if not self.is_classified:
            raise ProposedStoreError(
                f"claim {self.claim_key} v{self.version} 是未分类行（reason={self.reason}），"
                "没有内容可以装进 ProposedClaim —— 契约里不存在'未分类的 claim'"
            )
        from atlas.contracts import ProposedClaim, TaskVersions

        return ProposedClaim(
            claim_id=self.claim_key,
            raw_id=self.raw_id,
            kind=self.kind,
            value=self.value or "",
            quote=self.quote or "",
            confidence=float(self.confidence or 0.0),
            version=max(self.version, 1),
            versions=TaskVersions(
                code_version=self.code_version,
                config_version=self.config_version,
                model_version=self.model_version,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProposalRunRow:
    """`proposal_runs` 的一行：**运行账**（"这个单元在本配置下跑过没有"）。

    这张表是 SPEC §3 幂等与 §2.14 边车启动成本两条约束的交汇点：
    没有它就无法区分"没跑过"与"跑过了但降级为未分类"，于是降级单元会在每次重跑时
    被**重复调用模型**（成本爆炸且不幂等）。
    """

    run_id: str
    unit_id: str
    raw_id: str
    plan_digest: str
    status: str
    code_version: str
    config_version: str
    model_version: str
    label_space_version: str
    reason: Optional[str] = None
    batch_id: str = ""
    batch_size: int = 0
    retry_count: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: Optional[int] = None
    elapsed_ms: int = 0
    credential_route: str = ""
    model: str = ""
    created_at: Optional[datetime] = None

    @classmethod
    def make(
        cls,
        *,
        unit_id: str,
        raw_id: str,
        plan_digest: str,
        status: str,
        code_version: str,
        config_version: str,
        model_version: str,
        label_space_version: str,
        **rest: Any,
    ) -> "ProposalRunRow":
        run_id = "prun_" + _stable_digest([unit_id, plan_digest])[:32]
        return cls(
            run_id=run_id,
            unit_id=unit_id,
            raw_id=raw_id,
            plan_digest=plan_digest,
            status=status,
            code_version=code_version,
            config_version=config_version,
            model_version=model_version,
            label_space_version=label_space_version,
            **rest,
        )


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


class SqliteProposedStore:
    """`proposed_claims` + `proposal_runs` 的 SQLite 实现（线程安全，可跨线程使用）。

    写路径只有两个入口：`record_claim()`（单行，幂等）与 `record_run()`（运行账，幂等）。
    两者都在**一个事务**里做"查已有 → 决定是否写"，因此并发调用不会写出半成品。

    **可跨线程使用**：连接用 `check_same_thread=False` 打开，所有连接操作在
    `self._lock`（`threading.RLock`）内串行化 —— 与 `atlas.archive` / `atlas.labels`
    同一纪律（真实消费方 `ThreadingHTTPServer` 天生多线程，SPEC §2.11）。
    """

    def __init__(
        self,
        db_path: _PathLike | None = None,
        *,
        busy_timeout: float = 30.0,
    ) -> None:
        self._path = resolve_db_path(db_path)
        self._lock = threading.RLock()
        self._closed = False
        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn = sqlite3.connect(
                str(self._path),
                isolation_level=None,
                check_same_thread=False,
                timeout=busy_timeout,
            )
            self._conn.row_factory = sqlite3.Row
            # 建表**之前**先验形状：结构不同的同名表会让 CREATE 静默无操作。
            self._assert_shape_if_present(CLAIMS_TABLE, _CLAIM_COLUMNS)
            self._assert_shape_if_present(RUNS_TABLE, _RUN_COLUMNS)
            self._conn.executescript(_DDL)
            self._assert_shape(CLAIMS_TABLE, _CLAIM_COLUMNS)
            self._assert_shape(RUNS_TABLE, _RUN_COLUMNS)
            self._init_meta()

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #

    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（**只读**诊断用）。写入请走 `record_claim` / `record_run`。"""
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "SqliteProposedStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 写：单行提议（幂等 + 版本链）
    # ------------------------------------------------------------------ #

    def record_claim(self, row: ProposedClaimRow) -> Tuple[ProposedClaimRow, bool]:
        """写入一行提议（分类行或降级行）。返回 `(库里的行, 是否新写入)`。

        幂等判据（SPEC §3）：同一 `claim_key` 下**已存在同一 `output_digest`** ⇒
        **无变化**：返回既有行，**版本不推进**（这是"同输入 + 同配置 → 明确的'无变化'"）。
        输出不同 ⇒ 追加新版本，`supersedes` 指向上一版，旧版本**保留**。

        不变量由存储层强制（`CHECK` / `UNIQUE` / 触发器），不是靠调用方自觉。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._select_same_output(row.claim_key, row.output_digest)
                if existing is not None:
                    self._conn.commit()
                    return existing, False
                previous = self._head_version(row.claim_key)
                version = previous + 1
                supersedes = previous
                self._conn.execute(
                    f"INSERT INTO {CLAIMS_TABLE} ("
                    + ", ".join(_CLAIM_COLUMNS)
                    + ") VALUES ("
                    + ", ".join("?" for _ in _CLAIM_COLUMNS)
                    + ")",
                    (
                        row.claim_key,
                        version,
                        supersedes,
                        row.raw_id,
                        row.unit_id,
                        row.unit_kind,
                        row.unit_char_start,
                        row.unit_char_end,
                        row.entry_index,
                        row.title,
                        row.kind,
                        row.value,
                        row.quote,
                        row.confidence,
                        row.status,
                        row.reason,
                        row.detail,
                        row.code_version,
                        row.config_version,
                        row.model_version,
                        row.label_space_version,
                        row.plan_digest,
                        row.output_digest,
                        row.batch_id,
                        row.batch_position,
                        row.batch_size,
                        row.retry_count,
                        row.provider,
                        row.model,
                        row.credential_route,
                        row.input_digest,
                        row.source,
                        (row.created_at or _utcnow()).isoformat(),
                    ),
                )
                stored = self._select_one(row.claim_key, version)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        if stored is None:  # pragma: no cover - 不可能状态
            raise ProposedStoreError(
                f"claim {row.claim_key} v{version} 写入后无法读回：库状态与写入语义不一致"
            )
        return stored, True

    def record_run(self, run: ProposalRunRow) -> Tuple[ProposalRunRow, bool]:
        """写入一条运行账。同一 `(unit_id, plan_digest)` 重复写入 ⇒ **无变化**。"""
        with self._lock:
            existing = self._select_run(run.unit_id, run.plan_digest)
            if existing is not None:
                return existing, False
            self._conn.execute(
                f"INSERT INTO {RUNS_TABLE} ("
                + ", ".join(_RUN_COLUMNS)
                + ") VALUES ("
                + ", ".join("?" for _ in _RUN_COLUMNS)
                + ")",
                (
                    run.run_id,
                    run.unit_id,
                    run.raw_id,
                    run.plan_digest,
                    run.status,
                    run.reason,
                    run.batch_id,
                    run.batch_size,
                    run.retry_count,
                    run.calls,
                    run.input_tokens,
                    run.output_tokens,
                    run.reasoning_tokens,
                    run.elapsed_ms,
                    run.credential_route,
                    run.model,
                    run.code_version,
                    run.config_version,
                    run.model_version,
                    run.label_space_version,
                    (run.created_at or _utcnow()).isoformat(),
                ),
            )
            stored = self._select_run(run.unit_id, run.plan_digest)
        if stored is None:  # pragma: no cover - 不可能状态
            raise ProposedStoreError(
                f"运行账 {run.run_id} 写入后无法读回：库状态与写入语义不一致"
            )
        return stored, True

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def already_planned(self, unit_id: str, plan_digest: str) -> bool:
        """该单元在本配置下是否**已经跑过**（§3：已分类的单元不得重复调用模型）。

        这是"降级单元也不会被重复调用"的判据 —— 它查的是**运行账**，
        不查 claim（未分类的单元没有 claim，但它确实跑过了）。
        """
        return self._select_run(unit_id, plan_digest) is not None

    def planned_state(
        self, plan_digests: Mapping[str, str]
    ) -> Dict[str, Optional[ProposalRunRow]]:
        """批量查询运行账（一次一把锁）。`{unit_id: 运行账行或 None}`。

        T-105 的**重试决策**需要看到"上一次为什么没成"，因此这里返回整行而不只是布尔。
        """
        with self._lock:
            return {
                unit_id: self._select_run(unit_id, digest)
                for unit_id, digest in plan_digests.items()
            }

    def planned_unit_ids(self, plan_digests: Mapping[str, str]) -> Dict[str, bool]:
        """批量查询（一次一把锁，避免 N 次往返）。`{unit_id: 是否已跑过}`。"""
        with self._lock:
            out: Dict[str, bool] = {}
            for unit_id, digest in plan_digests.items():
                out[unit_id] = self._select_run(unit_id, digest) is not None
            return out

    def head(self, claim_key: str) -> Optional[ProposedClaimRow]:
        """该身份的最新版本（版本链的头）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {CLAIMS_TABLE} WHERE claim_key = ? "
                "ORDER BY version DESC LIMIT 1",
                (claim_key,),
            ).fetchone()
        return None if row is None else _row_to_claim(row)

    def history(self, claim_key: str) -> List[ProposedClaimRow]:
        """该身份的**完整版本链**（按版本升序，旧版本永不删除）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM {CLAIMS_TABLE} WHERE claim_key = ? ORDER BY version",
                (claim_key,),
            ).fetchall()
        return [_row_to_claim(row) for row in rows]

    def current_for_raw(self, raw_id: str) -> List[ProposedClaimRow]:
        """该 raw 上**全部身份的最新版本**（含未分类行）。按详情确定序排序。"""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.* FROM {CLAIMS_TABLE} c
                JOIN (SELECT claim_key, MAX(version) AS v FROM {CLAIMS_TABLE}
                      WHERE raw_id = ? GROUP BY claim_key) h
                  ON c.claim_key = h.claim_key AND c.version = h.v
                ORDER BY c.unit_char_start, c.unit_id, c.value, c.claim_key
                """,
                (raw_id,),
            ).fetchall()
        return [_row_to_claim(row) for row in rows]

    def current_for_unit(self, unit_id: str) -> List[ProposedClaimRow]:
        """该单元上**全部身份的最新版本**（含未分类行）。"""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.* FROM {CLAIMS_TABLE} c
                JOIN (SELECT claim_key, MAX(version) AS v FROM {CLAIMS_TABLE}
                      WHERE unit_id = ? GROUP BY claim_key) h
                  ON c.claim_key = h.claim_key AND c.version = h.v
                ORDER BY c.value, c.claim_key
                """,
                (unit_id,),
            ).fetchall()
        return [_row_to_claim(row) for row in rows]

    def claim_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {CLAIMS_TABLE}"
            ).fetchone()
        return int(row["n"])

    def run_count(self) -> int:
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {RUNS_TABLE}").fetchone()
        return int(row["n"])

    def status_counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT status, COUNT(*) AS n FROM {CLAIMS_TABLE} GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def reason_counts(self) -> Dict[str, int]:
        """降级理由的直方图（**未分类必须可审计**，不能只有一个总数）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT COALESCE(reason, '') AS r, COUNT(*) AS n FROM {CLAIMS_TABLE} "
                "WHERE status = ? GROUP BY r ORDER BY n DESC",
                (CLAIM_STATUS_UNCLASSIFIED,),
            ).fetchall()
        return {str(row["r"]): int(row["n"]) for row in rows}

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _select_one(self, claim_key: str, version: int) -> Optional[ProposedClaimRow]:
        row = self._conn.execute(
            f"SELECT * FROM {CLAIMS_TABLE} WHERE claim_key = ? AND version = ?",
            (claim_key, version),
        ).fetchone()
        return None if row is None else _row_to_claim(row)

    def _select_same_output(
        self, claim_key: str, output_digest: str
    ) -> Optional[ProposedClaimRow]:
        row = self._conn.execute(
            f"SELECT * FROM {CLAIMS_TABLE} WHERE claim_key = ? AND output_digest = ? "
            "ORDER BY version DESC LIMIT 1",
            (claim_key, output_digest),
        ).fetchone()
        return None if row is None else _row_to_claim(row)

    def _head_version(self, claim_key: str) -> int:
        row = self._conn.execute(
            f"SELECT COALESCE(MAX(version), 0) AS v FROM {CLAIMS_TABLE} WHERE claim_key = ?",
            (claim_key,),
        ).fetchone()
        return int(row["v"])

    def _select_run(self, unit_id: str, plan_digest: str) -> Optional[ProposalRunRow]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {RUNS_TABLE} WHERE unit_id = ? AND plan_digest = ?",
                (unit_id, plan_digest),
            ).fetchone()
        return None if row is None else _row_to_run(row)

    def _assert_shape_if_present(self, table: str, columns: Sequence[str]) -> None:
        if self._table_exists(table):
            self._assert_shape(table, columns)

    def _table_exists(self, table: str) -> bool:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def _assert_shape(self, table: str, columns: Sequence[str]) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        actual = tuple(str(row["name"]) for row in rows)
        if actual != tuple(columns):
            raise ProposedStoreError(
                f"{self._path} 里的 {table} 表结构与本模块预期不符"
                f"（实际列：{actual}，预期列：{tuple(columns)}）；"
                f"当前 SCHEMA_VERSION={SCHEMA_VERSION}。需要显式迁移，不做静默兼容"
            )

    def _init_meta(self) -> None:
        row = self._conn.execute(
            f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                f"INSERT INTO {META_TABLE} (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        if int(row["value"]) != SCHEMA_VERSION:
            raise ProposedStoreError(
                f"库文件 {self._path} 的 cognition schema 版本为 {row['value']}，"
                f"本代码只认 {SCHEMA_VERSION}；需要显式迁移，不做静默兼容"
            )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SqliteProposedStore(db_path={str(self._path)!r})"


# --------------------------------------------------------------------------- #
# 行 ↔ 对象
# --------------------------------------------------------------------------- #


def _row_to_claim(row: sqlite3.Row) -> ProposedClaimRow:
    payload: Dict[str, Any] = {key: row[key] for key in _CLAIM_COLUMNS}
    payload["confidence"] = (
        None if payload["confidence"] is None else float(payload["confidence"])
    )
    payload["entry_index"] = (
        None if payload["entry_index"] is None else int(payload["entry_index"])
    )
    payload["created_at"] = datetime.fromisoformat(str(payload["created_at"]))
    return ProposedClaimRow(**payload)


def _row_to_run(row: sqlite3.Row) -> ProposalRunRow:
    payload: Dict[str, Any] = {key: row[key] for key in _RUN_COLUMNS}
    for name in ("reasoning_tokens",):
        payload[name] = None if payload[name] is None else int(payload[name])
    payload["created_at"] = datetime.fromisoformat(str(payload["created_at"]))
    return ProposalRunRow(**payload)


def open_proposed_store(db_path: _PathLike | None = None) -> SqliteProposedStore:
    """便捷入口：打开（必要时创建）`proposed_claims` 仓储。"""
    return SqliteProposedStore(db_path=db_path)


# --------------------------------------------------------------------------- #
# 单元 → 行（供 propose 层使用的纯函数，避免它在两个模块间重复身份公式）
# --------------------------------------------------------------------------- #


def unit_row_fields(unit: Unit) -> Dict[str, Any]:
    """单元里**参与行身份与溯源**的字段（一处定义，避免两处漂移）。"""
    return {
        "raw_id": unit.raw_id,
        "unit_id": unit.unit_id,
        "unit_kind": unit.kind,
        "unit_char_start": unit.char_start,
        "unit_char_end": unit.char_end,
        "entry_index": unit.entry_index,
        "title": unit.title,
    }


def batch_id_for(raw_id: str, unit_ids: Sequence[str]) -> str:
    """批次 ID（`bat_…`）：同一 raw 上、同一有序单元列表 → 同一 ID（可复算）。"""
    from .classify import batch_key_for

    return "bat_" + batch_key_for(raw_id, unit_ids)[:32]


def label_space_version(space: LabelSpace) -> str:
    """写进 `label_space_version` 列的稳定标识（标签空间指纹 + 配置版本）。

    **不给默认值**：标签空间必须由组合根注入（SPEC §2.5 闭环）。
    """
    return f"{space.config_version}#{space.fingerprint[:32]}"
