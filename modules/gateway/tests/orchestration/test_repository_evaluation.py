"""Repository observers consume real merge actions without inventing worker runs."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.dispatch import graph_address
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationEdge, OrchestrationNode
from src.orchestration.repository_evaluation import observe_repository_evaluation
from src.orchestration.repository_evaluation_contract import harness_digest
from tests.orchestration import test_merge_controller as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.fixture
async def repository_evaluation(shared, monkeypatch):  # noqa: F811
    async with protocol.prepared_merge(shared, monkeypatch) as ctx:
        ctx.remote["rules"], ctx.remote["protection"], ctx.remote["reviews"] = [], None, []
        ctx.remote["graphql"]["data"]["repository"]["pullRequest"]["reviewDecision"] = None
        await protocol.tick(ctx)
        await protocol.tick(ctx)
        async with ctx.factory() as db:
            connection = await db.connection()
            await connection.run_sync(lambda sync: OrchestrationEdge.__table__.create(sync, checkfirst=True))
            parent = await db.get(OrchestrationNode, ctx.node.id)
            assert parent.state == "passed"
            address = graph_address(parent, flow_slug="cycle")
            ctx.spec = dict(
                evidence_schema="repository-evaluation/v1",
                runner=dict(adapter="engine-repository-evidence-v1", repository=ctx.binding.repo, repository_id=123, harness_sha256=harness_digest()),
                predecessors=[dict(address=address, required_checks=[dict(name="Unit tests", app_id=15368)])],
            )
            node = OrchestrationNode(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                epic_ref="E1",
                wave_ref="W1",
                node_ref="REPOEVAL",
                kind="eval",
                title="Verify repository evidence",
                issue_ref="999",
                state="ready",
                attempts=0,
            )
            db.add(node)
            await db.flush()
            db.add(OrchestrationEdge(org_id=parent.org_id, flow_id=parent.flow_id, from_node_id=parent.id, to_node_id=node.id))
            plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
            document = deepcopy(plan.plan_document)
            policy = {**document["execution_policy"]}
            policy["allowed_actions"] = [*policy["allowed_actions"], "evaluate"]
            policy["evaluation_acceptance"] = {graph_address(node, flow_slug="cycle"): "machine"}
            document["execution_policy"] = policy
            document["nodes"] = [
                *document.get("nodes", []),
                dict(
                    address=graph_address(node, flow_slug="cycle"),
                    kind=node.kind,
                    title=node.title,
                    issue_ref=node.issue_ref,
                    evaluation=ctx.spec,
                ),
            ]
            plan.plan_document = document
            await db.commit()
            ctx.eval_id = node.id
        ctx.provider = SimpleNamespace(token=AsyncMock(return_value="test-scoped-token"), observe=AsyncMock())

        async def observe(binding, spec, sources):
            return dict(
                pull_requests=[
                    dict(
                        **{key: value for key, value in source.items() if key not in {"required_checks", "provider_pr_node_id"}},
                        provider_pr_node_id=source.get("provider_pr_node_id", "PR_2"),
                        merged_at=datetime.now(UTC).isoformat(),
                        checks=[dict(**item, check_run_id=7, head_sha=source["head_sha"]) for item in source["required_checks"]],
                    )
                    for source in sources
                ],
                workflows=[],
                mandatory_passed=True,
            )

        ctx.provider.observe.side_effect = observe
        monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
        ctx.claim_spy = AsyncMock(side_effect=AssertionError("evidence reads must not claim an issue"))
        monkeypatch.setattr("src.orchestration.work_admission.admit", ctx.claim_spy)
        yield ctx


async def run(ctx):
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert await observe_repository_evaluation(db, node, provider=ctx.provider)
        await db.commit()
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
        return node.state, node.attempts, decisions


async def test_verified_repository_evidence_passes_through_engine_without_worker_or_claim(repository_evaluation):
    ctx = repository_evaluation
    state, attempts, decisions = await run(ctx)
    assert state == "passed" and attempts == 1
    assert len(decisions) == 1 and decisions[0].actor_id == "system:repository-evaluation"
    import json

    receipt = json.loads(decisions[0].reason)["receipt"]
    assert receipt["accepted_plan_version"] == 1 and receipt["mandatory_passed"] is True
    assert receipt["live_attestation"] is False
    source = receipt["pull_requests"][0]
    assert source["head_sha"] == ctx.head and source["merge_sha"] == "c" * 40
    assert source["review_ref"] and source["merge_operation_key"] and source["execution_id"]
    ctx.claim_spy.assert_not_awaited()


async def test_native_eval_becomes_ready_from_merged_code_without_deployment(repository_evaluation):
    from src.orchestration.tick import run_tick

    ctx = repository_evaluation
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.state = "pending"
        await db.commit()
        report = await run_tick(db)
        assert report.success and report.errors == 0 and ctx.eval_id not in report.blocked
        assert node.state == "ready"
    ctx.claim_spy.assert_not_awaited()


@pytest.mark.parametrize("case", ["no_authority", "changed_harness", "missing_parent", "changed_source", "failed_criterion"])
async def test_repository_refusals_never_pass_or_create_worker(repository_evaluation, case):
    ctx = repository_evaluation
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        if case == "no_authority":
            document["execution_policy"] = {**document["execution_policy"], "allowed_actions": ["develop", "review", "repair", "merge"]}
        elif case == "changed_harness":
            document["nodes"][-1]["evaluation"]["runner"]["harness_sha256"] = "0" * 64
        elif case == "missing_parent":
            node = await db.get(OrchestrationNode, ctx.node.id)
            node.state = "running"
        plan.plan_document = document
        await db.commit()
    if case == "failed_criterion":
        ctx.provider.observe.side_effect = None
        ctx.provider.observe.return_value = dict(pull_requests=[], workflows=[], mandatory_passed=False)
    elif case == "changed_source":
        original = ctx.provider.observe.side_effect

        async def changed(binding, spec, sources):
            from src.orchestration.models import OrchestrationPullRequestBinding

            async with ctx.factory() as db:
                row = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
                row.revision += 1
                await db.commit()
            return await original(binding, spec, sources)

        ctx.provider.observe.side_effect = changed
    state, attempts, decisions = await run(ctx)
    assert state == "ready" and attempts == 0 and decisions
    ctx.claim_spy.assert_not_awaited()
    if case in {"no_authority", "changed_harness", "missing_parent"}:
        ctx.provider.observe.assert_not_awaited()


async def test_external_pr_evidence_never_takes_the_foreign_issue_lane(repository_evaluation):
    ctx = repository_evaluation
    from sqlalchemy import delete

    async with ctx.factory() as db:
        await db.execute(delete(OrchestrationEdge).where(OrchestrationEdge.to_node_id == ctx.eval_id))
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        spec = {
            **document["nodes"][-1]["evaluation"],
            "predecessors": [],
            "external_pull_requests": [
                dict(
                    criterion_id="external-issue",
                    issue_number=5329,
                    pr_number=123,
                    head_sha="a" * 40,
                    merge_sha="b" * 40,
                    required_checks=[dict(name="Unit tests", app_id=15368)],
                )
            ],
        }
        document["nodes"][-1]["evaluation"] = spec
        plan.plan_document = document
        await db.commit()
    state, _, _ = await run(ctx)
    assert state == "passed"
    ctx.claim_spy.assert_not_awaited()
    assert ctx.provider.observe.call_args.args[2][0]["issue_number"] == 5329


async def test_slow_evaluations_share_one_small_budget_after_worker_admission(repository_evaluation, monkeypatch):
    import asyncio
    import time

    from src.orchestration import dispatch_pass

    ctx = repository_evaluation
    monkeypatch.setattr("src.orchestration.repository_evaluation.OBSERVATION_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("src.orchestration.repository_evaluation.RepositoryEvidenceProvider", lambda: ctx.provider)
    monkeypatch.setattr("src.orchestration.report_dispatch.recover_pending_reports", AsyncMock())

    async def slow_token(binding):
        await asyncio.Future()

    ctx.provider.token.side_effect = slow_token
    admitted = []
    original = dispatch_pass._dispatch_one

    async def dispatch(session, node, *, config, report):
        if node.kind == "story":
            # Routing/admission has its own integration suite. This test proves
            # the scheduler reaches an eligible worker before any slow observer.
            assert ctx.provider.token.await_count == 0
            admitted.append(node.id)
            node.state = "running"
            report.record(node.org_id, "dispatched")
        else:
            await original(session, node, config=config, report=report)

    monkeypatch.setattr(dispatch_pass, "_dispatch_one", dispatch)
    async with ctx.factory() as db:
        first = await db.get(OrchestrationNode, ctx.eval_id)
        second = OrchestrationNode(
            org_id=first.org_id,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="REPOEVAL2",
            kind="eval",
            title="Second evidence read",
            issue_ref="998",
            state="ready",
            attempts=0,
        )
        story = OrchestrationNode(
            org_id=first.org_id,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="NEWSTORY",
            kind="story",
            title="Eligible coding work",
            issue_ref="997",
            state="ready",
            attempts=0,
        )
        db.add_all([second, story])
        await db.flush()
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"].append(
            dict(
                address=graph_address(second, flow_slug="cycle"),
                kind="eval",
                title=second.title,
                issue_ref=second.issue_ref,
                evaluation=ctx.spec,
            )
        )
        document["execution_policy"]["evaluation_acceptance"][graph_address(second, flow_slug="cycle")] = "machine"
        plan.plan_document = document
        await db.commit()
        started = time.monotonic()
        report = await dispatch_pass.run_dispatch_pass(db, dispatch_pass.DispatchPassConfig(queue_url="queue", repo=ctx.binding.repo))
        assert time.monotonic() - started < 0.5
        assert admitted == [story.id] and report.dispatched == 1 and report.errors == 0
        assert ctx.provider.token.await_count == 1
        ctx.provider.observe.assert_not_awaited()
        await db.commit()
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.actor_id == "system:repository-evaluation")))
        assert len(decisions) == 1 and "repository_evaluation_time_budget" in decisions[0].reason
        retry_order = await dispatch_pass._fetch_ready_nodes(db, limit=10)
        assert retry_order[0].id != decisions[0].node_id
