"""Ownership admission at the protected delegated-dispatch boundary."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from starlette.concurrency import run_in_threadpool

from src.agentauth.policy import PolicyError
from src.orchestration.work_admission import admit, recover_exited_claims
from src.orchestration.work_claims import ClaimOwner, OwnerKind
from tests.agentauth import test_human_dispatch as human
from tests.orchestration import test_work_claims as sql

store = human.store
child_dispatch = human.child_dispatch
engine = sql.engine
session_factory = sql.session_factory
session = sql.session


@pytest.fixture
def enforcement(monkeypatch, store, child_dispatch, session_factory):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"EXEC#{child_dispatch.invocation}"}},
        UpdateExpression="SET provider_repository_id = :repo",
        ExpressionAttributeValues={":repo": {"N": "1234"}},
    )


async def test_delegated_child_admitted_before_publish_and_replay_is_idempotent(enforcement, child_dispatch, store):
    first = await run_in_threadpool(human.send_child, child_dispatch)
    assert await run_in_threadpool(human.send_child, child_dispatch) == first
    messages = child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]
    assert len(messages) == 1
    envelope = json.loads(messages[0]["Body"])
    assert envelope["work_claim_required"] is True
    assert envelope["source_ref"]["provider_repository_id"] == 1234


async def test_refused_child_frees_concurrency_and_cannot_publish_on_replay(enforcement, child_dispatch, store, session):
    await admit(session, org_id="tenant", repository_id=1234, issue=42, owner=ClaimOwner(OwnerKind.ENGINE_FLOW, "other-flow"), invocation_id="other")
    await session.commit()
    for _ in range(2):
        with pytest.raises(PolicyError, match="work ownership refused"):
            await run_in_threadpool(human.send_child, child_dispatch)
    assert not child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue).get("Messages")
    parent_grant = store._read("TENANT#tenant", f"GRANT#{child_dispatch.invocation}#1")["grant_id"]["S"]
    counter = store._read("TENANT#tenant", f"RESV#{parent_grant}")
    assert counter["in_flight"] == {"N": "0"}
    assert counter["total_dispatched"] == {"N": "1"}


async def test_pending_startup_cancellation_blocks_late_bootstrap_and_releases_slot(enforcement, child_dispatch, store, session):
    from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
    from src.agentauth.workload import VerifiedPod

    result = await run_in_threadpool(human.send_child, child_dispatch)
    queued = child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue)["Messages"][0]
    envelope = json.loads(queued["Body"])
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"EXEC#{result['invocation_id']}"}},
        UpdateExpression="SET arrived_at = :old",
        ExpressionAttributeValues={":old": {"S": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}},
    )
    report = await recover_exited_claims(session, store=store, workloads=None)
    assert report.released == 1
    with pytest.raises(BootstrapRefusedError):
        store.bind(
            invocation_id=result["invocation_id"],
            digest=envelope_digest(envelope),
            pod=VerifiedPod("late", "worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.3"),
            now=datetime.now(UTC),
        )
    parent_grant = store._read("TENANT#tenant", f"GRANT#{child_dispatch.invocation}#1")["grant_id"]["S"]
    assert store._read("TENANT#tenant", f"RESV#{parent_grant}")["in_flight"] == {"N": "0"}


async def test_same_lane_child_waits_without_credentials_until_parent_releases(enforcement, child_dispatch, store, session):
    from src.agentauth.bootstrap import envelope_digest
    from src.agentauth.workload import VerifiedPod
    from src.orchestration.work_admission import admit_deferred_bootstrap, maintain_worker_claim
    from src.orchestration.work_claims import WorkClaimError

    parent = store._read("TENANT#tenant", f"EXEC#{child_dispatch.invocation}")
    await admit(
        session,
        org_id="tenant",
        repository_id=1234,
        issue=42,
        owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, parent["flow_id"]["S"]),
        invocation_id=child_dispatch.invocation,
    )
    await session.commit()
    result = await run_in_threadpool(human.send_child, child_dispatch)
    queued = child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue)["Messages"][0]
    envelope = json.loads(queued["Body"])
    invocation = result["invocation_id"]
    digest = envelope_digest(envelope)
    with pytest.raises(WorkClaimError) as waiting:
        await admit_deferred_bootstrap(store, invocation, digest)
    assert waiting.value.code == "work_waiting"
    assert store.authority.load_execution(invocation_id=invocation, tenant_id="tenant").workload_binding is None
    await maintain_worker_claim(session, org_id="tenant", invocation_id=child_dispatch.invocation, terminal=True)
    await session.commit()
    await admit_deferred_bootstrap(store, invocation, digest)
    child = store.bind(
        invocation_id=invocation,
        digest=digest,
        pod=VerifiedPod("review-pod", "reviewer", "adp-agents", "agent-scaledjob-sa", "10.0.1.5"),
        now=datetime.now(UTC),
    )
    assert child.workload_binding == "review-pod"
    assert child.flow_id == parent["flow_id"]["S"]
