"""Dispatch and recovery adapters: who may call them, and what the body cannot do.

The central property here is T3-AC04: recovery is independently authenticated and
cannot be selected by arbitrary public request content. Two tests carry it --
a publisher-role credential is refused on the recovery route, and an unsigned
body that merely *claims* recovery gets nowhere.

`verify_producer` is patched at the module boundary rather than mocked out
wholesale, so the allowlist argument each route passes is observable. That
argument is the actual authorization decision; a test that stubbed the whole
function would assert nothing about it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

from src.agentauth import task_dispatch_routes
from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.routes import AgentRuntime, get_agent_runtime
from src.agentauth.task_dispatch_routes import (
    DISPATCH_ROLES_ENV,
    RECOVERY_ROLES_ENV,
    router,
)
from src.agentauth.task_work import TASK_WORK_INDEX, TaskWorkStore, work_shard

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
DISPATCH = "b5e9835b-fc24-4231-96f2-e8b8ca3681be"
TENANT = "t-4821"
PUBLISHER_ROLE = "adp-dev-task-publisher"
RECOVERY_ROLE = "adp-dev-task-recovery"
PROOF = "signed-sts-proof"
START = 1_760_000_000

ENV = {
    "ADP_TASK_API_ADMISSION_ENABLED": "true",
    "ADP_TASK_API_RECOVERY_ENABLED": "true",
    DISPATCH_ROLES_ENV: PUBLISHER_ROLE,
    RECOVERY_ROLES_ENV: RECOVERY_ROLE,
    "WEBHOOK_EVENTS_TABLE": "events",
}


@pytest.fixture
def client(monkeypatch):
    """A live app over moto, with the STS proof check observable.

    The fake verifier enforces the allowlist it is given, so "which allowlist did
    this route use?" is a real assertion rather than a comment.
    """
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="events",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
                {"AttributeName": "task_work_shard", "AttributeType": "S"},
                {"AttributeName": "task_due", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": TASK_WORK_INDEX,
                    "KeySchema": [
                        {"AttributeName": "task_work_shard", "KeyType": "HASH"},
                        {"AttributeName": "task_due", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        calls = []
        presented_role = ["none"]

        async def fake_verify(proof, identity, *, allowed_roles=None):
            calls.append({"proof": proof, "identity": identity, "allowed": set(allowed_roles or ())})
            if not proof or presented_role[0] not in (allowed_roles or set()):
                raise HTTPException(403, "forbidden")
            return presented_role[0]

        monkeypatch.setattr(task_dispatch_routes, "verify_producer", fake_verify)

        store = BootstrapStore(table_name="events", dynamodb_client=ddb)
        now = [START]
        work = TaskWorkStore(dynamodb_client=ddb, table_name="events", clock=lambda: now[0])

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(store=store, workloads=AsyncMock(), env=dict(ENV))
        # The routes and the test must share one clock. Left alone, the route
        # builds a store on the real clock while the fixture writes deadlines
        # against a fixed timestamp, so every claim looks long expired. Overriding
        # the dependency keeps the flag/allowlist checks in work_store under test
        # (they run before this returns) while making time controllable.
        app.dependency_overrides[task_dispatch_routes.work_store] = lambda: work
        with TestClient(app) as http:
            yield SimpleNamespace(
                http=http,
                work=work,
                ddb=ddb,
                calls=calls,
                role=presented_role,
                now=now,
                app=app,
                store=store,
            )


def accept(ctx, *, dispatch=DISPATCH):
    from datetime import UTC, datetime

    return ctx.work.put_work(
        task_id=TASK,
        kind="dispatch",
        tenant_id=TENANT,
        dispatch_id=dispatch,
        envelope_digest="a" * 64,
        deadline_at=datetime.fromtimestamp(ctx.now[0] + 3600, tz=UTC),
    )


def claim_body(**overrides):
    body = {
        "schema_version": "1.0",
        "dispatch_id": DISPATCH,
        "task_id": TASK,
        "producer_proof": PROOF,
    }
    body.update(overrides)
    return body


def recovery_body(**overrides):
    body = {
        "schema_version": "1.0",
        "shard": work_shard(TASK),
        "cursor": None,
        "limit": 10,
        "producer_proof": PROOF,
    }
    body.update(overrides)
    return body


# --- T3-AC04: recovery is independently authenticated -----------------------


def test_the_publisher_credential_cannot_reach_recovery(client):
    """The two producers are not interchangeable.

    Recovery can enumerate outstanding work across tenants in a shard. A
    compromised publisher must not gain that by calling a different route with
    the credential it already has.
    """
    client.role[0] = PUBLISHER_ROLE
    accept(client)

    response = client.http.post("/internal/v1/agent/task-dispatch/recovery/claim", json=recovery_body())

    assert response.status_code == 403
    assert client.calls[-1]["allowed"] == {RECOVERY_ROLE}, "the recovery allowlist applied"


def test_the_recovery_credential_is_accepted_on_recovery(client):
    client.role[0] = RECOVERY_ROLE
    accept(client)

    response = client.http.post("/internal/v1/agent/task-dispatch/recovery/claim", json=recovery_body())

    assert response.status_code == 200
    assert [item["dispatch_id"] for item in response.json()["work"]] == [DISPATCH]


def test_a_body_claiming_recovery_without_a_proof_is_refused(client):
    """T3-AC04: request content never selects authority.

    The body is the only thing an unauthenticated caller controls, so an empty
    proof must fail closed even when every other field is well-formed.
    """
    client.role[0] = RECOVERY_ROLE
    accept(client)

    response = client.http.post(
        "/internal/v1/agent/task-dispatch/recovery/claim",
        json=recovery_body(producer_proof=""),
    )

    assert response.status_code == 422, "an empty proof is not even a valid request"


def test_recovery_roles_are_checked_separately_from_dispatch_roles(client):
    """Each route names its own allowlist; neither inherits the other's."""
    client.role[0] = PUBLISHER_ROLE
    accept(client)

    client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body())
    dispatch_call = client.calls[-1]
    client.http.post("/internal/v1/agent/task-dispatch/recovery/claim", json=recovery_body())
    recovery_call = client.calls[-1]

    assert dispatch_call["allowed"] == {PUBLISHER_ROLE}
    assert recovery_call["allowed"] == {RECOVERY_ROLE}
    assert dispatch_call["allowed"] != recovery_call["allowed"]


def test_an_unconfigured_allowlist_refuses_rather_than_admitting_everyone(client):
    """An empty allowlist means "not deployed", never "anyone"."""
    client.role[0] = RECOVERY_ROLE
    client.app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
        store=client.store,
        workloads=AsyncMock(),
        env={**ENV, RECOVERY_ROLES_ENV: ""},
    )

    response = client.http.post("/internal/v1/agent/task-dispatch/recovery/claim", json=recovery_body())

    assert response.status_code == 503


def test_the_proof_is_bound_to_the_thing_being_claimed(client):
    """A proof signed for one dispatch must not claim another.

    The identity passed to verification is the dispatch ID, which is inside the
    signed headers, so a captured proof cannot be retargeted.
    """
    client.role[0] = PUBLISHER_ROLE
    accept(client)

    client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body())

    assert client.calls[-1]["identity"] == DISPATCH


# --- Rollout flags default off ----------------------------------------------


def test_every_route_is_closed_when_the_flags_are_off(client):
    """Design section 11: default-off, and "off" means 503 rather than a partial path."""
    client.role[0] = RECOVERY_ROLE
    client.app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
        store=client.store, workloads=AsyncMock(), env={DISPATCH_ROLES_ENV: PUBLISHER_ROLE}
    )

    for path, body in (
        ("/internal/v1/agent/task-dispatch/claim", claim_body()),
        ("/internal/v1/agent/task-dispatch/recovery/claim", recovery_body()),
    ):
        assert client.http.post(path, json=body).status_code == 503


def test_recovery_stays_closed_while_only_admission_is_enabled(client):
    """The two flags are independent; enabling publish must not enable the sweep."""
    client.role[0] = RECOVERY_ROLE
    client.app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
        store=client.store,
        workloads=AsyncMock(),
        env={**ENV, "ADP_TASK_API_RECOVERY_ENABLED": "false"},
    )

    response = client.http.post("/internal/v1/agent/task-dispatch/recovery/claim", json=recovery_body())

    assert response.status_code == 503


def test_a_non_true_flag_value_does_not_enable_the_path(client):
    """ "1", "yes" and "TRUE " must not be creative synonyms for enabled."""
    client.role[0] = RECOVERY_ROLE
    client.app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
        store=client.store,
        workloads=AsyncMock(),
        env={**ENV, "ADP_TASK_API_RECOVERY_ENABLED": "1"},
    )

    response = client.http.post("/internal/v1/agent/task-dispatch/recovery/claim", json=recovery_body())

    assert response.status_code == 503


# --- The body cannot contribute authority -----------------------------------


def test_the_claim_returns_the_committed_digest_not_a_caller_supplied_one(client):
    """The publisher is a courier, not an author.

    If the request could contribute to what gets published, the publisher could
    rewrite the task it was asked to dispatch.
    """
    client.role[0] = PUBLISHER_ROLE
    accept(client)

    response = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body())

    assert response.status_code == 200
    assert response.json()["envelope_digest"] == "a" * 64


def test_unknown_body_fields_are_rejected(client):
    """extra="forbid": a field nobody reads is a field somebody will later trust."""
    client.role[0] = PUBLISHER_ROLE
    accept(client)

    response = client.http.post(
        "/internal/v1/agent/task-dispatch/claim",
        json=claim_body(tenant_id="t-other", persona="agent-anything"),
    )

    assert response.status_code == 422


def test_a_shard_outside_the_contract_range_is_refused(client):
    """`shard` reaches a partition key, so it is pattern-bound to 16 values."""
    client.role[0] = RECOVERY_ROLE

    for bad in ("v1#16", "v2#01", "v1#1", "", "v1#00 or 1=1"):
        response = client.http.post(
            "/internal/v1/agent/task-dispatch/recovery/claim",
            json=recovery_body(shard=bad),
        )
        assert response.status_code == 422, bad


def test_a_limit_beyond_the_invocation_budget_is_refused(client):
    client.role[0] = RECOVERY_ROLE

    for bad in (0, -1, 101, 10_000):
        response = client.http.post(
            "/internal/v1/agent/task-dispatch/recovery/claim",
            json=recovery_body(limit=bad),
        )
        assert response.status_code == 422, bad


def test_malformed_identifiers_are_refused_before_any_store_access(client):
    client.role[0] = PUBLISHER_ROLE

    for body in (
        claim_body(task_id="../../etc/passwd"),
        claim_body(task_id="TASK_WORK#injected"),
        claim_body(dispatch_id="not-a-uuid"),
        claim_body(schema_version="2.0"),
    ):
        response = client.http.post("/internal/v1/agent/task-dispatch/claim", json=body)
        assert response.status_code == 422
    assert client.calls == [], "nothing reached authentication, let alone the store"


# --- Settlement outcomes ----------------------------------------------------


def test_settling_confirmed_reports_the_queue_acknowledgement(client):
    client.role[0] = PUBLISHER_ROLE
    accept(client)
    claimed = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body()).json()

    response = client.http.post(
        "/internal/v1/agent/task-dispatch/settle",
        json={
            "schema_version": "1.0",
            "dispatch_id": DISPATCH,
            "task_id": TASK,
            "lease_token": claimed["lease_token"],
            "publication_outcome": "confirmed",
            "sqs_message_id": "transport-message-id",
            "producer_proof": PROOF,
        },
    )

    assert response.status_code == 200
    assert response.json()["queue_ack_status"] == "confirmed"


def test_confirmed_without_a_transport_id_is_refused(client):
    """The contract's conditional requirement, enforced server-side."""
    client.role[0] = PUBLISHER_ROLE
    accept(client)
    claimed = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body()).json()

    response = client.http.post(
        "/internal/v1/agent/task-dispatch/settle",
        json={
            "schema_version": "1.0",
            "dispatch_id": DISPATCH,
            "task_id": TASK,
            "lease_token": claimed["lease_token"],
            "publication_outcome": "confirmed",
            "sqs_message_id": None,
            "producer_proof": PROOF,
        },
    )

    assert response.status_code == 409
    assert "confirmed_without_message_id" in response.json()["detail"]


def test_unknown_settlement_is_accepted_and_keeps_the_work_discoverable(client):
    """T3-AC01: the ambiguous case is a normal answer, not an error."""
    client.role[0] = PUBLISHER_ROLE
    accept(client)
    claimed = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body()).json()

    settled = client.http.post(
        "/internal/v1/agent/task-dispatch/settle",
        json={
            "schema_version": "1.0",
            "dispatch_id": DISPATCH,
            "task_id": TASK,
            "lease_token": claimed["lease_token"],
            "publication_outcome": "unknown",
            "sqs_message_id": None,
            "producer_proof": PROOF,
        },
    )

    assert settled.status_code == 200
    assert settled.json()["publication_outcome"] == "unknown"
    client.role[0] = RECOVERY_ROLE
    still_due = client.http.post("/internal/v1/agent/task-dispatch/recovery/claim", json=recovery_body()).json()
    assert [item["dispatch_id"] for item in still_due["work"]] == [DISPATCH]


def test_a_forged_lease_token_cannot_settle(client):
    """The lease token is the proof of ownership, and it is compared, not parsed."""
    client.role[0] = PUBLISHER_ROLE
    accept(client)
    client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body())

    response = client.http.post(
        "/internal/v1/agent/task-dispatch/settle",
        json={
            "schema_version": "1.0",
            "dispatch_id": DISPATCH,
            "task_id": TASK,
            "lease_token": "not-the-lease",
            "publication_outcome": "confirmed",
            "sqs_message_id": "transport-message-id",
            "producer_proof": PROOF,
        },
    )

    assert response.status_code == 409


def test_claiming_absent_work_is_a_404_not_a_created_record(client):
    """A claim must never bring work into existence."""
    client.role[0] = PUBLISHER_ROLE

    response = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body())

    assert response.status_code == 404
    from src.agentauth.task_work import DISPATCH_SORT_PREFIX

    assert client.work.read(TASK, f"{DISPATCH_SORT_PREFIX}{DISPATCH}") is None


def test_a_spent_try_budget_is_retryable_and_says_so(client):
    """429 rather than 409: the sweep should come back, not give up."""
    from src.agentauth.task_work import MAX_PUBLICATION_TRIES

    client.role[0] = PUBLISHER_ROLE
    accept(client)
    for _ in range(MAX_PUBLICATION_TRIES):
        claimed = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body()).json()
        client.http.post(
            "/internal/v1/agent/task-dispatch/settle",
            json={
                "schema_version": "1.0",
                "dispatch_id": DISPATCH,
                "task_id": TASK,
                "lease_token": claimed["lease_token"],
                "publication_outcome": "unknown",
                "sqs_message_id": None,
                "producer_proof": PROOF,
            },
        )
        client.now[0] += 1

    response = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body())

    assert response.status_code == 429


def test_responses_are_not_cacheable(client):
    """A lease token in a shared cache is a lease token handed to someone else."""
    client.role[0] = PUBLISHER_ROLE
    accept(client)

    response = client.http.post("/internal/v1/agent/task-dispatch/claim", json=claim_body())

    assert response.headers["Cache-Control"] == "no-store"
