"""Persistent, tenant-scoped transition window for legacy GitHub PAT delivery."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.audit import AuditLog
from src.shared.models.organization import Organization
from src.shared.models.vault import UserCredential

_START_KEY = "github_pat_grace_started_at"


async def github_pat_retired(session: AsyncSession, credential: UserCredential, *, grace_days: int, now: datetime | None = None) -> bool:
    if not (credential.service == "github" and credential.label == "github-pat" and credential.credential_type == "bearer"):
        return False

    moment = now or datetime.now(UTC)
    organization = await session.scalar(
        select(Organization).where(Organization.id == credential.org_id).with_for_update().execution_options(populate_existing=True)
    )
    if organization is None:
        return True
    started = (organization.settings or {}).get(_START_KEY)
    if started is None:
        started = moment.isoformat()
        organization.settings = {**(organization.settings or {}), _START_KEY: started}
        session.add(
            AuditLog(
                org_id=credential.org_id,
                actor_id=credential.user_id,
                event_type="github_pat_grace_started",
                details={"grace_days": grace_days},
            )
        )
        await session.commit()
    retired = moment >= datetime.fromisoformat(started).astimezone(UTC) + timedelta(days=grace_days)
    if retired:
        session.add(
            AuditLog(
                org_id=credential.org_id,
                actor_id=credential.user_id,
                event_type="github_pat_delivery_denied",
                details={"reason": "migration_required", "credential_id": credential.id},
            )
        )
        await session.commit()
    return retired
