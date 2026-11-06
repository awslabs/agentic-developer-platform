"""Transform setup metadata is readable without granting execution authority."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from l5_services.api.bindings import _intent_hash
from l5_services.api.configuration import view
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from l5_services.models.work import Work
from sqlalchemy import func, select

from src import l5_integration
from src.admin.membership_revocation import revoke_membership
from src.auth.aws_connection_authority import connection_binding
from src.auth.dependencies import get_current_user
from src.l5_service_identity import worker_id
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products import test_l5_configuration_host as configuration_fixtures
from tests.products.test_i05 import REQUEST

l5_owner = configuration_fixtures.l5_owner


@pytest.fixture
async def metadata_scope(test_client, test_engine, db_session, l5_owner, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "transform_metadata_fixture_01"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    credential = UserCredential(
        id=str(uuid4()),
        org_id=l5_owner.org_id,
        user_id=l5_owner.user_id,
        service="aws",
        credential_type="aws_role",
        label="Fixture connection",
        strict=True,
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture",
        aws_external_id="fixture-external-id",
        aws_verification_attempt="fixture-attempt",
        aws_verified_at=datetime.now(UTC),
        aws_verified_version_id="credential-version-1",
        scopes={"status": "verified", "account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Fixture"},
    )
    credential.aws_verified_binding = connection_binding(credential)
    reference = {
        "tenant_id": l5_owner.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": credential.aws_verified_version_id,
    }
    source = {**reference, "record_type": "repository_commit", "record_id": "fixture/repository", "record_revision": "a" * 40}
    settings = {
        "service_id": "transform",
        "enabled": False,
        "visibility": "product_team",
        "source_refs": [source],
        "capabilities": ["refactor"],
        "transform": {"connection_ref": reference, "region": "us-east-1"},
    }
    configuration = Configuration(
        tenant_id=l5_owner.org_id,
        configuration_id=str(uuid4()),
        product_id=product_id,
        project_id=project_id,
        service_id="transform",
        relationship_revision="1",
        owner_id=l5_owner.user_id,
        current_revision=1,
        handoff_state="linked",
    )
    version = ConfigurationVersion(
        tenant_id=l5_owner.org_id,
        configuration_id=configuration.configuration_id,
        revision=1,
        settings=settings,
        inherited_fields=[],
        gaps=["executor_binding_ref_missing", "test_policy_ref_missing"],
        state="disabled",
        hash="a" * 64,
    )
    db_session.add_all((credential, configuration, version))
    await db_session.commit()
    rights = {(credential.id, source["record_id"])}
    visible_connections = {credential.id}

    async def connections(db, actor):
        selected = await db.get(UserCredential, credential.id)
        return [({"provider": "aws", "connection_ref": {"id": credential.id}}, selected)] if credential.id in visible_connections else []

    monkeypatch.setattr(l5_integration, "permitted_source_refs", AsyncMock(side_effect=lambda *_: rights.copy()))
    monkeypatch.setattr(l5_integration, "authorized_connections", connections)
    publish = AsyncMock(side_effect=AssertionError("metadata must not publish provider work"))
    monkeypatch.setattr(l5_integration, "publish_l5_work", publish)
    return SimpleNamespace(
        path=f"/v1/products/{product_id}/projects/{project_id}/services/transform",
        configuration=configuration,
        version=version,
        credential=credential,
        reference=reference,
        source=source,
        rights=rights,
        connections=visible_connections,
        publish=publish,
    )


async def retain_metadata(db, scope):
    configuration = scope.configuration
    binding = Binding(
        tenant_id=configuration.tenant_id,
        binding_id=str(uuid4()),
        configuration_id=configuration.configuration_id,
        product_id=configuration.product_id,
        project_id=configuration.project_id,
        service_id="transform",
        scope=view(configuration, scope.version)["scope"],
        revision=1,
        generation=1,
        state="blocked",
        ownership="customer_linked",
        connection_ref=scope.reference,
        connection_key="fixture-transform",
        readiness=[],
        gaps=["executor_binding_ref_missing"],
    )
    intent = SetupIntent(
        tenant_id=configuration.tenant_id,
        operation_id=str(uuid4()),
        product_id=configuration.product_id,
        project_id=configuration.project_id,
        service_id="transform",
        actor_id=configuration.owner_id,
        idempotency_key="retained_transform_setup_01",
        configuration_id=configuration.configuration_id,
        scope=binding.scope,
        request={"action": "check", "binding_id": binding.binding_id, "configuration_revision": 1},
        binding_generation=1,
        authorization={"delegation_id": "historical-grant"},
        intent_hash="pending",
    )
    intent.intent_hash = _intent_hash(intent)
    operation = BindingOperation(
        tenant_id=configuration.tenant_id,
        operation_id=intent.operation_id,
        binding_id=binding.binding_id,
        action="check",
        state="blocked",
        revision=1,
        gaps=["executor_binding_ref_missing"],
    )
    db.add_all((binding, intent, operation))
    await db.commit()
    return binding, operation


def metadata_paths(binding, operation):
    return ("/bindings", f"/bindings/{binding.binding_id}", f"/binding-operations/{operation.operation_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_state", ["absent", "expired", "revoked"])
async def test_metadata_reads_do_not_require_execution_grant(test_client, db_session, metadata_scope, grant_state):
    scope = metadata_scope
    if grant_state != "absent":
        db_session.add(
            CredentialServiceDelegation(
                id="historical-grant",
                tenant_id=scope.configuration.tenant_id,
                credential_id=scope.credential.id,
                service_id=worker_id("transform"),
                product_id=scope.configuration.product_id,
                project_id=scope.configuration.project_id,
                created_by=scope.configuration.owner_id,
                operations=["credential:session"],
                sources=[{**scope.reference, "record_type": "credential"}],
                capabilities=["refactor"],
                visibility="restricted",
                revision=1,
                binding_generation=1,
                revoked_at=datetime.now(UTC) if grant_state == "revoked" else None,
                expires_at=datetime.now(UTC) + timedelta(hours=-1 if grant_state == "expired" else 1),
            )
        )
        await db_session.commit()
    empty = await test_client.get(scope.path + "/bindings")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"items": []}
    binding, operation = await retain_metadata(db_session, scope)
    for suffix in metadata_paths(binding, operation):
        response = await test_client.get(scope.path + suffix)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store" and response.headers["etag"]
        payload = response.json()["items"][0] if suffix == "/bindings" else response.json()
        assert payload["binding_id"] == binding.binding_id
        assert payload["state"] == "blocked" and payload["gaps"] == ["executor_binding_ref_missing"]
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == (grant_state != "absent")
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Work)) == 0
    scope.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial",
    [
        "membership",
        "relationship",
        "source",
        "connection",
        "credential_owner",
        "credential_revision",
        "configuration_owner",
        "wrong_role",
        "foreign_reference",
    ],
)
async def test_metadata_rechecks_current_read_authority(test_client, db_session, metadata_scope, denial):
    scope = metadata_scope
    binding, operation = await retain_metadata(db_session, scope)
    before = await test_client.get(scope.path + "/bindings")
    assert before.status_code == 200, before.text
    if denial == "membership":
        await revoke_membership(db_session, org_id=scope.configuration.tenant_id, user_id=scope.configuration.owner_id)
    elif denial == "relationship":
        scope.configuration.relationship_revision = "0"
    elif denial == "source":
        scope.rights.clear()
    elif denial == "connection":
        scope.connections.clear()
    elif denial == "credential_owner":
        scope.credential.user_id = "other-owner"
        scope.credential.aws_verified_binding = connection_binding(scope.credential)
    elif denial == "credential_revision":
        scope.credential.aws_verified_version_id = "new-version"
    elif denial == "configuration_owner":
        scope.configuration.owner_id = "other-owner"
    elif denial == "wrong_role":
        scope.credential.scopes = {**scope.credential.scopes, "role_arn": "arn:aws:iam::999999999999:role/Foreign"}
        scope.credential.aws_verified_binding = connection_binding(scope.credential)
    else:
        scope.version.settings = {
            **scope.version.settings,
            "transform": {**scope.version.settings["transform"], "executor_binding_ref": {**scope.reference, "project_id": "foreign-project"}},
        }
    await db_session.commit()
    for suffix in metadata_paths(binding, operation):
        denied = await test_client.get(scope.path + suffix)
        assert denied.status_code == (503 if denial == "wrong_role" else 404), denied.text
        assert binding.binding_id not in denied.text and scope.credential.id not in denied.text
    scope.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_stays_scoped_and_service_actor_requires_delegation(test_client, db_session, metadata_scope, app, l5_owner):
    scope = metadata_scope
    binding, operation = await retain_metadata(db_session, scope)
    for wrong_path in (
        scope.path.replace(scope.configuration.product_id, "foreign-product"),
        scope.path.replace(scope.configuration.project_id, "foreign-project"),
        scope.path.replace("/services/transform", "/services/security"),
    ):
        for suffix in metadata_paths(binding, operation):
            assert (await test_client.get(wrong_path + suffix)).status_code == 404
    for change in ({"account_type": "service"}, {"org_id": "foreign-tenant"}):
        actor = l5_owner.model_copy(update=change)
        app.dependency_overrides[get_current_user] = lambda: actor
        for suffix in metadata_paths(binding, operation):
            assert (await test_client.get(scope.path + suffix)).status_code == 404
    scope.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_read_does_not_authorize_setup_reconcile_or_admission(test_client, db_session, metadata_scope, l5_owner, monkeypatch):
    scope = metadata_scope
    binding, operation = await retain_metadata(db_session, scope)
    assert (await test_client.get(scope.path + "/bindings")).status_code == 200
    start = await test_client.post(
        scope.path + "/binding-operations",
        json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "metadata_no_setup_authority_01"},
    )
    reconcile = await test_client.post(
        scope.path + f"/binding-operations/{operation.operation_id}/reconcile",
        json={"reason": "Inspect retained setup"},
        headers={"Idempotency-Key": "metadata_no_reconcile_authority_01"},
    )
    for response in (start, reconcile):
        assert response.status_code == 403 and response.json()["detail"]["code"] == "delegation_denied", response.text
    # Satisfy readiness only, so actual admission must independently reject the
    # absent execution grant before it can inspect or dispatch the binding.
    scope.version.settings = {**scope.version.settings, "enabled": True}
    await db_session.commit()
    backend = l5_integration.GatewayConfigurationBackend(db_session, l5_owner)
    monkeypatch.setattr(type(backend), "transform_submission_ready", property(lambda _: True))
    backend.transform_configuration_readiness = AsyncMock(return_value={"ready": True})
    with pytest.raises(HTTPException) as denied:
        await backend.admission_authority(
            scope.configuration.product_id,
            scope.configuration.project_id,
            "transform",
            {"binding_id": binding.binding_id, "sources": [scope.source], "capability": "refactor", "configuration_revision": 1},
        )
    assert denied.value.status_code == 403 and denied.value.detail["code"] == "delegation_denied"
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Work)) == 0
    scope.publish.assert_not_awaited()
