"""Transactional, inert registration of an owner-scoped executor generation."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.crew.generation import advance_owner_generation, lock_owner_generation
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox

if TYPE_CHECKING:
    from src.crew.targets import CrewRunBinding


class CrewLeaseFencedError(Exception):
    """No currently authorized endpoint or publication generation exists."""


async def list_crew_executors(session, *, tenant_id: str, limit: int = 100, offset: int = 0) -> list[CrewExecutor]:
    if not tenant_id:
        raise ValueError("tenant is required")
    result = await session.scalars(
        select(CrewExecutor)
        .where(CrewExecutor.tenant_id == tenant_id)
        .order_by(CrewExecutor.created_at.desc(), CrewExecutor.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result)


class CrewExecutorInventory:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def lock_current_generation(self, session, binding: CrewRunBinding, *, now: int) -> CrewExecutor:
        """Fence a same-DB publication; write its pointer before committing this session.

        Replacement/end must update this row in the control DB first. The
        conditional UPDATE locks the owner row until the caller's transaction
        commits, so a fence cannot interleave between this check and publication.
        """
        predicate = (
            CrewExecutor.tenant_id == binding.tenant_id,
            CrewExecutor.user_id == binding.user_id,
            CrewExecutor.target_kind == binding.target_kind,
            CrewExecutor.state == "ready",
            CrewExecutor.generation == binding.generation,
            CrewExecutor.lease_generation == binding.generation,
            CrewExecutor.lease_expires_at > now,
        )
        result = await session.execute(update(CrewExecutor).where(*predicate).values(lease_generation=CrewExecutor.lease_generation))
        if result.rowcount != 1:
            raise CrewLeaseFencedError("lease_fenced")
        return await session.scalar(select(CrewExecutor).where(*predicate))

    @staticmethod
    async def bind_host(session, box, *, retire_pod=None) -> CrewExecutor:
        if (
            not box.id
            or not box.org_id
            or not box.user_id
            or not box.deployment_id
            or box.profile != "crew"
            or box.status != "running"
            or box.desired_state != "running"
            or box.observed_state != "running"
            or type(box.machine_generation) is not int
            or box.machine_generation < 1
        ):
            raise CrewLeaseFencedError("host box unavailable")
        if session.bind.dialect.name == "postgresql":
            owner_key = f"{len(box.org_id)}:{box.org_id}{box.user_id}".encode()
            lock_id = int.from_bytes(hashlib.sha256(owner_key).digest()[:8], "big", signed=True)
            await session.execute(select(func.pg_advisory_xact_lock(lock_id)))
        rows = (
            await session.scalars(
                select(CrewExecutor)
                .where(
                    CrewExecutor.tenant_id == box.org_id,
                    CrewExecutor.user_id == box.user_id,
                )
                .order_by(CrewExecutor.generation.desc(), CrewExecutor.id)
                .with_for_update()
            )
        ).all()
        active = next((row for row in rows if row.state != "deleted"), None)
        if active is not None and active.target_kind == "eks-pod" and active.generation < box.machine_generation:
            if (
                retire_pod is None
                or active.lease_generation not in (None, active.generation)
                or (active.state == "ready" and active.lease_generation != active.generation)
            ):
                raise CrewLeaseFencedError("pod handoff requires current owner authority")
            await retire_pod(active)
            active.state = "deleted"
            active.target_ref = None
            active.lease_generation = None
            active.lease_expires_at = None
            await session.flush()
            active = None
        if active is not None and active.target_kind == "ec2-instance" and active.dev_box_id == box.id and active.generation < box.machine_generation:
            # The box was replaced: the machine this executor bound is gone, so it can never resume
            # (dev 2026-10-18: a provisioning executor from the retired generation refused every
            # approval). Retire it here, as the pod handoff does, unless it still holds an unexpired
            # lease, which only the controller's fence may end.
            now = int(time.time())
            if active.lease_generation is not None and (active.lease_expires_at is None or active.lease_expires_at > now):
                raise CrewLeaseFencedError("host executor conflicts with canonical box")
            active.state = "deleted"
            active.target_ref = None
            active.lease_generation = None
            active.lease_expires_at = None
            await session.flush()
            active = None
        if active is not None and active.target_kind == "ec2-instance" and active.dev_box_id != box.id:
            # The executor belongs to another box of this owner. When that box is released (or no
            # longer exists) nothing can resume it; the controller retires it at release_verified,
            # but a row released before that leg existed, or one whose retirement raced this
            # enrollment, must not fence the owner's new box forever (dev 2026-10-20: a `stopped`
            # executor of the released box answered every enrollment 409, #8223 cause 23). An
            # unexpired lease still fences: only the controller's fence may end it.
            owner_box = await session.get(DevBox, active.dev_box_id) if active.dev_box_id else None
            if owner_box is None or owner_box.status == "released" or owner_box.desired_state == "released":
                now = int(time.time())
                if active.lease_generation is not None and (active.lease_expires_at is None or active.lease_expires_at > now):
                    raise CrewLeaseFencedError("host executor conflicts with canonical box")
                active.state = "deleted"
                active.target_ref = None
                active.lease_generation = None
                active.lease_expires_at = None
                await session.flush()
                active = None
        if active is not None:
            if (
                active.target_kind != "ec2-instance"
                or active.dev_box_id != box.id
                or active.generation != box.machine_generation
                or active.state == "ending"
            ):
                raise CrewLeaseFencedError("host executor conflicts with canonical box")
            return active
        previous = rows[0] if rows else None
        if previous is not None and box.machine_generation <= previous.generation:
            raise CrewLeaseFencedError("host and executor generations do not match")
        executor = CrewExecutor(
            tenant_id=box.org_id,
            user_id=box.user_id,
            target_kind="ec2-instance",
            dev_box_id=box.id,
            generation=box.machine_generation,
            state="provisioning",
            owner_key=secrets.token_hex(26),
            latest_verified_checkpoint_id=previous.latest_verified_checkpoint_id if previous else None,
        )
        session.add(executor)
        return executor

    async def ensure(self, *, tenant_id: str, user_id: str) -> CrewExecutor:
        if not tenant_id or not user_id:
            raise ValueError("tenant and user are required")
        async with self.session_factory() as session:
            async with session.begin():
                active = (CrewExecutor.tenant_id == tenant_id, CrewExecutor.user_id == user_id, CrewExecutor.state != "deleted")
                dialect = session.bind.dialect.name
                if dialect == "postgresql":
                    owner_key = f"{len(tenant_id)}:{tenant_id}{user_id}".encode()
                    lock_id = int.from_bytes(hashlib.sha256(owner_key).digest()[:8], "big", signed=True)
                    await session.execute(select(func.pg_advisory_xact_lock(lock_id)))
                cursor = await lock_owner_generation(session, tenant_id, user_id)
                row = await session.scalar(select(CrewExecutor).where(*active))
                if row is not None:
                    return row
                generation = advance_owner_generation(cursor)
                previous_checkpoint_id = await session.scalar(
                    select(CrewExecutor.latest_verified_checkpoint_id)
                    .where(CrewExecutor.tenant_id == tenant_id, CrewExecutor.user_id == user_id)
                    .order_by(CrewExecutor.generation.desc())
                    .limit(1)
                )
                insert = pg_insert if dialect == "postgresql" else sqlite_insert
                statement = (
                    insert(CrewExecutor)
                    .values(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        target_kind="eks-pod",
                        state="provisioning",
                        generation=generation,
                        owner_key=secrets.token_hex(26),
                        latest_verified_checkpoint_id=previous_checkpoint_id,
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id", "user_id"], index_where=text("state <> 'deleted'"))
                )
                await session.execute(statement)
                row = await session.scalar(select(CrewExecutor).where(*active))
                if row is None:
                    raise RuntimeError("executor registration did not produce an active owner row")
                return row
