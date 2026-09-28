"""Resolve a verified ADP organization through explicit server-held bindings."""

import uuid
from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy import select

from app.models.organization import Organization


async def bind_caller(db, caller):
    source = caller.principal.org_id
    organization = await db.scalar(
        select(Organization).where(Organization.adp_org_id == source)
    )
    if organization is not None:
        return replace(
            caller,
            principal=replace(caller.principal, org_id=str(organization.id)),
            source_org_id=source,
        )
    # Retain the pre-U23 UUID behavior only for an unbound legacy organization.
    # A UUID-shaped ADP claim cannot bypass a different explicit binding.
    try:
        identifier = uuid.UUID(source)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            403, "ADP organization has no reviewed domain binding"
        ) from None
    organization = await db.get(Organization, identifier)
    if organization is not None and organization.adp_org_id not in (None, source):
        raise HTTPException(403, "ADP organization does not match the domain binding")
    return caller
