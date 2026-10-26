"""T-003 验收判据 3：**降级路径可用且被测试**，且与"响亮失败"明确区分。

## 分界（本包的裁决，写在这里也写在 `PROTOCOL.md`）

**降级为"未分类"**（`CognitionResult.status == unclassified`，必带原因码）——
判据是「**PI 没拿到可用的模型输出**」：

| 原因码 | 触发 | 为什么是降级 |
|---|---|---|
| `unreachable_model` | 端点不可达 / 连接被拒 / DNS 失败 | 拿不到输出；下次模型可用时可重跑（Proposed 可覆写） |
| `timeout` | 边车看门狗超时、模型侧超时 | 同上 |
| `http_error` | 任意非 2xx（402 余额 / 429 限流 / 5xx） | 同上，且保留了探测到的状态码 |
| `model_deprecated` | HTTP 404 且响应体明确说模型已下架 | SPEC §2.14 决策三第 1 类；"模型不在了"不是本系统的 bug |
| `empty_completion` | 2xx 但没有任何可用文本 | 同上 |
| `unparseable_output` | 有文本，但抽不出"恰好一个完整 JSON 值"（围栏 / 说明 / 截断 / 多段） | 同上 |
| `sidecar_error` | 边车报错但没有更具体的原因 | 兜底，**原因原文保留在 `detail`** |

**响亮失败**（抛异常，绝不降级）——判据是「**这是必须修的 bug / 接线错误**」：

| 异常 | 触发 | 为什么不能降级 |
|---|---|---|
| `ModelEnvelopeError` | 解析出了 JSON，但结构违反冻结契约（缺字段 / 多余字段 / 类型 / 越界） | 契约违例是 bug；静默降级会把它藏起来 |
| `IsolationViolationError` | 边车报告注册了非零工具 | 隔离前提不成立，必须立刻可见（§4.7） |
| `SidecarUnavailableError` | node 不可用 / 依赖未安装 / 进程启动失败 | 环境/接线问题，与"模型不可用"是两回事 |
| `ProtocolError` | 协议版本不符 / 无帧 / 多帧 / 帧不可解析 / 未知 status / 未实现操作 | 代码与代码之间的契约 |
| `ConfigError` | 缺 API key / 配置非法 / 边车报 config_error | 配置缺失不是"模型不可用" |

> **不得静默吞掉**：本文件最后一条测试断言 `DegradeReason` 的**每个取值**都被至少
> 一条测试覆盖——新增一个原因码却没人测，就会在这里失败。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atlas.cognition import (  # noqa: E402
    CallStatus,
    CognitionConfig,
    CognitionRequest,
    DegradeReason,
    ModelEnvelopeError,
    PiSidecarCognitionPort,
    ProtocolError,
)
from tests.test_cognition_support import (  # noqa: E402
    MockServer,
    completion_body,
    fake_config,
    mock_port,
    require_sidecar_installed,
)

REQUEST = CognitionRequest(
    raw_id="raw_degrade",
    external_content="TSMC expands advanced packaging capacity for AI accelerators.",
    candidate_labels=("ai", "semis"),
    kind="industry",
)


def _reject_codes(reason: DegradeReason, detail: str = "boom") -> dict:
    """一个总是返回 `detail` 的降级响应体构造器（绕开真实 HTTP）。"""
    return {
        "status": "degraded",
        "reason": reason.value,
        "detail": detail,
        "model": "mock-model",
        "provider": "test-provider",
        "response_model": "",
        "elapsed_ms": 1,
        "text": "",
        "usage": None,
    }


# --------------------------------------------------------------------------- #
# 降级：端点不可达
# --------------------------------------------------------------------------- #


def test_unreachable_endpoint_degrades_to_unclassified() -> None:
    """端口 1 上没有服务 ⇒ 连接被拒 ⇒ 降级，而不是抛异常。"""
    require_sidecar_installed()  # 这条例外用**真实边车**；依赖未装则跳过（环境问题不是缺陷）
    port = PiSidecarCognitionPort(fake_config(base_url="http://127.0.0.1:1/v1"))
    result = port.extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.claims == ()
    assert result.output is None
    assert result.reason in (DegradeReason.UNREACHABLE_MODEL, DegradeReason.TIMEOUT)
    assert result.record.degraded is True
    assert result.record.detail


# --------------------------------------------------------------------------- #
# 降级：HTTP 错误 / 模型下架
# --------------------------------------------------------------------------- #


def test_http_402_insufficient_balance_degrades() -> None:
    with MockServer() as server:
        server.mock.push(
            lambda _payload: (
                402,
                {"error": {"message": "Insufficient Balance", "type": "insufficient_quota"}},
            )
        )
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.HTTP_ERROR
    assert "402" in result.record.detail


def test_http_500_degrades() -> None:
    with MockServer() as server:
        server.mock.push(lambda _p: (500, {"error": {"message": "internal error"}}))
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.HTTP_ERROR


def test_http_404_deprecated_model_degrades_with_model_deprecated() -> None:
    """SPEC §2.14 决策三第 1 类的实测形态：404 + `has been deprecated`。"""
    with MockServer() as server:
        server.mock.push(
            lambda _p: (
                404,
                {
                    "error": {
                        "message": "This model has been deprecated. It is recommended to migrate.",
                        "code": 404,
                    }
                },
            )
        )
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.MODEL_DEPRECATED
    assert "deprecated" in result.record.detail.lower()


def test_http_404_without_deprecation_words_is_plain_http_error() -> None:
    """活对照：同样是 404，但没有"下架"字样 ⇒ 归到 `http_error`（不误判）。"""
    with MockServer() as server:
        server.mock.push(lambda _p: (404, {"error": {"message": "no such route"}}))
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.HTTP_ERROR


# --------------------------------------------------------------------------- #
# 降级：空补全
# --------------------------------------------------------------------------- #


def test_empty_completion_degrades() -> None:
    with MockServer() as server:
        server.mock.push(
            lambda _p: (200, completion_body(""))
        )
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.EMPTY_COMPLETION


# --------------------------------------------------------------------------- #
# 降级：不可解析的输出（围栏也算不可解析吗？不算——围栏是**可**解析的）
# --------------------------------------------------------------------------- #


def test_unparseable_output_degrades() -> None:
    with MockServer() as server:
        server.mock.push("I cannot help with that request.")
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.UNPARSEABLE_OUTPUT
    assert result.output is None


def test_truncated_json_degrades_rather_than_guessing() -> None:
    with MockServer() as server:
        server.mock.push('{"schema_version": "cognition-output/1", "claims": [{"kind": "ind')
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.UNPARSEABLE_OUTPUT


def test_multi_value_output_degrades_rather_than_picking_one() -> None:
    """两段 JSON：宁可降级，也不"取第一个"。"""
    with MockServer() as server:
        server.mock.push(
            '{"schema_version": "cognition-output/1", "claims": []}\n'
            '{"schema_version": "cognition-output/1", "claims": []}'
        )
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.UNPARSEABLE_OUTPUT


def test_fenced_json_is_NOT_degraded() -> None:
    """**活对照**：围栏包裹是**可**解析的（鲁棒解析的正面判据）。"""
    with MockServer() as server:
        server.mock.push(
            "```json\n"
            '{"schema_version": "cognition-output/1", "claims": ['
            '{"kind": "industry", "value": "semis", "quote": "advanced packaging", '
            '"confidence": 0.7}]}\n'
            "```"
        )
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.OK, result.record.detail
    assert result.record.parse_strategy == "fence"
    assert result.claims[0].value == "semis"


# --------------------------------------------------------------------------- #
# 响亮失败：契约违例
# --------------------------------------------------------------------------- #


def test_extra_key_in_envelope_fails_loudly() -> None:
    """`extra="forbid"` 在**线上**生效：多余字段 ⇒ `ModelEnvelopeError`。"""
    with MockServer() as server:
        server.mock.push(
            json.dumps(
                {
                    "schema_version": "cognition-output/1",
                    "claims": [],
                    "char_start": 3,
                }
            )
        )
        with pytest.raises(ModelEnvelopeError) as excinfo:
            mock_port(server).extract(REQUEST)
    assert "char_start" in str(excinfo.value)


def test_claim_with_coordinates_fails_loudly() -> None:
    """SPEC §2.2：PI **不得**输出坐标——claim 里带坐标在类型层就构造不出来。"""
    with MockServer() as server:
        server.mock.push(
            json.dumps(
                {
                    "schema_version": "cognition-output/1",
                    "claims": [
                        {
                            "kind": "industry",
                            "value": "ai",
                            "quote": "advanced packaging",
                            "confidence": 0.9,
                            "char_start": 12,
                            "char_end": 30,
                        }
                    ],
                }
            )
        )
        with pytest.raises(ModelEnvelopeError) as excinfo:
            mock_port(server).extract(REQUEST)
    assert "char_start" in str(excinfo.value)


def test_confidence_out_of_range_fails_loudly() -> None:
    with MockServer() as server:
        server.mock.push(
            json.dumps(
                {
                    "schema_version": "cognition-output/1",
                    "claims": [
                        {
                            "kind": "industry",
                            "value": "ai",
                            "quote": "advanced packaging",
                            "confidence": 1.5,
                        }
                    ],
                }
            )
        )
        with pytest.raises(ModelEnvelopeError):
            mock_port(server).extract(REQUEST)


def test_missing_claims_key_fails_loudly() -> None:
    with MockServer() as server:
        server.mock.push('{"schema_version": "cognition-output/1"}')
        with pytest.raises(ModelEnvelopeError):
            mock_port(server).extract(REQUEST)


def test_wrong_schema_version_fails_loudly() -> None:
    with MockServer() as server:
        server.mock.push('{"schema_version": "cognition-output/2", "claims": []}')
        with pytest.raises(ModelEnvelopeError):
            mock_port(server).extract(REQUEST)


def test_top_level_array_fails_loudly() -> None:
    """解析出了 JSON，但顶层是数组 ⇒ 契约违例（不是"不可解析"）。"""
    with MockServer() as server:
        server.mock.push("[]")
        with pytest.raises(ModelEnvelopeError):
            mock_port(server).extract(REQUEST)


def test_valid_empty_claims_is_ok() -> None:
    """**活对照**：合法的空结果**不**被当成降级（"没有可分类的东西"是正常结论）。"""
    with MockServer() as server:
        server.mock.push('{"schema_version": "cognition-output/1", "claims": []}')
        result = mock_port(server).extract(REQUEST)
    assert result.status is CallStatus.OK
    assert result.claims == ()


# --------------------------------------------------------------------------- #
# 响亮失败：配置 / 环境
# --------------------------------------------------------------------------- #


def test_missing_api_key_fails_loudly_not_degraded() -> None:
    from atlas.cognition import ConfigError

    with pytest.raises(ConfigError):
        CognitionConfig.from_env(env={}, env_file=Path("/nonexistent-env-file"))


def test_sidecar_reports_config_error_fails_loudly() -> None:
    with MockServer() as server:
        server.mock.push(lambda _p: (200, completion_body("{}")))
        port = mock_port(server)
        # 直接把 sidecar 的 config_error 分支喂进结果转换层。
        from atlas.cognition.adapter import _Outcome, _usage_from  # noqa: F401

        frame = {
            "protocol": "atlas.cognition.sidecar/1",
            "toolsRegistered": 0,
            "result": {"status": "degraded", "reason": "config_error", "detail": "no api key"},
        }
        with pytest.raises(Exception) as excinfo:
            port._to_result(  # noqa: SLF001 - 直接测结果转换层
                REQUEST, "key", _Outcome(frame=frame, elapsed_ms=1, return_code=0), 0
            )
    assert "config_error" in str(excinfo.value) or "配置错误" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 覆盖度自检
# --------------------------------------------------------------------------- #


def test_every_degrade_reason_is_covered_by_a_real_test() -> None:
    """**不得静默吞掉**：每个原因码都必须有**确实存在**的测试覆盖。

    这里不只是写一段说明性文字——它会去 `tests/` 里**按名字找**那些测试函数。
    改名 / 删掉测试会让这条断言失败，从而防止"覆盖说明"变成空话。
    """
    coverage = {
        DegradeReason.UNREACHABLE_MODEL: [
            ("test_cognition_degrade.py", "test_unreachable_endpoint_degrades_to_unclassified"),
        ],
        DegradeReason.TIMEOUT: [
            ("test_cognition_protocol.py", "test_watchdog_timeout_degrades_with_timeout_reason"),
        ],
        DegradeReason.HTTP_ERROR: [
            ("test_cognition_degrade.py", "test_http_402_insufficient_balance_degrades"),
            ("test_cognition_degrade.py", "test_http_500_degrades"),
        ],
        DegradeReason.MODEL_DEPRECATED: [
            ("test_cognition_degrade.py", "test_http_404_deprecated_model_degrades_with_model_deprecated"),
        ],
        DegradeReason.EMPTY_COMPLETION: [
            ("test_cognition_degrade.py", "test_empty_completion_degrades"),
        ],
        DegradeReason.UNPARSEABLE_OUTPUT: [
            ("test_cognition_degrade.py", "test_unparseable_output_degrades"),
            ("test_cognition_degrade.py", "test_truncated_json_degrades_rather_than_guessing"),
            ("test_cognition_degrade.py", "test_multi_value_output_degrades_rather_than_picking_one"),
        ],
        DegradeReason.SIDECAR_ERROR: [
            ("test_cognition_protocol.py", "test_unmapped_degrade_reason_is_preserved_not_dropped"),
        ],
    }

    missing_reasons = set(DegradeReason) - set(coverage)
    assert not missing_reasons, (
        f"这些降级原因码没有测试覆盖：{sorted(r.value for r in missing_reasons)}"
    )

    tests_dir = Path(__file__).resolve().parent
    for reason, entries in coverage.items():
        for filename, test_name in entries:
            source = (tests_dir / filename).read_text(encoding="utf-8")
            assert f"def {test_name}(" in source, (
                f"{reason.value} 声称由 {filename}::{test_name} 覆盖，但该测试不存在"
            )


def test_degrade_reason_values_are_stable() -> None:
    """原因码是**对外可观测的契约值**（会被写进调用记录），改名即破坏兼容。"""
    assert {reason.value for reason in DegradeReason} == {
        "unreachable_model",
        "timeout",
        "http_error",
        "model_deprecated",
        "empty_completion",
        "unparseable_output",
        "sidecar_error",
    }
