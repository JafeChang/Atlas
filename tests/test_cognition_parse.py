"""T-003 验收判据 4：**围栏与噪声的鲁棒解析**，且不得"乱抓第一个 `{`"。

两部分：

A. **Python 解析器**（`atlas.cognition.parse`）—— 这是运行时真正用的实现；
B. **边车解析器**（`sidecar/json-extract.mjs`，经 `parse_via_sidecar`）——
   证明两套实现**行为一致**（协议里 parse 操作就是为这个对照组存在的）。

关键判据：**畸形 / 多段输出不会被解析成"看似合法的错误结果"**。
每条"必须拒绝"的断言都配一条"必须接受"的活对照，否则
"签名不匹配 / 异常类型不对"会伪装成"拒绝成功"（CLAUDE.md 硬规则 4）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atlas.cognition import ModelOutputError, extract_single_json  # noqa: E402
from tests.test_cognition_support import fake_port  # noqa: E402

VALID = '{"schema_version": "cognition-output/1", "claims": []}'

ACCEPTED = {
    "bare": VALID,
    "fenced_json": "```json\n" + VALID + "\n```",
    "fenced_bare": "```\n" + VALID + "\n```",
    "fenced_with_leading_newline": "\n```json\n" + VALID + "\n```\n",
    "prose_before_and_after": "Sure, here is the JSON:\n" + VALID + "\nHope that helps!",
    "bom": "\ufeff" + VALID,
    "crlf": VALID.replace(", ", ",\r\n"),
    "nested_braces_in_string": json.dumps(
        {
            "schema_version": "cognition-output/1",
            "claims": [
                {
                    "kind": "industry",
                    "value": "ai",
                    "quote": "braces {like} these",
                    "confidence": 0.4,
                }
            ],
        }
    ),
    "escaped_quote_in_string": json.dumps(
        {
            "schema_version": "cognition-output/1",
            "claims": [
                {
                    "kind": "industry",
                    "value": "ai",
                    "quote": 'he said \\"hello\\"',
                    "confidence": 0.4,
                }
            ],
        }
    ),
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_accepted_shapes_parse(name: str) -> None:
    extracted = extract_single_json(ACCEPTED[name])
    assert extracted.value["schema_version"] == "cognition-output/1"
    assert isinstance(extracted.strategy, str) and extracted.strategy


REJECTED = {
    # 说明文字**夹在两段 JSON 之间** ⇒ 绝不"取第一个"。
    "two_objects_with_prose_between": VALID + "\nAnd another one:\n" + VALID,
    "two_objects_back_to_back": VALID + "\n" + VALID,
    "trailing_garbage": VALID + " and then some extra text",
    "trailing_second_object": VALID + ' {"a": 1}',
    "truncated_mid_object": '{"schema_version": "cognition-output/1", "claims": [{"kind": "ind',
    "truncated_unclosed": '{"claims": []',
    "plain_prose": "I cannot help with that request.",
    "empty": "",
    "whitespace_only": "   \n\t ",
    # 围栏没闭合：不剥、也不拼，直接拒绝。
    "unclosed_fence": "```json\n" + VALID,
    # 围栏之后还有内容：畸形。
    "fence_then_trailing": "```json\n" + VALID + "\n```\ntrailing text",
    # 两段都在围栏里：畸形。
    "two_objects_inside_fence": "```json\n" + VALID + "\n" + VALID + "\n```",
    # 顶层标量：不是本契约的合法形状。
    "bare_string": '"just a string"',
    "bare_number": "42",
    "python_repr": "{'schema_version': 'cognition-output/1', 'claims': []}",
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_rejected_shapes_raise(name: str) -> None:
    with pytest.raises(ModelOutputError):
        extract_single_json(REJECTED[name])


def test_control_the_rejections_are_not_vacuous() -> None:
    """活对照：同一批"拒绝"用例里，稍作修补后**必须**被接受。

    证明拒绝是因为**歧义本身**，而不是因为解析器对所有输入都抛错。
    """
    # 把中间那段说明文字去掉 → 仍然两段并排，依旧拒绝（歧义没消失）。
    with pytest.raises(ModelOutputError):
        extract_single_json(VALID + "\n" + VALID)
    # 只留一段 → 接受。
    assert extract_single_json(VALID).value["schema_version"] == "cognition-output/1"
    # 截断的补全 → 拒绝；补全后 → 接受。
    broken = '{"claims": []'
    with pytest.raises(ModelOutputError):
        extract_single_json(broken)
    assert extract_single_json(broken + "}").value["claims"] == []


def test_first_brace_would_have_been_wrong() -> None:
    """点名 SPEC §2.14 要防的写法：`re.search(r"\\{.*\\}")` 会给出**错误**结果。

    这里给出一个反例：两段 JSON + 中间说明文字。贪心正则会跨过中间文字拼出一个
    **非法**的 JSON（然后被 `json.loads` 拒绝），非贪心正则会**静默**返回第一段——
    两者都不是我们要的：我们要的是"明确拒绝"。
    """
    import re

    text = '{"a": 1}\nnote\n{"b": 2}'
    greedy = re.search(r"\{.*\}", text, re.S)
    assert greedy is not None
    with pytest.raises(ValueError):
        json.loads(greedy.group(0))  # 贪心：拼出的东西根本不是合法 JSON
    non_greedy = re.search(r"\{.*?\}", text, re.S)
    assert non_greedy is not None
    assert json.loads(non_greedy.group(0)) == {"a": 1}  # 非贪心：静默丢掉第二段
    # 我们的解析器：两条路都不走，直接拒绝。
    with pytest.raises(ModelOutputError):
        extract_single_json(text)


# --------------------------------------------------------------------------- #
# B. 两套实现一致性
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_sidecar_parser_agrees_on_accepted(name: str) -> None:
    sidecar = fake_port().parse_via_sidecar(ACCEPTED[name])
    assert sidecar["ok"] is True
    assert sidecar["value"] == extract_single_json(ACCEPTED[name]).value


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_sidecar_parser_agrees_on_rejected(name: str) -> None:
    sidecar = fake_port().parse_via_sidecar(REJECTED[name])
    assert sidecar["ok"] is False
    with pytest.raises(ModelOutputError):
        extract_single_json(REJECTED[name])


def test_sidecar_parser_control_accepts_a_valid_document() -> None:
    """活对照：边车解析器**确实**能成功（不是恒返回 ok=False）。"""
    sidecar = fake_port().parse_via_sidecar(VALID)
    assert sidecar["ok"] is True
    assert sidecar["value"]["claims"] == []
    assert sidecar["strategy"] == "direct"
