"""Trusted HTTP admission and workload exchange with real emulator/SQL writes."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from src.agentauth import chat_data_routes
from src.agentauth.bootstrap import envelope_digest
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from tests.agentauth.test_chat_authority import runtime as runtime_fixture
from tests.agentauth.test_chat_authority import store as store_fixture
from tests.agentauth.test_work_producer import ROLE, proof
from tests.agentauth.test_work_producer import sts as sts_fixture

runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
ADMIT = "/internal/v1/agent/chat/data/admit"
EXCHANGE = "/v1/chat/data/bootstrap"


@pytest.fixture
async def client(runtime, sts, db_session_factory, monkeypatch):
    capabilities, authority, table, _, _, _, now = runtime
    protected = authority.store
    protected.client.delete_item(TableName=protected.table, Key={"pk": {"S": "CHAT-LAUNCH#run-a"}, "sk": {"S": "LAUNCH"}})
    protected.client.delete_item(TableName=protected.table, Key={"pk": {"S": "POD#chat-pod"}, "sk": {"S": "BINDING"}})
    protected.client.update_item(
        TableName=protected.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
        UpdateExpression="SET #status = :pending REMOVE workload_binding, pod_name, pod_ip",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":pending": {"S": "pending"}},
    )
    table.delete_item(Key={"PK": "session#session-a", "SK": "header"})
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setenv(
        "ADP_MODEL_ROOT_BINDINGS", json.dumps([{"source": "chat", "producer_role": ROLE, "tenant_id": "tenant", "personas": ["developer"]}])
    )
    monkeypatch.setattr(chat_data_routes, "clock", lambda: now)
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[chat_data_routes.runtime] = lambda: (authority, capabilities)

    async def database():
        async with db_session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as http:
        yield http


def document(runtime):
    pointer = runtime[1].store._read("INVOCATION#run-a", "DISPATCH")
    return {"run_id": "run-a", "envelope_digest": pointer["envelope_digest"]["S"], "pod_name": "chat-a", "pod_uid": "chat-pod"}


async def admit(client, runtime, **changes):
    body = {**document(runtime), **changes}
    return await client.post(ADMIT, json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


async def exchange(client, **headers):
    return await client.post(EXCHANGE, json={}, headers={"X-Adp-Workload-Token": "chat-token", **headers})


async def test_trusted_admission_exchange_refresh_and_retry(client, runtime, monkeypatch):
    before = await exchange(client)
    assert before.status_code == 404
    admitted = await admit(client, runtime)
    assert admitted.status_code == 200, admitted.text
    assert admitted.json() == {"run_id": "run-a", "session_id": "session-a", "lease_generation": 1}
    header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    assert (header["ownerUserId"], header["tenantId"], header["chatLease"]["run_id"]) == ("human", "tenant", "run-a")
    retry = await admit(client, runtime)
    assert retry.status_code == 200
    assert retry.json() == admitted.json()
    first = await exchange(client)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["expires_at"] == runtime[-1] + 300
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 100)
    refreshed = await exchange(client)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["expires_at"] == runtime[-1] + 400
    assert refreshed.json()["capability"] != first.json()["capability"]
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["chatLease"]["expires_at"] == runtime[-1] + 400


async def test_ownership_headers_do_not_select_a_principal(client, runtime):
    assert (await admit(client, runtime)).status_code == 200
    result = await exchange(client, **{"X-User-Id": "victim", "X-Tenant-Id": "other", "X-Run-Id": "another-run"})
    assert result.status_code == 200
    launch = runtime[0].launches.load(result.json()["run_id"])
    assert (launch.user_id, launch.tenant_id) == ("human", "tenant")
    result = await client.post(EXCHANGE, json={"run_id": "another-run"}, headers={"X-Adp-Workload-Token": "chat-token"})
    assert result.status_code == 422


@pytest.mark.parametrize("field,value", [("owner_user_id", "victim"), ("tenant_id", "other"), ("operations", ["anything"])])
async def test_admission_accepts_no_ownership_or_operation_overrides(client, runtime, field, value):
    assert (await admit(client, runtime, **{field: value})).status_code == 422
    assert runtime[1].store._read("CHAT-LAUNCH#run-a", "LAUNCH") is None


async def test_worker_role_cannot_admit_and_proof_cannot_be_rebound(client, runtime, sts):
    sts["role"] = "worker"
    assert (await admit(client, runtime)).status_code == 403
    sts["role"] = "webhook"
    body = document(runtime)
    assert (
        await client.post(ADMIT, json={**body, "pod_uid": "other"}, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})
    ).status_code == 403
    assert runtime[1].store._read("CHAT-LAUNCH#run-a", "LAUNCH") is None


async def test_invalid_workload_or_membership_revocation_prevents_refresh(client, runtime, db_session_factory):
    assert (await admit(client, runtime)).status_code == 200
    assert (await exchange(client, **{"X-Adp-Workload-Token": "invalid"})).status_code == 404
    async with db_session_factory() as db:
        await db.execute(update(TenantMembership).values(revoked_at=datetime.now(UTC)))
        await db.commit()
    assert (await exchange(client)).status_code == 404


async def test_expired_or_replaced_lease_cannot_be_revived_by_exchange(client, runtime, monkeypatch):
    assert (await admit(client, runtime)).status_code == 200
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 300)
    assert (await exchange(client)).status_code == 404
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1])
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.generation = :generation",
        ExpressionAttributeValues={":generation": 2},
    )
    assert (await exchange(client)).status_code == 404


async def test_conflicting_session_owner_is_not_adopted(client, runtime):
    header = {**runtime[3], "ownerUserId": "other"}
    header.pop("chatLease")
    runtime[2].put_item(Item=header)
    assert (await admit(client, runtime)).status_code == 404
    assert runtime[1].store._read("CHAT-LAUNCH#run-a", "LAUNCH") is None


async def test_authority_outage_and_disabled_rollout_are_not_empty_success(client, runtime, monkeypatch):
    assert (await admit(client, runtime)).status_code == 200
    runtime[4]["http_status"] = 503
    assert (await exchange(client)).status_code == 503
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await exchange(client)).json()["detail"]["error"] == "chat_data_disabled"
    assert (await admit(client, runtime)).status_code == 503


async def test_concurrent_header_creation_does_not_leave_a_launch_or_replace_owner(client, runtime, monkeypatch):
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    conflict = {**runtime[3], "ownerUserId": "other"}

    def collide(**kwargs):
        if any(action.get("Put", {}).get("Item", {}).get("pk") == {"S": "CHAT-LAUNCH#run-a"} for action in kwargs["TransactItems"]):
            runtime[2].put_item(Item=conflict)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", collide)
    assert (await admit(client, runtime)).status_code == 404
    assert protected._read("CHAT-LAUNCH#run-a", "LAUNCH") is None
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"] == conflict


async def test_lost_admission_response_reconciles_durable_launch_on_retry(client, runtime, monkeypatch):
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    state = {"fail": True}

    def lose_response(**kwargs):
        result = transact(**kwargs)
        if state["fail"] and any(action.get("Put", {}).get("Item", {}).get("pk") == {"S": "CHAT-LAUNCH#run-a"} for action in kwargs["TransactItems"]):
            state["fail"] = False
            raise EndpointConnectionError(endpoint_url="https://synthetic-dynamodb.test")
        return result

    monkeypatch.setattr(protected.client, "transact_write_items", lose_response)
    assert (await admit(client, runtime)).status_code == 503
    saved = protected._read("CHAT-LAUNCH#run-a", "LAUNCH")
    assert saved is not None
    assert (await admit(client, runtime)).status_code == 200
    assert protected._read("CHAT-LAUNCH#run-a", "LAUNCH") == saved
    assert (await exchange(client)).status_code == 200


async def test_admission_preserves_configured_retention(client, runtime, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "3600")
    assert (await admit(client, runtime)).status_code == 200
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["ttl"] == runtime[-1] + 3600
