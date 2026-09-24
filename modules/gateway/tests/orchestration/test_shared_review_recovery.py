"""Recovery of positively exited reviews keeps claims, attempts and old evidence."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.models import OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import ReviewRecoveryRequest, request_review_recovery
from src.orchestration.run_reports import OrchestrationRunReport, RunReportError
from src.orchestration.shared_cycle import validate_current_report_assignment
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401
from tests.orchestration.test_shared_review import document_for


@pytest.fixture
async def recovery(shared, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    assert (await protocol.tick(shared)).effects_succeeded == 1
    envelope = shared.calls[-1]
    run = envelope["message_id"]
    resolver = SimpleNamespace(
        read_current=AsyncMock(return_value={"tenant_id": protocol.ORG, "status": "failed", "arrived_at": "2026-01-01T00:00:00Z"})
    )
    monkeypatch.setattr("src.orchestration.controls.get_run_binding_resolver", AsyncMock(return_value=resolver))
    monkeypatch.setattr(
        "src.orchestration.pr_identity.resolve_pr_identity",
        AsyncMock(
            side_effect=lambda **kw: SimpleNamespace(
                head_sha=shared.head,
                provider_repository_id=shared.binding.provider_repository_id,
                provider_pr_node_id=shared.binding.provider_pr_node_id,
            )
        ),
    )
    async with shared.factory() as db:
        row = await db.get(OrchestrationRunReport, run)
        row.worker_receipt = {"recorded_at": datetime.now(UTC).isoformat()}
        row.block_code = "stale_head"
        row.review_receipt = None
        await db.commit()
    body = ReviewRecoveryRequest(
        expected_attempt=1,
        expected_plan_version=1,
        expected_run_id=run,
        expected_head_sha=shared.head,
        reason="Continue the retained story with a fresh Codex review after verified worker exit.",
    )
    return SimpleNamespace(ctx=shared, body=body, resolver=resolver, run=run)


async def request(ctx, *, accept=False, role="owner"):
    async with ctx.ctx.factory() as db:
        result = await request_review_recovery(
            db, org_id=protocol.ORG, node_id=ctx.ctx.node.id, actor_id="human", actor_role=role, request=ctx.body, accept=accept
        )
        if accept:
            await db.commit()
        return result


@pytest.mark.parametrize("failed_node", [False, True])
@pytest.mark.parametrize("failed_terminal", [False, True])
async def test_fresh_review_recovers_without_fabricating_old_receipts(recovery, monkeypatch, failed_node, failed_terminal):
    ctx = recovery.ctx
    prior_terminal = {"outcome": "failed"} if failed_terminal else None
    if failed_terminal:
        async with ctx.factory() as db:
            (await db.get(OrchestrationRunReport, recovery.run)).terminal_receipt = prior_terminal
            await db.commit()
        assert (await protocol.tick(ctx)).blocked == 1
    if failed_node:
        async with ctx.factory() as db:
            (await db.get(OrchestrationNode, ctx.node.id)).state = "failed"
            await db.commit()
    before = await protocol.state(ctx)
    preview = await request(recovery)
    assert (await protocol.state(ctx))[2].state == before[2].state
    recovery.body.expected_snapshot = preview["snapshot"]
    accepted = await request(recovery, accept=True)
    assert accepted["created"]
    again = await request(recovery, accept=True)
    assert not again["created"] and again["decision_id"] == accepted["decision_id"]
    assert (await protocol.state(ctx))[0].attempts == before[0].attempts
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    envelope = ctx.calls[-1]
    assert envelope["message_id"] != recovery.run and envelope["persona"] == "agent-codex-reviewer"
    after = await protocol.state(ctx)
    assert after[1].generation == before[1].generation and after[2].attempts == before[2].attempts
    async with ctx.factory() as db:
        old = await db.get(OrchestrationRunReport, recovery.run)
        assert old.terminal_receipt == prior_terminal and old.review_receipt is None and old.block_code == "stale_head"
        with pytest.raises(RunReportError, match="superseded"):
            await validate_current_report_assignment(db, old)
    # Fresh authenticated R1, not the recovery decision, makes the PR merge-ready.
    from src.orchestration.shared_review import record_shared_review

    monkeypatch.setenv("AGENT_RUN_LOGS_BUCKET", "review-test")
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_head_check_runs", AsyncMock(return_value=frozenset({"check-run:105036077448"})))
    async with ctx.factory() as db:
        receipt = await record_shared_review(
            db,
            credential=envelope["run_report"]["credential"],
            content=json.dumps(document_for(ctx, envelope)),
            storage=SimpleNamespace(put_object=lambda **kw: None),
        )
        assert receipt["recorded"], receipt
        await db.commit()
    await ctx.finish(envelope["message_id"])
    await protocol.tick(ctx)
    assert (await protocol.state(ctx))[0].phase == "merge_ready", vars((await protocol.state(ctx))[0])
    assert len(ctx.calls) == 2
    from tests.orchestration.test_shared_initial_dispatch import ACTUAL_ACTIVE_COUNT

    async with ctx.factory() as db:
        assert await ACTUAL_ACTIVE_COUNT(db, org_id=ctx.node.org_id, flow_id=ctx.flow.id) == 0


@pytest.mark.parametrize("status", [None, "in_progress", "pending", "unknown"])
async def test_missing_or_live_worker_cannot_be_displaced(recovery, status):
    recovery.resolver.read_current.return_value["status"] = status
    with pytest.raises(CycleBlockedError, match="prior_worker_active_or_unverified"):
        await request(recovery)
    assert len(recovery.ctx.calls) == 1


@pytest.mark.parametrize("change", ["head", "attempt", "plan", "claim", "receipt", "role"])
async def test_changed_recovery_preview_is_refused(recovery, change):
    recovery.body.expected_snapshot = (await request(recovery))["snapshot"]
    if change == "head":
        recovery.ctx.head = "b" * 40
    elif change == "attempt":
        recovery.body.expected_attempt = 2
    elif change == "plan":
        recovery.body.expected_plan_version = 2
    elif change in {"claim", "receipt"}:
        async with recovery.ctx.factory() as db:
            if change == "claim":
                (await db.get(OrchestrationWorkClaim, recovery.ctx.claim.id)).active_run_id = "other"
            else:
                (await db.get(OrchestrationRunReport, recovery.run)).terminal_receipt = {"outcome": "failed"}
            await db.commit()
    with pytest.raises((CycleBlockedError, RunReportError)):
        await request(recovery, accept=True, role="developer" if change == "role" else "owner")


@pytest.mark.parametrize("change", ["actor", "head", "receipt", "missing_exit", "live_exit"])
async def test_dispatch_revalidates_owner_decision_and_current_evidence(recovery, change):
    recovery.body.expected_snapshot = (await request(recovery))["snapshot"]
    accepted = await request(recovery, accept=True)
    if change == "head":
        recovery.ctx.head = "b" * 40
    else:
        async with recovery.ctx.factory() as db:
            if change == "actor":
                original = await db.get(OrchestrationDecision, accepted["decision_id"])
                db.add(
                    OrchestrationDecision(
                        org_id=original.org_id,
                        flow_id=original.flow_id,
                        node_id=original.node_id,
                        kind=original.kind,
                        actor_id="engine",
                        actor_kind="service",
                        actor_role="service",
                        reason=original.reason,
                    )
                )
            elif change == "receipt":
                (await db.get(OrchestrationRunReport, recovery.run)).terminal_receipt = {"outcome": "complete"}
            else:
                decision = await db.get(OrchestrationDecision, accepted["decision_id"])
                data = json.loads(decision.reason)
                data["worker_exit"] = None if change == "missing_exit" else {"status": "in_progress"}
                db.add(
                    OrchestrationDecision(
                        org_id=decision.org_id,
                        flow_id=decision.flow_id,
                        node_id=decision.node_id,
                        kind=decision.kind,
                        actor_id=decision.actor_id,
                        actor_kind=decision.actor_kind,
                        actor_role=decision.actor_role,
                        reason=json.dumps(data),
                    )
                )
            await db.commit()
    assert (await protocol.tick(recovery.ctx)).blocked == 1
    assert len(recovery.ctx.calls) == 1


async def test_accepted_exit_does_not_require_registry_access_by_scheduler(recovery):
    recovery.body.expected_snapshot = (await request(recovery))["snapshot"]
    await request(recovery, accept=True)
    recovery.resolver.read_current.reset_mock()
    recovery.resolver.read_current.side_effect = PermissionError("scheduler has no registry query grant")
    assert (await protocol.tick(recovery.ctx)).effects_succeeded == 1
    recovery.resolver.read_current.assert_not_called()
    assert recovery.ctx.calls[-1]["persona"] == "agent-codex-reviewer"
    assert recovery.ctx.calls[-1]["message_id"] != recovery.run


async def test_stale_cached_exit_cannot_authorize_a_live_worker(recovery):
    recovery.resolver.resolve = AsyncMock(return_value={"tenant_id": protocol.ORG, "status": "complete"})
    recovery.resolver.read_current.return_value["status"] = "in_progress"
    with pytest.raises(CycleBlockedError, match="prior_worker_active_or_unverified"):
        await request(recovery)
    recovery.resolver.resolve.assert_not_called()


async def mark_stalled(ctx):
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        node.state = "failed"
        db.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="node_stalled",
                actor_kind="service",
                actor_id="system:orchestration-stall-detector",
                actor_role="engine",
                from_state="running",
                to_state="failed",
                reason="worker timeout",
            )
        )
        await db.commit()


async def test_policy_recovers_stalled_review_through_existing_runner_once(recovery):
    from src.orchestration.review_recovery import recover_stalled_stories

    ctx = recovery.ctx
    await mark_stalled(ctx)
    count = await recover_stalled_stories(ctx.factory, resolver=recovery.resolver)
    async with ctx.factory() as db:
        from sqlalchemy import select

        reasons = list((await db.scalars(select(OrchestrationDecision.reason).where(OrchestrationDecision.kind == "stalled_review_blocked"))).all())
    assert count == 1, reasons
    assert await recover_stalled_stories(ctx.factory, resolver=recovery.resolver) == 0
    async with ctx.factory() as db:
        prior = await db.get(OrchestrationRunReport, recovery.run)
        assert prior.terminal_receipt is None and prior.review_receipt is None
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    assert len(ctx.calls) == 2
    assert ctx.calls[-1]["persona"] == "agent-codex-reviewer"
    assert ctx.calls[-1]["review_cycle_input"]["recovery"]["source"] == "existing_pr"
    assert ctx.calls[-1]["review_cycle_input"]["recovery"]["prior_run_id"] == recovery.run
    execution, claim, node, _ = await protocol.state(ctx)
    assert execution.attempts == 2 and node.attempts == 1
    from src.orchestration.review_recovery import recovered_worker_exit

    async with ctx.factory() as db:
        prior = await db.get(OrchestrationRunReport, recovery.run)
        assert await recovered_worker_exit(db, prior, claim)


async def test_failed_reviewer_recovers_without_waiting_for_outer_node_stall(recovery):
    from src.orchestration.models import OrchestrationAcceptedPlan
    from src.orchestration.review_recovery import recover_stalled_stories

    ctx = recovery.ctx
    async with ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, recovery.run)
        assert row.dispatch_metadata["persona"] == "agent-codex-reviewer"
        row.terminal_receipt = {"outcome": "failed"}
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = 3
        plan.plan_document = document
        await db.commit()
    assert (await protocol.tick(ctx)).blocked == 1
    assert (await protocol.state(ctx))[2].state == "running"
    assert await recover_stalled_stories(ctx.factory, resolver=recovery.resolver) == 1
    assert await recover_stalled_stories(ctx.factory, resolver=recovery.resolver) == 0
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    assert len(ctx.calls) == 2
    assert ctx.calls[-1]["persona"] == "agent-codex-reviewer"
    assert ctx.calls[-1]["review_cycle_input"]["recovery"]["prior_run_id"] == recovery.run
    execution, claim, node, _ = await protocol.state(ctx)
    assert node.attempts == 1 and execution.attempts == 2
    async with ctx.factory() as db:
        prior = await db.get(OrchestrationRunReport, recovery.run)
        assert prior.terminal_receipt == {"outcome": "failed"} and prior.review_receipt is None
        latest = await db.get(OrchestrationRunReport, claim.active_run_id)
        latest.terminal_receipt = {"outcome": "failed"}
        await db.commit()
    # The developer and both reviewers exhaust the same original allowance.
    assert await recover_stalled_stories(ctx.factory, resolver=recovery.resolver) == 0
    assert len(ctx.calls) == 2


@pytest.mark.parametrize("hold", ["live", "cancelled", "complete", "missing_terminal", "verdict", "paused", "developer", "wrong_trigger"])
async def test_failed_reviewer_recovery_preserves_failure_and_authority_boundaries(recovery, hold):
    from src.orchestration.models import OrchestrationFlow
    from src.orchestration.review_recovery import recover_stalled_stories

    ctx = recovery.ctx
    async with ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, recovery.run)
        row.terminal_receipt = {"outcome": "complete" if hold == "complete" else "failed"}
        if hold == "missing_terminal":
            row.terminal_receipt = None
        if hold == "verdict":
            row.review_receipt = {"recorded": True}
        if hold == "paused":
            (await db.get(OrchestrationFlow, ctx.flow.id)).execution_paused = True
        if hold in {"developer", "wrong_trigger"}:
            metadata = json.loads(json.dumps(row.dispatch_metadata))
            if hold == "developer":
                metadata["persona"] = "developer"
            else:
                metadata["intent"] = {"trigger": "other"}
            row.dispatch_metadata = metadata
        await db.commit()
    if hold in {"live", "cancelled"}:
        recovery.resolver.read_current.return_value["status"] = "in_progress" if hold == "live" else "cancelled"
    assert await recover_stalled_stories(ctx.factory, resolver=recovery.resolver) == 0
    assert len(ctx.calls) == 1


@pytest.mark.parametrize("reason", ["pause", "not_stalled", "limit", "live", "explicit_blocker"])
async def test_automatic_recovery_preserves_holds(recovery, reason):
    from src.orchestration.models import OrchestrationExecution, OrchestrationFlow
    from src.orchestration.review_recovery import recover_stalled_stories

    ctx = recovery.ctx
    if reason != "not_stalled":
        await mark_stalled(ctx)
    async with ctx.factory() as db:
        (await db.get(OrchestrationNode, ctx.node.id)).state = "failed"
        if reason == "pause":
            (await db.get(OrchestrationFlow, ctx.flow.id)).execution_paused = True
        if reason == "limit":
            from sqlalchemy import select

            execution = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.node.id))
            execution.attempts = 8
        if reason == "explicit_blocker":
            (await db.get(OrchestrationRunReport, recovery.run)).review_receipt = {"recorded": True}
        await db.commit()
    if reason == "live":
        recovery.resolver.read_current.return_value["status"] = "in_progress"
        recovery.resolver.read_current.return_value["arrived_at"] = datetime.now(UTC).isoformat()
    assert await recover_stalled_stories(ctx.factory, resolver=recovery.resolver) == 0
    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.node.id)).state == "failed"
        assert (await db.get(OrchestrationWorkClaim, ctx.claim.id)).active_run_id == recovery.run
    assert len(ctx.calls) == 1


def test_service_recovery_transition_is_narrow_and_opt_in():
    from src.orchestration.state import ActorKind, NodeState, transition

    args = dict(actor_kind=ActorKind.SERVICE, reason="verified policy recovery")
    assert not transition(NodeState.FAILED, NodeState.RUNNING, **args).allowed
    assert transition(NodeState.FAILED, NodeState.RUNNING, stalled_review_authorized=True, **args).allowed
    assert not transition(NodeState.HALTED, NodeState.RUNNING, stalled_review_authorized=True, **args).allowed
    assert not transition(NodeState.FAILED, NodeState.READY, stalled_review_authorized=True, **args).allowed


@pytest.mark.parametrize("lost_response", [False, True])
async def test_no_pr_creates_draft_from_checkpoint_and_reconciles_lost_response(recovery, monkeypatch, lost_response):
    import httpx
    from sqlalchemy import select

    from src.orchestration.models import OrchestrationPullRequestBinding
    from src.orchestration.pr_bindings import PullRequestIdentity, RegistrationTarget
    from src.orchestration.recovery_checkpoint import ensure_checkpoint_pr
    from src.orchestration.review_recovery import recover_stalled_stories

    ctx = recovery.ctx
    await mark_stalled(ctx)
    checkpoint = dict(repo=protocol.REPO, provider_repository_id=123, installation_id=42, branch="agent/issue-43", head_sha=ctx.head)
    monkeypatch.setattr("src.orchestration.recovery_checkpoint.provider_checkpoint", AsyncMock(return_value=checkpoint))
    target = RegistrationTarget(protocol.ORG, ctx.flow.id, ctx.node.id, 1, recovery.run, protocol.REPO, 43, 42, ctx.binding.accepted_scope)
    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_registration_target", AsyncMock(return_value=target))
    monkeypatch.setattr("src.orchestration.review_recovery.report_exit_resolver", lambda row: recovery.resolver)
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", AsyncMock(return_value=("app", "key")))
    monkeypatch.setattr(
        "src.knowledge.github_app_service.mint_installation_token_with_expiry",
        AsyncMock(side_effect=lambda *a, **kw: ("readable-refs" if kw["permissions"].get("contents") == "read" else "unreadable-refs", None)),
    )
    monkeypatch.setattr(
        "src.orchestration.pr_identity.resolve_pr_identity",
        AsyncMock(return_value=PullRequestIdentity(123, "PR_recovery", protocol.REPO, 78, ctx.head)),
    )
    created = []
    pr = {"number": 78, "head": {"sha": ctx.head, "ref": checkpoint["branch"], "repo": {"id": 123}}, "base": {"repo": {"id": 123}}}

    class Provider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, path, **kwargs):
            body = created if path.endswith("/pulls") else {"id": 123, "default_branch": "main"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", "https://api.github.com" + path))

        async def post(self, path, **kwargs):
            # GitHub refuses PR creation when the token cannot read both refs,
            # even with pull_requests:write ("not all refs are readable").
            if kwargs["headers"]["Authorization"] != "Bearer readable-refs":
                httpx.Response(
                    422,
                    json={"message": "not all refs are readable"},
                    request=httpx.Request("POST", "https://api.github.com" + path),
                ).raise_for_status()
            # The intent must be committed and visible on another connection
            # before GitHub receives a write.
            async with ctx.factory() as other:
                assert await other.scalar(select(OrchestrationDecision).where(OrchestrationDecision.kind == "recovery_pr_prepared"))
            assert kwargs["json"]["draft"] is True
            assert kwargs["json"]["head"] == checkpoint["branch"]
            created.append(pr)
            if lost_response:
                raise httpx.ReadTimeout("simulated lost response")
            return httpx.Response(201, json=pr, request=httpx.Request("POST", "https://api.github.com" + path))

    monkeypatch.setattr("src.orchestration.recovery_checkpoint.httpx.AsyncClient", lambda **kwargs: Provider())
    async with ctx.factory() as db:
        await db.delete(await db.get(OrchestrationPullRequestBinding, ctx.binding.id))
        await db.commit()

    async def prepare():
        async with ctx.factory() as db:
            node = await db.get(OrchestrationNode, ctx.node.id)
            row = await db.get(OrchestrationRunReport, recovery.run)
            execution, identity = await validate_current_report_assignment(db, row)
            return await ensure_checkpoint_pr(db, node=node, report=row, execution=execution, identity=identity)

    if lost_response:
        with pytest.raises(httpx.ReadTimeout):
            await prepare()
    binding = await prepare()
    assert binding.pr_number == 78 and binding.role == "implementation"
    assert len(created) == 1
    assert await recover_stalled_stories(ctx.factory, resolver=recovery.resolver) == 1
    async with ctx.factory() as db:
        decisions = list((await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "review_recovery_requested"))).all())
        assert json.loads(decisions[-1].reason)["recovery_source"] == "checkpoint"
        assert (await db.get(OrchestrationRunReport, recovery.run)).terminal_receipt is None


@pytest.mark.parametrize("changed", [None, "tenant_id", "event_id", "engine_node_id", "engine_attempt", "missing"])
async def test_recovery_registry_read_requires_exact_dispatch_binding(monkeypatch, changed):
    from unittest.mock import Mock

    from src.orchestration.review_recovery import report_exit_resolver

    report = SimpleNamespace(run_id="run", org_id="tenant", node_id="node", attempt=1, dispatch_metadata={"arrived_at": "arrival"})
    row = {"event_id": "run", "tenant_id": "tenant", "engine_node_id": "node", "engine_attempt": 1}
    if changed and changed != "missing":
        row[changed] = "other"
    table = SimpleNamespace(get_item=Mock(return_value={} if changed == "missing" else {"Item": row}))
    monkeypatch.setattr("src.orchestration.run_store.EngineRunStore.from_env", lambda: SimpleNamespace(table=table))
    resolver = report_exit_resolver(report)
    if changed:
        with pytest.raises(CycleBlockedError, match="recovery_registry_binding_changed"):
            await resolver.read_current("run")
    else:
        assert await resolver.read_current("run") == row
    table.get_item.assert_called_once_with(Key={"event_id": "run", "arrived_at": "arrival"}, ConsistentRead=True)


async def test_disabled_engine_does_not_scan_or_recover(monkeypatch):
    from unittest.mock import Mock

    from src.orchestration.review_recovery import recover_stalled_stories

    monkeypatch.delenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", raising=False)
    factory = Mock(side_effect=AssertionError("disabled engine must not read"))
    assert await recover_stalled_stories(factory) == 0
