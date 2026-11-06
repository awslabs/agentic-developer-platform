"""Repair SQL-owed Context work without entering the assessment state machine."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import or_, select, update

from l5_services.models.publication import Publication
from l5_services.publication.contributions import validate_receipt
from l5_services.publication.invalidation import validate_invalidation_receipt


def _scope_conditions(client):
    from l5_services.adapters.well_architected.runtime_callbacks import PublicationClient
    from l5_services.publication.installed import InstalledPublicationClient

    if isinstance(client, (PublicationClient, InstalledPublicationClient)):
        return client.scope_conditions()
    if client.scope is None:
        raise ValueError("Context repair sweep requires a verified delegation scope")
    kind = ("well_architected_result" if getattr(client, "well_architected", False) else
            "transform_result" if getattr(client, "transform", False) else
            "finops_cost_result" if getattr(client, "finops", False) else
            "devops_result" if getattr(client, "devops", False) else None)
    return (
        Publication.destination == getattr(client, "destination", "product-context"),
        (Publication.payload["kind"].as_string() == kind if kind
         else Publication.payload["kind"].as_string().is_(None)),
        Publication.tenant_id == client.scope["tenant_id"],
        Publication.payload["scope"]["product_id"].as_string() == client.scope["product_id"],
        Publication.payload["scope"]["project_id"].as_string() == client.scope["project_id"],
    )


async def claim_publication(sessions, tenant_id, publication_id, *, now=None, lease_seconds=60):
    now = now or datetime.now(UTC)
    owner = str(uuid4())
    async with sessions() as session:
        claimed = await session.execute(update(Publication).where(
            Publication.tenant_id == tenant_id, Publication.publication_id == publication_id,
            Publication.state.in_(("pending", "retry_wait", "publishing")),
            Publication.next_due_at <= now,
            or_(Publication.lease_until.is_(None), Publication.lease_until <= now),
        ).values(state="publishing", lease_owner=owner,
                 lease_until=now + timedelta(seconds=lease_seconds),
                 attempts=Publication.attempts + 1, revision=Publication.revision + 1),
            execution_options={"synchronize_session": False})
        if claimed.rowcount != 1:
            await session.rollback()
            return None
        publication = await session.get(Publication, (tenant_id, publication_id))
        await session.commit()
        return publication, owner


async def _finish(sessions, publication, owner, *, now, state, receipt=None, error=None):
    if receipt is not None:
        validate_receipt(publication, receipt)
    wait = min(3600, 60 * 2 ** min(publication.attempts - 1, 6))
    async with sessions() as session:
        updated = await session.execute(update(Publication).where(
            Publication.tenant_id == publication.tenant_id,
            Publication.publication_id == publication.publication_id,
            Publication.state == "publishing", Publication.lease_owner == owner,
            Publication.lease_until > now,
        ).values(state=state, receipt=receipt, last_error=error,
                 next_due_at=now + timedelta(seconds=wait) if state == "retry_wait" else now,
                 lease_owner=None, lease_until=None, revision=Publication.revision + 1),
            execution_options={"synchronize_session": False})
        if updated.rowcount != 1:
            await session.rollback()
            return False
        await session.commit()
        return True


async def repair_publication(sessions, client, tenant_id, publication_id, *, now=None):
    now = now or datetime.now(UTC)
    async with sessions() as session:
        candidate = await session.get(Publication, (tenant_id, publication_id))
        if candidate is None or not client.can_publish(candidate):
            return False
    claimed = await claim_publication(sessions, tenant_id, publication_id, now=now)
    if claimed is None:
        return False
    publication, owner = claimed
    try:
        try:
            receipt = await client.read_receipt(publication)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            receipt = await client.publish(publication)
        return await _finish(sessions, publication, owner, now=now, state="published", receipt=receipt)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        state = "unavailable" if code in {400, 401, 403, 404, 409, 410, 422} else "retry_wait"
        return await _finish(sessions, publication, owner, now=now, state=state,
                             error="consumer_rejected" if state == "unavailable" else "consumer_unavailable")
    except (httpx.RequestError, ConnectionError, TimeoutError):
        return await _finish(sessions, publication, owner, now=now, state="retry_wait",
                             error="transport_unavailable")
    except ValueError:
        return await _finish(sessions, publication, owner, now=now, state="retry_wait",
                             error="receipt_invalid")


async def repair_due_publications(sessions, client, *, now=None, limit=50):
    now = now or datetime.now(UTC)
    if not 1 <= limit <= 50:
        raise ValueError("repair limit must be between 1 and 50")
    async with sessions() as session:
        due = (await session.execute(select(Publication.tenant_id, Publication.publication_id).where(
            *_scope_conditions(client),
            Publication.state.in_(("pending", "retry_wait", "publishing")),
            Publication.next_due_at <= now,
            or_(Publication.lease_until.is_(None), Publication.lease_until <= now),
        ).order_by(Publication.next_due_at, Publication.tenant_id, Publication.publication_id)
            .limit(limit))).all()
    completed = 0
    for tenant_id, publication_id in due:
        completed += bool(await repair_publication(sessions, client, tenant_id, publication_id, now=now))
    return completed


async def activate_wa_publications(sessions, client, *, now=None, limit=50):
    """Promote only blocked debts admitted by a scoped WA peer, never revoked rows."""
    if not getattr(client, "well_architected", False) or not 1 <= limit <= 50:
        raise ValueError("qualified WA consumer required")
    now = now or datetime.now(UTC)
    async with sessions() as session:
        rows = (await session.scalars(select(Publication).where(
            *_scope_conditions(client), Publication.state == "unavailable",
            Publication.last_error == "consumer_schema_unadmitted",
            Publication.result_version > 1,
            Publication.invalidation_watermark == 0,
        ).order_by(Publication.next_due_at, Publication.publication_id)
            .limit(limit).with_for_update())).all()
        activated = 0
        for row in rows:
            if client.can_publish(row):
                row.state = "pending"
                row.last_error = None
                row.next_due_at = now
                row.revision += 1
                activated += 1
        await session.commit()
        return activated


async def repair_invalidation(sessions, client, tenant_id, publication_id, *, now=None):
    now = now or datetime.now(UTC)
    owner = str(uuid4())
    async with sessions() as session:
        claimed = await session.execute(update(Publication).where(
            Publication.tenant_id == tenant_id, Publication.publication_id == publication_id,
            Publication.state == "invalidation_pending", Publication.next_due_at <= now,
            or_(Publication.lease_until.is_(None), Publication.lease_until <= now),
        ).values(lease_owner=owner, lease_until=now + timedelta(seconds=60),
                 invalidation_attempts=Publication.invalidation_attempts + 1,
                 revision=Publication.revision + 1),
            execution_options={"synchronize_session": False})
        if claimed.rowcount != 1:
            await session.rollback()
            return False
        publication = await session.get(Publication, (tenant_id, publication_id))
        await session.commit()

    try:
        receipt = await client.invalidate(publication)
        validate_invalidation_receipt(publication, receipt)
        error = None
    except httpx.HTTPStatusError as failure:
        receipt = None
        error = "invalidation_rejected" if failure.response.status_code < 500 else "consumer_unavailable"
    except (httpx.RequestError, ConnectionError, TimeoutError):
        receipt = None
        error = "transport_unavailable"
    except ValueError:
        receipt = None
        error = "receipt_invalid"

    wait = min(3600, 60 * 2 ** min(publication.invalidation_attempts - 1, 6))
    async with sessions() as session:
        changed = await session.execute(update(Publication).where(
            Publication.tenant_id == tenant_id, Publication.publication_id == publication_id,
            Publication.state == "invalidation_pending", Publication.lease_owner == owner,
            Publication.lease_until > now,
            Publication.invalidation_watermark == publication.invalidation_watermark,
        ).values(state="invalidated" if receipt else "invalidation_pending",
                 invalidation_receipt=receipt,
                 applied_watermark=receipt["applied_watermark"] if receipt else publication.applied_watermark,
                 last_error=error, next_due_at=now if receipt else now + timedelta(seconds=wait),
                 lease_owner=None, lease_until=None, revision=Publication.revision + 1),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            await session.rollback()
            return False
        await session.commit()
        return True


async def repair_due_invalidations(sessions, client, *, now=None, limit=50):
    now = now or datetime.now(UTC)
    if not 1 <= limit <= 50:
        raise ValueError("repair limit must be between 1 and 50")
    async with sessions() as session:
        due = (await session.execute(select(Publication.tenant_id, Publication.publication_id).where(
            *_scope_conditions(client),
            Publication.state == "invalidation_pending", Publication.next_due_at <= now,
            or_(Publication.lease_until.is_(None), Publication.lease_until <= now),
        ).order_by(Publication.next_due_at, Publication.tenant_id, Publication.publication_id)
            .limit(limit))).all()
    completed = 0
    for tenant_id, publication_id in due:
        completed += bool(await repair_invalidation(sessions, client, tenant_id, publication_id, now=now))
    return completed
