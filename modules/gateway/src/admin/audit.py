"""Admin mutation metadata and compatibility helpers.

Production routes stage success metadata in AuditedAdminRoute, which commits
an independent intent before effects and a terminal receipt before responding.
Direct callers outside that route context retain the legacy caller-owned SQL
transaction contract and must commit it themselves. That compatibility path is
not a durability guarantee. Existing persona/Bedrock writers remain separate.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.audit_operation import current_operation
from src.shared.models.audit import AuditLog
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)

# Event-type prefix: all admin-audit events share this so they can be queried
# separately from vault / credential / persona events that share the table.
_PREFIX = "admin_"


def _build_details(
    *,
    target_type: str,
    target_id: str,
    outcome: str,
    correlation_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical details dict.  Never includes secrets or full bodies."""
    d: dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id,
        "outcome": outcome,
    }
    if correlation_id:
        d["correlation_id"] = correlation_id
    if extra:
        d.update(extra)
    return d


async def write_admin_audit(
    db: AsyncSession,
    *,
    actor: TokenContext,
    action: str,
    target_type: str,
    target_id: str,
    org_id: str | None = None,
    outcome: str = "success",
    correlation_id: str | None = None,
    extra: dict[str, Any] | None = None,
    best_effort: bool = False,
) -> None:
    """Stage the route's terminal receipt, or flush a legacy caller-owned row.

    Within an audited route, best_effort cannot suppress persistence failures:
    the route owns durable admission and terminal commits. Outside it, callers
    must commit explicitly; best_effort retains legacy behavior only there.
    """
    operation = current_operation.get()
    if operation is not None:
        if actor.user_id != operation.actor.user_id or actor.org_id != operation.actor.org_id:
            raise RuntimeError("Audit actor differs from authenticated operation")
        if operation.terminal is not None:
            raise RuntimeError("Duplicate admin success audit")
        # The generic identity endpoints can operate across organizations. Their
        # actor's home organization is not the identity owner's organization.
        target_org = org_id or operation.target_org
        if target_org is None and target_type in {"github_app", "pool_account"}:
            target_org = "platform"
        if target_type == "identity" and (extra or {}).get("user_id"):
            from src.shared.models.organization import User

            user = await db.get(User, extra["user_id"])
            if user is None:
                raise RuntimeError("Audit target owner unavailable")
            target_org = user.org_id
        operation.terminal = {
            "event_type": f"{_PREFIX}{action}",
            "outcome": outcome,
            "target_type": target_type,
            "target_id": target_id,
            "org_id": target_org,
            "extra": {k: v for k, v in (extra or {}).items() if k in {"user_id", "provider", "github_org_id"}},
        }
        return
    try:
        db.add(
            AuditLog(
                org_id=org_id or actor.org_id,
                event_type=f"{_PREFIX}{action}",
                actor_id=actor.user_id,
                details=_build_details(
                    target_type=target_type,
                    target_id=target_id,
                    outcome=outcome,
                    correlation_id=correlation_id,
                    extra=extra,
                ),
            )
        )
        await db.flush()
    except Exception:  # noqa: BLE001
        if not best_effort:
            raise
        logger.exception(
            "Best-effort admin audit write failed (non-fatal)",
            extra={"action": action, "actor": actor.user_id},
        )


async def write_admin_audit_on_refusal(
    db: AsyncSession,
    *,
    actor: TokenContext,
    action: str,
    target_type: str,
    target_id: str,
    org_id: str | None = None,
    reason: str = "",
    correlation_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a refused mutation on its own transaction, swallowing failures.

    A refusal writes no data row, so there is no caller transaction to ride.
    Committing here makes the refusal durable.  Swallowing failures ensures the
    operator always sees the actionable 403/409/422 even when the audit sink is
    down — losing the audit row is bad; replacing the rejection with a 500 is
    worse.
    """
    details = _build_details(
        target_type=target_type,
        target_id=target_id,
        outcome="denied",
        correlation_id=correlation_id,
        extra={**(extra or {}), "reason": reason} if reason else extra,
    )
    try:
        db.add(
            AuditLog(
                org_id=org_id or actor.org_id,
                event_type=f"{_PREFIX}{action}",
                actor_id=actor.user_id,
                details=details,
            )
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — the refusal must survive an audit-write failure
        logger.exception(
            "Could not record admin audit refusal",
            extra={"action": action, "actor": actor.user_id},
        )
        await db.rollback()
