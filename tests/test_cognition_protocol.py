"""T-003 验收判据 3/4（协议侧）：IPC 协议本身的行为。

覆盖四类**真实故障**，每一类都先跑一次"正常对照"，再断言故障路径：

1. **stdout 污染**——库/运行时往数据通道写非帧内容（这类边车最常见的真实故障）。
   边车有一个显式测试钩子 `noise: true` 会在**同一通道**上先写警告行、再写一个
   "看起来像帧但不是帧"的 JSON 行，最后才写真帧。读取方必须只认指纹帧。
   注意真实环境里 Node 也会往 **stderr** 打 `[UNDICI-EHPA] Warning: ...`；
   该警告不进数据通道，也无法干扰协议。
2. **看门狗超时**——子进程不产出、也不退出（桩进程 sleep）。
3. **协议版本不符**——job 里的 `protocol` 与边车不一致。
4. **不支持的操作**——边车返回 `unsupported_operation`。
5. **job 行不是 JSON**——边车回 `bad_job` 帧并以退出码 2 结束（响亮）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atlas.cognition import (  # noqa: E402
    FRAME_PREFIX,
    PROTOCOL,
    CallStatus,
    CognitionRequest,
    DegradeReason,
    PiSidecarCognitionPort,
    ProtocolError,
)
from atlas.cognition.adapter import _Outcome  # noqa: E402
from tests.test_cognition_support import (  # noqa: E402
    MockServer,
    fake_config,
    mock_port,
    require_sidecar_installed,
)

REQUEST = CognitionRequest(
    raw_id="raw_protocol",
    external_content="A short document about chips.",
    candidate_labels=("ai",),
    kind="industry",
)


def _stub(tmp_path: Path, body: str, name: str = "stub.mjs") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _port_for(path: Path, **overrides):
    port = PiSidecarCognitionPort(
        fake_config(**overrides), sidecar_path=path, require_installed=False
    )
    return port


# --------------------------------------------------------------------------- #
# 1. stdout 污染
# --------------------------------------------------------------------------- #


def test_stdout_noise_is_ignored_and_frame_still_parsed() -> None:
    """**活对照 + 故障路径**：同一份 job，`noise=False` 与 `noise=True` 都必须成功。"""
    with MockServer() as server:
        server.mock.push('{"schema_version": "cognition-output/1", "claims": []}')
        server.mock.push('{"schema_version": "cognition-output/1", "claims": []}')
        port = mock_port(server)
        node = port.node_binary()

        clean = port.raw_outcome(
            {
                "protocol": PROTOCOL,
                "operation": "call",
                "jobId": "clean",
                "call": {
                    "provider": "p",
                    "model": "mock-model",
                    "baseUrl": server.base_url,
                    "apiKey": "k",
                    "systemPrompt": "s",
                    "userContent": "u",
                    "noProxy": "127.0.0.1,localhost,::1",
                },
            }
        )
        noisy = port.raw_outcome(
            {
                "protocol": PROTOCOL,
                "operation": "call",
                "jobId": "noisy",
                "noise": True,
                "call": {
                    "provider": "p",
                    "model": "mock-model",
                    "baseUrl": server.base_url,
                    "apiKey": "k",
                    "systemPrompt": "s",
                    "userContent": "u",
                    "noProxy": "127.0.0.1,localhost,::1",
                },
            }
        )

    assert node.version
    assert clean.frame is not None and clean.noise == ()
    assert noisy.frame is not None, "噪声存在时仍然必须解析出唯一真帧"
    # 噪声行被**记录**下来（可观测），但没有被当成数据。
    assert noisy.noise, "注入的噪声没有被记录，说明测试钩子没生效"
    assert any("ExperimentalWarning" in line for line in noisy.noise)
    assert any('"toolsRegistered": 999' in line for line in noisy.noise)
    # 关键：真帧的 toolsRegistered 仍是 0，而不是被噪声里的 999 覆盖。
    assert noisy.frame["toolsRegistered"] == 0
    assert clean.frame["toolsRegistered"] == noisy.frame["toolsRegistered"] == 0


def test_real_sidecar_stderr_warning_does_not_pollute_the_protocol() -> None:
    """真实边车跑一次：stderr 上确实有 `[UNDICI-EHPA] Warning`，但帧照常解析。"""
    port = fake_port_for_real_sidecar()
    report = port.inspect()
    assert report["toolsRegistered"] == 0


def fake_port_for_real_sidecar() -> PiSidecarCognitionPort:
    return PiSidecarCognitionPort(fake_config(base_url="http://127.0.0.1:1/v1"))


# --------------------------------------------------------------------------- #
# 2. 看门狗超时
# --------------------------------------------------------------------------- #


def test_watchdog_timeout_degrades_with_timeout_reason(tmp_path: Path) -> None:
    sleepy = _stub(tmp_path, "process.stdin.resume();\nsetTimeout(() => {}, 600000);\n")
    port = _port_for(sleepy, timeout_seconds=1.0)
    result = port.extract(REQUEST)
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.TIMEOUT
    assert "看门狗超时" in result.record.detail
    assert result.record.degraded is True


def test_watchdog_control_process_that_answers_quickly_is_not_degraded(tmp_path: Path) -> None:
    """**活对照**：同一超时配置下，一个会立刻回帧的进程**不**降级。"""
    frame = (
        FRAME_PREFIX
        + '{"protocol": "atlas.cognition.sidecar/1", "jobId": "j", "sidecarSha256": "sha256:'
        + "0" * 64
        + '", "node": "v22", "toolsRegistered": 0, "result": {"status": "text", '
        '"text": "{\\"schema_version\\": \\"cognition-output/1\\", \\"claims\\": []}", '
        '"model": "m", "provider": "p", "usage": null}}\n'
    )
    quick = _stub(tmp_path, f"process.stdout.write({frame!r});\n")
    port = _port_for(quick, timeout_seconds=1.0)
    result = port.extract(REQUEST)
    assert result.status is CallStatus.OK
    assert result.claims == ()


# --------------------------------------------------------------------------- #
# 3. 协议版本
# --------------------------------------------------------------------------- #


def test_protocol_mismatch_is_rejected_loudly() -> None:
    """job 的 `protocol` 与边车不一致 ⇒ 边车回 error 帧 ⇒ Python 侧响亮失败。"""
    port = fake_port_for_real_sidecar()
    # 活对照：正确版本号的同一 job 走得通。
    ok = port.raw_outcome({"protocol": PROTOCOL, "operation": "inspect"})
    assert ok.frame is not None and ok.return_code == 0

    with pytest.raises(ProtocolError) as excinfo:
        port.raw_outcome({"protocol": "atlas.cognition.sidecar/999", "operation": "inspect"})
    assert "协议校验失败" in str(excinfo.value)


def test_unsupported_operation_is_rejected_loudly() -> None:
    port = fake_port_for_real_sidecar()
    # 活对照：受支持的操作成功。
    ok = port.raw_outcome({"protocol": PROTOCOL, "operation": "inspect"})
    assert ok.frame is not None

    with pytest.raises(ProtocolError) as excinfo:
        port.raw_outcome({"protocol": PROTOCOL, "operation": "definitely-not-an-op"})
    assert "不支持" in str(excinfo.value)


def test_bad_job_line_yields_bad_job_frame_and_exit_code_2() -> None:
    port = fake_port_for_real_sidecar()
    outcome = port.raw_outcome({"protocol": PROTOCOL, "operation": "inspect"})
    assert outcome.return_code == 0
    # 活对照：正常 job 退出码 0；下面用一个"非 JSON 的 job 行"走另一条路。
    import subprocess

    node = port.node_binary()
    process = subprocess.run(
        [node.path, str(port._sidecar_path)],  # noqa: SLF001
        input=b"{not json}\n",
        capture_output=True,
        env=port.sidecar_env(),
        cwd=str(port._sidecar_path.parent),  # noqa: SLF001
        timeout=60,
    )
    assert process.returncode == 2, process.stderr.decode()[-500:]
    line = process.stdout.decode("utf-8").splitlines()[0]
    assert line.startswith(FRAME_PREFIX)
    assert '"bad_job"' in line


# --------------------------------------------------------------------------- #
# 4. 无帧 / 多帧 / 未知 status
# --------------------------------------------------------------------------- #


def test_no_frame_at_all_is_a_loud_protocol_error(tmp_path: Path) -> None:
    silent = _stub(tmp_path, "process.stdin.resume();\n")
    port = _port_for(silent, timeout_seconds=1.0)
    with pytest.raises(ProtocolError) as excinfo:
        port.extract(REQUEST)
    assert "没有产出任何协议帧" in str(excinfo.value)


def test_two_frames_for_one_job_is_a_loud_protocol_error(tmp_path: Path) -> None:
    frame = (
        FRAME_PREFIX
        + '{"protocol": "atlas.cognition.sidecar/1", "jobId": "j", "sidecarSha256": "sha256:'
        + "0" * 64
        + '", "node": "v22", "toolsRegistered": 0, "result": {"status": "text", "text": "{}", '
        '"model": "m", "provider": "p", "usage": null}}\n'
    )
    twice = _stub(tmp_path, f"process.stdout.write({frame!r} + {frame!r});\n")
    port = _port_for(twice)
    with pytest.raises(ProtocolError) as excinfo:
        port.extract(REQUEST)
    assert "2 个帧" in str(excinfo.value)


def test_unknown_status_is_a_loud_protocol_error() -> None:
    port = fake_port_for_real_sidecar()
    frame = {
        "protocol": PROTOCOL,
        "toolsRegistered": 0,
        "result": {"status": "some-new-status"},
    }
    with pytest.raises(ProtocolError):
        port._to_result(  # noqa: SLF001
            REQUEST, "k", _Outcome(frame=frame, elapsed_ms=1, return_code=0), 0
        )


def test_unmapped_degrade_reason_is_preserved_not_dropped() -> None:
    """边车报了一个本包不认识的原因码 ⇒ 归到 `sidecar_error`，但**原文保留**。"""
    port = fake_port_for_real_sidecar()
    frame = {
        "protocol": PROTOCOL,
        "toolsRegistered": 0,
        "result": {"status": "degraded", "reason": "brand_new_reason", "detail": "why"},
    }
    result = port._to_result(  # noqa: SLF001
        REQUEST, "k", _Outcome(frame=frame, elapsed_ms=1, return_code=0), 0
    )
    assert result.status is CallStatus.UNCLASSIFIED
    assert result.reason is DegradeReason.SIDECAR_ERROR
    assert "brand_new_reason" in result.record.detail


# --------------------------------------------------------------------------- #
# 5. 依赖缺失 ⇒ 响亮失败（不是降级）
# --------------------------------------------------------------------------- #


def test_missing_sidecar_source_is_a_loud_failure(tmp_path: Path) -> None:
    from atlas.cognition import SidecarUnavailableError

    port = PiSidecarCognitionPort(
        fake_config(), sidecar_path=tmp_path / "not-there.mjs", require_installed=False
    )
    with pytest.raises(SidecarUnavailableError):
        port.extract(REQUEST)


def test_missing_node_modules_is_a_loud_failure(tmp_path: Path) -> None:
    from atlas.cognition import SidecarUnavailableError

    source = tmp_path / "sidecar" / "run.mjs"
    source.parent.mkdir()
    source.write_text("// nothing\n", encoding="utf-8")
    port = PiSidecarCognitionPort(fake_config(), sidecar_path=source)
    with pytest.raises(SidecarUnavailableError) as excinfo:
        port.extract(REQUEST)
    assert "依赖未安装" in str(excinfo.value)
    # 活对照：同一路径把 require_installed 关掉后，**不再**因为依赖而失败
    # （说明上面失败的原因确实是依赖检查，而不是别的东西）。
    permissive = PiSidecarCognitionPort(
        fake_config(timeout_seconds=1.0), sidecar_path=source, require_installed=False
    )
    with pytest.raises(ProtocolError):
        permissive.extract(REQUEST)
