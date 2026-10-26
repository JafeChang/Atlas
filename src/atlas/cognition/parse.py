"""T-003 模型输出的鲁棒解析（SPEC §2.14 决策三）。

**解析策略（确定性，可单测）**

1. 直接对整段文本做"恰好一个 JSON 值"的扫描；
2. 失败则剥掉一层 markdown 代码围栏（```json … ``` / ``` … ```），再扫一次；
3. 再失败则剥掉首尾空白，再扫一次；
4. 全部失败 → 抛 `ModelOutputError`（适配器把它降级为"未分类"，原因码
   `unparseable_output`）。

**为什么不用正则抓第一个 `{`**

`re.search(r"\\{.*\\}", text, re.S)` 这类写法在两段输出 / 前后有说明文字时会
**静默**取到"看起来合法"的对象——正是 SPEC §2.14 要防的失败模式。
这里的扫描器因此有两条硬约束：

- **唯一的顶层值**：值之后只允许空白。多余的内容（第二段 JSON、解释文字）一律拒绝；
- **字符串与转义感知**：`{"a": "}"}` 的花括号在字符串内，不参与配对。

它不是"更聪明的正则"，而是"宁可什么都不返回，也不返回一个错的对象"。

**围栏剥离的严格性**：只有**整段**文本恰好是一个围栏块时才剥离
（开头 ```` ``` ````、其后是语言标签、结尾 ```` ``` ```` 且之后无实质内容）。
凡是"围栏里塞了两段 / 结尾不像围栏"的畸形输入一律不剥离 → 落到"拒绝"分支，
而不是被"宽容地"拼出一个对象。
"""

from __future__ import annotations

import json
import re
from typing import Any, List, NamedTuple, Optional, Tuple

from .errors import ModelOutputError

__all__ = ["Extracted", "extract_single_json", "normalize_model_text"]

#: 围栏语言标签：允许 `json` / `JSON` / `jsonc` / `application/json` 等常见写法。
_FENCE_LABEL = re.compile(r"^[A-Za-z0-9_+.-]{0,32}$")

_WS = " \t\r\n\f\v"


class Extracted(NamedTuple):
    """一次成功的抽取。`strategy` 记录走了哪条路径（可审计）。"""

    value: Any
    strategy: str
    start: int
    end: int


def normalize_model_text(text: str) -> str:
    """统一换行、去 BOM、去首尾空白。

    只做**不会改变 JSON 语义**的归一化——不做引号替换、不去注释
    （那会引入"猜"的成分）。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _scan_value_end(text: str, start: int) -> int:
    """从 `start` 起扫描一个完整 JSON 值的结束位置（下标+1），失败返回 -1。"""
    if start >= len(text):
        return -1
    first = text[start]
    if first == '"':
        return _scan_string_end(text, start)
    if first not in "{[":
        # 顶层标量：不是本契约的合法形状，但扫完它才能准确判断"后面还有没有东西"。
        index = start
        while index < len(text) and text[index] not in ",]}" + _WS:
            index += 1
        return index if index > start else -1

    stack: List[str] = []
    in_string = False
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                return -1
            opener = stack.pop()
            if (opener == "{" and char != "}") or (opener == "[" and char != "]"):
                return -1
            if not stack:
                return index + 1
        index += 1
    # 不闭合 = 截断。拒绝，不猜。
    return -1


def _scan_string_end(text: str, start: int) -> int:
    escaped = False
    index = start + 1
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index + 1
        index += 1
    return -1


def _parse_exactly_one(text: str) -> Optional[Tuple[Any, int, int]]:
    stripped = text.strip(_WS)
    if not stripped:
        return None
    leading = text.index(stripped)
    end = _scan_value_end(text, leading)
    if end == -1:
        return None
    if text[end:].strip(_WS):
        # 尾随内容（第二段 JSON / 解释文字）⇒ 有歧义，拒绝。
        return None
    try:
        value = json.loads(text[leading:end])
    except ValueError:
        return None
    # 顶层必须是对象 / 数组：标量不是本契约的合法形状（与边车实现保持一致）。
    if not isinstance(value, (dict, list)):
        return None
    return value, leading, end


def _strip_code_fence(text: str) -> Optional[str]:
    """严格剥离"整段恰好一个围栏块"，否则返回 `None`。

    严格性来自三个条件，缺一不可：

    1. 去掉首尾空白后以 ```` ``` ```` 开头；
    2. 首行（```` ``` ```` 之后到第一个换行）是合法的语言标签；
    3. 正文里**恰好**有一个收尾 ```` ``` ````，且它之后没有实质内容。
    """
    trimmed = text.strip(_WS)
    if not trimmed.startswith("```"):
        return None
    newline = trimmed.find("\n")
    if newline == -1:
        return None
    label = trimmed[3:newline].strip()
    if not _FENCE_LABEL.match(label):
        return None
    body = trimmed[newline + 1 :]
    closing = body.find("```")
    if closing == -1:
        # 起手有围栏、收尾没有 ⇒ 畸形（例如输出被截断）。不剥，让上层拒绝。
        return None
    tail = body[closing + 3 :].strip(_WS)
    if tail:
        # 围栏之后还有内容 ⇒ 畸形。不剥。
        return None
    if body.find("```", closing + 3) != -1:
        return None
    inner = body[:closing]
    if "```" in inner:
        return None
    return inner.strip(_WS)


def extract_single_json(raw: str) -> Extracted:
    """从模型原始输出里抽出**恰好一个完整 JSON 值**；抽不出就响亮失败。"""
    if not isinstance(raw, str) or raw == "":
        raise ModelOutputError("模型输出为空，无法抽取 JSON")

    text = _normalize_for_parse(raw)
    candidates: List[Tuple[str, str, int]] = [(text, "direct", 0)]

    unfenced = _strip_code_fence(text)
    if unfenced is not None:
        offset = text.find(unfenced)
        candidates.append((unfenced, "fence", offset if offset >= 0 else 0))

    # 说明文字包裹：只在**去掉前后整行非 JSON 说明**后仍能恰好解析出一个值时才接受。
    stripped_lines = _strip_surrounding_prose(text)
    if stripped_lines is not None:
        offset = text.find(stripped_lines)
        candidates.append((stripped_lines, "prose", offset if offset >= 0 else 0))

    for candidate_text, strategy, offset in candidates:
        parsed = _parse_exactly_one(candidate_text)
        if parsed is not None:
            value, start, end = parsed
            return Extracted(
                value=value,
                strategy=strategy,
                start=offset + start,
                end=offset + end,
            )

    raise ModelOutputError(_describe_failure(text))


def _normalize_for_parse(raw: str) -> str:
    """只去 BOM、统一换行；可保留首尾空白（精确切片的 offset 才不会被移动）。"""
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    return raw.replace("\r\n", "\n").replace("\r", "\n")


#: 说明行里出现这些字符就说明它**不是**纯说明（可能是围栏/结构残留），不剥。
_STRUCTURAL_MARKERS = ("```", "{", "}", "[", "]")


def _strip_surrounding_prose(text: str) -> Optional[str]:
    """剥掉**纯说明行**包裹，前提是剩下的部分自身仍恰好是一个 JSON 值。

    实现刻意"笨"，并且有一条关键守卫：被丢掉的行**不得含任何结构字符**
    （反引号 / 花括号 / 方括号）。这条守卫让"围栏写坏了"的情形无法被伪装成
    "说明文字包裹"：

    * ``Sure, here is the JSON:`` + 一个对象 + ``Hope that helps!`` —— 纯说明，
      丢掉后剩下的正好是一个 JSON 值 ⇒ **接受**；
    * ```` ```json {...} ``` `` + 尾随文字 —— 首/尾行带反引号 ⇒ **不剥** ⇒ 拒绝；
    * ```` ```json {...} ``（围栏没闭合）—— 首行带反引号 ⇒ **不剥** ⇒ 拒绝；
    * ``{"a":1}`` + 说明 + ``{"b":2}`` —— 中间那行留在切片里，`json.loads` 直接失败
      ⇒ 拒绝（绝不"取第一个"）。
    """
    lines = text.split("\n")
    start = None
    end = None
    for index, line in enumerate(lines):
        candidate = line.strip(_WS)
        if candidate.startswith("{") or candidate.startswith("["):
            start = index
            break
    if start is None:
        return None
    for index in range(len(lines) - 1, start - 1, -1):
        candidate = lines[index].strip(_WS)
        if candidate.endswith("}") or candidate.endswith("]"):
            end = index
            break
    if end is None:
        return None

    for line in lines[:start] + lines[end + 1 :]:
        stripped = line.strip(_WS)
        if not stripped:
            continue
        if any(marker in stripped for marker in _STRUCTURAL_MARKERS):
            return None

    candidate = "\n".join(lines[start : end + 1]).strip(_WS)
    return candidate or None


def _describe_failure(raw: str) -> str:
    preview = normalize_model_text(raw)[:200].replace("\n", "\\n")
    return (
        "模型输出里抽不出'恰好一个完整 JSON 值'"
        f"（不完整 / 有多段 / 有尾随内容 / 根本不是 JSON）。前 200 字符：{preview!r}"
    )
