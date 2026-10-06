"""Read compiler-produced cleanup evidence using scoped, read-only record doubles."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from superplane_acceptance import _demo1_cleanup_probe
from superplane_acceptance._demo1_cleanup_probe import collect
from superplane_acceptance.demo1_report import reference
from superplane_acceptance.demo1_teardown import validate_teardown_review
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.retirement_access_authority import access_request
from workspace_provisioning.retirement_request import retirement_request
from workspace_provisioning.runtime_config import LifecycleRefused

from . import test_managed_retirement_request as producer_fixtures

composed = producer_fixtures.composed


def identity(value):
    return str(UUID(int=value))


class Records:
    def __init__(self, composed):
        self.inventory, plan, access, source, self.policy, _ = composed
        policy = self.policy
        self.plan = plan
        self.operations = {}
        now = datetime.now(UTC)
        common = dict(source.admitted_request().parameters)
        common.update(plan_revision="b" * 64)
        for phase, operation_id, request_id, parent in (
            ("prepare-infrastructure", identity(12), identity(30), None),
            ("apply-infrastructure", identity(13), identity(31), identity(12)),
            ("bootstrap-workspace", identity(14), identity(32), identity(13)),
        ):
            parameters = {**common, "lifecycle_phase": phase}
            if parent is not None:
                parameters["lifecycle_source_operation_id"] = parent
            if phase == "apply-infrastructure":
                parameters["allocation_id"] = plan.original_allocation_id
            self.record(
                OperationRequest("provision", request_id, parameters), operation_id
            )
        source = SimpleNamespace(**self.operations[identity(14)])
        paid_request = self.requests[identity(13)]
        paid = SimpleNamespace(
            state="succeeded",
            operation_id=identity(13),
            org_id=plan.org_id,
            workspace_id=plan.workspace_id,
            admitted_request=lambda: paid_request,
        )
        preparation = access_request(
            plan, source, policy, allocation_source=paid, prepare_destroy=True
        )
        control = self.record(preparation, identity(81))
        self.row = {
            **access,
            "parameters_json": canonical(dict(preparation.parameters)),
            "source_operation_id": control["operation_id"],
            "source_job_id": control["job_id"],
            "source_attempt_id": control["attempt_id"],
            "source_request_payload": control["request_payload"],
            "source_payload_digest": control["plan_digest"],
            "producer_holder": identity(90),
            "producer_attempt_id": control["attempt_id"],
            "producer_fence_token": 1,
            "request_revision": preparation.parameters["plan_revision"],
            "created_at": now - timedelta(seconds=1),
        }
        self.rehash()
        self.scope = {
            "org_id": plan.org_id,
            "workspace_id": plan.workspace_id,
            "request_id": identity(30),
            "plan_revision": "b" * 64,
            "account": plan.cluster_arn.split(":")[4],
            "region": plan.cluster_arn.split(":")[3],
            "authorized_at": (now - timedelta(hours=1)).isoformat(),
            "observed_at": now.isoformat(),
            "retirement_request_id": plan.retirement_request_id,
            "source_operation_id": identity(14),
            "preparation_request_id": preparation.idempotency_key,
            "preparation_revision": payload_digest(preparation),
            "preparation_plan_revision": preparation.parameters["plan_revision"],
            "original_allocation_id": plan.original_allocation_id,
            "approval_id": identity(80),
        }
        self.candidates = 1
        self.approved = True
        self.sealed = True
        self.in_snapshot = False
        self.current = identity(14)
        self.calls = []

    def record(self, request, operation_id):
        row = {
            "operation_id": operation_id,
            "org_id": self.plan.org_id,
            "workspace_id": self.plan.workspace_id,
            "job_id": identity(int(UUID(operation_id)) + 100),
            "attempt_id": identity(int(UUID(operation_id)) + 200),
            "state": "succeeded",
            "action": request.action,
            "idempotency_key": request.idempotency_key,
            "plan_digest": payload_digest(request),
            "request_payload": encode_payload(request),
        }
        self.operations[operation_id] = row
        if not hasattr(self, "requests"):
            self.requests = {}
        self.requests[operation_id] = request
        return row

    def rehash(self):
        self.row["artifact_id"] = digest(
            {
                key: value
                for key, value in self.row.items()
                if key not in {"artifact_id", "created_at"}
            }
        )

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def transaction(self, **options):
        assert options == {"isolation": "repeatable_read", "readonly": True}
        self.in_snapshot = True
        try:
            yield
        finally:
            self.in_snapshot = False

    def checked(self, sql, args):
        assert self.in_snapshot and sql.startswith("SELECT ")
        assert self.plan.org_id in map(str, args) and self.plan.workspace_id in map(
            str, args
        )
        self.calls.append((sql, args))

    async def fetch(self, sql, *args):
        self.checked(sql, args)
        assert "workspace_lifecycle_control_operations" in sql and "LIMIT 2" in sql
        assert args == (
            self.plan.org_id,
            self.plan.workspace_id,
            self.plan.retirement_request_id,
            identity(14),
        )
        return [{"artifact_id": self.row["artifact_id"]}] * self.candidates

    async def fetchrow(self, sql, *args):
        self.checked(sql, args)
        if "FROM workspaces " in sql:
            return {"provisioning_operation_id": self.current}
        if "FROM workspace_lifecycle_artifacts " in sql:
            return self.row if args[0] == self.row["artifact_id"] else None
        if "FROM harness_allocation_epoch " in sql:
            return None
        assert "FROM harness_operations " in sql
        return self.operations.get(args[0])

    async def fetchval(self, sql, *args):
        self.checked(sql, args)
        if "FROM harness_allocation_seal " in sql:
            return "sealed-original" if self.sealed else None
        assert "FROM harness_approval_consumption " in sql
        if "approval_id=$1" in sql:
            return self.approved and args[0] == identity(80)
        return self.approved

    def collect(self):
        return asyncio.run(collect(self.connect, self.scope, self.policy))


def test_cleanup_probe_reads_real_compiler_contract_and_refuses_changed_evidence(
    composed,
    monkeypatch,
):
    async def inventory(connection, org, workspace):
        assert connection.in_snapshot
        assert (org, workspace) == (
            connection.plan.org_id,
            connection.plan.workspace_id,
        )
        return connection.inventory

    monkeypatch.setattr(_demo1_cleanup_probe, "canonical_inventory", inventory)

    async def transport(connect, plan, bootstrap, paid):
        async with connect() as connection:
            assert connection.in_snapshot
        assert (
            plan.bootstrap_artifact_id == bootstrap.parameters["lifecycle_artifact_id"]
        )
        assert (
            paid["operation_id"]
            == bootstrap.parameters["lifecycle_source_operation_id"]
        )
        return {
            "cluster_arn": plan.cluster_arn,
            "cluster_name": plan.cluster_arn.rsplit("/", 1)[-1],
            "cluster_endpoint": "https://example.invalid",
            "cluster_certificate_authority_data": "ZmljdGlvbmFs",
        }

    monkeypatch.setattr(_demo1_cleanup_probe, "cluster_transport", transport)
    records = Records(composed)
    result = records.collect()
    assert result["status"] == "OBSERVED" and result["scope"] == records.scope
    assert result["artifact_id"] == records.row["artifact_id"]
    assert result["grant_count"] == len(records.plan.grants)
    assert (
        result["grants"] == json.loads(records.row["artifact_metadata_json"])["grants"]
    )
    assert digest(result["grants"]) == result["grant_set_sha256"]
    assert len(result["kubernetes"]["grants"]) == 8
    assert digest(result["kubernetes"]) == result["kubernetes_inventory_sha256"]
    assert result["plan_file_sha256"] == "d" * 64
    inventory, plan, _, _, policy, _ = composed
    source = SimpleNamespace(
        **records.operations[identity(14)],
        admitted_request=lambda: records.requests[identity(14)],
    )
    request, deletion = retirement_request(inventory, plan, records.row, source, policy)
    assert result["retirement_plan_sha256"] == request.parameters["plan_revision"]
    assert result["retirement_revision_sha256"] == payload_digest(request)
    now = datetime.now(UTC)
    artifact = {
        "status": "OBSERVED",
        "release_ref": reference("fixture-release"),
        "recorded_at": result["recorded_at"],
        "observed_at": now.isoformat(),
        "artifact_ref": reference(result["artifact_id"]),
        **{
            key.removesuffix("_sha256") + "_ref": reference(value)
            for key, value in result.items()
            if key.endswith("_sha256")
        },
    }
    selected = SimpleNamespace(
        authorized_at=datetime.fromisoformat(records.scope["authorized_at"]),
        deadline=now + timedelta(days=1),
        account=records.scope["account"],
        region=records.scope["region"],
    )
    envelope = SimpleNamespace(
        runtime_target=SimpleNamespace(release_id="fixture-release"),
        max_runtime_seconds=policy["operation_max_runtime_seconds"],
    )
    original = SimpleNamespace(
        workspace_id=plan.workspace_id, retirement_request_id=request.idempotency_key
    )
    review = {
        "request_id": request.idempotency_key,
        "workspace_id": plan.workspace_id,
        "source_operation_id": source.operation_id,
        "source_payload_digest": source.plan_digest,
        "lifecycle_artifact_id": request.parameters["lifecycle_artifact_id"],
        "account_id": selected.account,
        "region": selected.region,
        "inventory_sha256": request.parameters["retirement_inventory_sha256"],
        "lifecycle_policy_sha256": request.parameters["lifecycle_policy_sha256"],
        "runtime_config_sha256": request.parameters["runtime_config_sha256"],
        "steps": [asdict(step) for step in deletion.steps],
        "preserved": list(deletion.preserved),
        "admission_available": True,
        "blocked_reason": None,
        "revision": payload_digest(request),
        "approval_request": {
            "workspace_id": plan.workspace_id,
            "action": request.action,
            "idempotency_key": request.idempotency_key,
            "parameters": dict(request.parameters),
        },
    }
    matched = validate_teardown_review(
        review, selected, envelope, original, artifact, now
    )
    assert matched["review_status"] == "OBSERVED" and matched["status"] == "BLOCKED"
    assert matched["admission_submitted"] is False
    for failure in (
        "missing",
        "duplicate",
        "unsealed",
        "unapproved",
        "wrong_approval",
        "stale",
        "future",
        "changed_workspace",
        "root",
        "running",
        "attempt",
        "payload",
        "grant",
        "destroy",
        "fence",
        "artifact",
        "canonical_inventory",
        "policy",
    ):
        changed = copy.deepcopy(records)
        if failure in ("missing", "duplicate"):
            changed.candidates = 0 if failure == "missing" else 2
        elif failure == "unsealed":
            changed.sealed = False
        elif failure == "unapproved":
            changed.approved = False
        elif failure == "wrong_approval":
            changed.scope["approval_id"] = identity(99)
        elif failure in ("stale", "future"):
            changed.row["created_at"] += timedelta(days=-1 if failure == "stale" else 1)
        elif failure == "changed_workspace":
            changed.current = identity(99)
        elif failure == "root":
            changed.scope["request_id"] = identity(99)
        elif failure in ("running", "attempt", "payload"):
            key = {
                "running": "state",
                "attempt": "attempt_id",
                "payload": "plan_digest",
            }[failure]
            changed.operations[identity(81)][key] = (
                "running" if failure == "running" else "f" * 64
            )
        elif failure == "artifact":
            changed.row["producer_holder"] = identity(99)
        elif failure == "canonical_inventory":
            from dataclasses import replace

            changed.inventory = replace(changed.inventory, components_complete=False)
        elif failure == "policy":
            changed.policy["operation_max_runtime_seconds"] += 1
        else:
            metadata = json.loads(changed.row["artifact_metadata_json"])
            if failure == "grant":
                metadata["grants"][0]["identity"]["generation"] = "foreign"
            elif failure == "destroy":
                metadata["reviewed_destroy"]["target"]["workspace_id"] = identity(99)
            elif failure == "fence":
                metadata["retirement_fence"]["identity"]["policy_uid"] = "foreign"
            changed.row["artifact_metadata_json"] = canonical(metadata)
            changed.rehash()
        with pytest.raises((ValueError, LifecycleRefused)):
            changed.collect()
