"""Revision and idempotency checks shared by administrator metering transactions."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.budget.metering_incident_authority import verify_metering_incident_grant
from src.shared.models.metering import MeteringAttempt, MeteringAttemptScope, MeteringReconciliationEvent


class MeteringReconciliationConflictError(Exception):
    pass


@dataclass(frozen=True)
class MeteringReconciliationCommand:
    actor_id: str
    incident_id: str
    expected_revision: int
    idempotency_key: str
    payload_sha256: str
    action: str
    reason: str
    evidence_ref: str
    evidence_sha256: str
    evidence_version: str


async def reconciliation_result(session: AsyncSession, row: MeteringAttempt, scope_keys: tuple[str, ...], receipt_sha256: str | None) -> dict:
    now = datetime.now(UTC)
    unresolved = await session.scalar(
        select(MeteringAttempt.request_id)
        .join(
            MeteringAttemptScope,
            and_(
                MeteringAttempt.tenant_id == MeteringAttemptScope.tenant_id,
                MeteringAttempt.request_id == MeteringAttemptScope.request_id,
            ),
        )
        .where(
            MeteringAttemptScope.scope_key.in_(scope_keys),
            or_(
                MeteringAttempt.state.in_(("unknown", "quarantined")),
                and_(MeteringAttempt.state == "prepared", MeteringAttempt.prepare_deadline <= now),
                and_(MeteringAttempt.state == "dispatch_armed", MeteringAttempt.completion_deadline <= now),
            ),
        )
        .limit(1)
    )
    return {
        "disposition": row.state,
        "revision": row.revision,
        "spend_blocked": unresolved is not None,
        "exact_receipt": receipt_sha256 is not None,
    }


def require_current_incident_grant(command: MeteringReconciliationCommand, row: MeteringAttempt) -> None:
    grant = verify_metering_incident_grant(
        actor_id=command.actor_id,
        tenant_id=row.tenant_id,
        request_id=row.request_id,
        action=command.action,
        expected_revision=command.expected_revision,
        evidence_ref=command.evidence_ref,
        evidence_version=command.evidence_version,
        evidence_sha256=command.evidence_sha256,
    )
    if grant.incident_id != command.incident_id:
        from fastapi import HTTPException

        raise HTTPException(403, "Protected incident authority changed")


async def check_reconciliation_command(session: AsyncSession, row: MeteringAttempt, command: MeteringReconciliationCommand) -> dict | None:
    require_current_incident_grant(command, row)
    existing = await session.scalar(
        select(MeteringReconciliationEvent)
        .where(
            MeteringReconciliationEvent.tenant_id == row.tenant_id,
            MeteringReconciliationEvent.request_id == row.request_id,
            MeteringReconciliationEvent.idempotency_key == command.idempotency_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.payload_sha256 != command.payload_sha256 or existing.actor_id != command.actor_id or existing.authority != command.incident_id:
            raise MeteringReconciliationConflictError("Idempotency key conflicts with a prior action")
        if existing.result_snapshot is None:
            raise RuntimeError("Stored reconciliation response unavailable")
        return existing.result_snapshot
    if row.revision != command.expected_revision:
        raise MeteringReconciliationConflictError("Attempt changed. Refresh before reconciling")
    return None


async def record_reconciliation_command(
    session: AsyncSession,
    row: MeteringAttempt,
    command: MeteringReconciliationCommand,
    *,
    prior_revision: int,
    receipt_sha256: str | None,
    scope_keys: tuple[str, ...],
) -> dict:
    require_current_incident_grant(command, row)
    await session.flush()
    result = await reconciliation_result(session, row, scope_keys, receipt_sha256)
    session.add(
        MeteringReconciliationEvent(
            id=str(uuid4()),
            tenant_id=row.tenant_id,
            request_id=row.request_id,
            idempotency_key=command.idempotency_key,
            payload_sha256=command.payload_sha256,
            actor_id=command.actor_id,
            authority=command.incident_id,
            prior_revision=prior_revision,
            new_revision=row.revision,
            action=command.action,
            reason=command.reason,
            evidence_ref=command.evidence_ref,
            evidence_sha256=command.evidence_sha256,
            receipt_sha256=receipt_sha256,
            disposition=row.state,
            result_snapshot=result,
        )
    )
    return result


async def cancel_prepared_attempt(
    session_factory, journal, *, admission, scope_keys: tuple[str, ...], command: MeteringReconciliationCommand
) -> dict:
    import asyncio
    from decimal import Decimal

    from sqlalchemy import text

    from src.shared.models.metering import MeteringScopeGate

    if command.action != "confirm_not_dispatched" or command.evidence_ref != admission.key or command.evidence_sha256 != admission.sha256:
        raise ValueError("administrator evidence does not match acknowledged admission")
    evidence = await journal.read_admission(admission, deadline=asyncio.get_running_loop().time() + 5)
    async with session_factory() as session:
        await session.execute(text("SET LOCAL lock_timeout = '750ms'"))
        await session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
        for key in sorted(scope_keys):
            gate = await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == key).with_for_update())
            if gate is None:
                raise ValueError("metering cancellation gate unavailable")
        row = await session.scalar(
            select(MeteringAttempt)
            .where(MeteringAttempt.tenant_id == admission.tenant_id, MeteringAttempt.request_id == admission.request_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError("metering cancellation attempt unavailable")
        replay = await check_reconciliation_command(session, row, command)
        if replay is not None:
            return replay
        if (
            row.owner_id != admission.owner_id
            or row.state != "prepared"
            or row.final_version is not None
            or row.final_sha256 is not None
            or row.admission_key != admission.key
            or row.admission_version != admission.version_id
            or row.admission_sha256 != admission.sha256
            or row.attribution_snapshot.get("scopes") != list(sorted(scope_keys))
            or row.request_sha256 != evidence.get("request_sha256")
            or row.submitted_sha256 != evidence.get("submitted_sha256")
            or Decimal(evidence["bound_usd"]) != row.bound_usd
        ):
            raise ValueError("never-dispatched attempt cannot be proved")
        prior = row.revision
        row.state = "cancelled_unsubmitted"
        row.reconciliation_status = "proven_unsubmitted"
        row.revision += 1
        result = await record_reconciliation_command(session, row, command, prior_revision=prior, receipt_sha256=None, scope_keys=scope_keys)
        await session.commit()
        return result
