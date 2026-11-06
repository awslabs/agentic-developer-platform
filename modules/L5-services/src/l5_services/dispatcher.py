"""Post-commit dispatch of SQL-owned work using an opaque queue envelope."""

import logging
from asyncio import wait_for
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import uuid4

from sqlalchemy import and_, case, func, literal, or_, select, update

from l5_services.models.work import Work


MAX_DISPATCH_ATTEMPTS = 10
LOGGER = logging.getLogger(__name__)


def dispatch_limit(work_model):
    """Only explicit knowledge continuations may supplement dispatch attempts."""
    from l5_services.models.setup import SetupIntent, SetupWork

    if work_model is not SetupWork:
        return literal(MAX_DISPATCH_ATTEMPTS)
    knowledge = select(SetupIntent.tenant_id).where(
        SetupIntent.tenant_id == SetupWork.tenant_id, SetupIntent.operation_id == SetupWork.operation_id,
        SetupIntent.service_id == "devops", SetupIntent.request["action"].as_string() == "sync_knowledge",
    ).exists()
    return case((knowledge, func.coalesce(SetupWork.dispatch_attempt_limit, MAX_DISPATCH_ATTEMPTS)),
                else_=MAX_DISPATCH_ATTEMPTS)


def _eligible(now, work_model=Work):
    return and_(
        work_model.completed_at.is_(None), work_model.blocked_at.is_(None),
        work_model.due_at <= now + timedelta(seconds=900),
        or_(work_model.lease_until.is_(None), work_model.lease_until <= now),
        or_(work_model.dispatch_until.is_(None), work_model.dispatch_until <= now),
        or_(work_model.last_enqueued_at.is_(None), work_model.redeliver_after <= now),
    )


def service_work_condition(work_model, service_id):
    """Resolve ownership from durable SQL, never from an untrusted envelope."""
    if service_id is None:
        return True  # Backward-compatible shared runtime/test callers.
    from l5_services.models.run import Run
    from l5_services.models.setup import SetupIntent, SetupWork

    parent, key = (SetupIntent, "operation_id") if work_model is SetupWork else (Run, "run_id")
    return select(parent.tenant_id).where(
        parent.tenant_id == work_model.tenant_id,
        getattr(parent, key) == getattr(work_model, key),
        parent.service_id == service_id,
    ).exists()


async def dispatch_work(sessions, tenant_id, work_id, publish, *, now=None, redelivery_seconds=900,
                        work_model=Work, service_id=None):
    """Return whether a message was accepted; transport failures leave work owed."""
    now = now or datetime.now(UTC)
    claim = str(uuid4())
    async with sessions() as session:
        work = await session.get(work_model, (tenant_id, work_id), with_for_update=True)
        if work is None:
            return False
        if service_id is not None and await session.scalar(select(work_model.work_id).where(
            work_model.tenant_id == tenant_id, work_model.work_id == work_id,
            service_work_condition(work_model, service_id),
        )) is None:
            return False
        limit = await session.scalar(select(dispatch_limit(work_model)).where(
            work_model.tenant_id == tenant_id, work_model.work_id == work_id))
        if work.attempts >= limit:
            await session.execute(update(work_model).where(
                work_model.tenant_id == tenant_id, work_model.work_id == work_id,
                _eligible(now, work_model),
            ).values(blocked_at=now, last_error="dispatch_attempt_limit"),
                execution_options={"synchronize_session": False})
            await session.commit()
            return False
        applied = await session.execute(
            update(work_model).where(
                work_model.tenant_id == tenant_id, work_model.work_id == work_id, _eligible(now, work_model),
                work_model.attempts < dispatch_limit(work_model),
            ).values(dispatch_owner=claim, dispatch_until=now + timedelta(seconds=30),
                     attempts=work_model.attempts + 1),
            execution_options={"synchronize_session": False},
        )
        if applied.rowcount != 1:
            await session.rollback()
            return False
        due_at, generation = work.due_at, work.generation
        await session.commit()

    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    delay = min(900, max(0, ceil((due_at - now).total_seconds())))
    delivered = False
    try:
        envelope = {"schema_version": "1.0", "work_id": work_id, "generation": generation}
        await wait_for(publish(envelope, delay), timeout=5)
        delivered = True
    except Exception:
        pass

    async with sessions() as session:
        values = {"dispatch_owner": None, "dispatch_until": None}
        if delivered:
            values.update(
                last_enqueued_at=now, redeliver_after=max(now, due_at) + timedelta(seconds=redelivery_seconds),
                last_error=None,
            )
        else:
            values["last_error"] = "publication_failed"
        await session.execute(update(work_model).where(
            work_model.tenant_id == tenant_id, work_model.work_id == work_id, work_model.dispatch_owner == claim,
        ).values(**values), execution_options={"synchronize_session": False})
        await session.commit()
    return delivered


async def repair_owed_work(sessions, publish, *, now=None, limit=50, work_model=Work, service_id=None):
    """Select fairly across tenants; one failed row cannot stop other sends."""
    now = now or datetime.now(UTC)
    if not 1 <= limit <= 50:
        raise ValueError("repair limit must be between 1 and 50")
    scope = service_work_condition(work_model, service_id)
    async with sessions() as session:
        await session.execute(update(work_model).where(
            scope,
            _eligible(now, work_model), work_model.attempts >= dispatch_limit(work_model),
        ).values(blocked_at=now, last_error="dispatch_attempt_limit"),
            execution_options={"synchronize_session": False})
        await session.commit()
        ranked = select(
            work_model.tenant_id.label("tenant_id"), work_model.work_id.label("work_id"),
            work_model.due_at.label("due_at"),
            func.row_number().over(partition_by=work_model.tenant_id,
                                   order_by=(work_model.due_at, work_model.work_id)).label("rank"),
        ).where(_eligible(now, work_model), scope).subquery()
        rows = (await session.execute(select(ranked.c.tenant_id, ranked.c.work_id).order_by(
            ranked.c.rank, ranked.c.due_at, ranked.c.work_id,
        ).limit(limit))).all()
    sent = 0
    for tenant_id, work_id in rows:
        try:
            if await dispatch_work(sessions, tenant_id, work_id, publish, now=now,
                                   work_model=work_model, service_id=service_id):
                sent += 1
        except Exception:
            LOGGER.exception("Failed to repair work publication")
    return sent
