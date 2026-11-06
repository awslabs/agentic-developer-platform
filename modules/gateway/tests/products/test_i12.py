"""Product connection and administrator handoff scenarios."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app import create_app
from src.auth.cognito_jwt import CognitoTokenClaims
from src.auth.dependencies import _cognito_claims_to_context, get_current_user
from src.product_onboarding.callback_auth import PRODUCER, get_callback_user, mint_callback_token, signing_key
from src.products.models import Product, ProductOperation
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Team, TeamMembership
from src.shared.models.vault import UserCredential
from tests.products.membership import grant_platform_admin, revoke_platform_admin
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.mark.asyncio
async def handoff_authority(test_client, db_session, source_member, app, admin_token_context, monkeypatch):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    db_session.add(Department(id="dept-i12", org_id=source_member.org_id, name="Handoff department"))
    await db_session.flush()
    db_session.add(Team(id="team-i12", org_id=source_member.org_id, department_id="dept-i12", name="Handoff team"))
    product = await db_session.get(Product, (source_member.org_id, product_id))
    product.team_id = "team-i12"
    await db_session.commit()
    # Team assignment before joining the Product is an administrator setup path.
    await grant_platform_admin(db_session, source_member, monkeypatch)
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    session = await test_client.post(root, json={}, headers={"Idempotency-Key": "i12_session_create_123", "If-Profile-Revision": "1"})
    assert session.status_code == 201, session.text
    path = f"{root}/{session.json()['id']}/handoffs"
    headers = {"Idempotency-Key": "i12_handoff_create_123", "If-Profile-Revision": "1"}
    first = await test_client.post(path, json={"target": "administrator", "action": "assign_team"}, headers=headers)
    assert first.status_code == 202, first.text
    operation_id = first.json()["id"]
    assert first.json()["state"] == "waiting_external" and first.headers["Location"].endswith(operation_id)
    assert (await test_client.get(f"{root}/{session.json()['id']}")).json()["operation_ids"] == [operation_id]
    assert (await db_session.scalar(select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "startHandoff"))) == 1
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.actor_id == source_member.user_id and operation.request["generation"] == 1 and operation.request["sequence"] == 0
    assert operation.request["callback_scope"] == f"product-callback/{operation_id}"
    assert operation.receipt is None
    assert operation.request["team_id"] == "team-i12" and operation.request["member_id"] == source_member.user_id
    unrelated_product = await test_client.post(
        "/v1/products", json={**REQUEST, "name": "Unrelated work"}, headers={"Idempotency-Key": "i12_unrelated_product_123"}
    )
    assert unrelated_product.status_code == 201, unrelated_product.text
    unrelated_path = f"/v1/products/{unrelated_product.json()['product_id']}/projects/{unrelated_product.json()['project_id']}/onboarding-sessions"
    unrelated = await test_client.post(unrelated_path, json={}, headers={"Idempotency-Key": "i12_unrelated_session_123", "If-Profile-Revision": "1"})
    assert unrelated.status_code == 201 and unrelated.json()["state"] == "open"
    operation_path = f"/v1/products/{product_id}/projects/{project_id}/operations/{operation_id}"
    pending = await test_client.get(operation_path)
    assert pending.status_code == 200, pending.text
    assert pending.json()["state"] == "waiting_external"
    source_member.is_admin = False
    denied = await test_client.post(
        f"/admin/organizations/{source_member.org_id}/teams/team-i12/members",
        json={"user_id": source_member.user_id},
    )
    assert denied.status_code == 403, denied.text
    assert await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-i12")) is None
    app.dependency_overrides[get_current_user] = lambda: admin_token_context
    completed = await test_client.post(
        f"/admin/organizations/{source_member.org_id}/teams/team-i12/members",
        json={"user_id": source_member.user_id},
    )
    assert completed.status_code == 201, completed.text
    app.dependency_overrides[get_current_user] = lambda: source_member
    observed = await test_client.get(operation_path)
    assert observed.status_code == 200 and observed.json()["coverage"] == "complete"
    assert observed.json()["state"] == "waiting_external"
    membership_id = await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-i12"))
    assert membership_id is not None
    callback = {
        "sequence": 1,
        "binding_generation": 1,
        "receipt": {
            "tenant_id": source_member.org_id,
            "product_id": product_id,
            "project_id": project_id,
            "profile_revision": 1,
            "provider_operation_id": membership_id,
            "status": "succeeded",
        },
    }
    callback_path = f"/v1/product-callbacks/{operation_id}"
    assert (await test_client.post(callback_path, json=callback)).status_code == 403
    token = mint_callback_token(operation)
    callback_headers = {"Authorization": f"Bearer {token}"}
    accepted = await test_client.post(callback_path, json=callback, headers=callback_headers)
    assert accepted.status_code == 200 and accepted.json()["status"] == "applied", accepted.text
    duplicate = await test_client.post(callback_path, json=callback, headers=callback_headers)
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"
    db_session.expire_all()
    persisted = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert persisted.state == "succeeded" and persisted.callback_sequence == 1
    assert persisted.receipt["peer_receipt"]["provider_operation_id"] == membership_id
    app.dependency_overrides[get_current_user] = lambda: source_member
    finished = await test_client.get(f"{root}/{session.json()['id']}")
    assert finished.status_code == 200 and finished.json()["operation_ids"] == []
    assert finished.json()["state"] == "open"
    assert (await test_client.get(f"{unrelated_path}/{unrelated.json()['id']}")).json() == unrelated.json()
    assert (await test_client.get(operation_path)).json()["state"] == "succeeded"
    oauth_request = {"target": "oauth_connection", "action": "connect", "provider": "example"}
    oauth_headers = {**headers, "Idempotency-Key": "i12_oauth_handoff_123"}
    oauth = await test_client.post(path, json=oauth_request, headers=oauth_headers)
    assert oauth.status_code == 202, oauth.text
    oauth_path = f"/v1/products/{product_id}/projects/{project_id}/operations/{oauth.json()['id']}"
    assert "coverage" not in (await test_client.get(oauth_path)).json()
    oauth_operation = await db_session.get(ProductOperation, (source_member.org_id, oauth.json()["id"]))
    # Credential freshness belongs to this OAuth handoff. The earlier admin
    # callback may take arbitrarily long, so its timestamp cannot anchor this
    # fixture. Exercise both sides of the actual handoff's freshness boundary.
    credential = UserCredential(
        id="i12-credential",
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="example",
        provider="example",
        credential_type="oauth",
        label="Handoff fixture",
        secret_arn="fixture-only",
        created_at=oauth_operation.created_at - timedelta(seconds=1),
    )
    db_session.add(credential)
    await db_session.commit()
    stale = await test_client.get(oauth_path)
    assert stale.status_code == 200 and "coverage" not in stale.json(), stale.text
    credential.created_at = oauth_operation.created_at + timedelta(seconds=1)
    await db_session.commit()
    fresh = await test_client.get(oauth_path)
    assert fresh.status_code == 200 and fresh.json()["coverage"] == "complete", fresh.text
    assert (await test_client.post(path, json={"target": "oauth_connection", "action": "connect"}, headers=oauth_headers)).status_code == 422
    replay = await test_client.post(path, json={"target": "administrator", "action": "assign_team"}, headers=headers)
    assert replay.status_code == 200 and replay.json()["id"] == operation_id
    assert (await test_client.post(path, json=oauth_request, headers=headers)).status_code == 409
    invalid = await test_client.post(
        path, json={"target": "administrator", "action": "connect"}, headers={**headers, "Idempotency-Key": "i12_invalid_target_123"}
    )
    assert invalid.status_code == 422
    foreign = source_member.model_copy(update={"org_id": "other-tenant", "user_id": "foreign-member"})
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.post(path, json={"target": "administrator", "action": "assign_team"}, headers=headers)).status_code == 404
    assert (await test_client.get(operation_path)).status_code == 404


def test_callback_scope_survives_validated_service_claims():
    claims = CognitoTokenClaims(
        sub="service-sub",
        iss="https://issuer.invalid",
        client_id="service-client",
        token_use="access",
        iat=int(datetime.now(UTC).timestamp()),
        exp=int(datetime.now(UTC).timestamp()) + 3600,
        account_type="service",
        scope="product-callback/operation-id",
    )
    context = _cognito_claims_to_context(claims)
    assert context.user_id == "service-client"
    assert context.account_type == "service" and context.token_scopes == "product-callback/operation-id" and context.token_use == "access"


async def callback_fixture(test_client, db_session, source_member, monkeypatch):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    db_session.add(Department(id="dept-callback", org_id=source_member.org_id, name="Callback department"))
    await db_session.flush()
    db_session.add(Team(id="team-callback", org_id=source_member.org_id, department_id="dept-callback", name="Callback team"))
    product = await db_session.get(Product, (source_member.org_id, product_id))
    product.team_id = "team-callback"
    await db_session.commit()
    await grant_platform_admin(db_session, source_member, monkeypatch)
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    session = await test_client.post(root, json={}, headers={"Idempotency-Key": "callback_session_key_123", "If-Profile-Revision": "1"})
    assert session.status_code == 201, session.text
    created_handoff = await test_client.post(
        f"{root}/{session.json()['id']}/handoffs",
        json={"target": "administrator", "action": "assign_team"},
        headers={"Idempotency-Key": "callback_handoff_key_123", "If-Profile-Revision": "1"},
    )
    assert created_handoff.status_code == 202, created_handoff.text
    operation_id = created_handoff.json()["id"]
    receipt = {
        "tenant_id": source_member.org_id,
        "product_id": product_id,
        "project_id": project_id,
        "profile_revision": 1,
        "provider_operation_id": "unresolved-provider-attempt",
        "status": "failed",
    }
    return operation_id, f"/v1/product-callbacks/{operation_id}", {"sequence": 2, "binding_generation": 1, "receipt": receipt}


@pytest.mark.asyncio
async def test_callback_stale_generation_and_sequence(test_client, db_session, source_member, app, service_account_token_context, monkeypatch):
    operation_id, path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    producer = service_account_token_context.model_copy(
        update={"user_id": PRODUCER, "org_id": source_member.org_id, "token_scopes": f"product-callback/{operation_id}", "token_use": "access"}
    )
    app.dependency_overrides[get_callback_user] = lambda: producer
    for generation in (None, 2):
        stale = await test_client.post(path, json={**callback, "binding_generation": generation})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "callback_scope_mismatch"
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "waiting_external" and operation.callback_sequence == 0 and operation.receipt is None
    applied = await test_client.post(path, json=callback)
    assert applied.status_code == 200 and applied.json()["status"] == "applied", applied.text
    older = await test_client.post(path, json={**callback, "sequence": 1})
    assert older.status_code == 200 and older.json()["status"] == "ignored_older"
    duplicate = await test_client.post(path, json=callback)
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"
    conflict = await test_client.post(path, json={**callback, "receipt": {**callback["receipt"], "provider_operation_id": "other"}})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "callback_conflict"
    newer = await test_client.post(path, json={**callback, "sequence": 3})
    assert newer.status_code == 409 and newer.json()["detail"]["code"] == "operation_resolved"
    db_session.expire_all()
    persisted = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert persisted.state == "failed" and persisted.callback_sequence == 2
    assert persisted.receipt == {"peer_receipt": callback["receipt"], "sequence": 2}


@pytest.mark.asyncio
async def test_callback_rejects_wrong_scope_and_audience(test_client, db_session, source_member, app, service_account_token_context, monkeypatch):
    operation_id, path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    producer = service_account_token_context.model_copy(
        update={"user_id": PRODUCER, "org_id": source_member.org_id, "token_scopes": f"product-callback/{operation_id}", "token_use": "access"}
    )
    for unauthorized in (
        source_member.model_copy(update={"token_scopes": f"product-callback/{operation_id}"}),
        producer.model_copy(update={"auth_source": "iam"}),
        producer.model_copy(update={"token_use": "id"}),
        producer.model_copy(update={"token_scopes": "product-callback/other-operation"}),
        producer.model_copy(update={"token_scopes": f"other-scope product-callback/{operation_id}"}),
    ):
        app.dependency_overrides[get_callback_user] = lambda unauthorized=unauthorized: unauthorized
        refused = await test_client.post(path, json=callback)
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "callback_audience_denied"
    app.dependency_overrides[get_callback_user] = lambda: producer.model_copy(update={"org_id": "other-tenant"})
    assert (await test_client.post(path, json=callback)).status_code == 404
    app.dependency_overrides[get_callback_user] = lambda: producer
    for field, wrong in (
        ("tenant_id", "other-tenant"),
        ("product_id", "other-product"),
        ("project_id", "other-project"),
        ("profile_revision", 2),
    ):
        forged = await test_client.post(path, json={**callback, "receipt": {**callback["receipt"], field: wrong}})
        assert forged.status_code == 409 and forged.json()["detail"]["code"] == "callback_scope_mismatch"
    fabricated = await test_client.post(path, json={**callback, "receipt": {**callback["receipt"], "status": "succeeded"}})
    assert fabricated.status_code == 409 and fabricated.json()["detail"]["code"] == "effect_unverified"
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "waiting_external" and operation.callback_sequence == 0 and operation.receipt is None
    assert await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback")) is None
    operation.request = {**operation.request, "callback_scope": "product-callback/another-operation"}
    await db_session.commit()
    mismatch = await test_client.post(path, json=callback)
    assert mismatch.status_code == 403 and mismatch.json()["detail"]["code"] == "callback_audience_denied"
    db_session.expire_all()
    unchanged = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert unchanged.state == "waiting_external" and unchanged.callback_sequence == 0 and unchanged.receipt is None


@pytest.mark.asyncio
async def callback_admission(test_client, db_session, source_member, monkeypatch):
    operation_id, path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    token = mint_callback_token(operation)
    claims = jwt.decode(token, options={"verify_signature": False})

    def signed_request(payload, key=None):
        token = jwt.encode(payload, key or signing_key(), algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    forged_scope = await test_client.post(
        path,
        json=callback,
        headers={**signed_request({**claims, "scope": "product-callback/other"}), "X-Product-Callback-Scope": f"product-callback/{operation_id}"},
    )
    assert forged_scope.status_code == 403 and forged_scope.json()["detail"]["code"] == "callback_audience_denied"
    forged_tenant = await test_client.post(
        path,
        json={**callback, "receipt": {**callback["receipt"], "tenant_id": "other-tenant"}},
        headers=signed_request(claims),
    )
    assert forged_tenant.status_code == 409 and forged_tenant.json()["detail"]["code"] == "callback_scope_mismatch"
    stale_generation = await test_client.post(
        path,
        json={**callback, "binding_generation": 2},
        headers=signed_request(claims),
    )
    assert stale_generation.status_code == 409 and stale_generation.json()["detail"]["code"] == "callback_scope_mismatch"
    other_key = b"test-only-untrusted-signing-key-123456"
    invalid_signature = await test_client.post(path, json=callback, headers=signed_request(claims, key=other_key))
    assert invalid_signature.status_code == 403
    id_token = await test_client.post(
        path,
        json=callback,
        headers=signed_request({**claims, "token_use": "id", "aud": "service-client"}),
    )
    assert id_token.status_code == 403 and id_token.json()["detail"]["code"] == "callback_audience_denied"
    db_session.expire_all()
    untouched = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert untouched.state == "waiting_external" and untouched.receipt is None and untouched.callback_sequence == 0
    assert await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback")) is None
    valid = await test_client.post(path, json=callback, headers=signed_request(claims))
    assert valid.status_code == 200 and valid.json()["status"] == "applied", valid.text
    replay = await test_client.post(path, json=callback, headers=signed_request(claims))
    assert replay.status_code == 200 and replay.json()["status"] == "duplicate"
    older = await test_client.post(path, json={**callback, "sequence": 1}, headers=signed_request(claims))
    assert older.status_code == 200 and older.json()["status"] == "ignored_older"
    changed_receipt = await test_client.post(
        path,
        json={**callback, "receipt": {**callback["receipt"], "provider_operation_id": "other"}},
        headers=signed_request(claims),
    )
    assert changed_receipt.status_code == 409 and changed_receipt.json()["detail"]["code"] == "callback_conflict"
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.callback_sequence == callback["sequence"] and operation.receipt["peer_receipt"] == callback["receipt"]
    assert await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback")) is None


async def assign_callback_team(test_client, app, source_member, admin_token_context):
    app.dependency_overrides[get_current_user] = lambda: admin_token_context
    result = await test_client.post(
        f"/admin/organizations/{source_member.org_id}/teams/team-callback/members",
        json={"user_id": source_member.user_id},
    )
    source_member.is_admin = False
    app.dependency_overrides[get_current_user] = lambda: source_member
    assert result.status_code == 201, result.text


def recovery_path(callback):
    peer = callback["receipt"]
    return f"/v1/products/{peer['product_id']}/projects/{peer['project_id']}/operations"


@asynccontextmanager
async def restarted_gateway(test_engine, actor, *, callback_actor=None):
    restarted = create_app()

    async def fresh_db():
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    restarted.dependency_overrides[get_db] = fresh_db
    restarted.dependency_overrides[get_current_user] = lambda: actor
    restarted.dependency_overrides[get_callback_user] = lambda: actor if callback_actor is None else callback_actor
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_reconcile_observes_original_effect_once(test_client, db_session, source_member, app, admin_token_context, monkeypatch):
    operation_id, _, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    await assign_callback_team(test_client, app, source_member, admin_token_context)
    membership_id = await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback"))

    async def forbid_repeated_effect(*args, **kwargs):
        pytest.fail("Reconciliation must not repeat the admin membership write")

    monkeypatch.setattr("src.admin.team_memberships.add_membership", forbid_repeated_effect)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    operation.state = "outcome_unknown"
    await db_session.commit()
    root = f"{recovery_path(callback)}/{operation_id}"
    headers = {"Idempotency-Key": "reconcile_original_123", "If-Profile-Revision": "1"}
    assert (await test_client.post(f"{root}/reconcile")).status_code == 428
    result = await test_client.post(f"{root}/reconcile", headers=headers)
    assert result.status_code == 202 and result.json()["state"] == "succeeded", result.text
    assert result.headers["Location"].endswith(operation_id) and result.headers["Retry-After"] == "2"
    replay = await test_client.post(f"{root}/reconcile", headers=headers)
    assert replay.status_code == 200 and replay.json() == result.json()
    assert (await test_client.post(f"{root}/cancel", headers=headers)).json()["state"] == "succeeded"
    db_session.expire_all()
    persisted = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert persisted.provider_operation_id == membership_id and persisted.receipt["peer_receipt"]["provider_operation_id"] == membership_id
    assert await db_session.scalar(select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == "team-callback")) == 1


@pytest.mark.asyncio
async def test_reconcile_after_gateway_restart(test_client, test_engine, db_session, source_member, app, admin_token_context, monkeypatch):
    operation_id, _, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    await assign_callback_team(test_client, app, source_member, admin_token_context)
    membership_id = await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback"))
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    operation.state = "outcome_unknown"
    await db_session.commit()

    async def no_repeat(*args, **kwargs):
        pytest.fail("Recovery must observe the existing admin effect, not write it again")

    monkeypatch.setattr("src.admin.team_memberships.add_membership", no_repeat)
    root = f"{recovery_path(callback)}/{operation_id}"
    headers = {"Idempotency-Key": "restart_reconcile_123", "If-Profile-Revision": "1"}
    async with restarted_gateway(test_engine, source_member) as restarted:
        before = await restarted.get(root)
        assert before.status_code == 200 and before.json()["state"] == "outcome_unknown", before.text
        recovered = await restarted.post(f"{root}/reconcile", headers=headers)
        assert recovered.status_code == 202 and recovered.json()["state"] == "succeeded", recovered.text
        assert (await restarted.post(f"{root}/reconcile", headers=headers)).json() == recovered.json()
    async with restarted_gateway(test_engine, source_member) as restarted_again:
        assert (await restarted_again.get(root)).json()["state"] == "succeeded"
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as independent:
        persisted = await independent.get(ProductOperation, (source_member.org_id, operation_id))
        assert persisted.provider_operation_id == membership_id and persisted.state == "succeeded"
        count = await independent.scalar(select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == "team-callback"))
        assert count == 1


@pytest.mark.asyncio
async def handoff_recovery(test_client, test_engine, db_session, source_member, app, admin_token_context, service_account_token_context, monkeypatch):
    operation_id, _, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    await assign_callback_team(test_client, app, source_member, admin_token_context)
    membership_id = await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback"))
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    operation.state = "outcome_unknown"
    await db_session.commit()
    root = recovery_path(callback)
    session_id = operation.session_id
    session_root = f"{root.removesuffix('/operations')}/onboarding-sessions/{session_id}"
    headers = {"Idempotency-Key": "recovery_scenario_123", "If-Profile-Revision": "1"}
    async with restarted_gateway(test_engine, source_member) as recovered_app:
        recovered = await recovered_app.post(f"{root}/{operation_id}/reconcile", headers=headers)
        assert recovered.status_code == 202 and recovered.json()["state"] == "succeeded", recovered.text
        assert (await recovered_app.post(f"{root}/{operation_id}/reconcile", headers=headers)).json() == recovered.json()
        assert (await recovered_app.get(session_root)).json()["state"] == "open"
        connection = await recovered_app.post(
            f"{session_root}/handoffs",
            json={"target": "oauth_connection", "action": "connect", "provider": "example"},
            headers={**headers, "Idempotency-Key": "recovery_connection_123"},
        )
        assert connection.status_code == 202, connection.text
        cancelled_id = connection.json()["id"]
        cancelled = await recovered_app.post(f"{root}/{cancelled_id}/cancel", headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled", cancelled.text
    producer = service_account_token_context.model_copy(
        update={"user_id": PRODUCER, "org_id": source_member.org_id, "token_scopes": f"product-callback/{cancelled_id}", "token_use": "access"}
    )
    stale_receipt = {**callback["receipt"], "provider_operation_id": "not-created", "status": "succeeded"}
    # A fresh application must observe the committed recovery/cancellation state.
    # Both caller types use that application, as they do in production: changing
    # the callback principal does not require rebuilding every Gateway router.
    async with restarted_gateway(test_engine, source_member, callback_actor=producer) as restarted_again:
        assert (await restarted_again.get(f"{root}/{operation_id}")).json()["state"] == "succeeded"
        assert (await restarted_again.get(f"{root}/{cancelled_id}")).json()["state"] == "cancelled"
        assert (await restarted_again.post(f"{root}/{cancelled_id}/reconcile", headers=headers)).json()["state"] == "cancelled"
        rejected = await restarted_again.post(
            f"/v1/product-callbacks/{cancelled_id}",
            json={"sequence": 1, "binding_generation": 2, "receipt": stale_receipt},
        )
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "operation_cancelled"
    await revoke_platform_admin(db_session, source_member)
    db_session.add(TenantMembership(user_id=source_member.user_id, tenant_id=source_member.org_id, revoked_at=datetime.now(UTC)))
    await db_session.commit()
    # Restart again after revocation, preserving fresh-application access checks for
    # both the member and callback producer with the same persisted database.
    async with restarted_gateway(test_engine, source_member, callback_actor=producer) as revoked_app:
        assert (await revoked_app.get(f"{root}/{operation_id}")).status_code == 404
        assert (await revoked_app.post(f"{root}/{cancelled_id}/reconcile", headers=headers)).status_code == 404
        assert (
            await revoked_app.post(
                f"/v1/product-callbacks/{cancelled_id}",
                json={"sequence": 1, "binding_generation": 2, "receipt": stale_receipt},
            )
        ).status_code == 404
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as independent:
        original = await independent.get(ProductOperation, (source_member.org_id, operation_id))
        fenced = await independent.get(ProductOperation, (source_member.org_id, cancelled_id))
        assert original.state == "succeeded" and original.provider_operation_id == membership_id
        assert fenced.state == "cancelled" and fenced.callback_sequence == 0 and fenced.receipt is None
        assert await independent.scalar(select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == "team-callback")) == 1


@pytest.mark.asyncio
async def test_cancel_fences_callbacks_and_revoked_actor(test_client, db_session, source_member, app, service_account_token_context, monkeypatch):
    operation_id, callback_path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    root = f"{recovery_path(callback)}/{operation_id}"
    headers = {"Idempotency-Key": "cancel_pending_handoff_123", "If-Profile-Revision": "1"}
    wrong_revision = await test_client.post(f"{root}/cancel", headers={**headers, "If-Profile-Revision": "2"})
    assert wrong_revision.status_code == 412
    cancelled = await test_client.post(f"{root}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled", cancelled.text
    assert (await test_client.post(f"{root}/cancel", headers=headers)).json() == cancelled.json()
    assert (await test_client.post(f"{root}/reconcile", headers=headers)).json()["state"] == "cancelled"
    producer = service_account_token_context.model_copy(
        update={"user_id": PRODUCER, "org_id": source_member.org_id, "token_scopes": f"product-callback/{operation_id}", "token_use": "access"}
    )
    app.dependency_overrides[get_callback_user] = lambda: producer
    stale = await test_client.post(callback_path, json=callback)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "callback_scope_mismatch"
    fenced = await test_client.post(callback_path, json={**callback, "binding_generation": 2})
    assert fenced.status_code == 409 and fenced.json()["detail"]["code"] == "operation_cancelled"
    app.dependency_overrides[get_current_user] = lambda: source_member
    await revoke_platform_admin(db_session, source_member)
    db_session.add(TenantMembership(user_id=source_member.user_id, tenant_id=source_member.org_id, revoked_at=datetime.now(UTC)))
    await db_session.commit()
    assert (await test_client.get(root)).status_code == 404
    assert (await test_client.post(f"{root}/reconcile", headers=headers)).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: producer
    assert (await test_client.post(callback_path, json={**callback, "binding_generation": 2})).status_code == 404
    db_session.expire_all()
    persisted = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert persisted.state == "cancelled" and persisted.callback_sequence == 0 and persisted.receipt is None
    assert await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback")) is None


@pytest.mark.asyncio
async def test_cancel_preserves_committed_effect_for_reconciliation(test_client, db_session, source_member, app, admin_token_context, monkeypatch):
    operation_id, _, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    await assign_callback_team(test_client, app, source_member, admin_token_context)
    membership_id = await db_session.scalar(select(TeamMembership.id).where(TeamMembership.team_id == "team-callback"))
    root = f"{recovery_path(callback)}/{operation_id}"
    headers = {"Idempotency-Key": "cancel_after_effect_123", "If-Profile-Revision": "1"}
    cancelled = await test_client.post(f"{root}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    db_session.expire_all()
    persisted = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert persisted.provider_operation_id == membership_id
    reconciled = await test_client.post(f"{root}/reconcile", headers={**headers, "Idempotency-Key": "after_cancel_reconcile_123"})
    assert reconciled.status_code == 202 and reconciled.json()["state"] == "succeeded", reconciled.text
    assert await db_session.scalar(select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == "team-callback")) == 1
