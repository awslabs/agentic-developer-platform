"""Protected configuration preparation cannot reuse deployment authority or another owner."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.e2e.tools import atlassian_adapter_binding as binding
from tests.e2e.tools.test_atlassian_setup_live import configuration, private


def environment(tmp_path, monkeypatch):
    path, setup = configuration(tmp_path)
    setup["adapter_label"] = "oidc-adapter"
    private(path, json.dumps(setup))
    root = Path(path).parent
    private(root / "aws-config", "[profile oidc-adapter]\n")
    private(root / "aws-credentials", "")
    request = {
        "release": {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
                    "group": "atlassian-foundation", "group_generation": 1, "backend_generation": 1},
        "bindings": {"core_authorize_url": "https://gateway.example.test/internal/v1/tool-adapters/authorize",
                     "core_role_arn": "arn:aws:iam::123456789012:role/gateway-core",
                     "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
                     "edge_provenance_sha256": "c" * 64, "identity_workload": setup["workload_name"],
                     "identity_provider": setup["provider_name"], "config_revision": setup["metadata_sha256"]},
        "owner_id": setup["owner_id"], "expected_provider_user_id": setup["expected_subject"], "site_id": setup["site_id"],
        "observer_role_arn": setup["adapter_role_arn"],
    }
    for key, value in {
        "ADP_TOOLS_ATLASSIAN_SETUP_FILE": path, "GATEWAY_URL": setup["gateway_url"], "EXPECTED_ACCOUNT": setup["account"],
        "ADP_TOOLS_ATLASSIAN_ADAPTER_REQUEST_JSON": json.dumps(request), "RUNNER_TEMP": str(tmp_path),
        "ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE": str(root / "aws-config"), "GITHUB_ENV": str(tmp_path / "env"),
        "AWS_ACCESS_KEY_ID": "deployment-key", "AWS_SECRET_ACCESS_KEY": "deployment-secret", "AWS_ROLE_ARN": "deployment-role",
    }.items():
        monkeypatch.setenv(key, value)
    return root, request


@pytest.mark.parametrize("failed", [False, True])
def test_preparation_uses_only_isolated_observer_and_retains_no_request(tmp_path, monkeypatch, failed):
    root, request = environment(tmp_path, monkeypatch)

    def run(command, *, env, **kwargs):
        assert command[1:3] == ["-m", "modules.tools.adapters.observation_configuration"]
        assert env["AWS_PROFILE"] == "oidc-adapter"
        assert env["AWS_CONFIG_FILE"] == str(root / "aws-config")
        assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ROLE_ARN", "ADP_TOOLS_ATLASSIAN_ADAPTER_REQUEST_JSON"} & env.keys()
        prepared = json.loads((root / "adapter-request.json").read_text())
        assert prepared["owner_id"] == request["owner_id"]
        assert (root / "adapter-request.json").stat().st_mode & 0o077 == 0
        if not failed:
            private(command[-1], json.dumps({"mode": "observation", "release": request["release"]}))
        return subprocess.CompletedProcess(command, int(failed), "", "private-provider-error")

    monkeypatch.setattr(subprocess, "run", run)
    if failed:
        with pytest.raises(SystemExit, match="no deployment or acceptance"):
            binding.main()
        assert not (tmp_path / "env").exists()
    else:
        binding.main()
        assert "ADP_TOOLS_ADAPTER_OBSERVATION_FILE=" in (tmp_path / "env").read_text()
    assert not (root / "adapter-request.json").exists()


@pytest.mark.parametrize("field", ["owner_id", "site_id", "expected_provider_user_id", "observer_role_arn"])
def test_foreign_setup_is_rejected_before_any_sdk_process(tmp_path, monkeypatch, field):
    _, request = environment(tmp_path, monkeypatch)
    request[field] = "arn:aws:iam::123456789012:role/foreign" if field.endswith("arn") else "someone-else"
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_ADAPTER_REQUEST_JSON", json.dumps(request))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unauthorized process"))
    with pytest.raises(ValueError):
        binding.prepare_candidate()


def test_workflow_preparation_is_opt_in_and_not_a_deployment():
    workflow = yaml.safe_load(Path(".github/workflows/tools-live-evaluation.yml").read_text())
    step = next(item for item in workflow["jobs"]["evaluate"]["steps"] if item.get("name") == "Prepare protected adapter configuration from the existing owner grant")
    assert step["if"] == "always() && steps.scenario-selection.outputs.atlassian_setup == 'true' && inputs.prepare_atlassian_adapter"
    assert step["run"] == "python3 -m tests.e2e.tools.atlassian_adapter_binding"
    assert "secrets.ADP_TOOLS_ATLASSIAN_ADAPTER_REQUEST_JSON" in str(step["env"])
