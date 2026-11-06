"""Offline release-manifest checks; synthetic receipts never prove deployment."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e.product_onboarding import product_completion_release as release


def publish_attempt(root, receipt_path, environment):
    return release.publish(
        receipt_path,
        root / "manifest.json",
        desired_path=root / "desired.json",
        current_path=root / "current.json",
        plan_path=root / "plan.json",
        environment=environment,
    )


@pytest.fixture
def attempt(tmp_path):
    tmp_path.chmod(0o700)
    environment = {
        "PRODUCT_ENVIRONMENT": "dev",
        "PRODUCT_SUITE": "product-profile",
        "PRODUCT_MAX_PARALLEL": "8",
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40,
        "ADP_CONTRACT_DIGEST": "c" * 64,
        "ADP_CORRELATION": "d" * 64,
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    receipts = {
        name: {
            "desired_sha256": "e" * 64,
            "observed_sha256": "e" * 64,
            "source_fingerprint": "f" * 64,
            "observed_fingerprint": "f" * 64,
            "observed_source_revision": "a" * 40,
            "evidence_ref": f"evidence/{name}.json",
            "verified": True,
            "action": "deployed",
        }
        for name in release.COMPONENTS
    }
    receipts["gateway-frontend"]["action"] = "reused"
    receipts["gateway-frontend"]["observed_source_revision"] = "0" * 40
    desired = {
        name: {
            "sha256": receipt["desired_sha256"],
            "fingerprint": receipt["source_fingerprint"],
            "source_revision": environment["ADP_SOURCE_REVISION"],
        }
        for name, receipt in receipts.items()
    }
    current = {
        "gateway-frontend": {
            "sha256": "e" * 64,
            "fingerprint": "f" * 64,
            "source_revision": "0" * 40,
            "verified": True,
            "evidence_ref": "evidence/gateway-frontend.json",
        }
    }
    for filename, content in (("desired.json", desired), ("current.json", current)):
        item = tmp_path / filename
        item.write_text(json.dumps(content))
        item.chmod(0o600)
    release.write_plan(
        tmp_path / "desired.json",
        tmp_path / "current.json",
        tmp_path / "plan.json",
        environment=environment,
    )
    path = tmp_path / "component-receipts.json"

    def write():
        path.write_text(json.dumps(receipts))
        path.chmod(0o600)

    write()
    return tmp_path, path, receipts, environment, write


def test_publishes_all_observed_components_and_reuses_only_matching_artifacts(attempt):
    root, receipt_path, _, environment, _ = attempt
    destination = root / "manifest.json"
    digest = publish_attempt(root, receipt_path, environment)
    manifest = json.loads(destination.read_text())
    assert manifest["source_revision"] == environment["GITHUB_SHA"]
    assert manifest["run_attempt"] == 2
    assert manifest["release_id"] == "product-42-2"
    assert set(manifest["components"]) == set(release.COMPONENTS)
    assert manifest["components"]["gateway-frontend"]["action"] == "reused"
    assert manifest["components"]["gateway-backend"]["action"] == "deployed"
    assert digest == release.hashlib.sha256(destination.read_bytes()).hexdigest()
    assert (destination.stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        publish_attempt(root, receipt_path, environment)


def test_reuse_plan_deploys_changed_components_and_refuses_unverified_prior(attempt):
    root, _, _, environment, _ = attempt
    desired = json.loads((root / "desired.json").read_text())
    current = json.loads((root / "current.json").read_text())
    assert release.plan_actions(desired, current, environment) == {
        name: "reused" if name == "gateway-frontend" else "deployed"
        for name in release.COMPONENTS
    }
    current["gateway-frontend"]["sha256"] = "0" * 64
    assert (
        release.plan_actions(desired, current, environment)["gateway-frontend"]
        == "deployed"
    )
    current["gateway-frontend"]["verified"] = False
    with pytest.raises(ValueError, match="unverified"):
        release.plan_actions(desired, current, environment)


def test_workflow_commands_plan_then_publish_one_named_release(attempt):
    root, receipts, _, environment, _ = attempt
    (root / "plan.json").unlink()
    output = root / "outputs"
    command = [
        sys.executable,
        "-m",
        "tests.e2e.product_onboarding.product_completion_release",
        "--desired",
        str(root / "desired.json"),
        "--current",
        str(root / "current.json"),
        "--plan",
        str(root / "plan.json"),
    ]
    variables = {**os.environ, **environment, "GITHUB_OUTPUT": str(output)}
    planned = subprocess.run(
        command, env=variables, capture_output=True, text=True, check=False
    )
    assert planned.returncode == 0, planned.stderr
    published = subprocess.run(
        [
            *command,
            "--receipts",
            str(receipts),
            "--output",
            str(root / "manifest.json"),
        ],
        env=variables,
        capture_output=True,
        text=True,
        check=False,
    )
    assert published.returncode == 0, published.stderr
    assert "release_id=product-42-2" in output.read_text()
    assert "manifest_sha256=" in output.read_text()


def test_workflow_refuses_publication_without_source_owned_observations(attempt):
    root, receipts, _, environment, _ = attempt
    (root / "current.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        release.write_plan(
            root / "desired.json",
            root / "current.json",
            root / "new-plan.json",
            environment=environment,
        )
    with pytest.raises(ValueError, match="missing"):
        publish_attempt(root, receipts, environment)
    assert not (root / "manifest.json").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_component",
        "missing_observation",
        "mismatched_digest",
        "mismatched_fingerprint",
        "unverified",
        "rollback",
        "bad_action",
        "stale_source",
        "stale_workflow",
        "wrong_contract",
        "wrong_suite",
        "wrong_parallel",
        "wrong_target",
        "symlinked_receipts",
        "unsafe_reference",
        "stale_plan",
        "mismatched_candidate",
        "incorrect_reuse",
    ],
)
def test_refuses_incomplete_or_mismatched_release_without_manifest(attempt, failure):
    root, receipt_path, receipts, environment, write = attempt
    selected = receipts["gateway-backend"]
    if failure == "missing_component":
        del receipts["gateway-frontend"]
    elif failure == "missing_observation":
        del selected["evidence_ref"]
    elif failure == "mismatched_digest":
        selected["observed_sha256"] = "0" * 64
    elif failure == "mismatched_fingerprint":
        receipts["gateway-frontend"]["observed_fingerprint"] = "0" * 64
    elif failure == "unverified":
        selected["verified"] = False
    elif failure == "rollback":
        selected["observed_source_revision"] = "0" * 40
    elif failure == "bad_action":
        selected["action"] = "skipped"
    elif failure == "stale_source":
        environment["GITHUB_SHA"] = "0" * 40
    elif failure == "stale_workflow":
        environment["GITHUB_WORKFLOW_SHA"] = "0" * 40
    elif failure == "wrong_contract":
        environment["ADP_CONTRACT_DIGEST"] = ""
    elif failure == "wrong_suite":
        environment["PRODUCT_SUITE"] = "other"
    elif failure == "wrong_parallel":
        environment["PRODUCT_MAX_PARALLEL"] = "16"
    elif failure == "wrong_target":
        environment["GITHUB_REF"] = "refs/heads/feature"
    elif failure == "unsafe_reference":
        selected["evidence_ref"] = "evidence/../../foreign.json"
    elif failure == "stale_plan":
        (root / "plan.json").write_text("{}")
    elif failure == "mismatched_candidate":
        selected["desired_sha256"] = "0" * 64
        selected["observed_sha256"] = "0" * 64
    elif failure == "incorrect_reuse":
        selected["action"] = "reused"
    else:
        receipt_path.rename(root / "real.json")
        receipt_path.symlink_to(root / "real.json")
    if failure != "symlinked_receipts":
        write()
    with pytest.raises((ValueError, TypeError)):
        publish_attempt(root, receipt_path, environment)
    assert not (root / "manifest.json").exists()


def test_workflow_exposes_fixed_inputs_and_refuses_placeholder_manifest():
    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / ".github/workflows/product-profile-release.yml"
        ).read_text()
    )
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert {
        "adp_correlation",
        "adp_source_revision",
        "adp_definition_revision",
        "adp_contract_digest",
    } <= set(inputs)
    for name, value in (
        ("environment", "dev"),
        ("suite", "product-profile"),
        ("max_parallel", "8"),
    ):
        assert inputs[name]["options"] == [value]
        assert inputs[name]["default"] == value
    assert (
        workflow["jobs"]["release-manifest"]["if"] == "github.ref == 'refs/heads/main'"
    )
    steps = workflow["jobs"]["release-manifest"]["steps"]
    assert any("--receipts" in step.get("run", "") for step in steps)
    assert any(
        "--plan" in step.get("run", "") and "--receipts" not in step.get("run", "")
        for step in steps
    )
    assert workflow["env"]["ADP_DELIVERY_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert any("publish-deployment-evidence.py workflow-artifact product-profile-manifest" in step.get("run", "") for step in steps)
    assert not any(step.get("continue-on-error") for step in steps)
