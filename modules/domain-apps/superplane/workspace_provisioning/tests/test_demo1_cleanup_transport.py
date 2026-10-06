"""Immutable apply-artifact transport binding with real digest validation, offline."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from superplane_acceptance._demo1_cleanup_probe import cluster_transport
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.runtime_config import LifecycleRefused


@pytest.fixture
def recorded():
    plan = SimpleNamespace(
        org_id="example-org",
        workspace_id="example-workspace",
        original_allocation_id="paid-allocation",
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/example-owned",
    )
    original = OperationRequest(
        "provision",
        "apply-original",
        {
            "plan_revision": "a" * 64,
            "lifecycle_phase": "apply-infrastructure",
            "allocation_id": plan.original_allocation_id,
        },
    )
    paid = {
        "operation_id": "apply-operation",
        "job_id": "apply-job",
        "attempt_id": "apply-attempt",
        "request_payload": encode_payload(original),
        "plan_digest": payload_digest(original),
    }
    target = {
        "account_id": "123456789012",
        "aws_region": "us-east-1",
        "org_id": plan.org_id,
        "workspace_id": plan.workspace_id,
    }
    transport = {
        "cluster_arn": plan.cluster_arn,
        "cluster_name": "example-owned",
        "cluster_endpoint": "https://example-cluster.example.invalid",
        "cluster_certificate_authority_data": "ZmljdGlvbmFsLWNh",
    }
    metadata = {
        "next_phase": "bootstrap-workspace",
        "allocation_source_operation_id": paid["operation_id"],
        "outputs": {
            key: {"value": value, "type": "string", "sensitive": False}
            for key, value in {**target, **transport}.items()
        },
    }
    row = {
        "org_id": plan.org_id,
        "workspace_id": plan.workspace_id,
        "account_id": target["account_id"],
        "source_operation_id": paid["operation_id"],
        "source_job_id": paid["job_id"],
        "source_attempt_id": paid["attempt_id"],
        "source_request_payload": paid["request_payload"],
        "source_payload_digest": paid["plan_digest"],
        "parameters_json": canonical(dict(original.parameters)),
        "request_revision": original.parameters["plan_revision"],
        "target_json": canonical(target),
        "artifact_metadata_json": canonical(metadata),
        "created_at": datetime.now(UTC) - timedelta(days=2),
    }
    parameters = {
        "lifecycle_source_operation_id": paid["operation_id"],
        "lifecycle_request": canonical(
            {
                "mode": "existing-account-managed",
                "organization_id": "o-example1234",
                "management_account_id": "000000000001",
                "management_cluster": "example-management",
                "region": "us-east-1",
                "workspace_id": plan.workspace_id,
                "target_account_id": "123456789012",
            }
        ),
    }

    def rehash():
        row["artifact_id"] = digest(
            {
                key: value
                for key, value in row.items()
                if key not in {"artifact_id", "created_at"}
            }
        )
        plan.bootstrap_artifact_id = row["artifact_id"]
        parameters["lifecycle_artifact_id"] = row["artifact_id"]

    rehash()

    class Snapshot:
        async def fetchrow(self, sql, *args):
            assert sql.startswith("SELECT * FROM workspace_lifecycle_artifacts")
            return (
                row
                if args == (row["artifact_id"], row["org_id"], row["workspace_id"])
                else None
            )

    @asynccontextmanager
    async def connect():
        yield Snapshot()

    def run():
        return asyncio.run(
            cluster_transport(
                connect, plan, SimpleNamespace(parameters=parameters), paid
            )
        )

    return SimpleNamespace(
        run=run,
        rehash=rehash,
        row=row,
        paid=paid,
        parameters=parameters,
        plan=plan,
        transport=transport,
    )


def test_transport_comes_from_exact_historical_apply_artifact(recorded):
    assert recorded.run() == recorded.transport


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "tenant",
        "workspace",
        "target",
        "operation",
        "job",
        "attempt",
        "payload",
        "bootstrap-artifact",
        "bootstrap-parent",
        "mode",
        "output-shape",
        "missing-ca",
    ],
)
def test_foreign_or_changed_apply_transport_is_refused(recorded, case):
    if case == "hash":
        recorded.row["artifact_metadata_json"] = "{}"
    elif case in {"tenant", "workspace"}:
        recorded.row["org_id" if case == "tenant" else "workspace_id"] = "foreign"
        recorded.rehash()
    elif case == "target":
        metadata = json.loads(recorded.row["artifact_metadata_json"])
        metadata["outputs"]["account_id"]["value"] = "000000000000"
        recorded.row["artifact_metadata_json"] = canonical(metadata)
        recorded.rehash()
    elif case in {"operation", "job", "attempt", "payload"}:
        field = {
            "operation": "operation_id",
            "job": "job_id",
            "attempt": "attempt_id",
            "payload": "request_payload",
        }[case]
        recorded.paid[field] = "changed"
    elif case == "bootstrap-artifact":
        recorded.parameters["lifecycle_artifact_id"] = "f" * 64
    elif case == "bootstrap-parent":
        recorded.parameters["lifecycle_source_operation_id"] = "foreign"
    elif case == "mode":
        request = json.loads(recorded.parameters["lifecycle_request"])
        request["mode"] = "bring-existing-cluster"
        recorded.parameters["lifecycle_request"] = canonical(request)
    else:
        metadata = json.loads(recorded.row["artifact_metadata_json"])
        if case == "output-shape":
            metadata["outputs"]["cluster_endpoint"]["extra"] = True
        else:
            del metadata["outputs"]["cluster_certificate_authority_data"]
        recorded.row["artifact_metadata_json"] = canonical(metadata)
        recorded.rehash()
    with pytest.raises((ValueError, KeyError, LifecycleRefused)):
        recorded.run()
