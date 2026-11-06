# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""The selected human confirms the exact closed bootstrap and retained budget."""

import json
import uuid
from dataclasses import replace

import pytest
from fastapi import HTTPException
from harness_jobs.execution import audit, record_intent
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.leases import acquire
from harness_jobs.store import _record
from harness_jobs.identity import (
    OperationRefused,
    ResolvedPrincipal,
    decode_payload,
    encode_payload,
    payload_digest,
)
from harness_jobs.recovery import sweep_scoped_expired_leases

from app.services.operation_approvals import ApprovalDenied
from app.services.provider_recovery import SettledBootstrapRecoveryService
from app.routers.dispatch_recovery import call
from tests.reauthorization_schema import migration
from tests.test_provider_reauthorization_postgres import (
    planning_recovery as planning_recovery,
)
from tests.test_dispatch_reauthorization_postgres import recovery as recovery
from tests.test_lifecycle_api_postgres import (
    installation_postgres_url as installation_postgres_url,
)
from tests.test_lifecycle_api_postgres import ledger as ledger
from tests.test_lifecycle_api_postgres import lifecycle as lifecycle
from tests.test_lifecycle_api_postgres import pytestmark  # noqa: F401


@pytest.mark.parametrize(
    "fault",
    [None, "changed-closure", "changed-budget", "changed-assumption", "rejected"],
)
@pytest.mark.parametrize("legacy_terms", [False, True])
@pytest.mark.parametrize("same_cap", [False, True])
@pytest.mark.parametrize("assumption", [False, True])
async def test_distinct_approver_restores_only_reviewed_budget(
    recovery, fault, legacy_terms, same_cap, assumption, monkeypatch
):
    context, _, workspace_id, initial, _ = recovery
    source = _record(initial)
    request = decode_payload(source.request_payload)
    parameters = dict(request.parameters)
    parameters.update(
        lifecycle_phase="bootstrap-workspace",
        lifecycle_artifact_id="a" * 64,
        execution_steps=json.dumps(
            [
                {
                    "step_id": "bootstrap-workspace",
                    "provider": "superplane-lifecycle",
                    "operation_kind": "bootstrap-workspace",
                    "target": "a" * 64,
                }
            ]
        ),
    )
    request = replace(request, parameters=parameters)

    async def retained(db, lease, result):
        await db.execute(
            "INSERT INTO harness_recovery_settlements(receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,claim_holder,claim_attempt_id,claim_fence_token,payload_digest,accounting) "
            "VALUES('closed-fixture',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            source.operation_id,
            source.org_id,
            source.workspace_id,
            source.job_id,
            source.attempt_id,
            lease.holder,
            lease.attempt_id,
            lease.fence_token,
            "a" * 64,
            json.dumps(
                {"action": "unknown", "budget": "retain", "release_permitted": False}
            ),
        )

    async with context.connections.connect() as db:
        if same_cap:
            for table in (
                "operation_approvals",
                "harness_approval_consumption",
                "operation_budget_reservations",
            ):
                await db.execute("UPDATE " + table + " SET max_runtime_seconds=10800")
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
        await db.execute("UPDATE harness_operations SET state='running'")
        current = _record(await db.fetchrow("SELECT * FROM harness_operations"))
        granted = await acquire(
            db, operation_id=source.operation_id, holder="worker", attempt_id="worker"
        )
        step = admitted_steps(current)[0]
        await record_intent(
            db,
            granted,
            idempotency_key=step_key(current, step),
            provider=step.provider,
            operation_kind=step.operation_kind,
            target=step.target,
        )
        await db.execute(
            "UPDATE harness_operation_leases SET expires_at=now()-interval '1 hour',runtime_deadline=now()-interval '1 hour'"
        )
        lease = await db.fetchrow("SELECT * FROM harness_operation_leases")
        await audit(
            db,
            operation_id=source.operation_id,
            org_id=source.org_id,
            workspace_id=source.workspace_id,
            event="record_intent",
            actor=lease["holder"],
            allowed=True,
            attempt_id=lease["attempt_id"],
            fence_token=lease["fence_token"],
        )
        report = await sweep_scoped_expired_leases(
            db,
            principal=ResolvedPrincipal(
                source.org_id,
                source.workspace_id,
                "observer",
                frozenset({"workspace:recover"}),
            ),
            max_reconcile_attempts=1,
            on_settled=retained,
        )
        assert report.results[0].action == "unknown"
        await db.execute(
            "UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp()"
        )
        await db.execute(
            "UPDATE operation_budget_reservations SET state='retained',reason='uncertain bootstrap'"
        )
        original = dict(await db.fetchrow("SELECT * FROM operation_approvals"))
        operation = dict(await db.fetchrow("SELECT * FROM harness_operations"))
        budget = dict(await db.fetchrow("SELECT * FROM operation_budget_reservations"))
        if assumption:
            from harness_jobs import bootstrap_provenance_assumption
            from harness_jobs import settled_bootstrap

            closure = await settled_bootstrap.read(
                db,
                operation,
                dict(await db.fetchrow("SELECT * FROM harness_operation_leases")),
                dict(await db.fetchrow("SELECT * FROM harness_provider_call_intent")),
            )
            monkeypatch.setattr(
                bootstrap_provenance_assumption,
                "CLOSURE_SHA256",
                settled_bootstrap.digest(closure),
            )
    service = SettledBootstrapRecoveryService(context.sessions, context.composition)
    key = str(uuid.uuid4())
    if legacy_terms:
        await migration(context.sessions.kw["bind"], 57, "downgrade")
        with (
            context.actor(workspace_id=workspace_id),
            pytest.raises(HTTPException) as caught,
        ):
            await call(
                service.request(
                    workspace_id,
                    expected_version=operation["version"],
                    idempotency_key=key,
                    max_runtime_seconds=10800,
                )
            )
        assert caught.value.status_code == 503
        async with context.connections.connect() as db:
            assert not await db.fetchval(
                "SELECT count(*) FROM operation_reauthorizations"
            )
            assert (
                dict(await db.fetchrow("SELECT * FROM harness_operations")) == operation
            )
        await migration(context.sessions.kw["bind"], 57)
    with context.actor(workspace_id=workspace_id):
        ticket = await service.request(
            workspace_id,
            expected_version=operation["version"],
            idempotency_key=key,
            max_runtime_seconds=10800,
        )
        assert ticket["recovery_boundary"] == "settled-bootstrap-v1"
        assert ticket["effective_max_runtime_seconds"] == 10800
        assert ("provenance_assumption" in ticket) is assumption
        if assumption:
            assert ticket["provenance_assumption"]["executed_stdin_verified"] is False
            assert (
                "not captured in protected audit evidence" in ticket["recovery_scope"]
            )
        assert ticket["runtime_amendment"]["kind"] == (
            "bootstrap-runtime-reauthorization-v1"
            if same_cap
            else "bootstrap-runtime-extension-v1"
        )
        assert (
            ticket["envelope"]["max_runtime_seconds"] == original["max_runtime_seconds"]
        )
    async with context.connections.connect() as db:
        if fault == "changed-closure":
            await db.execute(
                "UPDATE harness_operation_leases SET closed_reason='changed'"
            )
        elif fault == "changed-budget":
            await db.execute(
                "UPDATE operation_budget_reservations SET max_cost_micros=max_cost_micros+1"
            )
        elif fault == "changed-assumption":
            from asyncpg import RaiseError
            from harness_jobs.settled_bootstrap import review_runtime

            terms = json.loads(
                await db.fetchval(
                    "SELECT amendment_json FROM operation_reauthorizations"
                )
            )
            if assumption:
                terms.pop("provenance_assumption")
            else:
                terms["provenance_assumption"] = {"executed_stdin_verified": True}
            with pytest.raises(OperationRefused):
                review_runtime(terms)
            with pytest.raises(
                RaiseError, match="reauthorization history is immutable"
            ):
                await db.execute(
                    "UPDATE operation_reauthorizations SET amendment_json=$1",
                    json.dumps(terms),
                )
    with context.actor("approver", workspace_id):
        if fault in {"changed-closure", "changed-budget"}:
            with pytest.raises((ApprovalDenied, OperationRefused)):
                await service.decide(
                    workspace_id, ticket["reauthorization_id"], "allowed-once"
                )
        else:
            result = await service.decide(
                workspace_id,
                ticket["reauthorization_id"],
                "rejected" if fault == "rejected" else "allowed-once",
            )
            assert result["decided_by"] == "approver"
    async with context.connections.connect() as db:
        assert dict(await db.fetchrow("SELECT * FROM operation_approvals")) == original
        assert dict(await db.fetchrow("SELECT * FROM harness_operations")) == operation
        after = dict(await db.fetchrow("SELECT * FROM operation_budget_reservations"))
        assert after["state"] == (
            "confirmed" if fault in {None, "changed-assumption"} else "retained"
        )
        if fault in {None, "changed-assumption"}:
            for key in (
                "max_resource_units",
                "max_runtime_seconds",
                "max_cost_micros",
                "job_id",
                "attempt_id",
                "reservation_id",
            ):
                assert after[key] == budget[key]
            assert ticket["reauthorization_id"] in after["reason"]
        assert not await db.fetchval("SELECT count(*) FROM harness_provider_retries")


def test_same_cap_terms_cannot_authorize_an_ordinary_unstarted_bootstrap():
    from types import SimpleNamespace
    from app.services.provider_recovery import UnstartedBootstrapRecoveryService

    terms = {
        "version": 1,
        "kind": "bootstrap-runtime-reauthorization-v1",
        "original_max_runtime_seconds": 10800,
        "max_runtime_seconds": 10800,
    }
    with pytest.raises(ApprovalDenied):
        UnstartedBootstrapRecoveryService.authority_lifetime_seconds(
            SimpleNamespace(max_runtime_seconds=10800), terms
        )
