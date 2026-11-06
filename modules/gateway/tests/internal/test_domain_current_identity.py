"""Registered producer reads current, selected human membership, not token roles."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
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
    monkeypatch.setattr(domain_operation_routes, "aws_client", lambda service: cognito)
    monkeypatch.setattr(
        domain_operation_routes,
        "binding_for",
        lambda domain, org_id: SimpleNamespace(domain=domain, org_id=org_id, adp_org_id=org_id, producer_registry_id="registered"),
    )
    monkeypatch.setattr(domain_operation_routes, "current_registry", lambda request, scope: state["registry"])

    app = FastAPI()
    app.include_router(domain_operation_routes.router)

    async def registered(request: Request):
        request.state.token_context = SimpleNamespace(org_id=state["selected"])

    app.dependency_overrides[verify_internal_or_irsa] = registered

    async def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, state, first_member, second_member, cognito


def payload(org_id="O1", *, subject="immutable-sub", principal_type="human"):
    return {"domain": "superplane", "org_id": org_id, "subject": subject, "principal_type": principal_type}


async def test_selected_organization_requires_committed_active_membership(identity_client):
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
    assert (await client.post(endpoint, json=payload("O2"))).status_code == 403
    assert second.revoked_at is None
    first.is_active = False
    second.is_active = True
    result = await client.post(endpoint, json=payload("O2"))
    assert result.status_code == 200 and result.json()["membership_id"] == second.id
    assert second.is_active
    assert (await client.post(endpoint, json=payload())).status_code == 403
    assert cognito.admin_get_user.call_count == 3


async def test_multi_organization_revocation_and_provider_outage_never_fall_back(identity_client, db_session):
    client, state, first, second, cognito = identity_client
    endpoint = "/internal/v1/controller-execution/current-identity"

    first.is_active = False
    second.is_active = True
    state["selected"] = "O2"
    await db_session.commit()
    response = await client.post(endpoint, json=payload("O2"))
    assert response.status_code == 200
    assert response.json()["membership_id"] == second.id

    second.revoked_at = datetime.now(UTC)
    first.is_active = True
    await db_session.commit()
    assert (await client.post(endpoint, json=payload("O2"))).status_code == 403
    state["selected"] = "O1"
    assert (await client.post(endpoint, json=payload("O2"))).status_code == 403
    response = await client.post(endpoint, json=payload("O1"))
    assert response.status_code == 200
    assert response.json()["membership_id"] == first.id

    original_lookup = cognito.admin_get_user.side_effect
    cognito.admin_get_user.side_effect = RuntimeError("identity provider unavailable")
    assert (await client.post(endpoint, json=payload("O1"))).status_code == 503
    cognito.admin_get_user.side_effect = original_lookup
    assert (await client.post(endpoint, json=payload("O1"))).status_code == 200
    assert (await client.post(endpoint, json=payload("O2"))).status_code == 403


async def test_workspace_switch_projects_selected_tenant_into_both_tokens_and_producer(identity_client, db_session, monkeypatch):
    import importlib.util
    from pathlib import Path

    from src.auth.workspaces import select_workspace
    from src.shared.schemas.auth import TokenContext

    client, state, first, second, cognito = identity_client

    def protected_user(**kwargs):
        return {
            "Username": kwargs["Username"],
            "Enabled": state["enabled"],
            "UserAttributes": [
                {"Name": "sub", "Value": "immutable-sub"},
                {"Name": "custom:org_id", "Value": state["selected"]},
            ],
        }

    cognito.admin_get_user.side_effect = protected_user
    monkeypatch.setattr("boto3.client", lambda service: cognito)
    monkeypatch.setattr("boto3.resource", lambda service: MagicMock())
    trigger_file = Path(__file__).resolve().parents[2] / "infra/modules/cognito/lambda/pre_token_generation.py"
    specification = importlib.util.spec_from_file_location("workspace_selected_pre_token", trigger_file)
    trigger = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(trigger)

    def projected(source):
        event = {
            "version": "3",
            "triggerSource": source,
            "userPoolId": "pool-test",
            "userName": "login",
            "request": {
                "userAttributes": {"sub": "immutable-sub", "custom:org_id": "other"},
                "clientMetadata": {"org_id": "other"},
            },
            "response": {},
        }
        overrides = trigger.handler(event, None)["response"]["claimsAndScopeOverrideDetails"]
        return {token: overrides[token]["claimsToAddOrOverride"]["custom:org_id"] for token in ("idTokenGeneration", "accessTokenGeneration")}

    endpoint = "/internal/v1/controller-execution/current-identity"
    assert projected("TokenGeneration_Authentication") == {"idTokenGeneration": "O1", "accessTokenGeneration": "O1"}
    assert (await client.post(endpoint, json=payload("O1"))).status_code == 200

    class Claims:
        async def set(self, subject, values):
            assert subject == "immutable-sub"
            previous = {"custom:org_id": state["selected"]}
            state["selected"] = values["custom:org_id"]
            return previous, values

    context = TokenContext(
        user_id="immutable-sub",
        org_id="O1",
        team_id="",
        department_id="",
        account_type="human",
        cognito_username="login",
        expires_at=datetime.now(UTC),
    )
    selected = await select_workspace(db_session, context, "O2", Claims())
    assert selected.org_id == "O2" and selected.is_current
    assert first.is_active is False and second.is_active is True
    for source in ("TokenGeneration_HostedAuth", "TokenGeneration_RefreshTokens"):
        assert projected(source) == {"idTokenGeneration": "O2", "accessTokenGeneration": "O2"}
    assert (await client.post(endpoint, json=payload("O1"))).status_code == 403
    response = await client.post(endpoint, json=payload("O2"))
    assert response.status_code == 200
    assert response.json()["membership_id"] == second.id


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


@pytest.mark.parametrize("distinct", [True, False])
async def test_paid_execution_rechecks_recorded_requester_and_deciding_approver(identity_client, db_session, monkeypatch, distinct):
    _, state, requester_membership, _, cognito = identity_client
    approver = User(
        id="approver-member",
        org_id="O1",
        team_id="",
        email="approver@example.test",
        cognito_sub="approver-sub",
    )
    db_session.add(approver)
    await db_session.flush()
    approver_membership = TenantMembership(user_id=approver.id, tenant_id="O1", is_active=True)
    db_session.add(approver_membership)
    await db_session.commit()

    enabled = {"immutable-sub": True, "approver-sub": True}
    cognito.list_users.side_effect = lambda **kwargs: {"Users": [{"Username": kwargs["Filter"].split('"')[1]}]}
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
    monkeypatch.setattr(
        domain_operation_dispatch,
        "harness",
        lambda name: SimpleNamespace(
            decode_payload=lambda raw: raw,
            payload_digest=lambda request: "digest",
        ),
    )

    async def current_approval(connection, operation, request):
        return datetime.now(UTC)

    monkeypatch.setattr(domain_operation_approval, "current_approval", current_approval)
    operation = {
        "operation_id": "original",
        "org_id": "domain-org",
        "requester": "immutable-sub",
        "approved_by": "approver-sub" if distinct else "immutable-sub",
        "requires_distinct_approver": distinct,
        "plan_digest": "digest",
        "request_payload": "original",
        "budget_state": "confirmed",
        "reservation_state": "confirmed",
        "reservation_id": "reservation",
        "workspace_id": "workspace",
        "job_id": "job",
        "attempt_id": "attempt",
        "max_resource_units": 0,
        "max_runtime_seconds": 3600,
        "max_cost_micros": 0,
    }

    class PaidConnection:
        async def fetchrow(self, statement, *args):
            assert "a.requires_distinct_approver" in statement
            return operation

    @asynccontextmanager
    async def domain_connection(_):
        class DomainConnection:
            async def fetchval(self, *_):
                return "O1"

            async def fetchrow(self, *_):
                return {"state": "confirmed"}

        yield DomainConnection()

    monkeypatch.setattr(domain_operation_dispatch, "domain_connect", domain_connection)
    binding = SimpleNamespace(org_id="domain-org", adp_org_id="O1", current_identity_enforced=True)

    async def permitted():
        return await domain_operation_dispatch.paid_operation(
            binding,
            "original",
            connection=PaidConnection(),
            require_current=True,
        )

    assert (await permitted())["approved_by"] == operation["approved_by"]
    assert cognito.admin_get_user.call_count == (2 if distinct else 1)
    deciding_membership = approver_membership if distinct else requester_membership
    deciding_membership.revoked_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(HTTPException) as revoked:
        await permitted()
    assert revoked.value.status_code == 403
    deciding_membership.revoked_at = None
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
    assert (await permitted())["approved_by"] == operation["approved_by"]


@pytest.mark.parametrize("policy", [True, None, "false", 0, "missing"])
async def test_same_human_refused_without_explicit_recorded_streamlined_policy(monkeypatch, policy):
    from unittest.mock import AsyncMock

    check = AsyncMock()
    monkeypatch.setattr(domain_current_identity, "current_human_identity", check)
    operation = {"requester": "human", "approved_by": "human"}
    if policy != "missing":
        operation["requires_distinct_approver"] = policy
    with pytest.raises(HTTPException) as refused:
        await domain_current_identity.revalidate_original_humans(operation, adp_org_id="O1")
    assert refused.value.status_code == 403
    check.assert_not_awaited()


async def test_recovery_worker_rechecks_persisted_humans_before_a_protected_call(monkeypatch):
    from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION

    original = {
        "domain": "superplane",
        "org_id": "domain-org",
        "operation_id": "original",
        "job_id": "job",
        "attempt_id": "attempt",
        "workspace_id": "workspace",
        "mode": "recovery",
    }
    operation = {
        **original,
        "requester": "original-requester",
        "approved_by": "deciding-approver",
    }
    binding = SimpleNamespace(
        adp_org_id="O1",
        org_id="domain-org",
        repo="org/repo",
        current_identity_enforced=True,
    )
    record = SimpleNamespace(principal="worker", tenant_id="O1")
    grant = SimpleNamespace(
        principal="worker",
        authority=SimpleNamespace(kind=AUTHORITY_PAID_DOMAIN_OPERATION),
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
    assert (
        await domain_operation_runtime.validate_paid_execution(
            record,
            grant,
            store=object(),
            allow_terminal=True,
        )
    ) == (binding, original, operation)


async def test_identity_provider_readiness_requires_registered_producer(identity_client, monkeypatch):
    client, state, _, _, cognito = identity_client
    route = "/internal/v1/controller-execution/current-identity/readiness"
    monkeypatch.setattr(domain_operation_routes, "cognito_user_pool_id", lambda: "pool-test")
    original_binding = domain_operation_routes.binding_for

    def selected_binding(domain, org_id):
        if (domain, org_id) != ("superplane", "O1"):
            raise HTTPException(503, "domain operation binding unavailable")
        return original_binding(domain, org_id)

    monkeypatch.setattr(domain_operation_routes, "binding_for", selected_binding)
    response = await client.post(route, json={"domain": "superplane", "org_id": "O1"})
    assert response.status_code == 200
    assert response.json() == {"version": 1, "domain": "superplane", "org_id": "O1", "adp_org_id": "O1"}
    assert (await client.post(route, json={"domain": "superplane", "org_id": "O2"})).status_code == 503
    state["registry"] = "different"
    assert (await client.post(route, json={"domain": "superplane", "org_id": "O1"})).status_code == 403
    state["registry"] = "registered"
    cognito.list_users.side_effect = RuntimeError("upstream offline")
    assert (await client.post(route, json={"domain": "superplane", "org_id": "O1"})).status_code == 503
    cognito.list_users.side_effect = None
    monkeypatch.setattr(domain_operation_routes, "cognito_user_pool_id", lambda: "")
    assert (await client.post(route, json={"domain": "superplane", "org_id": "O1"})).status_code == 503


async def test_signed_domain_reader_reaches_registered_gateway_identity(identity_client, monkeypatch):
    import hmac
    import json
    from dataclasses import asdict, replace
    from pathlib import Path

    import botocore.auth
    import botocore.awsrequest
    import httpx
    from botocore.credentials import Credentials

    from src.auth.agent_registry import AgentRegistryService
    from src.internal import domain_operation_store
    from src.internal.domain_operation_runtime import current_registry

    modules = Path(__file__).resolve().parents[3]
    for dependency in (
        "harness/jobs",
        "domain-apps/superplane/contracts",
        "domain-apps/superplane/auth",
        "domain-apps/superplane/src/superplane-api",
    ):
        monkeypatch.syspath_prepend(str(modules / dependency))
    from app.adapters.operation_dispatch import ProducerTransport
    from app.current_identity import IdentityDenied, ProducerIdentityReader, require_current_identity

    client, state, first, _, cognito = identity_client
    gateway_app = client._transport.app
    gateway_app.dependency_overrides.pop(verify_internal_or_irsa)
    monkeypatch.setattr(domain_operation_routes, "binding_for", domain_operation_store.binding_for)
    monkeypatch.setattr(domain_operation_routes, "current_registry", current_registry)
    monkeypatch.setattr(domain_operation_routes, "cognito_user_pool_id", lambda: "pool-test")

    binding = domain_operation_store.DomainBinding(
        "superplane",
        "domain-one",
        "O1",
        "registered",
        "worker",
        "secret",
        "superplane",
        "https://sqs.us-east-1.amazonaws.com/123456789012/fixture",
        "namespace",
        "service",
        "worker",
        ("sha256:" + "a" * 64,),
        "org/repo",
        "https://domain.example",
        "observer-secret",
        domain_database_secret_id="domain-secret",
        domain_database_schema="domain_data",
    )
    monkeypatch.setenv(
        "ADP_DOMAIN_OPERATION_BINDINGS",
        json.dumps(
            [
                asdict(binding),
                asdict(replace(binding, org_id="domain-two", adp_org_id="O2")),
            ]
        ),
    )
    role = "arn:aws:iam::123456789012:role/registered-producer"
    role_id = "AROA11111111111111111"
    item = {
        "agent_id": {"S": "registered"},
        "role_arn": {"S": role},
        "iam_role_id": {"S": role_id},
        "owner": {"S": "webhook-terraform-domain-operations-v1"},
        "agent_name": {"S": "producer"},
        "org_id": {"S": "O1"},
        "team_id": {"S": "team"},
        "scope": {"S": "internal"},
        "status": {"S": "active"},
        "credential_scopes": {"SS": ["domain:operation-producer"]},
    }
    registry = AgentRegistryService(table_name="identity-registry")
    registry._dynamodb = MagicMock()
    registry._dynamodb.query.return_value = {"Items": [item]}
    registry._dynamodb.get_item.return_value = {"Item": item}
    registry._iam = MagicMock()
    registry._iam.get_role.return_value = {"Role": {"Arn": role, "RoleId": role_id}}
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: registry)
    settings = SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret="edge-proof", internal_api_key="")
    monkeypatch.setattr("src.internal.auth_deps.get_settings", lambda: settings)
    monkeypatch.setattr("src.auth.middleware.get_settings", lambda: settings)
    credentials = Credentials("fixture-access", "fixture-signing")
    forwarded = []
    backend = AsyncClient(transport=ASGITransport(app=gateway_app), base_url="https://gateway.example")

    async def trusted_edge(request):
        expected = botocore.awsrequest.AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
        )
        timestamp = request.headers.get("X-Amz-Date")
        if not timestamp:
            return httpx.Response(403)
        expected.context["timestamp"] = timestamp
        signer = botocore.auth.SigV4Auth(credentials, "execute-api", "us-east-1")
        signer._modify_request_before_signing(expected)
        digest = signer.signature(signer.string_to_sign(expected, signer.canonical_request(expected)), expected)
        if not hmac.compare_digest(request.headers.get("Authorization", "").split("Signature=")[-1], digest):
            return httpx.Response(403)
        forwarded.append(request.url.path)
        result = await backend.post(
            request.url.path,
            content=request.content,
            headers={
                "Content-Type": "application/json",
                "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/registered-producer/pod",
                "X-Adp-Edge-Provenance": "edge-proof",
            },
        )
        return httpx.Response(result.status_code, content=result.content)

    producer = ProducerTransport(
        "https://gateway.example",
        "us-east-1",
        session=SimpleNamespace(get_credentials=lambda: credentials),
        client=AsyncClient(transport=httpx.MockTransport(trusted_edge)),
    )
    try:
        reader_one = ProducerIdentityReader(producer, domain_org_id="domain-one", adp_org_id="O1")
        assert (
            await require_current_identity(reader_one, subject="immutable-sub", principal_type="human", adp_org_id="O1")
        ).membership_id == first.id
        assert await producer.post("/current-identity/readiness", {"domain": "superplane", "org_id": "domain-one"}, distinguish_denial=True) == {
            "version": 1,
            "domain": "superplane",
            "org_id": "domain-one",
            "adp_org_id": "O1",
        }
        state["selected"] = "O2"
        with pytest.raises(IdentityDenied):
            await require_current_identity(reader_one, subject="immutable-sub", principal_type="human", adp_org_id="O1")
        reader_two = ProducerIdentityReader(producer, domain_org_id="domain-two", adp_org_id="O2")
        before = cognito.admin_get_user.call_count
        with pytest.raises(IdentityDenied):
            await require_current_identity(reader_two, subject="immutable-sub", principal_type="human", adp_org_id="O2")
        assert cognito.admin_get_user.call_count == before
        state["selected"] = "O1"
        item["status"] = {"S": "revoked"}
        with pytest.raises(IdentityDenied):
            await require_current_identity(reader_one, subject="immutable-sub", principal_type="human", adp_org_id="O1")
        item["status"] = {"S": "active"}
        assert registry.get_agent_by_role_arn(role) is not None
        registry._iam.get_role.return_value["Role"]["RoleId"] = "AROA22222222222222222"
        before = cognito.admin_get_user.call_count
        with pytest.raises(IdentityDenied):
            await require_current_identity(reader_one, subject="immutable-sub", principal_type="human", adp_org_id="O1")
        assert cognito.admin_get_user.call_count == before
        registry._iam.get_role.assert_called_with(RoleName="registered-producer")
        assert all(call.kwargs["ConsistentRead"] for call in registry._dynamodb.get_item.call_args_list)
        assert len(forwarded) == 6
        forged = await producer.client.post(
            "https://gateway.example/internal/v1/controller-execution/current-identity",
            json={"domain": "superplane", "org_id": "domain-one"},
        )
        assert forged.status_code == 403
        assert len(forwarded) == 6
    finally:
        await producer.aclose()
        await backend.aclose()


async def test_org_local_membership_wins_over_legacy_home_membership(identity_client, db_session):
    client, state, first, second, _ = identity_client
    legacy = TenantMembership(user_id="member-one", tenant_id="O2", is_active=False)
    db_session.add(legacy)
    await db_session.commit()
    state["selected"] = "O2"
    first.is_active = False
    second.is_active = True
    endpoint = "/internal/v1/controller-execution/current-identity"
    response = await client.post(endpoint, json=payload("O2"))
    assert response.status_code == 200
    assert response.json()["membership_id"] == second.id
    second.revoked_at = datetime.now(UTC)
    await db_session.commit()
    assert (await client.post(endpoint, json=payload("O2"))).status_code == 403


async def test_legacy_revocation_does_not_fall_back_to_local_membership(identity_client, db_session):
    client, state, _, _, _ = identity_client
    db_session.add(TenantMembership(user_id="member-one", tenant_id="O2", is_active=False, revoked_at=datetime.now(UTC)))
    await db_session.commit()
    state["selected"] = "O2"
    response = await client.post("/internal/v1/controller-execution/current-identity", json=payload("O2"))
    assert response.status_code == 403
