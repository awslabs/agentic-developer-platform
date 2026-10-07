"""Real protected writes behind source-scoped and body-bound ingress proof."""

import json
import os
from datetime import UTC, datetime

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient

from src.agentauth import external_roots
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.agentauth.external_roots import RootAdmission
from src.agentauth.model_policy import _resolve_principal
from src.orchestration import intake_wiring
from src.shared.database import get_db
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from tests.agentauth.test_bootstrap_routes import store as store_fixture
from tests.agentauth.test_work_producer import ROLE, proof
from tests.agentauth.test_work_producer import sts as sts_fixture

store = store_fixture
sts = sts_fixture


@pytest.fixture
async def root_client(store, sts, db_session, monkeypatch):
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="root-context",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    monkeypatch.setenv("BG_INTAKE_CONTEXT_TABLE", table.name)
    monkeypatch.setattr(intake_wiring, "_context_table", table)
    db_session.add_all(
        [
            Organization(id="tenant", name="Tenant"),
            Organization(id="elsewhere", name="Elsewhere"),
            User(id="human", cognito_sub="sub-human", org_id="tenant", team_id="team", email="human@example.test"),
            User(id="other", cognito_sub="other-sub", org_id="elsewhere", team_id="team", email="other@example.test"),
            User(id="bot", cognito_sub="bot-sub", user_kind="bot", org_id="tenant", team_id="team", email="bot@example.test"),
        ]
    )
    await db_session.commit()
    db_session.add(
        UserIdentity(
            user_id="human",
            org_id="tenant",
            team_id="team",
            provider="gitlab",
            provider_user_id="https://gitlab.example#42",
            verification_method="admin_attested",
            verified_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    monkeypatch.setenv(
        "ADP_MODEL_ROOT_BINDINGS",
        json.dumps(
            [
                {"source": "chat", "producer_role": ROLE, "tenant_id": "tenant", "personas": ["developer"]},
                {
                    "source": "gitlab",
                    "producer_role": ROLE,
                    "tenant_id": "tenant",
                    "personas": ["developer"],
                    "instance": "https://gitlab.example",
                    "project_id": 7,
                    "repo": "group/repo",
                },
            ]
        ),
    )
    app = FastAPI()
    app.include_router(external_roots.router)
    app.dependency_overrides[external_roots.root_store] = lambda: store

    async def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        client.gateway_app = app
        yield client


def body(source="chat", **changes):
    return (
        RootAdmission(
            source=source,
            subject="sub-human" if source == "chat" else "42",
            instance="" if source == "chat" else "https://gitlab.example",
            project_id=0 if source == "chat" else 7,
            envelope={
                "message_id": "root-a",
                "message": "Authenticated user input",
                "session_id": "session-a",
                "tenant_id": "tenant",
                "persona": "developer",
                "arrived_at": datetime.now(UTC).isoformat(),
                "correlation": {"parent_principal": "victim#1", "root_human_id": "attacker"},
            },
        ).model_dump(mode="json")
        | changes
    )


async def post(client, document, token=None):
    return await client.post(
        "/internal/v1/agent/roots/admit", json=document, headers={"X-Adp-Producer-Proof": token or proof(envelope_digest(document))}
    )


@pytest.mark.parametrize("source", ["chat", "gitlab"])
async def test_root_is_canonical_protected_and_immutable_before_publication(root_client, store, db_session, source):
    document = body(source)
    response = await post(root_client, document)
    assert response.status_code == 200, response.text
    final = response.json()["envelope"]
    assert final["correlation"] == {"correlation_id": "root-a", "root_human_id": "human", "is_human_rooted": True}
    execution = store._read("TENANT#tenant", "EXEC#root-a")
    assert execution["envelope_digest"] == {"S": envelope_digest(final)}
    assert "parent_principal" not in execution
    grant = store.live_grant(invocation_id="root-a", tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert {action.value for action in grant.allowed_actions} == {"monitor"}
    authority = store._read("TENANT#tenant", f"AUTHORITY#{grant.authority.reference_id}")
    assert await _resolve_principal(db_session, tenant_id="tenant", grant=grant, authority=authority) == ("human", "human")
    assert (await post(root_client, document)).json()["envelope"] == final
    changed = {**document, "envelope": {**document["envelope"], "message": "different work"}}
    assert (await post(root_client, changed)).status_code == 403


@pytest.mark.parametrize("enabled", [None, "false", "true"])
@pytest.mark.parametrize("context_access", ["unconfigured", "read-only", "writable"])
async def test_chat_input_staging_requires_explicit_enablement(root_client, store, monkeypatch, enabled, context_access):
    if enabled is None:
        monkeypatch.delenv("ADP_CHAT_DATA_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", enabled)
    table = intake_wiring._context_table
    if context_access == "unconfigured":
        monkeypatch.delenv("BG_INTAKE_CONTEXT_TABLE")
    context_writes = []
    transact_write = store.client.transact_write_items

    def guarded_transaction(**kwargs):
        writes = [operation for operation in kwargs["TransactItems"] if any(item["TableName"] == table.name for item in operation.values())]
        context_writes.extend(writes)
        if writes and context_access == "read-only":
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Context writes denied"}}, "TransactWriteItems")
        return transact_write(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", guarded_transaction)
    document = body()
    response = await post(root_client, document)
    staging_enabled = enabled == "true"
    admitted = not staging_enabled or context_access == "writable"
    assert response.status_code == (200 if admitted else 403), response.text
    assert len(context_writes) == int(staging_enabled and context_access != "unconfigured")
    execution = store._read("TENANT#tenant", "EXEC#root-a")
    if admitted:
        assert store._read("INVOCATION#root-a", "DISPATCH") is not None
        assert ("chat_user_turn" in execution) == staging_enabled
        assert response.json()["envelope"].get("session_mode") == ("ephemeral" if staging_enabled else None)
        assert (await post(root_client, document)).json()["envelope"] == response.json()["envelope"]
    else:
        assert execution is None
        assert store._read("INVOCATION#root-a", "DISPATCH") is None
    retained = table.scan()["Items"]
    if staging_enabled and admitted:
        assert len(retained) == 1
        assert retained[0]["PK"] == "chat-input#root-a"
        assert retained[0]["ownerUserId"] == "human"
        assert retained[0]["tenantId"] == "tenant"
        assert retained[0]["payload"]["input"]["message"] == document["envelope"]["message"]
    else:
        assert retained == []
        assert "chat_user_turn" not in (execution or {})
        if not staging_enabled:
            assert context_writes == []


async def test_persistent_ingest_root_accepts_ordered_mailbox_turns_before_publication(root_client, monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    mailbox = ChatSessionMailbox(intake_wiring._context_table)
    now = int(datetime.now(UTC).timestamp())
    mailbox.select_mode(session_id="session-a", owner=("tenant", "", "human"), mode="persistent", now=now)
    first = body()
    first["envelope"]["session_mode"] = "ephemeral"
    accepted = await post(root_client, first)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["envelope"]["session_mode"] == "persistent"
    second = body(envelope={**first["envelope"], "message_id": "root-b", "message": "second turn"})
    accepted_second = await post(root_client, second)
    assert accepted_second.status_code == 200, accepted_second.text
    assert (await post(root_client, first)).status_code == 200
    assert mailbox.state(session_id="session-a", owner=("tenant", "", "human"), now=now)["sequence"] == 2
    assert mailbox.table.get_item(Key={"PK": "session#session-a", "SK": "mailbox#00000001"})["Item"]["message"] == "Authenticated user input"
    assert mailbox.table.get_item(Key={"PK": "session#session-a", "SK": "mailbox#00000002"})["Item"]["message"] == "second turn"


async def test_persistent_ingest_root_cannot_append_to_another_owner_or_reuse_turn_id(root_client, store, monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    mailbox = ChatSessionMailbox(intake_wiring._context_table)
    now = int(datetime.now(UTC).timestamp())
    mailbox.select_mode(session_id="session-a", owner=("tenant", "", "someone-else"), mode="persistent", now=now)
    assert (await post(root_client, body())).status_code == 403
    assert store._read("INVOCATION#root-a", "DISPATCH") is None
    assert mailbox.state(session_id="session-a", owner=("tenant", "", "someone-else"), now=now)["sequence"] == 0
    mailbox.select_mode(session_id="session-b", owner=("tenant", "", "human"), mode="persistent", now=now)
    accepted = body(envelope={**body()["envelope"], "session_id": "session-b", "message_id": "root-b"})
    assert (await post(root_client, accepted)).status_code == 200
    changed = body(envelope={**accepted["envelope"], "message": "different"})
    assert (await post(root_client, changed)).status_code == 403
    assert mailbox.state(session_id="session-b", owner=("tenant", "", "human"), now=now)["sequence"] == 1


async def test_persistent_ingest_recovers_mailbox_failure_after_root_registration(root_client, store, monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    mailbox = ChatSessionMailbox(intake_wiring._context_table)
    now = int(datetime.now(UTC).timestamp())
    owner = ("tenant", "", "human")
    mailbox.select_mode(session_id="session-a", owner=owner, mode="persistent", now=now)
    write = mailbox.table.meta.client.transact_write_items
    failed = False

    def once(**kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "temporary write failure"}}, "TransactWriteItems")
        return write(**kwargs)

    monkeypatch.setattr(mailbox.table.meta.client, "transact_write_items", once)
    document = body()
    first = await post(root_client, document)
    assert first.status_code == 503
    assert store._read("INVOCATION#root-a", "DISPATCH") is not None
    assert mailbox.state(session_id="session-a", owner=owner, now=now)["sequence"] == 0
    retry = await post(root_client, document)
    assert retry.status_code == 200, retry.text
    assert mailbox.state(session_id="session-a", owner=owner, now=now)["sequence"] == 1
    assert (await post(root_client, document)).status_code == 200
    assert mailbox.state(session_id="session-a", owner=owner, now=now)["sequence"] == 1


@pytest.mark.parametrize("subject", ["other-sub", "bot-sub", "unknown"])
async def test_cross_tenant_bot_or_unknown_subject_never_gets_authority(root_client, store, subject):
    response = await post(root_client, body(subject=subject))
    assert response.status_code == 403
    assert store._read("INVOCATION#root-a", "DISPATCH") is None


@pytest.mark.parametrize("change", [{"subject": "alice"}, {"instance": "https://other.example"}, {"project_id": 8}])
async def test_gitlab_never_resolves_a_username_other_instance_or_other_project(root_client, store, change):
    response = await post(root_client, body("gitlab", **change))
    assert response.status_code == 403
    assert store._read("INVOCATION#root-a", "DISPATCH") is None


async def test_worker_role_cannot_use_this_ingress_surface(root_client, store, sts):
    sts["role"] = "worker"
    assert (await post(root_client, body())).status_code == 403
    assert store._read("INVOCATION#root-a", "DISPATCH") is None


@pytest.mark.parametrize(
    "changes", [{"message": None}, {"message": ""}, {"attachments": ["https://object.test/private"]}, {"attachments": ["art_a", "art_a"]}]
)
async def test_chat_root_requires_bounded_user_input_and_artifact_references(root_client, store, changes):
    document = body()
    document["envelope"].update(changes)
    assert (await post(root_client, document)).status_code == 403
    assert store._read("INVOCATION#root-a", "DISPATCH") is None


async def test_producer_proof_is_not_replayable_with_a_changed_request(root_client, store, sts):
    document = body()
    token = proof(envelope_digest(document))
    document["subject"] = "bot-sub"
    assert (await post(root_client, document, token)).status_code == 403
    assert sts["requests"] == []
    assert store._read("INVOCATION#root-a", "DISPATCH") is None


@pytest.mark.parametrize("source", ["chat", "gitlab"])
async def test_external_root_freezes_persona_mapping_before_publication(root_client, store, db_session, source):
    from src.agentauth.model_policy import parse_snapshot, resolve_decision
    from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaModelPreference
    from tests.agentauth.test_model_policy import SONNET

    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.283",
            enforcement_posture="report_only",
            revision=1,
            posture_revision=1,
        )
    )
    db_session.add(
        PersonaModelPreference(
            org_id="tenant",
            principal_kind="human",
            principal_source="self",
            principal_id="human",
            persona_key="developer",
            canonical_model_id=SONNET,
            requested_alias="sonnet",
            revision=1,
            updated_by="human",
            updated_by_source="self",
        )
    )
    await db_session.commit()
    response = await post(root_client, body(source))
    assert response.status_code == 200, response.text
    assert response.json()["model_policy_snapshot"]["status"] == "available"
    execution = store._read("TENANT#tenant", "EXEC#root-a")
    snapshot = parse_snapshot(execution["model_policy_snapshot"]["S"], execution["model_policy_snapshot_digest"]["S"])
    assert snapshot.principal_id == "human"
    assert resolve_decision(snapshot, invocation_id="root-a", persona="developer").resolved_model_id == SONNET


async def test_chat_cannot_bind_a_native_codex_persona_to_the_claude_harness(root_client, store, monkeypatch):
    monkeypatch.setenv(
        "ADP_MODEL_ROOT_BINDINGS",
        json.dumps([{"source": "chat", "producer_role": ROLE, "tenant_id": "tenant", "personas": ["agent-codex-reviewer"]}]),
    )
    document = body()
    document["envelope"]["persona"] = "agent-codex-reviewer"
    assert (await post(root_client, document)).status_code == 403
    assert store._read("INVOCATION#root-a", "DISPATCH") is None


async def test_chat_supervisor_admission_role_cannot_provision_root(root_client, store, sts, monkeypatch):
    bindings = json.loads(os.environ["ADP_MODEL_ROOT_BINDINGS"])
    bindings[0]["chat_supervisor_role"] = "arn:aws:iam::123456789012:role/chat-supervisor"
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    sts["role"] = "chat-supervisor"
    assert (await post(root_client, body())).status_code == 403
    assert store._read("INVOCATION#root-a", "DISPATCH") is None


def test_supervisor_role_cannot_be_registered_for_non_chat_producer(monkeypatch):
    monkeypatch.setenv(
        "ADP_MODEL_ROOT_BINDINGS",
        json.dumps(
            [
                {
                    "source": "gitlab",
                    "producer_role": ROLE,
                    "tenant_id": "tenant",
                    "personas": ["developer"],
                    "chat_supervisor_role": "arn:aws:iam::123456789012:role/chat-supervisor",
                }
            ]
        ),
    )
    with pytest.raises(HTTPException) as refusal:
        external_roots.root_bindings()
    assert refusal.value.status_code == 503


def test_supervisor_role_cannot_reuse_ingress_producer_identity(monkeypatch):
    monkeypatch.setenv(
        "ADP_MODEL_ROOT_BINDINGS",
        json.dumps(
            [
                {
                    "source": "chat",
                    "producer_role": ROLE,
                    "tenant_id": "tenant",
                    "personas": ["developer"],
                    "chat_supervisor_role": ROLE,
                }
            ]
        ),
    )
    with pytest.raises(HTTPException) as refusal:
        external_roots.root_bindings()
    assert refusal.value.status_code == 503
