"""Protected manifest entrypoint retains separately enabled cleanup capability."""

import json
from pathlib import Path

import pytest

from tests.e2e.tools import atlassian_client_binding as binding
from tests.e2e.tools import atlassian_fixture_provider as cleanup
from tests.e2e.tools import atlassian_readback_grant as grant
from tests.e2e.tools.atlassian_client_matrix import CLIENTS, load_isolated_fixtures
from tests.e2e.tools.test_atlassian_readback_grant import manifest


def cleanup_manifest():
    source = manifest()
    source["cleanup"] = {
        client: {
            "enabled": True,
            "tenant_id": "tenant",
            "site_id": "fixture-site",
            "project_key": "OWNED",
            "delete_call": {
                "schema_sha256": "a" * 64,
                "arguments": {
                    "cloudId": {"fixture": "site_id"},
                    "issueIdOrKey": {"fixture": "provider_id"},
                },
            },
            "projects_call": {
                "schema_sha256": "b" * 64,
                "arguments": {"cloudId": {"fixture": "site_id"}},
            },
        }
        for client in CLIENTS
    }
    return source


def bind(source, directory):
    return binding.bind_clients(
        json.dumps(source),
        directory=directory,
        account="123456789012",
        region="us-east-1",
        reader_role=source["readback"]["codex"]["adapter_role_arn"],
    )


@pytest.fixture
def no_provider_calls(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Manifest binding must not request consent or call the provider")

    monkeypatch.setattr(cleanup, "_aws", unexpected)
    monkeypatch.setattr(grant, "_aws", unexpected)
    monkeypatch.setattr(
        grant.IdentityReadbackGrant,
        "reader",
        lambda self: grant.BoundProviderReader(self, self.owner_id),
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_binding_preserves_optional_cleanup_through_real_loader(
    tmp_path, no_provider_calls, enabled
):
    source = cleanup_manifest() if enabled else manifest()
    path, config = bind(source, tmp_path)
    assert json.loads(path.read_text()) == source
    assert path.stat().st_mode & 0o077 == 0
    assert config.stat().st_mode & 0o077 == 0
    fixtures = load_isolated_fixtures(path)
    for client in CLIENTS:
        assert fixtures[client]["owner_id"] == source["owners"][client]
        assert ("cleanup_provider" in fixtures[client]) is enabled
        if enabled:
            provider = fixtures[client]["cleanup_provider"]
            assert type(provider) is cleanup.JiraFixtureProvider
            assert provider.identity == {
                "run_id": source["run_id"],
                "owner_id": source["owners"][client],
                "tenant_id": "tenant",
                "site_id": "fixture-site",
                "project_key": "OWNED",
            }
            assert provider.grant.config == source["readback"][client]
            assert provider.marker is None


@pytest.mark.parametrize(
    "damage",
    [
        "missing_client",
        "extra_client",
        "not_object",
        "disabled",
        "foreign_site",
        "secret",
        "schema",
    ],
)
def test_invalid_cleanup_is_rejected_before_materializing_files(
    tmp_path, no_provider_calls, damage
):
    source = cleanup_manifest()
    capability = source["cleanup"]["codex"]
    if damage == "missing_client":
        del source["cleanup"]["claude-code"]
    elif damage == "extra_client":
        source["cleanup"]["other"] = capability
    elif damage == "not_object":
        source["cleanup"] = []
    elif damage == "disabled":
        capability["enabled"] = False
    elif damage == "foreign_site":
        capability["site_id"] = "another-site"
    elif damage == "secret":
        capability["access_token"] = "must-not-be-persisted"
    else:
        capability["delete_call"]["schema_sha256"] = "unobserved"
    with pytest.raises(ValueError):
        bind(source, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_protected_main_exports_manifest_with_cleanup(
    tmp_path, no_provider_calls, monkeypatch
):
    source = cleanup_manifest()
    exported = tmp_path / "github-env"
    for key, value in {
        "ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_JSON": json.dumps(source),
        "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN": source["readback"]["codex"][
            "adapter_role_arn"
        ],
        "EXPECTED_ACCOUNT": "123456789012",
        "AWS_REGION": "us-east-1",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(exported),
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE",
        "ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_JSON",
        "ADP_TOOLS_ATLASSIAN_NATIVE_SESSIONS_JSON",
    ):
        monkeypatch.delenv(key, raising=False)
    binding.main()
    values = dict(line.split("=", 1) for line in exported.read_text().splitlines())
    assert set(values) == {
        "ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE",
        "ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE",
    }
    path = Path(values["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"])
    fixtures = load_isolated_fixtures(path)
    assert all(
        type(row["cleanup_provider"]) is cleanup.JiraFixtureProvider
        for row in fixtures.values()
    )
    assert json.loads(path.read_text()) == source
