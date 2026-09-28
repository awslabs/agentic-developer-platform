"""Approval proposals retain admission lineage and reject changed artifact bytes."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from workspace_provisioning.artifacts import (
    canonical,
    continuation_parameters,
    digest,
    initial_execution_steps,
    proposal,
    read_artifact,
)
from workspace_provisioning.runtime_config import LifecycleRefused


def artifact(phase="apply-infrastructure"):
    parameters = {
        "plan_revision": "b" * 64,
        "lifecycle_request": canonical({"mode": "existing-account-managed"}),
        "lifecycle_inputs": canonical({"isolation_mode": "namespace"}),
        "lifecycle_allocation_max_resource_units": "2",
        "lifecycle_allocation_max_runtime_seconds": "900",
        "lifecycle_allocation_max_cost_micros": "3000000",
    }
    request = OperationRequest("provision", "source-request", parameters)
    row = {
        "org_id": "org-original",
        "workspace_id": "workspace-original",
        "source_operation_id": "source-operation",
        "source_job_id": "source-job",
        "source_attempt_id": "original-admission-attempt",
        "producer_holder": "actual-worker",
        "producer_attempt_id": "actual-worker-attempt",
        "producer_fence_token": 4,
        "source_payload_digest": payload_digest(request),
        "source_request_payload": encode_payload(request),
        "request_revision": parameters["plan_revision"],
        "account_id": "000000000002",
        "target_json": canonical({"account_id": "000000000002"}),
        "parameters_json": canonical(parameters),
        "artifact_metadata_json": canonical({"next_phase": phase}),
    }
    row["artifact_id"] = digest(row)
    row["created_at"] = datetime.now(UTC)
    return row


class Reader:
    def __init__(self, row):
        self.row = row

    @asynccontextmanager
    async def connect(self):
        yield self

    async def fetchrow(self, sql, *args):
        assert "org_id=$2 AND workspace_id=$3" in sql
        if args != tuple(
            self.row[key] for key in ("artifact_id", "org_id", "workspace_id")
        ):
            return None
        return self.row


def read(row, **overrides):
    scope = {key: row[key] for key in ("artifact_id", "org_id", "workspace_id")}
    return asyncio.run(read_artifact(Reader(row).connect, **(scope | overrides)))


def test_original_admission_attempt_is_distinct_from_producer_lease_attempt():
    row = read(artifact())
    public = proposal(row)
    assert public["source_attempt_id"] == "original-admission-attempt"
    assert "actual-worker-attempt" not in canonical(public)
    assert "source_request_payload" not in public


@pytest.mark.parametrize(
    "column",
    [
        "target_json",
        "artifact_metadata_json",
        "source_job_id",
        "source_attempt_id",
        "producer_attempt_id",
        "producer_fence_token",
        "source_payload_digest",
    ],
)
def test_changed_immutable_artifact_is_refused(column):
    row = artifact()
    row[column] = 9 if column == "producer_fence_token" else "changed"
    with pytest.raises((LifecycleRefused, ValueError)):
        read(row)


def test_even_rehashed_row_cannot_relabel_original_payload_parameters():
    row = artifact()
    row["parameters_json"] = canonical({"plan_revision": "c" * 64})
    row["artifact_id"] = digest(
        {k: v for k, v in row.items() if k not in {"artifact_id", "created_at"}}
    )
    with pytest.raises(LifecycleRefused, match="digest changed"):
        read(row)


def test_cross_workspace_and_expired_proposal_are_refused():
    row = artifact()
    with pytest.raises(LifecycleRefused, match="no plan"):
        read(row, workspace_id="other-workspace")
    row["created_at"] -= timedelta(hours=2)
    with pytest.raises(LifecycleRefused, match="expired"):
        read(row)


@pytest.mark.parametrize(
    "phase,units,cost",
    [
        ("apply-infrastructure", "2", "3000000"),
        ("bootstrap-workspace", "0", "0"),
        ("bootstrap-account", "0", "0"),
        ("prepare-infrastructure", "0", "0"),
    ],
)
def test_continuations_bind_exact_artifact_and_only_apply_reuses_allocation(
    phase, units, cost
):
    row = artifact(phase)
    parameters = continuation_parameters(row)
    assert parameters["max_resource_units"] == units
    assert parameters["max_cost_micros"] == cost
    assert parameters["max_runtime_seconds"] == "900"
    assert parameters["lifecycle_allocation_max_cost_micros"] == "3000000"
    assert parameters["lifecycle_source_operation_id"] == "source-operation"
    assert row["artifact_id"] in parameters["execution_steps"]
    assert parameters["aws_account_id"] == row["account_id"]


def test_adoption_requires_reviewed_discovery_before_bootstrap():
    parameters = json.loads(artifact()["parameters_json"])
    parameters["lifecycle_request"] = canonical({"mode": "bring-existing-cluster"})
    assert "prepare-adoption" in initial_execution_steps(parameters)
    assert "bootstrap-workspace" not in initial_execution_steps(parameters)


def test_new_account_continuation_keeps_original_management_credential():
    row = artifact("bootstrap-account")
    parameters = json.loads(row["parameters_json"])
    parameters["lifecycle_request"] = canonical(
        {"mode": "new-account-managed", "management_account_id": "000000000001"}
    )
    parameters.update(
        aws_account_id="000000000001",
        provider_account_id="000000000001",
        credential_id="management-reference",
    )
    row["parameters_json"] = canonical(parameters)
    continued = continuation_parameters(row)
    assert continued["aws_account_id"] == "000000000001"
    assert continued["provider_account_id"] == "000000000001"
    assert continued["credential_id"] == "management-reference"
    assert row["account_id"] == "000000000002"


def test_terminal_bootstrap_artifact_keeps_apply_allocation_lineage():
    row = artifact("complete")
    parameters = json.loads(row["parameters_json"])
    parameters.update(
        lifecycle_phase="bootstrap-workspace", lifecycle_artifact_id="a" * 64
    )
    row["parameters_json"] = canonical(parameters)
    row["artifact_metadata_json"] = canonical(
        {"next_phase": "complete", "allocation_source_operation_id": "original-apply"}
    )
    ready = proposal(row)
    assert ready["status"] == "ready"
    assert ready["lifecycle_artifact_id"] == "a" * 64
    assert ready["allocation_source_operation_id"] == "original-apply"
    with pytest.raises(LifecycleRefused, match="phase is invalid"):
        continuation_parameters(row)
