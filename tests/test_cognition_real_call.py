"""T-003 验收判据 2：**一次返回 JSON 的真实调用**（硬规则 1 / SPEC §4.1）。

这是一条**真实**的端到端证据：Python → 边车（node + pi-ai）→ 远端模型 → 结构化结果。
它**不是 mock**：请求真的发到 provider，token 计数与耗时都来自真实响应。

**默认 skip**：真实调用会消耗凭据/额度，也会被网络与远端状态影响。因此它由
`ATLAS_COGNITION_LIVE_TESTS=1` 显式开启（本任务的证据就是这样跑出来的）：

    export HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897
    ATLAS_COGNITION_LIVE_TESTS=1 ./.venv-new/bin/python -m pytest \
        tests/test_cognition_real_call.py -q -s

这样做的理由：硬规则 4 要求门禁退出码为 0，而"能不能调用远端模型"**不是本提交的
属性**（模型下架、限流、余额、代理都会影响它）。默认 skip 让门禁确定，显式开启让
证据真实——两者都不牺牲：**证据不是用 mock 伪造的，只是不在每次 pytest 里重跑**。
没有凭据时同样 skip。

同时断言 SPEC §2.2 的分工：PI **只输出 quote，不输出坐标**——抽取结果里
**没有**任何坐标字段（`char_start` / `char_end` / `block_id` 等）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atlas.cognition import (  # noqa: E402
    CallStatus,
    CognitionRequest,
    DS_ROUTE,
    OA_ROUTE,
)
from tests.test_cognition_support import (  # noqa: E402
    live_port,
    live_routes,
)

DOCUMENT = (
    "TSMC said it will expand advanced packaging capacity at its Chiayi facility "
    "next year, citing strong demand from AI accelerator customers. The company "
    "also flagged higher electricity costs in Taiwan as a margin risk."
)

CANDIDATES = ("ai", "semis", "energy")

LIVE_ENV_VAR = "ATLAS_COGNITION_LIVE_TESTS"


def _require_live_enabled() -> None:
    if os.environ.get(LIVE_ENV_VAR) != "1":
        pytest.skip(
            f"真实调用默认不跑（会消耗凭据/额度且受远端状态影响）；"
            f"设 {LIVE_ENV_VAR}=1 显式开启"
        )


def _run(route: str):
    port = live_port(route)
    request = CognitionRequest(
        raw_id=f"raw_real_{route}",
        external_content=DOCUMENT,
        candidate_labels=CANDIDATES,
        kind="industry",
    )
    return port, port.extract(request)


@pytest.mark.parametrize("route", [DS_ROUTE, OA_ROUTE])
def test_criterion_2_real_call_returns_structured_json(route: str) -> None:
    _require_live_enabled()
    if route not in live_routes():
        pytest.skip(f"路由 {route} 的凭据不可用")

    port, result = _run(route)
    config = port.config

    # 路由可辨识（不含密钥）：provider / model / credential_route 都写进记录。
    assert config.base_url.split("://", 1)[-1].split("/", 1)[0] in result.record.credential_route
    assert result.record.model == config.model
    assert result.record.provider == config.provider

    if result.status is not CallStatus.OK:
        # 模型被下架 / 限流 / 余额不足都是**可能发生**的运营事件，不是本任务的缺陷。
        # 但"降级"必须是显式的、带原因码的，而且绝不能携带编造的结果。
        assert result.reason is not None, "降级必须带原因码"
        assert result.claims == () and result.output is None, "降级不得返回编造结果"
        assert result.record.degraded is True
        pytest.skip(
            f"路由 {route} 当前不可用（已如实降级为 {result.reason.value}）："
            f"{result.record.detail[:160]}"
        )

    # --- 真实数字：必须来自响应，而不是零值 ---------------------------------
    record = result.record
    assert record.elapsed_ms > 0, "耗时为 0，说明没有真正发起调用"
    assert record.usage.total_tokens > 0, f"token 计数为 0：{record.usage.as_dict()}"
    assert record.usage.input_tokens > 0
    assert record.usage.output_tokens > 0

    # --- 结构化结果 ---------------------------------------------------------
    assert result.output is not None
    assert result.claims, "真实模型没有产出任何 claim，无法证明结构化返回"
    for claim in result.claims:
        assert claim.quote.strip(), "quote 不得为空"
        assert claim.kind == "industry"
        assert claim.value in CANDIDATES, f"value 不在候选标签表内：{claim.value!r}"
        assert 0.0 <= claim.confidence <= 1.0

    # SPEC §2.2：PI **只输出 quote，不输出坐标**。
    dumped = result.output.model_dump()
    assert "claims" in dumped
    for claim in dumped["claims"]:
        assert set(claim) == {"kind", "value", "quote", "confidence"}, (
            f"claim 里出现了契约外的字段（可能是坐标）：{sorted(claim)}"
        )

    # 零工具。
    assert record.tools_declared == 0
    assert record.tool_calls == 0

    # 版本三元组（SPEC §3 / §5 登记 #4）。
    assert record.code_version and record.config_version and record.model_version
    assert record.model_version == config.model


def test_criterion_2_quote_is_verbatim_from_the_document() -> None:
    """quote 必须能在原文里**逐字**找到（否则 §2.2 的确定性匹配必然失败）。"""
    _require_live_enabled()
    if DS_ROUTE not in live_routes():
        pytest.skip("官方路由凭据不可用")
    _, result = _run(DS_ROUTE)
    if result.status is not CallStatus.OK:
        pytest.skip(f"官方路由当前不可用（已如实降级为 {result.reason.value}）")
    assert result.claims, "没有 claim，无法验证 quote"
    for claim in result.claims:
        assert claim.quote in DOCUMENT, f"quote 不是原文逐字切片：{claim.quote!r}"


def test_criterion_2_latency_and_cost_are_reported() -> None:
    """把真实耗时 / token / 成本**打印出来**（T-105 批量分类的预算依据）。"""
    _require_live_enabled()
    if DS_ROUTE not in live_routes():
        pytest.skip("官方路由凭据不可用")
    port, result = _run(DS_ROUTE)
    record = result.record
    usage = record.usage
    cost = "未报告" if usage.cost_total is None else f"{usage.cost_total} {usage.cost_currency}"
    message = (
        f"\n[真实调用] route={port.config.route_name} provider={record.provider}"
        f" model={record.model} response_model={record.response_model or '(未报告)'}"
        f"\n  status={record.status.value} reason={record.reason.value if record.reason else None}"
        f"\n  elapsed_ms={record.elapsed_ms}"
        f" input={usage.input_tokens} output={usage.output_tokens}"
        f" reasoning={usage.reasoning_tokens} cache_read={usage.cache_read_tokens}"
        f" total={usage.total_tokens}"
        f"\n  thinking_chars={record.thinking_chars} cost={cost}"
        f"\n  claims={len(result.claims)} parse_strategy={record.parse_strategy}"
    )
    print(message)
    # 无论成功还是降级，记录都必须携带可观测的耗时；成功时还必须有 token 计数。
    assert record.elapsed_ms > 0
    if result.status is CallStatus.OK:
        assert usage.total_tokens > 0


def test_criterion_2_records_model_and_prompt_versions() -> None:
    """每次调用记录 model / prompt 版本（§5 登记 #4）。"""
    _require_live_enabled()
    if DS_ROUTE not in live_routes():
        pytest.skip("官方路由凭据不可用")
    port, result = _run(DS_ROUTE)
    record = result.record
    assert record.model == port.config.model
    assert record.code_version == port.config.prompt_version
    assert record.versions_dict()["model_version"] == port.config.model
    assert len(record.idempotency_key) == 64
    assert len(record.config_digest) == 64
    assert record.sidecar_code_version.startswith("sha256:")


def test_live_gate_skips_by_default() -> None:
    """**活对照**：不加环境变量时，真实调用类测试必须是被**跳过**的。

    单独跑一个断言成本为零的"门"检查：如果这个测试失败，说明真实调用会无条件
    跑起来，门禁就不再确定。
    """
    if os.environ.get(LIVE_ENV_VAR) == "1":
        pytest.skip("本次运行显式开启了真实调用")
    with pytest.raises(BaseException) as excinfo:
        _require_live_enabled()
    assert "ATLAS_COGNITION_LIVE_TESTS" in str(excinfo.value)
