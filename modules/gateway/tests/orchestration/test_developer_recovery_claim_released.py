"""A story settled on its released work claim retries like any other settled developer failure (#7727).

The results pass fails a governed story whose claim was released as ``failed`` (by the exiting
worker) or ``abandoned`` (by the engine once the pod was proven gone) without a terminal receipt.
Developer recovery used to refuse those settlements (``worker_exit_unverified`` / ``work_owner_changed``)
because it looked for a receipt that cannot exist, so every worker death during a gateway rollout
needed an operator resume.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.developer_recovery import recover_developer, recover_failed_developers
from src.orchestration.models import OrchestrationDecision, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.results import observe_results
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, store  # noqa: F401


class _NoRunRecord:
    def get(self, run_id, arrived_at):
        return None


async def settled_by_release(cycle, monkeypatch, *, release_reason="failed", persona="agent-codex-developer", backoff=False):  # noqa: F811
    observed = datetime.now(UTC) + timedelta(seconds=0 if backoff else 120)
    monkeypatch.setattr("src.orchestration.developer_recovery.utcnow", lambda: observed)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-signing-key")
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        execution.handoff_receipt_ref = None
        claim = await db.get(OrchestrationWorkClaim, execution.claim_id)
        # The exiting worker released its claim as failed, or the engine abandoned it:
        # the run stays on the row as its last owner and nothing holds it any more.
        claim.state, claim.release_reason, claim.claim_event_id, claim.active_run_id = "released", release_reason, cycle.root, None
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                created_at=datetime.now(UTC) - timedelta(minutes=2),
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                reason=json.dumps({"attempt": 1, "run_id": cycle.root, "dispatch_envelope": {"persona": persona}}),
            )
        )
        await db.commit()
        monkeypatch.setattr("src.orchestration.delivery_adoption.observe_historical_delivery", _none)
        monkeypatch.setattr("src.orchestration.results.run_result_for_assignment", _none)
        monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", _none)
        monkeypatch.setattr("src.orchestration.results.protected_terminal_outcome", _none)
        outcome = await observe_results(db, run_store=_NoRunRecord())
        assert outcome.advanced == 1, outcome.reasons
        await db.commit()
        failure = (
            await db.scalars(
                select(OrchestrationDecision).where(OrchestrationDecision.node_id == cycle.node.id, OrchestrationDecision.to_state == "failed")
            )
        ).first()
        payload = json.loads(failure.reason)
        assert payload["status_source"] == "claim_released" and payload["claim_release_reason"] == release_reason


async def _none(*args, **kwargs):
    return None


@pytest.mark.parametrize("release_reason", ["failed", "abandoned"])
async def test_claim_released_settlement_schedules_the_developer_retry(cycle, monkeypatch, release_reason):  # noqa: F811
    await settled_by_release(cycle, monkeypatch, release_reason=release_reason)
    async with cycle.factory() as db:
        assert await recover_failed_developers(db) == 1
        await db.commit()
        node = await db.get(cycle.node.__class__, cycle.node.id)
        assert node.state == "ready"
        scheduled = (
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == cycle.node.id, OrchestrationDecision.kind == "developer_retry_scheduled"
                )
            )
        ).one()
        data = json.loads(scheduled.reason)
        assert data["reason"] == "claim_released_retry" and data["previous_run_id"] == cycle.root and data["preserve_existing_work"] is True


async def test_claim_released_retry_respects_backoff(cycle, monkeypatch):  # noqa: F811
    await settled_by_release(cycle, monkeypatch, backoff=True)
    async with cycle.factory() as db:
        node = await db.get(cycle.node.__class__, cycle.node.id)
        assert await recover_developer(db, node, now=datetime.now(UTC)) == "retry_backoff"


async def test_claim_released_retry_requires_a_developer_persona(cycle, monkeypatch):  # noqa: F811
    await settled_by_release(cycle, monkeypatch, persona="operations")
    async with cycle.factory() as db:
        node = await db.get(cycle.node.__class__, cycle.node.id)
        assert await recover_developer(db, node, now=datetime.now(UTC) + timedelta(seconds=120)) == "not_a_failed_developer"


async def test_claim_released_retry_requires_the_release_to_still_name_the_run(cycle, monkeypatch):  # noqa: F811
    await settled_by_release(cycle, monkeypatch)
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        claim = await db.get(OrchestrationWorkClaim, execution.claim_id)
        claim.claim_event_id = "orch:someone-else"
        await db.commit()
        node = await db.get(cycle.node.__class__, cycle.node.id)
        assert await recover_developer(db, node, now=datetime.now(UTC) + timedelta(seconds=120)) == "work_owner_changed"
