"""Creation uncertainty never permits a second sandbox or a synthetic cleanup."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from src.agentauth import chat_data_routes
from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_admission import admit
from src.agentauth.chat_cancellation import CancelTurn, ChatCancellation
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_crew_run import CrewRunStore
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from src.agentauth.workload import VerifiedPod
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.organization import Organization, User
from tests.agentauth import test_chat_pre_admission_cleanup as cleanup
from tests.agentauth import test_chat_sandbox_workload as workload_checks
from tests.agentauth import test_chat_terminal_publication as publication
from tests.agentauth.test_external_roots import proof

client = cleanup.client
runtime = cleanup.runtime
store = cleanup.store
sts = cleanup.sts
retained_input_table = cleanup.retained_input_table
registered_owner = cleanup.registered_owner
transport = cleanup.transport
fixtures = cleanup.fixtures
recovery = cleanup.recovery
RESERVE = "/internal/v1/agent/chat/data/reserve"
verifier = workload_checks.verifier
crew_verifier = workload_checks.crew_verifier
consumer = publication.consumer


@pytest.fixture
def crew_store(runtime, unbound, monkeypatch):
    registry = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="crew-creation-runs",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    crew = CrewRunStore(registry)
    monkeypatch.setattr("src.agentauth.chat_data_routes.current_crew_store", lambda: crew)
    monkeypatch.setattr("src.agentauth.chat_authority.current_crew_store", lambda: crew)
    monkeypatch.setattr("src.agentauth.chat_session_recovery.current_crew_store", lambda: crew)
    monkeypatch.setattr("src.agentauth.chat_supervisor_resume.current_crew_store", lambda: crew)
    monkeypatch.setattr("src.agentauth.chat_data_routes._get_context_table", lambda: runtime[2])
    monkeypatch.setattr(runtime[1], "crew_workloads", lambda: chat_data_routes.crew_workloads())
    monkeypatch.setattr(runtime[1].workloads, "find_reserved_sandbox", lambda **_: None)
    mailbox = ChatSessionMailbox(runtime[2])
    owner = ("tenant", "team", "human")
    mailbox.select_mode(session_id="session-a", owner=owner, mode="persistent", now=runtime[-1])
    mailbox.accept(session_id="session-a", owner=owner, turn=AcceptedTurn(turn_id="run-write", message=unbound[2]["message"]), now=runtime[-1])
    return crew


@pytest.mark.parametrize(
    "configured, expected",
    [
        (None, 20),
        ("1", 1),
        ("20", 20),
        ("21", None),
        ("25", None),
        ("0", None),
        ("-1", None),
        ("invalid", None),
        ("02", None),
        ("", None),
    ],
)
def test_crew_capacity_configuration(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("ADP_MAX_SESSIONS_PER_CREW", raising=False)
    else:
        monkeypatch.setenv("ADP_MAX_SESSIONS_PER_CREW", configured)
    if expected is None:
        with pytest.raises(HTTPException) as error:
            chat_data_routes.crew_session_limit()
        assert error.value.status_code == 503
    else:
        assert chat_data_routes.crew_session_limit() == expected


async def test_user_scoped_run_requires_matching_ready_inventory(client, runtime, unbound, crew_store, db_session_factory, monkeypatch):
    target_ref = unbound[0].name
    pod_uid = unbound[0].uid
    now = runtime[-1]
    verified_pod = {"name": target_ref, "uid": pod_uid, "owner_key": "b" * 52}
    verified_lookups = []

    def verify_bound(*, name, uid):
        verified_lookups.append((name, uid))
        return SimpleNamespace(**verified_pod)

    monkeypatch.setattr(chat_data_routes, "crew_workloads", lambda: SimpleNamespace(verify_bound=verify_bound))
    async with db_session_factory() as db:
        executor = CrewExecutor(
            tenant_id="tenant",
            user_id="human",
            owner_key="a" * 52,
            target_kind="eks-pod",
            target_ref=pod_uid,
            generation=1,
            lease_generation=2,
            lease_expires_at=now + 300,
            state="ready",
        )
        db.add(executor)
        await db.commit()

    body = {"session_id": "session-a", "target_kind": "eks-pod", "target_ref": target_ref, "executor_scope": "user"}
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    original_scalar = AsyncSession.scalar
    inventory_queries = []

    async def record_inventory_lock(session, statement, *args, **kwargs):
        if any(column.get("entity") is CrewExecutor for column in getattr(statement, "column_descriptions", ())):
            inventory_queries.append(str(statement.compile(dialect=postgresql.dialect())))
        return await original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", record_inventory_lock)
    response = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert response.status_code == 409
    assert crew_store.table.scan()["Count"] == 0
    assert inventory_queries and all("FOR UPDATE" in query for query in inventory_queries)

    async with db_session_factory() as db:
        executor = await db.get(CrewExecutor, executor.id)
        executor.lease_generation = 1
        await db.commit()
    response = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert response.status_code == 409
    assert verified_lookups == [(target_ref, pod_uid)]
    assert crew_store.table.scan()["Count"] == 0

    verified_pod["owner_key"] = "a" * 52
    verified_pod["uid"] = "another-pod"
    response = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert response.status_code == 409
    assert crew_store.table.scan()["Count"] == 0

    verified_pod["uid"] = pod_uid
    response = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert response.status_code == 409
    assert crew_store.table.scan()["Count"] == 0
    lease_row = {
        "PK": "crew#tenant#human",
        "SK": "executor",
        "tenantId": "tenant",
        "ownerUserId": "human",
        "crewLease": {
            "tenant_id": "tenant",
            "user_id": "human",
            "target_kind": "eks-pod",
            "target_ref": pod_uid,
            "generation": 2,
            "expires_at": now + 60,
            "endpoint": "http://crew-pod.example.test",
        },
    }
    runtime[2].put_item(Item=lease_row)
    response = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert response.status_code == 409
    assert crew_store.table.scan()["Count"] == 0
    lease_row["crewLease"]["generation"] = 1
    runtime[2].put_item(Item=lease_row)
    response = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert response.status_code == 409
    assert verified_lookups[-1] == (target_ref, pod_uid)
    assert crew_store.table.scan()["Count"] == 0


async def test_provisioning_crew_run_prepares_pod_without_granting_model_authority(
    client,
    runtime,
    unbound,
    crew_store,
    db_session_factory,
    monkeypatch,
):
    workloads = SimpleNamespace(find_reserved_sandbox=lambda **_: None, find_exited_sandbox=lambda **_: None)
    monkeypatch.setattr(chat_data_routes, "crew_workloads", lambda: workloads)
    async with db_session_factory() as db:
        executor = CrewExecutor(
            tenant_id="tenant",
            user_id="human",
            owner_key="a" * 52,
            target_kind="eks-pod",
            generation=1,
            state="provisioning",
        )
        db.add(executor)
        await db.commit()
    body = {
        "session_id": "session-a",
        "target_kind": "eks-pod",
        "target_ref": unbound[0].name,
        "executor_scope": "user",
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
    }
    path = "/internal/v1/agent/chat/crew-run"
    no_creation = {key: value for key, value in body.items() if key not in ("run_id", "envelope_digest")}
    denied = await client.post(path, json=no_creation, headers={"X-Adp-Producer-Proof": proof(envelope_digest(no_creation))})
    assert denied.status_code == 409
    assert crew_store.table.scan()["Count"] == 0

    assert (await reserve(client, unbound)).status_code == 200
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "provisioning"
    assert response.json()["owner_key"] == "a" * 52
    assert "pod_uid" not in response.json()
    run_id = response.json()["run_id"]
    assert crew_store.read_run(run_id)["status"] == "provisioning"
    recovered = await client.post(path, json=body, headers=headers)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json() == response.json()
    assert crew_store.table.scan()["Count"] == 3
    assert crew_store.read_run(run_id)["status"] == "provisioning"
    async with db_session_factory() as db:
        assert (await db.get(CrewExecutor, executor.id)).state == "provisioning"


async def test_crew_run_mints_for_a_tenant_member_whose_own_org_differs(client, runtime, unbound, crew_store, db_session_factory, monkeypatch):
    """Crew supervision resolves the session owner through tenant membership, not users.org_id."""
    workloads = SimpleNamespace(find_reserved_sandbox=lambda **_: None, find_exited_sandbox=lambda **_: None)
    monkeypatch.setattr(chat_data_routes, "crew_workloads", lambda: workloads)
    async with db_session_factory() as db:
        db.add_all(
            [
                Organization(id="personal", name="Personal"),
                CrewExecutor(tenant_id="tenant", user_id="human", owner_key="a" * 52, target_kind="eks-pod", generation=1, state="provisioning"),
            ]
        )
        await db.execute(update(User).where(User.id == "human").values(org_id="personal"))
        await db.commit()
    body = {
        "session_id": "session-a",
        "target_kind": "eks-pod",
        "target_ref": unbound[0].name,
        "executor_scope": "user",
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
    }
    assert (await reserve(client, unbound)).status_code == 200
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    response = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "provisioning"


@pytest.mark.parametrize("ready_before_activation", [False, True])
@pytest.mark.parametrize(
    "changed",
    [
        None,
        "lease",
        "lease_state",
        "creation",
        "session",
        "pod",
        "owner",
        "inventory",
        "missing_lease",
        "wrong_tenant",
        "provisional",
    ],
)
async def test_crew_activation_requires_unchanged_owner_pod_lease_and_creation(
    client,
    runtime,
    unbound,
    crew_store,
    db_session_factory,
    monkeypatch,
    changed,
    ready_before_activation,
):
    pod = replace(unbound[0], owner_key="a" * 52)
    monkeypatch.setattr(chat_data_routes, "crew_workloads", lambda: SimpleNamespace(verify_bound=lambda **_: pod))
    now = runtime[-1]
    async with db_session_factory() as db:
        executor = CrewExecutor(
            tenant_id="tenant",
            user_id="human",
            owner_key="a" * 52,
            target_kind="eks-pod",
            generation=1,
            state="provisioning",
        )
        db.add(executor)
        await db.commit()
    assert (await reserve(client, unbound)).status_code == 200
    prepare = {
        "session_id": "session-a",
        "target_kind": "eks-pod",
        "target_ref": pod.name,
        "executor_scope": "user",
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
    }
    response = await client.post(
        "/internal/v1/agent/chat/crew-run",
        json=prepare,
        headers={"X-Adp-Producer-Proof": proof(envelope_digest(prepare))},
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["run_id"]
    lease = {
        "tenant_id": "tenant",
        "user_id": "human",
        "target_kind": "eks-pod",
        "target_ref": pod.uid,
        "generation": 1,
        "expires_at": now + 300,
        "endpoint": "http://crew-pod.example.test",
    }
    runtime[2].put_item(
        Item={
            "PK": "crew#tenant#human",
            "SK": "executor",
            "tenantId": "tenant",
            "ownerUserId": "human",
            "crewLease": lease,
        }
    )
    if ready_before_activation:
        async with db_session_factory() as db:
            executor = await db.get(CrewExecutor, executor.id)
            executor.state = "ready"
            executor.target_ref = pod.uid
            executor.lease_generation = 1
            executor.lease_expires_at = now + 300
            await db.commit()
    activate = {
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
        "crew_run_id": run_id,
        "pod_name": pod.name,
        "pod_uid": pod.uid,
        "lease_generation": 1,
    }
    path = "/internal/v1/agent/chat/crew-run/activate"
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(activate))}
    assert (await client.post(path, json=activate)).status_code in (401, 403, 404)
    if changed == "missing_lease":
        runtime[2].delete_item(Key={"PK": "crew#tenant#human", "SK": "executor"})
    elif changed == "wrong_tenant":
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET tenantId = :tenant",
            ExpressionAttributeValues={":tenant": "other-tenant"},
        )
    elif changed == "pod":
        activate["pod_uid"] = "wrong-pod"
        headers = {"X-Adp-Producer-Proof": proof(envelope_digest(activate))}
    elif changed == "owner":
        pod = replace(pod, owner_key="b" * 52)
    elif changed == "inventory":
        async with db_session_factory() as db:
            executor = await db.get(CrewExecutor, executor.id)
            executor.generation = 2
            await db.commit()
    elif changed == "provisional":
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLeaseState = :state",
            ExpressionAttributeValues={":state": "provisioning"},
        )
    elif changed in {"lease", "lease_state", "creation", "session"}:
        original = crew_store.activate_for_pod

        def replace_before_activation(**arguments):
            if changed == "lease":
                runtime[2].update_item(
                    Key={"PK": "crew#tenant#human", "SK": "executor"},
                    UpdateExpression="SET crewLease.generation = :generation",
                    ExpressionAttributeValues={":generation": 2},
                )
            elif changed == "lease_state":
                runtime[2].update_item(
                    Key={"PK": "crew#tenant#human", "SK": "executor"},
                    UpdateExpression="SET crewLeaseState = :state",
                    ExpressionAttributeValues={":state": "revoked"},
                )
            elif changed == "session":
                runtime[2].update_item(
                    Key={"PK": "session#session-a", "SK": "header"},
                    UpdateExpression="SET sessionState = :ended",
                    ExpressionAttributeValues={":ended": "ended"},
                )
            else:
                runtime[1].store.client.update_item(
                    TableName=runtime[1].store.table,
                    Key={"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "CREATION"}},
                    UpdateExpression="SET crew_run_id = :run",
                    ExpressionAttributeValues={":run": {"S": "changed-run"}},
                )
            return original(**arguments)

        monkeypatch.setattr(crew_store, "activate_for_pod", replace_before_activation)
    response = await client.post(path, json=activate, headers=headers)
    succeeds = changed is None or (changed == "provisional" and not ready_before_activation)
    assert response.status_code == (200 if succeeds else 409), response.text
    assert crew_store.read_run(run_id)["status"] == ("active" if succeeds else "provisioning")
    async with db_session_factory() as db:
        current = await db.get(CrewExecutor, executor.id)
        expected_state = "ready" if ready_before_activation or changed in {None, "lease", "lease_state", "creation", "session"} else "provisioning"
        assert current.state == expected_state
        if expected_state == "ready" and changed != "inventory":
            assert (current.target_ref, current.lease_generation) == (pod.uid, 1)
    if changed is None:
        first_header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
        runtime[2].put_item(Item={**first_header, "PK": "session#session-b"})
        second_body = {"session_id": "session-b", "target_kind": "eks-pod", "target_ref": pod.name, "executor_scope": "user"}
        second_headers = {"X-Adp-Producer-Proof": proof(envelope_digest(second_body))}
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLeaseState = :state",
            ExpressionAttributeValues={":state": "provisioning"},
        )
        assert (await client.post("/internal/v1/agent/chat/crew-run", json=second_body, headers=second_headers)).status_code == 409
        assert crew_store.table.get_item(Key={"event_id": "crew-owner#tenant#human", "arrived_at": "crew"})["Item"]["session_count"] == 1
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLeaseState = :state",
            ExpressionAttributeValues={":state": "ready"},
        )
        monkeypatch.setenv("ADP_MAX_SESSIONS_PER_CREW", "invalid")
        assert (await client.post("/internal/v1/agent/chat/crew-run", json=second_body, headers=second_headers)).status_code == 503
        monkeypatch.setenv("ADP_MAX_SESSIONS_PER_CREW", "1")
        assert (await client.post("/internal/v1/agent/chat/crew-run", json=second_body, headers=second_headers)).status_code == 409
        monkeypatch.setenv("ADP_MAX_SESSIONS_PER_CREW", "2")
        original_attach = crew_store.attach_session

        def changed_state_during_attach(**arguments):
            runtime[2].update_item(
                Key={"PK": "crew#tenant#human", "SK": "executor"},
                UpdateExpression="SET crewLeaseState = :state",
                ExpressionAttributeValues={":state": "provisioning"},
            )
            return original_attach(**arguments)

        monkeypatch.setattr(crew_store, "attach_session", changed_state_during_attach)
        assert (await client.post("/internal/v1/agent/chat/crew-run", json=second_body, headers=second_headers)).status_code == 409
        assert crew_store.table.get_item(Key={"event_id": "crew-owner#tenant#human", "arrived_at": "crew"})["Item"]["session_count"] == 1
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLeaseState = :state",
            ExpressionAttributeValues={":state": "ready"},
        )
        monkeypatch.setattr(crew_store, "attach_session", original_attach)

        def changed_lease_during_attach(**arguments):
            runtime[2].update_item(
                Key={"PK": "crew#tenant#human", "SK": "executor"},
                UpdateExpression="SET crewLease.generation = :generation",
                ExpressionAttributeValues={":generation": 2},
            )
            return original_attach(**arguments)

        monkeypatch.setattr(crew_store, "attach_session", changed_lease_during_attach)
        assert (await client.post("/internal/v1/agent/chat/crew-run", json=second_body, headers=second_headers)).status_code == 409
        owner_key = {"event_id": "crew-owner#tenant#human", "arrived_at": "crew"}
        assert crew_store.table.get_item(Key=owner_key)["Item"]["session_count"] == 1
        monkeypatch.setattr(crew_store, "attach_session", original_attach)
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLease.generation = :generation",
            ExpressionAttributeValues={":generation": 1},
        )
        attached = await client.post("/internal/v1/agent/chat/crew-run", json=second_body, headers=second_headers)
        assert attached.status_code == 201, attached.text
        assert attached.json()["run_id"] == run_id
        assert (attached.json()["pod_name"], attached.json()["pod_uid"]) == (pod.name, pod.uid)
        owner_row = crew_store.table.get_item(Key=owner_key)["Item"]
        assert owner_row["session_count"] == 2
        assert owner_row["pod_uid"] == crew_store.read_run(run_id)["pod_uid"] == pod.uid
        second_marker = crew_store.table.get_item(Key={"event_id": "crew-lease#tenant#session-b#1", "arrived_at": "crew"})["Item"]
        assert second_marker["run_id"] == run_id
        assert len([row for row in crew_store.table.scan()["Items"] if row.get("kind") == "crew"]) == 1
        runtime[2].put_item(Item={**first_header, "PK": "session#session-c"})
        third_body = {**second_body, "session_id": "session-c"}
        third_headers = {"X-Adp-Producer-Proof": proof(envelope_digest(third_body))}
        assert (await client.post("/internal/v1/agent/chat/crew-run", json=third_body, headers=third_headers)).status_code == 409
        assert crew_store.table.get_item(Key=owner_key)["Item"]["session_count"] == 2
        assert (await client.post("/internal/v1/agent/chat/crew-run", json=second_body, headers=second_headers)).status_code == 409
    if changed == "lease" and not ready_before_activation:
        monkeypatch.setattr(crew_store, "activate_for_pod", original)
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLease.generation = :generation",
            ExpressionAttributeValues={":generation": 1},
        )
        assert (await client.post(path, json=activate, headers=headers)).status_code == 200
        assert crew_store.read_run(run_id)["status"] == "active"


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("lost_reply", [False, True])
@pytest.mark.parametrize("scope", ["session", "user"])
async def test_crew_mint_receipt_survives_lost_response_and_creation_retry(
    client, runtime, unbound, crew_store, monkeypatch, created, lost_reply, scope, db_session_factory
):
    crew = crew_store
    if scope == "user":
        async with db_session_factory() as db:
            db.add(CrewExecutor(tenant_id="tenant", user_id="human", owner_key="a" * 52, target_kind="eks-pod", generation=1, state="provisioning"))
            await db.commit()
        pod = replace(unbound[0], owner_key="a" * 52)
        workloads = SimpleNamespace(
            find_exited_sandbox=lambda **_: None,
            find_reserved_sandbox=lambda **_: pod if created else None,
            verify_bound=lambda **_: pod,
        )
        monkeypatch.setattr(chat_data_routes, "crew_workloads", lambda: workloads)
    assert (await reserve(client, unbound)).status_code == 200
    body = {
        "session_id": "session-a",
        "target_kind": "eks-pod",
        "target_ref": unbound[0].name,
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
        "executor_scope": scope,
    }
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    original_mint = crew.mint

    def mint(**arguments):
        run_id = original_mint(**arguments)
        if lost_reply:
            raise EndpointConnectionError(endpoint_url="https://dynamodb.test")
        return run_id

    monkeypatch.setattr(crew, "mint", mint)
    minted = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert minted.status_code == (503 if lost_reply else 201), minted.text
    run_id = creation(runtime)["crew_run_id"]["S"]
    if not lost_reply:
        assert run_id == minted.json()["run_id"]
    repeated = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers=headers)
    assert repeated.status_code == (201 if scope == "user" else 409), repeated.text
    if scope == "user":
        assert repeated.json() == {"run_id": run_id, "lease_generation": 1, "owner_key": "a" * 52, "state": "provisioning"}
    monkeypatch.setattr(runtime[1].workloads, "find_reserved_sandbox", lambda **_: unbound[0] if created else None)
    for _restart in range(2):
        resumed = await recovery.resume(client, runtime)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["crew_run_id"] == run_id
        assert resumed.json().get("sandbox_uid") == (unbound[0].uid if created else None)
    assert crew.table.scan()["Count"] == (3 if scope == "user" else 2)
    assert crew.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=1)
    assert (await recovery.resume(client, runtime)).status_code == 409


@pytest.mark.parametrize("revoke_failure", [None, "before", "after"])
async def test_pre_admission_removal_revokes_crew_and_preserves_replacement(
    client, runtime, unbound, crew_store, transport, monkeypatch, revoke_failure
):
    assert (await reserve(client, unbound)).status_code == 200
    body = {
        "session_id": "session-a",
        "target_kind": "eks-pod",
        "target_ref": unbound[0].name,
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
    }
    minted = await client.post("/internal/v1/agent/chat/crew-run", json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})
    assert minted.status_code == 201, minted.text
    run_id = minted.json()["run_id"]
    saved_creation = creation(runtime)
    assert saved_creation["crew_run_id"] == {"S": run_id}
    assert saved_creation["crew_lease_generation"] == {"N": "1"}
    replacement_body = {"session_id": "session-a", "target_kind": "eks-pod", "target_ref": "replacement-pod"}
    replacement_headers = {"X-Adp-Producer-Proof": proof(envelope_digest(replacement_body))}
    assert (await client.post("/internal/v1/agent/chat/crew-run", json=replacement_body, headers=replacement_headers)).status_code == 409

    unbound[1]["exited"] = True
    resumed = await recovery.resume(client, runtime)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "pre_admission_cleanup"
    assert (await cleanup.removal(client, runtime, unbound, pod_uid="foreign-pod-uid")).status_code == 404
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is False
    assert crew_store.read_run(run_id)["status"] == "active"
    assert crew_store.table.scan()["Count"] == 2
    transport.client.send_message.assert_not_called()

    unbound[1]["absent"] = True
    original_revoke = crew_store.revoke_for_lease
    if revoke_failure is not None:

        def unavailable(**arguments):
            if revoke_failure == "after":
                original_revoke(**arguments)
            raise EndpointConnectionError(endpoint_url="https://dynamodb.test")

        monkeypatch.setattr(crew_store, "revoke_for_lease", unavailable)
        removed = await cleanup.removal(client, runtime, unbound)
        assert removed.status_code == 503, removed.text
        assert "removed_at" in cleanup.receipt(runtime)
        transport.client.send_message.assert_not_called()
        monkeypatch.setattr(crew_store, "revoke_for_lease", original_revoke)
        retried = await recovery.resume(client, runtime)
        assert retried.status_code == 200, retried.text
        assert retried.json()["removed"] is True

    removed = await cleanup.removal(client, runtime, unbound)
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] is True
    assert crew_store.read_run(run_id) is None
    assert crew_store.table.scan()["Count"] == 0
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    assert creation(runtime) == saved_creation
    assert transport.client.send_message.call_count == 1

    replacement = await client.post("/internal/v1/agent/chat/crew-run", json=replacement_body, headers=replacement_headers)
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["lease_generation"] == minted.json()["lease_generation"]
    replacement_run_id = replacement.json()["run_id"]
    assert replacement_run_id != run_id
    replacement_rows = crew_store.table.scan()["Items"]
    for response in (await recovery.resume(client, runtime), await cleanup.removal(client, runtime, unbound)):
        assert response.status_code == 200, response.text
        assert response.json()["removed"] is True
    assert crew_store.table.scan()["Items"] == replacement_rows
    assert crew_store.read_run(replacement_run_id)["status"] == "active"
    assert transport.client.send_message.call_count == 1


@pytest.fixture
def unbound(runtime, transport, retained_input_table, registered_owner, sts, monkeypatch):
    recovery.supervisor(sts, monkeypatch)
    envelope = fixtures.history.prepare(runtime, message_id="run-write")
    run_hash = hashlib.sha256(b"run-write").hexdigest()
    pod = VerifiedPod(
        "original-created-uid",
        f"chat-turn-{run_hash[:12]}-{run_hash[12:32]}",
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    state = {"exited": False, "absent": False, "observations": []}
    monkeypatch.setattr(runtime[1].workloads, "_chat_sandbox", True)
    monkeypatch.setattr(runtime[1].workloads, "_digests", frozenset({pod.image_digest}))
    original_verify = runtime[1].workloads.verify_bound
    monkeypatch.setattr(
        runtime[1].workloads, "verify_bound", lambda **fields: pod if fields == {"name": pod.name, "uid": pod.uid} else original_verify(**fields)
    )

    def discover(**fields):
        assert fields == {"name": pod.name, "run_hash": run_hash, "image_digest": pod.image_digest}
        state["observations"].append("discover")
        return pod.uid if state["exited"] else None

    def exited(**fields):
        assert fields == {"name": pod.name, "uid": pod.uid, "run_hash": run_hash}
        state["observations"].append("exit")
        return pod.image_digest if state["exited"] else None

    monkeypatch.setattr(runtime[1].workloads, "find_exited_sandbox", discover)
    monkeypatch.setattr(runtime[1].workloads, "exited_sandbox_image", exited)
    monkeypatch.setattr(runtime[1].workloads, "is_absent", lambda **fields: fields == {"name": pod.name} and state["absent"])
    return pod, state, envelope


async def reserve(client, unbound, **changes):
    pod, _, envelope = unbound
    body = {"run_id": "run-write", "envelope_digest": envelope_digest(envelope), "image_digest": pod.image_digest, **changes}
    return await client.post(RESERVE, json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


def creation(runtime):
    return runtime[1].store._read("CHAT-LAUNCH#run-write", "CREATION")


def execution(runtime):
    return fixtures.finalization.execution(runtime)


def admit_original(runtime, unbound, **changes):
    return admit(
        runtime[1], run_id="run-write", digest=envelope_digest(unbound[2]), pod=replace(unbound[0], **changes), team_id="team", now=runtime[-1]
    )


async def test_creation_permission_is_issued_once_then_only_the_original_pod_can_be_cleaned(client, runtime, unbound, transport):
    response = await reserve(client, unbound)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "run_id": "run-write",
        "session_id": "session-a",
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "session_mode": "ephemeral",
        "state": "create",
        "attempt": 1,
        "pod_name": unbound[0].name,
        "image_digest": unbound[0].image_digest,
    }
    saved = creation(runtime)
    assert execution(runtime)["chat_sandbox_creation"] == saved["binding"]
    assert "workload_binding" not in execution(runtime)
    assert (await reserve(client, unbound)).status_code == 409
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert cleanup.receipt(runtime) is None
    assert "workload_binding" not in execution(runtime)
    unbound[1]["exited"] = True
    resumed = await recovery.resume(client, runtime)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "pre_admission_cleanup" and resumed.json()["sandbox_uid"] == unbound[0].uid
    assert execution(runtime)["workload_binding"] == {"S": unbound[0].uid}
    assert runtime[1].store._read(f"POD#{unbound[0].uid}", "BINDING")["invocation_id"] == {"S": "run-write"}
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is False
    unbound[1].update(absent=True)
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is True
    unbound[1]["observations"].clear()
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert not unbound[1]["observations"]
    assert (await reserve(client, unbound)).status_code == 409
    with pytest.raises(BootstrapRefusedError):
        admit_original(runtime, unbound)
    assert creation(runtime) == saved
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    assert (await recovery.completion.complete(client, runtime)).status_code == 404
    assert recovery.completion.publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    assert transport.client.send_message.call_count == 1
    assert recovery.completion.publication.payload(transport)["status"] == "interrupted"


async def test_legitimate_reserved_creation_still_admits_without_waiting_for_exit(client, runtime, unbound):
    assert (await reserve(client, unbound)).status_code == 200
    launch = admit_original(runtime, unbound)
    assert launch.sandbox_uid == unbound[0].uid
    assert admit_original(runtime, unbound) == launch
    assert (await recovery.resume(client, runtime)).json()["state"] == "admitted"
    assert (await reserve(client, unbound)).status_code == 409
    assert not unbound[1]["observations"]


@pytest.mark.parametrize("created", [False, True])
async def test_persistent_launch_intent_can_resume_only_its_original_creation(client, runtime, unbound, monkeypatch, created):
    monkeypatch.setattr(runtime[1].workloads, "find_reserved_sandbox", lambda **_: unbound[0] if created else None)
    mailbox = ChatSessionMailbox(runtime[2])
    owner = ("tenant", "team", "human")
    mailbox.select_mode(session_id="session-a", owner=owner, mode="persistent", now=runtime[-1])
    mailbox.accept(session_id="session-a", owner=owner, turn=AcceptedTurn(turn_id="run-write", message=unbound[2]["message"]), now=runtime[-1])
    assert (await reserve(client, unbound)).status_code == 200
    saved = creation(runtime)
    for _restart in range(2):
        resumed = await recovery.resume(client, runtime)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["state"] == "creation_reserved"
        assert resumed.json()["pod_name"] == unbound[0].name
        assert resumed.json()["session_mode"] == "persistent"
        assert resumed.json().get("sandbox_uid") == (unbound[0].uid if created else None)
        assert creation(runtime) == saved
        assert "workload_binding" not in execution(runtime)

    original = runtime[1].store.client.transact_write_items

    def cancelled(**request):
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}},
            UpdateExpression="SET abort_command_id = :cancel",
            ExpressionAttributeValues={":cancel": {"S": "cancel-race"}},
        )
        return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", cancelled)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert creation(runtime) == saved


@pytest.mark.parametrize(
    "change", [{"name": "chat-turn-" + hashlib.sha256(b"run-write").hexdigest()[:12] + "-other"}, {"image_digest": "sha256:" + "b" * 64}]
)
async def test_admission_cannot_substitute_reserved_name_or_image(client, runtime, unbound, change):
    assert (await reserve(client, unbound)).status_code == 200
    with pytest.raises(ChatAuthorizationRefusedError):
        admit_original(runtime, unbound, **change)
    assert "workload_binding" not in execution(runtime)
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None


async def test_binding_itself_cannot_overwrite_reserved_name(client, runtime, unbound):
    assert (await reserve(client, unbound)).status_code == 200
    with pytest.raises(BootstrapRefusedError):
        runtime[1].store.bind(
            invocation_id="run-write",
            digest=envelope_digest(unbound[2]),
            pod=replace(unbound[0], name=unbound[0].name + "x"),
            now=datetime.fromtimestamp(runtime[-1], UTC),
        )
    assert "workload_binding" not in execution(runtime)


@pytest.mark.parametrize("stage", ["reservation", "adoption"])
async def test_lost_transaction_response_never_reissues_create_permission(client, runtime, unbound, monkeypatch, stage):
    if stage == "adoption":
        assert (await reserve(client, unbound)).status_code == 200
        unbound[1]["exited"] = True
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    monkeypatch.setattr(protected.client, "transact_write_items", lost)
    result = await reserve(client, unbound) if stage == "reservation" else await recovery.resume(client, runtime)
    assert result.status_code == 503
    monkeypatch.setattr(protected.client, "transact_write_items", original)
    assert (await reserve(client, unbound)).status_code == 409
    result = await recovery.resume(client, runtime)
    assert result.status_code == (409 if stage == "reservation" else 200)
    assert creation(runtime) is not None
    assert (cleanup.receipt(runtime) is not None) is (stage == "adoption")


@pytest.mark.parametrize("stage", ["reservation", "adoption"])
@pytest.mark.parametrize("change", ["attempt", "abort", "binding", "dispatch", "launch", "reservation"])
async def test_concurrent_change_fences_creation_and_adoption(client, runtime, unbound, monkeypatch, stage, change):
    if stage == "adoption":
        assert (await reserve(client, unbound)).status_code == 200
        unbound[1]["exited"] = True
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def race(**kwargs):
        if change in {"attempt", "abort", "binding"}:
            item = execution(runtime)
            field, value = {
                "attempt": ("current_attempt", {"N": "2"}),
                "abort": ("abort_command_id", {"S": "cancel"}),
                "binding": ("workload_binding", {"S": "foreign-pod"}),
            }[change]
            item[field] = value
        elif change == "dispatch":
            item = protected._read("INVOCATION#run-write", "DISPATCH")
            item["envelope_digest"] = {"S": "f" * 64}
        else:
            item = {"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "LAUNCH" if change == "launch" else "CREATION"}}
        fixtures.write_protected(runtime, item)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", race)
    response = await reserve(client, unbound) if stage == "reservation" else await recovery.resume(client, runtime)
    assert response.status_code == 409, response.text
    assert cleanup.receipt(runtime) is None
    assert "chat_pre_admission_cleanup" not in execution(runtime)
    assert protected._read(f"POD#{unbound[0].uid}", "BINDING") is None


@pytest.mark.parametrize("change", [{"tenant_id": "foreign"}, {"session_id": "foreign"}, {"attempt": 2}, {"pod_name": "foreign"}])
async def test_reservation_accepts_no_caller_selected_scope(client, runtime, unbound, change):
    assert (await reserve(client, unbound, **change)).status_code == 422
    assert creation(runtime) is None


@pytest.mark.parametrize("change", [{"run_id": "foreign"}, {"envelope_digest": "f" * 64}, {"image_digest": "sha256:" + "b" * 64}])
async def test_forged_root_or_unapproved_image_cannot_reserve(client, runtime, unbound, change):
    assert (await reserve(client, unbound, **change)).status_code == 404
    assert creation(runtime) is None


async def test_worker_cannot_reserve_sandbox(client, runtime, unbound, sts):
    sts["role"] = "worker"
    assert (await reserve(client, unbound)).status_code == 403
    assert creation(runtime) is None


@pytest.mark.parametrize("proof_kind", ["missing", "malformed", "unsigned", "wrong-body", "sts-refused"])
async def test_reservation_requires_verified_body_bound_proof(client, runtime, unbound, sts, proof_kind):
    body = {"run_id": "run-write", "envelope_digest": envelope_digest(unbound[2]), "image_digest": unbound[0].image_digest}
    digest = envelope_digest(body)
    headers = {
        "missing": {},
        "malformed": {"X-Adp-Producer-Proof": "invalid-proof"},
        "unsigned": {"X-Adp-Producer-Proof": proof(digest, signed=False)},
        "wrong-body": {"X-Adp-Producer-Proof": proof(envelope_digest({**body, "image_digest": "sha256:" + "b" * 64}))},
        "sts-refused": {"X-Adp-Producer-Proof": proof(digest)},
    }[proof_kind]
    if proof_kind == "sts-refused":
        sts["status"] = 403
    before = execution(runtime)
    assert (await client.post(RESERVE, json=body, headers=headers)).status_code == 403
    assert creation(runtime) is None
    assert execution(runtime) == before


@pytest.mark.parametrize("binding_field,value", [("tenant_id", "foreign-tenant"), ("personas", ["reviewer"])])
async def test_reservation_refuses_other_tenant_or_persona_supervision(client, runtime, unbound, monkeypatch, binding_field, value):
    bindings = json.loads(os.environ["ADP_MODEL_ROOT_BINDINGS"])
    bindings[0][binding_field] = value
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    before = execution(runtime)
    assert (await reserve(client, unbound)).status_code == 404
    assert creation(runtime) is None
    assert execution(runtime) == before


async def test_expired_root_cannot_authorize_creation(client, runtime, unbound):
    protected = runtime[1].store
    grant = protected.authority.load_grant(principal="run-write#1", tenant_id="tenant")
    authority = protected._read("TENANT#tenant", f"AUTHORITY#{grant.authority.reference_id}")
    authority["expires_at"] = {"S": "2000-01-01T00:00:00Z"}
    fixtures.write_protected(runtime, authority)
    assert (await reserve(client, unbound)).status_code == 404
    assert creation(runtime) is None


async def test_foreign_pod_binding_cannot_be_adopted(client, runtime, unbound):
    assert (await reserve(client, unbound)).status_code == 200
    unbound[1]["exited"] = True
    fixtures.write_protected(runtime, {"pk": {"S": f"POD#{unbound[0].uid}"}, "sk": {"S": "BINDING"}, "invocation_id": {"S": "foreign"}})
    assert (await recovery.resume(client, runtime)).status_code == 404
    assert cleanup.receipt(runtime) is None
    assert "workload_binding" not in execution(runtime)


async def test_disappearing_pod_between_discovery_and_exit_does_not_establish_cleanup(client, runtime, unbound, monkeypatch):
    assert (await reserve(client, unbound)).status_code == 200
    unbound[1]["exited"] = True
    monkeypatch.setattr(runtime[1].workloads, "exited_sandbox_image", lambda **fields: None)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert cleanup.receipt(runtime) is None
    assert "workload_binding" not in execution(runtime)


async def test_cancelled_reservation_keeps_cleanup_ahead_of_terminal_delivery(client, runtime, unbound, transport):
    assert (await reserve(client, unbound)).status_code == 200
    service = ChatCancellation(runtime[1].store, runtime[2], transport.sessions)
    body = CancelTurn(session_id="session-a", task_id="task-a")
    service.cancel(body, service.resolve(body, "tenant", "human"))
    assert (await reserve(client, unbound)).status_code == 409
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert "chat_queued_terminal" not in execution(runtime)
    assert "chat_pre_admission_terminal" not in execution(runtime)
    assert fixtures.outbox(runtime) is None
    unbound[1]["exited"] = True
    assert (await recovery.resume(client, runtime)).status_code == 200
    assert "abort_command_id" in execution(runtime)
    assert "chat_terminal" not in execution(runtime)
    transport.client.send_message.assert_not_called()
    unbound[1]["absent"] = True
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is True
    assert recovery.completion.publication.payload(transport)["status"] == "cancelled"
    assert execution(runtime)["status"] == {"S": "cancelled"}
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert transport.client.send_message.call_count == 1
    assert (await recovery.completion.complete(client, runtime)).status_code == 404


@pytest.mark.parametrize("operation", ["lease", "ready"])
@pytest.mark.parametrize("proof_kind", ["missing", "malformed", "unsigned", "wrong-body", "sts-refused", "worker"])
async def test_crew_lease_and_readiness_require_verified_supervisor_proof(client, runtime, unbound, crew_store, sts, operation, proof_kind):
    body = {
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
        "crew_run_id": "00000000-0000-4000-8000-000000000001",
        "pod_name": unbound[0].name,
        "pod_uid": unbound[0].uid,
        "lease_generation": 1,
    }
    digest = envelope_digest(body)
    headers = {
        "missing": {},
        "malformed": {"X-Adp-Producer-Proof": "invalid-proof"},
        "unsigned": {"X-Adp-Producer-Proof": proof(digest, signed=False)},
        "wrong-body": {"X-Adp-Producer-Proof": proof(envelope_digest({**body, "lease_generation": 2}))},
        "sts-refused": {"X-Adp-Producer-Proof": proof(digest)},
        "worker": {"X-Adp-Producer-Proof": proof(digest)},
    }[proof_kind]
    if proof_kind == "sts-refused":
        sts["status"] = 403
    elif proof_kind == "worker":
        sts["role"] = "worker"
    before_execution = execution(runtime)
    before_context = runtime[2].scan()["Items"]
    before_runs = crew_store.table.scan()["Items"]
    response = await client.post(f"/internal/v1/agent/chat/crew-run/{operation}", json=body, headers=headers)
    assert response.status_code == 403, response.text
    assert creation(runtime) is None
    assert execution(runtime) == before_execution
    assert runtime[2].scan()["Items"] == before_context
    assert crew_store.table.scan()["Items"] == before_runs


@pytest.mark.parametrize("changed", [None, "owner", "ip", "session_hash", "generation", "creation", "session_race"])
async def test_provisional_crew_lease_publishes_only_verified_private_pod(
    client,
    runtime,
    unbound,
    crew_store,
    db_session_factory,
    monkeypatch,
    changed,
):
    pod = replace(unbound[0], owner_key="a" * 52, ip="10.1.2.3", session_hash=hashlib.sha256(b"session-a").hexdigest())
    monkeypatch.setattr(chat_data_routes, "crew_workloads", lambda: SimpleNamespace(verify_bound=lambda **_: pod))
    async with db_session_factory() as db:
        db.add(CrewExecutor(tenant_id="tenant", user_id="human", owner_key="a" * 52, target_kind="eks-pod", generation=1, state="provisioning"))
        await db.commit()
    assert (await reserve(client, unbound)).status_code == 200
    prepare = {
        "session_id": "session-a",
        "target_kind": "eks-pod",
        "target_ref": pod.name,
        "executor_scope": "user",
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
    }
    minted = await client.post("/internal/v1/agent/chat/crew-run", json=prepare, headers={"X-Adp-Producer-Proof": proof(envelope_digest(prepare))})
    assert minted.status_code == 201, minted.text
    body = {
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
        "crew_run_id": minted.json()["run_id"],
        "pod_name": pod.name,
        "pod_uid": pod.uid,
        "lease_generation": 1,
    }
    path = "/internal/v1/agent/chat/crew-run/lease"
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    assert (await client.post(path, json=body)).status_code in (401, 403, 404)
    if changed == "owner":
        pod = replace(pod, owner_key="b" * 52)
    elif changed == "ip":
        pod = replace(pod, ip="8.8.8.8")
    elif changed == "session_hash":
        pod = replace(pod, session_hash=hashlib.sha256(b"session-other").hexdigest())
    elif changed == "generation":
        body["lease_generation"] = 2
        headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    elif changed == "creation":
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "CREATION"}},
            UpdateExpression="SET crew_run_id = :run",
            ExpressionAttributeValues={":run": {"S": "changed-run"}},
        )
    elif changed == "session_race":
        original = crew_store.table.meta.client.transact_write_items

        def revoke_before_publication(**kwargs):
            runtime[2].update_item(
                Key={"PK": "session#session-a", "SK": "header"},
                UpdateExpression="SET sessionState = :ended",
                ExpressionAttributeValues={":ended": "ended"},
            )
            return original(**kwargs)

        monkeypatch.setattr(crew_store.table.meta.client, "transact_write_items", revoke_before_publication)
    response = await client.post(path, json=body, headers=headers)
    assert response.status_code == (200 if changed is None else 409), response.text
    row = runtime[2].get_item(Key={"PK": "crew#tenant#human", "SK": "executor"}).get("Item")
    if changed is None:
        assert row["crewLeaseState"] == "provisioning"
        assert row["crewLease"]["target_ref"] == pod.uid
        assert row["crewLease"]["endpoint"] == "http://10.1.2.3:5476"
        assert crew_store.read_run(body["crew_run_id"])["status"] == "provisioning"
        assert (await client.post(path, json=body, headers=headers)).status_code == 200
    else:
        assert row is None


@pytest.mark.parametrize("changed", [None, "lease_race", "owner_race", "inventory", "wrong_pod", "endpoint"])
async def test_user_crew_admission_requires_activated_owner_and_transactional_lease(
    client,
    runtime,
    unbound,
    crew_store,
    db_session_factory,
    monkeypatch,
    changed,
    crew_verifier,
    transport,
    consumer,
):
    pod = replace(
        unbound[0],
        uid="pod-uid",
        owner_key="a" * 52,
        ip="10.0.0.5",
        service_account="adp-crew-pod",
        session_hash=hashlib.sha256(b"session-a").hexdigest(),
    )
    crew, sandbox, state = crew_verifier
    monkeypatch.setattr(workload_checks, "NAME", pod.name)
    state["annotations"].update({"adp.io/run-hash": pod.run_hash, "adp.io/session-hash": pod.session_hash})
    monkeypatch.setattr(chat_data_routes, "crew_workloads", lambda: crew)
    async with db_session_factory() as db:
        executor = CrewExecutor(tenant_id="tenant", user_id="human", owner_key="a" * 52, target_kind="eks-pod", generation=1, state="provisioning")
        db.add(executor)
        await db.commit()
    assert (await reserve(client, unbound)).status_code == 200
    prepare = {
        "session_id": "session-a",
        "target_kind": "eks-pod",
        "target_ref": pod.name,
        "executor_scope": "user",
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
    }
    minted = await client.post("/internal/v1/agent/chat/crew-run", json=prepare, headers={"X-Adp-Producer-Proof": proof(envelope_digest(prepare))})
    assert minted.status_code == 201, minted.text
    monkeypatch.setattr(runtime[1], "workloads", sandbox)
    with pytest.raises(workload_checks.workload.WorkloadRefusedError):
        sandbox.verify_bound(name=pod.name, uid=pod.uid)
    assert crew.verify_bound(name=pod.name, uid=pod.uid) == pod
    unadmitted = await client.post("/v1/chat/data/bootstrap", json={}, headers={"X-Adp-Workload-Token": "crew-token"})
    assert unadmitted.status_code == 404, unadmitted.text
    body = {
        "run_id": "run-write",
        "envelope_digest": envelope_digest(unbound[2]),
        "crew_run_id": minted.json()["run_id"],
        "pod_name": pod.name,
        "pod_uid": pod.uid,
        "lease_generation": 1,
    }
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    admission = {"run_id": "run-write", "envelope_digest": envelope_digest(unbound[2]), "pod_name": pod.name, "pod_uid": pod.uid}
    admit_headers = {"X-Adp-Producer-Proof": proof(envelope_digest(admission))}
    admit_path = "/internal/v1/agent/chat/data/admit"
    refused = await client.post(admit_path, json=admission, headers=admit_headers)
    assert refused.status_code in (404, 409), refused.text
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    assert "workload_binding" not in runtime[1].store._read("TENANT#tenant", "EXEC#run-write")
    lease = await client.post("/internal/v1/agent/chat/crew-run/lease", json=body, headers=headers)
    assert lease.status_code == 200, lease.text
    refused = await client.post(admit_path, json=admission, headers=admit_headers)
    assert refused.status_code in (404, 409), refused.text
    activation = await client.post("/internal/v1/agent/chat/crew-run/activate", json=body, headers=headers)
    assert activation.status_code == 200, activation.text
    if changed is None:
        recovered = await client.post(
            "/internal/v1/agent/chat/crew-run", json=prepare, headers={"X-Adp-Producer-Proof": proof(envelope_digest(prepare))}
        )
        assert recovered.status_code == 201, recovered.text
        assert recovered.json() == {
            "run_id": body["crew_run_id"],
            "lease_generation": 1,
            "owner_key": "a" * 52,
            "state": "provisioning",
            "activated": True,
        }
        before_admission = await client.post("/internal/v1/agent/chat/crew-run/ready", json=body, headers=headers)
        assert before_admission.status_code in (404, 409), before_admission.text
        assert runtime[2].get_item(Key={"PK": "crew#tenant#human", "SK": "executor"})["Item"]["crewLeaseState"] == "provisioning"
    if changed == "inventory":
        async with db_session_factory() as db:
            executor = await db.get(CrewExecutor, executor.id)
            executor.generation = 2
            await db.commit()
    elif changed == "wrong_pod":
        state["labels"]["adp.io/owner-key"] = "b" * 52
    elif changed == "endpoint":
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLease.endpoint = :other",
            ExpressionAttributeValues={":other": "http://10.9.9.9:5476"},
        )
    elif changed in {"lease_race", "owner_race"}:
        original = runtime[1].store.client.transact_write_items

        def revoke_before_admission(**kwargs):
            if changed == "lease_race":
                runtime[2].update_item(
                    Key={"PK": "crew#tenant#human", "SK": "executor"},
                    UpdateExpression="SET crewLeaseState = :revoked",
                    ExpressionAttributeValues={":revoked": "revoked"},
                )
            else:
                crew_store.table.update_item(
                    Key={"event_id": "crew-owner#tenant#human", "arrived_at": "crew"},
                    UpdateExpression="SET pod_uid = :other",
                    ExpressionAttributeValues={":other": "other-pod"},
                )
            return original(**kwargs)

        monkeypatch.setattr(runtime[1].store.client, "transact_write_items", revoke_before_admission)
    response = await client.post(admit_path, json=admission, headers=admit_headers)
    assert response.status_code == (200 if changed is None else 404), response.text
    if changed is None:
        exchange = await client.post("/v1/chat/data/bootstrap", json={}, headers={"X-Adp-Workload-Token": "crew-token"})
        assert exchange.status_code == 200, exchange.text
        capability = exchange.json()["capability"]
        read = await client.post(
            "/v1/chat/data/history/messages",
            json={"run_id": "run-write", "session_id": "session-a", "ids": []},
            headers={"Authorization": f"Bearer {capability}", "X-Adp-Workload-Token": "crew-token"},
        )
        assert read.status_code == 200, read.text
    assert (runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is not None) == (changed is None)
    if changed is None:

        async def resumed_readiness(expected):
            resume_body = {"run_id": "run-write", "envelope_digest": envelope_digest(unbound[2])}
            resumed = await client.post(
                "/internal/v1/agent/chat/data/resume", json=resume_body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(resume_body))}
            )
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["crew_readiness"] == {"run_id": body["crew_run_id"], "lease_generation": 1, "state": expected}
            assert resumed.json()["crew_owner_pod"] is True
            assert resumed.json()["sandbox_uid"] == pod.uid
            assert len([row for row in crew_store.table.scan()["Items"] if row.get("kind") == "crew"]) == 1
            assert crew_store.table.get_item(Key={"event_id": "crew-owner#tenant#human", "arrived_at": "crew"})["Item"]["session_count"] == 1

        await resumed_readiness("provisioning")
        assert (await client.post("/internal/v1/agent/chat/crew-run/ready", json=body)).status_code in (401, 403, 404)

        async def unhealthy(_):
            return False

        monkeypatch.setattr(chat_data_routes, "crew_native_healthy", unhealthy)
        not_ready = await client.post("/internal/v1/agent/chat/crew-run/ready", json=body, headers=headers)
        assert not_ready.status_code == 425, not_ready.text
        assert runtime[2].get_item(Key={"PK": "crew#tenant#human", "SK": "executor"})["Item"]["crewLeaseState"] == "provisioning"
        async with db_session_factory() as db:
            assert (await db.get(CrewExecutor, executor.id)).state == "provisioning"
        await resumed_readiness("provisioning")

        async def healthy(endpoint):
            assert endpoint == f"http://{pod.ip}:5476"
            return True

        monkeypatch.setattr(chat_data_routes, "crew_native_healthy", healthy)
        original = crew_store.table.meta.client.transact_write_items

        def revoke_before_ready(**kwargs):
            runtime[2].update_item(
                Key={"PK": "crew#tenant#human", "SK": "executor"},
                UpdateExpression="SET crewLeaseState = :revoked",
                ExpressionAttributeValues={":revoked": "revoked"},
            )
            return original(**kwargs)

        monkeypatch.setattr(crew_store.table.meta.client, "transact_write_items", revoke_before_ready)
        changed_lease = await client.post("/internal/v1/agent/chat/crew-run/ready", json=body, headers=headers)
        assert changed_lease.status_code == 409, changed_lease.text
        runtime[2].update_item(
            Key={"PK": "crew#tenant#human", "SK": "executor"},
            UpdateExpression="SET crewLeaseState = :state",
            ExpressionAttributeValues={":state": "provisioning"},
        )
        monkeypatch.setattr(crew_store.table.meta.client, "transact_write_items", original)
        session_key = {"PK": "session#session-a", "SK": "header"}
        session_lease = runtime[2].get_item(Key=session_key)["Item"]["chatLease"]

        def replace_session_lease_before_ready(**kwargs):
            runtime[2].update_item(
                Key=session_key,
                UpdateExpression="SET chatLease.expires_at = :expiry",
                ExpressionAttributeValues={":expiry": session_lease["expires_at"] - 1},
            )
            return original(**kwargs)

        monkeypatch.setattr(crew_store.table.meta.client, "transact_write_items", replace_session_lease_before_ready)
        stale_session = await client.post("/internal/v1/agent/chat/crew-run/ready", json=body, headers=headers)
        assert stale_session.status_code == 409, stale_session.text
        runtime[2].update_item(Key=session_key, UpdateExpression="SET chatLease = :lease", ExpressionAttributeValues={":lease": session_lease})
        monkeypatch.setattr(crew_store.table.meta.client, "transact_write_items", original)
        original_commit = AsyncSession.commit

        async def fail_first_inventory_commit(session):
            monkeypatch.setattr(AsyncSession, "commit", original_commit)
            raise chat_data_routes.SQLAlchemyError("injected inventory commit failure")

        monkeypatch.setattr(AsyncSession, "commit", fail_first_inventory_commit)
        partial = await client.post("/internal/v1/agent/chat/crew-run/ready", json=body, headers=headers)
        assert partial.status_code == 503, partial.text
        assert runtime[2].get_item(Key={"PK": "crew#tenant#human", "SK": "executor"})["Item"]["crewLeaseState"] == "ready"
        async with db_session_factory() as db:
            assert (await db.get(CrewExecutor, executor.id)).state == "provisioning"
        ready = await client.post("/internal/v1/agent/chat/crew-run/ready", json=body, headers=headers)
        assert ready.status_code == 200, ready.text
        assert ready.json() == {"run_id": body["crew_run_id"], "lease_generation": 1, "state": "ready"}
        await resumed_readiness("ready")
        assert (await client.post("/internal/v1/agent/chat/crew-run/ready", json=body, headers=headers)).status_code == 200
        async with db_session_factory() as db:
            current = await db.get(CrewExecutor, executor.id)
            assert (current.state, current.target_ref, current.lease_generation) == ("ready", pod.uid, 1)
        runtime[2].put_item(Item={**runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"], "PK": "session#session-b"})
        second = {"session_id": "session-b", "target_kind": "eks-pod", "target_ref": pod.name, "executor_scope": "user"}
        attached = await client.post(
            "/internal/v1/agent/chat/crew-run", json=second, headers={"X-Adp-Producer-Proof": proof(envelope_digest(second))}
        )
        assert attached.status_code == 201, attached.text
        assert attached.json()["run_id"] == body["crew_run_id"]
        assert (attached.json()["pod_name"], attached.json()["pod_uid"]) == (pod.name, pod.uid)
        assert crew_store.table.get_item(Key={"event_id": "crew-owner#tenant#human", "arrived_at": "crew"})["Item"]["session_count"] == 2
        next_envelope = fixtures.history.prepare(
            runtime,
            message_id="run-third",
            session_id="session-c",
            source_ref={"repo": "chat/session-c"},
            task_id="task-c",
        )
        monkeypatch.setattr(runtime[1].workloads, "find_exited_sandbox", lambda **_: None)
        monkeypatch.setattr(runtime[1].workloads, "find_reserved_sandbox", lambda **_: None)
        mailbox = ChatSessionMailbox(runtime[2])
        mailbox.select_mode(session_id="session-c", owner=("tenant", "team", "human"), mode="persistent", now=runtime[-1])
        mailbox.accept(
            session_id="session-c",
            owner=("tenant", "team", "human"),
            turn=AcceptedTurn(turn_id="run-third", message=next_envelope["message"]),
            now=runtime[-1],
        )
        reservation = {"run_id": "run-third", "envelope_digest": envelope_digest(next_envelope), "image_digest": pod.image_digest}
        reserved = await client.post(RESERVE, json=reservation, headers={"X-Adp-Producer-Proof": proof(envelope_digest(reservation))})
        assert reserved.status_code == 200, reserved.text
        assert reserved.json()["pod_name"] != pod.name
        next_session = {
            "session_id": "session-c",
            "target_kind": "eks-pod",
            "target_ref": reserved.json()["pod_name"],
            "executor_scope": "user",
            "run_id": "run-third",
            "envelope_digest": envelope_digest(next_envelope),
        }
        joined = await client.post(
            "/internal/v1/agent/chat/crew-run", json=next_session, headers={"X-Adp-Producer-Proof": proof(envelope_digest(next_session))}
        )
        assert joined.status_code == 201, joined.text
        assert (joined.json()["run_id"], joined.json()["pod_name"], joined.json()["pod_uid"]) == (
            body["crew_run_id"],
            pod.name,
            pod.uid,
        )
        assert runtime[1].store._read("CHAT-LAUNCH#run-third", "CREATION")["crew_run_id"] == {"S": body["crew_run_id"]}
        assert crew_store.table.get_item(Key={"event_id": "crew-owner#tenant#human", "arrived_at": "crew"})["Item"]["session_count"] == 3
        next_admission = {"run_id": "run-third", "envelope_digest": envelope_digest(next_envelope), "pod_name": pod.name, "pod_uid": pod.uid}
        next_headers = {"X-Adp-Producer-Proof": proof(envelope_digest(next_admission))}
        second_marker_key = {"event_id": "crew-lease#tenant#session-c#1", "arrived_at": "crew"}
        original_admission_write = runtime[1].store.client.transact_write_items

        def revoke_attachment_before_admission(**arguments):
            crew_store.table.update_item(
                Key=second_marker_key,
                UpdateExpression="SET #status = :revoked",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":revoked": "revoked"},
            )
            return original_admission_write(**arguments)

        monkeypatch.setattr(runtime[1].store.client, "transact_write_items", revoke_attachment_before_admission)
        denied = await client.post(admit_path, json=next_admission, headers=next_headers)
        assert denied.status_code == 404, denied.text
        assert runtime[1].store._read("TENANT#tenant", "EXEC#run-third")["status"] == {"S": "pending"}
        assert runtime[1].store._read("CHAT-LAUNCH#run-third", "LAUNCH") is None
        crew_store.table.update_item(
            Key=second_marker_key,
            UpdateExpression="SET #status = :active",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":active": "active"},
        )
        monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original_admission_write)
        shared = await client.post(admit_path, json=next_admission, headers=next_headers)
        assert shared.status_code == 200, shared.text
        assert (shared.json()["session_id"], shared.json()["run_id"]) == ("session-c", "run-third")
        resume_body = {"run_id": "run-third", "envelope_digest": envelope_digest(next_envelope)}
        resumed = await client.post(
            "/internal/v1/agent/chat/data/resume", json=resume_body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(resume_body))}
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["crew_owner_pod"] is True
        assert "crew_readiness" not in resumed.json()
        assert resumed.json()["pod_name"] == pod.name
        assert resumed.json()["sandbox_uid"] == pod.uid
        assert (await client.post(admit_path, json=next_admission, headers=next_headers)).status_code == 200
        assert runtime[1].store._read("TENANT#tenant", "EXEC#run-third")["workload_binding"] == {"S": pod.uid}
        assert runtime[1].store._read("TENANT#tenant", "EXEC#run-write")["workload_binding"] == {"S": pod.uid}
        assert len([row for row in crew_store.table.scan()["Items"] if row.get("kind") == "crew"]) == 1
        for attached_session in ("session-b", "session-c"):
            assert crew_store.revoke_for_lease(tenant_id="tenant", session_id=attached_session, lease_generation=1)
        state.update(phase="Succeeded", container_state={"terminated": {"exitCode": 0}})
        exit_response = await client.post("/internal/v1/agent/chat/data/exit", json=admission, headers=admit_headers)
        assert exit_response.status_code == 200, exit_response.text
        assert exit_response.json()["terminated"] is True
        state["absent"] = True
        removed = await client.post("/internal/v1/agent/chat/data/teardown", json=admission, headers=admit_headers)
        assert removed.status_code == 200, removed.text
        assert removed.json()["removed"] is True
        assert crew_store.read_run(body["crew_run_id"]) is None
        assert crew_store.table.get_item(Key={"event_id": "crew-owner#tenant#human", "arrived_at": "crew"}).get("Item") is None
        state.update(absent=False, phase="Running", container_state={"running": {}})
        with pytest.raises(workload_checks.workload.WorkloadRefusedError, match="Crew binding unavailable"):
            runtime[1].workloads_for_run("run-write")
        assert not runtime[1].current(runtime[0].launches.load("run-write"), runtime[-1])
        assert (await client.post("/v1/chat/data/bootstrap", json={}, headers={"X-Adp-Workload-Token": "crew-token"})).status_code == 404
        refused_read = await client.post(
            "/v1/chat/data/history/messages",
            json={"run_id": "run-write", "session_id": "session-a", "ids": []},
            headers={"Authorization": f"Bearer {capability}", "X-Adp-Workload-Token": "crew-token"},
        )
        assert refused_read.status_code == 404, refused_read.text
        refused_model = await client.post(
            "/v1/chat/model/keys",
            json={"run_id": "run-write", "session_id": "session-a"},
            headers={"Authorization": f"Bearer {capability}", "X-Adp-Workload-Token": "crew-token"},
        )
        assert refused_model.status_code == 404, refused_model.text
        for path in ("exit", "teardown"):
            replay = await client.post(f"/internal/v1/agent/chat/data/{path}", json=admission, headers=admit_headers)
            assert replay.status_code == 200, replay.text
        finalized = await client.post("/internal/v1/agent/chat/data/finalize", json=admission, headers=admit_headers)
        assert finalized.status_code == 200, finalized.text
        consumer._process_response(publication.payload(transport))
        completed = await client.post("/internal/v1/agent/chat/data/complete", json=admission, headers=admit_headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()["processing_lock_released"] is True
        for path, expected in (("finalize", finalized.json()), ("complete", completed.json())):
            replay = await client.post(f"/internal/v1/agent/chat/data/{path}", json=admission, headers=admit_headers)
            assert replay.status_code == 200, replay.text
            assert replay.json() == expected


@pytest.mark.parametrize(
    "health,expected",
    [
        ({"ok": True, "app": "kirocrew"}, True),
        ({"ok": True, "app": "other"}, False),
        ({"ok": False, "app": "kirocrew"}, False),
        ({"payload": "x" * 1100}, False),
    ],
)
async def test_native_crew_health_requires_small_matching_response(monkeypatch, health, expected):
    import httpx

    client_type = httpx.AsyncClient

    def reply(request):
        assert str(request.url) == "http://10.1.2.3:5476/api/health"
        return httpx.Response(200, json=health)

    monkeypatch.setattr(chat_data_routes.httpx, "AsyncClient", lambda **options: client_type(transport=httpx.MockTransport(reply), **options))
    assert await chat_data_routes.crew_native_healthy("http://10.1.2.3:5476") is expected
