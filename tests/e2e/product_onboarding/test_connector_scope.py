"""Controlled connector cell scenarios use the gateway routes without a deployment."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import delete, select

from modules.gateway.tests import conftest as gateway_test_environment
from src.auth.dependencies import get_current_user
from src.internal import connector_context_routes
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import L5WorkPermit, get_l5_work_authorizer
from src.products.models import ConnectorCell, ConnectorCollectionGrant, ConnectorEvidenceDependency, ConnectorModuleAdmission, ConnectorOperationContext, ConnectorPolicyRealm, ProductSourceBinding
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.vault import CredentialServiceDelegation, UserCredential

pytest_plugins = (gateway_test_environment.__name__,)

CONTEXTS = "/internal/v1/connector-contexts"
PRODUCT = {"name": "Example Connector Profile", "purpose": "Collect registered evidence", "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}}
PRIVATE_DESCRIPTOR = {
    "version": 1,
    "audience_expression": "current:owner",
    "policies": [
        {"ref": "source-private", "revision": 1, "visibility": "private", "audience_expression": "current:owner"},
        {"ref": "evidence-private", "revision": 1, "visibility": "private", "audience_expression": "current:owner"},
        {"ref": "ci-private", "revision": 1, "visibility": "private", "audience_expression": "current:ci-owner"},
        {"ref": "cost-private", "revision": 1, "visibility": "private", "audience_expression": "current:billing-owner"},
    ],
}
SHARED_DESCRIPTOR = {
    "version": 1,
    "audience_expression": "current:team-readers",
    "policies": [
        {"ref": "team-source", "revision": 1, "visibility": "shared", "audience_expression": "current:team-readers"},
        {"ref": "team-evidence", "revision": 1, "visibility": "shared", "audience_expression": "current:team-readers"},
        {"ref": "ci-personal", "revision": 1, "visibility": "private", "audience_expression": "current:ci-owner"},
    ],
}


@pytest.fixture
async def connector_authority(test_client, db_session, app, valid_token_context, monkeypatch):
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    monkeypatch.setenv("DEVLAKE_COLLECTION_ENABLED", "true")
    primary = valid_token_context
    second_tenant = primary.model_copy(update={"org_id": "tenant-scenario-other", "user_id": "owner-scenario-other", "team_id": "team-scenario-other"})
    for owner in (primary, second_tenant):
        db_session.add(Organization(id=owner.org_id, name=f"Synthetic {owner.org_id}"))
        db_session.add(User(id=owner.user_id, org_id=owner.org_id, team_id=owner.team_id, email=f"{owner.user_id}@example.test", user_kind="human"))
        db_session.add(Team(id=owner.team_id, org_id=owner.org_id, department_id="fixture-department", name=owner.team_id))
        db_session.add(TeamMembership(user_id=owner.user_id, org_id=owner.org_id, team_id=owner.team_id, is_primary=True))
    await db_session.commit()

    async def register(owner, suffix, *, shared=False):
        app.dependency_overrides[get_current_user] = lambda: owner
        created = await test_client.post("/v1/products", json={**PRODUCT, "name": f"Example Connector {suffix}"}, headers={"Idempotency-Key": f"connector-scenario-product-{suffix}"})
        assert created.status_code == 201, created.text
        product_id, project_id = created.json()["product_id"], created.json()["project_id"]
        credential = UserCredential(org_id=owner.org_id, user_id=None if shared else owner.user_id, team_id=owner.team_id if shared else None, service="document", credential_type="api_key", label=f"synthetic-scenario-{suffix}", secret_arn="synthetic-test-ref")
        db_session.add(credential)
        await db_session.flush()
        clock = datetime.now(UTC)
        binding = ProductSourceBinding(
            tenant_id=owner.org_id, project_id=project_id, credential_id=credential.id, kind="document", provider="document",
            namespace="team" if shared else "private", native_id=f"document-{suffix}", desired={}, status="healthy",
            observed={"observed_at": clock.isoformat(), "stale_after": (clock + timedelta(minutes=20)).isoformat(), "source_revision": "revision-one"},
        )
        db_session.add(binding)
        descriptor = SHARED_DESCRIPTOR if shared else PRIVATE_DESCRIPTOR
        realm = ConnectorPolicyRealm(
            tenant_id=owner.org_id, project_id=project_id, id=f"realm-{suffix}", revision=1,
            descriptor_digest=hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            descriptor=descriptor, source_policy_ref="team-source" if shared else "source-private", evidence_policy_ref="team-evidence" if shared else "evidence-private",
        )
        db_session.add(realm)
        await db_session.flush()
        cell = ConnectorCell(
            tenant_id=owner.org_id, project_id=project_id, id=f"cell-{suffix}", realm_id=realm.id, realm_revision=1,
            database_ref=f"db-{suffix}", role_ref=f"role-{suffix}", object_prefix=f"{'shared' if shared else 'private'}-{suffix}/",
            workload_id=f"registered-worker-{suffix}", audience="shared" if shared else "private",
        )
        db_session.add(cell)
        await db_session.commit()
        selected = await test_client.post(
            f"/v1/products/{product_id}/projects/{project_id}/collection-grants",
            json={"binding_id": binding.id, "binding_revision": 1, "binding_generation": 1, "cell_id": cell.id, "consumer": cell.workload_id, "purpose": "collection"},
            headers={"Idempotency-Key": f"connector-scenario-grant-{suffix}", "If-Profile-Revision": "1"},
        )
        assert selected.status_code == 201, selected.text
        source = {"tenant_id": owner.org_id, "project_id": project_id, "source_connection_id": credential.id, "record_type": "document", "record_id": f"document-{suffix}", "record_revision": "revision-one"}
        grant = await db_session.get(ConnectorCollectionGrant, (owner.org_id, selected.json()["id"]))
        assert grant.source_selections == [source]
        delegation = CredentialServiceDelegation(
            tenant_id=owner.org_id, credential_id=credential.id, service_id=cell.workload_id, product_id=product_id,
            project_id=project_id, created_by=owner.user_id, operations=["product:read", "source:read"], sources=[source],
            capabilities=[], visibility="restricted", operation_id=f"run-{suffix}", attempt_id=f"attempt-{suffix}",
            lease_fence=2, binding_generation=1, expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add(delegation)
        await db_session.commit()
        worker = owner.model_copy(update={
            "user_id": f"iam-agent:{cell.workload_id}", "agent_registry_id": cell.workload_id,
            "org_id": "platform-service", "account_type": "service", "auth_source": "iam", "scope": "internal",
        })
        body = {"product_id": product_id, "project_id": project_id, "grant_id": grant.id, "delegation_id": delegation.id,
                "delegation_revision": 1, "operation_id": delegation.operation_id, "attempt_id": delegation.attempt_id}
        return {"owner": owner, "worker": worker, "cell": cell, "grant": grant, "delegation": delegation, "body": body, "source": source,
                "destination": {"database_ref": f"db-{suffix}", "role_ref": f"role-{suffix}", "object_prefix": f"{'shared' if shared else 'private'}-{suffix}/"}}

    scopes = [await register(primary, "one", shared=True), await register(primary, "same"), await register(second_tenant, "other")]
    selected_actor = {"identity": scopes[0]["worker"]}

    async def verified(request: Request):
        request.state.token_context = selected_actor["identity"]

    app.dependency_overrides[verify_internal_or_irsa] = verified
    permitted = {
        scope["delegation"].id: (
            scope["owner"].org_id, scope["body"]["product_id"], scope["body"]["project_id"],
            scope["source"]["source_connection_id"], scope["body"]["operation_id"], scope["body"]["attempt_id"],
            scope["source"], scope["worker"].agent_registry_id,
        )
        for scope in scopes
    }

    async def admitted_work(current, requested):
        allowed = permitted.get(current.id)
        if allowed is None or allowed[:6] != (
            current.tenant_id, current.product_id, current.project_id, current.credential_id, requested.operation_id, requested.attempt_id
        ) or allowed[6] != requested.connection_ref.model_dump() or allowed[7] != current.service_id:
            raise HTTPException(404, detail={"code": "not_found"})
        return L5WorkPermit(*allowed[:6], lease_fence=2, binding_generation=1, expires_at=datetime.now(UTC) + timedelta(hours=1))

    app.dependency_overrides[get_l5_work_authorizer] = lambda: admitted_work
    return scopes, selected_actor


@pytest.mark.asyncio
async def test_context_authority(test_client, db_session_factory, app, connector_authority):
    scopes, selected_actor = connector_authority
    assert len({scope["owner"].org_id for scope in scopes}) == 2
    assert len({scope["body"]["project_id"] for scope in scopes[:2]}) == 2
    async with db_session_factory() as independent:
        rows = (await independent.scalars(select(ConnectorCell))).all()
        assert {(row.tenant_id, row.project_id): (row.id, row.workload_id, row.database_ref, row.role_ref, row.object_prefix) for row in rows} == {
            (scope["owner"].org_id, scope["body"]["project_id"]): (
                scope["cell"].id, scope["worker"].agent_registry_id, *scope["destination"].values()
            ) for scope in scopes
        }
        assert len((await independent.scalars(select(ConnectorCollectionGrant))).all()) == 3
    handles = []
    for scope in scopes:
        selected_actor["identity"] = scope["worker"]
        created = await test_client.post(CONTEXTS + "/resolve", json=scope["body"])
        assert created.status_code == 201, created.text
        assert set(created.json()) == {"context_version", "handle", "expires_at"}
        handle = created.json()["handle"]
        handles.append(handle)
        readback = await test_client.get(f"{CONTEXTS}/{handle}")
        assert readback.status_code == 200, readback.text
        assert readback.json()["destination"] == scope["destination"]
        assert readback.json()["source_selections"] == [scope["source"]]
        assert (readback.json()["tenant_id"], readback.json()["project_id"], readback.json()["cell_id"]) == (
            scope["owner"].org_id, scope["body"]["project_id"], scope["cell"].id
        )
    assert len(set(handles)) == 3
    selected_actor["identity"] = scopes[0]["worker"]
    for foreign in scopes[1:]:
        assert (await test_client.post(CONTEXTS + "/resolve", json=foreign["body"])).status_code == 404
    for forged in ({"project_id": scopes[1]["body"]["project_id"]}, {"grant_id": scopes[2]["grant"].id},
                   {"tenant_id": scopes[2]["owner"].org_id}, {"database_ref": "unregistered-db"},
                   {"object_prefix": "shared/"}, {"audience": "shared"}):
        denied = await test_client.post(CONTEXTS + "/resolve", json={**scopes[0]["body"], **forged})
        assert denied.status_code in (404, 422), (forged, denied.text)
        assert "handle" not in denied.json()
    assert (await test_client.get(f"{CONTEXTS}/{handles[1]}")).status_code == 404
    app.dependency_overrides.pop(get_l5_work_authorizer)
    missing_permit = await test_client.post(CONTEXTS + "/resolve", json=scopes[0]["body"])
    assert missing_permit.status_code == 503 and "handle" not in missing_permit.json()
    async with db_session_factory() as independent:
        rows = (await independent.scalars(select(ConnectorOperationContext))).all()
        assert {(row.tenant_id, row.cell_id, row.id) for row in rows} == {
            (scope["owner"].org_id, scope["cell"].id, handle) for scope, handle in zip(scopes, handles, strict=True)
        }


def policy_dependency(scope, identity, policies=(), children=(), revision=1):
    return ConnectorEvidenceDependency(
        tenant_id=scope["owner"].org_id, project_id=scope["body"]["project_id"], cell_id=scope["cell"].id,
        identity_hash=hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        identity=identity, policy_refs=list(policies), depends_on=list(children), revision=revision,
    )


@pytest.mark.asyncio
async def test_policy_realms(test_client, db_session, db_session_factory, connector_authority):
    scopes, selected_actor = connector_authority
    shared, private = scopes[:2]
    ci = {**private["source"], "record_type": "ci_report", "record_id": "ci-one"}
    cost = {**private["source"], "record_type": "execution_cost", "record_id": "cost-one"}
    private_graph = [
        policy_dependency(private, private["source"], children=(ci,)),
        policy_dependency(private, ci, policies=("ci-private",), children=(cost,)),
        policy_dependency(private, cost, policies=("cost-private",)),
    ]
    db_session.add_all(private_graph + [policy_dependency(shared, shared["source"])])
    await db_session.commit()
    selected_actor["identity"] = private["worker"]
    private_created = await test_client.post(CONTEXTS + "/resolve", json=private["body"])
    assert private_created.status_code == 201, private_created.text
    private_path = f"{CONTEXTS}/{private_created.json()['handle']}"
    private_read = await test_client.get(private_path)
    assert private_read.status_code == 200, private_read.text
    envelope = private_read.json()["policy_envelope"]
    assert envelope["identity"] == private["source"]
    assert envelope["dependency_identities"] == [ci, private["source"], cost]
    assert [policy["ref"] for policy in envelope["required_policies"]] == ["ci-private", "cost-private", "evidence-private", "source-private"]
    assert [(policy["ref"], policy["audience_expression"]) for policy in envelope["required_policies"]][:2] == [
        ("ci-private", "current:ci-owner"), ("cost-private", "current:billing-owner")
    ]
    assert envelope["audience"] == private_read.json()["audience"] == "private"
    expected_versions = [
        {
            "identity": node.identity, "revision": 1,
            "metadata_digest": hashlib.sha256(json.dumps({"identity": node.identity, "policy_refs": node.policy_refs, "depends_on": node.depends_on, "revision": 1}, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        for node in (private_graph[1], private_graph[0], private_graph[2])
    ]
    assert envelope["dependency_versions"] == expected_versions
    assert private_read.json()["destination"] == private["destination"]
    assert private_read.json()["destinations"] == {
        "raw": "private-same/raw/", "quarantine": "private-same/quarantine/", "results": "private-same/results/"
    }
    derived = await test_client.post(private_path + "/derive", json={"root": private["source"], "asserted_audience": "private"})
    assert derived.status_code == 200 and derived.json()["policy_envelope"] == envelope
    for forged in ({"root": private["source"], "asserted_audience": "shared"},
                   {"root": {**private["source"], "record_id": "unknown"}}):
        denied = await test_client.post(private_path + "/derive", json=forged)
        assert denied.status_code == 409 and "destinations" not in denied.json()

    selected_actor["identity"] = shared["worker"]
    shared_created = await test_client.post(CONTEXTS + "/resolve", json=shared["body"])
    assert shared_created.status_code == 201, shared_created.text
    shared_path = f"{CONTEXTS}/{shared_created.json()['handle']}"
    shared_read = await test_client.get(shared_path)
    assert shared_read.status_code == 200, shared_read.text
    assert shared_read.json()["policy_envelope"]["identity"] == shared["source"]
    assert [policy["ref"] for policy in shared_read.json()["policy_envelope"]["required_policies"]] == ["team-evidence", "team-source"]
    assert shared_read.json()["audience"] == "shared"
    assert shared_read.json()["destination"] == shared["destination"]
    assert shared_read.json()["destinations"] == {"raw": "shared-one/raw/", "results": "shared-one/results/"}
    shared_derived = await test_client.post(shared_path + "/derive", json={"root": shared["source"], "asserted_audience": "shared"})
    assert shared_derived.status_code == 200 and shared_derived.json()["policy_envelope"] == shared_read.json()["policy_envelope"]
    assert shared_derived.json()["destinations"] == shared_read.json()["destinations"]

    private_ci = {**shared["source"], "record_type": "ci_report", "record_id": "personal-ci"}
    db_session.add_all([
        policy_dependency(shared, shared["source"], children=(private_ci,), revision=2),
        policy_dependency(shared, private_ci, policies=("ci-personal",)),
    ])
    await db_session.commit()
    denied_read = await test_client.get(shared_path)
    assert denied_read.status_code == 409 and "destination" not in denied_read.json()
    denied_derive = await test_client.post(shared_path + "/derive", json={"root": shared["source"], "asserted_audience": "shared"})
    assert denied_derive.status_code == 409 and "destinations" not in denied_derive.json()
    async with db_session_factory() as independent:
        stored = (await independent.scalars(select(ConnectorEvidenceDependency))).all()
        assert len(stored) == 6
        cells = (await independent.scalars(select(ConnectorCell))).all()
        assert {(cell.id, cell.audience, cell.object_prefix) for cell in cells} == {
            ("cell-one", "shared", "shared-one/"), ("cell-same", "private", "private-same/"),
            ("cell-other", "private", "private-other/")
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("transition,denied_status", [("membership", 404), ("source", 404), ("cell", 409), ("module", 409)])
async def test_revocation_replay(test_client, db_session, db_session_factory, connector_authority, monkeypatch, transition, denied_status):
    scopes, selected_actor = connector_authority
    owned, foreign = scopes[1:]
    clock = {"now": datetime.now(UTC)}
    monkeypatch.setattr(connector_context_routes, "utcnow", lambda: clock["now"])
    created = {}
    for scope in (owned, foreign):
        selected_actor["identity"] = scope["worker"]
        response = await test_client.post(CONTEXTS + "/resolve", json=scope["body"])
        assert response.status_code == 201, response.text
        created[scope["cell"].id] = response.json()
        replay = await test_client.post(CONTEXTS + "/resolve", json=scope["body"])
        assert replay.status_code == 200 and replay.json() == response.json()
    owned_handle = created[owned["cell"].id]["handle"]
    foreign_handle = created[foreign["cell"].id]["handle"]
    selected_actor["identity"] = owned["worker"]
    if transition == "membership":
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == owned["owner"].user_id))
        await db_session.commit()
    elif transition == "source":
        binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.id == owned["grant"].binding_id))
        binding.generation += 1
        await db_session.commit()
    elif transition == "cell":
        owned["cell"].state = "retired"
        await db_session.commit()
    else:
        path = f"{CONTEXTS}/module/{owned['owner'].org_id}"
        assert (await test_client.put(path, json={"enabled": False})).status_code == 403
        operator = owned["worker"].model_copy(update={
            "org_id": owned["owner"].org_id, "credential_scopes": ["connector:module:transition"]
        })
        selected_actor["identity"] = operator
        assert (await test_client.put(f"{CONTEXTS}/module/{foreign['owner'].org_id}", json={"enabled": False})).status_code == 403
        disabled = await test_client.put(path, json={"enabled": False})
        assert disabled.status_code == 200 and disabled.json() == {"enabled": False, "generation": 2}
        async with db_session_factory() as independent:
            admission = await independent.get(ConnectorModuleAdmission, owned["owner"].org_id)
            assert admission.enabled is False and admission.generation == 2
            assert admission.changed_at.replace(tzinfo=UTC) == clock["now"]
        selected_actor["identity"] = owned["worker"]
        assert (await test_client.post(CONTEXTS + "/resolve", json=owned["body"])).status_code == 503
        selected_actor["identity"] = operator
        clock["now"] += timedelta(seconds=1)
        enabled = await test_client.put(path, json={"enabled": True})
        assert enabled.status_code == 200 and enabled.json() == {"enabled": True, "generation": 3}
        selected_actor["identity"] = owned["worker"]

    denied_read = await test_client.get(f"{CONTEXTS}/{owned_handle}")
    denied_replay = await test_client.post(CONTEXTS + "/resolve", json=owned["body"])
    assert denied_read.status_code == (404 if transition == "module" else denied_status)
    assert denied_replay.status_code == denied_status
    assert "destination" not in denied_read.json() and "handle" not in denied_replay.json()
    selected_actor["identity"] = foreign["worker"]
    independent_replay = await test_client.post(CONTEXTS + "/resolve", json=foreign["body"])
    assert independent_replay.status_code == 200 and independent_replay.json() == created[foreign["cell"].id]
    assert (await test_client.get(f"{CONTEXTS}/{foreign_handle}")).status_code == 200
    async with db_session_factory() as independent:
        owned_row = await independent.get(ConnectorOperationContext, (owned["owner"].org_id, owned_handle))
        foreign_row = await independent.get(ConnectorOperationContext, (foreign["owner"].org_id, foreign_handle))
        assert owned_row.id != foreign_row.id and owned_row.module_generation == foreign_row.module_generation == 1
        assert foreign_row.revoked_at is None
        if transition == "module":
            assert owned_row.revoked_at.replace(tzinfo=UTC) == clock["now"] - timedelta(seconds=1)
            assert (await independent.get(ConnectorCollectionGrant, (owned["owner"].org_id, owned["grant"].id))).state == "revoked"
            assert (await independent.get(ConnectorCell, (owned["owner"].org_id, owned["cell"].id))).routing_revision == 2
            admission = await independent.get(ConnectorModuleAdmission, owned["owner"].org_id)
            assert admission.enabled is True and admission.generation == 3
        else:
            assert owned_row.revoked_at is None
        assert await independent.get(ConnectorModuleAdmission, foreign["owner"].org_id) is None
        assert len((await independent.scalars(select(ConnectorOperationContext))).all()) == 2
    clock["now"] = datetime.fromisoformat(created[foreign["cell"].id]["expires_at"])
    assert (await test_client.get(f"{CONTEXTS}/{foreign_handle}")).status_code == 404
