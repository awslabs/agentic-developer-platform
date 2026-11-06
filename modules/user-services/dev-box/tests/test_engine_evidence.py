"""The module publishes independently authenticated engine context and release artifacts."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

MODULE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dev_box_engine_evidence", MODULE / "engine_evidence.py")
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)


def fixture():
    manifest = {"source_revision": "a" * 40, "definition_revision": "b" * 40,
                "account_id": "000000000000", "region": "us-east-1", "module_sha256": "c" * 64,
                "manifest_sha256": "d" * 64, "environment": "dev"}
    receipt = {**{key: manifest[key] for key in ("source_revision", "definition_revision", "account_id", "region", "module_sha256")},
               "schema_version": 1, "component": "dev-box", "release_manifest_sha256": "d" * 64,
               "image_id": "ami-0123456789abcdef0", "launch_template_id": "lt-0123456789abcdef0",
               "launch_template_version": 4, "guest_security_group_id": "sg-33333333333333333",
               "gateway_security_group_id": "sg-11111111111111111", "private_subnet_id": "subnet-0123456789abcdef0",
               "guest_instance_profile_arn": "arn:aws:iam::000000000000:instance-profile/guest",
               "guest_role_arn": "arn:aws:iam::000000000000:role/guest",
               "controller_role_arn": "arn:aws:iam::000000000000:role/controller", "dashboard_enabled": False,
               "controller_status": "absent", "controller_digest": None, "controller_ready": False,
               "verified_at": datetime.now(UTC).isoformat()}
    environment = {"INPUT_CUSTOMER_ACCOUNT_ID": manifest["account_id"], "ADP_SOURCE_REVISION": "a" * 40,
                   "ADP_DEFINITION_REVISION": "b" * 40, "INPUT_ENVIRONMENT": "dev",
                   "INPUT_CUSTOMER_AWS_LABEL": "fixture", "INPUT_CUSTOMER_USER_ID": "fixture-owner",
                   "ADP_CORRELATION": "e" * 64, "GITHUB_REPOSITORY_ID": "17", "GITHUB_RUN_ID": "42",
                   "GITHUB_RUN_ATTEMPT": "1"}
    return manifest, receipt, environment


def test_engine_artifacts_preserve_run_and_unready_state():
    context, release = ENGINE.engine_evidence(*fixture())
    assert context["resource_kind"] == "ec2-launch-template"
    assert context["run_id"] == release["run_id"] == 42
    assert release["dev_box_receipt"]["controller_ready"] is False
    assert release["image_digest"] is None


@pytest.mark.parametrize("change", ["account_id", "source_revision", "definition_revision", "module_sha256", "release_manifest_sha256"])
def test_engine_artifacts_reject_identity_mismatch(change):
    manifest, receipt, environment = fixture()
    receipt[change] = "wrong"
    with pytest.raises(ValueError):
        ENGINE.engine_evidence(manifest, receipt, environment)


def test_deploy_workflow_uploads_both_exact_run_artifacts_after_readback():
    workflow = yaml.safe_load((MODULE.parents[2] / ".github/workflows/dev-box-deploy.yml").read_text())
    assert workflow["run-name"].startswith("${{ inputs.adp_correlation && format('ADP deployment")
    steps = workflow["jobs"]["deploy"]["steps"]
    assert "runtime.py" in steps[4]["run"]
    assert "engine_evidence.py" in steps[6]["run"]
    assert steps[7]["with"]["name"] == "adp-deployment-context-dev-box-deploy.yml-${{ github.run_attempt }}"
    assert steps[8]["with"]["name"] == "adp-release-dev-box-${{ github.run_attempt }}"
