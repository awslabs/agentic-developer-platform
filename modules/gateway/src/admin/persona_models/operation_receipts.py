"""Transactional model-policy replay receipts in the existing audit store."""

from __future__ import annotations

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.shared.models.audit import AuditLog

from .posture_service import PLATFORM_AUDIT_ORG


async def begin_operation(db, *, actor_id: str, operation_id: UUID | None, resource: str, request: dict):
    """Reserve a deterministic audit ID in the same transaction as the mutation.

    A concurrent duplicate INSERT waits for the first transaction; after conflict
    its committed receipt is returned. A failed mutation rolls back its receipt.
    This does not depend on a per-process mutex or a separate commit window.
    """
    if operation_id is None:
        return None, None
    key = str(uuid5(NAMESPACE_URL, f"adp:model-policy:{actor_id}:{operation_id}"))
    fingerprint = hashlib.sha256(json.dumps(dict(resource=resource, request=request), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = await db.get(AuditLog, key)
    if previous is not None:
        return None, _replay(previous, fingerprint)
    receipt = AuditLog(
        id=key,
        org_id=PLATFORM_AUDIT_ORG,
        event_type="persona_model_cli_operation",
        actor_id=actor_id,
        details={"operation_id": str(operation_id), "fingerprint": fingerprint, "resource": resource},
    )
    db.add(receipt)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        previous = await db.get(AuditLog, key)
        return None, _replay(previous, fingerprint)
    return receipt, None


def finish_operation(receipt, response: dict) -> None:
    if receipt is not None:
        receipt.details = {**receipt.details, "response": response}


def _replay(previous, fingerprint):
    if previous is None or not isinstance(previous.details, dict) or previous.details.get("fingerprint") != fingerprint:
        raise HTTPException(409, detail={"reason": "operation_id_conflict"})
    response = previous.details.get("response")
    if not isinstance(response, dict):
        raise HTTPException(409, detail={"reason": "operation_result_unavailable"})
    return response
