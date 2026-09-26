"""Optimistic routing revisions over the canonical records; no second policy store."""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import text


async def serialize_writes(db):
    """All routing writers share a transaction lock, including legacy browser paths.

    Held through probing, admin-pin checks and commit. It also covers an absent
    mapping, for which SELECT FOR UPDATE alone would leave a race.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 56330001})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def mapping_revision(mapping):
    if mapping is None:
        return "absent"
    return digest(
        {
            key: getattr(mapping, key, None)
            for key in (
                "id",
                "scope_type",
                "scope_id_org",
                "scope_id_team",
                "scope_id_user",
                "destination_id",
                "authored_by_user_id",
                "updated_at",
            )
        }
    )


def destination_revision(destination):
    return digest(
        {
            key: getattr(destination, key, None)
            for key in (
                "id",
                "account_id",
                "role_arn",
                "credential_id",
                "owner_org_id",
                "is_platform_registered",
                "routing_capable",
                "verified_at",
                "region",
                "updated_at",
            )
        }
    )


def require_revision(expected, actual):
    if expected is not None and expected != actual:
        raise HTTPException(409, detail={"reason": "stale_revision", "message": "Routing changed; read and review the current state."})
