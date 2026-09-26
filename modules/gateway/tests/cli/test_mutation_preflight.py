"""Definitive capability failures stop shipped mutations before transport."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
import adp_common as common  # noqa: E402


class Transport:
    machine = False

    def __init__(self, operation):
        self.operation = operation
        self.calls = []

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        assert path == common.CAPABILITIES_PATH
        return {
            "schema_version": common.CAPABILITY_SCHEMA_VERSIONS[0],
            "gateway": {"state": "yes", "release": "test", "source": "test"},
            "tenant": {"org_id": "org-alpha"},
            "operations": [
                {
                    "id": self.operation,
                    "supported": "yes",
                    "enabled": "no",
                    "permitted": "yes",
                    "ready": "yes",
                    "mutates": True,
                }
            ],
        }


def load(name):
    path = CLI / f"adp-{name}.py"
    spec = importlib.util.spec_from_file_location(f"preflight_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_cache_identity(monkeypatch):
    monkeypatch.setattr(common, "capability_cache_context", lambda token=None: (None, None))


@pytest.mark.parametrize(
    ("helper", "argv", "operation"),
    [
        ("aws", ["connect", "--account", "123456789012", "--yes"], "connections.aws.write"),
        ("aws", ["verify", "connection-1", "--yes"], "connections.aws.verify.write"),
        ("bedrock", ["connect", "--account", "123456789012", "--org", "org-alpha", "--yes"], "routing.bedrock.write"),
        ("bedrock", ["verify", "destination-1", "--yes"], "routing.bedrock.verify.write"),
        ("github", ["connect", "--repo", "owner/repo", "--yes"], "github.connection.write"),
        ("github-admin", ["setup", "--new", "--github-org", "owner", "--yes"], "github.app.admin.setup.write"),
        ("github-admin", ["revalidate"], "github.app.admin.revalidate.write"),
        ("models", ["mappings", "set", "--persona", "developer", "--model", "model", "--yes"], "models.mapping.self.write"),
        ("models", ["mappings", "reset", "--persona", "developer", "--service-principal", "worker", "--yes"], "models.mapping.managed.write"),
        ("flow", ["gate", "approve", "gate-1", "--yes"], "flows.approve.write"),
    ],
)
def test_definitive_disabled_mutation_sends_only_discovery(helper, argv, operation):
    module = load(helper)
    transport = Transport(operation)

    with pytest.raises(common.CliError) as caught:
        module.run(module.parser().parse_args(argv), transport)

    assert caught.value.code == "feature_disabled"
    assert transport.calls == [("GET", common.CAPABILITIES_PATH)]


def test_superplane_session_refuses_disabled_mutation_with_pinned_token(monkeypatch):
    module = load("superplane")
    monkeypatch.setattr(common, "access_token", lambda: "test-session")
    transport = Transport("superplane.workspace.write")
    with pytest.raises(common.CliError) as caught:
        module.SessionApi(transport).request("POST", module.API_BASE + "/workspaces", {"name": "workspace"})
    assert caught.value.code == "feature_disabled"
    assert transport.calls == [("GET", common.CAPABILITIES_PATH)]


def test_interactive_refine_only_checks_capabilities_after_local_prompt(monkeypatch):
    module = load("flow")
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "Improve checkout")
    transport = Transport("flows.draft.write")
    with pytest.raises(common.CliError) as caught:
        module.run(module.parser().parse_args(["start", "--refine-only"]), transport)
    assert caught.value.code == "feature_disabled"
    assert transport.calls == [("GET", common.CAPABILITIES_PATH)]
