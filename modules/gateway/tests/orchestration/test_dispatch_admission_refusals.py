"""Actual shared dispatch persists refusals after rolling back unused ownership."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration import shared_policy
from src.orchestration.delivery_progress import node_progress
from src.orchestration.dispatch_pass import run_dispatch_pass
from src.orchestration.models import (
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.base import Base
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.mark.parametrize("reason", ["budget_unavailable", "held_by_other_owner"])
async def test_shared_refusal_survives_rollback_without_admission_side_effects(shared, monkeypatch, reason):  # noqa: F811
    ctx = shared
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    monkeypatch.setenv("ADP_SHARED_RUN_REPORTING_ENABLED", "true")
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=123))
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
    monkeypatch.setattr("src.shared.identity.resolver.resolve_root_user_entity_id", AsyncMock(return_value="human"))
    meter = shared_policy.read_flow_meter
    if reason == "budget_unavailable":
        monkeypatch.setattr(shared_policy, "read_flow_meter", AsyncMock(return_value=None))
    async with ctx.factory.kw["bind"].begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[OrchestrationEdge.__table__]))
    async with ctx.factory() as db:
        fresh = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.flow.id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="N2",
            kind="story",
            state="ready",
            title="Undispatched story",
            issue_ref="44",
            attempts=0,
        )
        db.add(fresh)
        competitor = None
        if reason == "held_by_other_owner":
            competitor = OrchestrationWorkClaim(
                org_id=ctx.node.org_id,
                provider_repository_id=123,
                issue_number=44,
                owner_kind="engine_flow",
                owner_ref="other-owner",
                active_run_id="other-run",
                state="held",
                generation=1,
            )
            db.add(competitor)
        await db.commit()
        node_id = fresh.id
    async with ctx.factory() as db:
        report = await run_dispatch_pass(db, config=ctx.service.config)
        assert report.pending == [] and report.dispatched == 0 and report.admission_refused == 1 and report.errors == 0
        await db.commit()
    # A new database session proves the diagnostic survived both savepoints.
    async with ctx.factory() as db:
        fresh = await db.get(OrchestrationNode, node_id)
        assert (fresh.state, fresh.attempts) == ("ready", 0)
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node_id)))
        assert len(decisions) == 1 and decisions[0].kind == "transition_rejected" and decisions[0].to_state is None
        detail = json.loads(decisions[0].rejection_reason)
        assert detail["block_code"] == reason and detail["attempt"] == 0 and detail["accepted_plan_version"] == 1
        assert detail["owner"] and detail["required_input"] and detail["detail"]
        assert await db.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node_id)) is None
        assert await db.scalar(select(OrchestrationRunReport.run_id).where(OrchestrationRunReport.node_id == node_id)) is None
        claims = list(await db.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.issue_number == 44)))
        assert [claim.id for claim in claims] == ([competitor.id] if competitor else [])
        if competitor:
            assert (claims[0].owner_ref, claims[0].active_run_id, claims[0].generation) == ("other-owner", "other-run", 1)
        progress = node_progress(
            node=fresh,
            binding=None,
            dispatch={},
            result={},
            policy_enabled=True,
            plan_version=1,
            policy_hash=ctx.policy.policy_hash,
            admission_refusal=detail,
        )
        assert progress.stage == "admission" and progress.blocker == reason
        assert progress.actor == ("platform-operator" if reason == "budget_unavailable" else "engine")
        assert progress.next_action and progress.next_check_at is None and progress.scheduled_action is None
    if reason == "budget_unavailable":
        monkeypatch.setattr(shared_policy, "read_flow_meter", meter)
        async with ctx.factory() as db:
            report = await run_dispatch_pass(db, config=ctx.service.config)
            assert report.dispatched == 1 and len(report.pending) == 1
            await db.commit()
            fresh = await db.get(OrchestrationNode, node_id)
            assert (fresh.state, fresh.attempts) == ("running", 1)
            progress = node_progress(
                node=fresh,
                binding=None,
                dispatch={},
                result={},
                policy_enabled=True,
                plan_version=1,
                policy_hash=ctx.policy.policy_hash,
                admission_refusal=detail,
            )
            assert progress.stage == "development" and progress.blocker is None
