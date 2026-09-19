"""Durable operator alerting when saved mappings point at retired models.

The database claim is committed before SNS publish. A delivered row is terminal;
an expired failed claim may be leased again with a new token. This is an
operator-only push channel. Owners continue to discover lifecycle state through
the existing list/catalogue/explain UI and CLI surfaces.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.orchestration.notify import Notification, notify
from src.shared.models.base import new_uuid
from src.shared.models.persona_models import PersonaModelPreference, PersonaModelRetirementAlert

from . import catalogue

DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 10


@dataclass(frozen=True)
class RetirementCandidate:
    preference_id: str
    org_id: str
    persona_key: str
    owner_kind: str
    owner_id: str
    model_id: str
    lifecycle_revision: str


@dataclass(frozen=True)
class ClaimedRetirement:
    candidate: RetirementCandidate
    claim_token: str
    retry: bool


@dataclass
class RetirementAlertReport:
    mappings_examined: int = 0
    claims_acquired: int = 0
    retries_acquired: int = 0
    delivered: int = 0
    notifications_failed: int = 0
    lost_claims: int = 0
    errors: int = 0

    @property
    def success(self) -> bool:
        return self.notifications_failed == 0 and self.errors == 0


def _retired_models() -> dict[str, str]:
    return {model.canonical_model_id: model.lifecycle_revision for model in catalogue.PLATFORM_MODEL_CATALOGUE if model.lifecycle == "retired"}


async def _candidates(session: AsyncSession) -> list[RetirementCandidate]:
    retired = _retired_models()
    if not retired:
        return []
    rows = (
        await session.scalars(
            select(PersonaModelPreference).where(
                PersonaModelPreference.canonical_model_id.in_(tuple(retired)),
            )
        )
    ).all()
    return [
        RetirementCandidate(
            preference_id=row.id,
            org_id=row.org_id,
            persona_key=row.persona_key,
            owner_kind=row.principal_kind,
            owner_id=row.principal_id,
            model_id=row.canonical_model_id,
            lifecycle_revision=retired[row.canonical_model_id],
        )
        for row in rows
    ]


def _claim_insert(session: AsyncSession):
    table = PersonaModelRetirementAlert.__table__
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - production and test dialects are explicit
        raise RuntimeError(f"retirement alert claims do not support database dialect {dialect!r}")
    return insert(table)


async def claim_retirement(
    session_factory: async_sessionmaker[AsyncSession],
    candidate: RetirementCandidate,
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ClaimedRetirement | None:
    """Atomically acquire one durable claim and commit it before returning."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    token = new_uuid()
    lease = current + timedelta(seconds=lease_seconds)
    async with session_factory() as session:
        statement = (
            _claim_insert(session)
            .values(
                id=new_uuid(),
                org_id=candidate.org_id,
                preference_id=candidate.preference_id,
                persona_key=candidate.persona_key,
                preference_owner_kind=candidate.owner_kind,
                preference_owner_id=candidate.owner_id,
                canonical_model_id=candidate.model_id,
                lifecycle_revision=candidate.lifecycle_revision,
                state="claimed",
                claim_token=token,
                lease_expires_at=lease,
                attempt_count=1,
                last_error=None,
                claimed_at=current,
                delivered_at=None,
                created_at=current,
                updated_at=current,
            )
            .on_conflict_do_nothing(index_elements=["preference_id", "canonical_model_id", "lifecycle_revision"])
        )
        inserted = await session.execute(statement)
        if inserted.rowcount == 1:
            await session.commit()
            return ClaimedRetirement(candidate=candidate, claim_token=token, retry=False)

        retried = await session.execute(
            update(PersonaModelRetirementAlert)
            .where(
                PersonaModelRetirementAlert.preference_id == candidate.preference_id,
                PersonaModelRetirementAlert.canonical_model_id == candidate.model_id,
                PersonaModelRetirementAlert.lifecycle_revision == candidate.lifecycle_revision,
                PersonaModelRetirementAlert.org_id == candidate.org_id,
                PersonaModelRetirementAlert.state == "claimed",
                PersonaModelRetirementAlert.lease_expires_at <= current,
                PersonaModelRetirementAlert.attempt_count < max_attempts,
            )
            .values(
                claim_token=token,
                lease_expires_at=lease,
                attempt_count=PersonaModelRetirementAlert.attempt_count + 1,
                last_error=None,
                claimed_at=current,
                updated_at=current,
            )
        )
        await session.commit()
        if retried.rowcount == 1:
            return ClaimedRetirement(candidate=candidate, claim_token=token, retry=True)
        return None


async def mark_delivered(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedRetirement,
    *,
    now: datetime | None = None,
) -> bool:
    """Transition only the still-owned claim token to delivered."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    async with session_factory() as session:
        result = await session.execute(
            update(PersonaModelRetirementAlert)
            .where(
                PersonaModelRetirementAlert.preference_id == claimed.candidate.preference_id,
                PersonaModelRetirementAlert.canonical_model_id == claimed.candidate.model_id,
                PersonaModelRetirementAlert.lifecycle_revision == claimed.candidate.lifecycle_revision,
                PersonaModelRetirementAlert.org_id == claimed.candidate.org_id,
                PersonaModelRetirementAlert.state == "claimed",
                PersonaModelRetirementAlert.claim_token == claimed.claim_token,
            )
            .values(state="delivered", delivered_at=current, updated_at=current, last_error=None)
        )
        await session.commit()
        return result.rowcount == 1


async def mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedRetirement,
    error: Exception,
    *,
    now: datetime | None = None,
) -> bool:
    """Release the lease for retry while storing only a sanitized error class."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    async with session_factory() as session:
        result = await session.execute(
            update(PersonaModelRetirementAlert)
            .where(
                PersonaModelRetirementAlert.preference_id == claimed.candidate.preference_id,
                PersonaModelRetirementAlert.canonical_model_id == claimed.candidate.model_id,
                PersonaModelRetirementAlert.lifecycle_revision == claimed.candidate.lifecycle_revision,
                PersonaModelRetirementAlert.org_id == claimed.candidate.org_id,
                PersonaModelRetirementAlert.state == "claimed",
                PersonaModelRetirementAlert.claim_token == claimed.claim_token,
            )
            .values(
                last_error=type(error).__name__[:512],
                lease_expires_at=current,
                updated_at=current,
            )
        )
        await session.commit()
        return result.rowcount == 1


def _notification(candidate: RetirementCandidate) -> Notification:
    return Notification(
        org_id=candidate.org_id,
        flow_id=candidate.preference_id,
        node_id=candidate.preference_id,
        event="persona_model_retired",
        summary=f"Saved {candidate.persona_key} preference points to retired model {candidate.model_id}.",
        detail={
            "audience": "platform_operator",
            "preference_id": candidate.preference_id,
            "persona_key": candidate.persona_key,
            "preference_owner_kind": candidate.owner_kind,
            "preference_owner_id": candidate.owner_id,
            "canonical_model_id": candidate.model_id,
            "lifecycle_revision": candidate.lifecycle_revision,
        },
    )


async def run_retirement_alert_pass(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    notify_fn: Callable[[Notification], str] = notify,
    now: datetime | None = None,
) -> RetirementAlertReport:
    """Scan, durably claim, publish and seal retired mapping transitions."""
    report = RetirementAlertReport()
    try:
        async with session_factory() as session:
            candidates = await _candidates(session)
    except Exception:
        report.errors += 1
        return report
    report.mappings_examined = len(candidates)

    for candidate in candidates:
        try:
            claimed = await claim_retirement(session_factory, candidate, now=now)
        except Exception:  # each mapping is independent; keep scanning
            report.errors += 1
            continue
        if claimed is None:
            report.lost_claims += 1
            continue
        report.claims_acquired += 1
        if claimed.retry:
            report.retries_acquired += 1
        try:
            notify_fn(_notification(candidate))
        except Exception as exc:  # notification failures are durable and retryable
            report.notifications_failed += 1
            try:
                released = await mark_failed(session_factory, claimed, exc, now=now)
            except Exception:
                report.errors += 1
            else:
                if not released:
                    report.errors += 1
            continue
        try:
            delivered = await mark_delivered(session_factory, claimed, now=now)
        except Exception:
            report.errors += 1
        else:
            if delivered:
                report.delivered += 1
            else:
                report.errors += 1
    return report
