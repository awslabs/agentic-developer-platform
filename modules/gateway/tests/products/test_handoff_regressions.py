"""Regressions for expired callbacks and exact native effect readback."""

from datetime import UTC, datetime, timedelta

import pytest

from src.auth.dependencies import get_current_user
from src.product_onboarding.callback_auth import PRODUCER, get_callback_user
from src.products.models import ProductOnboardingSession, ProductOperation
from src.shared.models.vault import ChannelTenantMap, UserCredential
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i12 import callback_fixture


@pytest.mark.parametrize("deadline_offset", [-1, 0, 1])
@pytest.mark.parametrize("prior_receipt", [False, True])
async def test_callback_deadline_preserves_persisted_data(
    test_client,
    db_session,
    source_member,
    app,
    service_account_token_context,
    monkeypatch,
    deadline_offset,
    prior_receipt,
):
    operation_id, path, callback = await callback_fixture(test_client, db_session, source_member, monkeypatch)
    producer = service_account_token_context.model_copy(
        update={"user_id": PRODUCER, "org_id": source_member.org_id, "token_scopes": f"product-callback/{operation_id}", "token_use": "access"}
    )
    app.dependency_overrides[get_callback_user] = lambda: producer
    if prior_receipt:
        running = {**callback, "sequence": 1, "receipt": {**callback["receipt"], "status": "running"}}
        assert (await test_client.post(path, json=running)).status_code == 200
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    session_id = operation.session_id
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    monkeypatch.setattr("src.product_onboarding.callbacks.utcnow", lambda: now)
    operation.expires_at = now + timedelta(seconds=deadline_offset)
    await db_session.commit()
    await db_session.refresh(operation)
    before = (operation.state, operation.callback_sequence, operation.receipt, operation.provider_operation_id, operation.updated_at)
    result = await test_client.post(path, json=callback)
    if deadline_offset > 0:
        assert result.status_code == 200, result.text
        return
    assert result.status_code == 410, result.text
    assert result.json()["detail"]["code"] == "operation_expired"
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert (operation.state, operation.callback_sequence, operation.receipt, operation.provider_operation_id, operation.updated_at) == before
    session = await db_session.get(ProductOnboardingSession, (source_member.org_id, session_id))
    assert session.state == "waiting_external"


async def connection_fixture(test_client, db_session, source_member, target):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}"
    headers = {"Idempotency-Key": "exact_effect_session_123", "If-Profile-Revision": "1"}
    session = await test_client.post(f"{root}/onboarding-sessions", json={}, headers=headers)
    assert session.status_code == 201, session.text
    request = {"target": target, "action": "connect"}
    if target == "oauth_connection":
        request["provider"] = "example"
    handoff = await test_client.post(
        f"{root}/onboarding-sessions/{session.json()['id']}/handoffs",
        json=request,
        headers={**headers, "Idempotency-Key": "exact_effect_handoff_123"},
    )
    assert handoff.status_code == 202, handoff.text
    operation_id = handoff.json()["id"]
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    for ordinal, effect_id in enumerate(("aaa-unrelated-effect", "zzz-intended-effect"), start=1):
        created_at = operation.created_at + timedelta(seconds=ordinal)
        if target == "github_connection":
            effect = ChannelTenantMap(
                id=effect_id,
                provider="github",
                org_id=source_member.org_id,
                provider_scope_id=f"example-account-{ordinal}",
                installation_id=str(700 + ordinal),
                installed_by_user_id=source_member.user_id,
                created_at=created_at,
            )
        else:
            effect = UserCredential(
                id=effect_id,
                org_id=source_member.org_id,
                user_id=source_member.user_id,
                service="aws" if target == "aws_connection" else "example",
                provider="example",
                credential_type="oauth",
                label=effect_id,
                secret_arn="fixture-only",
                created_at=created_at,
                aws_verified_at=created_at if target == "aws_connection" else None,
            )
        db_session.add(effect)
        await db_session.flush()
    await db_session.commit()
    callback = {
        "sequence": 2,
        "binding_generation": 1,
        "receipt": {
            "tenant_id": source_member.org_id,
            "product_id": product_id,
            "project_id": project_id,
            "profile_revision": 1,
            "provider_operation_id": "zzz-intended-effect",
            "status": "succeeded",
        },
    }
    return operation_id, f"{root}/operations/{operation_id}", callback


@pytest.mark.parametrize("target", ["oauth_connection", "aws_connection", "github_connection"])
@pytest.mark.parametrize("completion", ["callback", "running_callback", "reconcile", "cancel_reconcile"])
async def test_connection_completion_uses_exact_effect(
    test_client,
    db_session,
    source_member,
    app,
    service_account_token_context,
    target,
    completion,
):
    operation_id, path, callback = await connection_fixture(test_client, db_session, source_member, target)
    producer = service_account_token_context.model_copy(
        update={"user_id": PRODUCER, "org_id": source_member.org_id, "token_scopes": f"product-callback/{operation_id}", "token_use": "access"}
    )
    app.dependency_overrides[get_callback_user] = lambda: producer
    callback_path = f"/v1/product-callbacks/{operation_id}"
    if completion != "callback":
        running = {**callback, "sequence": 1, "receipt": {**callback["receipt"], "status": "running"}}
        assert (await test_client.post(callback_path, json=running)).status_code == 200
    if completion in ("callback", "running_callback"):
        result = await test_client.post(callback_path, json=callback)
        assert result.status_code == 200, result.text
    else:
        app.dependency_overrides[get_current_user] = lambda: source_member
        headers = {"Idempotency-Key": "exact_effect_reconcile_123", "If-Profile-Revision": "1"}
        if completion == "cancel_reconcile":
            assert (await test_client.post(f"{path}/cancel", headers=headers)).status_code == 200
        result = await test_client.post(f"{path}/reconcile", headers=headers)
        assert result.status_code == 202, result.text
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "succeeded"
    assert operation.provider_operation_id == "zzz-intended-effect"


@pytest.mark.parametrize("target", ["oauth_connection", "aws_connection", "github_connection"])
@pytest.mark.parametrize("ineligible", ["missing", "foreign", "invisible", "unverified"])
async def test_pinned_effect_never_falls_back_to_another_connection(
    test_client,
    db_session,
    source_member,
    app,
    service_account_token_context,
    target,
    ineligible,
):
    operation_id, path, callback = await connection_fixture(test_client, db_session, source_member, target)
    model = ChannelTenantMap if target == "github_connection" else UserCredential
    effect = await db_session.get(model, "zzz-intended-effect")
    if ineligible == "missing":
        await db_session.delete(effect)
    elif ineligible == "foreign":
        effect.org_id = "other-tenant"
    elif target == "github_connection":
        if ineligible == "invisible":
            effect.provider_scope_id = "personal:example-account:other-user"
            effect.installed_by_user_id = "other-user"
        else:
            effect.ownership_disputed = True
    elif ineligible == "invisible":
        effect.user_id = "other-user"
    elif target == "aws_connection":
        effect.aws_verified_at = None
    else:
        effect.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    operation.provider_operation_id = "zzz-intended-effect"
    await db_session.commit()
    observed = await test_client.get(path)
    assert observed.status_code == 200, observed.text
    assert "coverage" not in observed.json()
    producer = service_account_token_context.model_copy(
        update={"user_id": PRODUCER, "org_id": source_member.org_id, "token_scopes": f"product-callback/{operation_id}", "token_use": "access"}
    )
    app.dependency_overrides[get_callback_user] = lambda: producer
    result = await test_client.post(f"/v1/product-callbacks/{operation_id}", json=callback)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "effect_unverified", result.text
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.state == "waiting_external" and operation.callback_sequence == 0 and operation.receipt is None
    assert operation.provider_operation_id == "zzz-intended-effect"
