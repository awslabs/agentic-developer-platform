"""Durable Product-owned repository capture, recovered by every Gateway replica.

Only an already verified admission may enqueue work. The retained subject is an
authorization subject, never a token: current membership, source ownership and
the revocable work delegation are checked again by GatewayAdmissionSourceClient.
No native assessment is submitted until all snapshot receipts are sealed.
"""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.products.models import ProductOperation
from src.shared.models.base import new_uuid

OPERATION = "captureL5Source"
CAPTURE_SECONDS = 300
LEASE_SECONDS = 330
MAX_ATTEMPTS = 3
LOGGER = logging.getLogger(__name__)


class CaptureSubject(BaseModel):
    """Retained admission subject; not an authentication token or admin bypass."""

    model_config = ConfigDict(extra="forbid")
    user_id: str
    org_id: str
    team_id: str
    account_type: Literal["human", "service"]
    auth_source: str
    scope: str
    agent_registry_id: str
    is_admin: Literal[False] = False
    cognito_username: Literal[""] = ""
    credential_scopes: list[str] = []


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pending(job):
    return HTTPException(
        503,
        detail={
            "code": "source_capture_preparing",
            "preparation_id": job.id,
            "stage": (job.receipt or {}).get("stage", "queued"),
            "retry_after_seconds": 5,
            "message": "Preparing the complete repository snapshot. This continues if you leave the page; "
            "reuse the original request to check its run.",
        },
        headers={"Retry-After": "5", "Cache-Control": "no-store"},
    )


async def prepare_capture(client, body, binding, preparation, key):
    """Called only after the caller has checked the current admission authority."""
    if (
        client.service_id != "security"
        or client.operation not in {"submit", "retry"}
        or not isinstance(client.run_input, dict)
        or client.run_input.get("capability") != "code_review"
        or client.operation == "retry"
        and not client.retry_context
    ):
        raise HTTPException(409, detail={"code": "source_preparation_unavailable"})
    actor = client.actor
    subject = {
        name: getattr(actor, name, "")
        for name in (
            "user_id",
            "org_id",
            "team_id",
            "account_type",
            "auth_source",
            "scope",
            "agent_registry_id",
        )
    }
    # Persist no admin bypass, JWT, credentials, or caller-influenced attribution.
    subject.update(is_admin=False, cognito_username="", credential_scopes=list(getattr(actor, "credential_scopes", [])))
    request = {
        "source_request": body.model_dump(mode="json"),
        "capture_key": key,
        "admission_key": client.admission_key,
        "verified": client.verified,
        "run_input": client.run_input,
        "subject": subject,
        "operation": client.operation,
        "retry_context": client.retry_context,
    }
    key_hash = _digest([client.grant_id, key])
    query = (
        select(ProductOperation)
        .where(
            ProductOperation.tenant_id == client.tenant_id,
            ProductOperation.actor_id == client.owner_id,
            ProductOperation.operation == OPERATION,
            ProductOperation.key_hash == key_hash,
        )
        .execution_options(populate_existing=True)
    )
    job = await client.db.scalar(query)
    if job is None:
        job = ProductOperation(
            tenant_id=client.tenant_id,
            id=new_uuid(),
            actor_id=client.owner_id,
            product_id=preparation.product_id,
            project_id=preparation.project_id,
            binding_id=binding.id,
            binding_generation=binding.generation,
            operation=OPERATION,
            key_hash=key_hash,
            request_hash=_digest(request),
            request=request,
            state="pending",
            expires_at=preparation.deadline_at,
            receipt={"stage": "queued"},
        )
        client.db.add(job)
        try:
            await client.db.commit()
        except IntegrityError:
            await client.db.rollback()
            job = await client.db.scalar(query)
            if job is None:
                raise
    if job.request_hash != _digest(request):
        raise HTTPException(409, detail={"code": "idempotency_conflict"})
    if _aware(job.expires_at) <= datetime.now(UTC):
        raise HTTPException(
            410,
            detail={
                "code": "source_expired",
                "message": "The original assessment authorization expired during preparation. Review the saved setup before starting another run.",
            },
        )
    if job.state in {"failed", "cancelled"}:
        raise HTTPException(
            409,
            detail={
                "code": "source_capture_failed",
                "preparation_id": job.id,
                "cause": (job.receipt or {}).get("code", "source_capture_unavailable"),
                "message": "Repository preparation could not complete. Check source access, repository limits "
                "and the saved authorization before starting another run.",
            },
        )
    raise pending(job)


def _available(now):
    return and_(
        ProductOperation.operation == OPERATION,
        ProductOperation.expires_at > now,
        ProductOperation.attempt_count < MAX_ATTEMPTS,
        or_(ProductOperation.retry_at.is_(None), ProductOperation.retry_at <= now),
        or_(ProductOperation.state == "pending", and_(ProductOperation.state == "running", ProductOperation.lease_until <= now)),
    )


async def claim_capture(sessions, *, now=None):
    now = now or datetime.now(UTC)
    async with sessions() as db:
        # Expired/exhausted work is terminal and never receives a new deadline.
        await db.execute(
            update(ProductOperation)
            .where(
                ProductOperation.operation == OPERATION,
                ProductOperation.state.in_(("pending", "running")),
                or_(ProductOperation.expires_at <= now, and_(ProductOperation.attempt_count >= MAX_ATTEMPTS, ProductOperation.lease_until <= now)),
            )
            .values(state="failed", lease_owner=None, lease_until=None, receipt={"code": "source_capture_deadline_or_attempt_limit"})
        )
        candidate = await db.scalar(select(ProductOperation).where(_available(now)).order_by(ProductOperation.created_at).limit(1))
        if candidate is None:
            await db.commit()
            return None
        owner = new_uuid()
        changed = await db.execute(
            update(ProductOperation)
            .where(
                ProductOperation.tenant_id == candidate.tenant_id,
                ProductOperation.id == candidate.id,
                _available(now),
            )
            .values(
                state="running",
                lease_owner=owner,
                lease_until=min(now + timedelta(seconds=LEASE_SECONDS), _aware(candidate.expires_at)),
                attempt_count=ProductOperation.attempt_count + 1,
                receipt={"stage": "capturing"},
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        identity = candidate.tenant_id, candidate.id, owner
        await db.commit()
        return identity if changed.rowcount == 1 else None


async def current_capture(db, identity, *, seal=False):
    tenant, identifier, owner = identity
    job = await db.get(ProductOperation, (tenant, identifier), populate_existing=True, with_for_update=seal)
    if (
        job is None
        or job.operation != OPERATION
        or job.state != "running"
        or job.lease_owner != owner
        or job.lease_until is None
        or min(_aware(job.lease_until), _aware(job.expires_at)) <= datetime.now(UTC)
    ):
        raise HTTPException(409, detail={"code": "source_capture_lease_changed"})
    return job


async def update_capture(db, identity, values, *, require_live=True):
    """The write itself fences stale workers, including error/shutdown races."""
    now = datetime.now(UTC)
    guards = [
        ProductOperation.tenant_id == identity[0],
        ProductOperation.id == identity[1],
        ProductOperation.operation == OPERATION,
        ProductOperation.state == "running",
        ProductOperation.lease_owner == identity[2],
    ]
    if require_live:
        guards.extend([ProductOperation.lease_until > now, ProductOperation.expires_at > now])
    changed = await db.execute(
        update(ProductOperation).where(*guards).values(**values, updated_at=now), execution_options={"synchronize_session": False}
    )
    if changed.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, detail={"code": "source_capture_lease_changed"})
    await db.commit()


async def _continue_submission(db, job):
    """Recheck the original selection before admitting it; reuse its exact key."""
    from src.l5_integration import GatewayConfigurationBackend
    from src.l5_queue_registry import publisher

    request = job.request
    backend = GatewayConfigurationBackend(db, CaptureSubject.model_validate(request["subject"]), publish=publisher("security"))
    if request["subject"]["account_type"] == "human":
        if request["operation"] == "retry":
            original = request["retry_context"]
            return await backend.retry_run(
                job.product_id, job.project_id, "security", original["run_id"], original["reason"], request["admission_key"]
            )
        return await backend.submit(job.product_id, job.project_id, "security", request["run_input"], request["admission_key"])
    from l5_services.events.sources import admit_source_receipt
    from l5_services.models.event import EventReceipt

    from src.l5_security_schedule import schedule_authority

    verified = request["verified"]
    receipt = await db.get(
        EventReceipt,
        (
            job.tenant_id,
            job.product_id,
            job.project_id,
            "security",
            verified["trigger"]["trigger_id"],
            verified["event_id"],
        ),
    )
    if receipt is None or receipt.idempotency_key != request["admission_key"] or receipt.run_input != request["run_input"]:
        raise HTTPException(404, detail={"code": "not_found"})
    return await admit_source_receipt(db, backend.sessions, receipt, lambda selected: schedule_authority(backend, selected), backend.publish)


async def run_capture(sessions, identity, *, continue_submission=_continue_submission):
    from src.products.l5_admission_capture import GatewayAdmissionSourceClient

    try:
        async with asyncio.timeout(CAPTURE_SECONDS):
            async with sessions() as db:
                job = await current_capture(db, identity)
                request = job.request
                source = request["source_request"]

                async def fence(current_db, *, seal=False):
                    await current_capture(current_db, identity, seal=seal)

                client = GatewayAdmissionSourceClient(
                    db,
                    CaptureSubject.model_validate(request["subject"]),
                    source["delegation_id"],
                    request["verified"],
                    request["operation"],
                    request["admission_key"],
                    service_id="security",
                    run_input=request["run_input"],
                    capture_fence=fence,
                )
                snapshot = await client.pin_source(job.product_id, job.project_id, source, idempotency_key=request["capture_key"])
                job = await current_capture(db, identity)
                await update_capture(db, identity, {"receipt": {"stage": "finalizing", "snapshot_id": snapshot["snapshot_id"]}})
                try:
                    await continue_submission(db, job)
                except HTTPException as error:
                    if not isinstance(error.detail, dict) or error.detail.get("code") != "source_capture_preparing":
                        raise
                    # A second source now owns the durable continuation.
                await update_capture(db, identity, {"state": "succeeded", "lease_owner": None, "lease_until": None})
                return
    except asyncio.CancelledError:
        # Leave the lease durable; another replica recovers after its expiry.
        raise
    except Exception as error:
        code = "source_capture_timed_out" if isinstance(error, TimeoutError) else "source_capture_unavailable"
        transient = isinstance(error, TimeoutError | ConnectionError)
        if isinstance(error, DBAPIError):
            sqlstate = getattr(error.orig, "sqlstate", None)
            transient = error.connection_invalidated or sqlstate in {"40001", "40P01", "08003", "08006", "57P01"}
        if isinstance(error, HTTPException):
            code = error.detail.get("code", code) if isinstance(error.detail, dict) else code
            transient = error.status_code >= 500
        async with sessions() as db:
            job = await db.get(ProductOperation, identity[:2], populate_existing=True)
            if job is None or job.state != "running" or job.lease_owner != identity[2]:
                return
            retry = transient and job.attempt_count < MAX_ATTEMPTS and _aware(job.expires_at) > datetime.now(UTC)
            try:
                await update_capture(
                    db,
                    identity,
                    {
                        "state": "pending" if retry else "failed",
                        "lease_owner": None,
                        "lease_until": None,
                        "retry_at": datetime.now(UTC) + timedelta(seconds=10) if retry else None,
                        "receipt": {"code": code, "stage": "queued" if retry else "failed"},
                    },
                    require_live=False,
                )
            except HTTPException:
                return
        LOGGER.warning("Product repository preparation deferred or failed", extra={"code": code, "retryable": retry})


async def maintain_source_captures(sessions):
    """One bounded capture per replica; database leases coordinate all replicas."""
    while True:
        try:
            identity = await claim_capture(sessions)
            if identity is not None:
                await run_capture(sessions, identity)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("Product capture recovery unavailable")
        await asyncio.sleep(5)
