"""Bounded, fenced recovery of durable metering attempts without replaying inference."""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import rfc8785
from sqlalchemy import or_, select, text, tuple_

from src.budget.metering_settlement import settle_metering_receipt
from src.shared.models.metering import MeteringAttempt, MeteringAttemptScope, MeteringReconciliationEvent, MeteringScopeGate
from src.usage.metering_journal import ImmutableMeteringJournal, MeteringObjectReceipt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryCandidate:
    tenant_id: str
    request_id: str
    owner_id: str
    revision: int
    producer_epoch: int
    admission_key: str
    admission_version: str
    admission_sha256: str
    final_key: str
    final_version: str | None
    final_sha256: str | None
    completion_deadline: datetime

    def admission(self) -> MeteringObjectReceipt:
        return MeteringObjectReceipt(
            self.tenant_id, self.request_id, self.owner_id, self.admission_key, self.admission_version, self.admission_sha256
        )

    def final(self) -> MeteringObjectReceipt | None:
        if self.final_version and self.final_sha256:
            return MeteringObjectReceipt(self.tenant_id, self.request_id, self.owner_id, self.final_key, self.final_version, self.final_sha256)
        return None


async def scan_metering_attempts(
    session_factory,
    journal: ImmutableMeteringJournal,
    *,
    now: datetime | None = None,
    limit: int = 100,
    cursor: tuple[datetime, str, str] | None = None,
    budget_seconds: float = 30,
) -> tuple[list[str], tuple[datetime, str, str] | None]:
    """Process one bounded page. Return a cursor even when evidence is unavailable."""
    if not 1 <= limit <= 100:
        raise ValueError("metering recovery page must contain 1–100 attempts")
    if not 0 < budget_seconds <= 30:
        raise ValueError("metering recovery budget must be at most 30 seconds")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("metering recovery clock must be timezone-aware")
    async with asyncio.timeout_at(deadline):
        async with session_factory() as session:
            await session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            query = (
                select(MeteringAttempt)
                .where(
                    MeteringAttempt.state.in_(("dispatch_armed", "unknown", "quarantined")),
                    or_(
                        MeteringAttempt.completion_deadline <= now,
                        MeteringAttempt.final_version.is_not(None),
                        MeteringAttempt.state.in_(("unknown", "quarantined")),
                    ),
                )
                .order_by(MeteringAttempt.completion_deadline, MeteringAttempt.tenant_id, MeteringAttempt.request_id)
                .limit(limit)
            )
            if cursor is not None:
                query = query.where(tuple_(MeteringAttempt.completion_deadline, MeteringAttempt.tenant_id, MeteringAttempt.request_id) > cursor)
            rows = (await session.scalars(query)).all()
            candidates = [
                RecoveryCandidate(
                    row.tenant_id,
                    row.request_id,
                    row.owner_id,
                    row.revision,
                    row.producer_epoch,
                    row.admission_key,
                    row.admission_version,
                    row.admission_sha256,
                    row.final_key,
                    row.final_version,
                    row.final_sha256,
                    row.completion_deadline,
                )
                for row in rows
            ]
    results = []
    next_cursor = None
    for candidate in candidates:
        if loop.time() >= deadline:
            return results, next_cursor
        next_cursor = (candidate.completion_deadline, candidate.tenant_id, candidate.request_id)
        try:
            async with asyncio.timeout_at(min(deadline, loop.time() + 5)):
                results.append(await recover_metering_attempt(session_factory, journal, candidate, now=now, deadline=deadline))
        except TimeoutError:
            logger.warning("Metering recovery time budget retained affected exposure", extra={"request_id": candidate.request_id})
            results.append("held")
        except Exception:
            logger.exception("Metering recovery retained affected exposure", extra={"request_id": candidate.request_id})
            results.append("held")
    return results, next_cursor if len(candidates) == limit else None


async def recover_metering_attempt(
    session_factory, journal: ImmutableMeteringJournal, candidate: RecoveryCandidate, *, now: datetime, deadline: float
) -> str:
    admission = candidate.admission()
    receipt = candidate.final()
    if receipt is None:
        receipt = await journal.discover_final(admission, deadline=min(deadline, asyncio.get_running_loop().time() + 5))
    evidence = await journal.read_final(receipt, deadline=min(deadline, asyncio.get_running_loop().time() + 5)) if receipt else None
    unsent_receipt = await journal.discover_unsent(admission, deadline=min(deadline, asyncio.get_running_loop().time() + 5))
    unsent = await journal.read_unsent(unsent_receipt, deadline=min(deadline, asyncio.get_running_loop().time() + 5)) if unsent_receipt else None
    async with session_factory() as session:
        await session.execute(text("SET LOCAL lock_timeout = '750ms'"))
        await session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
        scope_keys = tuple(
            (
                await session.scalars(
                    select(MeteringAttemptScope.scope_key)
                    .where(MeteringAttemptScope.tenant_id == candidate.tenant_id, MeteringAttemptScope.request_id == candidate.request_id)
                    .order_by(MeteringAttemptScope.scope_key)
                )
            ).all()
        )
        for key in scope_keys:
            if await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == key).with_for_update()) is None:
                raise ValueError("metering recovery scope gate missing")
        row = await session.scalar(
            select(MeteringAttempt)
            .where(MeteringAttempt.tenant_id == candidate.tenant_id, MeteringAttempt.request_id == candidate.request_id)
            .with_for_update()
        )
        if (
            row is None
            or row.revision != candidate.revision
            or row.producer_epoch != candidate.producer_epoch
            or row.state not in ("dispatch_armed", "unknown", "quarantined")
            or row.owner_id != candidate.owner_id
            or row.admission_key != admission.key
            or row.admission_version != admission.version_id
            or row.admission_sha256 != admission.sha256
            or row.final_key != candidate.final_key
            or row.attribution_snapshot.get("scopes") != list(scope_keys)
        ):
            return "stale"
        if row.state == "quarantined":
            return "quarantined"
        if unsent_receipt is not None:
            if (
                receipt is not None
                or row.final_version is not None
                or row.state != "dispatch_armed"
                or unsent["admission_sha256"] != admission.sha256
                or unsent["admission_version_id"] != admission.version_id
                or unsent["revision"] != row.revision
                or unsent["producer_epoch"] != row.producer_epoch
                or unsent["request_sha256"] != row.request_sha256
                or unsent["submitted_sha256"] != row.submitted_sha256
                or unsent["quote_sha256"] != hashlib.sha256(rfc8785.dumps(row.quote_snapshot)).hexdigest()
            ):
                action, new_state = "quarantine", "quarantined"
            else:
                action, new_state = "cancel_unsent", "cancelled_unsubmitted"
        elif receipt is None:
            if row.state != "dispatch_armed" or row.completion_deadline > now:
                return "held"
            action, new_state = "unknown", "unknown"
        elif (
            receipt.key != row.final_key
            or receipt.tenant_id != row.tenant_id
            or receipt.owner_id != row.owner_id
            or evidence.get("admission_version_id") != admission.version_id
            or evidence.get("admission_sha256") != admission.sha256
            or (row.final_version is not None and (row.final_version, row.final_sha256) != (receipt.version_id, receipt.sha256))
        ):
            action, new_state = "quarantine", "quarantined"
        else:
            action, new_state = "attach", row.state
            row.final_version, row.final_sha256 = receipt.version_id, receipt.sha256
        if action == "quarantine" and row.state == "quarantined":
            return "quarantined"
        if action == "attach" and candidate.final() is not None:
            await session.rollback()
        else:
            prior = row.revision
            row.state = new_state
            row.reconciliation_status = new_state if action != "attach" else "receipt_attached"
            row.revision += 1
            proof_receipt = unsent_receipt if action == "cancel_unsent" else receipt
            digest = proof_receipt.sha256 if proof_receipt is not None else admission.sha256
            payload = rfc8785.dumps([row.tenant_id, row.request_id, action, prior, row.producer_epoch, digest])
            session.add(
                MeteringReconciliationEvent(
                    id=str(uuid4()),
                    tenant_id=row.tenant_id,
                    request_id=row.request_id,
                    idempotency_key=hashlib.sha256(payload).hexdigest(),
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    actor_id="metering-recovery",
                    authority="trusted-journal",
                    prior_revision=prior,
                    new_revision=row.revision,
                    action=action,
                    reason=(
                        "verified no-send proof"
                        if action == "cancel_unsent"
                        else "contradictory no-send and final evidence"
                        if unsent_receipt is not None and receipt is not None
                        else "verified receipt"
                        if receipt is not None
                        else "completion deadline passed without receipt"
                    ),
                    evidence_ref=proof_receipt.key if proof_receipt is not None else admission.key,
                    evidence_sha256=digest,
                    receipt_sha256=receipt.sha256 if receipt is not None and action == "attach" else None,
                    disposition=new_state,
                )
            )
            await session.commit()
    if action != "attach":
        return new_state
    await settle_metering_receipt(session_factory, journal, receipt=receipt, admission=admission, scope_keys=scope_keys, lock_timeout_ms=750)
    return "settled"


async def maintain_metering_recovery(session_factory, journal: ImmutableMeteringJournal, *, interval_seconds: float = 5) -> None:
    cursor = None
    while True:
        try:
            _, cursor = await scan_metering_attempts(session_factory, journal, cursor=cursor)
        except Exception:
            logger.exception("Metering recovery scan failed; durable holds remain")
            cursor = None
        await asyncio.sleep(interval_seconds)
