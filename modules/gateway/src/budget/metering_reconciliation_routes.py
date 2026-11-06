"""Human-only evidence-backed resolution of durable metering attempts."""

import hashlib
import os
from typing import Annotated, Literal

import rfc8785
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session
from src.auth.dependencies import get_current_user
from src.budget.metering_admission import MeteringAdmissionError
from src.budget.metering_incident_authority import verify_metering_incident_grant
from src.budget.metering_reconciliation import (
    MeteringReconciliationCommand,
    MeteringReconciliationConflictError,
    cancel_prepared_attempt,
    check_reconciliation_command,
)
from src.budget.metering_settlement import settle_metering_receipt
from src.shared.config import get_settings
from src.shared.database import get_accounting_session_factory, get_db
from src.shared.models.audit import AuditLog
from src.shared.models.metering import MeteringAttempt
from src.shared.schemas.auth import TokenContext
from src.usage.metering_journal import ImmutableMeteringJournal, MeteringObjectReceipt

router = APIRouter(prefix="/admin/metering", tags=["metering"])
User = Annotated[TokenContext, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
RequestID = Annotated[str, Path(min_length=1, max_length=255)]


class MeteringReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=255)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    action: Literal["apply_trusted_receipt", "confirm_not_dispatched"]
    reason: str = Field(min_length=3, max_length=1000)
    evidence_ref: str = Field(min_length=1, max_length=768)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_key: str | None = Field(default=None, min_length=1, max_length=768)
    receipt_version: str | None = Field(default=None, min_length=1, max_length=255)
    receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evidence_for_action(self):
        if self.action == "apply_trusted_receipt":
            if not self.receipt_key or not self.receipt_version or self.receipt_hash != self.evidence_sha256 or self.receipt_key != self.evidence_ref:
                raise ValueError("trusted receipt requires exact versioned evidence")
        elif any(value is not None for value in (self.receipt_key, self.receipt_version, self.receipt_hash)):
            raise ValueError("unsubmitted proof cannot use a final usage receipt")
        return self


async def _audit_access(factory, user, body, request_id: str, outcome: str) -> None:
    async with factory() as session:
        session.add(
            AuditLog(
                org_id=body.tenant_id,
                actor_id=user.user_id,
                event_type="metering_reconciliation_access",
                details={
                    "request_id": request_id,
                    "action": body.action,
                    "outcome": outcome,
                    "evidence_ref_sha256": hashlib.sha256(body.evidence_ref.encode()).hexdigest(),
                    "evidence_sha256": body.evidence_sha256,
                    "idempotency_key_sha256": hashlib.sha256(body.idempotency_key.encode()).hexdigest(),
                },
            )
        )
        await session.commit()


@router.post("/attempts/{request_id}/reconcile")
async def reconcile_attempt(request_id: RequestID, body: MeteringReconciliationRequest, user: User, db: DB):
    factory = get_accounting_session_factory()
    try:
        await _audit_access(factory, user, body, request_id, "requested")
        return await _reconcile_attempt(request_id, body, user, db, factory)
    except (HTTPException, AccessDeniedError) as error:
        try:
            await _audit_access(factory, user, body, request_id, f"denied:{getattr(error, 'status_code', 403)}")
        except SQLAlchemyError:
            raise HTTPException(503, "Metering audit unavailable") from None
        raise
    except SQLAlchemyError:
        raise HTTPException(503, "Metering audit unavailable") from None


async def _reconcile_attempt(request_id, body, user, db, factory):
    access = AccessControl(db)
    access.require_platform_admin(user)
    await access.check_permission(user, Permission.BUDGET_UPDATE, target_org_id=body.tenant_id)
    try:
        human = await authorize_human_session(user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "A current authenticated human administrator is required") from None
    await db.rollback()
    try:
        async with factory() as session:
            row = await session.get(MeteringAttempt, (body.tenant_id, request_id))
            if row is None or not row.admission_version or not row.admission_sha256:
                raise HTTPException(404, "Metering attempt unavailable")
            admission = MeteringObjectReceipt(
                body.tenant_id, request_id, row.owner_id, row.admission_key, row.admission_version, row.admission_sha256
            )
            scope_keys = tuple(row.attribution_snapshot["scopes"])
            final_key = row.final_key
    except SQLAlchemyError:
        raise HTTPException(503, "Metering authority unavailable") from None
    expected_key = final_key if body.action == "apply_trusted_receipt" else admission.key
    if (
        body.evidence_ref != expected_key
        or expected_key != ImmutableMeteringJournal.object_keys(body.tenant_id, request_id)[1 if body.action == "apply_trusted_receipt" else 0]
    ):
        raise HTTPException(422, "Invalid metering evidence reference")
    evidence_version = body.receipt_version if body.action == "apply_trusted_receipt" else admission.version_id
    if body.action == "confirm_not_dispatched" and body.evidence_sha256 != admission.sha256:
        raise HTTPException(422, "Invalid admission evidence digest")
    grant = verify_metering_incident_grant(
        actor_id=human.user_id,
        tenant_id=body.tenant_id,
        request_id=request_id,
        action=body.action,
        expected_revision=body.expected_revision,
        evidence_ref=body.evidence_ref,
        evidence_version=evidence_version,
        evidence_sha256=body.evidence_sha256,
    )
    digest = hashlib.sha256(rfc8785.dumps({"actor_id": human.user_id, "request_id": request_id, "body": body.model_dump(mode="json")})).hexdigest()
    command = MeteringReconciliationCommand(
        actor_id=human.user_id,
        incident_id=grant.incident_id,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
        payload_sha256=digest,
        action=body.action,
        reason=body.reason,
        evidence_ref=body.evidence_ref,
        evidence_sha256=body.evidence_sha256,
        evidence_version=evidence_version,
    )
    try:
        async with factory() as session:
            current = await session.get(MeteringAttempt, (body.tenant_id, request_id))
            if current is None:
                raise HTTPException(404, "Metering attempt unavailable")
            replay = await check_reconciliation_command(session, current, command)
            if replay is not None:
                return replay
    except MeteringReconciliationConflictError:
        raise HTTPException(409, "Metering attempt changed or idempotency key conflicts") from None
    except SQLAlchemyError:
        raise HTTPException(503, "Metering authority unavailable") from None
    bucket = os.getenv("BG_METERING_JOURNAL_BUCKET")
    if not bucket:
        raise HTTPException(503, "Metering journal unavailable")
    journal = ImmutableMeteringJournal(bucket, region=get_settings().mantle_region)
    try:
        if body.action == "confirm_not_dispatched":
            return await cancel_prepared_attempt(factory, journal, admission=admission, scope_keys=scope_keys, command=command)
        receipt = MeteringObjectReceipt(body.tenant_id, request_id, admission.owner_id, body.receipt_key, body.receipt_version, body.receipt_hash)
        return await settle_metering_receipt(
            factory, journal, receipt=receipt, admission=admission, scope_keys=scope_keys, admin_command=command, lock_timeout_ms=750
        )
    except MeteringReconciliationConflictError:
        raise HTTPException(409, "Metering attempt changed or idempotency key conflicts") from None
    except (ValueError, MeteringAdmissionError):
        raise HTTPException(422, "Metering evidence cannot establish the requested action") from None
    except (BotoCoreError, ClientError, TimeoutError, SQLAlchemyError):
        raise HTTPException(503, "Metering evidence or authority unavailable") from None
