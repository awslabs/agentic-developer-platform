"""Protected entrypoint wiring with controlled provider boundaries."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools import atlassian_regression_binding as binding
from tests.e2e.tools import atlassian_regression_scenario as scenario
from tests.e2e.tools import legacy_live_bindings as legacy
from tests.e2e.tools import runner
from tests.e2e.tools import test_legacy_live_bindings as fixtures


def environment(tmp_path, cell="github:cli"):
    release = tmp_path / "release.json"
    flow._write_private(release, {})
    doc = fixtures.manifest(cell)
    doc.pop("binding")
    doc.pop("target")
    return {
        "ADP_TOOLS_RELEASE_MANIFEST_FILE": str(release),
        "SOURCE_REVISION": "a" * 40, "GITHUB_SHA": "b" * 40,
        "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "RUNNER_TEMP": str(tmp_path), "EXPECTED_ACCOUNT": fixtures.ACCOUNT,
        "AWS_REGION": fixtures.REGION, "GATEWAY_URL": fixtures.GATEWAY,
        "GATEWAY_REVISION": fixtures.TARGET["gateway"],
        "GATEWAY_IMAGE": fixtures.TARGET["gateway_image"],
        "FACADE_IMAGE": fixtures.TARGET["facade_image"],
        "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN": fixtures.ROLE,
        "ADP_TOOLS_ATLASSIAN_REGRESSION_JSON": json.dumps(doc),
        "ADP_TOOLS_ATLASSIAN_REGRESSION_SESSIONS_JSON": json.dumps({cell: "private-session"} if cell else {}),
    }


@pytest.mark.parametrize("configured", [True, False])
def test_entrypoint_supplies_protected_clients_to_repository_collector(tmp_path, monkeypatch, configured):
    env = environment(tmp_path)
    if configured:
        path = binding.materialize(env)
        env["ADP_TOOLS_ATLASSIAN_REGRESSION_FILE"] = str(path)
        assert (path.parent / "aws-config").stat().st_mode & 0o777 == 0o600
        assert fixtures.ROLE in (path.parent / "aws-config").read_text()
    else:
        monkeypatch.delenv("ADP_TOOLS_ATLASSIAN_REGRESSION_FILE", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runner, "private_access_bindings", lambda: (None, None))
    monkeypatch.setattr(runner, "private_release_bindings", lambda: ({}, None))
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    calls = []

    def run(driver):
        calls.append(driver)
        return {"cell": "github:cli", "status": "pending", "reason": "controlled-provider-unavailable"}

    monkeypatch.setattr(legacy.LegacyCell, "run", run)
    monkeypatch.setattr(legacy.LegacyCell, "close", lambda *_: None)
    output = tmp_path / "evidence"
    assert runner.main(["--scenario-filter", "SCN-8454-05", "--output", str(output),
                        "--gateway-url", env["GATEWAY_URL"], "--gateway-revision", env["GATEWAY_REVISION"],
                        "--gateway-image", env["GATEWAY_IMAGE"], "--facade-image", env["FACADE_IMAGE"]]) == 1
    report = flow._decode((output / "evidence.json").read_bytes())
    assert report["unregistered_scenarios"] == []
    row = report["scenarios"][0]
    assert row["status"] == "blocked" and len(row["observations"]["cells"]) == 9
    assert len(calls) == int(configured)
    if configured:
        verified = scenario.inspect_native_regression(row["observations"], manifest=binding.load_manifest(env))
        assert verified["full_ac04"] == "pending"
        altered = deepcopy(row["observations"])
        altered["native_parity"]["cases"][0]["observable_parity"] = "pass"
        with pytest.raises(ValueError):
            scenario.inspect_native_regression(altered, manifest=binding.load_manifest(env))
    else:
        assert all(cell["reason"] == "missing-provider-access" for cell in row["observations"]["cells"])


@pytest.mark.parametrize("field", ["binding", "role", "target"])
def test_foreign_protected_binding_rejected_before_materializing_sessions(tmp_path, field):
    env = environment(tmp_path)
    doc = json.loads(env["ADP_TOOLS_ATLASSIAN_REGRESSION_JSON"])
    if field == "role":
        doc["cells"]["github:cli"]["grant"]["adapter_role_arn"] = "foreign"
    else:
        doc[field] = {"source_revision": "f" * 40}
    env["ADP_TOOLS_ATLASSIAN_REGRESSION_JSON"] = json.dumps(doc)
    with pytest.raises(ValueError):
        binding.materialize(env)
    assert not list(tmp_path.glob("legacy-live-*"))


def test_config_write_failure_removes_unstarted_sessions(tmp_path, monkeypatch):
    env = environment(tmp_path)
    monkeypatch.setattr(binding, "OIDC_SCRIPT", tmp_path / "missing.py")
    with pytest.raises(FileNotFoundError):
        binding.materialize(env)
    assert not list(tmp_path.glob("legacy-live-*"))


def test_workflow_materializes_before_execution_and_clears_only_credentials():
    steps = yaml.safe_load((Path(__file__).resolve().parents[3] / ".github/workflows/tools-live-evaluation.yml").read_text())["jobs"]["evaluate"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Bind protected legacy regression clients and provider readers") < names.index("Exercise deployed gateway through the MCP client")
    cleanup = steps[names.index("Remove owned evaluation configuration and test-user session")]
    assert cleanup["if"] == "always()"
    assert '"$regression_root" -type f -name session.token -delete' in cleanup["run"]
    assert 'rm -rf -- "$regression_root"' not in cleanup["run"]
