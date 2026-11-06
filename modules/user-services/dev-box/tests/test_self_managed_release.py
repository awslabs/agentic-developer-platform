"""The self-managed stage produces the same fail-closed manifest as the workflow."""

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("dev_box_self_managed_release", SCRIPTS / "self_managed_release.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SOURCE = "a" * 40


def outputs(**context_overrides):
    context = {
        "account_id": "000000000000",
        "environment": "dev",
        "region": "us-east-1",
        "deployment_id": "example-deployment",
        "image_id": "ami-0123456789abcdef0",
        "image_revision": "base-20261017-aaaaaaaaaaaa",
        "template_revision": "r1",
        "instance_size": "m6i.xlarge",
        "workspace_disk_gb": 100,
        "allowed_sizes": ["m6i.xlarge"],
        "max_disk_gb": 100,
        "private_subnet_id": "subnet-0123456789abcdef0",
        "gateway_security_group_id": "sg-11111111111111111",
        "ssm_endpoint_security_group_id": "sg-22222222222222222",
        "controller_principal_arn": "arn:aws:iam::000000000000:role/adp-dev-dev-box-controller-runtime",
        "kms_key_arn": "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000",
        "dashboard_enabled": False,
        "run_table_arn": "arn:aws:dynamodb:us-east-1:000000000000:table/adp-dev-webhook-events",
        "run_table_kms_key_arn": "",
        "gateway_execute_arn": "",
        "crew_profile_revision": "",
        "crew_model_id": "",
    }
    context.update(context_overrides)
    return {"release_context": {"sensitive": False, "type": "object", "value": context}}


def test_manifest_matches_the_workflow_shape_and_is_deterministic():
    manifest = MODULE.build_manifest(outputs(), source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64)
    again = MODULE.build_manifest(outputs(), source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64)
    assert manifest == again
    assert manifest["component"] == "dev-box" and manifest["controller_status"] == "absent"
    assert manifest["source_revision"] == manifest["definition_revision"] == SOURCE
    assert manifest["run_table_arn"].endswith("adp-dev-webhook-events")
    assert "run_table_kms_key_arn" not in manifest, "an AWS-owned key leaves no field (and no digest change)"
    assert "gateway_execute_arn" not in manifest and "crew_model_id" not in manifest
    assert "self-managed" not in json.dumps(manifest), "the connection label enters only as a digest"
    assert len(manifest["manifest_sha256"]) == 64
    assert manifest["correlation"] == MODULE.correlation(outputs()["release_context"]["value"], SOURCE)


def test_pinned_crew_profile_carries_the_gateway_route_and_dashboard():
    manifest = MODULE.build_manifest(
        outputs(gateway_execute_arn="arn:aws:execute-api:us-east-1:000000000000:abc123/dev", crew_profile_revision="crew-r1",
                crew_model_id="example-model", dashboard_enabled=True),
        source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64,
    )
    assert manifest["gateway_execute_arn"].endswith("abc123/dev") and manifest["crew_model_id"] == "example-model"
    assert manifest["crew_profile_revision"] == "crew-r1" and manifest["dashboard_enabled"] is True


def test_customer_kms_run_registry_records_the_key_the_foundation_granted():
    key = "arn:aws:kms:us-east-1:000000000000:key/11111111-2222-3333-4444-555555555555"
    manifest = MODULE.build_manifest(outputs(run_table_kms_key_arn=key), source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64)
    assert manifest["run_table_kms_key_arn"] == key
    plain = MODULE.build_manifest(outputs(), source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64)
    assert plain["manifest_sha256"] != manifest["manifest_sha256"]
    # The foundation output may predate the field (release_context without the key).
    legacy = outputs()
    del legacy["release_context"]["value"]["run_table_kms_key_arn"]
    assert MODULE.build_manifest(legacy, source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64) == plain


@pytest.mark.parametrize("overrides,message", [
    ({"image_revision": ""}, "image_revision is empty"),
    ({"run_table_kms_key_arn": "arn:aws:kms:us-east-1:999999999999:key/11111111-2222-3333-4444-555555555555"}, "invalid run registry key"),
    ({"run_table_arn": "", "run_table_kms_key_arn": "arn:aws:kms:us-east-1:000000000000:key/11111111-2222-3333-4444-555555555555"},
     "run registry key requires the approved run table"),
    ({"image_id": "not-an-ami"}, "invalid image"),
    ({"controller_principal_arn": "someone"}, "invalid controller principal"),
    ({"workspace_disk_gb": 101}, "invalid workspace disk size"),
])
def test_refuses_incomplete_or_foreign_release_context(overrides, message):
    with pytest.raises(ValueError, match=message):
        MODULE.build_manifest(outputs(**overrides), source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64)


def test_missing_release_context_is_an_actionable_error(tmp_path, capsys):
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps({"launch_template_id": {"value": "lt-1"}}))
    code = MODULE.main(["--outputs", str(path), "--source-revision", SOURCE, "--infra-sha256", "d" * 64,
                        "--module-sha256", "e" * 64, "--output", str(tmp_path / "release.json")])
    assert code == 2
    assert "release_context" in capsys.readouterr().err


def test_cli_writes_the_manifest_and_prints_its_digest(tmp_path, capsys):
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(outputs()))
    target = tmp_path / "release.json"
    assert MODULE.main(["--outputs", str(path), "--source-revision", SOURCE, "--infra-sha256", "d" * 64,
                        "--module-sha256", "e" * 64, "--output", str(target)]) == 0
    manifest = json.loads(target.read_text())
    assert capsys.readouterr().out.strip() == manifest["manifest_sha256"]


def test_user_data_crew_bootstrap_context_pins_the_bundle_in_the_manifest():
    bundle_sha = "c" * 64
    uri = f"s3://adp-terraform-state-000000000000/dev-box/image-inputs/{bundle_sha}/crew.tar.gz"
    manifest = MODULE.build_manifest(
        outputs(crew_profile_revision="crew-r1", crew_bootstrap="user-data", crew_bundle_s3_uri=uri,
                crew_bundle_sha256=bundle_sha, crew_source_revision="f" * 40),
        source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64,
    )
    assert manifest["crew_bootstrap"] == "user-data" and manifest["crew_bundle_sha256"] == bundle_sha
    assert manifest["crew_bundle_s3_uri"] == uri and manifest["crew_source_revision"] == "f" * 40
    assert manifest["source_revision"] == SOURCE, "the release source (base AMI) and the bundle source are pinned separately"


def test_image_crew_bootstrap_context_leaves_the_manifest_unchanged():
    plain = MODULE.build_manifest(outputs(), source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64)
    with_mode = MODULE.build_manifest(
        outputs(crew_bootstrap="image", crew_bundle_s3_uri="", crew_bundle_sha256="", crew_source_revision=""),
        source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64,
    )
    assert plain == with_mode and "crew_bootstrap" not in plain


def test_user_data_context_without_its_pins_is_refused():
    with pytest.raises(ValueError, match="Crew bundle digest"):
        MODULE.build_manifest(outputs(crew_profile_revision="crew-r1", crew_bootstrap="user-data"),
                              source_revision=SOURCE, infra_sha256="d" * 64, module_sha256="e" * 64)


def test_launch_plan_from_the_foundation_outputs_enters_the_manifest():
    plain = MODULE.build_manifest(outputs(), source_revision=SOURCE, infra_sha256="1" * 64, module_sha256="2" * 64)
    assert not {"fallback_sizes", "instance_hour_rates_usd", "private_subnets"} & set(plain)
    planned = MODULE.build_manifest(outputs(
        allowed_sizes=["m6a.xlarge", "m6i.xlarge"], instance_size="m6a.xlarge",
        fallback_sizes=["m6a.xlarge", "m6i.xlarge"],
        instance_hour_rates_usd={"m6a.xlarge": "0.1728", "m6i.xlarge": "0.192"},
        private_subnets=[{"subnet_id": "subnet-0123456789abcdef0", "availability_zone": "us-east-1a"},
                         {"subnet_id": "subnet-0fedcba9876543210", "availability_zone": "us-east-1b"}],
    ), source_revision=SOURCE, infra_sha256="1" * 64, module_sha256="2" * 64)
    assert planned["fallback_sizes"] == ["m6a.xlarge", "m6i.xlarge"]
    assert planned["instance_hour_rates_usd"] == {"m6a.xlarge": "0.1728", "m6i.xlarge": "0.192"}
    assert [item["availability_zone"] for item in planned["private_subnets"]] == ["us-east-1a", "us-east-1b"]


@pytest.mark.parametrize("model_id, valid", [
    ("x", True), ("global.anthropic.example-model:1", True),
    ("a" * 256, True), ("a" * 257, False),
    ("", False), ("bad/model", False), ("bad model", False),
    ("model\n", False),
])
def test_model_id_validation_is_portable_and_bounded(model_id, valid):
    script = (SCRIPTS / "deploy-dev-box.sh").read_text()
    patterns = re.findall(r'\[\[ "\$CREW_MODEL_ID" =~ (\S+) \]\]', script)
    assert len(patterns) == 2  # Flag and persisted SSM model pin.
    for pattern in patterns:
        result = subprocess.run(
            ["bash", "-c", '[[ "$1" =~ $2 ]]', "validate-model", model_id, pattern],
            capture_output=True, text=True,
        )
        assert result.returncode == (0 if valid else 1), result.stderr
