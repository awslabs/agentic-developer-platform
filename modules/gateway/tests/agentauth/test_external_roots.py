"""Real protected writes behind source-scoped and body-bound ingress proof."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from src.agentauth import external_roots
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.external_roots import RootAdmission
from src.agentauth.model_policy import _resolve_principal
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
            verification_method="admin_manual",
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
            harness_contract_revision="0.3.220",
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
