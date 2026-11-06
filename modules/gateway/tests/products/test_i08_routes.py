"""Gateway binding routes recover operations and unlink without provider mutation."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app import create_app
from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductOperation, ProductSourceBinding
from src.shared.database import get_db
from src.shared.models.vault import ChannelTenantMap
from tests.products.test_i08_api import mutation, setup_source


@pytest.mark.asyncio
async def test_operation_status_survives_restart_without_provider_read(test_client, db_session, test_engine, source_member, monkeypatch):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_status_12345"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    status_path = path.removesuffix("/bindings") + "/operations/"
    calls = []

    async def interrupted(binding, installation):
        calls.append("interrupted")
        raise RuntimeError("simulated process stop")

    monkeypatch.setattr("src.products.source_routes.observe_binding", interrupted)
    with pytest.raises(RuntimeError, match="simulated process stop"):
        await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_status_12345"))
    db_session.expire_all()
    operation = (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "verifyBinding"))).one()
    operation_id = operation.id
    waiting = await test_client.get(status_path + operation_id)
    assert waiting.status_code == 200 and waiting.json()["state"] == "running"
    assert "coverage" not in waiting.json() and calls == ["interrupted"]

    restarted_app = create_app()
    restarted_app.dependency_overrides[get_current_user] = lambda: source_member
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def restored_db():
        async with sessions() as session:
            yield session

    restarted_app.dependency_overrides[get_db] = restored_db
    async with AsyncClient(transport=ASGITransport(app=restarted_app), base_url="http://test") as restarted:
        restored = await restarted.get(status_path + operation_id)
        assert restored.status_code == 200 and restored.json() == waiting.json()
    operation.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    observed_at = datetime.now(UTC)

    async def observed(binding, installation):
        calls.append("observed")
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": observed_at.isoformat(),
                "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
                "source_revision": "status-fixture",
                "native_id": binding.native_id,
                "namespace": binding.namespace,
            },
        }

    monkeypatch.setattr("src.products.source_routes.observe_binding", observed)
    recovered = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_status_12345"))
    assert recovered.status_code == 202 and recovered.json()["operation_id"] == operation_id
    assert recovered.json()["status"] == "succeeded"
    replay = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_status_12345"))
    assert replay.json() == recovered.json() and calls == ["interrupted", "observed"]
    status = (await test_client.get(status_path + operation_id)).json()
    assert status == {
        "id": operation_id,
        "tenant_id": source_member.org_id,
        "product_id": path.split("/")[3],
        "project_id": path.split("/")[5],
        "revision": 2,
        "kind": "verifyBinding",
        "state": "succeeded",
        "coverage": "complete",
    }
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: observed_at + timedelta(hours=1))
    stale = (await test_client.get(status_path + operation_id)).json()
    assert stale["state"] == "expired" and stale["coverage"] == "unavailable"
    assert (await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))).observed["observed_at"] == observed_at.isoformat()
    mapping = await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])
    mapping.ownership_disputed = True
    await db_session.commit()
    restricted = (await test_client.get(status_path + operation_id)).json()
    assert restricted["state"] == "cancelled" and restricted["coverage"] == "denied"
    assert selection["native_id"] not in str(restricted)


@pytest.mark.asyncio
async def test_public_unlink_fences_late_read_and_replays_without_uninstall(test_client, db_session, source_member, monkeypatch):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_route_unlink_123"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    deletion = []

    async def observed_after_unlink(binding, installation):
        response = await test_client.delete(f"{path}/{binding_id}", headers=mutation(2, "unlink_route_12345"))
        deletion.append(response)
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": datetime.now(UTC).isoformat(),
                "stale_after": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "source_revision": "late-completion",
                "native_id": binding.native_id,
                "namespace": binding.namespace,
            },
        }

    monkeypatch.setattr("src.products.source_routes.observe_binding", observed_after_unlink)
    result = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_route_unlink_123"))
    assert result.status_code == 202 and result.json()["status"] == "cancelled"
    assert len(deletion) == 1 and deletion[0].status_code == 200
    assert deletion[0].json()["status"] == "unlinked" and "selection" not in deletion[0].json()
    assert deletion[0].headers["Profile-Revision"] == "3"
    replay = await test_client.delete(f"{path}/{binding_id}", headers=mutation(2, "unlink_route_12345"))
    assert replay.status_code == 200 and replay.json() == deletion[0].json()
    assert replay.headers["Profile-Revision"] == "3"
    assert (await test_client.get(path)).json()["items"] == [deletion[0].json()]
    status_path = path.removesuffix("/bindings") + "/operations/" + result.json()["operation_id"]
    status = (await test_client.get(status_path)).json()
    assert status["state"] == "cancelled" and status["coverage"] == "denied"
    db_session.expire_all()
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))
    assert binding.generation == 2 and binding.observed is None
    assert (await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])) is not None
    product = await db_session.get(Product, (source_member.org_id, path.split("/")[3]))
    assert product.revision == 3
    unlink_operation = (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "unlinkBinding"))).one()
    unlink_status = (await test_client.get(path.removesuffix("/bindings") + "/operations/" + unlink_operation.id)).json()
    assert unlink_status["kind"] == "unlinkBinding" and unlink_status["state"] == "succeeded"
    assert unlink_status["revision"] == 3 and "coverage" not in unlink_status
    assert (await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "retry_after_unlink_123"))).status_code == 404


@pytest.mark.asyncio
async def test_unlink_requires_scope_revision_and_accepts_lost_source_access(test_client, db_session, app, source_member):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_denied_unlink_123"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    url = f"{path}/{binding_id}"
    assert (await test_client.delete(url)).status_code == 428
    assert (await test_client.delete(url, headers=mutation(1, "unlink_stale_12345"))).status_code == 412
    foreign = source_member.model_copy(update={"org_id": "another-tenant", "user_id": "other-user"})
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.delete(url, headers=mutation(2, "unlink_foreign_12345"))).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: source_member
    mapping = await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])
    mapping.ownership_disputed = True
    await db_session.commit()
    removed = await test_client.delete(url, headers=mutation(2, "unlink_revoked_12345"))
    assert removed.status_code == 200 and removed.json()["status"] == "unlinked"
    assert "selection" not in removed.json()
    unlink_operation = (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "unlinkBinding"))).one()
    operation_path = path.removesuffix("/bindings") + "/operations/" + unlink_operation.id
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.get(operation_path)).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: source_member
    conflict = await test_client.delete(f"{path}/different-binding", headers=mutation(2, "unlink_revoked_12345"))
    assert conflict.status_code == 409
    status_path = path.removesuffix("/bindings") + "/operations/" + "no-such-operation"
    assert (await test_client.get(status_path)).status_code == 404
    assert (await test_client.delete(url, headers=mutation(2, "unlink_new_key_12345"))).status_code == 404
