"""Explicit, audited human grants with live workspace and membership checks."""

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_auth.policy import Permission, expand_permissions

from app.current_identity import IdentityUnavailable, require_current_identity
from app.models.event import Event
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.models.workspace_grant_change import WorkspaceGrantChange
from app.schemas.workspace_access import GrantHumanAccessRequest, WorkspaceAccessResponse


def _effective(row: WorkspaceGrantRecord) -> list[Permission]:
    known = set()
    for value in row.permission_values():
        try:
            known.add(Permission(value))
        except ValueError:
            pass
    return sorted(expand_permissions(known), key=str)


def _response(row: WorkspaceGrantRecord, event: Event | None = None) -> WorkspaceAccessResponse:
    details = json.loads(event.details_json) if event and event.details_json else {}
    return WorkspaceAccessResponse(
        workspace_id=row.workspace_id, grant_id=row.id, revision=row.revision,
        principal_type="human", subject=row.principal,
        effective_permissions=_effective(row),
        source="explicit_assignment" if event else "preexisting_grant",
        granted_by=event.principal if event else None,
        reason=details.get("reason"),
        request_id=uuid.UUID(details["request_id"]) if details.get("request_id") else None,
    )


async def _require_current_pair(reader, caller, target_subject: str) -> None:
    try:
        await require_current_identity(
            reader, subject=caller.principal.subject, principal_type="human",
            adp_org_id=caller.source_org_id, membership_id=caller.identity_evidence,
        )
        await require_current_identity(
            reader, subject=target_subject, principal_type="human",
            adp_org_id=caller.source_org_id,
        )
    except IdentityUnavailable:
        raise HTTPException(403, "current human membership required") from None


async def read_my_access(db: AsyncSession, workspace_id: uuid.UUID, caller, reader) -> WorkspaceAccessResponse:
    if caller.principal.account_type != "human" or not caller.source_org_id:
        raise HTTPException(403, "human workspace identity required")
    if reader is None:
        raise HTTPException(503, "current ADP identity reader unavailable")
    try:
        await require_current_identity(
            reader, subject=caller.principal.subject, principal_type="human",
            adp_org_id=caller.source_org_id, membership_id=caller.identity_evidence,
        )
    except IdentityUnavailable:
        raise HTTPException(403, "current human membership required") from None
    row = await db.scalar(select(WorkspaceGrantRecord).where(
        WorkspaceGrantRecord.workspace_id == workspace_id,
        WorkspaceGrantRecord.org_id == uuid.UUID(caller.principal.org_id),
        WorkspaceGrantRecord.principal == caller.principal.subject,
        WorkspaceGrantRecord.principal_type == "human",
        WorkspaceGrantRecord.revoked_at.is_(None),
    ))
    if row is None:
        raise HTTPException(403, "active workspace grant required")
    change = await db.scalar(select(WorkspaceGrantChange).where(
        WorkspaceGrantChange.grant_id == row.id,
        WorkspaceGrantChange.revision == row.revision,
    ))
    event = await db.get(Event, change.event_id) if change else None
    return _response(row, event)


async def grant_human_access(
    db: AsyncSession, workspace_id: uuid.UUID, caller, body: GrantHumanAccessRequest, reader,
) -> WorkspaceAccessResponse:
    if caller.principal.account_type != "human" or not caller.source_org_id:
        raise HTTPException(403, "current human ADP organization binding required")
    if reader is None:
        raise HTTPException(503, "current ADP identity reader unavailable")
    if body.target_subject == caller.principal.subject:
        raise HTTPException(403, "self-assignment is not supported")
    requested = set(body.permissions)
    if len(requested) != len(body.permissions):
        raise HTTPException(422, "duplicate workspace permission")
    org_id = uuid.UUID(caller.principal.org_id)
    workspace = await db.scalar(select(Workspace).where(
        Workspace.id == workspace_id, Workspace.org_id == org_id,
    ).with_for_update())
    if workspace is None or workspace.status in ("Teardown", "Deleted"):
        raise HTTPException(403, "active workspace binding required")
    await _require_current_pair(reader, caller, body.target_subject)
    actor = await db.scalar(select(WorkspaceGrantRecord).where(
        WorkspaceGrantRecord.workspace_id == workspace_id,
        WorkspaceGrantRecord.org_id == org_id,
        WorkspaceGrantRecord.principal == caller.principal.subject,
        WorkspaceGrantRecord.principal_type == "human",
        WorkspaceGrantRecord.revoked_at.is_(None),
    ).with_for_update())
    if actor is None or Permission.ADMINISTER not in _effective(actor):
        raise HTTPException(403, "explicit live workspace administrator grant required")
    if not expand_permissions(requested).issubset(set(_effective(actor))):
        raise HTTPException(403, "requested permissions exceed administrator ceiling")
    target = await db.scalar(select(WorkspaceGrantRecord).where(
        WorkspaceGrantRecord.workspace_id == workspace_id,
        WorkspaceGrantRecord.principal == body.target_subject,
    ).with_for_update())
    if target is not None and (target.revoked_at is not None or target.org_id != org_id
                               or target.principal_type != "human"):
        raise HTTPException(409, "grant cannot be restored or substituted")
    fingerprint = hashlib.sha256(json.dumps({
        "actor": caller.principal.subject, "org_id": str(org_id),
        "request": body.model_dump(mode="json", exclude={"request_id"}),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = await db.scalar(select(WorkspaceGrantChange).where(
        WorkspaceGrantChange.workspace_id == workspace_id,
        WorkspaceGrantChange.request_id == body.request_id,
    ))
    if previous is not None:
        if (previous.fingerprint != fingerprint or target is None
                or target.id != previous.grant_id or target.revision != previous.revision):
            raise HTTPException(409, "request identity conflicts with current grant")
        return _response(target, await db.get(Event, previous.event_id))
    revision = target.revision if target else 0
    if body.expected_revision != revision:
        raise HTTPException(409, "stale workspace grant revision")
    await _require_current_pair(reader, caller, body.target_subject)
    before = [str(permission) for permission in _effective(target)] if target else []
    if target is None:
        target = WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=org_id, principal=body.target_subject,
            principal_type="human", permissions=" ".join(sorted(requested)), revision=1,
        )
        db.add(target)
    else:
        target.permissions = " ".join(sorted(requested))
        target.revision += 1
    await db.flush()
    event = Event(
        org_id=org_id, principal=caller.principal.subject, outcome="allowed",
        action="assigned", resource_type="workspace_grant", resource_id=target.id,
        event_type="workspace_access", details_json=json.dumps({
            "actor_type": "human", "target": body.target_subject, "target_type": "human",
            "workspace_id": str(workspace_id), "org_id": str(org_id),
            "before": before, "after": [str(permission) for permission in _effective(target)],
            "reason": body.reason, "request_id": str(body.request_id),
            "revision": target.revision,
        }, sort_keys=True),
    )
    db.add(event)
    await db.flush()
    db.add(WorkspaceGrantChange(
        workspace_id=workspace_id, request_id=body.request_id, grant_id=target.id,
        event_id=event.id, fingerprint=fingerprint, revision=target.revision,
    ))
    await db.commit()
    return _response(target, event)
