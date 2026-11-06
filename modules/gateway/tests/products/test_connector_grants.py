"""Collection grants require an owned, current source and an admitted cell."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.products.models import ConnectorCell, ConnectorCollectionGrant, ConnectorPolicyRealm, ProductOperation, ProductSourceBinding
from src.shared.models.vault import UserCredential
from tests.products.test_i01 import HEADERS, REQUEST


async def make_registered_selection(test_client, db_session, source_member, *, suffix="", workload_id="registered-worker"):
    headers = HEADERS if not suffix else {"Idempotency-Key": f"run_owned_key_123456{suffix}"}
    created = await test_client.post("/v1/products", json={**REQUEST, "name": f"Example Planner{suffix}"}, headers=headers)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    clock = datetime.now(UTC)
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="document",
        credential_type="api_key",
        label=f"synthetic-scope-source{suffix}",
        secret_arn="synthetic-test-ref",
    )
    db_session.add(credential)
    await db_session.flush()
    binding = ProductSourceBinding(
        tenant_id=source_member.org_id,
        project_id=project_id,
        credential_id=credential.id,
        kind="document",
        provider="document",
        namespace="private",
        native_id="document-one",
        desired={},
        observed={"observed_at": clock.isoformat(), "stale_after": (clock + timedelta(minutes=20)).isoformat(), "source_revision": "revision-one"},
        status="healthy",
    )
    db_session.add(binding)
    descriptor = {
        "version": 1,
        "audience_expression": "current:owner",
        "policies": [
            {"ref": "source-private", "revision": 1, "visibility": "private", "audience_expression": "current:owner"},
            {"ref": "evidence-private", "revision": 1, "visibility": "private", "audience_expression": "current:owner"},
            {"ref": "ci-private", "revision": 1, "visibility": "private", "audience_expression": "current:ci-owner"},
            {"ref": "cost-private", "revision": 1, "visibility": "private", "audience_expression": "current:billing-owner"},
        ],
    }
    digest = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    db_session.add(
        ConnectorPolicyRealm(
            tenant_id=source_member.org_id,
            project_id=project_id,
            id=f"realm-one{suffix}",
            revision=1,
            descriptor_digest=digest,
            descriptor=descriptor,
            source_policy_ref="source-private",
            evidence_policy_ref="evidence-private",
        )
    )
    await db_session.flush()
    db_session.add(
        ConnectorCell(
            tenant_id=source_member.org_id,
            project_id=project_id,
            id=f"cell-one{suffix}",
            realm_id=f"realm-one{suffix}",
            realm_revision=1,
            database_ref=f"controlled-db-ref{suffix}",
            role_ref=f"controlled-role-ref{suffix}",
            object_prefix=f"restricted{suffix}/",
            workload_id=workload_id,
            audience="private",
        )
    )
    await db_session.commit()
    return {
        "path": f"/v1/products/{product_id}/projects/{project_id}/collection-grants",
        "headers": {"Idempotency-Key": f"example_collection_key_123456{suffix}", "If-Profile-Revision": "1"},
        "body": {
            "binding_id": binding.id,
            "binding_generation": 1,
            "binding_revision": 1,
            "cell_id": f"cell-one{suffix}",
            "consumer": workload_id,
            "purpose": "collection",
        },
        "project_id": project_id,
        "tenant_id": source_member.org_id,
    }


@pytest.fixture
async def registered_selection(test_client, db_session, source_member):
    return await make_registered_selection(test_client, db_session, source_member)


@pytest.mark.asyncio
async def test_collection_grant_uses_current_authority_and_server_source(test_client, db_session, registered_selection):
    selection = registered_selection
    created = await test_client.post(selection["path"], json=selection["body"], headers=selection["headers"])
    assert created.status_code == 201, created.text
    grant = await db_session.scalar(select(ConnectorCollectionGrant))
    assert grant.id == created.json()["id"]
    assert grant.source_selections == [
        {
            "tenant_id": selection["tenant_id"],
            "project_id": selection["project_id"],
            "source_connection_id": (await db_session.get(ProductSourceBinding, (selection["tenant_id"], grant.binding_id))).credential_id,
            "record_type": "document",
            "record_id": "document-one",
            "record_revision": "revision-one",
        }
    ]
    assert grant.consumer == "registered-worker" and grant.policy_revision == 1
    replay = await test_client.post(selection["path"], json=selection["body"], headers=selection["headers"])
    assert replay.status_code == 200 and replay.json() == created.json()
    assert len((await db_session.scalars(select(ConnectorCollectionGrant))).all()) == 1
    assert len((await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "createCollectionGrant"))).all()) == 1


@pytest.mark.asyncio
async def test_collection_grant_denies_unregistered_selection_and_stale_authority(test_client, db_session, app, source_member, registered_selection):
    selection = registered_selection
    for changed in ({"cell_id": "foreign-cell"}, {"consumer": "unregistered-worker"}, {"database_ref": "injected"}):
        response = await test_client.post(selection["path"], json={**selection["body"], **changed}, headers=selection["headers"])
        assert response.status_code in (404, 422)
    assert (await db_session.scalars(select(ConnectorCollectionGrant))).all() == []

    service = source_member.model_copy(update={"account_type": "service", "auth_source": "iam", "agent_registry_id": "registered-worker"})
    app.dependency_overrides[get_current_user] = lambda: service
    assert (await test_client.post(selection["path"], json=selection["body"], headers=selection["headers"])).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: source_member

    second = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Second Project"},
        headers={"Idempotency-Key": "second_product_scope_123456"},
    )
    assert second.status_code == 201
    other_project_path = selection["path"].replace(selection["project_id"], second.json()["project_id"])
    assert (await test_client.post(other_project_path, json=selection["body"], headers=selection["headers"])).status_code == 404

    binding = await db_session.get(ProductSourceBinding, (selection["tenant_id"], selection["body"]["binding_id"]))
    credential = await db_session.get(UserCredential, binding.credential_id)
    credential.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    assert (await test_client.post(selection["path"], json=selection["body"], headers=selection["headers"])).status_code == 404
    credential.expires_at = None
    binding = await db_session.get(ProductSourceBinding, (selection["tenant_id"], selection["body"]["binding_id"]))
    binding.status = "revoked"
    await db_session.commit()
    assert (await test_client.post(selection["path"], json=selection["body"], headers=selection["headers"])).status_code == 409
    assert (await db_session.scalars(select(ConnectorCollectionGrant))).all() == []
