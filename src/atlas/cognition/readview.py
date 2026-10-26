"""机器分类的只读浏览投影：最新运行决定状态，不把旧标签当成新结果。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .store import ProposedStoreError


@dataclass(frozen=True)
class ClassificationView:
    unit_id: str
    raw_id: str
    unit_kind: str
    status: str
    labels: tuple[str, ...]
    model: str
    origin: str = "ai"
    reason: str = ""


def read_classifications(db_path: str | Path) -> dict[str, ClassificationView]:
    """每次读取一份一致快照；不创建文件、表或触发器，不缓存过时结果。

    跨模型的相同提议可能被内容幂等复用，未留下当前 plan 的提议行。
    此时明确返回 history 来源，不能声称历史标签集合就是新模型的完整输出。
    """
    path = Path(db_path)
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"proposal_runs", "proposed_claims"}
        if not tables & required:
            return {}
        if not required <= tables:
            raise ProposedStoreError("分类库结构不完整：运行账与提议表必须同时存在")
        runs = list(db.execute(
            "SELECT r.* FROM proposal_runs r JOIN "
            "(SELECT unit_id,MAX(rowid) AS latest FROM proposal_runs GROUP BY unit_id) h "
            "ON r.rowid=h.latest ORDER BY r.rowid"
        ))
        claims = list(db.execute("SELECT * FROM proposed_claims ORDER BY rowid"))
    by_unit: dict[str, list[sqlite3.Row]] = {}
    for claim in claims:
        by_unit.setdefault(claim["unit_id"], []).append(claim)
    result = {}
    for run in runs:
        rows = by_unit.get(run["unit_id"], [])
        if not rows:
            raise ProposedStoreError(f"分类运行 {run['run_id']} 没有对应提议或降级记录")
        heads = list({row["claim_key"]: row for row in rows}.values())
        origin = "ai"
        candidates = []
        if run["status"] == "classified":
            candidates = [r for r in heads if r["status"] == "classified" and r["plan_digest"] == run["plan_digest"]]
            if not candidates:
                origin = "history"
                candidates = [r for r in heads if r["status"] == "classified"]
        scores: dict[str, float] = {}
        for row in candidates:
            scores[row["value"]] = max(scores.get(row["value"], 0), row["confidence"] or 0)
        result[run["unit_id"]] = ClassificationView(
            unit_id=run["unit_id"], raw_id=run["raw_id"], unit_kind=rows[-1]["unit_kind"],
            status=run["status"], labels=tuple(sorted(scores, key=lambda v: (-scores[v], v))),
            model=run["model"], origin=origin, reason=run["reason"] or "",
        )
    return result
