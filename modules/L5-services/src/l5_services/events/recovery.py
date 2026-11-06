"""Reconcile event receipts with independently committed run admissions."""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from l5_services.events.sources import admit_source_receipt
from l5_services.events.schedules import finops_admissions_enabled
from l5_services.models.event import EventReceipt
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.events.observations import observe

LOGGER = logging.getLogger(__name__)


async def repair_committed_events(sessions, *, limit=50, service_id=None):
    if not 1 <= limit <= 50:
        raise ValueError("repair limit must be between 1 and 50")
    async with sessions() as session:
        pairs = (await session.execute(select(EventReceipt, RunReceipt.run_id).join(
            RunReceipt, and_(
                RunReceipt.tenant_id == EventReceipt.tenant_id,
                RunReceipt.product_id == EventReceipt.product_id,
                RunReceipt.project_id == EventReceipt.project_id,
                RunReceipt.service_id == EventReceipt.service_id,
                RunReceipt.idempotency_key == EventReceipt.idempotency_key,
                RunReceipt.operation == "submit",
            ),
        ).where(EventReceipt.state == "pending",
                EventReceipt.service_id == service_id if service_id else True).order_by(
            EventReceipt.received_at, EventReceipt.tenant_id, EventReceipt.trigger_id, EventReceipt.event_id,
        ).limit(limit))).all()
        repaired = 0
        for receipt, run_id in pairs:
            run = await session.get(Run, (receipt.tenant_id, run_id))
            if (run is None or run.product_id != receipt.product_id or run.project_id != receipt.project_id
                    or run.service_id != receipt.service_id or run.trigger_id != receipt.trigger_id
                    or run.configuration_id != receipt.configuration_id
                    or run.configuration_revision != receipt.configuration_revision
                    or run.input != receipt.run_input or run.admission.get("event_id") != receipt.event_id):
                receipt.state, receipt.reason = "conflict", "event_lineage_conflict"
            else:
                receipt.run_id, receipt.state, receipt.reason = run_id, "admitted", None
                observe(session, receipt, "recovered_commit", datetime.now(UTC))
                repaired += 1
            await session.commit()
        return repaired


async def repair_pending_events(service, *, now=None, limit=50):
    verify_target = getattr(service, "verify_event_target", None)
    admit_event = getattr(service, "admit_event", None)
    if verify_target is None and not callable(admit_event):
        return 0
    if not 1 <= limit <= 50:
        raise ValueError("repair limit must be between 1 and 50")
    now = now or datetime.now(UTC)
    async with service.sessions() as session:
        pending = select(
            EventReceipt.tenant_id, EventReceipt.product_id, EventReceipt.project_id,
            EventReceipt.service_id, EventReceipt.trigger_id, EventReceipt.event_id,
        ).where(EventReceipt.state == "pending", or_(
            EventReceipt.retry_at.is_(None), EventReceipt.retry_at <= now,
        ))
        if getattr(service, "service_id", None):
            pending = pending.where(EventReceipt.service_id == service.service_id)
        if not finops_admissions_enabled():
            pending = pending.where(EventReceipt.service_id != "finops")
        keys = (await session.execute(pending.order_by(
            EventReceipt.received_at, EventReceipt.tenant_id,
            EventReceipt.trigger_id, EventReceipt.event_id,
        ).limit(limit))).all()
    admitted = 0
    for key in keys:
        async with service.sessions() as session:
            receipt = await session.get(EventReceipt, tuple(key), with_for_update=True)
            if receipt is None or receipt.state != "pending":
                continue
            receipt.recovery_attempts += 1
            receipt.retry_at = now + timedelta(seconds=min(900, 60 * 2 ** min(receipt.recovery_attempts - 1, 4)))
            await session.commit()
            try:
                if callable(admit_event):
                    await admit_event(receipt)
                    await session.refresh(receipt)
                    if receipt.state != "admitted" or not receipt.run_id:
                        raise HTTPException(503, detail={"code": "schedule_admission_unverified"})
                else:
                    await admit_source_receipt(session, service.sessions, receipt, verify_target, service.publish,
                                               now=now, budget_guard=getattr(service, "budget_guard", None))
                receipt.reason = None
                observe(session, receipt, "repair_admitted", now)
                await session.commit()
                admitted += 1
            except HTTPException as error:
                await session.rollback()
                receipt = await session.get(EventReceipt, tuple(key))
                if receipt.state != "pending":
                    continue
                code = error.detail.get("code") if isinstance(error.detail, dict) else None
                # A permanent authorization denial stays actionable instead of
                # retrying a nightly schedule forever; transport faults retry.
                if error.status_code in {404, 409, 410, 422} or error.status_code == 403 and receipt.service_id in {
                        "well_architected", "transform", "security"}:
                    receipt.reason = code if isinstance(code, str) and len(code) <= 64 else "admission_rejected"
                    receipt.state = "rejected"
                else:
                    receipt.reason = (code if receipt.service_id in {"well_architected", "transform", "security"} and code in {
                        "native_readback_unavailable", "schedule_gateway_unavailable", "work_queue_unavailable", "branch_resolution_unavailable",
                        "selected_connection_unavailable", "schedule_admission_unavailable", "schedule_admission_unverified",
                        "transform_qualification_unavailable", "transform_configuration_unavailable",
                        "security_native_refresh_unavailable", "security_provider_identity_unavailable",
                    } else "retryable_dependency")
                observe(session, receipt, "repair_denied", now)
                await session.commit()
            except Exception:
                await session.rollback()
                LOGGER.exception("Failed to repair pending L5 event")
    return admitted
