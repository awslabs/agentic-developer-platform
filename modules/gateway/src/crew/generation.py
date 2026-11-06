"""Serialize the one owner-wide generation shared by boxes and pods."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.shared.models.crew_executor import CrewExecutor, CrewOwnerGeneration
from src.shared.models.dev_box import DevBox


async def lock_owner_generation(session, tenant_id: str, user_id: str) -> CrewOwnerGeneration:
    dialect = session.bind.dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError("unsupported owner generation database")
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    await session.execute(
        insert(CrewOwnerGeneration)
        .values(
            tenant_id=tenant_id,
            user_id=user_id,
            generation=0,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "user_id"])
    )
    cursor = await session.scalar(
        select(CrewOwnerGeneration)
        .where(
            CrewOwnerGeneration.tenant_id == tenant_id,
            CrewOwnerGeneration.user_id == user_id,
        )
        .with_for_update()
    )
    latest_pod = await session.scalar(
        select(func.max(CrewExecutor.generation)).where(
            CrewExecutor.tenant_id == tenant_id,
            CrewExecutor.user_id == user_id,
        )
    )
    latest_box = await session.scalar(
        select(func.max(DevBox.machine_generation)).where(
            DevBox.org_id == tenant_id,
            DevBox.user_id == user_id,
        )
    )
    cursor.generation = max(cursor.generation, latest_pod or 0, latest_box or 0)
    return cursor


def advance_owner_generation(cursor: CrewOwnerGeneration) -> int:
    cursor.generation += 1
    return cursor.generation
