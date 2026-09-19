"""Audited versioned runtime-posture mutation (PMM-07).

Canonical design §4.2 and §9 require the enforcement posture to be changed only
by a platform administrator, through an operation that is versioned, audited and
durable — and require operational rollback to be that same audited operation
rather than an ad-hoc edit.  The settings table existing is not that operation:
before this module there was no supported way to change the posture at all, so
the rollback §9 depends on had no implementation.

Three properties are load-bearing here:

* **Compare-and-set on a monotonic revision.** A caller states the revision it
  believes is current.  A stale expectation is refused rather than applied, so
  two administrators cannot silently overwrite each other and an operator
  rolling back cannot be racing an unseen forward change.
* **Mutation and audit share one transaction.** The audit row is flushed, not
  committed, alongside the update; either both land or neither does.  A change
  that cannot be audited does not happen.
* **A pending change is never visible as a live decision.** The session is
  marked while the write is uncommitted so the bounded posture cache cannot
  promote a value that may still roll back.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.persona_models.catalogue import COMPATIBILITY_CLASSES
from src.agentauth.runtime_posture import (
    RuntimePosture,
    RuntimePostureError,
    clear_session_uncommitted,
    coerce_posture,
    coerce_posture_revision,
    mark_session_uncommitted,
    reset_posture_cache,
)
from src.shared.models.audit import AuditLog
from src.shared.models.base import utcnow
from src.shared.models.persona_models import PersonaModelPolicySetting

logger = logging.getLogger("bedrockgateway.admin.persona_models.posture")

#: ``AuditLog.org_id`` is non-nullable, but the policy-settings row is
#: deliberately cross-tenant (no ``TenantMixin``).  Reuse the established
#: platform sentinel rather than inventing a second one or writing "".
PLATFORM_AUDIT_ORG = "__platform__"

POSTURE_CHANGED_EVENT = "persona_model_posture_changed"
POSTURE_REJECTED_EVENT = "persona_model_posture_rejected"


class PostureMutationError(Exception):
    """An audited posture change was refused.  Carries a stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class PostureConflictError(PostureMutationError):
    """The caller's expected revision was not the current one."""

    def __init__(self, *, current_posture: str, current_revision: int) -> None:
        super().__init__(
            "posture_revision_conflict",
            "The runtime posture changed since it was read; re-read and retry.",
        )
        self.current_posture = current_posture
        self.current_revision = current_revision


async def get_posture_setting(
    db: AsyncSession,
    *,
    compatibility_class: str,
) -> PersonaModelPolicySetting:
    """Load one policy-settings row, refusing unknown or unprovisioned classes."""
    if compatibility_class not in COMPATIBILITY_CLASSES:
        raise PostureMutationError(
            "compatibility_class_unknown",
            "Unknown compatibility class.",
        )
    row = await db.scalar(
        select(PersonaModelPolicySetting).where(
            PersonaModelPolicySetting.compatibility_class == compatibility_class,
        )
    )
    if row is None:
        # Creating the row here would let a posture change invent a platform
        # class that migrations never provisioned.  That is a readiness failure.
        raise PostureMutationError(
            "runtime_posture_unavailable",
            "No policy settings row exists for this compatibility class.",
        )
    return row


async def set_runtime_posture(
    db: AsyncSession,
    *,
    compatibility_class: str,
    posture: str,
    expected_revision: int,
    actor_id: str,
    actor_kind: str = "platform_admin",
    reason: str | None = None,
) -> PersonaModelPolicySetting:
    """Change the posture for one compatibility class, atomically and audited.

    The caller is responsible for the platform-admin authorization gate and for
    committing.  This function flushes the update together with its audit row so
    the two cannot diverge, and leaves the session marked as holding uncommitted
    posture state until the caller commits.

    Raises:
        PostureMutationError: unknown class, unprovisioned row, unsupported
            posture or malformed expected revision.
        PostureConflictError: the expected revision is not current.
    """
    try:
        target_posture: RuntimePosture = coerce_posture(posture)
    except RuntimePostureError as exc:
        raise PostureMutationError(exc.reason, "Unsupported runtime posture.") from None
    try:
        expected = coerce_posture_revision(expected_revision)
    except RuntimePostureError as exc:
        raise PostureMutationError(exc.reason, "Malformed expected posture revision.") from None

    row = await get_posture_setting(db, compatibility_class=compatibility_class)
    before_posture = row.enforcement_posture
    before_revision = row.posture_revision

    if before_revision != expected:
        raise PostureConflictError(current_posture=before_posture, current_revision=before_revision)

    # A bare UPDATE statement leaves no dirty ORM object, so the posture cache's
    # automatic detection would not see this write.  Marking is mandatory, not
    # belt-and-braces, for the compare-and-set path.
    mark_session_uncommitted(db)

    now = utcnow()
    result = await db.execute(
        update(PersonaModelPolicySetting)
        .where(
            PersonaModelPolicySetting.compatibility_class == compatibility_class,
            PersonaModelPolicySetting.posture_revision == expected,
        )
        .values(
            enforcement_posture=target_posture,
            posture_revision=expected + 1,
            updated_by=actor_id,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        # Another administrator moved the revision between our read and this
        # UPDATE.  The database, not this process, decides who won.
        await db.refresh(row)
        raise PostureConflictError(
            current_posture=row.enforcement_posture,
            current_revision=row.posture_revision,
        )

    db.add(
        AuditLog(
            org_id=PLATFORM_AUDIT_ORG,
            event_type=POSTURE_CHANGED_EVENT,
            actor_id=actor_id,
            details={
                "compatibility_class": compatibility_class,
                "before_posture": before_posture,
                "after_posture": target_posture,
                "before_posture_revision": before_revision,
                "after_posture_revision": expected + 1,
                "actor_kind": actor_kind,
                "subject_key": compatibility_class,
                "change_reason": reason,
            },
        )
    )
    # One flush: the update and its audit row share the caller's transaction.
    await db.flush()
    await db.refresh(row)
    logger.info(
        "Runtime posture change staged",
        extra={
            "compatibility_class": compatibility_class,
            "before_posture": before_posture,
            "after_posture": target_posture,
            "after_posture_revision": expected + 1,
        },
    )
    return row


def finalize_posture_commit(db: AsyncSession) -> None:
    """Clear the uncommitted marker after the caller has committed.

    ``reset_posture_cache()`` is a single-process convenience only: other
    gateway instances never observe it, so the rollback guarantee still rests on
    the bounded time-based expiry in :mod:`src.agentauth.runtime_posture`.
    """
    clear_session_uncommitted(db)
    reset_posture_cache()


async def write_posture_refusal_audit(
    db: AsyncSession,
    *,
    compatibility_class: str,
    requested_posture: str,
    reason: str,
    actor_id: str | None,
    actor_kind: str = "platform_admin",
) -> None:
    """Record a refused posture change on its own transaction.

    A refusal writes no settings row, so there is no caller transaction to ride;
    committing here is what makes the refusal an actual audit record.
    """
    try:
        db.add(
            AuditLog(
                org_id=PLATFORM_AUDIT_ORG,
                event_type=POSTURE_REJECTED_EVENT,
                actor_id=actor_id,
                details={
                    "compatibility_class": compatibility_class,
                    "requested_posture": requested_posture,
                    "reason": reason,
                    "actor_kind": actor_kind,
                    "subject_key": compatibility_class,
                },
            )
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not record runtime-posture refusal audit",
            extra={"compatibility_class": compatibility_class, "reason": reason},
        )
        await db.rollback()
