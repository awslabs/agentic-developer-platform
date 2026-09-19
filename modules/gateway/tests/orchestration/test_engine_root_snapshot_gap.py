"""PMM-07: pin *why* an orchestration root carries no model-policy snapshot.

The runtime inventory records `orchestration` as `blocked`. A "blocked" claim is
only honest if it is the verified consequence of the code rather than an
assumption, so these tests reproduce the three refusals that make up the
blocker. They are written to FAIL once the orchestration dispatch contract is
reordered -- at which point the inventory entry must be promoted rather than
left stale, which is the whole point of pinning it.

The ordering, from `dispatch_pass.py`:

1. `_dispatch_one` reserves the work claim with `admit()` inside the tick
   transaction (`:953`).
2. The tick commits.
3. `publish_pending` -- *synchronous*, post-commit, no session -- calls
   `EngineAuthorityWriter.provision()` (`:1073`), which is the first moment the
   protected execution/grant records exist.
4. The worker bootstraps and its pod bind flips the execution to `active`.

`ensure_snapshot_report_only` is only ever reached from `admit_pending()`, and
there is no point in that sequence where it could succeed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore, envelope_digest
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.agentauth.model_policy import ModelPolicyError, bootstrap_model_policy, ensure_snapshot_for_admission
from src.agentauth.workload import VerifiedPod
from src.orchestration.work_admission import admit_pending
from src.orchestration.work_claims import WorkClaimError

INVOCATION = "node-a:1"
DECISION = "dec-1"


@pytest.fixture
def store(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "authority")
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        yield BootstrapStore(table_name="authority", dynamodb_client=client)


def _engine_grant(now):
    return DelegatedGrant(
        grant_id=f"grant:{INVOCATION}:1",
        tenant_id="tenant",
        principal=f"{INVOCATION}#1",
        authority=AuthorityReference("gate_decision", DECISION, "human", "tenant"),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        repo_scope=frozenset({"org/repo"}),
        flow_id="flow-1",
        expires_at=now + timedelta(hours=1),
    )


def _engine_envelope():
    return {
        "message_id": INVOCATION,
        "tenant_id": "tenant",
        "persona": "developer",
        "arrived_at": "2026-09-19T09:00:00Z",
        "source_ref": {"repo": "org/repo", "issue": 7},
    }


def _provision(store, now):
    """Stand in for `EngineAuthorityWriter.provision()` in publish_pending."""
    store.client.put_item(
        TableName=store.table,
        Item={
            "pk": {"S": "TENANT#tenant"},
            "sk": {"S": f"AUTHORITY#{DECISION}"},
            "status": {"S": "active"},
            "authority_kind": {"S": "gate_decision"},
            "human_id": {"S": "human"},
            "flow_id": {"S": "flow-1"},
            "created_at": {"S": "2026-09-19T09:00:00Z"},
            "expires_at": {"S": "2026-09-26T09:00:00Z"},
        },
    )
    store.provision_pending(envelope=_engine_envelope(), grant=_engine_grant(now), now=now)


@pytest.mark.asyncio
async def test_engine_admission_precedes_its_own_execution_record(store):
    """Step 1: `admit_pending` cannot run where the engine admits.

    `_dispatch_one` admits inside the tick transaction, before `provision()`
    has written anything. Swapping `admit()` for `admit_pending()` there -- the
    obvious one-line "fix" -- cannot work: there is no dispatch pointer to read.
    """
    with pytest.raises(WorkClaimError) as refusal:
        await admit_pending(store, INVOCATION)

    assert refusal.value.code == "dispatch_unresolved"


@pytest.mark.asyncio
async def test_engine_root_reaches_the_worker_with_no_snapshot(store):
    """Step 3-4: by the time the record exists, nothing has attached a snapshot.

    This is the user-visible consequence: the signed proposal an orchestration
    run should carry is simply absent, so a saved mapping cannot be compared
    against legacy behaviour on this path at all.
    """
    now = datetime.now(UTC)
    _provision(store, now)

    execution = store._read("TENANT#tenant", f"EXEC#{INVOCATION}")
    assert [key for key in execution if "model_policy" in key] == []

    record = type(
        "Record",
        (),
        {"tenant_id": "tenant", "invocation_id": INVOCATION, "principal": f"{INVOCATION}#1", "current_attempt": 1},
    )()
    assert bootstrap_model_policy(store=store, record=record, grant=_engine_grant(now)) == {
        "posture": "report_only",
        "status": "unavailable",
        "reason": "snapshot_missing",
    }


@pytest.mark.asyncio
async def test_snapshot_cannot_be_attached_once_the_worker_has_bound(store):
    """Step 4: a late attach is refused, so "do it at bootstrap" does not work.

    `_persist_snapshot` is guarded on `status = pending` precisely so a snapshot
    cannot be introduced after execution begins. The pod bind has already moved
    the execution to `active`, which is what forecloses the late-attach option.
    """
    now = datetime.now(UTC)
    _provision(store, now)
    store.bind(
        invocation_id=INVOCATION,
        digest=envelope_digest(_engine_envelope()),
        pod=VerifiedPod("pod-uid", "worker", "adp-agents", "agent-scaledjob-sa", "10.0.0.1"),
        now=now,
    )
    assert store._read("TENANT#tenant", f"EXEC#{INVOCATION}")["status"] == {"S": "active"}

    # Session is unused: the refusal happens on the DynamoDB precondition,
    # before any database work, so no session is needed to demonstrate it.
    with pytest.raises(ModelPolicyError) as refusal:
        await ensure_snapshot_for_admission(None, store=store, invocation_id=INVOCATION)

    assert refusal.value.reason == "dispatch_not_pending"
