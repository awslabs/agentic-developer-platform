"""Registered producer reads current, selected human membership, not token roles."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.internal import (
    domain_current_identity,
    domain_operation_approval,
    domain_operation_dispatch,
    domain_operation_routes,
    domain_operation_runtime,
)
from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity


@pytest.fixture
async def identity_client(db_session, monkeypatch):
    db_session.add_all([Organization(id="O1", name="First"), Organization(id="O2", name="Second")])
    await db_session.flush()
    first = User(id="member-one", org_id="O1", team_id="", email="same@example.test", cognito_sub="immutable-sub")
    second = User(id="member-two", org_id="O2", team_id="", email="same@example.test")
    db_session.add_all([first, second])
    await db_session.flush()
    first_member = TenantMembership(user_id=first.id, tenant_id="O1", is_active=True)
    second_member = TenantMembership(user_id=second.id, tenant_id="O2", is_active=False)
    db_session.add_all(
        [
            first_member,
            second_member,
            UserIdentity(
                user_id=second.id,
                org_id="O2",
                team_id="",
                provider="cognito",
                provider_user_id="immutable-sub",
                verification_method="org_placement",
            ),
        ]
    )
    await db_session.commit()

    state = {"selected": "O1", "enabled": True, "registry": "registered"}
    cognito = MagicMock()
    cognito.list_users.return_value = {"Users": [{"Username": "login"}]}
    cognito.admin_get_user.side_effect = lambda **kwargs: {
        "Enabled": state["enabled"],
        "UserAttributes": [
            {"Name": "sub", "Value": "immutable-sub"},
            {"Name": "custom:org_id", "Value": state["selected"]},
        ],
    }
    monkeypatch.setattr(domain_current_identity, "cognito_user_pool_id", lambda: "pool-test")
    monkeypatch.setattr(domain_current_identity, "aws_client", lambda service: cognito)
    monkeypatch.setattr(
        domain_operation_routes,
        "binding_for",
        lambda domain, org_id: SimpleNamespace(adp_org_id=org_id, producer_registry_id="registered"),
    )
    monkeypatch.setattr(domain_operation_routes, "current_registry", lambda request, scope: state["registry"])

    app = FastAPI()
    app.include_router(domain_operation_routes.router)
    app.dependency_overrides[verify_internal_or_irsa] = lambda: None

    async def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, state, first_member, second_member, cognito


def payload(org_id="O1", *, subject="immutable-sub", principal_type="human"):
    return {"domain": "superplane", "org_id": org_id, "subject": subject, "principal_type": principal_type}


async def test_selected_organization_only_and_switch_does_not_revoke_membership(identity_client):
    client, state, first, second, cognito = identity_client
    endpoint = "/internal/v1/controller-execution/current-identity"
    response = await client.post(endpoint, json=payload())
    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "subject": "immutable-sub",
        "principal_type": "human",
        "adp_org_id": "O1",
        "membership_id": first.id,
        "active": True,
        "enabled": True,
    }
    assert (await client.post(endpoint, json=payload("O2"))).status_code == 403
    state["selected"] = "O2"
    result = await client.post(endpoint, json=payload("O2"))
    assert result.status_code == 200 and result.json()["membership_id"] == second.id
    assert not second.is_active
    assert (await client.post(endpoint, json=payload())).status_code == 403
    assert cognito.admin_get_user.call_count == 4


@pytest.mark.parametrize("change", ["revoked", "disabled", "no-membership", "service", "substituted", "registry", "absent-pool", "unavailable"])
async def test_no_unproven_authority(identity_client, monkeypatch, change):
    client, state, first, _, cognito = identity_client
    if change == "revoked":
        first.revoked_at = datetime.now(UTC)
    elif change == "disabled":
        state["enabled"] = False
    elif change == "no-membership":
        first.tenant_id = "O2"
    elif change == "registry":
        state["registry"] = "different"
    elif change == "absent-pool":
        monkeypatch.setattr(domain_current_identity, "cognito_user_pool_id", lambda: "")
    elif change == "unavailable":
        cognito.admin_get_user.side_effect = RuntimeError("unavailable")
    request = payload(
        subject="different-sub" if change == "substituted" else "immutable-sub",
        principal_type="service" if change == "service" else "human",
    )
    response = await client.post("/internal/v1/controller-execution/current-identity", json=request)
    assert response.status_code == (503 if change in {"absent-pool", "unavailable"} else 403)
    if change in {"service", "registry"}:
        cognito.admin_get_user.assert_not_called()


async def test_paid_execution_rechecks_recorded_requester_and_deciding_approver(
    identity_client, db_session, monkeypatch
):
    _, state, requester_membership, _, cognito = identity_client
    approver = User(
        id="approver-member", org_id="O1", team_id="", email="approver@example.test",
        cognito_sub="approver-sub",
    )
    db_session.add(approver)
    await db_session.flush()
    approver_membership = TenantMembership(user_id=approver.id, tenant_id="O1", is_active=True)
    db_session.add(approver_membership)
    await db_session.commit()

    enabled = {"immutable-sub": True, "approver-sub": True}
    cognito.list_users.side_effect = lambda **kwargs: {
        "Users": [{"Username": kwargs["Filter"].split('"')[1]}]
    }
    cognito.admin_get_user.side_effect = lambda **kwargs: {
        "Enabled": enabled[kwargs["Username"]],
        "UserAttributes": [
            {"Name": "sub", "Value": kwargs["Username"]},
            {"Name": "custom:org_id", "Value": state["selected"]},
        ],
    }

    @asynccontextmanager
    async def membership_session():
        yield db_session

    monkeypatch.setattr(domain_current_identity, "get_session_factory", lambda: membership_session)
    monkeypatch.setattr(domain_operation_dispatch, "harness", lambda name: SimpleNamespace(
        decode_payload=lambda raw: raw, payload_digest=lambda request: "digest",
    ))

    async def current_approval(connection, operation, request):
        return datetime.now(UTC)

    monkeypatch.setattr(domain_operation_approval, "current_approval", current_approval)
    operation = {
        "operation_id": "original", "org_id": "domain-org", "requester": "immutable-sub",
        "approved_by": "approver-sub", "plan_digest": "digest", "request_payload": "original",
        "budget_state": "confirmed", "reservation_state": "confirmed",
    }

    class PaidConnection:
        async def fetchrow(self, statement, *args):
            return operation

    binding = SimpleNamespace(org_id="domain-org", adp_org_id="O1", current_identity_enforced=True)

    async def permitted():
        return await domain_operation_dispatch.paid_operation(
            binding, "original", connection=PaidConnection(), require_current=True,
        )

    assert (await permitted())["approved_by"] == "approver-sub"
    assert cognito.admin_get_user.call_count == 2
    approver_membership.revoked_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(HTTPException) as revoked:
        await permitted()
    assert revoked.value.status_code == 403
    approver_membership.revoked_at = None
    enabled["immutable-sub"] = False
    await db_session.commit()
    with pytest.raises(HTTPException) as disabled:
        await permitted()
    assert disabled.value.status_code == 403
    enabled["immutable-sub"] = True
    requester_membership.revoked_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(HTTPException) as removed:
        await permitted()
    assert removed.value.status_code == 403

    requester_membership.revoked_at = None
    await db_session.commit()
    cognito.admin_get_user.side_effect = RuntimeError("provider unavailable")
    with pytest.raises(HTTPException) as unavailable:
        await permitted()
    assert unavailable.value.status_code == 503

    binding.current_identity_enforced = False
    assert (await permitted())["approved_by"] == "approver-sub"


async def test_recovery_worker_rechecks_persisted_humans_before_a_protected_call(monkeypatch):
    from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION

    original = {
        "domain": "superplane", "org_id": "domain-org", "operation_id": "original",
        "job_id": "job", "attempt_id": "attempt", "workspace_id": "workspace", "mode": "recovery",
    }
    operation = {
        **original, "requester": "original-requester", "approved_by": "deciding-approver",
    }
    binding = SimpleNamespace(
        adp_org_id="O1", org_id="domain-org", repo="org/repo", current_identity_enforced=True,
    )
    record = SimpleNamespace(principal="worker", tenant_id="O1")
    grant = SimpleNamespace(
        principal="worker", authority=SimpleNamespace(kind=AUTHORITY_PAID_DOMAIN_OPERATION),
        repo_scope=frozenset({"org/repo"}),
    )
    monkeypatch.setattr(domain_operation_runtime, "metadata", lambda store, record: original)
    monkeypatch.setattr(domain_operation_runtime, "binding_for", lambda domain, org_id: binding)

    async def read_operation(bound, operation_id, **kwargs):
        return operation

    monkeypatch.setattr(domain_operation_runtime, "paid_operation", read_operation)
    checked = []

    async def check_humans(persisted, *, adp_org_id):
        checked.append((persisted, adp_org_id))
        raise HTTPException(403, "approver was removed")

    monkeypatch.setattr(domain_current_identity, "revalidate_original_humans", check_humans)
    with pytest.raises(HTTPException) as refused:
        await domain_operation_runtime.validate_paid_execution(record, grant, store=object())
    assert refused.value.status_code == 403
    assert checked == [(operation, "O1")]
    assert (await domain_operation_runtime.validate_paid_execution(
        record, grant, store=object(), allow_terminal=True,
    )) == (binding, original, operation)
