"""Historical owner acceptance is explicit provenance, not synthetic engine work."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationPullRequestBinding,
)
from src.orchestration.repository_evaluation_contract import Predecessor
from tests.orchestration.test_repository_evaluation import repository_evaluation, run  # noqa: F401
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


def test_existing_predecessor_contract_hash_input_is_unchanged():
    original = {"address": "flow/epic/wave/story", "required_checks": [{"name": "Tests", "app_id": 15368}]}
    assert Predecessor.model_validate(original).model_dump(mode="json") == original


async def owner_acceptance(ctx, case=None):
    async with ctx.factory() as db:
        execution = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.node.id))
        execution.phase = execution.status = "concluded"
        execution.pending_action_key = None
        await db.execute(
            delete(OrchestrationAction).where(
                OrchestrationAction.execution_id.in_(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == ctx.node.id)),
                OrchestrationAction.kind == "merge_pull_request",
            )
        )
        decision = OrchestrationDecision(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            node_id=ctx.node.id,
            actor_id="human",
            actor_kind="human",
            actor_role="platform_admin",
            kind="result_observed",
            from_state="failed",
            to_state="passed",
            reason=json.dumps(
                dict(
                    mode="owner_acceptance_of_existing_merge",
                    repo=ctx.binding.repo,
                    pr_number=ctx.binding.pr_number,
                    head_sha=ctx.head,
                    merge_sha="c" * 40,
                )
            ),
        )
        if case in {"worker", "foreign_node", "foreign_flow", "foreign_org", "wrong_role", "not_passed"}:
            if case == "foreign_flow":
                db.add(OrchestrationFlow(id="other", org_id=ctx.node.org_id, slug="other", title="Other flow"))
                await db.flush()
            field, value = {
                "worker": ("actor_kind", "service"),
                "foreign_node": ("node_id", ctx.eval_id),
                "foreign_flow": ("flow_id", "other"),
                "foreign_org": ("org_id", "other"),
                "wrong_role": ("actor_role", "user"),
                "not_passed": ("to_state", "failed"),
            }[case]
            setattr(decision, field, value)
        elif case == "old_attempt":
            decision.created_at = datetime.now(UTC) - timedelta(days=1)
        elif case == "live_execution":
            execution = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.node.id))
            execution.status = "awaiting_external"
        elif case in {"wrong_mode", "changed_head", "changed_pr", "changed_repo", "bad_sha"}:
            data = json.loads(decision.reason)
            key, value = {
                "wrong_mode": ("mode", "worker_claim"),
                "changed_head": ("head_sha", "d" * 40),
                "changed_pr": ("pr_number", 999),
                "changed_repo": ("repo", "other/repo"),
                "bad_sha": ("merge_sha", "bad"),
            }[case]
            data[key] = value
            decision.reason = json.dumps(data)
        db.add(decision)
        await db.flush()
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        if case != "unbound":
            document["nodes"][-1]["evaluation"]["predecessors"][0]["accepted_merge_decision_id"] = decision.id
        plan.plan_document = document
        await db.commit()
        return decision.id


async def test_owner_acceptance_preserves_provenance_and_observes_evidence(repository_evaluation):  # noqa: F811
    ctx = repository_evaluation
    identity = await owner_acceptance(ctx)
    state, attempts, decisions = await run(ctx)
    assert (state, attempts) == ("passed", 1)
    source = json.loads(decisions[-1].reason)["receipt"]["pull_requests"][0]
    assert source["accepted_merge_decision_id"] == identity
    assert source["merge_operation_key"] is None and source["review_ref"] is None
    assert source["head_sha"] == ctx.head and source["merge_sha"] == "c" * 40
    ctx.provider.observe.assert_awaited_once()
    ctx.claim_spy.assert_not_awaited()


@pytest.mark.parametrize(
    "case",
    [
        "unbound",
        "worker",
        "foreign_node",
        "foreign_flow",
        "foreign_org",
        "wrong_role",
        "not_passed",
        "wrong_mode",
        "changed_head",
        "changed_pr",
        "changed_repo",
        "bad_sha",
        "old_attempt",
        "live_execution",
    ],
)
async def test_owner_acceptance_refusals_preserve_block(repository_evaluation, case):  # noqa: F811
    ctx = repository_evaluation
    await owner_acceptance(ctx, case)
    state, attempts, _ = await run(ctx)
    assert (state, attempts) == ("ready", 0)
    ctx.provider.observe.assert_not_awaited()


async def test_owner_acceptance_changed_during_observation_is_not_settled(repository_evaluation):  # noqa: F811
    ctx = repository_evaluation
    await owner_acceptance(ctx)
    observe = ctx.provider.observe.side_effect

    async def changed(binding, spec, sources):
        async with ctx.factory() as db:
            record = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
            record.revision += 1
            await db.commit()
        return await observe(binding, spec, sources)

    ctx.provider.observe.side_effect = changed
    assert (await run(ctx))[:2] == ("ready", 0)


async def test_acceptance_by_another_principal_is_refused(repository_evaluation, monkeypatch):  # noqa: F811
    ctx = repository_evaluation
    await owner_acceptance(ctx)
    monkeypatch.setattr("src.orchestration.accepted_merge_evidence.policy_owner_matches", AsyncMock(return_value=False))
    assert (await run(ctx))[:2] == ("ready", 0)
    ctx.provider.observe.assert_not_awaited()
