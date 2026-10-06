"""Current-human organization access without role or workspace inheritance."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.current_identity import IdentityUnavailable, require_current_identity
from app.models.event import Event
from app.models.organization import Organization
from app.models.organization_grant import ORGANIZATION_ADMINISTER, ORGANIZATION_READ, OrganizationGrantRecord
from app.models.organization_grant_change import OrganizationGrantChange
from app.schemas.organization_access import (
    AssignOrganizationAccessRequest, RevokeOrganizationAccessRequest,
    OrganizationAccessResponse, OrganizationAssignmentResponse, OrganizationAssignmentsResponse,
)


def _permissions(grant: OrganizationGrantRecord) -> set[str]:
    return grant.permission_values() & {ORGANIZATION_READ, ORGANIZATION_ADMINISTER}


def _assignment(grant: OrganizationGrantRecord, change=None, event=None) -> OrganizationAssignmentResponse:
    reason = {"assigned": "access_assignment", "revoked": "access_revocation"}.get(event.action) if event else None
    return OrganizationAssignmentResponse(
        organization_id=grant.org_id, grant_id=grant.id, revision=grant.revision, principal_type=grant.principal_type,
        subject=grant.principal, assigned_permissions=sorted(_permissions(grant)),
        revoked_at=grant.revoked_at, granted_by=grant.granted_by, granted_at=grant.created_at,
        source=("explicit_revocation" if event.action == "revoked" else "explicit_assignment") if reason else "stored_organization_grant",
        changed_by=event.principal if reason else None, changed_at=event.created_at if reason else None,
        reason=reason, request_id=change.request_id if reason else None,
    )


def _effective(grant: OrganizationGrantRecord) -> list[str]:
    permissions = _permissions(grant) if grant.revoked_at is None else set()
    if ORGANIZATION_ADMINISTER in permissions:
        permissions.add(ORGANIZATION_READ)
    return sorted(permissions)


def _response(grant: OrganizationGrantRecord, change=None, event=None) -> OrganizationAccessResponse:
    return OrganizationAccessResponse(
        **_assignment(grant, change, event).model_dump(), effective_permissions=_effective(grant),
        revocation_effect="future_authority_only" if grant.revoked_at else None,
    )


async def _evidence(db: AsyncSession, grants: list[OrganizationGrantRecord]) -> dict:
    if not grants:
        return {}
    rows = await db.execute(select(OrganizationGrantChange, Event).join(
        Event, Event.id == OrganizationGrantChange.event_id,
    ).where(
        OrganizationGrantChange.org_id == grants[0].org_id,
        tuple_(OrganizationGrantChange.grant_id, OrganizationGrantChange.revision).in_(
            [(grant.id, grant.revision) for grant in grants],
        ),
        Event.org_id == OrganizationGrantChange.org_id,
        Event.resource_id == OrganizationGrantChange.grant_id,
        Event.resource_type == "organization_grant", Event.event_type == "organization_access",
        Event.action.in_(["assigned", "revoked"]),
    ))
    return {change.grant_id: (change, event) for change, event in rows}


async def _authorized_grant(db: AsyncSession, caller, reader, permission: str, *, write=False) -> OrganizationGrantRecord:
    if caller.principal.account_type != "human" or not caller.source_org_id:
        raise HTTPException(403, "current human ADP organization binding required")
    if reader is None:
        raise HTTPException(503, "current ADP identity reader unavailable")
    try:
        await require_current_identity(
            reader, subject=caller.principal.subject, principal_type="human",
            adp_org_id=caller.source_org_id, membership_id=caller.identity_evidence,
        )
    except IdentityUnavailable:
        raise HTTPException(403, "current human membership required") from None
    org_id = uuid.UUID(caller.principal.org_id)
    organization = await db.scalar(select(Organization).where(
        Organization.id == org_id, Organization.adp_org_id == caller.source_org_id,
    ))
    if organization is None:
        raise HTTPException(403, "current ADP organization binding required")
    grant = await db.scalar(select(OrganizationGrantRecord).where(
        OrganizationGrantRecord.org_id == org_id,
        OrganizationGrantRecord.principal == caller.principal.subject,
        OrganizationGrantRecord.principal_type == "human",
        OrganizationGrantRecord.revoked_at.is_(None),
    ).with_for_update(read=not write).execution_options(populate_existing=True))
    if grant is None or not (_permissions(grant) & {permission, ORGANIZATION_ADMINISTER}):
        raise HTTPException(403, "explicit live organization grant required")
    return grant


async def read_my_organization_access(db: AsyncSession, caller, reader) -> OrganizationAccessResponse:
    grant = await _authorized_grant(db, caller, reader, ORGANIZATION_READ)
    evidence = await _evidence(db, [grant])
    return _response(grant, *evidence.get(grant.id, (None, None)))


async def list_organization_assignments(
    db: AsyncSession, caller, reader, *, limit: int = 50, after: uuid.UUID | None = None,
) -> OrganizationAssignmentsResponse:
    actor = await _authorized_grant(db, caller, reader, ORGANIZATION_ADMINISTER)
    query = select(OrganizationGrantRecord).where(
        OrganizationGrantRecord.org_id == actor.org_id,
    ).order_by(OrganizationGrantRecord.id)
    if after is not None:
        query = query.where(OrganizationGrantRecord.id > after)
    rows = list((await db.scalars(query.limit(limit + 1))).all())
    page = rows[:limit]
    evidence = await _evidence(db, page)
    return OrganizationAssignmentsResponse(
        organization_id=actor.org_id, assignments=[_assignment(grant, *evidence.get(grant.id, (None, None))) for grant in page],
        next_after=page[-1].id if len(rows) > limit else None,
    )


async def _require_current_pair(reader, caller, target_subject: str) -> None:
    try:
        await require_current_identity(reader, subject=caller.principal.subject, principal_type="human",
                                       adp_org_id=caller.source_org_id, membership_id=caller.identity_evidence)
        await require_current_identity(reader, subject=target_subject, principal_type="human", adp_org_id=caller.source_org_id)
    except IdentityUnavailable:
        raise HTTPException(403, "current human membership required") from None


async def mutate_organization_access(
    db: AsyncSession, caller, reader,
    body: AssignOrganizationAccessRequest | RevokeOrganizationAccessRequest,
    *, grant_id: uuid.UUID | None = None,
) -> OrganizationAccessResponse:
    revoking = isinstance(body, RevokeOrganizationAccessRequest)
    if caller.principal.account_type != "human" or not caller.source_org_id:
        raise HTTPException(403, "current human ADP organization binding required")
    if reader is None:
        raise HTTPException(503, "current ADP identity reader unavailable")
    if body.target_subject == caller.principal.subject:
        if revoking:
            raise HTTPException(409, "self-revocation is gated pending last-administrator and recovery policy")
        raise HTTPException(403, "self-assignment is not supported")
    org_id = uuid.UUID(caller.principal.org_id)
    organization = await db.scalar(select(Organization).where(
        Organization.id == org_id, Organization.adp_org_id == caller.source_org_id,
    ).with_for_update().execution_options(populate_existing=True))
    if organization is None:
        raise HTTPException(403, "current ADP organization binding required")
    await _require_current_pair(reader, caller, body.target_subject)
    actor = await _authorized_grant(db, caller, reader, ORGANIZATION_ADMINISTER, write=True)
    requested = set() if revoking else set(body.permissions)
    if not requested.issubset(set(_effective(actor))):
        raise HTTPException(403, "requested permissions exceed administrator ceiling")
    target = await db.scalar(select(OrganizationGrantRecord).where(
        OrganizationGrantRecord.org_id == org_id, OrganizationGrantRecord.principal == body.target_subject,
    ).with_for_update().execution_options(populate_existing=True))
    if revoking and (target is None or target.id != grant_id or target.principal_type != "human"):
        raise HTTPException(404, "organization grant not found for target identity")
    if target is not None and (target.principal_type != "human" or (target.revoked_at and not revoking)):
        raise HTTPException(409, "grant cannot be restored or substituted")
    fingerprint = hashlib.sha256(json.dumps({
        "operation": "revoke" if revoking else "assign", "actor": caller.principal.subject,
        "org_id": str(org_id), "grant_id": str(grant_id) if grant_id else None,
        "request": body.model_dump(mode="json", exclude={"request_id"}),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = await db.scalar(select(OrganizationGrantChange).where(
        OrganizationGrantChange.org_id == org_id, OrganizationGrantChange.request_id == body.request_id,
    ))
    if previous is not None:
        if (previous.fingerprint != fingerprint or target is None or previous.grant_id != target.id
                or previous.revision != target.revision or bool(target.revoked_at) != revoking):
            raise HTTPException(409, "request identity conflicts with current grant")
        evidence = await _evidence(db, [target])
        if target.id not in evidence:
            raise HTTPException(409, "organization grant audit evidence unavailable")
        return _response(target, *evidence[target.id])
    if (target is not None and target.revoked_at) or body.expected_revision != (target.revision if target else 0):
        raise HTTPException(409, "stale or revoked organization grant")
    await _require_current_pair(reader, caller, body.target_subject)
    before = _effective(target) if target else []
    if target is None:
        target = OrganizationGrantRecord(org_id=org_id, principal=body.target_subject, principal_type="human",
                                         permissions=" ".join(sorted(requested)), granted_by=caller.principal.subject, revision=1)
        db.add(target)
    else:
        target.revision += 1
        if revoking:
            target.revoked_at = datetime.now(UTC)
        else:
            target.permissions = " ".join(sorted(requested))
    await db.flush()
    event = Event(
        org_id=org_id, principal=caller.principal.subject, outcome="allowed",
        action="revoked" if revoking else "assigned", resource_type="organization_grant", resource_id=target.id,
        request_path=f"/orgs/current/access/v1/grants/{target.id}/revoke" if revoking else "/orgs/current/access/v1/grants",
        event_type="organization_access", details_json=json.dumps({
            "actor_type": "human", "target": body.target_subject, "target_type": "human",
            "scope": "organization", "org_id": str(org_id), "before": before, "after": _effective(target),
            "reason": body.reason, "request_id": str(body.request_id), "before_revision": body.expected_revision,
            "revision": target.revision, "revoked_at": target.revoked_at.isoformat() if target.revoked_at else None,
            "revocation_effect": "future_authority_only" if revoking else None,
        }, sort_keys=True),
    )
    db.add(event)
    await db.flush()
    change = OrganizationGrantChange(org_id=org_id, request_id=body.request_id, grant_id=target.id,
                                     event_id=event.id, fingerprint=fingerprint, revision=target.revision)
    db.add(change)
    await db.commit()
    return _response(target, change, event)
