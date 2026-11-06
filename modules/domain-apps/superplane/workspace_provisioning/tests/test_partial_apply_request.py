"""Repair preparation has one identity, zero spend and unchanged original inputs."""

import copy
import json
from types import SimpleNamespace

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from workspace_provisioning.artifacts import digest
from workspace_provisioning.partial_apply_request import preparation_request
from workspace_provisioning.partial_apply_execution import verify_completed_inventory
from workspace_provisioning.runtime_config import LifecycleRefused


@pytest.fixture
def source_case():
    p = {
        "lifecycle_phase": "apply-infrastructure",
        "lifecycle_artifact_id": "a" * 64,
        "lifecycle_source_operation_id": "original-plan",
        "allocation_id": "paid-allocation",
        "lifecycle_root_allocation_id": "root-allocation",
        "plan_revision": "b" * 64,
        "lifecycle_request": json.dumps(
            {
                "mode": "existing-account-managed",
                "workspace_id": "workspace",
                "region": "us-west-2",
                "target_account_id": "123456789012",
            }
        ),
        "lifecycle_inputs": json.dumps(
            {"isolation_mode": "dedicated", "budget_max_daily_usd": "100"}
        ),
        "aws_account_id": "123456789012",
        "credential_id": "spda1:original-provider",
        "max_resource_units": "2",
        "max_cost_micros": "100000000",
        "max_runtime_seconds": "3600",
        "lifecycle_allocation_max_cost_micros": "100000000",
        "execution_steps": "original-descriptor",
    }
    original = OperationRequest("provision", "original-request", p)
    source = SimpleNamespace(
        operation_id="partial-apply",
        org_id="org",
        workspace_id="workspace",
        version=2,
        state="running",
        plan_digest=payload_digest(original),
        request_payload=encode_payload(original),
        admitted_request=lambda: original,
    )
    evidence = {
        k: getattr(source, k)
        for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
    }
    evidence.update(
        source_version=2,
        source_fence=1,
        original_artifact_id="a" * 64,
        apply_started=True,
        evidence_kind="partial-apply-state-v1",
        account_id="123456789012",
        region="us-west-2",
        credential_id=p["credential_id"],
        snapshot={"serial": 2},
        record_id="sppa1:" + "c" * 64,
    )
    return source, evidence, p


def test_same_source_has_one_separate_zero_spend_preparation(source_case):
    source, evidence, original = source_case
    before = copy.deepcopy(source.__dict__)
    a = preparation_request(source, evidence)
    assert a == preparation_request(source, evidence)
    p = a.parameters
    assert a.idempotency_key != "original-request"
    assert (
        p["allocation_id"]
        != p["partial_apply_original_allocation_id"]
        == "paid-allocation"
    )
    assert p["max_resource_units"] == p["max_cost_micros"] == "0"
    for key in (
        "lifecycle_request",
        "lifecycle_inputs",
        "lifecycle_root_allocation_id",
        "plan_revision",
        "max_runtime_seconds",
        "lifecycle_allocation_max_cost_micros",
        "credential_id",
    ):
        assert p[key] == original[key]
    assert "lifecycle_artifact_id" not in p and "lifecycle_source_operation_id" not in p
    assert p["partial_apply_owner_evidence_sha256"] == digest(evidence)
    assert source.__dict__ == before


def test_fresh_inspection_has_its_own_exact_approval_identity(source_case):
    source, old, original = source_case
    previous = preparation_request(source, old)
    fresh = {
        **old,
        "version": 2,
        "inspection_id": "1" * 16,
        "record_id": "sppa1:" + "d" * 64,
    }
    current = preparation_request(source, fresh)
    assert current.idempotency_key != previous.idempotency_key
    assert current == preparation_request(source, fresh)
    assert (
        current.parameters["partial_apply_source_operation_id"] == source.operation_id
    )
    assert (
        current.parameters["partial_apply_source_fence"]
        == previous.parameters["partial_apply_source_fence"]
    )
    assert (
        current.parameters["lifecycle_allocation_max_cost_micros"]
        == original["lifecycle_allocation_max_cost_micros"]
    )
    assert (
        current.parameters["max_cost_micros"]
        == current.parameters["max_resource_units"]
        == "0"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_version", 3),
        ("source_fence", True),
        ("original_artifact_id", "d" * 64),
        ("workspace_id", "foreign"),
        ("account_id", "000000000000"),
        ("region", "us-east-1"),
        ("plan_digest", "e" * 64),
        ("credential_id", "spda1:other"),
        ("apply_started", False),
        ("evidence_kind", "planning-start-barrier-v1"),
        ("record_id", "sppr1:" + "c" * 64),
    ],
)
def test_preparation_cannot_relabel_scope_or_reuse_planning_absence(
    source_case, key, value
):
    source, evidence, _ = source_case
    evidence[key] = value
    with pytest.raises(LifecycleRefused):
        preparation_request(source, evidence)


def test_completed_repair_must_preserve_all_retained_provider_identities():
    snapshot = {
        "resources": {
            "aws_eks_cluster.workspace": {"type": "aws_eks_cluster", "id": "cluster"},
            "aws_eip.nat[0]": {"type": "aws_eip", "id": "eipalloc-old"},
        }
    }
    metadata = {
        "applied_resources": [
            {
                "address": address,
                "type": v["type"],
                "identity": {
                    "id": v["id"] if address != "aws_eip.nat[0]" else "eipalloc-new"
                },
            }
            for address, v in snapshot["resources"].items()
        ]
    }
    verify_completed_inventory(metadata, snapshot)
    metadata["applied_resources"][0]["identity"]["id"] = "replaced-cluster"
    with pytest.raises(LifecycleRefused, match="retained"):
        verify_completed_inventory(metadata, snapshot)
