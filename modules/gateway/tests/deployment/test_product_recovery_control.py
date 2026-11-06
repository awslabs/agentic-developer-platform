"""The deploy-role entrypoint cannot switch an unbound Gateway."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
fixture_spec = importlib.util.spec_from_file_location(
    "test_product_recovery_fixture", ROOT / "modules/gateway/tests/deployment/test_product_recovery.py"
)
fixture_module = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixture_module)
fixture_gateway = fixture_module.gateway
IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@" + fixture_module.IMAGE.rsplit("@", 1)[-1]
CANDIDATE_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@" + fixture_module.CANDIDATE_IMAGE.rsplit("@", 1)[-1]
spec = importlib.util.spec_from_file_location("product_recovery_control", ROOT / "modules/gateway/scripts/product-recovery-control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


@pytest.fixture
def gateway(fixture_gateway):
    fixture_gateway["current"]["metadata"]["namespace"] = "adp-gateway"
    fixture_gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] = IMAGE
    return fixture_gateway


@pytest.fixture
def binding(monkeypatch):
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "b" * 40)
    monkeypatch.setenv("ADP_DEPLOY_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment")
    return {
        "run_id": "product-42-1",
        "source_revision": "a" * 40,
        "workflow_revision": "b" * 40,
        "baseline_source_revision": "c" * 40,
        "candidate_image_digest": IMAGE.rsplit("@", 1)[-1],
        "baseline_image_digest": CANDIDATE_IMAGE.rsplit("@", 1)[-1],
        "resource_id": "adp-dev-eks-cluster/adp-gateway",
        "account_id": "123456789012",
    }


def identity():
    return {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/adp-dev-trusted-deployment/fixture"}


def test_held_switch_requires_quiescence_and_restores_candidate(gateway, binding):
    quiesced = control.execute(binding, "quiesce", identity=identity, run=gateway["run"])
    assert quiesced["admissions_disabled"] is True
    assert quiesced["run_id"] == binding["run_id"]
    baseline = control.execute(binding, "baseline", identity=identity, run=gateway["run"])
    assert baseline["admissions_enabled"] is False
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] == CANDIDATE_IMAGE
    restored = control.execute(binding, "candidate", identity=identity, run=gateway["run"])
    assert restored["admissions_enabled"] is True
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE


def test_protected_control_restores_candidate_after_interrupted_baseline(gateway, binding):
    control.execute(binding, "quiesce", identity=identity, run=gateway["run"])
    original = gateway["run"]

    def interrupted(args, payload=None):
        if "rollout" in args and gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] == CANDIDATE_IMAGE:
            raise TimeoutError("baseline readiness interrupted")
        return original(args, payload)

    with pytest.raises(TimeoutError):
        control.execute(binding, "baseline", identity=identity, run=interrupted)
    receipt = control.execute(binding, "candidate", identity=identity, run=gateway["run"])
    assert receipt["admissions_enabled"] is True


def test_protected_control_restores_candidate_after_interrupted_quiescence(gateway, binding):
    control.execute(binding, "quiesce", identity=identity, run=gateway["run"])
    restored = control.execute(binding, "candidate", identity=identity, run=gateway["run"])
    assert restored["admissions_enabled"] is True
    assert gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == "true"


def test_candidate_restoration_failure_remains_unverified_until_retry(gateway, binding):
    control.execute(binding, "quiesce", identity=identity, run=gateway["run"])
    control.execute(binding, "baseline", identity=identity, run=gateway["run"])
    original = gateway["run"]

    def interrupted(args, payload=None):
        if "rollout" in args and gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE:
            raise TimeoutError("candidate restoration readiness interrupted")
        return original(args, payload)

    with pytest.raises(TimeoutError):
        control.execute(binding, "candidate", identity=identity, run=interrupted)
    assert control.execute(binding, "candidate", identity=identity, run=original)["admissions_enabled"] is True


@pytest.mark.parametrize("change", ["run", "attempt", "workflow", "role", "account", "target", "image", "repository", "source", "flag"])
def test_protected_control_rejects_foreign_or_changed_run(gateway, binding, monkeypatch, change):
    if change == "run":
        monkeypatch.setenv("GITHUB_RUN_ID", "43")
    elif change == "attempt":
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    elif change == "workflow":
        monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "c" * 40)
    elif change == "role":
        monkeypatch.setenv("ADP_DEPLOY_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-other-trusted-deployment")
    elif change == "account":
        binding["account_id"] = "234567890123"
    elif change == "target":
        binding["resource_id"] = "adp-prod-eks-cluster/adp-gateway"
    elif change == "image":
        gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] = CANDIDATE_IMAGE
    elif change == "repository":
        gateway["current"]["spec"]["template"]["spec"]["containers"][0]["image"] = fixture_module.IMAGE
    elif change == "source":
        binding["source_revision"] = "bad"
    else:
        gateway["current"]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "true"
    with pytest.raises(ValueError):
        control.execute(binding, "quiesce", identity=identity, run=gateway["run"])
    assert not any("replace" in args for args in gateway["calls"])


def test_cli_writes_private_receipt_after_verified_switch(gateway, binding, tmp_path, monkeypatch):
    monkeypatch.setattr(control, "caller_identity", identity)
    monkeypatch.setattr(control.recovery, "command", gateway["run"])
    binding_path = tmp_path / "binding.json"
    receipt_path = tmp_path / "receipt.json"
    binding_path.write_text(json.dumps(binding))
    result = control.main(["--binding", str(binding_path), "--phase", "quiesce", "--receipt", str(receipt_path)])
    assert json.loads(receipt_path.read_text()) == result
