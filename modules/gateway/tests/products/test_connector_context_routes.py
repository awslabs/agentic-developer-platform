"""Context handles resolve only through current delegated gateway authority."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import delete, select

from src.auth.dependencies import get_current_user
from src.internal import connector_context_routes
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.connector_work_authority import get_connector_work_authorizer
from src.internal.l5_credential_routes import L5WorkPermit
from src.products.models import (
    ConnectorCell,
    ConnectorCollectionGrant,
    ConnectorEvidenceDependency,
    ConnectorModuleAdmission,
    ConnectorOperationContext,
    ConnectorPolicyRealm,
    ProductSourceBinding,
)
from src.shared.models.organization import Organization, TeamMembership, User
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.membership import add_team_member
from tests.products.test_connector_grants import make_registered_selection


@pytest.fixture
async def operation_authority(test_client, db_session, app, source_member, monkeypatch):
    grant_selection_fixture = await make_registered_selection(test_client, db_session, source_member)
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    monkeypatch.setenv("DEVLAKE_COLLECTION_ENABLED", "true")
    created = await test_client.post(
        grant_selection_fixture["path"], json=grant_selection_fixture["body"], headers=grant_selection_fixture["headers"]
    )
    assert created.status_code == 201, created.text
    collection = await db_session.get(ConnectorCollectionGrant, (source_member.org_id, created.json()["id"]))
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, collection.binding_id))
    project_id = grant_selection_fixture["project_id"]
    product_id = grant_selection_fixture["path"].split("/")[3]
    delegation = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=binding.credential_id,
        service_id="registered-worker",
        product_id=product_id,
        project_id=project_id,
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=collection.source_selections,
        capabilities=[],
        visibility="restricted",
        operation_id="run-one",
        attempt_id="attempt-one",
        lease_fence=2,
        binding_generation=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(delegation)
    await db_session.commit()
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:registered-worker",
            "agent_registry_id": "registered-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
        }
    )

    async def verified(request: Request):
        request.state.token_context = service

    app.dependency_overrides[verify_internal_or_irsa] = verified
    allowed = (source_member.org_id, product_id, project_id, binding.credential_id, "run-one", "attempt-one")

    async def fixed_permit(current, requested):
        if (
            current.tenant_id,
            current.product_id,
            current.project_id,
            current.credential_id,
            requested.operation_id,
            requested.attempt_id,
        ) != allowed:
            raise HTTPException(404, detail={"code": "not_found"})
        if requested.connection_ref.model_dump() != collection.source_selections[0]:
            raise HTTPException(404, detail={"code": "not_found"})
        return L5WorkPermit(*allowed, lease_fence=2, binding_generation=1, expires_at=datetime.now(UTC) + timedelta(hours=1))

    app.dependency_overrides[get_connector_work_authorizer] = lambda: fixed_permit
    return {
        "body": {
            "product_id": product_id,
            "project_id": project_id,
            "grant_id": collection.id,
            "delegation_id": delegation.id,
            "delegation_revision": 1,
            "operation_id": "run-one",
            "attempt_id": "attempt-one",
        },
        "service": service,
        "collection": collection,
        "binding": binding,
        "delegation": delegation,
    }


@pytest.mark.asyncio
async def test_context_route_uses_registered_cell_and_independent_readback(test_client, db_session, operation_authority):
    authority = operation_authority
    cell = await db_session.get(ConnectorCell, (authority["collection"].tenant_id, authority["collection"].cell_id))
    assert (cell.database_ref, cell.role_ref, cell.object_prefix) == ("controlled-db-ref", "controlled-role-ref", "restricted/")

    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])
    assert created.status_code == 201, created.text
    assert set(created.json()) == {"context_version", "handle", "expires_at"}
    assert created.json()["context_version"] == 1
    replay = await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])
    assert replay.status_code == 200 and replay.json() == created.json()
    assert len((await db_session.scalars(select(ConnectorOperationContext))).all()) == 1

    readback = await test_client.get(f"/internal/v1/connector-contexts/{created.json()['handle']}")
    assert readback.status_code == 200, readback.text
    assert readback.json()["destination"] == {
        "database_ref": "controlled-db-ref",
        "role_ref": "controlled-role-ref",
        "object_prefix": "restricted/",
    }
    assert readback.json()["source_selections"] == authority["collection"].source_selections
    assert (readback.json()["tenant_id"], readback.json()["project_id"], readback.json()["cell_id"]) == (
        authority["collection"].tenant_id,
        authority["collection"].project_id,
        cell.id,
    )


@pytest.mark.asyncio
async def test_replay_preserves_cell_local_identity_without_disclosing_other_cell(test_client, db_session, app, source_member, operation_authority):
    first = operation_authority
    selection = await make_registered_selection(
        test_client, db_session, source_member, suffix="-replay", workload_id=first["service"].agent_registry_id
    )
    created_grant = await test_client.post(selection["path"], json=selection["body"], headers=selection["headers"])
    assert created_grant.status_code == 201, created_grant.text
    second_grant = await db_session.get(ConnectorCollectionGrant, (source_member.org_id, created_grant.json()["id"]))
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, second_grant.binding_id))
    second_delegation = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        credential_id=binding.credential_id,
        service_id=second_grant.consumer,
        product_id=selection["path"].split("/")[3],
        project_id=selection["project_id"],
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=second_grant.source_selections,
        capabilities=[],
        visibility="restricted",
        operation_id=first["body"]["operation_id"],
        attempt_id=first["body"]["attempt_id"],
        lease_fence=2,
        binding_generation=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(second_delegation)
    await db_session.commit()
    second_body = {
        **first["body"],
        "product_id": second_delegation.product_id,
        "project_id": second_delegation.project_id,
        "grant_id": second_grant.id,
        "delegation_id": second_delegation.id,
    }
    expected = {
        first["delegation"].id: first["collection"].source_selections[0],
        second_delegation.id: second_grant.source_selections[0],
    }

    async def fixed_permit(current, requested):
        assert requested.connection_ref.model_dump() == expected[current.id]
        return L5WorkPermit(
            current.tenant_id,
            current.product_id,
            current.project_id,
            current.credential_id,
            requested.operation_id,
            requested.attempt_id,
            lease_fence=2,
            binding_generation=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    app.dependency_overrides[get_connector_work_authorizer] = lambda: fixed_permit
    endpoint = "/internal/v1/connector-contexts/resolve"
    first_created = await test_client.post(endpoint, json=first["body"])
    assert first_created.status_code == 201, first_created.text
    first_context = await db_session.get(ConnectorOperationContext, (source_member.org_id, first_created.json()["handle"]))
    first_created_at = first_context.created_at
    first_replay = await test_client.post(endpoint, json=first["body"])
    assert first_replay.status_code == 200 and first_replay.json() == first_created.json()
    second_created = await test_client.post(endpoint, json=second_body)
    assert second_created.status_code == 201, second_created.text
    assert second_created.json()["handle"] != first_created.json()["handle"]
    first_read = await test_client.get(f"/internal/v1/connector-contexts/{first_created.json()['handle']}")
    second_read = await test_client.get(f"/internal/v1/connector-contexts/{second_created.json()['handle']}")
    assert (first_read.status_code, second_read.status_code) == (200, 200)
    assert (first_read.json()["cell_id"], second_read.json()["cell_id"]) == (first["collection"].cell_id, second_grant.cell_id)
    assert first_read.json()["destination"] != second_read.json()["destination"]
    second_replay = await test_client.post(endpoint, json=second_body)
    first_replay = await test_client.post(endpoint, json=first["body"])
    assert second_replay.status_code == first_replay.status_code == 200
    assert second_replay.json() == second_created.json() and first_replay.json() == first_created.json()
    assert (await test_client.post(endpoint, json={**second_body, "grant_id": first["collection"].id})).status_code == 404
    rows = (await db_session.scalars(select(ConnectorOperationContext))).all()
    assert {(row.cell_id, row.id) for row in rows} == {
        (first["collection"].cell_id, first_created.json()["handle"]),
        (second_grant.cell_id, second_created.json()["handle"]),
    }
    assert all(row.revoked_at is None and row.created_at is not None for row in rows)
    await db_session.refresh(first_context)
    assert first_context.created_at == first_created_at
    first["collection"].state = "revoked"
    first["collection"].revoked_at = datetime.now(UTC)
    await db_session.commit()
    stale = await test_client.post(endpoint, json=first["body"])
    assert stale.status_code == 409 and "handle" not in stale.json()
    independent_replay = await test_client.post(endpoint, json=second_body)
    assert independent_replay.status_code == 200 and independent_replay.json() == second_created.json()
    assert (await test_client.get(f"/internal/v1/connector-contexts/{first_created.json()['handle']}")).status_code == 409
    assert (await test_client.get(f"/internal/v1/connector-contexts/{second_created.json()['handle']}")).status_code == 200
    assert len((await db_session.scalars(select(ConnectorOperationContext))).all()) == 2


@pytest.mark.asyncio
async def test_module_transition_revokes_contexts_grants_and_cell_generation(test_client, db_session, app, source_member, operation_authority):
    authority = operation_authority
    tenant_id = authority["collection"].tenant_id
    path = f"/internal/v1/connector-contexts/module/{tenant_id}"
    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])
    assert created.status_code == 201
    handle = created.json()["handle"]
    cell = await db_session.get(ConnectorCell, (tenant_id, authority["collection"].cell_id))
    initial_revision = cell.routing_revision

    assert (await test_client.put(path, json={"enabled": False})).status_code == 403
    operator = authority["service"].model_copy(update={"org_id": tenant_id, "credential_scopes": ["connector:module:transition"]})

    selected_actor = {"identity": operator}

    async def authenticated_operator(request: Request):
        request.state.token_context = selected_actor["identity"]

    app.dependency_overrides[verify_internal_or_irsa] = authenticated_operator
    assert (await test_client.put("/internal/v1/connector-contexts/module/foreign-tenant", json={"enabled": False})).status_code == 403
    disabled = await test_client.put(path, json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json() == {"enabled": False, "generation": 2}
    assert (await test_client.put(path, json={"enabled": False})).json() == disabled.json()
    await db_session.refresh(cell)
    await db_session.refresh(authority["collection"])
    context = await db_session.get(ConnectorOperationContext, (tenant_id, handle))
    await db_session.refresh(context)
    assert cell.routing_revision == initial_revision + 1
    assert authority["collection"].state == "revoked" and authority["collection"].revoked_at is not None
    assert context.revoked_at is not None and context.module_generation == 1
    module = await db_session.get(ConnectorModuleAdmission, tenant_id)
    assert module.enabled is False and module.generation == 2

    selected_actor["identity"] = authority["service"]
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handle}")).status_code == 404
    assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])).status_code == 503
    selected_actor["identity"] = operator
    enabled = await test_client.put(path, json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json() == {"enabled": True, "generation": 3}
    selected_actor["identity"] = authority["service"]
    assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])).status_code == 409
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handle}")).status_code == 404

    fresh_grant_response = await test_client.post(
        f"/v1/products/{authority['body']['product_id']}/projects/{authority['body']['project_id']}/collection-grants",
        json={
            "binding_id": authority["binding"].id,
            "binding_generation": authority["binding"].generation,
            "binding_revision": authority["binding"].revision,
            "cell_id": authority["collection"].cell_id,
            "consumer": authority["collection"].consumer,
            "purpose": "collection",
        },
        headers={"Idempotency-Key": "fresh-after-module-disable-8919", "If-Profile-Revision": "1"},
    )
    assert fresh_grant_response.status_code == 201, fresh_grant_response.text
    fresh_grant = await db_session.get(ConnectorCollectionGrant, (tenant_id, fresh_grant_response.json()["id"]))
    fresh_delegation = CredentialServiceDelegation(
        tenant_id=tenant_id,
        credential_id=authority["binding"].credential_id,
        service_id=authority["collection"].consumer,
        product_id=authority["body"]["product_id"],
        project_id=authority["body"]["project_id"],
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=fresh_grant.source_selections,
        capabilities=[],
        visibility="restricted",
        operation_id=authority["body"]["operation_id"],
        attempt_id=authority["body"]["attempt_id"],
        lease_fence=3,
        binding_generation=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(fresh_delegation)
    await db_session.commit()

    async def fresh_permit(current, requested):
        assert current.id == fresh_delegation.id
        assert requested.connection_ref.model_dump() == fresh_grant.source_selections[0]
        return L5WorkPermit(
            current.tenant_id,
            current.product_id,
            current.project_id,
            current.credential_id,
            requested.operation_id,
            requested.attempt_id,
            lease_fence=current.lease_fence,
            binding_generation=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    app.dependency_overrides[get_connector_work_authorizer] = lambda: fresh_permit
    new_body = {**authority["body"], "grant_id": fresh_grant.id, "delegation_id": fresh_delegation.id}
    stale_replay = await test_client.post("/internal/v1/connector-contexts/resolve", json=new_body)
    assert stale_replay.status_code == 409 and "handle" not in stale_replay.json()
    fresh_delegation.revision += 1
    fresh_delegation.operation_id = "run-after-enable"
    fresh_delegation.attempt_id = "attempt-after-enable"
    fresh_delegation.lease_fence += 1
    await db_session.commit()
    new_body.update(delegation_revision=fresh_delegation.revision, operation_id=fresh_delegation.operation_id, attempt_id=fresh_delegation.attempt_id)
    fresh_context = await test_client.post("/internal/v1/connector-contexts/resolve", json=new_body)
    assert fresh_context.status_code == 201, fresh_context.text
    assert fresh_context.json()["handle"] != handle
    new_row = await db_session.get(ConnectorOperationContext, (tenant_id, fresh_context.json()["handle"]))
    assert new_row.module_generation == 3 and new_row.routing_revision == initial_revision + 1
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handle}")).status_code == 404


@pytest.mark.asyncio
async def test_observed_flag_disable_remains_revoked_after_flag_rebound(test_client, db_session, operation_authority, monkeypatch):
    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=operation_authority["body"])
    assert created.status_code == 201
    monkeypatch.setenv("DEVLAKE_COLLECTION_ENABLED", "false")
    disabled = await test_client.post("/internal/v1/connector-contexts/resolve", json=operation_authority["body"])
    assert disabled.status_code == 503
    monkeypatch.setenv("DEVLAKE_COLLECTION_ENABLED", "true")
    tenant_id = operation_authority["collection"].tenant_id
    assert (await db_session.get(ConnectorModuleAdmission, tenant_id)).enabled is False
    assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=operation_authority["body"])).status_code == 503
    assert (await test_client.get(f"/internal/v1/connector-contexts/{created.json()['handle']}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["expired", "membership", "source", "delegation", "cell"])
async def test_context_readback_requires_current_scope(test_client, db_session, source_member, operation_authority, transition):
    authority = operation_authority
    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])
    assert created.status_code == 201
    handle = created.json()["handle"]
    context = await db_session.get(ConnectorOperationContext, (source_member.org_id, handle))
    if transition == "expired":
        context.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif transition == "membership":
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == source_member.user_id))
    elif transition == "source":
        authority["binding"].generation += 1
    elif transition == "delegation":
        authority["delegation"].revoked_at = datetime.now(UTC)
    else:
        cell = await db_session.get(ConnectorCell, (source_member.org_id, authority["collection"].cell_id))
        cell.state = "retired"
    await db_session.commit()
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handle}")).status_code in (404, 409)
    if transition != "expired":
        assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])).status_code in (404, 409)


@pytest.mark.asyncio
async def test_fixed_clock_context_expiry_fences_read_and_replay(test_client, db_session, operation_authority, monkeypatch):
    clock = {"now": datetime.now(UTC)}
    monkeypatch.setattr(connector_context_routes, "utcnow", lambda: clock["now"])
    body = operation_authority["body"]
    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=body)
    assert created.status_code == 201, created.text
    handle = created.json()["handle"]
    deadline = datetime.fromisoformat(created.json()["expires_at"])
    clock["now"] = deadline - timedelta(microseconds=1)
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handle}")).status_code == 200
    clock["now"] = deadline
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handle}")).status_code == 404
    replay = await test_client.post("/internal/v1/connector-contexts/resolve", json=body)
    assert replay.status_code in (403, 409)
    assert "handle" not in replay.json()
    assert [row.id for row in (await db_session.scalars(select(ConnectorOperationContext))).all()] == [handle]


@pytest.mark.asyncio
async def test_fixed_clock_module_disable_preserves_revocation_on_enable(test_client, db_session, app, operation_authority, monkeypatch):
    clock = {"now": datetime.now(UTC)}
    monkeypatch.setattr(connector_context_routes, "utcnow", lambda: clock["now"])
    authority = operation_authority
    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])
    assert created.status_code == 201, created.text
    tenant_id = authority["collection"].tenant_id
    handle = created.json()["handle"]
    operator = authority["service"].model_copy(update={"org_id": tenant_id, "credential_scopes": ["connector:module:transition"]})
    current_actor = {"identity": operator}

    async def verified(request: Request):
        request.state.token_context = current_actor["identity"]

    app.dependency_overrides[verify_internal_or_irsa] = verified
    transition = f"/internal/v1/connector-contexts/module/{tenant_id}"
    disabled = await test_client.put(transition, json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json() == {"enabled": False, "generation": 2}
    context = await db_session.get(ConnectorOperationContext, (tenant_id, handle))
    await db_session.refresh(context)
    await db_session.refresh(authority["collection"])
    assert context.revoked_at.replace(tzinfo=UTC) == clock["now"]
    assert authority["collection"].revoked_at.replace(tzinfo=UTC) == clock["now"]
    clock["now"] += timedelta(seconds=1)
    enabled = await test_client.put(transition, json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json() == {"enabled": True, "generation": 3}
    module = await db_session.get(ConnectorModuleAdmission, tenant_id)
    await db_session.refresh(module)
    assert module.changed_at.replace(tzinfo=UTC) == clock["now"]
    current_actor["identity"] = authority["service"]
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handle}")).status_code == 404
    assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=authority["body"])).status_code == 409


@pytest.mark.asyncio
async def test_context_route_denies_forged_scope_and_unbound_workload(test_client, db_session, app, operation_authority):
    authority = operation_authority
    body = authority["body"]
    for change in (
        {"product_id": "foreign-product"},
        {"project_id": "foreign-project"},
        {"grant_id": "foreign-grant"},
        {"delegation_id": "foreign-delegation"},
        {"database_ref": "injected"},
        {"cell_id": "foreign-cell"},
        {"tenant_id": "foreign-tenant"},
        {"object_prefix": "foreign/"},
        {"audience": "shared"},
        {"operation_id": "foreign-operation"},
    ):
        response = await test_client.post("/internal/v1/connector-contexts/resolve", json={**body, **change})
        assert response.status_code in (404, 422), (change, response.text)
    unbound = authority["service"].model_copy(update={"agent_registry_id": "foreign-worker"})

    async def other_worker(request: Request):
        request.state.token_context = unbound

    app.dependency_overrides[verify_internal_or_irsa] = other_worker
    assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=body)).status_code == 404
    assert (await db_session.scalars(select(ConnectorOperationContext))).all() == []


@pytest.mark.asyncio
async def test_registered_workload_without_current_work_permit_gets_no_context(test_client, db_session, app, operation_authority):
    app.dependency_overrides.pop(get_connector_work_authorizer)
    response = await test_client.post("/internal/v1/connector-contexts/resolve", json=operation_authority["body"])
    assert response.status_code == 409 and response.json()["detail"]["code"] == "stale_collection_work"
    assert (await db_session.scalars(select(ConnectorOperationContext))).all() == []


@pytest.mark.asyncio
async def test_context_authority_across_tenants_projects_and_workers(
    test_client,
    db_session,
    db_session_factory,
    app,
    source_member,
    operation_authority,
):
    base = operation_authority
    same_tenant = await make_registered_selection(
        test_client,
        db_session,
        source_member,
        suffix="-same",
        workload_id="registered-worker-same",
    )
    other_owner = source_member.model_copy(update={"org_id": "tenant-other", "user_id": "owner-other", "team_id": "team-other"})
    db_session.add(Organization(id=other_owner.org_id, name="Second synthetic tenant"))
    db_session.add(
        User(
            id=other_owner.user_id,
            org_id=other_owner.org_id,
            team_id=other_owner.team_id,
            email="owner-other@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, other_owner)
    await db_session.commit()

    async def grant_scope(selection, owner, operation_id, attempt_id):
        app.dependency_overrides[get_current_user] = lambda: owner
        response = await test_client.post(selection["path"], json=selection["body"], headers=selection["headers"])
        assert response.status_code == 201, response.text
        collection = await db_session.get(ConnectorCollectionGrant, (owner.org_id, response.json()["id"]))
        binding = await db_session.get(ProductSourceBinding, (owner.org_id, collection.binding_id))
        delegation = CredentialServiceDelegation(
            tenant_id=owner.org_id,
            credential_id=binding.credential_id,
            service_id=collection.consumer,
            product_id=selection["path"].split("/")[3],
            project_id=selection["project_id"],
            created_by=owner.user_id,
            operations=["product:read", "source:read"],
            sources=collection.source_selections,
            capabilities=[],
            visibility="restricted",
            operation_id=operation_id,
            attempt_id=attempt_id,
            lease_fence=2,
            binding_generation=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add(delegation)
        await db_session.commit()
        body = {
            "product_id": delegation.product_id,
            "project_id": delegation.project_id,
            "grant_id": collection.id,
            "delegation_id": delegation.id,
            "delegation_revision": 1,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
        }
        return collection, binding, delegation, body

    same = await grant_scope(same_tenant, source_member, "run-same", "attempt-same")
    app.dependency_overrides[get_current_user] = lambda: other_owner
    other_tenant = await make_registered_selection(
        test_client,
        db_session,
        other_owner,
        suffix="-other",
        workload_id="registered-worker-other",
    )
    other = await grant_scope(other_tenant, other_owner, "run-other", "attempt-other")

    scopes = [
        (base["collection"], base["binding"], base["delegation"], base["body"]),
        same,
        other,
    ]
    expected_routes = {
        (source_member.org_id, base["body"]["project_id"]): (
            "cell-one",
            "controlled-db-ref",
            "controlled-role-ref",
            "restricted/",
            "registered-worker",
        ),
        (source_member.org_id, same_tenant["project_id"]): (
            "cell-one-same",
            "controlled-db-ref-same",
            "controlled-role-ref-same",
            "restricted-same/",
            "registered-worker-same",
        ),
        (other_owner.org_id, other_tenant["project_id"]): (
            "cell-one-other",
            "controlled-db-ref-other",
            "controlled-role-ref-other",
            "restricted-other/",
            "registered-worker-other",
        ),
    }
    assert len(expected_routes) == 3 and len({tenant for tenant, _ in expected_routes}) == 2
    assert sum(tenant == source_member.org_id for tenant, _ in expected_routes) == 2
    async with db_session_factory() as independent:
        cells = (await independent.scalars(select(ConnectorCell))).all()
        assert {
            (cell.tenant_id, cell.project_id): (cell.id, cell.database_ref, cell.role_ref, cell.object_prefix, cell.workload_id) for cell in cells
        } == expected_routes
        assert len((await independent.scalars(select(ConnectorCollectionGrant))).all()) == 3

    selected_actor = {"identity": base["service"]}

    async def verified(request: Request):
        request.state.token_context = selected_actor["identity"]

    app.dependency_overrides[verify_internal_or_irsa] = verified
    fixed_oracle = {
        (
            collection.tenant_id,
            delegation.product_id,
            collection.project_id,
            binding.credential_id,
            delegation.operation_id,
            delegation.attempt_id,
        ): (
            collection.consumer,
            {
                "tenant_id": collection.tenant_id,
                "project_id": collection.project_id,
                "source_connection_id": binding.credential_id,
                "record_type": "document",
                "record_id": "document-one",
                "record_revision": "revision-one",
            },
        )
        for collection, binding, delegation, _ in scopes
    }

    async def permit(current, requested):
        key = (current.tenant_id, current.product_id, current.project_id, current.credential_id, requested.operation_id, requested.attempt_id)
        expected = fixed_oracle.get(key)
        if expected is None or expected != (current.service_id, requested.connection_ref.model_dump()):
            raise HTTPException(404, detail={"code": "not_found"})
        return L5WorkPermit(*key, lease_fence=2, binding_generation=1, expires_at=datetime.now(UTC) + timedelta(hours=1))

    app.dependency_overrides[get_connector_work_authorizer] = lambda: permit
    handles = []
    for collection, binding, _, body in scopes:
        selected_actor["identity"] = base["service"].model_copy(
            update={
                "user_id": f"iam-agent:{collection.consumer}",
                "agent_registry_id": collection.consumer,
            }
        )
        response = await test_client.post("/internal/v1/connector-contexts/resolve", json=body)
        assert response.status_code == 201, response.text
        assert set(response.json()) == {"context_version", "handle", "expires_at"}
        handle = response.json()["handle"]
        handles.append(handle)
        readback = await test_client.get(f"/internal/v1/connector-contexts/{handle}")
        assert readback.status_code == 200, readback.text
        expected_cell, expected_db, expected_role, expected_prefix, _ = expected_routes[(collection.tenant_id, collection.project_id)]
        assert readback.json()["cell_id"] == expected_cell
        assert readback.json()["destination"] == {
            "database_ref": expected_db,
            "role_ref": expected_role,
            "object_prefix": expected_prefix,
        }
        assert readback.json()["source_selections"] == [
            fixed_oracle[
                (collection.tenant_id, body["product_id"], collection.project_id, binding.credential_id, body["operation_id"], body["attempt_id"])
            ][1]
        ]

    assert len(set(handles)) == 3
    selected_actor["identity"] = base["service"]
    for _, _, _, body in (same, other):
        assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=body)).status_code == 404
    foreign_project = {**base["body"], "project_id": same_tenant["project_id"]}
    assert (await test_client.post("/internal/v1/connector-contexts/resolve", json=foreign_project)).status_code == 404
    selected_actor["identity"] = base["service"].model_copy(update={"agent_registry_id": "registered-worker-same"})
    assert (await test_client.get(f"/internal/v1/connector-contexts/{handles[0]}")).status_code == 404
    async with db_session_factory() as independent:
        assert len((await independent.scalars(select(ConnectorOperationContext))).all()) == 3


@pytest.mark.asyncio
async def test_private_dependency_graph_cannot_use_shared_route(test_client, db_session, operation_authority):
    collection = operation_authority["collection"]
    original = collection.source_selections[0]
    ci = {**original, "record_type": "ci_report", "record_id": "ci-one"}
    cost = {**original, "record_type": "execution_cost", "record_id": "cost-one"}
    for reference, policies, dependencies in (
        (original, [], [ci, cost]),
        (ci, ["ci-private"], []),
        (cost, ["cost-private"], []),
    ):
        digest = hashlib.sha256(json.dumps(reference, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db_session.add(
            ConnectorEvidenceDependency(
                tenant_id=collection.tenant_id,
                project_id=collection.project_id,
                cell_id=collection.cell_id,
                identity_hash=digest,
                revision=1,
                identity=reference,
                policy_refs=policies,
                depends_on=dependencies,
            )
        )
    await db_session.commit()
    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=operation_authority["body"])
    assert created.status_code == 201, created.text
    path = f"/internal/v1/connector-contexts/{created.json()['handle']}"
    readback = await test_client.get(path)
    assert readback.status_code == 200, readback.text
    assert readback.json()["policy_envelope"]["audience"] == "private"
    assert readback.json()["destinations"] == {
        "raw": "restricted/raw/",
        "quarantine": "restricted/quarantine/",
        "results": "restricted/results/",
    }
    derived = await test_client.post(path + "/derive", json={"root": original, "asserted_audience": "private"})
    assert derived.status_code == 200, derived.text
    assert derived.json()["policy_envelope"]["identity"] == original
    assert derived.json()["policy_envelope"]["dependency_identities"] == [ci, original, cost]
    assert [item["ref"] for item in derived.json()["policy_envelope"]["required_policies"]] == [
        "ci-private",
        "cost-private",
        "evidence-private",
        "source-private",
    ]
    assert [item["revision"] for item in derived.json()["policy_envelope"]["dependency_versions"]] == [1, 1, 1]
    assert all(len(item["metadata_digest"]) == 64 for item in derived.json()["policy_envelope"]["dependency_versions"])
    assert derived.json()["destinations"] == readback.json()["destinations"]

    assert (await test_client.post(path + "/derive", json={"root": original, "asserted_audience": "shared"})).status_code == 409
    assert (await test_client.post(path + "/derive", json={"root": {**original, "record_id": "unregistered"}})).status_code == 409
    assert (await test_client.post(path + "/derive", json={"root": {**original, "tenant_id": "foreign"}})).status_code == 404
    cell = await db_session.get(ConnectorCell, (collection.tenant_id, collection.cell_id))
    cell.audience = "shared"
    cell.object_prefix = "shared/"
    await db_session.commit()
    assert (await test_client.get(path)).status_code == 409


@pytest.mark.asyncio
async def test_shared_cell_requires_shared_connection_and_compatible_policy(test_client, db_session, app, source_member, operation_authority):
    scope = operation_authority
    project_id = scope["body"]["project_id"]
    tenant_id = scope["collection"].tenant_id
    descriptor = {
        "version": 1,
        "audience_expression": "current:team-readers",
        "policies": [
            {"ref": "team-source", "revision": 1, "visibility": "shared", "audience_expression": "current:team-readers"},
            {"ref": "team-evidence", "revision": 1, "visibility": "shared", "audience_expression": "current:team-readers"},
            {"ref": "ci-personal", "revision": 1, "visibility": "private", "audience_expression": "current:owner"},
        ],
    }
    digest = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    db_session.add(
        ConnectorPolicyRealm(
            tenant_id=tenant_id,
            project_id=project_id,
            id="shared-realm",
            revision=1,
            descriptor_digest=digest,
            descriptor=descriptor,
            source_policy_ref="team-source",
            evidence_policy_ref="team-evidence",
        )
    )
    db_session.add(
        ConnectorCell(
            tenant_id=tenant_id,
            project_id=project_id,
            id="shared-cell",
            realm_id="shared-realm",
            realm_revision=1,
            database_ref="shared-db",
            role_ref="shared-role",
            object_prefix="shared/",
            workload_id="registered-worker",
            audience="shared",
        )
    )
    shared_credential = UserCredential(
        org_id=tenant_id,
        team_id=source_member.team_id,
        service="document",
        credential_type="api_key",
        label="synthetic-team-source",
        secret_arn="synthetic-team-ref",
    )
    db_session.add(shared_credential)
    await db_session.flush()
    binding = ProductSourceBinding(
        tenant_id=tenant_id,
        project_id=project_id,
        credential_id=shared_credential.id,
        kind="document",
        provider="document",
        namespace="team",
        native_id="team-document",
        desired={},
        observed={
            "observed_at": datetime.now(UTC).isoformat(),
            "stale_after": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
            "source_revision": "revision-one",
        },
        status="healthy",
    )
    db_session.add(binding)
    await db_session.commit()
    grant_response = await test_client.post(
        f"/v1/products/{scope['body']['product_id']}/projects/{project_id}/collection-grants",
        json={
            "binding_id": binding.id,
            "binding_generation": 1,
            "binding_revision": 1,
            "cell_id": "shared-cell",
            "consumer": "registered-worker",
            "purpose": "collection",
        },
        headers={"If-Profile-Revision": "1", "Idempotency-Key": "shared_collection_key_123456"},
    )
    assert grant_response.status_code == 201, grant_response.text
    shared_grant = await db_session.get(ConnectorCollectionGrant, (tenant_id, grant_response.json()["id"]))
    delegation = CredentialServiceDelegation(
        tenant_id=tenant_id,
        credential_id=shared_credential.id,
        service_id="registered-worker",
        product_id=scope["body"]["product_id"],
        project_id=project_id,
        created_by=source_member.user_id,
        operations=["product:read", "source:read"],
        sources=shared_grant.source_selections,
        capabilities=[],
        visibility="restricted",
        operation_id="run-shared",
        attempt_id="attempt-shared",
        lease_fence=2,
        binding_generation=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(delegation)
    await db_session.commit()
    expected_source = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_connection_id": shared_credential.id,
        "record_type": "document",
        "record_id": "team-document",
        "record_revision": "revision-one",
    }

    async def fixed_permit(current, requested):
        if current.id != delegation.id or requested.connection_ref.model_dump() != expected_source:
            raise HTTPException(404, detail={"code": "not_found"})
        return L5WorkPermit(
            tenant_id,
            delegation.product_id,
            project_id,
            shared_credential.id,
            "run-shared",
            "attempt-shared",
            lease_fence=2,
            binding_generation=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    app.dependency_overrides[get_connector_work_authorizer] = lambda: fixed_permit
    body = {
        "product_id": delegation.product_id,
        "project_id": project_id,
        "grant_id": shared_grant.id,
        "delegation_id": delegation.id,
        "delegation_revision": 1,
        "operation_id": "run-shared",
        "attempt_id": "attempt-shared",
    }
    shared_context = await test_client.post("/internal/v1/connector-contexts/resolve", json=body)
    assert shared_context.status_code == 201, shared_context.text
    readback = await test_client.get(f"/internal/v1/connector-contexts/{shared_context.json()['handle']}")
    assert readback.status_code == 200, readback.text
    assert readback.json()["policy_envelope"]["audience"] == "shared"
    assert readback.json()["destinations"] == {"raw": "shared/raw/", "results": "shared/results/"}

    shared_credential.user_id = source_member.user_id
    shared_credential.team_id = None
    await db_session.commit()
    assert (await test_client.get(f"/internal/v1/connector-contexts/{shared_context.json()['handle']}")).status_code == 409
    shared_credential.user_id = None
    shared_credential.team_id = source_member.team_id
    await db_session.commit()

    root = shared_grant.source_selections[0]
    restricted = {**root, "record_type": "ci_report", "record_id": "private-ci"}
    for identity, policies, dependencies in ((root, [], [restricted]), (restricted, ["ci-personal"], [])):
        db_session.add(
            ConnectorEvidenceDependency(
                tenant_id=tenant_id,
                project_id=project_id,
                cell_id="shared-cell",
                identity_hash=hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                revision=1,
                identity=identity,
                policy_refs=policies,
                depends_on=dependencies,
            )
        )
    await db_session.commit()
    derive_path = f"/internal/v1/connector-contexts/{shared_context.json()['handle']}/derive"
    denied_readback = await test_client.get(f"/internal/v1/connector-contexts/{shared_context.json()['handle']}")
    assert denied_readback.status_code == 409 and denied_readback.json()["detail"]["code"] == "policy_denied"
    for request in ({"root": root, "asserted_audience": "shared"}, {"root": root}, {"root": {**root, "record_id": "unknown"}}):
        denied = await test_client.post(derive_path, json=request)
        assert denied.status_code == 409 and denied.json()["detail"]["code"] == "policy_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "expired", "cancelled", "attempt", "fence", "source", "cell", "grant", "consumer", "binding"])
async def test_collection_context_reads_durable_product_work_lease(test_client, db_session, app, operation_authority, change):
    from src.products.models import ProductOperation

    authority = operation_authority
    delegation = authority["delegation"]
    body = authority["body"]
    operation = ProductOperation(
        tenant_id=delegation.tenant_id,
        id=body["operation_id"],
        product_id=delegation.product_id,
        project_id=delegation.project_id,
        binding_id=authority["binding"].id,
        binding_generation=authority["binding"].generation,
        actor_id=delegation.created_by,
        operation="collectConnectorSource",
        key_hash="a" * 64,
        request_hash="b" * 64,
        state="running",
        lease_owner=body["attempt_id"],
        lease_until=datetime.now(UTC) + timedelta(seconds=60),
        attempt_count=2,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        request={
            "consumer": authority["service"].agent_registry_id,
            "delegation_id": delegation.id,
            "delegation_revision": delegation.revision,
            "source_ref": authority["collection"].source_selections[0],
            "cell_id": authority["collection"].cell_id,
            "grant_id": authority["collection"].id,
        },
    )
    db_session.add(operation)
    await db_session.commit()
    app.dependency_overrides.pop(get_connector_work_authorizer)
    created = await test_client.post("/internal/v1/connector-contexts/resolve", json=body)
    assert created.status_code == 201, created.text
    endpoint = "/internal/v1/connector-contexts/" + created.json()["handle"]
    current = await test_client.get(endpoint)
    assert current.status_code == 200, current.text
    assert current.json()["lease_fence"] == 2
    assert current.json()["binding_id"] == authority["binding"].id
    assert current.json()["module_generation"] == 1
    assert datetime.fromisoformat(current.json()["expires_at"]) <= operation.lease_until.replace(tzinfo=UTC)
    if change == "none":
        return
    if change == "expired":
        operation.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "cancelled":
        operation.state = "cancelled"
    elif change == "attempt":
        operation.lease_owner = "new-attempt"
    elif change == "fence":
        operation.attempt_count += 1
    elif change == "binding":
        operation.binding_generation += 1
    else:
        field = {"source": "source_ref", "cell": "cell_id", "grant": "grant_id", "consumer": "consumer"}[change]
        operation.request = {**operation.request, field: "foreign"}
    await db_session.commit()
    denied = await test_client.get(endpoint)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "stale_collection_work"
