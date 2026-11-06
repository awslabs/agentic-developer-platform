"""Fixture-only release validation; no AWS mutation or credentials required."""

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "release.py"
SPEC = importlib.util.spec_from_file_location("dev_box_release", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def dispatch():
    return {
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


def test_release_manifest_is_deterministic_and_excludes_connection_label(dispatch):
    manifest = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert manifest == MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert manifest["image_id"] == dispatch["image_id"]
    assert manifest["source_revision"] == dispatch["adp_source_revision"]
    assert manifest["controller_status"] == "absent"
    assert dispatch["customer_aws_label"] not in json.dumps(manifest)
    assert len(manifest["manifest_sha256"]) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("adp_source_revision", "f" * 40),
        ("adp_definition_revision", "f" * 40),
        ("adp_correlation", "missing"),
        ("customer_aws_label", ""),
        ("customer_account_id", ""),
        ("workspace_disk_gb", 20000),
        ("allowed_sizes", ["t3.medium"]),
        ("max_disk_gb", 20),
        ("image_id", "latest"),
    ],
)
def test_invalid_dispatch_rejected_before_release(dispatch, field, value):
    dispatch[field] = value
    with pytest.raises(ValueError):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)


def test_cli_uses_selected_connection_without_publishing_its_label(dispatch, tmp_path):
    import os
    import subprocess
    import sys

    output = tmp_path / "release.json"
    environment = dict(os.environ)
    environment.update(
        {"DEV_BOX_" + key.upper(): json.dumps(value) if isinstance(value, (list, bool)) else str(value) for key, value in dispatch.items()}
    )
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--from-env", "--actual-source", "a" * 40, "--actual-definition", "b" * 40, "--output", str(output)],
        check=False,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text())
    assert manifest == MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert dispatch["customer_aws_label"] not in output.read_text()


def test_missing_connection_rejected_without_creating_release(dispatch, tmp_path):
    import subprocess
    import sys

    dispatch["customer_aws_label"] = ""
    fixture = tmp_path / "dispatch.json"
    fixture.write_text(json.dumps(dispatch))
    output = tmp_path / "release.json"
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--dispatch",
            str(fixture),
            "--actual-source",
            "a" * 40,
            "--actual-definition",
            "b" * 40,
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "invalid selected connection" in result.stderr
    assert not output.exists()


def test_host_run_table_is_opt_in_and_pinned_to_approved_account(dispatch):
    assert "run_table_arn" not in MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    dispatch["run_table_arn"] = "arn:aws:dynamodb:us-east-1:000000000000:table/example-host-runs"
    manifest = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert manifest["run_table_arn"] == dispatch["run_table_arn"]
    dispatch["run_table_arn"] = "arn:aws:dynamodb:us-east-1:999999999999:table/other-runs"
    with pytest.raises(ValueError, match="host run table"):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)


def test_run_registry_key_is_opt_in_and_pinned_to_the_registry_account(dispatch):
    key = "arn:aws:kms:us-east-1:000000000000:key/11111111-2222-3333-4444-555555555555"
    dispatch["run_table_kms_key_arn"] = key
    with pytest.raises(ValueError, match="run registry key requires the approved run table"):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    dispatch["run_table_arn"] = "arn:aws:dynamodb:us-east-1:000000000000:table/example-host-runs"
    manifest = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert manifest["run_table_kms_key_arn"] == key
    for candidate in (
        "arn:aws:kms:us-east-1:999999999999:key/11111111-2222-3333-4444-555555555555",
        "arn:aws:kms:us-west-2:000000000000:key/11111111-2222-3333-4444-555555555555",
        "arn:aws:kms:us-east-1:000000000000:alias/example",
        "arn:aws:kms:us-east-1:000000000000:key/*",
        "11111111-2222-3333-4444-555555555555",
    ):
        dispatch["run_table_kms_key_arn"] = candidate
        with pytest.raises(ValueError, match="invalid run registry key"):
            MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    # The empty workflow variable (release.py --from-env) means an AWS-owned key: no field.
    dispatch["run_table_kms_key_arn"] = ""
    without_key = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert "run_table_kms_key_arn" not in without_key
    assert without_key["manifest_sha256"] != manifest["manifest_sha256"], "the key is part of the release identity"


def test_guest_gateway_grant_requires_exact_selected_api_and_registry(dispatch):
    selected = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
    dispatch["run_table_arn"] = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
    dispatch["gateway_execute_arn"] = selected
    dispatch["crew_profile_revision"] = "crew-r1"
    dispatch["crew_model_id"] = "us.anthropic.example-v1:0"
    manifest = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert manifest["gateway_execute_arn"] == selected
    assert manifest["crew_model_id"] == "us.anthropic.example-v1:0"
    for candidate in (
        "arn:aws:execute-api:us-east-1:111111111111:exampleapi/dev",
        "arn:aws:execute-api:us-west-2:000000000000:exampleapi/dev",
        selected + "/*",
        "arn:aws:iam::000000000000:role/guest",
    ):
        dispatch["gateway_execute_arn"] = candidate
        with pytest.raises(ValueError, match="approved model gateway"):
            MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    dispatch["gateway_execute_arn"] = selected
    for invalid in ("", "bad/id", "a" * 257):
        dispatch["crew_model_id"] = invalid
        with pytest.raises(ValueError, match="approved Crew model"):
            MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    dispatch["crew_model_id"] = "us.anthropic.example-v1:0"
    dispatch.pop("run_table_arn")
    with pytest.raises(ValueError, match="approved run table"):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)


def test_dashboard_release_requires_pinned_crew_profile(dispatch):
    dispatch["dashboard_enabled"] = True
    with pytest.raises(ValueError, match="Crew profile revision"):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    dispatch["crew_profile_revision"] = "latest/unsafe"
    with pytest.raises(ValueError, match="Crew profile revision"):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    dispatch["crew_profile_revision"] = "crew-r1"
    manifest = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert manifest["crew_profile_revision"] == "crew-r1"
    assert manifest["dashboard_enabled"] is True


def test_permissions_accept_rules_merged_by_ec2_into_one_entry():
    """EC2 merges same-port rules into one IpPermission with several group pairs."""
    import importlib.util as _util

    runtime_spec = _util.spec_from_file_location("dev_box_runtime", MODULE_PATH.parent / "runtime.py")
    runtime = _util.module_from_spec(runtime_spec)
    runtime_spec.loader.exec_module(runtime)
    _permissions = runtime._permissions

    expected = {(443, "sg-gateway"), (443, "sg-ssm")}
    merged = [
        {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "IpRanges": [],
            "Ipv6Ranges": [],
            "PrefixListIds": [],
            "UserIdGroupPairs": [{"GroupId": "sg-gateway", "UserId": "1"}, {"GroupId": "sg-ssm", "UserId": "1"}],
        }
    ]
    split = [
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": "sg-gateway", "UserId": "1"}]},
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": "sg-ssm", "UserId": "1"}]},
    ]
    assert _permissions(merged, expected)
    assert _permissions(split, expected)
    extra_cidr = [dict(merged[0], IpRanges=[{"CidrIp": "0.0.0.0/0"}])]
    assert not _permissions(extra_cidr, expected)
    extra_pair = [dict(merged[0], UserIdGroupPairs=merged[0]["UserIdGroupPairs"] + [{"GroupId": "sg-other", "UserId": "1"}])]
    assert not _permissions(extra_pair, expected)
    duplicate = [dict(merged[0], UserIdGroupPairs=merged[0]["UserIdGroupPairs"] + [{"GroupId": "sg-ssm", "UserId": "1"}])]
    assert not _permissions(duplicate, expected)
    assert not _permissions([dict(merged[0], ToPort=444)], expected)
    assert not _permissions([], expected)


# --- Crew bootstrap modes (#8055) -------------------------------------------------
BUNDLE_SHA = "c" * 64
BUNDLE_URI = f"s3://adp-terraform-state-000000000000/dev-box/image-inputs/{BUNDLE_SHA}/crew.tar.gz"


def test_image_mode_manifest_is_byte_identical_without_bootstrap_fields(dispatch):
    before = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    dispatch["crew_bootstrap"] = "image"
    after = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert before == after
    assert not {"crew_bootstrap", "crew_bundle_s3_uri", "crew_bundle_sha256", "crew_source_revision"} & set(after)


def test_user_data_mode_pins_profile_bundle_location_digest_and_source(dispatch):
    dispatch.update(crew_profile_revision="crew-r1", crew_bootstrap="user-data", crew_bundle_s3_uri=BUNDLE_URI,
                    crew_bundle_sha256=BUNDLE_SHA, crew_source_revision="f" * 40)
    manifest = MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)
    assert manifest["crew_bootstrap"] == "user-data" and manifest["crew_bundle_s3_uri"] == BUNDLE_URI
    assert manifest["crew_bundle_sha256"] == BUNDLE_SHA and manifest["crew_source_revision"] == "f" * 40
    assert manifest["crew_profile_revision"] == "crew-r1"
    assert manifest == MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"crew_profile_revision": ""}, "approved Crew profile revision"),
        ({"crew_bundle_sha256": "C" * 64}, "Crew bundle digest"),
        ({"crew_bundle_s3_uri": "https://example.com/crew.tar.gz"}, "Crew bundle location"),
        ({"crew_bundle_s3_uri": f"s3://adp-terraform-state-000000000000/dev-box/image-inputs/{'d' * 64}/crew.tar.gz"}, "not addressed by its digest"),
        ({"crew_bundle_s3_uri": "s3://adp-terraform-state-000000000000/dev-box/image-inputs/latest/crew.tar.gz"}, "Crew bundle location"),
        ({"crew_source_revision": "main"}, "Crew source revision"),
        ({"crew_bootstrap": "ssm"}, "invalid Crew bootstrap mode"),
    ],
)
def test_user_data_mode_rejects_unpinned_or_mismatched_bundle_inputs(dispatch, changes, message):
    dispatch.update(crew_profile_revision="crew-r1", crew_bootstrap="user-data", crew_bundle_s3_uri=BUNDLE_URI,
                    crew_bundle_sha256=BUNDLE_SHA, crew_source_revision="f" * 40)
    dispatch.update(changes)
    with pytest.raises(ValueError, match=message):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)


def test_image_mode_refuses_stray_bundle_inputs(dispatch):
    dispatch["crew_bundle_sha256"] = BUNDLE_SHA
    with pytest.raises(ValueError, match="require user-data bootstrap"):
        MODULE.create_manifest(dispatch, "a" * 40, "b" * 40)


# --- Capacity fallback plan (#8423) --------------------------------------------------------------

PLAN = {
    "fallback_sizes": ["t3.small", "t3a.small"],
    "instance_hour_rates_usd": {"t3a.small": "0.0188", "t3.small": "0.0208"},
    "private_subnets": [
        {"subnet_id": "subnet-0123456789abcdef0", "availability_zone": "us-east-1a"},
        {"subnet_id": "subnet-0fedcba9876543210", "availability_zone": "us-east-1b"},
    ],
}


def test_launch_plan_is_opt_in_and_canonical(dispatch):
    without = MODULE.create_manifest(dict(dispatch), "a" * 40, "b" * 40)
    assert not {"fallback_sizes", "instance_hour_rates_usd", "private_subnets"} & set(without)
    planned = MODULE.create_manifest({**dispatch, "allowed_sizes": ["t3.small", "t3a.small"], **PLAN}, "a" * 40, "b" * 40)
    assert planned["fallback_sizes"] == ["t3.small", "t3a.small"]
    assert list(planned["instance_hour_rates_usd"]) == ["t3.small", "t3a.small"]
    assert planned["private_subnets"] == PLAN["private_subnets"]
    assert planned["manifest_sha256"] != without["manifest_sha256"]


@pytest.mark.parametrize("field, value, message", [
    ("fallback_sizes", ["t3a.small", "t3.small"], "fallback instance sizes"),      # must start with the template's size
    ("fallback_sizes", ["t3.small", "t3.large"], "fallback instance sizes"),       # only admitted sizes
    ("fallback_sizes", ["t3.small", "t3.small"], "fallback instance sizes"),       # no duplicates
    ("fallback_sizes", [], "fallback instance sizes"),
    ("instance_hour_rates_usd", {"t3.small": "0.0208"}, "instance hour rates"),    # every fallback size priced
    ("instance_hour_rates_usd", {"t3.small": "x", "t3a.small": "0.0188"}, "instance hour rates"),
    ("instance_hour_rates_usd", {"t3.small": "0.0208", "t3a.small": "0.0188", "t3.large": "0.1"}, "instance hour rates"),
    ("private_subnets", [{"subnet_id": "subnet-0fedcba9876543210", "availability_zone": "us-east-1b"}], "private subnets"),  # template subnet first
    ("private_subnets", [{"subnet_id": "subnet-0123456789abcdef0", "availability_zone": "us-east-1a"}, {"subnet_id": "subnet-0123456789abcdef0", "availability_zone": "us-east-1a"}], "private subnets"),
    ("private_subnets", [{"subnet_id": "subnet-0123456789abcdef0", "availability_zone": "nowhere"}], "private subnets"),
    ("private_subnets", [{"subnet_id": "subnet-0123456789abcdef0"}], "private subnets"),
])
def test_invalid_launch_plan_is_refused(dispatch, field, value, message):
    with pytest.raises(ValueError, match=message):
        MODULE.create_manifest({**dispatch, "allowed_sizes": ["t3.small", "t3a.small"], **PLAN, field: value}, "a" * 40, "b" * 40)
