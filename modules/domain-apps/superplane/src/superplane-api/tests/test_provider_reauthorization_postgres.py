# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""A planning retry needs its own scoped human decision, never an old ticket."""

import uuid

import pytest
from harness_jobs.execution import record_intent
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.leases import acquire
from harness_jobs.store import _record

from app.services.operation_approvals import ApprovalDenied
from app.services.provider_recovery import (
    ProviderRecoveryService,
    SavedPlanCompletionService,
    InterruptedPlanningRecoveryService,
    UnstartedPlanningRecoveryService,
    UnstartedBootstrapRecoveryService,
)
from tests.test_dispatch_reauthorization_postgres import recovery as recovery
from tests.test_lifecycle_api_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    lifecycle as lifecycle,
    pytestmark as pytestmark,
)


@pytest.mark.parametrize("runtime_seconds", [None, 10800])
async def test_bootstrap_retry_requires_bootstrap_phase_and_its_own_decision(
    planning_recovery,
    runtime_seconds,
):
    from dataclasses import replace
    import json
    from harness_jobs.identity import decode_payload, encode_payload, payload_digest

    context, planning, _, workspace_id, operation, _ = planning_recovery
    service = UnstartedBootstrapRecoveryService(context.sessions, context.composition)
    with context.actor(workspace_id=workspace_id):
        with pytest.raises(ApprovalDenied):
            await service.request(
                workspace_id,
                expected_version=operation.version,
                idempotency_key=str(uuid.uuid4()),
            )
    # Isolated fixture models a separately admitted bootstrap. No production
    # recovery endpoint changes an original request, digest, or approval.
    original = decode_payload(operation.request_payload)
    parameters = dict(original.parameters)
    parameters.update(
        lifecycle_phase="bootstrap-workspace", lifecycle_artifact_id="a" * 64
    )
    parameters["execution_steps"] = json.dumps(
        [
            {
                "step_id": "bootstrap-workspace",
                "provider": "superplane-lifecycle",
                "operation_kind": "bootstrap-workspace",
                "target": "a" * 64,
            }
        ]
    )
    request = replace(original, parameters=parameters)
    async with context.connections.connect() as db:
        for table in ("harness_operations", "operation_approvals"):
            await db.execute(
                "UPDATE " + table + " SET request_payload=$1,plan_digest=$2",
                encode_payload(request),
                payload_digest(request),
            )
        await db.execute(
            "UPDATE harness_approval_consumption SET plan_digest=$1",
            payload_digest(request),
        )
        old = dict(await db.fetchrow("SELECT * FROM operation_approvals"))
    with context.actor(workspace_id=workspace_id):
        request_key = str(uuid.uuid4())
        ticket = await service.request(
            workspace_id,
            expected_version=operation.version,
            idempotency_key=request_key,
            max_runtime_seconds=runtime_seconds,
        )
        assert ticket["reauthorization_scope"] == "unstarted-bootstrap"
        assert "no Terraform replay" in ticket["recovery_scope"]
        assert ticket == await service.request(
            workspace_id,
            expected_version=operation.version,
            idempotency_key=request_key,
            max_runtime_seconds=runtime_seconds,
        )
        if runtime_seconds is not None:
            assert ticket["runtime_amendment"] == {
                "version": 1,
                "kind": "bootstrap-runtime-extension-v1",
                "original_max_runtime_seconds": old["max_runtime_seconds"],
                "max_runtime_seconds": 10800,
            }
            assert ticket["effective_max_runtime_seconds"] == 10800
            assert (
                ticket["envelope"]["max_runtime_seconds"] == old["max_runtime_seconds"]
            )
            with pytest.raises(ApprovalDenied, match="different admission"):
                await service.request(
                    workspace_id,
                    expected_version=operation.version,
                    idempotency_key=request_key,
                )
        else:
            assert "runtime_amendment" not in ticket
        for invalid in (10801, True, "10800", old["max_runtime_seconds"]):
            with pytest.raises(ApprovalDenied, match="runtime amendment"):
                await service.request(
                    workspace_id,
                    expected_version=operation.version,
                    idempotency_key=str(uuid.uuid4()),
                    max_runtime_seconds=invalid,
                )
    with context.actor("approver", workspace_id):
        with pytest.raises(ApprovalDenied):
            await planning.decide(
                workspace_id, ticket["reauthorization_id"], "allowed-once"
            )
        assert (
            await service.decide(
                workspace_id, ticket["reauthorization_id"], "allowed-once"
            )
        )["result"] == "allowed-once"
    async with context.connections.connect() as db:
        assert dict(await db.fetchrow("SELECT * FROM operation_approvals")) == old
        renewal = await db.fetchrow("SELECT * FROM operation_reauthorizations")
        assert (renewal["expires_at"] - renewal["created_at"]).total_seconds() == (
            10800
            if runtime_seconds is not None
            else min(3600, old["max_runtime_seconds"])
        )
        assert await db.fetchval("SELECT count(*) FROM operation_reauthorizations") == 1
        assert await db.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert (
            await db.fetchval("SELECT stage FROM harness_provider_call_intent")
            == "intended"
        )


def test_bootstrap_review_exposes_duration_without_widening_other_recovery_routes():
    from pydantic import ValidationError
    from app.routers.dispatch_recovery import Review
    from app.routers.provider_recovery import BootstrapReview

    request = {"expected_version": 2, "idempotency_key": str(uuid.uuid4())}
    assert BootstrapReview(**request).max_runtime_seconds is None
    assert (
        BootstrapReview(**request, max_runtime_seconds=10800).max_runtime_seconds
        == 10800
    )
    with pytest.raises(ValidationError):
        Review(**request, max_runtime_seconds=10800)
    for invalid in (True, "10800", 10800.0, 900, 10801, -1):
        with pytest.raises(ValidationError):
            BootstrapReview(**request, max_runtime_seconds=invalid)


@pytest.fixture
async def planning_recovery(recovery):
    context, old_service, workspace_id, _, old = recovery
    async with context.connections.connect() as db:
        await db.execute("UPDATE harness_operations SET state='running'")
        operation = _record(await db.fetchrow("SELECT * FROM harness_operations"))
        lease = await acquire(
            db,
            operation_id=operation.operation_id,
            holder="worker",
            attempt_id="worker",
        )
        step = admitted_steps(operation)[0]
        await record_intent(
            db,
            lease,
            idempotency_key=step_key(operation, step),
            provider=step.provider,
            operation_kind=step.operation_kind,
            target=step.target,
        )
        await db.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 hour',"
            "runtime_deadline=clock_timestamp()-interval '1 hour'"
        )
    return (
        context,
        ProviderRecoveryService(context.sessions, context.composition),
        old_service,
        workspace_id,
        operation,
        old,
    )


async def test_retry_review_and_decision_preserve_old_approval(planning_recovery):
    context, service, old_service, workspace_id, operation, old = planning_recovery
    with context.actor(workspace_id=workspace_id):
        ticket = await service.request(
            workspace_id,
            expected_version=operation.version,
            idempotency_key=str(uuid.uuid4()),
        )
        assert ticket["reauthorization_scope"] == "uncredentialed-planning"
        with pytest.raises(ApprovalDenied):
            await service.decide(
                workspace_id, ticket["reauthorization_id"], "allowed-once"
            )
    with context.actor("approver", workspace_id):
        with pytest.raises(ApprovalDenied):
            await old_service.decide(
                workspace_id, ticket["reauthorization_id"], "allowed-once"
            )
        decided = await service.decide(
            workspace_id, ticket["reauthorization_id"], "allowed-once"
        )
        assert decided["result"] == "allowed-once"
    async with context.connections.connect() as db:
        assert dict(await db.fetchrow("SELECT * FROM operation_approvals")) == old
        assert (
            await db.fetchval("SELECT stage FROM harness_provider_call_intent")
            == "intended"
        )
        assert await db.fetchval("SELECT count(*) FROM harness_provider_retries") == 0


@pytest.mark.parametrize(
    "service_type",
    [
        UnstartedPlanningRecoveryService,
        SavedPlanCompletionService,
        InterruptedPlanningRecoveryService,
    ],
)
async def test_start_barrier_decision_is_separate_and_retains_original(
    planning_recovery,
    service_type,
):
    context, old_service, _, workspace_id, operation, old = planning_recovery
    service = service_type(context.sessions, context.composition)
    with context.actor(workspace_id=workspace_id):
        ticket = await service.request(
            workspace_id,
            expected_version=operation.version,
            idempotency_key=str(uuid.uuid4()),
        )
        assert ticket["reauthorization_scope"] == service_type.recovery_scope
        assert (
            "issued AWS sessions expired"
            if service_type is UnstartedPlanningRecoveryService
            else "recorded abandoned plan lock"
            if service_type is InterruptedPlanningRecoveryService
            else "No Terraform plan or apply is replayed"
        ) in ticket["recovery_scope"]
        assert ticket["result"] == "pending"
    with context.actor("approver", workspace_id):
        with pytest.raises(ApprovalDenied):
            await old_service.decide(
                workspace_id, ticket["reauthorization_id"], "allowed-once"
            )
        decided = await service.decide(
            workspace_id, ticket["reauthorization_id"], "allowed-once"
        )
        assert decided["result"] == "allowed-once"
    async with context.connections.connect() as db:
        assert dict(await db.fetchrow("SELECT * FROM operation_approvals")) == old
        assert (
            await db.fetchval("SELECT stage FROM harness_provider_call_intent")
            == "intended"
        )
        assert await db.fetchval("SELECT count(*) FROM harness_provider_retries") == 0


async def test_live_or_completed_provider_call_cannot_get_retry_review(
    planning_recovery,
):
    context, service, _, workspace_id, operation, _ = planning_recovery
    async with context.connections.connect() as db:
        await db.execute(
            "UPDATE harness_operation_leases SET runtime_deadline=clock_timestamp()+interval '1 hour'"
        )
    with context.actor(workspace_id=workspace_id), pytest.raises(ApprovalDenied):
        await service.request(
            workspace_id,
            expected_version=operation.version,
            idempotency_key=str(uuid.uuid4()),
        )
