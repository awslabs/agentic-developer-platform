"""Read-only verification recovers a durable attempt without duplicating operations."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.models import ProductOperation, ProductSourceBinding
from tests.products.test_i08_api import mutation, setup_source


async def create_selection(test_client, db_session, source_member):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_recoverable_123"))
    assert created.status_code == 201, created.text
    return path, created.json()["id"]


@pytest.mark.asyncio
async def test_expired_lease_recovers_pending_verification_after_restart(test_client, db_session, test_engine, source_member, monkeypatch):
    path, binding_id = await create_selection(test_client, db_session, source_member)
    calls = []

    async def interrupted(binding, installation):
        calls.append((binding.id, installation.id))
        raise RuntimeError("simulated worker interruption")

    monkeypatch.setattr("src.products.source_routes.observe_binding", interrupted)
    with pytest.raises(RuntimeError, match="simulated worker interruption"):
        await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_recoverable_123"))
    db_session.expire_all()
    operation = (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "verifyBinding"))).one()
    operation_id = operation.id
    assert operation.state == "running" and operation.lease_owner and operation.attempt_count == 1
    assert operation.binding_id == binding_id and operation.binding_generation == 1
    assert (await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))).observed is None

    still_running = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_recoverable_123"))
    assert still_running.status_code == 202 and still_running.json()["status"] == "running"
    assert len(calls) == 1
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as resumed:
        saved = await resumed.get(ProductOperation, (source_member.org_id, operation_id))
        saved.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await resumed.commit()

    async def observed(binding, installation):
        calls.append((binding.id, installation.id))
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": datetime.now(UTC).isoformat(),
                "stale_after": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "source_revision": "observed-after-restart",
                "native_id": binding.native_id,
                "namespace": binding.namespace,
            },
        }

    monkeypatch.setattr("src.products.source_routes.observe_binding", observed)
    recovered = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_recoverable_123"))
    assert recovered.status_code == 202, recovered.text
    assert recovered.json() == {"operation_id": operation_id, "status": "succeeded", "coverage": "complete"}
    assert len(calls) == 2
    db_session.expire_all()
    saved = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert saved.attempt_count == 2 and saved.lease_owner is None and saved.lease_until is None
    assert (await db_session.get(ProductSourceBinding, (source_member.org_id, binding_id))).observed["source_revision"] == "observed-after-restart"
    assert len((await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "verifyBinding"))).all()) == 1


@pytest.mark.asyncio
async def test_failure_retry_is_bounded_and_reuses_same_operation(test_client, db_session, source_member, monkeypatch):
    path, binding_id = await create_selection(test_client, db_session, source_member)
    calls = []

    async def unavailable(binding, installation):
        calls.append(binding.id)
        return {"coverage": "unavailable", "status": "unverified", "reason": "provider_read_unavailable"}

    monkeypatch.setattr("src.products.source_routes.observe_binding", unavailable)
    failed = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_retry_bounded_123"))
    assert failed.status_code == 202 and failed.json()["status"] == "failed"
    throttled = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_retry_bounded_123"))
    assert throttled.json() == failed.json() and len(calls) == 1
    db_session.expire_all()
    operation = (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "verifyBinding"))).one()
    operation.retry_at = datetime.now(UTC) - timedelta(seconds=1)
    operation.attempt_count = 9
    operation_id = operation.id
    await db_session.commit()
    final_try = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_retry_bounded_123"))
    assert final_try.json()["operation_id"] == failed.json()["operation_id"]
    db_session.expire_all()
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    assert operation.attempt_count == 10 and len(calls) == 2
    operation.retry_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    exhausted = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_retry_bounded_123"))
    assert exhausted.json() == final_try.json() and len(calls) == 2


@pytest.mark.asyncio
async def test_expired_attempt_cannot_publish_late_observation(test_client, db_session, source_member, monkeypatch):
    path, binding_id = await create_selection(test_client, db_session, source_member)

    async def observation_after_expiry(binding, installation):
        operation = (await db_session.scalars(select(ProductOperation).where(ProductOperation.operation == "verifyBinding"))).one()
        operation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": datetime.now(UTC).isoformat(),
                "stale_after": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "source_revision": "too-late",
                "native_id": binding.native_id,
                "namespace": binding.namespace,
            },
        }

    monkeypatch.setattr("src.products.source_routes.observe_binding", observation_after_expiry)
    result = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_expired_12345"))
    assert result.status_code == 202 and result.json()["status"] == "cancelled"
    db_session.expire_all()
