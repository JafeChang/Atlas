"""T-003 认知层错误层级。

与 `atlas.contracts.errors` 同一先例：契约违例**不继承 `ValueError`**，
因此不会被 pydantic 包装成 `ValidationError`，调用方拿到的是有语义的领域异常。

**失败语义的分界（本包的核心裁决，见 `PROTOCOL.md`）**

| 情形 | 处理 |
|---|---|
| 模型调用本身失败（不可达 / 超时 / HTTP 4xx 5xx / 模型下架 / 空补全） | **降级**为"未分类" + 保留原因码（SPEC §2.14 决策四） |
| 调用成功但输出**无法解析**出唯一 JSON 值 | **降级**（"未分类"，原因码 `unparseable_output`）——PI 拿不到可用输出 |
| 解析出 JSON 但**违反契约结构**（缺字段 / 多字段 / 类型错 / 越界） | **响亮失败** `ModelEnvelopeError`——这是必须修的 bug，不得静默降级 |
| 边车缺依赖 / node 不可用 / 协议版本不符 / 未实现的操作 | **响亮失败**——环境或接线问题，不得降级成"未分类" |
"""

from __future__ import annotations

__all__ = [
    "CognitionError",
    "ConfigError",
    "CognitionPortError",
    "IsolationViolationError",
    "ModelEnvelopeError",
    "ModelOutputError",
    "ProtocolError",
    "SidecarUnavailableError",
]


class CognitionError(Exception):
    """认知层所有错误的基类。"""


class ConfigError(CognitionError):
    """认知层配置非法或不完整（缺 key、provider 未知、超时非正等）。

    这是**响亮失败**：配置缺失不是"模型不可用"，不得降级为"未分类"。
    """


class SidecarUnavailableError(CognitionError):
    """边车不可用：node 找不到、最低版本不满足、依赖未安装、进程启动失败。

    这是**响亮失败**：属于环境/接线问题，与"模型不可用"是两回事。
    """


class ProtocolError(CognitionError):
    """边车协议违例：版本不符、帧不可解析、操作未实现、缺少结果帧。

    这是**响亮失败**：协议是代码与代码之间的契约，出错必须可见。
    """


class ModelOutputError(CognitionError):
    """模型输出不是"恰好一个完整 JSON 值"（多段 / 有尾随内容 / 截断 / 纯文本）。

    该错误由解析层抛出；适配器捕获它并**降级**为"未分类"（保留原因码），
    因为不可解析的输出属于"PI 没拿到可用输出"，而不是接线错误。
    """


class ModelEnvelopeError(CognitionError):
    """模型输出解析成了 JSON，但**结构违反契约**（缺字段 / 多余字段 / 类型错 / 越界）。

    这是**响亮失败**：契约由冻结模型（`extra="forbid"`）强制，违反即 bug。
    """


class CognitionPortError(CognitionError):
    """`CognitionPort` 被以非法方式使用（例如空输入、空候选标签集）。"""


class IsolationViolationError(CognitionError):
    """隔离前提被破坏：边车报告注册了**非零**工具（SPEC §2.14 决策一 / §4.7）。

    这是**响亮失败**：一旦有工具就重新出现执行面，门槛立刻复活，
    必须上真隔离（容器 / 微VM），而不是继续用当前方案。
    """
