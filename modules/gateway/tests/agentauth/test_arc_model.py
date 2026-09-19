"""Real OIDC signatures, tenant identity, protected roots and live policy."""

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi import FastAPI
from sqlalchemy import update

from src.agentauth import arc_model
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV, verify_envelope
from src.agentauth.external_roots import root_store
from src.agentauth.model_policy import _resolve_principal, canonical_json
from src.agentauth.runtime_posture import reset_posture_cache
from src.shared.database import get_db
from src.shared.models.organization import Organization, User
from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaModelPreference, ServicePrincipal, ServicePrincipalAlias
from src.shared.models.vault import UserIdentity
from tests.agentauth.test_bootstrap_routes import store as store_fixture
from tests.agentauth.test_work_producer import ROLE, proof

store = store_fixture


@pytest.fixture
async def arc_context(store, report_only_db, db_session, monkeypatch):
    signing = ed25519.Ed25519PrivateKey.generate()
    monkeypatch.setenv(SIGNING_KEY_ID_ENV, "arc-test")
    monkeypatch.setenv(
        SIGNING_KEY_ENV, signing.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    )
    oidc = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(oidc.public_key())) | {"kid": "github-test"}
    now = int(time.time())
    claims = {
        "iss": arc_model.ISSUER,
        "aud": arc_model.AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "sub": "repo:org/repo:ref:refs/heads/main",
        "repository": "org/repo",
        "repository_id": "123",
        "workflow_ref": "org/repo/.github/workflows/agent-developer.yml@refs/heads/main",
        "run_id": "456",
        "run_attempt": "1",
        "actor_id": "42",
        "event_name": "issues",
    }
    binding = {
        "repository_id": "123",
        "repository": "org/repo",
        "workflow_ref": claims["workflow_ref"],
        "runner_role": ROLE,
        "tenant_id": "tenant",
        "persona": "developer",
        "service_identity": "github_actions:org/repo:developer",
    }
    monkeypatch.setenv("ADP_ARC_MODEL_BINDINGS", json.dumps([binding]))
    db_session.add_all(
        [Organization(id="tenant", name="Tenant"), User(id="human", cognito_sub="sub", org_id="tenant", team_id="team", email="h@example.test")]
    )
    await db_session.commit()
    db_session.add(
        UserIdentity(
            user_id="human",
            org_id="tenant",
            team_id="team",
            provider="github",
            provider_user_id="42",
            verification_method="admin_manual",
            verified_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    from src.proxy.bedrock_routing import BedrockTarget
    from tests.agentauth.test_model_policy import SONNET, _add_invocability_evidence

    db_session.add(
        PersonaModelPreference(
            org_id="tenant",
            principal_kind="human",
            principal_source="self",
            principal_id="human",
            persona_key="developer",
            canonical_model_id=SONNET,
            revision=1,
            updated_by="human",
            updated_by_source="self",
        )
    )
    _add_invocability_evidence(db_session, account_id="111111111111", outcome="proven", expires_at=datetime.now(UTC) + timedelta(hours=1))
    await db_session.commit()
    monkeypatch.setattr(
        "src.proxy.bedrock_routing.bedrock_routing_resolver.resolve",
        AsyncMock(return_value=BedrockTarget(account_id="111111111111", region="us-east-1", rung="user")),
    )
    state = {"role": "webhook", "jwks": public, "claims": claims, "binding": binding, "key": oidc, "signing": signing}
    original = httpx.AsyncClient

    def remote(request):
        if request.url.host == "token.actions.githubusercontent.com":
            assert request.url.path == "/.well-known/jwks"
            return httpx.Response(200, json={"keys": [state["jwks"]]})
        assert request.url.host == "sts.us-east-1.amazonaws.com"
        return httpx.Response(
            200,
            text="<GetCallerIdentityResponse><GetCallerIdentityResult><Arn>arn:aws:sts::123456789012:assumed-role/"
            + state["role"]
            + "/session</Arn></GetCallerIdentityResult></GetCallerIdentityResponse>",
        )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(remote), **kw))
    app = FastAPI()
    app.include_router(arc_model.router)
    app.dependency_overrides[root_store] = lambda: store

    async def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    async with original(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield client, state


async def decide(context, **changes):
    client, state = context
    token = jwt.encode(state["claims"] | changes, state["key"], algorithm="RS256", headers={"kid": "github-test"})
    body = {"nonce": "a" * 64, "model_policy_contract": 1, "github_oidc_token": token}
    return await client.post("/internal/v1/agent/arc/model-decision", json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


async def test_arc_human_root_is_signed_repeatable_and_observes_enforcement(arc_context, store, db_session):
    response = await decide(arc_context)
    assert response.status_code == 200, response.text
    document = response.json()
    result = document["result"]
    assert result["model_policy"]["posture"] == "report_only"
    verified = verify_envelope(
        document["assertion"],
        public_keys={"arc-test": arc_context[1]["signing"].public_key()},
        expected_run_id=result["invocation_id"],
        expected_generation=1,
        expected_action="model_policy_response",
        expected_command_id="a" * 64,
        expected_audience=MODEL_POLICY_AUDIENCE,
        request_body=canonical_json(result),
    )
    assert verified.tenant_id == "tenant"
    grant = store.live_grant(invocation_id=result["invocation_id"], tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert grant.authority.human_id == "human"
    assert (await decide(arc_context)).json()["result"]["invocation_id"] == result["invocation_id"]
    await db_session.execute(update(PersonaModelPolicySetting).values(enforcement_posture="enforcing", posture_revision=2))
    await db_session.commit()
    reset_posture_cache()
    enforcing = await decide(arc_context)
    assert enforcing.status_code == 200, enforcing.text
    from tests.agentauth.test_model_policy import SONNET

    assert enforcing.json()["result"]["model_policy"]["decision"]["resolved_model_id"] == SONNET


@pytest.mark.parametrize(
    "changes", [{"actor_id": "99"}, {"repository_id": "124"}, {"workflow_ref": "untrusted"}, {"aud": "wrong"}, {"exp": 1}, {"event_name": "schedule"}]
)
async def test_arc_unregistered_or_invalid_identity_never_gets_a_decision(arc_context, changes):
    assert (await decide(arc_context, **changes)).status_code == 403


async def test_arc_rejects_forged_signature_and_worker_role(arc_context):
    original = arc_context[1]["key"]
    arc_context[1]["key"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert (await decide(arc_context)).status_code == 403
    arc_context[1]["key"] = original
    arc_context[1]["role"] = "worker"
    assert (await decide(arc_context)).status_code == 403


async def test_arc_service_uses_registered_canonical_principal_and_refuses_revocation(arc_context, db_session, store):
    service = ServicePrincipal(canonical_service_principal_id="service-a", org_id="tenant", display_name="ARC", approved_by="human")
    db_session.add(service)
    await db_session.commit()
    alias = ServicePrincipalAlias(
        org_id="tenant",
        alias_source="github_actions",
        alias_id=arc_context[1]["binding"]["service_identity"],
        canonical_service_principal_id="service-a",
        registered_by="human",
    )
    db_session.add(alias)
    await db_session.commit()
    response = await decide(arc_context, event_name="schedule")
    assert response.status_code == 200, response.text
    invocation = response.json()["result"]["invocation_id"]
    grant = store.live_grant(invocation_id=invocation, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    authority = store._read("TENANT#tenant", f"AUTHORITY#{grant.authority.reference_id}")
    assert await _resolve_principal(db_session, tenant_id="tenant", grant=grant, authority=authority) == ("service_account", "service-a")
    alias.is_active = False
    alias.revoked_at = datetime.now(UTC)
    alias.revoked_by = "human"
    await db_session.commit()
    assert (await decide(arc_context, event_name="schedule")).status_code == 403
