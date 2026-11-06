import json

import pytest
from fastapi.testclient import TestClient

from tool_adapters.config import ConfigurationError, load_configuration
from tool_adapters.server import application, create_app


def test_foundation_health_metadata_and_readiness_work_without_core(configuration):
    with TestClient(create_app(configuration, source_revision="a" * 40)) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert set(client.get("/contract").json()) == {"service", "contract_version", "schema_sha256"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["mode"] == "foundation"
        assert ready.json()["execution_enabled"] is False


@pytest.mark.parametrize(
    "path",
    [
        "/internal/v1/tools/atlassian/mcp",
        "/internal/v1/tools/github/mcp",
        "/internal/v1/tools/slack/mcp",
        "/internal/v1/tools/jira/mcp",
        "/internal/v1/tool-adapters/observe",
        "/internal/v1/tool-adapters/authorize",
    ],
)
def test_direct_or_forged_requests_are_denied_without_echoing_credentials(configuration, path):
    with TestClient(create_app(configuration, source_revision="a" * 40)) as client:
        for headers in (
            {},
            {
                "Authorization": "Bearer header-secret-sentinel",
                "x-caller-identity": "forged",
                "x-adp-edge-provenance": "forged",
                "x-adp-assertion": "forged",
            },
        ):
            response = client.post(path, headers=headers, content="body-secret-sentinel")
            assert response.status_code == 403
            assert response.json() == {"error": "adapter_authority_required"}
            assert "sentinel" not in response.text


@pytest.mark.parametrize("revision", [None, "bad", "b" * 40])
def test_unmatched_build_never_reports_ready(configuration, revision):
    with TestClient(create_app(configuration, source_revision=revision)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


def test_missing_configuration_does_not_load_cloud_credentials_or_report_ready(monkeypatch):
    monkeypatch.delenv("ADP_TOOL_ADAPTER_CONFIG", raising=False)
    with TestClient(application()) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404


@pytest.mark.parametrize(
    "content",
    [
        '{"schema_version":"1.0","schema_version":"2.0"}',
        '{"client_secret":"config-secret-sentinel"}',
        "not json",
        "x" * 65537,
    ],
)
def test_bad_protected_configuration_is_bounded_and_does_not_leak(tmp_path, monkeypatch, content, capsys):
    path = tmp_path / "config.json"
    path.write_text(content)
    with pytest.raises(ConfigurationError, match="^tool_adapter_configuration_invalid$"):
        load_configuration(str(path))
    monkeypatch.setenv("ADP_TOOL_ADAPTER_CONFIG", str(path))
    with TestClient(application()) as client:
        assert client.get("/readyz").status_code == 503
        assert "sentinel" not in client.get("/contract").text
    assert "sentinel" not in str(capsys.readouterr())


def test_loads_only_versioned_configuration(tmp_path, configuration):
    path = tmp_path / "config.json"
    path.write_text(configuration.model_dump_json())
    assert load_configuration(str(path)) == configuration
    changed = json.loads(path.read_text())
    changed["mode"] = "active"
    path.write_text(json.dumps(changed))
    with pytest.raises(ConfigurationError):
        load_configuration(str(path))


def test_pinned_schema_config_accepts_64k_plus_but_rejects_oversized_snapshot(release, tmp_path):
    import hashlib

    from tool_adapters.atlassian import READ_TOOLS
    from tool_adapters.atlassian_observe import canonical
    from tool_adapters.config import MAX_CONFIGURATION_BYTES, AdapterConfiguration

    schema = {"type": "object", "properties": {"cloudId": {"type": "string"}}, "description": "x" * 70_000}
    digest = hashlib.sha256(canonical(schema)).hexdigest()
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/core",
                "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                "edge_provenance_sha256": "a" * 64,
                "identity_workload": "adp-tools-fixture-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-fixture-atlassian-mcp-user",
                "config_revision": "b" * 64,
                "subject_schema_sha256": "c" * 64,
                "sites_schema_sha256": "d" * 64,
                "gateway_role_arn": "arn:aws:iam::123456789012:role/gateway",
                "read_schemas": {
                    name: schema if name == "search_jira" else {"type": "object", "properties": {}}
                    for name in READ_TOOLS
                },
                "read_schemas_sha256": {
                    name: digest
                    if name == "search_jira"
                    else hashlib.sha256(canonical({"type": "object", "properties": {}})).hexdigest()
                    for name in READ_TOOLS
                },
            },
        }
    )
    path = tmp_path / "pinned.json"
    path.write_text(config.model_dump_json())
    assert 65536 < path.stat().st_size < MAX_CONFIGURATION_BYTES
    assert load_configuration(str(path)) == config
    large_schema = {"type": "object", "properties": {"cloudId": {"type": "string", "description": "s" * 260_000}}}
    large_digest = hashlib.sha256(canonical(large_schema)).hexdigest()
    large = config.model_dump()
    large["observation"]["read_schemas"] = {name: large_schema for name in READ_TOOLS}
    large["observation"]["read_schemas_sha256"] = {name: large_digest for name in READ_TOOLS}
    large_config = AdapterConfiguration.model_validate(large)
    path.write_text(large_config.model_dump_json())
    assert 256 * 1024 < path.stat().st_size < MAX_CONFIGURATION_BYTES
    assert load_configuration(str(path)) == large_config
    path.write_text(large_config.model_dump_json() + " " * MAX_CONFIGURATION_BYTES)
    with pytest.raises(ConfigurationError, match="tool_adapter_configuration_invalid"):
        load_configuration(str(path))
