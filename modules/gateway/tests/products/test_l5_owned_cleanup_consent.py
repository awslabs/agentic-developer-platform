"""Real owner rows and setup leases, with only external provider I/O replaced."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from l5_services.api.bindings import _digest, _intent_hash
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.profile_claim import ProfileClaim
from l5_services.models.setup import SetupIntent, SetupWork
from sqlalchemy import delete, select

from src.auth.aws_connection_authority import connection_binding
from src.auth.dependencies import get_current_user
from src.auth.l5_cleanup_scope import current_cleanup_scope
from src.auth.l5_execution_scope import current_execution_scope
from src.internal.assume_role_routes import get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import CredentialSessionRequest, get_l5_work_authorizer, verify_l5_work
from src.internal.sts_assume_service import AssumeRoleResult
from src.l5_integration import GatewayConfigurationBackend
from src.l5_service_identity import WORKER_IDS
from src.products.models import ProductSourceBinding
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation
from tests.products import test_l5_owner_delegation as fixtures
from tests.products.test_l5_owner_delegation_work import _wa_resources

consent_fixture = fixtures.consent_fixture
l5_owner = fixtures.l5_owner
REAL_AUTHORIZE_SETTINGS = GatewayConfigurationBackend.authorize_settings


@pytest.fixture
async def cleanup_fixture(consent_fixture, db_session, l5_owner, monkeypatch):
    # Restore the real authorization method replaced by the generic fixture.
    monkeypatch.setattr(GatewayConfigurationBackend, "authorize_settings", REAL_AUTHORIZE_SETTINGS)
    fixture = consent_fixture
    credential = fixture["credential"]
    credential.agent_consent = True
    credential.scopes = {**credential.scopes, "source": "imported_role"}
    credential.aws_verified_binding = connection_binding(credential)
    mapping = ChannelTenantMap(
        org_id=l5_owner.org_id, provider="github", provider_scope_id="fixture-org", installation_id="17", ownership_disputed=False
    )
    db_session.add(mapping)
    await db_session.flush()
    source = ProductSourceBinding(
        tenant_id=l5_owner.org_id,
        project_id=fixture["project_id"],
        kind="repository",
        provider="github",
        installation_id=mapping.id,
        namespace="fixture-org",
        native_id="27",
        desired={"source_revision": "a" * 40},
        observed={"source_revision": "a" * 40},
        status="revoked",
    )
    db_session.add(source)
    selected_source = {
        **fixture["reference"],
        "source_connection_id": mapping.id,
        "record_type": "repository_commit",
        "record_id": "27",
        "record_revision": "a" * 40,
    }
    fixture["bindings"] = {}
    wa_resources = await _wa_resources(db_session, l5_owner, fixture, monkeypatch)
    for service in ("security", "well_architected"):
        config, version = fixture["configurations"][service]
        if service == "well_architected":
            version.settings = {
                **version.settings,
                "well_architected": {
                    "pillars": ["SECURITY"],
                    "execution_role_ref": wa_resources["execution_role_ref"],
                    "source_store_ref": wa_resources["source_store_ref"],
                },
            }
        identity = {"account_id": "123456789012", "region": "us-east-1", "role_arn": credential.scopes["role_arn"]}
        if service == "well_architected":
            identity["execution_role_arn"] = version.settings[service]["execution_role_ref"]["record_id"]
        native = {
            **fixture["reference"],
            "record_type": "agent_profile" if service == "well_architected" else "security_space",
            "record_id": "arn:aws:wellarchitected:us-east-1:123456789012:agent-profile/Owned" if service == "well_architected" else "owned-space",
            "record_revision": "a" * 64,
        }
        origin = SetupIntent(
            tenant_id=l5_owner.org_id,
            operation_id=str(uuid4()),
            product_id=fixture["product_id"],
            project_id=fixture["project_id"],
            service_id=service,
            actor_id=l5_owner.user_id,
            idempotency_key="original_create_0001",
            configuration_id=config.configuration_id,
            scope=view(config, version)["scope"],
            request={"action": "create", "configuration_revision": 1},
            binding_generation=1,
            authorization={"actor_id": l5_owner.user_id, "delegation_id": "original-consent", "delegation_revision": 1},
            intent_hash="",
        )
        origin.intent_hash = _intent_hash(origin)
        binding = Binding(
            tenant_id=l5_owner.org_id,
            binding_id=str(uuid4()),
            configuration_id=config.configuration_id,
            product_id=fixture["product_id"],
            project_id=fixture["project_id"],
            service_id=service,
            scope=origin.scope,
            revision=1,
            generation=1,
            state="ready",
            ownership="adp_created",
            connection_ref=fixture["reference"],
            connection_key=_digest(fixture["reference"]),
            native_resource_ref=native,
            native_key=_digest(native),
            provider_identity=identity,
            creation_receipt={
                "operation_id": origin.operation_id,
                "intent_hash": origin.intent_hash,
                "native_resource_ref": native,
                "provider_identity": identity,
            },
            readiness=[],
            gaps=[],
        )
        current = ConfigurationVersion(
            tenant_id=l5_owner.org_id,
            configuration_id=config.configuration_id,
            revision=2,
            settings={**version.settings, "enabled": False, "source_refs": [selected_source], "capabilities": []},
            inherited_fields=[],
            gaps=[],
            state="disabled",
            hash="b" * 64,
        )
        config.current_revision = 2
        db_session.add_all((origin, binding, current))
        if service == "well_architected":
            db_session.add(
                ProfileClaim(
                    service_id=service,
                    native_identity_key=_digest((identity["account_id"], identity["region"], native["record_id"])),
                    tenant_id=l5_owner.org_id,
                    product_id=fixture["product_id"],
                    project_id=fixture["project_id"],
                )
            )
        fixture["configurations"][service] = config, current
        fixture["bindings"][service] = binding
    await db_session.commit()
    return fixture


async def authorize_cleanup(client, db, fixture, service):
    path = fixture["path"] + "/" + service
    query = {"binding_id": fixture["bindings"][service].binding_id, "for_control": True, "control_action": "cleanup"}
    preview = await client.get(path + "/delegation-scope", params=query)
    assert preview.status_code == 200, preview.text
    body = {**query, "scope_digest": preview.json()["scope_digest"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    response = await client.post(
        path + "/delegations", json=body, headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "cleanup_consent_0001"}
    )
    assert response.status_code == 200, response.text
    grant = await db.get(CredentialServiceDelegation, response.json()["delegation_id"], populate_existing=True)
    return path, grant


async def admitted_cleanup(client, db, fixture, owner, service):
    path, grant = await authorize_cleanup(client, db, fixture, service)
    backend = GatewayConfigurationBackend(db, owner)
    binding = fixture["bindings"][service]
    request = {"action": "cleanup", "configuration_revision": 2, "binding_id": binding.binding_id, "owned_cleanup_confirmed": True}
    operation = await backend.start_binding(fixture["product_id"], fixture["project_id"], service, request, "cleanup_operation_0001", '"1"')
    work = await db.scalar(select(SetupWork).where(SetupWork.operation_id == operation.operation_id))
    work.lease_owner, work.fence, work.lease_until = "cleanup-worker", 1, datetime.now(UTC) + timedelta(minutes=5)
    await db.commit()
    await db.refresh(binding)
    body = CredentialSessionRequest(
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        operation_id=operation.operation_id,
        attempt_id=work.work_id,
        connection_ref={**fixture["reference"], "record_type": "credential"},
        lease_owner=work.lease_owner,
        lease_fence=work.fence,
    )
    return SimpleNamespace(path=path, grant=grant, body=body, binding=binding, backend=backend, request=request, work=work, operation=operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["security", "well_architected"])
async def test_disabled_revoked_content_cleanup_has_exact_policy_and_no_execution(test_client, db_session, cleanup_fixture, l5_owner, service):
    selected = await admitted_cleanup(test_client, db_session, cleanup_fixture, l5_owner, service)
    grant = selected.grant
    assert grant.operations == ["binding:cleanup", "credential:session"] and grant.capabilities == [] and len(grant.sources) == 1
    assert not await current_execution_scope(db_session, grant)
    assert await current_cleanup_scope(db_session, grant, admitted=True)
    permit = await get_l5_work_authorizer(db_session)(grant, selected.body)
    assert permit.setup_verified["owned_cleanup"] == grant.execution_scope["owned_resource"]
    policy = json.loads(permit.session_policy)
    expected = (
        {
            "wellarchitected:GetAgentProfile",
            "wellarchitected:ListAgentProfiles",
            "wellarchitected:ListAgentContexts",
            "wellarchitected:DeleteAgentProfile",
        }
        if service == "well_architected"
        else {"securityagent:ListAgentSpaces", "securityagent:BatchGetAgentSpaces", "securityagent:DeleteAgentSpace"}
    )
    assert {action for statement in policy["Statement"] for action in statement["Action"]} == expected
    for statement in policy["Statement"]:
        assert statement["Condition"]["DateLessThan"]
        if any("Delete" in action for action in statement["Action"]):
            assert "*" not in str(statement["Resource"])
    with pytest.raises(HTTPException):
        await verify_l5_work(db_session, grant, selected.body)
    worker = l5_owner.model_copy(update={"account_type": "service", "auth_source": "iam", "agent_registry_id": WORKER_IDS[service]})
    for action in ("credential:session", "product:read", "source:read", "source:pin"):
        assert await service_product_scope(db_session, worker, grant.id, grant.product_id, grant.project_id, action) is None
    for action in ("create", "link", "update", "resume", "check"):
        with pytest.raises(HTTPException):
            await selected.backend.start_binding(
                grant.product_id, grant.project_id, service, {**selected.request, "action": action}, "wrong_action_" + action
            )
    replay = await selected.backend.start_binding(grant.product_id, grant.project_id, service, selected.request, "cleanup_operation_0001", '"1"')
    assert replay.operation_id == selected.operation.operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["security", "well_architected"])
@pytest.mark.parametrize(
    "change", ["owner", "membership", "connection", "receipt", "native", "generation", "adopted", "expired", "revoked", "intent", "lease"]
)
async def test_cleanup_denies_current_authority_and_provenance_changes(test_client, db_session, cleanup_fixture, l5_owner, service, change):
    selected = await admitted_cleanup(test_client, db_session, cleanup_fixture, l5_owner, service)
    config, _ = cleanup_fixture["configurations"][service]
    if change == "owner":
        config.owner_id = "different-owner"
    elif change == "membership":
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
    elif change == "connection":
        cleanup_fixture["credential"].aws_verified_version_id = "changed"
    elif change == "receipt":
        selected.binding.creation_receipt = {**selected.binding.creation_receipt, "intent_hash": "f" * 64}
    elif change == "native":
        selected.binding.native_resource_ref = {**selected.binding.native_resource_ref, "record_id": "other"}
    elif change == "generation":
        selected.binding.generation += 1
    elif change == "adopted":
        selected.binding.ownership = "customer_linked"
    elif change == "expired":
        selected.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "revoked":
        selected.grant.revoked_at = datetime.now(UTC)
    elif change == "intent":
        intent = await db_session.get(SetupIntent, (l5_owner.org_id, selected.operation.operation_id))
        intent.request = {**intent.request, "action": "resume"}
    else:
        selected.work.fence += 1
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(selected.grant, selected.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["security", "well_architected"])
async def test_adopted_resource_cannot_obtain_cleanup_consent(test_client, db_session, cleanup_fixture, service):
    binding = cleanup_fixture["bindings"][service]
    binding.ownership = "customer_linked"
    await db_session.commit()
    response = await test_client.get(
        cleanup_fixture["path"] + "/" + service + "/delegation-scope",
        params={"binding_id": binding.binding_id, "for_control": True, "control_action": "cleanup"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["security", "well_architected"])
@pytest.mark.parametrize("late_change", [None, "membership", "connection"])
async def test_cleanup_mounted_broker_uses_real_work_policy_and_denies_foreign_worker(
    test_client, db_session, cleanup_fixture, l5_owner, app, monkeypatch, service, late_change
):
    selected = await admitted_cleanup(test_client, db_session, cleanup_fixture, l5_owner, service)
    credential = cleanup_fixture["credential"]
    worker = l5_owner.model_copy(
        update={
            "user_id": "iam-agent:" + WORKER_IDS[service],
            "agent_registry_id": WORKER_IDS[service],
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["l5:credential-session"],
        }
    )

    async def verified(request: Request):
        request.state.token_context = worker

    app.dependency_overrides[verify_internal_or_irsa] = verified
    manager = Mock()
    manager.current_version_id.return_value = credential.aws_verified_version_id
    manager.get_secret_at_version.return_value = (
        json.dumps(
            {
                "role_arn": credential.scopes["role_arn"],
                "external_id": credential.aws_external_id,
                "account_id": "123456789012",
                "session_duration_seconds": 900,
                "default_region": "us-east-1",
            }
        ),
        credential.aws_verified_version_id,
    )
    app.dependency_overrides[get_secrets_manager] = lambda: manager
    assumed = Mock(
        return_value=AssumeRoleResult(
            access_key_id="fixture-key",
            secret_access_key="fixture-secret",
            session_token="fixture-token",
            expiration=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            region="us-east-1",
            profile_name="fixture",
        )
    )
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    monkeypatch.setattr("src.internal.assume_role_routes.assume_role", assumed)

    async def provider_identity(*_args):
        if late_change == "membership":
            await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
            await db_session.commit()
        elif late_change == "connection":
            credential.aws_verified_version_id = "changed-during-provider-call"
            await db_session.commit()
        return selected.binding.provider_identity

    monkeypatch.setattr("src.internal.l5_credential_routes.verify_assumed_session", AsyncMock(side_effect=provider_identity))
    response = await test_client.post("/internal/v1/l5/credential-session", json=selected.body.model_dump())
    if late_change is not None:
        assert response.status_code in {403, 409} and assumed.call_count == 1
        assert "access_key_id" not in response.json()
        return
    assert response.status_code == 200, response.text
    assert assumed.call_count == 1 and response.json()["setup_verified"]["authorization"]["control_action"] == "cleanup"
    policy = json.loads(assumed.call_args.kwargs["session_policy"])
    assert all(not any(action.startswith(("s3:", "iam:")) for action in statement["Action"]) for statement in policy["Statement"])
    worker = worker.model_copy(update={"agent_registry_id": WORKER_IDS["devops"]})
    denied = await test_client.post("/internal/v1/l5/credential-session", json=selected.body.model_dump())
    assert denied.status_code == 403 and assumed.call_count == 1
    worker = worker.model_copy(update={"agent_registry_id": WORKER_IDS[service]})
    await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
    await db_session.commit()
    denied = await test_client.post("/internal/v1/l5/credential-session", json=selected.body.model_dump())
    assert denied.status_code == 403 and assumed.call_count == 1


@pytest.mark.asyncio
async def test_wa_cleanup_broker_rechecks_retained_claim(test_client, db_session, cleanup_fixture, l5_owner):
    selected = await admitted_cleanup(test_client, db_session, cleanup_fixture, l5_owner, "well_architected")
    await db_session.execute(delete(ProfileClaim))
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(selected.grant, selected.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["security", "well_architected"])
@pytest.mark.parametrize("state", ["accepted", "failed", "succeeded"])
async def test_owned_cleanup_metadata_survives_disabled_sources_and_expired_revoked_consent(
    test_client, db_session, cleanup_fixture, l5_owner, service, state
):
    path = cleanup_fixture["path"] + "/" + service
    binding = cleanup_fixture["bindings"][service]
    status_path = path + "/bindings/" + binding.binding_id + "/cleanup-status"
    before = await test_client.get(status_path)
    assert before.status_code == 200 and before.headers["etag"] == '"1"'
    selected = await admitted_cleanup(test_client, db_session, cleanup_fixture, l5_owner, service)
    selected.operation.state = state
    selected.binding.state = {"accepted": "cleanup_pending", "failed": "cleanup_failed", "succeeded": "cleaned"}[state]
    if state == "succeeded":
        selected.operation.cleanup_receipt = {"absent": True, "native_resource_ref": binding.native_resource_ref}
    selected.grant.revoked_at = datetime.now(UTC)
    selected.grant.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    cleanup_fixture["credential"].aws_verified_version_id = None
    await db_session.commit()
    poll = await test_client.get(path + "/binding-operations/" + selected.operation.operation_id)
    assert poll.status_code == 200 and poll.json()["state"] == state, poll.text
    metadata = await test_client.get(status_path)
    assert metadata.status_code == 200 and metadata.headers["Cache-Control"] == "no-store", metadata.text
    assert metadata.json()["state"] == selected.binding.state
    assert set(metadata.json()) == {"binding_id", "revision", "generation", "state", "ownership", "owned_resource"}
    assert (await test_client.get(path + "/bindings")).status_code == 404
    assert (await test_client.get(path + "/bindings/" + binding.binding_id)).status_code == 404
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(selected.grant, selected.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["security", "well_architected"])
@pytest.mark.parametrize("change", ["owner", "foreign-user", "membership-admin"])
async def test_cleanup_metadata_requires_current_real_owner(test_client, db_session, cleanup_fixture, l5_owner, app, service, change):
    selected = await admitted_cleanup(test_client, db_session, cleanup_fixture, l5_owner, service)
    if change == "owner":
        cleanup_fixture["configurations"][service][0].owner_id = "other-owner"
    elif change == "foreign-user":
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"user_id": "another-user"})
    else:
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"is_platform_admin": True})
    await db_session.commit()
    for suffix in ("/binding-operations/" + selected.operation.operation_id, "/bindings/" + selected.binding.binding_id + "/cleanup-status"):
        response = await test_client.get(selected.path + suffix)
        assert response.status_code == 404, response.text
        assert "owned_resource" not in response.json() and "cleanup_receipt" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["security", "well_architected"])
async def test_legacy_cleanup_poll_retains_current_source_authorization(test_client, db_session, cleanup_fixture, l5_owner, monkeypatch, service):
    selected = await admitted_cleanup(test_client, db_session, cleanup_fixture, l5_owner, service)
    intent = await db_session.get(SetupIntent, (l5_owner.org_id, selected.operation.operation_id))
    intent.authorization = {key: value for key, value in intent.authorization.items() if key not in {"control_action", "owned_cleanup_hash"}}
    intent.intent_hash = _intent_hash(intent)
    await db_session.commit()
    path = selected.path + "/binding-operations/" + selected.operation.operation_id
    assert (await test_client.get(path)).status_code == 404
    # Legacy receipts use the ordinary reader and retain its real source checks.
    for module in ("src.products.source_routes", "src.product_onboarding.source_discovery"):
        monkeypatch.setattr(module + ".assert_installation_owned_by", AsyncMock())
    source = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.kind == "repository"))
    source.status = "healthy"
    db_session.add(
        ProductSourceBinding(
            tenant_id=l5_owner.org_id,
            project_id=cleanup_fixture["project_id"],
            kind="environment",
            provider="aws",
            credential_id=cleanup_fixture["credential"].id,
            namespace="fixture-environment",
            native_id="123456789012",
            desired={},
            status="healthy",
        )
    )
    await db_session.commit()
    response = await test_client.get(path)
    assert response.status_code == 200, response.text
    source.status = "revoked"
    await db_session.commit()
    assert (await test_client.get(path)).status_code == 404
