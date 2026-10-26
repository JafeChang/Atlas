"""`CognitionPort` —— 认知层的**稳定接口**（SPEC §2.14 决策二）。

PI 边车只是它的**一个适配器**。换 provider、换模型、换回直连 HTTP、换成别的语言写的
边车，都只换实现类，调用方不动——这是"engine 可替换"在认知层上的落地。

接口形状刻意最小：**一个**方法，输入是一个不可变快照，输出是一个冻结结果。

关于 `NotImplementedError`（CLAUDE.md 硬规则 2）：`CognitionPort` 是 `ABC`，
`extract` 是 `@abstractmethod`，因此"没实现就当作能跑"在类型层就不可能——
子类不实现它根本无法实例化。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .contracts import CognitionRequest, CognitionResult

__all__ = ["CognitionPort"]


class CognitionPort(ABC):
    """认知层的唯一入口。

    实现者必须满足：

    1. **只输出 quote，不输出坐标**（SPEC §2.2）——由 `CognitionOutput` 的冻结契约强制；
    2. 每次调用携带 `(code_version, config_version, model_version)`（SPEC §3）；
    3. 模型不可用时返回 `unclassified` + 原因码，**不得**用规则猜测顶替（§2.14 决策四）；
    4. 契约/环境问题**响亮失败**，不得降级掩盖。
    """

    @abstractmethod
    def extract(self, request: CognitionRequest) -> CognitionResult:
        """对一条不可信外部内容做一次最小结构化调用。"""
        raise NotImplementedError
