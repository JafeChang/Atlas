from pathlib import Path

import pytest

from atlas.cognition import (
    GO_API_KEY_ENV,
    GO_BASE_URL,
    GO_MODEL,
    GO_PROVIDER,
    GO_ROUTE,
    CallStatus,
    CognitionConfig,
    CognitionRequest,
    ConfigError,
    PiSidecarCognitionPort,
)
from tests.test_cognition_support import MockServer, require_sidecar_installed


def test_go_route_uses_its_own_key_model_and_endpoint() -> None:
    env = {
        'OPENCODE_GO_API_KEY': 'go-test-key',
        'ATLAS_OPENAI_API_KEY': 'other-test-key',
        'DEEPSEEK_API_KEY': 'deepseek-test-key',
        'ATLAS_LLM_MODEL': 'openrouter-model',
    }
    config = CognitionConfig.from_env(env=env, route=GO_ROUTE)
    assert config.provider == GO_PROVIDER
    assert config.model == GO_MODEL == "deepseek-v4.1-flash"
    assert config.base_url == GO_BASE_URL
    assert config.api_key_env == GO_API_KEY_ENV
    assert config.api_key == 'go-test-key'
    assert 'go-test-key' not in repr(config)


def test_go_config_can_use_a_local_key_file_and_override_the_model(tmp_path: Path) -> None:
    path = tmp_path / 'atlas.env'
    path.write_text('OPENCODE_GO_API_KEY=file-test-key\nOPENCODE_GO_MODEL=file-model\n')
    config = CognitionConfig.from_env(
        env={'OPENCODE_GO_MODEL': 'process-model', 'OPENCODE_GO_BASE_URL': 'http://127.0.0.1:1/v1'},
        env_file=path, route=GO_ROUTE,
    )
    assert config.model == 'process-model'
    assert config.api_key == 'file-test-key'
    assert config.api_key_env == GO_API_KEY_ENV


def test_missing_go_key_does_not_fall_back_to_another_providers_key() -> None:
    with pytest.raises(ConfigError, match=GO_API_KEY_ENV):
        CognitionConfig.from_env(env={'ATLAS_OPENAI_API_KEY': 'other-test-key'}, route=GO_ROUTE)


def test_opencode_key_selection_requires_the_actual_hostname() -> None:
    config = CognitionConfig.from_env(
        env={'ATLAS_OPENAI_API_KEY': 'other-test-key', 'OPENCODE_GO_API_KEY': 'go-test-key'},
        route='openai-compatible', base_url='https://opencode.ai.example.test/v1',
    )
    assert config.api_key == 'other-test-key'


def test_go_sends_own_client_identity_and_a_stable_session_per_raw() -> None:
    require_sidecar_installed()
    with MockServer() as server:
        config = CognitionConfig.from_env(
            env={'OPENCODE_GO_API_KEY': 'go-test-key'}, route=GO_ROUTE,
            base_url=server.base_url, proxy_url='', timeout_seconds=10,
        )
        port = PiSidecarCognitionPort(config)
        for raw_id, content in (
            ('raw-session-a', 'First batch of the document.'),
            ('raw-session-a', 'Second batch of the same document.'),
            ('raw-session-b', 'Another document.'),
        ):
            result = port.extract(CognitionRequest(
                raw_id=raw_id, external_content=content, candidate_labels=('ai',), kind='industry',
            ))
            assert result.status is CallStatus.OK, result.reason
            assert result.record.tools_declared == result.record.tool_calls == 0
        headers = server.mock.request_headers
        assert len(headers) == 3
        assert all(h['user-agent'] == 'Atlas/0.1' for h in headers)
        assert all(h['authorization'] == 'Bearer go-test-key' for h in headers)
        sessions = [h['x-opencode-session'] for h in headers]
        assert sessions[0] == sessions[1] and sessions[0] != sessions[2]
        assert all(s.startswith('atlas-') and 'raw-session' not in s for s in sessions)
