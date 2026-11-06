"""Generation checks and dependent publications share one control-DB transaction."""

import asyncio

import pytest
from sqlalchemy import select, text, update

from src.crew.inventory import CrewExecutorInventory
from src.crew.targets import CrewLeaseFencedError, CrewOwner, CrewRunBinding
from src.shared.models.crew_executor import CrewExecutor

pytest_plugins = ["tests.crew.test_targets"]


async def test_owner_and_generation_survive_replacement_without_cross_owner_publication(target):
    resolver, owner, table, factory = target
    inventory = CrewExecutorInventory(factory)
    binding = CrewRunBinding(tenant_id=owner.tenant_id, user_id=owner.user_id, generation=1)
    async with factory() as session:
        original_id = await session.scalar(select(CrewExecutor.id).where(CrewExecutor.tenant_id == owner.tenant_id))
    async with factory.begin() as session:
        await session.execute(text("CREATE TABLE publications (tenant_id TEXT, user_id TEXT, generation INT)"))
        assert (await inventory.lock_current_generation(session, binding, now=100)).target_ref == "pod-a"
        await session.execute(
            text("INSERT INTO publications VALUES (:tenant, :user, :generation)"),
            {"tenant": binding.tenant_id, "user": binding.user_id, "generation": binding.generation},
        )
    assert await resolver.end(owner, generation=1)
    async with factory.begin() as session:
        with pytest.raises(CrewLeaseFencedError):
            await inventory.lock_current_generation(session, binding, now=101)
        await session.execute(update(CrewExecutor).where(CrewExecutor.tenant_id == owner.tenant_id).values(state="deleted"))
    replacement = await resolver.ensure(owner)
    assert replacement.id != original_id and replacement.generation == 3
    table.item["crewLease"].update(target_ref="pod-new", generation=3, expires_at=400)
    await resolver.record_ready(owner, generation=3, pod_uid="pod-new", now=101)
    other_owner = CrewOwner("tenant-b", "user-b")
    other = await resolver.ensure(other_owner)
    table.item.update(tenantId="tenant-b", ownerUserId="user-b")
    table.item["crewLease"].update(tenant_id="tenant-b", user_id="user-b", target_ref="pod-b", generation=1)
    await resolver.record_ready(other_owner, generation=other.generation, pod_uid="pod-b", now=101)
    async with factory.begin() as session:
        with pytest.raises(CrewLeaseFencedError):
            await inventory.lock_current_generation(session, binding, now=102)
        with pytest.raises(CrewLeaseFencedError):
            await inventory.lock_current_generation(session, CrewRunBinding(tenant_id="tenant-b", user_id="user-a", generation=2), now=102)
        assert (
            await inventory.lock_current_generation(session, CrewRunBinding(tenant_id="tenant-b", user_id="user-b", generation=1), now=102)
        ).id == other.id
        assert (
            await inventory.lock_current_generation(session, CrewRunBinding(tenant_id="tenant-a", user_id="user-a", generation=3), now=102)
        ).id == replacement.id
    async with factory() as session:
        assert (await session.execute(text("SELECT * FROM publications"))).all() == [("tenant-a", "user-a", 1)]
        rows = (await session.scalars(select(CrewExecutor).where(CrewExecutor.tenant_id == "tenant-a"))).all()
        assert len(rows) == 2 and {row.generation for row in rows} == {1, 3}


async def test_control_db_fence_waits_for_publication_transaction(target):
    resolver, owner, _, factory = target
    inventory = CrewExecutorInventory(factory)
    binding = CrewRunBinding(tenant_id=owner.tenant_id, user_id=owner.user_id, generation=1)
    async with factory.begin() as session:
        await inventory.lock_current_generation(session, binding, now=100)
        fence = asyncio.create_task(resolver.end(owner, generation=1))
        await asyncio.sleep(0.05)
        assert not fence.done()
    assert await fence is True
    async with factory.begin() as session:
        with pytest.raises(CrewLeaseFencedError):
            await inventory.lock_current_generation(session, binding, now=101)


async def test_expired_or_wrong_target_binding_cannot_publish(target):
    _, owner, _, factory = target
    inventory = CrewExecutorInventory(factory)
    async with factory.begin() as session:
        with pytest.raises(CrewLeaseFencedError):
            await inventory.lock_current_generation(session, CrewRunBinding(tenant_id=owner.tenant_id, user_id=owner.user_id, generation=1), now=200)
        with pytest.raises(CrewLeaseFencedError):
            await inventory.lock_current_generation(
                session,
                CrewRunBinding(tenant_id=owner.tenant_id, user_id=owner.user_id, generation=1, target_kind="ec2-instance"),
                now=100,
            )
