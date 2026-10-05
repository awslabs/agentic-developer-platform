"""A committed protected dispatch survives provisioning/queue loss, once only."""

# ruff: noqa: F811

import json
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
    second = await run_dispatch_pass(session, _config())
    assert len(second.pending) == 1
    assert second.pending[0].envelope == pending.envelope
    assert node.attempts == 1
    executions = (await session.scalars(select(OrchestrationExecution))).all()
    claims = (await session.scalars(select(OrchestrationWorkClaim))).all()
    assert len(executions) == len(claims) == 1
    assert claims[0].active_run_id == pending.envelope["message_id"]
