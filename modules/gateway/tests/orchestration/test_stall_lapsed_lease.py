"""A stalled, acknowledged worker whose lease lapsed a full period ago has its claim released (#7742).

Production shape (tools flow TLS-S19, 2026-10-15): the worker hung after a model response, kept a
``held`` claim with no heartbeat for hours, ignored SIGTERM when its pod was deleted, and the engine's
only answer was the stall detector — whose ``failed`` node a human then resumed straight back into the
same dead execution, because the claim was still held. Lease expiry alone never authorises a takeover;
the fence below requires the stall judgement, a lease lapsed for one further full period, and a
protected execution record with no exit and no status change since.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.developer_recovery import recover_developer
from src.orchestration.models import OrchestrationDecision, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.stall import _Candidate, _fence_lapsed_lease_assignment
from src.orchestration.work_claims import DEFAULT_LEASE_SECONDS
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, store  # noqa: F401


async def _stalled_candidate(cycle, *, lapsed_seconds, heartbeat_after_expiry=False):  # noqa: F811
    now = datetime.now(UTC)
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        execution.handoff_receipt_ref = None
        claim = await db.get(OrchestrationWorkClaim, execution.claim_id)
        claim.state, claim.active_run_id, claim.claim_event_id = "held", cycle.root, cycle.root
        claim.lease_expires_at = now - timedelta(seconds=lapsed_seconds)
        claim.heartbeat_at = (
            (now - timedelta(seconds=1)) if heartbeat_after_expiry else claim.lease_expires_at - timedelta(seconds=DEFAULT_LEASE_SECONDS)
        )
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.state = "running"
        candidate = _Candidate(
            node_id=node.id,
            org_id=node.org_id,
            flow_id=node.flow_id,
            observed_state="running",
            attempts=node.attempts,
            since=now - timedelta(hours=6),
        )
        await db.commit()
    return candidate, now


def _exec_item(cycle, status="active", **extra):  # noqa: F811
    item = {
        "pk": {"S": f"TENANT#{ORG}"},
        "sk": {"S": f"EXEC#{cycle.root}"},
        "tenant_id": {"S": ORG},
        "invocation_id": {"S": cycle.root},
        "flow_id": {"S": cycle.flow.id},
        "orchestration_node_id": {"S": cycle.node.id},
        "orchestration_node_attempt": {"N": "1"},
        "status": {"S": status},
        "persona": {"S": "developer"},
        "arrived_at": {"S": (datetime.now(UTC) - timedelta(hours=6)).isoformat()},
    }
    item.update(extra)
    cycle.store.client.put_item(TableName=cycle.store.table, Item=item)


async def test_lapsed_lease_fence_releases_the_dead_claim_and_concludes_the_execution(cycle):  # noqa: F811
    candidate, now = await _stalled_candidate(cycle, lapsed_seconds=DEFAULT_LEASE_SECONDS + 60)
    _exec_item(cycle)
    async with cycle.factory() as db:
        fence = await _fence_lapsed_lease_assignment(db, candidate, now=now, store=cycle.store)
        await db.commit()
    assert fence is not None and fence["fence"] == "lease_lapsed_stall" and fence["run_id"] == cycle.root
    async with cycle.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, fence["claim_id"])
        assert claim.state == "released" and claim.release_reason == "abandoned" and claim.active_run_id is None
        assert claim.claim_event_id == cycle.root
        execution = await db.get(OrchestrationExecution, fence["failed_execution_id"])
        assert execution.status == "concluded"


@pytest.mark.parametrize(
    "case",
    ["lease_not_lapsed_long_enough", "heartbeat_after_expiry", "exec_terminal", "exec_cancelled", "exec_missing", "status_updated_after_expiry"],
)
async def test_lapsed_lease_fence_refuses_without_all_three_facts(cycle, case):  # noqa: F811
    lapsed = DEFAULT_LEASE_SECONDS // 2 if case == "lease_not_lapsed_long_enough" else DEFAULT_LEASE_SECONDS + 60
    candidate, now = await _stalled_candidate(cycle, lapsed_seconds=lapsed, heartbeat_after_expiry=case == "heartbeat_after_expiry")
    if case == "exec_terminal":
        _exec_item(cycle, status="completed", terminal_outcome={"S": "complete"})
    elif case == "exec_cancelled":
        _exec_item(cycle, status="cancelled", work_claim_cancellation={"S": "unacknowledged_stall"})
    elif case == "status_updated_after_expiry":
        _exec_item(cycle, status_updated_at={"S": now.isoformat()})
    elif case != "exec_missing":
        _exec_item(cycle)
    async with cycle.factory() as db:
        fence = await _fence_lapsed_lease_assignment(db, candidate, now=now, store=cycle.store)
        await db.commit()
    assert fence is None, case
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        claim = await db.get(OrchestrationWorkClaim, execution.claim_id)
        assert claim.state == "held" and execution.status != "concluded"


async def test_developer_recovery_retries_a_lease_lapsed_stall(cycle, monkeypatch):  # noqa: F811
    """The stall decision carrying the fence is the same exit evidence as a claim-released settlement."""
    from src.orchestration.policy_admission import AdmissionInputs
    from tests.orchestration.test_policy_admission import _policy

    candidate, now = await _stalled_candidate(cycle, lapsed_seconds=DEFAULT_LEASE_SECONDS + 60)
    _exec_item(cycle)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    async with cycle.factory() as db:
        fence = await _fence_lapsed_lease_assignment(db, candidate, now=now, store=cycle.store)
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.state = "failed"
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                kind="node_stalled",
                actor_id="system:orchestration-stall-detector",
                actor_role="engine",
                actor_kind="service",
                from_state="running",
                to_state="failed",
                reason=json.dumps({**fence, "reason": "stalled: lease lapsed"}),
            )
        )
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_role="engine",
                actor_kind="service",
                reason=json.dumps({"attempt": 1, "run_id": cycle.root, "dispatch_envelope": {"persona": "developer"}}),
            )
        )
        await db.commit()
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", lambda *a, **k: _admission())

    async def _admission():
        return AdmissionInputs(_policy(), 1)

    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        result = await recover_developer(db, node, now=now + timedelta(seconds=120))
        await db.commit()
    assert result == "scheduled", result
    async with cycle.factory() as db:
        scheduled = (
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == cycle.node.id, OrchestrationDecision.kind == "developer_retry_scheduled"
                )
            )
        ).first()
        assert scheduled is not None and json.loads(scheduled.reason)["reason"] == "claim_released_retry"
