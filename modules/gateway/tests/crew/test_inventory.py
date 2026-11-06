"""Registration fixtures use independent database connections for concurrent callers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.crew.inventory import CrewExecutorInventory, CrewLeaseFencedError
from src.shared.models.crew_executor import CrewExecutor, CrewOwnerGeneration
from src.shared.models.dev_box import DevBox


@pytest.fixture
async def inventory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crew.sqlite'}", connect_args={"timeout": 10})
    async with engine.begin() as connection:
        await connection.run_sync(CrewExecutor.__table__.create)
        await connection.run_sync(CrewOwnerGeneration.__table__.create)
        await connection.run_sync(DevBox.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield CrewExecutorInventory(factory), factory
    finally:
        await engine.dispose()


async def test_concurrent_ensure_has_one_live_owner_and_generation(inventory):
    repository, factory = inventory
    rows = await asyncio.gather(*(repository.ensure(tenant_id="tenant-a", user_id="user-a") for _ in range(8)))
    assert {row.id for row in rows} == {rows[0].id}
    assert {row.generation for row in rows} == {1}
    assert len(rows[0].owner_key) == 52
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CrewExecutor)) == 1


async def test_partial_index_rejects_second_live_row_but_separates_tenants(inventory):
    repository, factory = inventory
    first, other_tenant = await asyncio.gather(
        repository.ensure(tenant_id="tenant-a", user_id="shared-user"),
        repository.ensure(tenant_id="tenant-b", user_id="shared-user"),
    )
    assert first.id != other_tenant.id
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).where(CrewExecutor.id == first.id).values(state="deleted"))
    replacement = await repository.ensure(tenant_id="tenant-a", user_id="shared-user")
    assert replacement.id != first.id and replacement.generation == first.generation + 1
    assert replacement.owner_key != first.owner_key
    assert other_tenant.owner_key != first.owner_key
    assert (await repository.ensure(tenant_id="tenant-b", user_id="shared-user")).id == other_tenant.id


async def test_replacement_generation_inherits_only_its_owners_selected_checkpoint(inventory):
    repository, factory = inventory
    first = await repository.ensure(tenant_id="tenant-a", user_id="shared-user")
    other = await repository.ensure(tenant_id="tenant-b", user_id="shared-user")
    async with factory.begin() as session:
        await session.execute(
            update(CrewExecutor).where(CrewExecutor.id == first.id).values(state="deleted", latest_verified_checkpoint_id="checkpoint-v1")
        )
        await session.execute(update(CrewExecutor).where(CrewExecutor.id == other.id).values(latest_verified_checkpoint_id="foreign-checkpoint"))
    replacement = await repository.ensure(tenant_id="tenant-a", user_id="shared-user")
    assert replacement.generation == first.generation + 1
    assert replacement.latest_verified_checkpoint_id == "checkpoint-v1"
    assert replacement.owner_key != first.owner_key
    async with factory.begin() as session:
        await session.execute(update(CrewExecutor).where(CrewExecutor.id == replacement.id).values(state="deleted"))
    third = await repository.ensure(tenant_id="tenant-a", user_id="shared-user")
    assert third.generation == first.generation + 2
    assert third.latest_verified_checkpoint_id == "checkpoint-v1"
    assert (await repository.ensure(tenant_id="tenant-b", user_id="shared-user")).id == other.id


async def test_registration_requires_both_owner_parts(inventory):
    repository, _ = inventory
    with pytest.raises(ValueError, match="tenant and user"):
        await repository.ensure(tenant_id="", user_id="user-a")


async def test_postgres_owner_lock_uses_a_bindable_integer_not_text():
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.begin = MagicMock()
    session.begin.return_value.__aenter__.return_value = session
    session.bind.dialect.name = "postgresql"
    executor = CrewExecutor(id="example", tenant_id="tenant-a", user_id="user-a", generation=1)
    cursor = CrewOwnerGeneration(tenant_id="tenant-a", user_id="user-a", generation=0)
    session.scalar.side_effect = [cursor, None, None, None, None, executor]
    factory = MagicMock(return_value=session)
    assert await CrewExecutorInventory(factory).ensure(tenant_id="tenant-a", user_id="user-a") is executor
    lock_statement = session.execute.call_args_list[0].args[0]
    values = lock_statement.compile(dialect=postgresql.dialect()).params.values()
    assert len(tuple(values)) == 1
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in values)
    cursor_statement = session.execute.call_args_list[1].args[0]
    assert "ON CONFLICT (tenant_id, user_id) DO NOTHING" in str(cursor_statement.compile(dialect=postgresql.dialect()))
    insert_statement = session.execute.call_args_list[2].args[0]
    compiled = insert_statement.compile(dialect=postgresql.dialect())
    assert "ON CONFLICT (tenant_id, user_id) WHERE state <> 'deleted' DO NOTHING" in str(compiled)
    assert "state_1" not in compiled.params


def _box(box_id, *, generation, status="running", desired_state="running", observed_state="running", user_id="shared-user"):
    return DevBox(
        id=box_id,
        user_id=user_id,
        org_id="tenant-a",
        deployment_id="dep",
        template_id="template",
        template_revision="r1",
        profile="crew",
        machine_generation=generation,
        status=status,
        desired_state=desired_state,
        observed_state=observed_state,
    )


async def _seed_released_box_executor(factory, *, state="stopped", lease_generation=None, lease_expires_at=None, checkpoint="checkpoint-v17"):
    async with factory.begin() as session:
        session.add(_box("released-box", generation=17, status="released", desired_state="released", observed_state="released"))
        session.add(_box("new-box", generation=20))
        session.add(
            CrewExecutor(
                id="stale",
                tenant_id="tenant-a",
                user_id="shared-user",
                target_kind="ec2-instance",
                dev_box_id="released-box",
                generation=17,
                state=state,
                owner_key="k" * 52,
                target_ref="released-box",
                lease_generation=lease_generation,
                lease_expires_at=lease_expires_at,
                latest_verified_checkpoint_id=checkpoint,
            )
        )


async def test_bind_host_retires_a_released_boxs_executor_and_inherits_its_checkpoint(inventory):
    """#8223 cause 23: the released box's `stopped` executor answered every enrollment of the owner's next box 409."""
    repository, factory = inventory
    await _seed_released_box_executor(factory)
    async with factory() as session:
        new_box = await session.get(DevBox, "new-box")
        executor = await CrewExecutorInventory.bind_host(session, new_box)
        await session.commit()
        assert executor.dev_box_id == "new-box" and executor.generation == 20 and executor.state == "provisioning"
        assert executor.latest_verified_checkpoint_id == "checkpoint-v17"
        stale = await session.get(CrewExecutor, "stale")
        assert stale.state == "deleted" and stale.target_ref is None and stale.lease_generation is None
        assert stale.latest_verified_checkpoint_id == "checkpoint-v17"


async def test_bind_host_keeps_fencing_a_released_boxs_executor_with_an_unexpired_lease(inventory):
    repository, factory = inventory
    await _seed_released_box_executor(factory, state="ready", lease_generation=17, lease_expires_at=4_000_000_000)
    async with factory() as session:
        new_box = await session.get(DevBox, "new-box")
        with pytest.raises(CrewLeaseFencedError, match="conflicts with canonical box"):
            await CrewExecutorInventory.bind_host(session, new_box)
        await session.rollback()
        assert (await session.get(CrewExecutor, "stale")).state == "ready"


async def test_bind_host_still_fences_another_live_box_of_the_owner(inventory):
    repository, factory = inventory
    await _seed_released_box_executor(factory)
    async with factory.begin() as session:
        # One live box per owner and deployment is enforced by the partial unique index, so the
        # owner's other live box has to sit in another deployment.
        other = await session.get(DevBox, "released-box")
        other.status, other.desired_state, other.observed_state = "stopped", "stopped", "stopped"
        other.deployment_id = "dep-2"
    async with factory() as session:
        new_box = await session.get(DevBox, "new-box")
        with pytest.raises(CrewLeaseFencedError, match="conflicts with canonical box"):
            await CrewExecutorInventory.bind_host(session, new_box)
        await session.rollback()
        assert (await session.get(CrewExecutor, "stale")).state == "stopped"
