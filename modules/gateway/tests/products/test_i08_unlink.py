"""Unlink fences verification while retaining the shared provider connection."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.models import ProductOperation, ProductSourceBinding, Project
from src.products.verification_operations import tombstone_binding
from src.shared.models.vault import ChannelTenantMap
from tests.products.test_i08_api import mutation, setup_source


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "running", "failed"])
async def test_unlink_tombstones_attempt_without_uninstalling_connection(
    state,
    test_client,
    db_session,
    source_member,
):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_unlink_12345"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    product_id, project_id = path.split("/")[3], path.split("/")[5]
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))
    project = await db_session.get(Project, (source_member.org_id, project_id))
    project.configuration = {
        "basic": {
            "repositories": [
                {
                    "tenant_id": source_member.org_id,
                    "project_id": project_id,
                    "source_connection_id": binding.installation_id,
                    "record_id": binding.native_id,
                    "record_revision": "1",
                }
            ]
        }
    }
    operation = ProductOperation(
        tenant_id=source_member.org_id,
        actor_id=source_member.user_id,
        operation="verifyBinding",
        key_hash="a" * 64,
        request_hash="b" * 64,
        state=state,
        request={"binding_id": binding_id, "generation": 1},
        product_id=product_id,
        project_id=project_id,
        binding_id=binding_id,
        binding_generation=1,
        expected_revision=2,
        lease_owner="attempt-owner" if state == "running" else None,
        lease_until=datetime.now(UTC) + timedelta(minutes=1) if state == "running" else None,
        retry_at=datetime.now(UTC) + timedelta(minutes=1) if state == "failed" else None,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(operation)
    await db_session.commit()
    operation_id = operation.id
    connection = await db_session.get(ChannelTenantMap, binding.installation_id)
    connection_id, installation_id = connection.id, connection.installation_id
    await tombstone_binding(db_session, binding)
    await db_session.commit()
    db_session.expire_all()
    saved = await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))
    assert (saved.status, saved.generation, saved.revision) == ("unlinked", 2, 3)
    assert saved.unlinked_at is not None and saved.observed is None
    cancelled = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert (cancelled.state, cancelled.receipt) == ("cancelled", {"coverage": "denied"})
    assert cancelled.lease_owner is None and cancelled.lease_until is None and cancelled.retry_at is None
    assert (await db_session.get(ChannelTenantMap, connection_id)).installation_id == installation_id

    listing = await test_client.get(path)
    assert listing.status_code == 200
    assert listing.json()["items"] == [
        {
            "id": binding_id,
            "project_id": project_id,
            "generation": 2,
            "revision": 3,
            "status": "unlinked",
        }
    ]
    profile = await test_client.get(path.removesuffix("/bindings"))
    assert profile.status_code == 200 and selection["native_id"] not in str(profile.json())
    assert (await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "late_retry_12345"))).status_code == 404

    if state == "pending":
        with pytest.raises(HTTPException) as error:
            await tombstone_binding(db_session, saved)
        assert error.value.status_code == 409
        await db_session.rollback()
        rebound = await test_client.post(path, json=selection, headers=mutation(2, "rebind_after_unlink_123"))
        assert rebound.status_code == 201 and rebound.json()["id"] != binding_id
        assert (await db_session.get(ProductOperation, (source_member.org_id, operation_id))).state == "cancelled"


@pytest.mark.asyncio
async def test_unlink_while_provider_read_is_pending_rejects_late_completion(
    test_client,
    db_session,
    test_engine,
    source_member,
    monkeypatch,
):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_late_unlink_123"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    calls = []

    async def observe_after_unlink(binding, installation):
        calls.append((binding.native_id, installation.id))
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as writer:
            current = await writer.get(ProductSourceBinding, (source_member.org_id, binding_id))
            await tombstone_binding(writer, current)
            await writer.commit()
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": datetime.now(UTC).isoformat(),
                "stale_after": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "source_revision": "must-not-publish",
                "native_id": binding.native_id,
                "namespace": binding.namespace,
            },
        }

    monkeypatch.setattr("src.products.source_routes.observe_binding", observe_after_unlink)
    result = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_late_unlink_123"))
    assert result.status_code == 202 and result.json()["status"] == "cancelled"
    assert calls == [(selection["native_id"], selection["connection_ref"]["id"])]
    db_session.expire_all()
    saved = await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))
    assert saved.status == "unlinked" and saved.generation == 2 and saved.observed is None
    operation = await db_session.get(ProductOperation, (source_member.org_id, result.json()["operation_id"]))
    assert operation.state == "cancelled" and operation.receipt == {"coverage": "denied"}
    assert (await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])) is not None
