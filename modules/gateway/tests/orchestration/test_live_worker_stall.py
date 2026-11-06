"""An active worker must not lose authorization at the legacy 5.4-hour cutoff."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration import stall
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.work_claims import DEFAULT_LEASE_SECONDS
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, store  # noqa: F401


async def _worker(ctx, monkeypatch, *, kind="protected", dispatch_case=None):
    now = datetime.now(UTC)
    start = now - timedelta(hours=7)
    run = ctx.root
    if kind == "reviewer":
        assert (await protocol.tick(ctx)).effects_succeeded == 1
        run = ctx.calls[-1]["message_id"]
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false" if kind == "shared" else "true")
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: ctx.service.writer)
    raw = ctx.store._read(f"TENANT#{protocol.ORG}", f"EXEC#{run}")
    raw.pop("terminal_outcome", None)  # The review-cycle fixture starts with a completed author.
    raw.update(status={"S": "active"}, workload_binding={"S": "current-worker-pod"})
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        node.updated_at = start
        claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
        claim.heartbeat_at = now - timedelta(seconds=10)
        claim.lease_expires_at = claim.heartbeat_at + timedelta(seconds=DEFAULT_LEASE_SECONDS)
        if kind == "reviewer":
            dispatch = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "review_cycle_dispatch"))
            dispatch.created_at = start
        else:
            execution = await db.get(OrchestrationExecution, ctx.execution.id)
            execution.handoff_receipt_ref = None
        if kind == "protected":
            db.add(
                OrchestrationDecision(
                    org_id=protocol.ORG,
                    flow_id=ctx.flow.id,
                    node_id=ctx.node.id,
                    kind="node_dispatched",
                    actor_id="untrusted" if dispatch_case == "untrusted_dispatch" else "system:orchestration-dispatch",
                    actor_kind="service",
                    actor_role="engine",
                    reason="invalid JSON"
                    if dispatch_case == "malformed_dispatch"
                    else json.dumps(
                        {
                            "run_id": "another-run" if dispatch_case == "different_dispatch_run" else run,
                            "attempt": 2 if dispatch_case == "different_dispatch_attempt" else ctx.node.attempts,
                        }
                    ),
                    created_at=start,
                )
            )
        elif kind in {"shared", "protected_report"}:
            db.add(
                OrchestrationRunReport(
                    run_id=run,
                    credential_hash="c" * 64,
                    org_id=protocol.ORG,
                    flow_id=ctx.flow.id,
                    node_id=ctx.node.id,
                    attempt=ctx.node.attempts,
                    persona="agent-codex-developer",
                    repo=protocol.REPO,
                    installation_id=42,
                    dispatch_metadata={},
                    worker_receipt={"recorded_at": start.isoformat()},
                    created_at=start,
                    expires_at=now + timedelta(hours=1),
                )
            )
        await db.commit()
    return now, run, raw


async def _detect(ctx, now, *, expected_stalls):
    async with ctx.factory() as db:
        result = await stall.detect_stalls(db, now=now, config=stall.StallConfig())
        assert result.errors == 0
        assert result.stalls_detected == expected_stalls
        node = await db.get(OrchestrationNode, ctx.node.id)
        claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
        assert node.state == ("failed" if expected_stalls else "running")
        assert claim.state == "held"
        await db.commit()


@pytest.mark.parametrize("kind", ["protected", "protected_report", "shared", "reviewer"])
async def test_acknowledged_current_worker_with_renewed_lease_survives_legacy_cutoff(cycle, monkeypatch, kind):  # noqa: F811
    now, _, _ = await _worker(cycle, monkeypatch, kind=kind)
    await _detect(cycle, now, expected_stalls=0)


@pytest.mark.parametrize(
    "case",
    [
        "expired_lease",
        "stale_heartbeat",
        "future_heartbeat",
        "no_heartbeat",
        "different_claim_event",
        "different_generation",
        "different_report_tenant",
        "unacknowledged",
        "unbound",
        "terminal",
        "cancelled",
        "different_execution_tenant",
        "different_execution_flow",
        "different_execution_node",
        "different_execution_attempt",
        "missing_execution",
        "shared_unacknowledged",
        "different_dispatch_run",
        "different_dispatch_attempt",
        "untrusted_dispatch",
        "malformed_dispatch",
    ],
)
async def test_live_exception_requires_current_identity_and_positive_liveness(cycle, monkeypatch, case):  # noqa: F811
    kind = "shared" if case == "shared_unacknowledged" else "protected_report" if case == "different_report_tenant" else "protected"
    now, run, raw = await _worker(cycle, monkeypatch, kind=kind, dispatch_case=case)
    async with cycle.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
        report = await db.get(OrchestrationRunReport, run)
        if case == "expired_lease":
            claim.heartbeat_at = now - timedelta(seconds=DEFAULT_LEASE_SECONDS + 1)
            claim.lease_expires_at = now - timedelta(seconds=1)
        elif case == "stale_heartbeat":
            claim.heartbeat_at = now - timedelta(hours=2)
        elif case == "future_heartbeat":
            claim.heartbeat_at = now + timedelta(hours=1)
            claim.lease_expires_at = now + timedelta(hours=2)
        elif case == "no_heartbeat":
            claim.heartbeat_at = None
        elif case == "different_claim_event":
            claim.claim_event_id = "another-worker"
        elif case == "different_generation":
            claim.generation += 1
        elif case == "different_report_tenant":
            report.org_id = "another-tenant"
        elif case == "shared_unacknowledged":
            report.worker_receipt = None
        await db.commit()
    if case == "unacknowledged":
        raw["status"] = {"S": "pending"}
    elif case == "unbound":
        raw.pop("workload_binding")
    elif case == "terminal":
        raw["terminal_outcome"] = {"S": "failed"}
    elif case == "cancelled":
        raw["work_claim_cancellation"] = {"S": "cancelled"}
    elif case.startswith("different_execution_"):
        key = {"tenant": "tenant_id", "flow": "flow_id", "node": "orchestration_node_id", "attempt": "orchestration_node_attempt"}[
            case.removeprefix("different_execution_")
        ]
        raw[key] = {"N": "2"} if key == "orchestration_node_attempt" else {"S": "different"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    if case == "missing_execution":
        cycle.store.client.delete_item(TableName=cycle.store.table, Key={"pk": raw["pk"], "sk": raw["sk"]})
    await _detect(cycle, now, expected_stalls=1)


async def test_lease_renewal_between_scan_and_transition_prevents_false_failure(cycle, monkeypatch):  # noqa: F811
    now, _, _ = await _worker(cycle, monkeypatch)
    async with cycle.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
        claim.heartbeat_at = now - timedelta(seconds=DEFAULT_LEASE_SECONDS + 1)
        claim.lease_expires_at = now - timedelta(seconds=1)
        await db.commit()
    original = stall._fence_lapsed_lease_assignment

    async def renewed_before_fence(db, candidate, **kwargs):
        async with cycle.factory() as concurrent:
            claim = await concurrent.get(OrchestrationWorkClaim, cycle.claim.id)
            claim.heartbeat_at = datetime.now(UTC)
            claim.lease_expires_at = claim.heartbeat_at + timedelta(seconds=DEFAULT_LEASE_SECONDS)
            await concurrent.commit()
        return await original(db, candidate, **kwargs)

    monkeypatch.setattr(stall, "_fence_lapsed_lease_assignment", renewed_before_fence)
    await _detect(cycle, now, expected_stalls=0)
