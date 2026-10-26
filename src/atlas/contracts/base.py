"""契约记录基类。

三条代码层约定（不是文档约定）：

1. `frozen=True` —— 记录一经构造，字段**不可再赋值**（pydantic `ValidationError`）。
2. `extra="forbid"` —— 未声明字段一律拒绝。这条很重要：PI / 抽取器**不得**输出坐标，
   于是 `ProposedEvidence(..., char_start=3)` 在类型层就构造不出来。
3. `model_copy(update=...)` 是 pydantic 自带的、**绕过 `frozen` 的后门**，
   这里显式封死：需要"新版本"就必须构造新记录，不允许悄悄改一份拷贝。
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from .errors import ImmutabilityError

__all__ = ["ContractModel"]


class ContractModel(BaseModel):
    """所有领域契约记录的基类：冻结、字段闭合、封死 model_copy 后门。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def model_copy(
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise ImmutabilityError(
                f"{type(self).__name__} 是写入即不可变的契约记录，"
                "禁止用 model_copy(update=...) 篡改已构造的实例；"
                "需要新版本请显式构造新记录（Raw / Confirmed 层则根本不允许变更）。"
            )
        return super().model_copy(deep=deep)
