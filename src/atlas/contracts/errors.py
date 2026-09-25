"""T-002 领域契约的错误层级。

契约违例一律用这里的显式异常表达：不变量在代码层强制时**必须抛错**，
不得降级为警告、日志或静默返回（CLAUDE.md 硬规则 2）。

注意：这些异常**不继承 `ValueError`**。pydantic 只把 `ValueError` / `AssertionError`
包装成 `ValidationError`；继承 `Exception` 可以让契约违例原样穿透校验层，
调用方拿到的是有语义的领域异常，而不是被包装过的通用错误。
"""

from __future__ import annotations

__all__ = [
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
]


class ContractError(Exception):
    """所有契约违例的基类。"""


class InvalidContractStateError(ContractError):
    """尝试构造非法状态：字段自相矛盾、区间非法、标识与内容不一致等。"""


class IdError(InvalidContractStateError):
    """标识符不合法，或与它所标识的内容不一致（被篡改 / 手工拼错）。"""


class AnchorError(InvalidContractStateError):
    """证据锚点真值非法：区间为空、越界，或与所绑定的 raw 不一致。"""


class VersionError(InvalidContractStateError):
    """版本链规则被破坏：版次不连续、supersedes 指向错误、或绕过当前版本。"""


class ImmutabilityError(ContractError):
    """对只增不改层（Raw / Confirmed）的修改、覆盖或删除尝试。

    Raw 与 Confirmed 是**写入即不可变**的事实层；任何变更都必须表达为
    新记录（Confirmed 用 supersede 追加修正记录），而不是改动已有记录。
    """


class NotFoundError(ContractError):
    """按标识查找记录失败（不静默返回空对象）。"""


class UnverifiedEvidenceError(ContractError):
    """未经确定性校验的证据被用于派生 Confirmed。

    SPEC §2.2 / §2.3：quote 未经确定性匹配（未取得 anchor，或状态为"未验证"）时
    **不得**产生 Confirmed。
    """


class EvidenceMismatchError(UnverifiedEvidenceError):
    """声明的锚点与按 quote 确定性重算出的锚点不一致（伪造或过期）。"""


class TaskContractError(ContractError):
    """任务规范（SPEC §3）被破坏：缺版本字段、非确定性输出、重试不留半成品等。"""
