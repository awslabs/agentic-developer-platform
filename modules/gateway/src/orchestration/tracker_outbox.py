"""Durable tracker publication intent and verified post-commit recovery."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import OrchestrationTrackerPublication
from .tracker_projection import PendingTrackerProjection, TrackerProjectionReport, resolve_parent_reference


def _publication_pending(row: OrchestrationTrackerPublication) -> PendingTrackerProjection:
    return PendingTrackerProjection(
        org_id=row.org_id,
        flow_id=row.flow_id,
        repo=row.repo,
        issue_number=row.issue_number,
        installation_id=row.installation_id,
        body_region=row.body_region,
        version=row.version,
        watermark=row.watermark,
        repository_id=row.repository_id,
        epic_ref=row.epic_ref,
        operation_id=row.id,
    )


async def stage_tracker_publications(session: AsyncSession, report: TrackerProjectionReport, *, limit: int = 20) -> None:
    if not report.enabled:
        return
    expired = (
        select(OrchestrationTrackerPublication.id)
        .where(
            OrchestrationTrackerPublication.completed_at < datetime.now(UTC) - timedelta(days=30),
        )
        .order_by(OrchestrationTrackerPublication.completed_at)
        .limit(limit)
    )
    await session.execute(delete(OrchestrationTrackerPublication).where(OrchestrationTrackerPublication.id.in_(expired)))
    recovery_limit = limit if not report.pending else max(1, limit // 2)
    owed = (
        await session.scalars(
            select(OrchestrationTrackerPublication)
            .where(
                OrchestrationTrackerPublication.completed_at.is_(None),
            )
            .order_by(
                OrchestrationTrackerPublication.last_attempted_at.is_not(None),
                OrchestrationTrackerPublication.last_attempted_at,
                OrchestrationTrackerPublication.created_at,
                OrchestrationTrackerPublication.id,
            )
            .limit(recovery_limit)
        )
    ).all()
    pending = [_publication_pending(row) for row in owed]
    present = {item.operation_id for item in pending}
    for candidate in report.pending:
        if len(pending) >= limit:
            report.capped = True
            break
        if candidate.repository_id is None or candidate.epic_ref is None:
            report.record(candidate.org_id, "projections_refused")
            continue
        outstanding = await session.scalar(
            select(OrchestrationTrackerPublication.id)
            .where(
                OrchestrationTrackerPublication.org_id == candidate.org_id,
                OrchestrationTrackerPublication.flow_id == candidate.flow_id,
                OrchestrationTrackerPublication.repository_id == candidate.repository_id,
                OrchestrationTrackerPublication.installation_id == candidate.installation_id,
                OrchestrationTrackerPublication.issue_number == candidate.issue_number,
                OrchestrationTrackerPublication.completed_at.is_(None),
            )
            .limit(1)
        )
        if outstanding is not None:
            continue
        identity = json.dumps(
            (
                candidate.org_id,
                candidate.flow_id,
                candidate.epic_ref,
                str(candidate.repository_id),
                candidate.repo,
                str(candidate.installation_id),
                str(candidate.issue_number),
                str(candidate.version),
                str(candidate.watermark),
                candidate.body_region,
            ),
            separators=(",", ":"),
        )
        operation_id = sha256(identity.encode("utf-8")).hexdigest()
        if operation_id in present:
            continue
        stored = await session.get(OrchestrationTrackerPublication, operation_id)
        if stored is not None:
            if stored.completed_at is None:
                pending.append(_publication_pending(stored))
            continue
        item = replace(candidate, operation_id=operation_id)
        dialect = session.get_bind().dialect.name
        insert = postgres_insert if dialect == "postgresql" else sqlite_insert
        await session.execute(
            insert(OrchestrationTrackerPublication)
            .values(
                id=operation_id,
                org_id=item.org_id,
                flow_id=item.flow_id,
                epic_ref=item.epic_ref,
                repository_id=item.repository_id,
                repo=item.repo,
                installation_id=item.installation_id,
                issue_number=item.issue_number,
                body_region=item.body_region,
                version=item.version,
                watermark=item.watermark,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        pending.append(item)
        present.add(operation_id)
    report.pending = pending


async def pending_authorized(pending: PendingTrackerProjection, session_factory: async_sessionmaker[AsyncSession]) -> bool:
    if pending.operation_id is None:
        return True
    if pending.repository_id is None or pending.epic_ref is None:
        return False
    async with session_factory() as session:
        row = await session.get(OrchestrationTrackerPublication, pending.operation_id)
        if row is None or row.org_id != pending.org_id or row.completed_at is not None or _publication_pending(row) != pending:
            return False
        target = await resolve_parent_reference(
            session, org_id=pending.org_id, flow_id=pending.flow_id, epic_ref=pending.epic_ref, operation="tracker_write"
        )
        return target is not None and (
            target.repository_id,
            target.full_name,
            target.installation_id,
            target.issue_number,
        ) == (pending.repository_id, pending.repo, pending.installation_id, pending.issue_number)


async def record_publication_attempt(pending: PendingTrackerProjection, session_factory: async_sessionmaker[AsyncSession]) -> None:
    if pending.operation_id is None:
        return
    async with session_factory() as session:
        await session.execute(
            update(OrchestrationTrackerPublication)
            .where(
                OrchestrationTrackerPublication.id == pending.operation_id,
                OrchestrationTrackerPublication.org_id == pending.org_id,
                OrchestrationTrackerPublication.completed_at.is_(None),
            )
            .values(last_attempted_at=datetime.now(UTC))
        )
        await session.commit()


async def complete_publication(pending: PendingTrackerProjection, session_factory: async_sessionmaker[AsyncSession]) -> None:
    if pending.operation_id is None:
        return
    async with session_factory() as session:
        await session.execute(
            update(OrchestrationTrackerPublication)
            .where(
                OrchestrationTrackerPublication.id == pending.operation_id,
                OrchestrationTrackerPublication.org_id == pending.org_id,
                OrchestrationTrackerPublication.completed_at.is_(None),
            )
            .values(completed_at=datetime.now(UTC))
        )
        await session.commit()
