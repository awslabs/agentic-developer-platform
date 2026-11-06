"""Workflow wiring must not dispatch cloud actions before revision/connection checks."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"


def test_module_ci_paths_and_offline_verification():
    workflow = yaml.safe_load((WORKFLOWS / "dev-box-ci.yml").read_text())
    paths = workflow.get("on", workflow.get(True))["pull_request"]["paths"]
    assert "modules/user-services/dev-box/**" in paths
    steps = workflow["jobs"]["module"]["steps"]
    assert any("test_release.py" in step.get("run", "") for step in steps)
    assert any("terraform -chdir=modules/user-services/dev-box/infra test" in step.get("run", "") for step in steps)


def test_module_deploy_guards_before_checkout_and_provider_mutation():
    workflow = yaml.safe_load((WORKFLOWS / "dev-box-deploy.yml").read_text())
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert {"adp_correlation", "adp_source_revision", "adp_definition_revision",
            "customer_account_id", "customer_aws_label", "customer_user_id", "environment"} <= inputs.keys()
    job = workflow["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    steps = job["steps"]
    assert steps[0]["name"].startswith("Guard engine")
    assert steps[1]["with"]["ref"] == "${{ inputs.adp_source_revision }}"
    assert "release.py --from-env" in steps[2]["run"]
    assert "deploy.sh" in steps[3]["run"]
    assert "runtime.py" in steps[4]["run"]
    assert "output -json" in steps[4]["run"]
    assert "dev-box-runtime-receipt.json" in steps[5]["run"]
    assert "put-object" in steps[5]["run"]


@pytest.mark.parametrize("overrides,allowed", [
    ({}, True),
    ({"ADP_SOURCE": "main"}, False),
    ({"ADP_DEFINITION": "d" * 40}, False),
    ({"SELECTED_LABEL": "other"}, False),
    ({"APPROVED_LABEL": ""}, False),
    ({"TARGET_ACCOUNT": "999999999999"}, False),
])
def test_module_guard_rejects_unapproved_dispatch_before_checkout(overrides, allowed):
    steps = yaml.safe_load((WORKFLOWS / "dev-box-deploy.yml").read_text())["jobs"]["deploy"]["steps"]
    environment = {"PATH": os.environ["PATH"], "ADP_SOURCE": "a" * 40, "ADP_DEFINITION": "b" * 40,
                   "ACTUAL_DEFINITION": "b" * 40, "ADP_CORRELATION": "c" * 64,
                   "SELECTED_LABEL": "approved", "APPROVED_LABEL": "approved",
                   "TARGET_ACCOUNT": "000000000000", "APPROVED_ACCOUNT": "000000000000"}
    environment.update(overrides)
    result = subprocess.run(["bash", "-c", steps[0]["run"]], check=False, env=environment, capture_output=True)
    assert (result.returncode == 0) is allowed


@pytest.mark.parametrize("revision", ["source", "definition"])
def test_revision_mismatch_blocks_manifest_before_any_cloud_operation(tmp_path, revision):
    import json
    import sys

    dispatch = {
        "adp_correlation": "c" * 64,
        "adp_source_revision": "a" * 40,
        "adp_definition_revision": "b" * 40,
        "customer_aws_label": "selected-example",
        "customer_account_id": "000000000000",
        "environment": "dev",
        "region": "us-east-1",
        "image_id": "ami-0123456789abcdef0",
        "image_revision": "base-v1",
        "template_revision": "example-r1",
        "instance_size": "t3.small",
        "workspace_disk_gb": 40,
        "allowed_sizes": ["t3.small"],
        "max_disk_gb": 64,
        "infra_sha256": "d" * 64,
        "module_sha256": "e" * 64,
        "private_subnet_id": "subnet-0123456789abcdef0",
        "gateway_security_group_id": "sg-11111111111111111",
        "ssm_endpoint_security_group_id": "sg-22222222222222222",
        "controller_principal_arn": "arn:aws:iam::000000000000:role/example-controller",
        "deployment_id": "example-deployment",
        "kms_key_arn": "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000",
        "dashboard_enabled": False,
    }
    context = tmp_path / "dispatch.json"
    context.write_text(json.dumps(dispatch))
    output = tmp_path / "dev-box-release.json"
    actual_source = "f" * 40 if revision == "source" else "a" * 40
    actual_definition = "f" * 40 if revision == "definition" else "b" * 40
    result = subprocess.run(
        [sys.executable, str(ROOT / "modules/user-services/dev-box/release.py"),
         "--dispatch", str(context), "--actual-source", actual_source,
         "--actual-definition", actual_definition, "--output", str(output)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "source or workflow definition revision mismatch" in result.stderr
    assert not output.exists()


def test_actual_checkout_mismatch_fails_before_packaging_or_deploy():
    steps = yaml.safe_load((WORKFLOWS / "dev-box-deploy.yml").read_text())["jobs"]["deploy"]["steps"]
    build_script = steps[2]["run"]
    assert build_script.index('test "$(git rev-parse HEAD)"') < build_script.index("tar -czf")
    assert build_script.index("tar -czf") < build_script.index("release.py --from-env")
    checkout_guard = build_script.split("tar -czf", 1)[0]
    environment = {"PATH": os.environ["PATH"], "DEV_BOX_ADP_SOURCE_REVISION": "f" * 40}
    result = subprocess.run(["bash", "-c", checkout_guard], check=False, cwd=ROOT,
                            env=environment, capture_output=True)
    assert result.returncode != 0
    assert steps[3]["name"].startswith("Deploy approved foundation")


def test_missing_selected_connection_stops_workflow_before_checkout():
    steps = yaml.safe_load((WORKFLOWS / "dev-box-deploy.yml").read_text())["jobs"]["deploy"]["steps"]
    environment = {"PATH": os.environ["PATH"], "ADP_SOURCE": "a" * 40,
                   "ADP_DEFINITION": "b" * 40, "ACTUAL_DEFINITION": "b" * 40,
                   "ADP_CORRELATION": "c" * 64, "SELECTED_LABEL": "",
                   "APPROVED_LABEL": "", "TARGET_ACCOUNT": "000000000000",
                   "APPROVED_ACCOUNT": "000000000000"}
    result = subprocess.run(["bash", "-c", steps[0]["run"]], check=False,
                            env=environment, capture_output=True)
    assert result.returncode != 0
    assert steps[1]["uses"].startswith("actions/checkout@")


def test_missing_selected_connection_stops_deploy_before_broker(tmp_path):
    broker = tmp_path / "adp-cred"
    marker = tmp_path / "broker-was-called"
    broker.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 99\n")
    broker.chmod(0o755)
    environment = dict(os.environ)
    environment.update({"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                        "TARGET_ACCOUNT_ID": "000000000000",
                        "EXPECTED_ASSUMED_ROLE_PREFIX": "arn:aws:sts::000000000000:assumed-role/example-role",
                        "ADP_STATE_BUCKET": "example-state-bucket",
                        "ADP_STATE_KEY": "example/state.tfstate",
                        "TF_VAR_region": "us-east-1",
                        "TF_VAR_kms_key_arn": "arn:aws:kms:us-east-1:000000000000:key/example"})
    environment.pop("ADP_CONNECTION_LABEL", None)
    result = subprocess.run(["bash", str(ROOT / "modules/user-services/dev-box/deploy.sh")],
                            check=False, env=environment, cwd=tmp_path, capture_output=True)
    assert result.returncode != 0
    assert not marker.exists()
    assert not (tmp_path / "terraform.tfstate").exists()


def test_release_artifact_includes_runtime_adapter_and_is_verified_before_receipt():
    steps = yaml.safe_load((WORKFLOWS / "dev-box-deploy.yml").read_text())["jobs"]["deploy"]["steps"]
    assert "runtime.py" in steps[2]["run"]
    assert "DEV_BOX_MODULE_SHA256" in steps[2]["run"]
    assert steps[2]["run"].index("DEV_BOX_MODULE_SHA256") < steps[2]["run"].index("release.py --from-env")
    assert "adp-cred assume" in steps[4]["run"] and "runtime.py" in steps[4]["run"]
    assert "dev-box-runtime-receipt.json" in steps[5]["run"]


def test_dashboard_profile_revision_is_release_input_before_image_verification():
    workflow = yaml.safe_load((WORKFLOWS / "dev-box-deploy.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    assert steps[2]["env"]["DEV_BOX_CREW_PROFILE_REVISION"] == "${{ vars.ADP_DEV_BOX_CREW_PROFILE_REVISION || '' }}"
    assert steps[2]["env"]["DEV_BOX_CREW_MODEL_ID"] == "${{ vars.ADP_DEV_BOX_CREW_MODEL_ID || '' }}"
    assert "release.py --from-env" in steps[2]["run"]
    assert "runtime.py" in steps[4]["run"]


def test_crew_bootstrap_pins_reach_both_the_release_context_and_terraform():
    """#8055: the workflow passes the same Crew bootstrap inputs to release.py and to the foundation."""
    workflow = yaml.safe_load((WORKFLOWS / "dev-box-deploy.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    context, apply = steps[2]["env"], steps[3]["env"]
    assert context["DEV_BOX_CREW_BOOTSTRAP"] == apply["TF_VAR_crew_bootstrap"] == "${{ vars.ADP_DEV_BOX_CREW_BOOTSTRAP || 'image' }}"
    for name in ("CREW_BUNDLE_S3_URI", "CREW_BUNDLE_SHA256", "CREW_SOURCE_REVISION", "CREW_PROFILE_REVISION", "CREW_MODEL_ID"):
        assert context[f"DEV_BOX_{name}"] == apply[f"TF_VAR_{name.lower()}"] == f"${{{{ vars.ADP_DEV_BOX_{name} || '' }}}}", name
    stage = (ROOT / "modules/user-services/dev-box/scripts/deploy-dev-box.sh").read_text()
    for flag in ("--crew-bootstrap", "--crew-bundle-sha", "--profile-revision", "--crew-model-id"):
        assert f"    {flag})" in stage, flag
    for variable in ("crew_bootstrap", "crew_bundle_s3_uri", "crew_manifest_s3_uri", "crew_bundle_sha256", "crew_source_revision"):
        assert f'-var "{variable}=' in stage, variable
    assert '"BG_DEV_BOX_CREW_BOOTSTRAP": ("crew-bootstrap", "image")' in stage
    assert "dev-box/image-inputs/${CREW_BUNDLE_SHA}" in stage
    # The bundle build (#8054) owns these SSM keys; the stage reads them, the foundation never declares them.
    assert 'ssm_or_empty "$SSM_PREFIX/crew-bundle-sha256"' in stage and 'ssm_or_empty "$SSM_PREFIX/crew-bundle-revision"' in stage
    infra = (ROOT / "modules/user-services/dev-box/infra/self_managed.tf").read_text()
    assert '"crew-bootstrap"' in infra and '"crew-bundle-sha256"' not in infra.split("resource \"aws_ssm_parameter\" \"stage\"", 1)[1]
