"""Settled development failures retry once per authorized attempt, never live work."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.developer_recovery import recover_failed_developers, retry_context
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.results import observe_results
from src.orchestration.run_reports import prepare_run_report, record_terminal
from src.orchestration.state import ActorKind, NodeState, transition
from tests.orchestration.test_review_cycle import ORG, REPO, cycle, pg_server, pg_url, store  # noqa: F401


async def settled(cycle, monkeypatch, failure=None, backoff=False):  # noqa: F811
    observed = datetime.now(UTC) + timedelta(seconds=0 if backoff else 120)
    monkeypatch.setattr("src.orchestration.developer_recovery.utcnow", lambda: observed)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-signing-key")
    async with cycle.factory() as db:
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
                reason=json.dumps({"attempt": 1, "run_id": cycle.root}),
            )
        )
        row = await prepare_run_report(
            db,
            {
                "message_id": cycle.root,
                "tenant_id": ORG,
                "persona": "developer",
                "source_ref": {"repo": REPO, "installation_id": 42, "provider_repository_id": 123},
                "orchestration": {"node_id": cycle.node.id, "flow_id": cycle.flow.id, "attempt": 1},
                "pr_binding_required": True,
            },
        )
        record_terminal(row, "failed", failure=failure)
        await db.commit()
        outcome = await observe_results(db)
        assert outcome.advanced == 1, outcome
        await db.commit()


@pytest.mark.parametrize(
    "block",
    [
        None,
        "paused",
        "exhausted",
        "expired",
        "deadline",
        "human_gate",
        "no_repair",
        "new_plan",
        "claim_changed",
        "successor",
        "uncertain_effect",
        "live_execution",
        "backoff",
        "human_failure",
        "policy_failure",
        "cancelled_failure",
    ],
)
async def test_recovery_respects_settlement_policy_and_existing_owner(cycle, monkeypatch, block):  # noqa: F811
    await settled(
        cycle,
        monkeypatch,
        {"category": "policy" if block == "policy_failure" else "cancelled"} if block in {"policy_failure", "cancelled_failure"} else None,
        backoff=block == "backoff",
    )
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        if block == "paused":
            (await db.get(OrchestrationFlow, cycle.flow.id)).execution_paused = True
        if block in {"exhausted", "expired", "deadline", "human_gate", "no_repair", "new_plan"}:
            plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == cycle.flow.id))
            document = json.loads(json.dumps(plan.plan_document))
            policy = document["execution_policy"]
            if block == "exhausted":
                policy["limits"]["max_attempts_per_node"] = 1
            if block == "expired":
                policy["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            if block == "deadline":
                policy["limits"]["max_wall_clock_seconds"] = 1
            if block == "human_gate":
                policy["human_gates"] = ["repair"]
            if block == "no_repair":
                policy["allowed_actions"].remove("repair")
            if block == "new_plan":
                plan.version += 1
            plan.plan_document = document
        if block == "claim_changed":
            (await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)).generation += 1
        if block in {"successor", "uncertain_effect"}:
            db.add(
                OrchestrationAction(
                    org_id=ORG,
                    execution_id=cycle.execution.id,
                    operation_key="other-work",
                    kind="review_cycle_dispatch" if block == "successor" else "open_pr",
                    status="succeeded" if block == "successor" else "unknown",
                    attempt=1,
                )
            )
        if block == "live_execution":
            (await db.get(OrchestrationExecution, cycle.execution.id)).status = "runnable"
        if block == "human_failure":
            db.add(
                OrchestrationDecision(
                    org_id=ORG,
                    flow_id=cycle.flow.id,
                    node_id=node.id,
                    kind="result_observed",
                    actor_id="human",
                    actor_kind="human",
                    actor_role="owner",
                    to_state="failed",
                    reason="operator intervention",
                )
            )
        await db.commit()
        recovered = await recover_failed_developers(db)
        assert recovered == int(block is None), [
            r.reason for r in (await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "developer_retry_checked"))).all()
        ]
        assert node.state == ("ready" if block is None else "failed")
        assert node.attempts == 1
        context = await retry_context(db, node, 1)
        if block is None:
            assert context["previous_run_id"] == cycle.root
            assert context["preserve_existing_work"] is True
        else:
            assert context is None
        await db.commit()
        assert await recover_failed_developers(db) == 0


async def test_concurrent_ticks_schedule_only_one_recovery(cycle, monkeypatch):  # noqa: F811
    await settled(cycle, monkeypatch)

    async def tick():
        async with cycle.factory() as db:
            count = await recover_failed_developers(db)
            await db.commit()
            return count

    assert sum(await asyncio.gather(tick(), tick())) == 1


@pytest.mark.parametrize("source", list(NodeState))
def test_retry_proof_never_clears_other_states(source):
    result = transition(source, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="retry", developer_retry_authorized=True)
    assert result.allowed == (source in {NodeState.PENDING, NodeState.FAILED})
    if source == NodeState.FAILED:
        assert not transition(source, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="unverified").allowed


async def test_disabled_engine_does_not_recover(cycle, monkeypatch):  # noqa: F811
    await settled(cycle, monkeypatch)
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "false")
    async with cycle.factory() as db:
        assert await recover_failed_developers(db) == 0
        assert (await db.get(OrchestrationNode, cycle.node.id)).state == "failed"


@pytest.mark.parametrize(
    "outcome,persona",
    [("failed", "developer"), ("aborted", "developer"), ("cancelled", "developer"), ("budget_stopped", "developer"), ("failed", "reviewer")],
)
async def test_protected_recovery_requires_developer_failure(cycle, monkeypatch, outcome, persona):  # noqa: F811
    from types import SimpleNamespace
    from unittest.mock import Mock

    from sqlalchemy import delete

    from src.orchestration.run_reports import OrchestrationRunReport

    await settled(cycle, monkeypatch)
    async with cycle.factory() as db:
        await db.execute(delete(OrchestrationRunReport).where(OrchestrationRunReport.run_id == cycle.root))
        await db.commit()
        protected = Mock()
        protected._read.return_value = {
            "status": {"S": "completed"},
            "terminal_outcome": {"S": outcome},
            "persona": {"S": persona},
            "tenant_id": {"S": ORG},
            "invocation_id": {"S": cycle.root},
            "flow_id": {"S": cycle.flow.id},
            "orchestration_node_id": {"S": cycle.node.id},
            "orchestration_node_attempt": {"N": "1"},
        }
        monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: SimpleNamespace(store=protected))
        assert await recover_failed_developers(db) == int(outcome == "failed" and persona == "developer")
