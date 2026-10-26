"""任务规范（SPEC §3）：引擎可替换的前提。

形状：`f(输入快照, 配置快照) → 新版本对象`

每个任务都必须满足：

- **幂等**：同输入 + 同配置 → 同输出，或明确的"无变化"
- **可重试**：失败不留半成品
- **可重算**：输出能从输入 + 配置重新生成
- **版本化**：输出携带 `(code_version, config_version, model_version)`
- **边界**：Raw 只增不改 / Proposed 可覆写 / Confirmed 只增不改

本模块只定义契约与幂等键计算，不提供执行引擎（T-110 负责）。
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, Optional

from pydantic import Field

from .base import ContractModel
from .errors import TaskContractError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskVersions(ContractModel):
    """每次输出都必须携带的版本三元组。"""

    code_version: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    model_version: Optional[str] = None


class Snapshot(ContractModel):
    """不可变输入/配置快照。快照不可变是"可重算"的前提。"""

    payload: Dict[str, Any] = Field(default_factory=dict)

    def digest(self) -> str:
        """规范化 JSON 的 sha256 —— 键序无关，确保幂等键稳定。"""
        canonical = json.dumps(self.payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TaskOutput(ContractModel):
    """任务产物：新版本对象 + 版本三元组 + 幂等键。"""

    task_name: str
    idempotency_key: str
    versions: TaskVersions
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    produced_at: datetime = Field(default_factory=_utcnow)


class AtlasTask(ABC):
    """所有 Atlas 任务的基类。

    子类只实现 `run()`；`execute()` 负责计算幂等键并包装产物，
    从而让"幂等 / 版本化"由契约强制，而不是靠各任务自觉。

    `execute()` 在 `run()` 抛异常时**原样向上抛**，不吞异常、不返回半成品
    （CLAUDE.md 硬规则 2）。
    """

    name: ClassVar[str] = "unnamed"

    def __init__(self, versions: TaskVersions) -> None:
        self.versions = versions

    @abstractmethod
    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        """纯计算：不落盘、不改全局状态。未实现的部分必须抛 `NotImplementedError`。"""
        raise NotImplementedError

    def idempotency_key(self, inputs: Snapshot, config: Snapshot) -> str:
        material = "\x1f".join(
            [
                self.name,
                inputs.digest(),
                config.digest(),
                self.versions.code_version,
                self.versions.config_version,
                self.versions.model_version or "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def execute(self, inputs: Snapshot, config: Snapshot) -> TaskOutput:
        if not self.name or self.name == "unnamed":
            raise TaskContractError("任务必须声明 name，否则幂等键不可区分")
        artifacts = self.run(inputs, config)
        if not isinstance(artifacts, dict):
            raise TaskContractError(
                f"{self.name}.run() 必须返回 dict，收到 {type(artifacts).__name__}"
            )
        return TaskOutput(
            task_name=self.name,
            idempotency_key=self.idempotency_key(inputs, config),
            versions=self.versions,
            artifacts=artifacts,
        )
