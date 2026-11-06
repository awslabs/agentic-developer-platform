"""A committed protected dispatch survives provisioning/queue loss, once only."""

# ruff: noqa: F811

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.dispatch_pass import publish_pending, run_dispatch_pass
from src.orchestration.models import OrchestrationDecision, OrchestrationFlow
from tests.orchestration.test_dispatch_pass import (  # noqa: F401
    FakeSQS,
    _config,
    _ready_story,
    engine,
    policy_bound_dispatch,
    protected_engine,
    provider_repository_identity,
    session,
    session_factory,
    work_claims_enabled,
)


@pytest.mark.parametrize("prefix", ["", "#"])
@pytest.mark.parametrize("persona", ["developer", "agent-codex-developer"])
@pytest.mark.parametrize("failure", ["before_provision", "queue_failure", "lost_ack"])
async def test_next_tick_replays_exact_protected_assignment(session, protected_engine, failure, persona, prefix):
    store, writer = protected_engine
    _, node, _ = await _ready_story(session)
    node.issue_ref = prefix + node.issue_ref
    first = await run_dispatch_pass(session, _config(persona=persona))
    await session.commit()
    pending = first.pending[0]
    original = json.loads(json.dumps(pending.envelope))
    if failure == "before_provision":
        from types import SimpleNamespace
        from unittest.mock import Mock

        from src.orchestration.dispatch_pass import prepare_pending

        await prepare_pending(session, first, writer=SimpleNamespace(provision=Mock(side_effect=RuntimeError("provisioning failed"))))
        assert first.publish_failed == 1 and not first.pending
    elif failure == "queue_failure":
        publish_pending(first, _config(persona=persona), client=FakeSQS(fail=True))
    elif failure == "lost_ack":
        publish_pending(first, _config(persona=persona), client=FakeSQS())
    # Simulate losing the process and its in-memory pending list.
    second = await run_dispatch_pass(session, _config(persona=persona))
    assert len(second.pending) == 1
    replay = second.pending[0]
    assert replay.envelope == original
    assert replay.node_attempt == pending.node_attempt == 1
    assert replay.deduplication_id == pending.deduplication_id
    await session.commit()
    sqs = FakeSQS()
    publish_pending(second, _config(persona=persona), client=sqs)
    assert sqs.envelope() == original
    assert second.publish_failed == 0
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 1
    decisions = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched"))).all()
    assert len(decisions) == 1


@pytest.mark.parametrize("change", ["active", "completed", "cancelled", "revoked", "paused", "policy", "digest", "completed_digest"])
async def test_protected_replay_does_not_restart_or_bypass_fences(session, protected_engine, monkeypatch, change):
    from src.orchestration.execution_policy import Decision, DenyReason

    store, writer = protected_engine
    await _ready_story(session)
    first = await run_dispatch_pass(session, _config())
    await session.commit()
    pending = first.pending[0]
    writer.provision(pending)
    key = {"pk": {"S": f"TENANT#{pending.org_id}"}, "sk": {"S": f"EXEC#{pending.envelope['message_id']}"}}
    if change in {"active", "completed", "cancelled", "revoked", "completed_digest"}:
        store.client.update_item(
            TableName=store.table,
            Key=key,
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": {"S": "completed" if change == "completed_digest" else change}},
        )
        if change == "completed_digest":
            store.client.update_item(
                TableName=store.table, Key=key, UpdateExpression="SET envelope_digest = :s", ExpressionAttributeValues={":s": {"S": "historical"}}
            )
    elif change == "digest":
        store.client.update_item(
            TableName=store.table, Key=key, UpdateExpression="SET envelope_digest = :s", ExpressionAttributeValues={":s": {"S": "wrong"}}
        )
    elif change == "paused":
        flow = await session.get(OrchestrationFlow, pending.genesis.flow_id)
        flow.execution_paused = True
        await session.commit()
    elif change == "policy":
        monkeypatch.setattr(
            "src.orchestration.policy_admission.authorize_node_dispatch", AsyncMock(return_value=Decision.block(DenyReason.POLICY_EXPIRED, "expired"))
        )
    second = await run_dispatch_pass(session, _config())
    assert second.pending == []
    if change == "completed_digest":
        assert second.publish_failed == 0  # A terminal worker has no dispatch to replay.
    elif change == "digest":
        assert second.publish_failed == 1  # Pending dispatches still enforce the digest.


@pytest.mark.parametrize(
    "status,expected", [(None, "worker_dispatch_unpublished"), ("pending", "worker_start_pending"), ("active", False), ("completed", True)]
)
async def test_monitor_distinguishes_missing_unstarted_and_running_worker(status, expected):
    from types import SimpleNamespace

    from src.orchestration.review_cycle import CycleBlockedError
    from src.orchestration.review_cycle_dispatch import ReviewCycleServices

    node = SimpleNamespace(org_id="tenant", id="node", attempts=1)
    raw = (
        None
        if status is None
        else {"tenant_id": {"S": "tenant"}, "orchestration_node_id": {"S": "node"}, "status": {"S": status}, "terminal_outcome": {"S": "complete"}}
    )
    if status == "active":
        raw["workload_binding"] = {"S": "verified-pod"}
    service = SimpleNamespace(protected=AsyncMock(return_value=raw))
    if isinstance(expected, str):
        with pytest.raises(CycleBlockedError, match=expected):
            await ReviewCycleServices.development_complete(service, None, node)
    else:
        assert await ReviewCycleServices.development_complete(service, None, node) is expected


async def test_governed_replay_retains_claim_attempt_and_shared_budget(session, protected_engine, policy_bound_dispatch, work_claims_enabled):
    from src.orchestration.models import OrchestrationExecution, OrchestrationWorkClaim
    from tests.orchestration.test_dispatch_pass import _accept_execution_policy

    flow, node, _ = await _ready_story(session)
    await _accept_execution_policy(session, flow)
    first = await run_dispatch_pass(session, _config())
    assert len(first.pending) == 1
    pending = first.pending[0]
    assert pending.envelope["handoff_required"]
    await session.commit()
    from src.orchestration.protected_dispatch_recovery import recover_one

    decision = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched"))).one()
    dispatch = json.loads(decision.reason)
    handoff = pending.envelope["handoff_expect"]
    assert dispatch["assignment"]["execution_id"] == handoff["execution_id"]
    assert dispatch["assignment"]["accepted_plan_version"] == handoff["accepted_plan_version"]
    for field in ("execution_id", "claim_generation"):
        changed = json.loads(decision.reason)
        changed["assignment"][field] = "wrong" if field == "execution_id" else handoff[field] + 1
        with pytest.raises(ValueError, match="protected_dispatch_execution_changed"):
            await recover_one(session, node=node, dispatch=changed, envelope=pending.envelope, config=_config(), store=protected_engine[0])
    second = await run_dispatch_pass(session, _config())
    assert len(second.pending) == 1
    assert second.pending[0].envelope == pending.envelope
    assert node.attempts == 1
    executions = (await session.scalars(select(OrchestrationExecution))).all()
    claims = (await session.scalars(select(OrchestrationWorkClaim))).all()
    assert len(executions) == len(claims) == 1
    assert claims[0].active_run_id == pending.envelope["message_id"]


@pytest.mark.parametrize(
    "status,bound,failures",
    [("active", False, 0), ("completed", False, 0), ("cancelled", False, 0), ("revoked", False, 0), ("pending", True, 0), ("pending", False, 1)],
)
async def test_started_worker_is_not_replayed_after_claim_handoff(session, protected_engine, work_claims_enabled, status, bound, failures):
    from src.orchestration.models import OrchestrationWorkClaim

    store, writer = protected_engine
    await _ready_story(session)
    first = await run_dispatch_pass(session, _config())
    await session.commit()
    pending = first.pending[0]
    writer.provision(pending)
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": f"TENANT#{pending.org_id}"}, "sk": {"S": f"EXEC#{pending.envelope['message_id']}"}},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": status}},
    )
    if bound:
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": f"TENANT#{pending.org_id}"}, "sk": {"S": f"EXEC#{pending.envelope['message_id']}"}},
            UpdateExpression="SET workload_binding = :b",
            ExpressionAttributeValues={":b": {"S": "verified-pod"}},
        )
    claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
    claim.active_run_id = "reviewer-invocation"
    await session.commit()

    second = await run_dispatch_pass(session, _config())
    assert second.pending == []
    assert second.publish_failed == failures
    assert claim.active_run_id == "reviewer-invocation"


@pytest.mark.parametrize("interruption", ["before_publish", "lost_ack"])
@pytest.mark.parametrize("restart_repo", ["example/b", ""])
async def test_product_dispatch_replays_frozen_assignment_after_restart(
    session, protected_engine, work_claims_enabled, monkeypatch, interruption, restart_repo
):
    from src.orchestration import repository_binding
    from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationWorkClaim
    from tests.orchestration.test_dispatch_pass import INSTALLATION_A, _make_approval, _make_flow, _make_node, _make_org

    await _make_org(session)
    flow = await _make_flow(session)
    flow.product_id = "product-1"
    approval = await _make_approval(session, flow)
    node = await _make_node(session, flow)
    node.repository_identity = "example/a"
    node.provider_repository_id = 1001
    node.repository_connection = {"provider": "github", "installation_id": INSTALLATION_A}
    session.add(
        OrchestrationAcceptedPlan(
            org_id=flow.org_id,
            flow_id=flow.id,
            version=1,
            plan_hash="a" * 64,
            accepted_by_decision_id=approval.id,
            plan_document={
                "product_id": flow.product_id,
                "nodes": [
                    {
                        "address": f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}",
                        "kind": "story",
                        "title": node.title,
                        "issue_ref": node.issue_ref,
                        "repository_identity": node.repository_identity,
                        "provider_repository_id": node.provider_repository_id,
                        "repository_connection": node.repository_connection,
                    }
                ],
            },
        )
    )
    await session.flush()

    async def verify(_session, *, name, repository_id, installation_id, issue, **_kwargs):
        assert (name, repository_id, installation_id, issue) == ("example/a", 1001, INSTALLATION_A, 4196)
        return repository_binding.VerifiedRepository("github", repository_id, name, installation_id, name, issue, "dispatch")

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verify)
    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
    first = await run_dispatch_pass(session, _config(repo=""))
    assert len(first.pending) == 1
    original = first.pending[0].envelope
    original_deduplication_id = first.pending[0].deduplication_id
    await session.commit()
    if interruption == "lost_ack":
        publish_pending(first, _config(repo=""), client=FakeSQS())
    if interruption == "before_publish" and restart_repo == "example/b":
        original_plan = await session.scalar(
            select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id, OrchestrationAcceptedPlan.version == 1)
        )
        original_plan.superseded_at = datetime.now(UTC)
        session.add(
            OrchestrationAcceptedPlan(
                org_id=flow.org_id,
                flow_id=flow.id,
                version=2,
                plan_hash="b" * 64,
                accepted_by_decision_id=approval.id,
                plan_document=original_plan.plan_document,
            )
        )
        await session.commit()
    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "false")
    held = await run_dispatch_pass(session, _config(repo=restart_repo))
    assert held.pending == []
    assert node.attempts == 1
    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
    if interruption == "before_publish" and restart_repo == "example/b":
        await _assert_product_replay_held_by_gates(session, node, flow, original, protected_engine[0], monkeypatch)
    replay = await run_dispatch_pass(session, _config(repo=restart_repo))
    assert len(replay.pending) == 1
    assert replay.pending[0].envelope == original
    assert replay.pending[0].deduplication_id == original_deduplication_id
    assert node.attempts == 1
    decisions = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched"))).all()
    assert len(decisions) == 1
    assignment = json.loads(decisions[0].reason)["assignment"]
    claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
    assert (assignment["tenant_id"], assignment["flow_id"], assignment["node_id"]) == (flow.org_id, flow.id, node.id)
    assert (assignment["repo"], assignment["provider_repository_id"], assignment["installation_id"]) == ("example/a", 1001, INSTALLATION_A)
    assert (assignment["accepted_plan_version"], assignment["accepted_plan_hash"]) == (1, "a" * 64)
    assert (assignment["claim_id"], assignment["claim_generation"], assignment["invocation_id"]) == (
        claim.id,
        claim.generation,
        original["message_id"],
    )
    assert claim.active_run_id == original["message_id"]
    from src.orchestration.protected_dispatch_recovery import recover_one

    for field, changed_value, refusal in (
        ("accepted_plan_hash", "tampered", "protected_dispatch_plan_changed"),
        ("execution_id", "other-execution", "protected_dispatch_execution_changed"),
        ("provider_repository_id", 1002, "protected_dispatch_assignment_changed"),
    ):
        changed = json.loads(decisions[0].reason)
        changed["assignment"][field] = changed_value
        with pytest.raises(ValueError, match=refusal):
            await recover_one(session, node=node, dispatch=changed, envelope=original, config=_config(repo=restart_repo), store=protected_engine[0])
    sqs = FakeSQS()
    publish_pending(replay, _config(repo=restart_repo), client=sqs)
    assert sqs.envelope() == original
    assert protected_engine[0].live_grant(
        invocation_id=original["message_id"], tenant_id=flow.org_id, attempt=1, now=datetime.now(UTC)
    ).repo_scope == frozenset({"example/a"})


@pytest.mark.parametrize("tamper", ["generation", "missing_claim"])
async def test_protected_replay_refuses_changed_claim_generation(session, protected_engine, work_claims_enabled, tamper):
    from src.orchestration.protected_dispatch_recovery import recover_one
    from tests.orchestration.test_dispatch_pass import _ready_story

    _, node, _ = await _ready_story(session)
    first = await run_dispatch_pass(session, _config())
    await session.commit()
    envelope = first.pending[0].envelope
    decision = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched"))).one()
    dispatch = json.loads(decision.reason)
    if tamper == "generation":
        dispatch["assignment"]["claim_generation"] += 1
    else:
        dispatch["assignment"]["claim_id"] = None
    with pytest.raises(ValueError, match="protected_dispatch_claim_changed"):
        await recover_one(session, node=node, dispatch=dispatch, envelope=envelope, config=_config(), store=protected_engine[0])
    assert node.attempts == 1


async def _assert_product_replay_held_by_gates(session, node, flow, envelope, store, monkeypatch):
    from src.orchestration import policy_admission, repository_binding
    from src.orchestration.execution_policy import Decision, DenyReason
    from src.orchestration.models import OrchestrationWorkClaim
    from src.orchestration.state import NodeState

    async def revoked(*_args, **_kwargs):
        raise repository_binding.RepositoryBindingError("connection_revoked")

    async def exhausted(*_args, **_kwargs):
        return Decision.block(DenyReason.WALL_CLOCK_LIMIT_EXCEEDED, "deadline exhausted")

    for refusal in ("paused", "superseded", "product_changed", "revoked", "exhausted"):
        with monkeypatch.context() as patch:
            if refusal == "paused":
                flow.execution_paused = True
            elif refusal == "superseded":
                node.state = NodeState.SUPERSEDED.value
            elif refusal == "product_changed":
                flow.product_id = "another-product"
            elif refusal == "revoked":
                patch.setattr(repository_binding, "resolve_story_repository", revoked)
            else:
                patch.setattr(policy_admission, "authorize_node_dispatch", exhausted)
            await session.flush()
            blocked = await run_dispatch_pass(session, _config(repo="example/b"))
            assert blocked.pending == []
            assert blocked.dispatched == 0
            if refusal in {"revoked", "exhausted"}:
                assert blocked.publish_failed == 1
            if refusal == "paused":
                flow.execution_paused = False
            elif refusal == "superseded":
                node.state = NodeState.RUNNING.value
            elif refusal == "product_changed":
                flow.product_id = "product-1"
            await session.flush()
    claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
    assert claim.state == "held"
    assert claim.active_run_id == envelope["message_id"]
    assert node.attempts == 1
    assert store._read(f"TENANT#{flow.org_id}", f"EXEC#{envelope['message_id']}") is None
