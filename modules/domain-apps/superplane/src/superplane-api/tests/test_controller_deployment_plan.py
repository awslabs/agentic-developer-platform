"""The producer must obey the actual controller contract without mutable defaults."""

from copy import deepcopy
import base64
import json
from functools import lru_cache
from uuid import uuid4

import pytest
from harness_jobs.identity import ContractViolation, OperationRefused, OperationRequest
from superplane_executor.deployment_plan import (
    BATCH_FIELDS,
    build_deployment_preview,
    teardown_request,
    validate_request,
)
from superplane_executor.plan import Plan


@lru_cache(maxsize=1)
def public_certificate_data():
    from tests.test_kubeconfig import _generate_self_signed

    certificate, _ = _generate_self_signed("synthetic-controller-ca.example.invalid")
    return base64.b64encode(certificate).decode("ascii")


def profile_fixture(cluster_id):
    return {
        "cluster_id": str(cluster_id),
        "cluster_arn": "arn:aws:eks:us-west-2:000000000002:cluster/workspace",
        "endpoint": "https://workspace.example.invalid",
        "namespace": "tenant-models",
        "provider_account_id": "000000000002",
        "region": "us-west-2",
        "image_id": "ami-0123456789abcdef0",
        "instance_type": "g5.xlarge",
        "node_count": 1,
        "disk_size": 100,
        "instance_profile": "approved-nodes",
        "vpc_name": "workspace",
        "security_group": "approved-workers",
        "service_cidr": "172.20.0.0/16",
        "certificate_authority": public_certificate_data(),
        "physical_gpus": 1,
        "max_runtime_seconds": 900,
        "max_cost_micros": 1_000_000,
        "credential_reference": {
            "credential_id": "cred-fixture",
            "credential_service": "aws",
            "credential_label": "fixture",
        },
        "serving_auth_contract": "superplane-token-file-header-v1",
        "model_options": {
            "model_name": "fixture/model",
            "precision": "fp16",
            "serving_framework": "vllm",
            "replicas": 1,
            "gpu_per_replica": 1,
            "tensor_parallel_size": 1,
            "max_model_len": None,
        },
        "workload": {
            "kind": "serving",
            "image": "registry.example/verified-server@sha256:" + "a" * 64,
            "command": ["/app/serve"],
            "args": ["--model", "fixture/model"],
            "gpu_count": 1,
            "cpu": "2000m",
            "memory": "8Gi",
            "port": 8000,
            "auth_secret": "model-access",
        },
    }


def build(profile=None, **changes):
    profile = profile or profile_fixture(uuid4())
    values = {
        "org_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "request_id": str(uuid4()),
        "profile_id": "approved-model",
        "profile": profile,
        "target": {
            key: profile[key]
            for key in (
                "cluster_id",
                "cluster_arn",
                "endpoint",
                "namespace",
                "provider_account_id",
            )
        },
        "name": "model-server",
        "model_options": deepcopy(profile["model_options"]),
        **changes,
    }
    return build_deployment_preview(**values), values


@pytest.mark.parametrize(
    "change",
    [None, "endpoint", "secret", "model", "invocation", "empty-command", "unbounded"],
)
def test_batch_profile_requires_closed_immutable_invocation_without_serving_surface(
    change,
):
    profile = profile_fixture(uuid4())
    profile["model_options"] = {}
    profile["serving_auth_contract"] = None
    profile["workload"].update(
        kind="batch", port=None, auth_secret=None, command=["/app/batch"]
    )
    if change == "endpoint":
        profile["workload"]["port"] = 8000
    elif change == "secret":
        profile["workload"]["auth_secret"] = "token"
    elif change == "model":
        profile["model_options"] = {"model_name": "a-model"}
    elif change == "empty-command":
        profile["workload"]["command"] = []
    elif change == "unbounded":
        profile["max_runtime_seconds"] = 0
    options = {key: deepcopy(profile["workload"][key]) for key in BATCH_FIELDS}
    if change == "invocation":
        options["command"] = ["/app/other"]
    if change is not None:
        with pytest.raises(OperationRefused):
            build(
                profile, model_options={}, workload_kind="batch", batch_options=options
            )
    else:
        preview, values = build(
            profile, model_options={}, workload_kind="batch", batch_options=options
        )
        parsed = validate_request(
            preview.request,
            values["target"],
            org_id=values["org_id"],
            workspace_id=values["workspace_id"],
        )
        assert parsed.data["workload"]["kind"] == "batch"
        assert preview.deployment_request == {
            "name": values["name"],
            "profile_id": values["profile_id"],
            "kind": "batch",
            **options,
        }


def test_producer_and_teardown_use_same_real_plan_validator_and_allocation():
    preview, values = build()
    original = validate_request(
        preview.request,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    stopped = teardown_request(
        preview.request,
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
        request_id=str(uuid4()),
        source_operation_id="original-paid-operation",
    )
    teardown = validate_request(
        stopped,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    assert original.data == teardown.data
    assert original.cluster_name == teardown.cluster_name
    assert (
        stopped.parameters["allocation_id"]
        == preview.request.parameters["allocation_id"]
    )
    assert [item["operation_kind"] for item in teardown.steps] == ["delete_cluster"]


def test_normal_certificate_plan_fits_unchanged_shared_parameter_bounds():
    preview, values = build()
    parameters = preview.request.parameters
    certificate = parameters["controller_certificate_authority"]
    wire = json.loads(parameters["controller_plan"])
    assert 1200 < len(certificate) <= 2000
    assert wire["version"] == 2
    assert "certificate_authority" not in wire
    assert len(parameters["controller_plan"]) <= 2000
    # This representative real-format certificate cannot fit in the old single
    # parameter alongside its plan. The producer must not return a tiny-fixture
    # success while rejecting the normal request before admission.
    combined = {**wire, "certificate_authority": certificate}
    del combined["certificate_authority_sha256"]
    assert len(json.dumps(combined, separators=(",", ":"))) > 2000
    plan = validate_request(
        preview.request,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    assert plan.data["certificate_authority"] == certificate


@pytest.mark.parametrize(
    "change",
    [
        None,
        "fixed",
        "empty",
        "duplicate",
        "overflow",
        "physical",
        "boolean",
        "undersized",
        "injection",
        "cpu",
        "memory",
    ],
)
def test_gpu_constraints_use_skypilot_selection_with_an_exact_physical_envelope(change):
    profile = profile_fixture(uuid4())
    del profile["instance_type"]
    profile.update(
        accelerators=["A10G:1", "L4:1"],
        max_gpus_per_node=4,
        physical_gpus=4,
        cpus=4,
        memory_gb=32,
    )
    if change == "cpu":
        profile["cpus"] = 2
    elif change == "memory":
        profile["memory_gb"] = 8
    elif change == "fixed":
        profile["instance_type"] = "g5.xlarge"
    elif change == "empty":
        profile["accelerators"] = []
    elif change == "duplicate":
        profile["accelerators"] = ["L4:1", "L4:1"]
    elif change == "overflow":
        profile["accelerators"] = ["H100:8"]
    elif change == "physical":
        profile["physical_gpus"] = 1
    elif change == "boolean":
        profile["max_gpus_per_node"] = True
    elif change == "undersized":
        profile["workload"]["gpu_count"] = 2
        profile["model_options"]["gpu_per_replica"] = 2
    elif change == "injection":
        profile["accelerators"] = ["L4:1;echo bad"]
    if change:
        with pytest.raises(OperationRefused):
            build(profile)
        return
    preview, values = build(profile)
    parsed = validate_request(
        preview.request,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    assert parsed.data["version"] == 3
    assert "instance_type" not in parsed.data
    assert parsed.data["accelerators"] == ["A10G:1", "L4:1"]
    assert preview.request.parameters["max_resource_units"] == "4"
    stopped = teardown_request(
        preview.request,
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
        request_id=str(uuid4()),
        source_operation_id="original-paid-operation",
    )
    teardown = validate_request(
        stopped,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    assert parsed.data == teardown.data
    assert parsed.cluster_name == teardown.cluster_name
    assert (
        stopped.parameters["max_resource_units"]
        == stopped.parameters["max_cost_micros"]
        == "0"
    )


def regional_profile_fixture(cluster_id):
    profile = profile_fixture(cluster_id)
    del profile["instance_type"]
    for key in ("region", "image_id", "vpc_name", "security_group", "instance_profile"):
        del profile[key]
    profile.update(
        regions=[
            {
                "region": "us-west-2",
                "image_id": "ami-0123456789abcdef0",
                "vpc_name": "workspace-west",
                "security_group": "approved-workers-west",
                "vpc_id": "vpc-0123456789abcdef0",
                "security_group_id": "sg-0123456789abcdef0",
                "subnet_ids": ["subnet-0123456789abcdef0"],
                "instance_profile": "approved-nodes-west",
            },
            {
                "region": "us-east-1",
                "image_id": "ami-0123456789abcdef1",
                "vpc_name": "workspace-east",
                "security_group": "approved-workers-east",
                "vpc_id": "vpc-0123456789abcdef0",
                "security_group_id": "sg-0123456789abcdef0",
                "subnet_ids": ["subnet-0123456789abcdef0"],
                "instance_profile": "approved-nodes-east",
            },
        ],
        accelerators=["A10G:1", "L4:1"],
        max_gpus_per_node=4,
        physical_gpus=4,
        cpus=4,
        memory_gb=32,
    )
    return profile


@pytest.mark.parametrize(
    "change",
    [
        None,
        "single-region",
        "duplicate-region",
        "duplicate-image",
        "too-many",
        "empty",
        "extra-field",
    ],
)
def test_regional_plan_reaches_skypilot_without_pinning_one_region(change):
    """#5925 acceptance 1: >=2 eligible regions reach the plan unpinned; a
    single-region request (the existing v1-3 shape) still works unchanged.
    """
    profile = regional_profile_fixture(uuid4())
    if change == "single-region":
        profile["regions"] = profile["regions"][:1]
    elif change == "duplicate-region":
        profile["regions"][1]["region"] = profile["regions"][0]["region"]
    elif change == "duplicate-image":
        profile["regions"][1]["image_id"] = profile["regions"][0]["image_id"]
    elif change == "too-many":
        profile["regions"] = profile["regions"] * 3
    elif change == "empty":
        profile["regions"] = []
    elif change == "extra-field":
        profile["regions"][0]["zone"] = "us-west-2a"
    if change in (
        "duplicate-region",
        "duplicate-image",
        "too-many",
        "empty",
        "extra-field",
    ):
        with pytest.raises(OperationRefused):
            build(profile)
        return
    preview, values = build(profile)
    parsed = validate_request(
        preview.request,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    assert parsed.data["version"] == 4
    assert "region" not in parsed.data
    assert "image_id" not in parsed.data
    assert "instance_type" not in parsed.data
    assert len(parsed.data["regions"]) == (1 if change == "single-region" else 2)
    bindings = parsed.region_bindings
    assert len(bindings) == len(parsed.data["regions"])
    assert preview.request.parameters["max_resource_units"] == "4"
    # Teardown preserves the original bounded region set unchanged.
    stopped = teardown_request(
        preview.request,
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
        request_id=str(uuid4()),
        source_operation_id="original-paid-operation",
    )
    teardown = validate_request(
        stopped,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    assert parsed.data == teardown.data


@pytest.mark.parametrize(
    "change",
    ["missing", "certificate", "digest", "oversize", "non-certificate", "target"],
)
def test_v2_certificate_and_target_are_bound_to_exact_approved_parameters(change):
    preview, values = build()
    parameters = dict(preview.request.parameters)
    wire = json.loads(parameters["controller_plan"])
    if change == "missing":
        del parameters["controller_certificate_authority"]
    elif change == "certificate":
        parameters["controller_certificate_authority"] = (
            parameters["controller_certificate_authority"][:-4] + "AAAA"
        )
    elif change == "digest":
        wire["certificate_authority_sha256"] = "0" * 64
    elif change == "oversize":
        parameters["controller_certificate_authority"] = "A" * 2001
    elif change == "non-certificate":
        import hashlib

        parameters["controller_certificate_authority"] = base64.b64encode(
            b"not a PEM certificate"
        ).decode()
        wire["certificate_authority_sha256"] = hashlib.sha256(
            parameters["controller_certificate_authority"].encode()
        ).hexdigest()
    else:
        values["target"]["endpoint"] = "https://replacement.example.invalid"
    parameters["controller_plan"] = json.dumps(wire, separators=(",", ":"))
    with pytest.raises((OperationRefused, ContractViolation)):
        request = OperationRequest(
            "provision", preview.request.idempotency_key, parameters
        )
        Plan.validate_request(
            request,
            values["target"],
            org_id=values["org_id"],
            workspace_id=values["workspace_id"],
            max_resource_units=1,
            max_runtime_seconds=900,
            max_cost_micros=1_000_000,
        )


def test_v1_existing_admission_remains_readable_without_transport_rewrite():
    preview, values = build()
    parameters = dict(preview.request.parameters)
    wire = json.loads(parameters["controller_plan"])
    del wire["certificate_authority_sha256"]
    del parameters["controller_certificate_authority"]
    wire.update(version=1, certificate_authority="historical-test-ca")
    parameters["controller_plan"] = json.dumps(wire, separators=(",", ":"))
    request = OperationRequest("provision", preview.request.idempotency_key, parameters)
    plan = Plan.validate_request(
        request,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
        max_resource_units=1,
        max_runtime_seconds=900,
        max_cost_micros=1_000_000,
    )
    assert plan.data == wire


@pytest.mark.parametrize(
    "change",
    [
        "latest",
        "auth",
        "replicas",
        "missing-ami",
        "oversize-ca",
        "unbounded",
        "namespace",
        "gpu-mismatch",
    ],
)
def test_unsupported_or_unreviewed_profile_never_produces_paid_request(change):
    profile = profile_fixture(uuid4())
    target = {
        key: profile[key]
        for key in (
            "cluster_id",
            "cluster_arn",
            "endpoint",
            "namespace",
            "provider_account_id",
        )
    }
    if change == "latest":
        profile["workload"]["image"] = "vllm/vllm-openai:latest"
    elif change == "auth":
        profile["serving_auth_contract"] = "raw-vllm"
    elif change == "replicas":
        profile["model_options"]["replicas"] = 2
    elif change == "missing-ami":
        del profile["image_id"]
    elif change == "oversize-ca":
        profile["certificate_authority"] = "a" * 2001
    elif change == "unbounded":
        profile["max_cost_micros"] = 0
    elif change == "namespace":
        target["namespace"] = "another-tenant"
    elif change == "gpu-mismatch":
        profile["physical_gpus"] = 0
    with pytest.raises(OperationRefused):
        build(profile, target=target)


@pytest.mark.parametrize(
    "action,change", [("provision", "zero-envelope"), ("teardown", "wrong-step")]
)
def test_zero_new_spend_exception_is_limited_to_reviewed_teardown(action, change):
    preview, values = build()
    request = preview.request
    if action == "teardown":
        request = teardown_request(
            request,
            org_id=values["org_id"],
            workspace_id=values["workspace_id"],
            request_id=str(uuid4()),
            source_operation_id="original-paid-operation",
        )
    parameters = dict(request.parameters)
    if change == "zero-envelope":
        parameters.update(max_resource_units="0", max_cost_micros="0")
    else:
        steps = json.loads(parameters["execution_steps"])
        steps[0]["operation_kind"] = "launch"
        parameters["execution_steps"] = json.dumps(steps, separators=(",", ":"))
    altered = OperationRequest(action, request.idempotency_key, parameters)
    with pytest.raises(OperationRefused):
        validate_request(
            altered,
            values["target"],
            org_id=values["org_id"],
            workspace_id=values["workspace_id"],
        )


def test_network_profile_is_carried_unchanged_through_preview_and_teardown():
    profile = regional_profile_fixture(uuid4())

    def side(octet):
        return {
            "vpc_id": "vpc-0123456789abcdef0",
            "vpc_cidr": f"10.{octet}.0.0/16",
            "node_cidr": f"10.{octet}.0.0/24",
            "pod_cidr": f"10.{octet + 10}.0.0/16",
            "subnet_ids": ["subnet-0123456789abcdef0"],
            "transit_gateway_id": "tgw-0123456789abcdef0",
            "attachment_id": None,
            "transit_gateway_route_table_id": "tgw-rtb-0123456789abcdef0",
            "vpc_route_table_ids": ["rtb-0123456789abcdef0"],
            "security_group_id": "sg-0123456789abcdef0",
        }

    profile["network"] = {
        "cluster": {
            "version": 1,
            "cluster_id": profile["cluster_id"],
            "membership_generation": "a" * 64,
            "network": side(1),
        },
        "regions": {
            "us-east-1": {
                "network": side(2),
                "peering_id": None,
                "dns": {
                    "rule_id": "rslvr-rr-12345678",
                    "association_id": "rslvr-rrassoc-12345678",
                    "outbound_endpoint_id": "rslvr-out-12345678",
                    "inbound_endpoint_id": "rslvr-in-12345678",
                },
            }
        },
    }
    preview, values = build(profile)
    parsed = validate_request(
        preview.request,
        values["target"],
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
    )
    assert parsed.network == profile["network"]
    stopped = teardown_request(
        preview.request,
        org_id=values["org_id"],
        workspace_id=values["workspace_id"],
        request_id=str(uuid4()),
        source_operation_id="original",
    )
    assert (
        stopped.parameters["controller_network_cluster"]
        == preview.request.parameters["controller_network_cluster"]
    )
    assert (
        stopped.parameters["controller_network_regions"]
        == preview.request.parameters["controller_network_regions"]
    )
