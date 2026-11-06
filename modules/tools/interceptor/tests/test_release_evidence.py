"""The Lambda workflow must upload a pinned, schema-compatible release."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "modules/tools/interceptor/release_evidence.py"
WORKFLOW = ROOT / ".github/workflows/tools-interceptor-deploy.yml"
HASH = "a" * 43 + "="


def release_module():
    spec = importlib.util.spec_from_file_location("interceptor_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context():
    return {
        "schema_version": 1,
        "repository_id": 1,
        "run_id": 42,
        "run_attempt": 3,
        "source_revision": "a" * 40,
        "workflow_revision": "b" * 40,
        "workflow_path": ".github/workflows/tools-interceptor-deploy.yml",
        "account_id": "123456789012",
        "region": "us-east-1",
        "resource_kind": "lambda-function",
        "resource_id": "arn:aws:lambda:us-east-1:123456789012:function:example-interceptor",
        "inputs": {"environment": "dev"},
        "correlation": "c" * 64,
    }


def test_alias_receipt_binds_to_release_artifact():
    release = release_module().build_release(context(), {"version": "42", "code_sha256": HASH}, 42, 3)
    assert release["component"] == "tools-interceptor"
    assert release["lambda_version"] == "42" and release["code_sha256"] == HASH
    assert release["resource_id"] == context()["resource_id"]
    assert "correlation" not in release and "inputs" not in release


@pytest.mark.parametrize("change", ["wrong_run", "wrong_attempt", "wrong_kind", "wrong_arn", "wrong_version", "wrong_hash"])
def test_alias_receipt_rejects_wrong_workflow_or_code(change):
    identity = context()
    alias = {"version": "42", "code_sha256": HASH}
    if change == "wrong_run":
        identity["run_id"] += 1
    elif change == "wrong_attempt":
        identity["run_attempt"] += 1
    elif change == "wrong_kind":
        identity["resource_kind"] = "eks-namespace"
    elif change == "wrong_arn":
        identity["resource_id"] = identity["resource_id"].replace("123456789012", "999999999999")
    elif change == "wrong_version":
        alias["version"] = "$LATEST"
    else:
        alias["code_sha256"] = "invalid"
    with pytest.raises(ValueError):
        release_module().build_release(identity, alias, 42, 3)


def test_workflow_binds_release_after_alias_readback_before_upload():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Publish verified Lambda version and promote live alias") < names.index("Bind Lambda release to reviewed workflow")
    assert names.index("Bind Lambda release to reviewed workflow") < names.index("Publish release receipt")
    assert steps[names.index("Bind Lambda release to reviewed workflow")]["run"] == "python3 modules/tools/interceptor/release_evidence.py"


def test_release_file_replaces_only_workflow_alias_receipt(tmp_path, monkeypatch):
    context_file = tmp_path / "adp-deployment-context" / "deployment-context.json"
    context_file.parent.mkdir()
    context_file.write_text(json.dumps(context()))
    release_file = tmp_path / "adp-release-tools-interceptor" / "release.json"
    release_file.parent.mkdir()
    release_file.write_text(json.dumps({"version": "42", "code_sha256": HASH}))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    release_module().main()
    assert json.loads(release_file.read_bytes())["lambda_version"] == "42"
