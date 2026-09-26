"""T-003 验收判据 5：**幂等与可重算**（SPEC §3）+ 配置即代码之外的一切。

判据：

5.1 同输入 + 同配置 ⇒ **同一个幂等键**；输入或配置任一变化 ⇒ 键变化；
5.2 开启缓存后，同键第二次调用**不再请求模型**（`call_count` 不增加），
    返回的结果与第一次**语义等价**（SPEC §3 的可操作定义）；
5.3 调用记录携带 `(code_version, config_version, model_version)` 三元组；
5.4 **凭据路由可辨识但不泄露**：记录里有 provider / base_url / 密钥指纹，
    绝无密钥本体（连 `repr` 都不行）；
5.5 **路由切换是纯配置变更**：官方 DeepSeek 端点 ↔ 环境内兼容凭据，
    只改 provider / base_url / model / api_key_env 四个值；
5.6 缺 key 时**响亮失败**（`ConfigError`），不降级。

> 这些是"引擎可替换"的前提：任务 = f(输入快照, 配置快照) → 新版本对象。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atlas.cognition import (  # noqa: E402
    CONFIG_VERSION,
    DS_API_KEY_ENV,
    DS_BASE_URL,
    DS_MODEL,
    DS_ROUTE,
    OA_ROUTE,
    PROMPT_VERSION,
    CognitionConfig,
    CognitionRequest,
    ConfigError,
    PiSidecarCognitionPort,
    available_routes,
)
from tests.test_cognition_support import (  # noqa: E402
    MockServer,
    fake_config,
    live_config,
    mock_port,
    require_sidecar_installed,
    route_credential_available,
)

REQUEST = CognitionRequest(
    raw_id="raw_idem",
    external_content="The chipmaker raised its capex guidance for advanced nodes.",
    candidate_labels=("ai", "semis"),
    kind="industry",
)
EMPTY_CLAIMS = '{"schema_version": "cognition-output/1", "claims": []}'


def _calls(port: PiSidecarCognitionPort) -> int:
    return len(port.records)


# --------------------------------------------------------------------------- #
# 5.1 / 5.2 幂等键与缓存
# --------------------------------------------------------------------------- #


def test_same_input_and_config_gives_same_key() -> None:
    config = fake_config()
    first = PiSidecarCognitionPort(config)
    second = PiSidecarCognitionPort(config)
    assert first.idempotency_key(REQUEST) == second.idempotency_key(REQUEST)


def test_different_input_changes_key() -> None:
    port = PiSidecarCognitionPort(fake_config())
    other = CognitionRequest(
        raw_id="raw_idem",
        external_content="A completely different sentence.",
        candidate_labels=("ai", "semis"),
        kind="industry",
    )
    assert port.idempotency_key(REQUEST) != port.idempotency_key(other)


def test_different_config_changes_key() -> None:
    base = PiSidecarCognitionPort(fake_config())
    other = PiSidecarCognitionPort(fake_config(model="another-model"))
    assert base.idempotency_key(REQUEST) != other.idempotency_key(REQUEST)
    # 版本三元组任一变化也必须改变幂等键（SPEC §3）。
    bumped_config = PiSidecarCognitionPort(fake_config(config_version="cognition-config/2"))
    bumped_prompt = PiSidecarCognitionPort(fake_config(prompt_version="cognition-extract-prompt/2"))
    assert base.idempotency_key(REQUEST) != bumped_config.idempotency_key(REQUEST)
    assert base.idempotency_key(REQUEST) != bumped_prompt.idempotency_key(REQUEST)


def test_cache_prevents_second_model_call() -> None:
    """幂等的可操作定义：同键第二次调用**不触碰模型**，结果语义等价。"""
    with MockServer() as server:
        server.mock.push(EMPTY_CLAIMS).push(EMPTY_CLAIMS)
        uncached = mock_port(server)
        assert uncached.extract(REQUEST).status.value == "ok"
        assert uncached.extract(REQUEST).status.value == "ok"
        assert server.mock.call_count == 2, "对照：不开缓存时确实调用了模型两次"

    with MockServer() as server:
        server.mock.push(EMPTY_CLAIMS)
        cached = mock_port(server, cache=True)
        first = cached.extract(REQUEST)
        second = cached.extract(REQUEST)
        assert server.mock.call_count == 1, "同键第二次调用不应再请求模型"
    assert first.record.idempotency_key == second.record.idempotency_key
    assert first.record.as_dict() == second.record.as_dict()
    assert _calls(cached) == 1, "缓存命中不应重复记录调用"


def test_cache_key_includes_config_so_different_config_still_calls() -> None:
    with MockServer() as server:
        server.mock.push(EMPTY_CLAIMS).push(EMPTY_CLAIMS)
        first = mock_port(server, cache=True, temperature=0.0)
        second = mock_port(server, cache=True, temperature=0.5)
        first.extract(REQUEST)
        second.extract(REQUEST)
        assert server.mock.call_count == 2


# --------------------------------------------------------------------------- #
# 5.3 版本三元组
# --------------------------------------------------------------------------- #


def test_record_carries_version_triple() -> None:
    with MockServer() as server:
        server.mock.push(EMPTY_CLAIMS)
        result = mock_port(server).extract(REQUEST)
    record = result.record
    assert record.versions_dict() == {
        "code_version": PROMPT_VERSION,
        "config_version": CONFIG_VERSION,
        "model_version": "mock-model",
    }
    payload = record.as_dict()
    assert set(record.versions_dict()) <= set(payload)


def test_model_version_tracks_the_configured_model() -> None:
    with MockServer() as server:
        server.mock.push(EMPTY_CLAIMS)
        result = mock_port(server, model="deepseek-flash", model_version="deepseek-flash").extract(
            REQUEST
        )
    assert result.record.model_version == "deepseek-flash"


# --------------------------------------------------------------------------- #
# 5.4 不泄露密钥
# --------------------------------------------------------------------------- #


def test_secret_never_appears_in_config_repr_or_records() -> None:
    require_sidecar_installed()  # 经真实边车跑一次；依赖未装则跳过（环境问题不是缺陷）
    secret = "sk-t003-super-secret-value"
    config = fake_config(api_key=secret, proxy_url="http://user:pass@127.0.0.1:7897")
    assert secret not in repr(config)
    assert "pass@" not in repr(config)
    with MockServer() as server:
        server.mock.push(EMPTY_CLAIMS)
        port = PiSidecarCognitionPort(config)
        port._node = port.node_binary()  # noqa: SLF001
        object.__setattr__(port, "_sidecar_path", port._sidecar_path)
        # 用 mock 的 base_url 覆盖，但不改 key。
        port.config = config.with_overrides(base_url=server.base_url)
        result = port.extract(REQUEST)
    serialized = json.dumps(result.record.as_dict(), ensure_ascii=False)
    assert secret not in serialized
    assert "pass@" not in serialized
    # 活对照：密钥确实参与了配置（指纹非空），否则"没泄露"是因为根本没设。
    assert result.record.credential_route.endswith(config.api_key_fingerprint())
    assert len(result.record.credential_route.split("#")[-1]) == 16


def test_api_key_does_not_change_config_digest_but_changes_route_label() -> None:
    """同配置换钥匙：`config_digest` 不变（不含凭据），但 `credential_route` 变。"""
    a = fake_config(api_key="key-aaa")
    b = fake_config(api_key="key-bbb")
    assert a.public_digest() == b.public_digest()
    assert a.credential_route() != b.credential_route()
    assert a.credential_route().endswith(a.api_key_fingerprint())
    assert "key-aaa" not in a.credential_route()
    assert "key-bbb" not in b.credential_route()


def test_config_digest_is_stable_across_processes() -> None:
    """同配置的指纹必须可复现（否则"可重算"不成立）。"""
    config = fake_config()
    assert config.public_digest() == fake_config().public_digest()
    assert len(config.public_digest()) == 64


# --------------------------------------------------------------------------- #
# 5.5 路由是纯配置
# --------------------------------------------------------------------------- #


def test_default_route_is_official_deepseek() -> None:
    config = CognitionConfig.from_env(env={}, env_file=Path("/nonexistent"), require_key=False)
    assert config.route_name == DS_ROUTE
    assert config.provider == "deepseek"
    assert config.model == DS_MODEL == "deepseek-flash"
    assert config.base_url == DS_BASE_URL == "https://api.deepseek.com"
    assert config.api_key_env == DS_API_KEY_ENV == "DEEPSEEK_API_KEY"


def test_route_can_be_switched_with_config_only() -> None:
    """**不用改代码**就能切到环境内兼容凭据（SPEC §2.14 决策二）。"""
    env = {
        "ATLAS_OPENAI_API_KEY": "k",
        "ATLAS_LLM_BASE_URL": "https://openrouter.ai/api/v1",
        "ATLAS_LLM_MODEL": "deepseek/deepseek-v4-flash",
    }
    config = CognitionConfig.from_env(env=env, env_file=Path("/nonexistent"), route=OA_ROUTE)
    assert config.provider == "atlas-openai-compatible"
    assert config.model == "deepseek/deepseek-v4-flash"
    assert config.api_key_env == "ATLAS_OPENAI_API_KEY"
    assert config.api_key == "k"


def test_route_env_var_selects_the_route() -> None:
    env = {"ATLAS_COGNITION_ROUTE": OA_ROUTE, "ATLAS_OPENAI_API_KEY": "k"}
    config = CognitionConfig.from_env(env=env, env_file=Path("/nonexistent"))
    assert config.route_name == OA_ROUTE


def test_unknown_route_is_rejected_loudly() -> None:
    with pytest.raises(ConfigError):
        CognitionConfig.from_env(
            env={}, env_file=Path("/nonexistent"), route="does-not-exist", require_key=False
        )


def test_deepseek_base_url_implies_the_deepseek_key_variable() -> None:
    """官方端点用独立的 `DEEPSEEK_API_KEY`——不借用环境内兼容凭据。"""
    config = CognitionConfig.from_env(
        env={"DEEPSEEK_API_KEY": "k"},
        env_file=Path("/nonexistent"),
        base_url="https://api.deepseek.com/v1",
    )
    assert config.api_key_env == "DEEPSEEK_API_KEY"
    assert config.api_key == "k"
    # 活对照：换个 base_url 就会用另一套变量名。
    other = CognitionConfig.from_env(
        env={"ATLAS_OPENAI_API_KEY": "j"},
        env_file=Path("/nonexistent"),
        base_url="https://openrouter.ai/api/v1",
    )
    assert other.api_key_env == "ATLAS_OPENAI_API_KEY"


def test_available_routes_reports_both_without_secrets() -> None:
    table = available_routes()
    assert set(table) == {DS_ROUTE, OA_ROUTE}
    for name, entry in table.items():
        assert set(entry) >= {"provider", "model", "base_url", "api_key_env", "credential_available"}
        assert "api_key" not in entry
        assert isinstance(entry["credential_available"], bool)


# --------------------------------------------------------------------------- #
# 5.6 缺 key 响亮失败
# --------------------------------------------------------------------------- #


def test_missing_key_fails_loudly() -> None:
    with pytest.raises(ConfigError) as excinfo:
        CognitionConfig.from_env(env={}, env_file=Path("/nonexistent"))
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)
    # 活对照：同一个调用给了 key 就成功。
    ok = CognitionConfig.from_env(
        env={"DEEPSEEK_API_KEY": "k"}, env_file=Path("/nonexistent")
    )
    assert ok.api_key == "k"


def test_invalid_config_values_are_rejected() -> None:
    with pytest.raises(ConfigError):
        fake_config(timeout_seconds=0)
    with pytest.raises(ConfigError):
        fake_config(base_url="ftp://example.com")
    with pytest.raises(ConfigError):
        fake_config(temperature=5.0)
    with pytest.raises(ConfigError):
        fake_config(max_output_tokens=0)
    with pytest.raises(ConfigError):
        fake_config(model="")


def test_live_configs_are_constructible_for_available_routes() -> None:
    """有凭据的路由，配置必须能真的构造出来（否则"配置就位"是空话）。"""
    for route in (DS_ROUTE, OA_ROUTE):
        if not route_credential_available(route):
            continue
        config = live_config(route)
        assert config.api_key, f"{route} 的凭据读不到"
        assert config.base_url.startswith("https://")
        assert len(config.api_key_fingerprint()) == 16
