"""Owner consent must be exact, renewable without resurrection, and revocable."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.setup import SetupWork
from sqlalchemy import delete, func, select

from src.auth.aws_connection_authority import connection_binding
from src.l5_service_identity import WORKER_IDS
from src.products.models import Product, Project
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.audit import AuditLog
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products import test_l5_configuration_host as configuration_fixtures
from tests.products.test_i05 import REQUEST

l5_owner = configuration_fixtures.l5_owner


@pytest.fixture
async def consent_fixture(test_client, test_engine, db_session, l5_owner, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "consent_product_fixture_01"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    credential = UserCredential(
        id=str(uuid4()),
        org_id=l5_owner.org_id,
        user_id=l5_owner.user_id,
        service="aws",
        credential_type="aws_role",
        label="Owner-selected provider",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture",
        strict=True,
        aws_external_id="fixture-external-id",
        aws_verification_attempt="fixture-attempt",
        aws_verified_at=datetime.now(UTC),
        aws_verified_version_id="credential-version-1",
        scopes={"status": "verified", "account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Fixture"},
    )
    credential.aws_verified_binding = connection_binding(credential)
    db_session.add(credential)
    reference = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": credential.aws_verified_version_id,
    }
    configurations = {}
    for service_id in WORKER_IDS:
        configuration_id = str(uuid4())
        selected = {"connection_ref": reference, "region": "us-east-1"}
        settings = {
            "service_id": service_id,
            "enabled": True,
            "source_refs": [],
            "visibility": "only_me",
            "capabilities": [
                {
                    "security": "code_review",
                    "devops": "investigation",
                    "finops": "cost_inquiry",
                    "transform": "custom_transform",
                    "well_architected": "architecture_review",
                }[service_id]
            ],
        }
        if service_id == "devops":
            settings[service_id] = {"hosting_connection_ref": reference, "region": "us-east-1"}
        elif service_id == "transform":
            settings[service_id] = selected
        else:
            settings.update(selected)
        configuration = Configuration(
            tenant_id=l5_owner.org_id,
            configuration_id=configuration_id,
            product_id=product_id,
            project_id=project_id,
            service_id=service_id,
            relationship_revision="1",
            owner_id=l5_owner.user_id,
            current_revision=1,
            handoff_state="linked",
        )
        version = ConfigurationVersion(
            tenant_id=l5_owner.org_id,
            configuration_id=configuration_id,
            revision=1,
            settings=settings,
            inherited_fields=[],
            gaps=[],
            state="ready",
            hash="a" * 64,
        )
        db_session.add_all((configuration, version))
        configurations[service_id] = (configuration, version)
    await db_session.commit()
    # Source/connection selection authorization has its own mounted integration
    # tests. Keep its boundary explicit here, including its denial below.
    authorize_sources = AsyncMock()
    monkeypatch.setattr("src.l5_integration.GatewayConfigurationBackend.authorize_settings", authorize_sources)
    return {
        "path": f"/v1/products/{product_id}/projects/{project_id}/services",
        "product_id": product_id,
        "project_id": project_id,
        "credential": credential,
        "reference": reference,
        "configurations": configurations,
        "source_authority": authorize_sources,
    }


async def request_grant(client, path, *, key="explicit_owner_consent_0001", **changes):
    preview = await client.get(path + "/delegation-scope")
    assert preview.status_code == 200, preview.text
    body = {"scope_digest": preview.json()["scope_digest"], "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(), **changes}
    headers = {"If-Match": preview.headers["etag"], "Idempotency-Key": key}
    response = await client.post(path + "/delegations", json=body, headers=headers)
    return response, body, headers, preview


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", list(WORKER_IDS))
async def test_disabled_configuration_preview_does_not_issue_unusable_consent(test_client, db_session, consent_fixture, service_id):
    _, version = consent_fixture["configurations"][service_id]
    version.settings = {**version.settings, "enabled": False}
    await db_session.commit()
    response, _, _, preview = await request_grant(test_client, consent_fixture["path"] + "/" + service_id)
    assert preview.status_code == 200
    assert response.status_code == 409 and response.json()["detail"]["code"] == "configuration_disabled"
    assert "enable" in response.json()["detail"]["message"]
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 0
    await db_session.refresh(version)
    assert version.settings["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", list(WORKER_IDS))
async def test_exact_owner_consent_retry_revocation_and_renewal(test_client, db_session, consent_fixture, l5_owner, service_id):
    fixture = consent_fixture
    path = fixture["path"] + "/" + service_id
    response, body, headers, preview = await request_grant(test_client, path)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["service_id"] == WORKER_IDS[service_id]
    assert result["product_id"] == fixture["product_id"] and result["project_id"] == fixture["project_id"]
    assert result["sources"] == [{**fixture["reference"], "record_type": "credential"}]
    assert result["binding_generation"] == 1 and result["active"]
    assert result["capabilities"] == preview.json()["scope"]["capabilities"]
    assert result["operations"] == (
        ["credential:session", "product:read"]
        if service_id in {"finops", "devops"}
        else ["credential:session", "product:read", "source:pin", "source:read"]
    )
    assert preview.headers["cache-control"] == response.headers["cache-control"] == "no-store"
    retry = await test_client.post(path + "/delegations", json=body, headers=headers)
    assert retry.status_code == 200 and retry.json() == result
    conflict = await test_client.post(
        path + "/delegations", json={**body, "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat()}, headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    duplicate = await test_client.post(path + "/delegations", json=body, headers={**headers, "Idempotency-Key": "other_owner_consent_0001"})
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "delegation_already_active"
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 0
    _, version = fixture["configurations"][service_id]
    await db_session.refresh(version)
    assert version.settings["enabled"] is True
    grant = await db_session.get(CredentialServiceDelegation, result["delegation_id"], populate_existing=True)
    child = CredentialServiceDelegation(
        parent_delegation_id=grant.id,
        tenant_id=grant.tenant_id,
        product_id=grant.product_id,
        project_id=grant.project_id,
        credential_id=grant.credential_id,
        service_id=grant.service_id,
        created_by=grant.created_by,
        operations=grant.operations,
        sources=grant.sources,
        capabilities=grant.capabilities,
        visibility=grant.visibility,
        binding_generation=grant.binding_generation,
        execution_scope=grant.execution_scope,
    )
    assert await current_l5_parent(db_session, child)
    revoked = await test_client.delete(path + "/delegations/" + grant.id, headers={"If-Match": response.headers["etag"]})
    assert revoked.status_code == 200 and revoked.json()["active"] is False and revoked.json()["revision"] == 2
    assert not await current_l5_parent(db_session, child)
    replay = await test_client.post(path + "/delegations", json=body, headers=headers)
    assert replay.status_code == 409 and replay.json()["detail"]["code"] == "delegation_inactive"
    renewed = await test_client.post(path + "/delegations", json=body, headers={**headers, "Idempotency-Key": "renewed_owner_consent_0001"})
    assert renewed.status_code == 200 and renewed.json()["delegation_id"] != grant.id
    assert not await current_l5_parent(db_session, child)
    assert len((await test_client.get(path + "/delegations")).json()["items"]) == 2
    audits = (await db_session.scalars(select(AuditLog).where(AuditLog.event_type.in_(["l5_service_authorized", "l5_service_revoked"])))).all()
    assert len(audits) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "source",
        "connection",
        "credential_owner",
        "owner",
        "membership",
        "product_archive",
        "project_archive",
        "service",
        "config_revision",
        "foreign_tenant",
    ],
)
async def test_current_authority_changes_deny_consent(test_client, db_session, consent_fixture, l5_owner, app, change):
    from src.auth.dependencies import get_current_user

    fixture = consent_fixture
    path = fixture["path"] + "/security"
    preview = await test_client.get(path + "/delegation-scope")
    assert preview.status_code == 200
    if change == "source":
        fixture["source_authority"].side_effect = HTTPException(404)
    elif change == "connection":
        fixture["credential"].aws_verified_version_id = "rotated-version"
    elif change == "credential_owner":
        fixture["credential"].user_id = "another-owner"
    elif change == "owner":
        fixture["configurations"]["security"][0].owner_id = "another-owner"
    elif change == "membership":
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
        # A platform-admin label must not substitute for Product team membership.
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"is_admin": True})
    elif change in {"product_archive", "project_archive"}:
        target = (
            await db_session.get(Product, (l5_owner.org_id, fixture["product_id"]))
            if change == "product_archive"
            else await db_session.get(Project, (l5_owner.org_id, fixture["project_id"]))
        )
        target.archived_at = datetime.now(UTC)
    elif change == "service":
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(
            update={"account_type": "service", "auth_source": "iam", "agent_registry_id": "l5-worker"}
        )
    elif change == "foreign_tenant":
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"org_id": "foreign-tenant"})
    else:
        fixture["configurations"]["security"][1].hash = "b" * 64
    await db_session.commit()
    result = await test_client.post(
        path + "/delegations",
        json={
            "scope_digest": preview.json()["scope_digest"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "denied_owner_consent_001"},
    )
    assert result.status_code in {404, 409, 412}, result.text
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 0


@pytest.mark.asyncio
async def test_preconditions_and_expiry_are_explicit(test_client, consent_fixture):
    path = consent_fixture["path"] + "/devops"
    preview = await test_client.get(path + "/delegation-scope")
    body = {"scope_digest": preview.json()["scope_digest"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    assert (await test_client.post(path + "/delegations", json=body)).status_code == 428
    headers = {"If-Match": preview.headers["etag"], "Idempotency-Key": "bounded_owner_consent_0001"}
    for expires in (datetime.now(UTC) - timedelta(seconds=1), datetime.now(UTC) + timedelta(days=2)):
        result = await test_client.post(path + "/delegations", json={**body, "expires_at": expires.isoformat()}, headers=headers)
        assert result.status_code == 422 and result.json()["detail"]["code"] == "delegation_expiry_invalid"
    assert (await test_client.post(path + "/delegations", json={**body, "service_id": "l5-repair"}, headers=headers)).status_code == 422
    assert (await test_client.post(path + "/delegations", json={**body, "scope_digest": "0" * 64}, headers=headers)).status_code == 412
    assert (await test_client.get(consent_fixture["path"] + "/repair/delegation-scope")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("archive", ["product", "project"])
async def test_revoke_survives_archive_and_connection_or_source_loss(test_client, db_session, consent_fixture, l5_owner, archive):
    fixture = consent_fixture
    path = fixture["path"] + "/security"
    response, _, _, _ = await request_grant(test_client, path)
    assert response.status_code == 200
    fixture["credential"].aws_verified_version_id = None
    fixture["source_authority"].side_effect = HTTPException(404)
    target = (
        await db_session.get(Product, (l5_owner.org_id, fixture["product_id"]))
        if archive == "product"
        else await db_session.get(Project, (l5_owner.org_id, fixture["project_id"]))
    )
    target.archived_at = datetime.now(UTC)
    await db_session.commit()
    assert (await test_client.get(path + "/delegation-scope")).status_code == 404
    assert (await test_client.get(path + "/delegations")).status_code == 200
    revoke_path = path + "/delegations/" + response.json()["delegation_id"]
    assert (await test_client.delete(revoke_path)).status_code == 428
    assert (await test_client.delete(revoke_path, headers={"If-Match": '"7"'})).status_code == 412
    revoked = await test_client.delete(revoke_path, headers={"If-Match": response.headers["etag"]})
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None


@pytest.mark.asyncio
async def test_exact_binding_generation_and_foreign_binding_denied(test_client, db_session, consent_fixture, l5_owner):
    from l5_services.api.configuration import view

    fixture = consent_fixture
    path = fixture["path"] + "/security"
    configuration, version = fixture["configurations"]["security"]
    binding = Binding(
        tenant_id=l5_owner.org_id,
        binding_id=str(uuid4()),
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id="security",
        configuration_id=configuration.configuration_id,
        revision=1,
        generation=3,
        state="ready",
        ownership="customer_linked",
        scope=view(configuration, version)["scope"],
        connection_ref=fixture["reference"],
        connection_key="fixture-connection",
        gaps=[],
        readiness=[],
    )
    db_session.add(binding)
    await db_session.commit()
    current = await test_client.get(path + "/delegation-scope", params={"binding_id": binding.binding_id})
    following = await test_client.get(path + "/delegation-scope", params={"binding_id": binding.binding_id, "for_control": "true"})
    assert current.status_code == following.status_code == 200
    assert current.json()["scope"]["binding_generation"] == 3
    assert following.json()["scope"]["binding_generation"] == 4
    assert current.json()["scope_digest"] != following.json()["scope_digest"]
    assert (await test_client.get(path + "/delegation-scope", params={"for_control": "true"})).status_code == 422
    binding.product_id = "foreign-product"
    await db_session.commit()
    assert (await test_client.get(path + "/delegation-scope", params={"binding_id": binding.binding_id})).status_code == 404
