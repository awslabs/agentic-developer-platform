"""Live versioned runtime posture for agent model policy (PMM-07).

The trusted root snapshot freezes *which* model a principal selected.  It must
never freeze *whether the platform is currently enforcing that selection*: the
posture is the audited operational control used to roll enforcement back, so a
chain that inherited ``enforcing`` at its root must not keep enforcing after an
operator has reverted the setting.  Canonical design §9 requires the rollback to
take effect within a measured, bounded window.

This module is therefore the single read path for the posture, and it is read
per hop rather than taken from the snapshot.  Staleness is bounded purely by
elapsed time so the guarantee holds across separate gateway instances and
processes; there is no reliance on a local invalidation call that a second
replica would never observe.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.pool import SingletonThreadPool, StaticPool

from src.shared.models.persona_models import PersonaModelPolicySetting

logger = logging.getLogger("bedrockgateway.agentauth.runtime_posture")

RuntimePosture = Literal["disabled", "report_only", "enforcing"]

#: The closed vocabulary.  Anything else is unknown and must fail closed rather
#: than be guessed at or relabelled as the permissive value.
RUNTIME_POSTURES: tuple[RuntimePosture, ...] = ("disabled", "report_only", "enforcing")

POSTURE_CACHE_TTL_ENV = "AGENT_MODEL_POSTURE_CACHE_TTL_SECONDS"
DEFAULT_POSTURE_CACHE_TTL_SECONDS = 30
#: Hard ceiling on how long a rolled-back posture can still be observed by any
#: gateway instance.  Operational rollback waits this long and then verifies.
MAX_POSTURE_CACHE_TTL_SECONDS = 60

#: Session ``info`` key marking that this session holds uncommitted writes to
#: the posture setting.  The audited mutation service sets it so that a value
#: which may still roll back can never be promoted into the shared cache.
UNCOMMITTED_SESSION_FLAG = "persona_model_posture_uncommitted"


class RuntimePostureError(Exception):
    """The live posture could not be read, or is not a supported value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LivePosture:
    """One posture observation, with the bound on how stale it may be."""

    compatibility_class: str
    posture: RuntimePosture
    posture_revision: int
    observed_at: datetime
    expires_at: datetime
    source: Literal["live", "cache"]

    @property
    def enforcing(self) -> bool:
        return self.posture == "enforcing"

    def to_evidence(self) -> dict[str, object]:
        return {
            "compatibility_class": self.compatibility_class,
            "runtime_posture": self.posture,
            "posture_revision": self.posture_revision,
            "posture_observed_at": self.observed_at.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "posture_source": self.source,
        }


def measured_cache_ttl_seconds() -> int:
    """The configured bound, clamped.  Operational rollback waits this long."""
    try:
        configured = int(os.environ.get(POSTURE_CACHE_TTL_ENV, DEFAULT_POSTURE_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        configured = DEFAULT_POSTURE_CACHE_TTL_SECONDS
    return max(0, min(configured, MAX_POSTURE_CACHE_TTL_SECONDS))


# Process-wide cache of committed observations only: class -> LivePosture.
_CACHE: dict[str, LivePosture] = {}


def reset_posture_cache() -> None:
    """Drop cached observations.

    A convenience for tests and for a single process after an audited change.
    It is deliberately *not* the mechanism the rollback guarantee rests on:
    other instances never see this call, so correctness comes from the
    time-based expiry above.
    """
    _CACHE.clear()


def mark_session_uncommitted(session: AsyncSession) -> None:
    """Record that this session holds a posture write that may still roll back."""
    session.info[UNCOMMITTED_SESSION_FLAG] = True


def clear_session_uncommitted(session: AsyncSession) -> None:
    """Record that this session's posture writes are durably committed."""
    session.info.pop(UNCOMMITTED_SESSION_FLAG, None)


def _flag_pending_posture_writes(sync_session: SyncSession, _flush_context, _instances) -> None:
    """Latch the uncommitted marker at flush time.

    ``flush()`` moves objects out of ``session.dirty``, so inspecting those
    collections after a flush would wrongly report a clean session.  This
    listener records the fact durably in ``session.info`` instead.

    This is one of several detectors, not the guarantee: it does not see
    arbitrary Core/bulk UPDATE statements, so ``mark_session_uncommitted`` is
    mandatory on the mutation path and the cache-write decision below rests on
    a transaction-independent read rather than on this flag.
    """
    tracked = list(sync_session.new) + list(sync_session.dirty) + list(sync_session.deleted)
    if any(isinstance(obj, PersonaModelPolicySetting) for obj in tracked):
        sync_session.info[UNCOMMITTED_SESSION_FLAG] = True


def _flag_core_posture_statements(sync_session: SyncSession, statement, *_args, **_kwargs) -> None:
    """Catch bulk/Core UPDATE and DELETE against the settings table.

    ``before_flush`` never fires for ``session.execute(update(...))``, which is
    exactly the shape the audited compare-and-set uses.
    """
    table = getattr(statement, "table", None)
    if table is not None and table.name == PersonaModelPolicySetting.__tablename__ and not statement.is_select:
        sync_session.info[UNCOMMITTED_SESSION_FLAG] = True


def _clear_pending_posture_writes(sync_session: SyncSession) -> None:
    sync_session.info.pop(UNCOMMITTED_SESSION_FLAG, None)


def _clear_on_real_commit(sync_session: SyncSession) -> None:
    """Clear the marker only for a genuine outer commit.

    Releasing a nested savepoint also fires ``after_commit`` in some code
    paths; treating that as a commit would clear the marker while the outer
    transaction is still open and let a value that can still roll back be
    published.  Only clear when no transaction remains active.
    """
    transaction = sync_session.get_transaction()
    if transaction is None or not transaction.is_active:
        _clear_pending_posture_writes(sync_session)


event.listen(SyncSession, "before_flush", _flag_pending_posture_writes)
event.listen(SyncSession, "do_orm_execute", lambda state: _flag_core_posture_statements(state.session, state.statement))
event.listen(SyncSession, "after_commit", _clear_on_real_commit)
event.listen(SyncSession, "after_soft_rollback", lambda s, _ctx: _clear_pending_posture_writes(s))


def _has_uncommitted_posture_writes(session: AsyncSession) -> bool:
    if session.info.get(UNCOMMITTED_SESSION_FLAG):
        return True
    sync_session = getattr(session, "sync_session", None)
    if sync_session is not None and sync_session.info.get(UNCOMMITTED_SESSION_FLAG):
        return True
    tracked = list(session.new) + list(session.dirty) + list(session.deleted)
    return any(isinstance(obj, PersonaModelPolicySetting) for obj in tracked)


def coerce_posture(value: object) -> RuntimePosture:
    """Validate one posture string against the closed vocabulary."""
    if not isinstance(value, str) or value not in RUNTIME_POSTURES:
        raise RuntimePostureError("runtime_posture_unsupported")
    return value  # type: ignore[return-value]


def coerce_posture_revision(value: object) -> int:
    """Validate a monotonic posture revision.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, and accepting
    ``True`` as revision 1 would let a malformed payload look well-formed.
    """
    if type(value) is not int or value < 1:
        raise RuntimePostureError("posture_revision_unsupported")
    return value


async def read_live_posture(
    session: AsyncSession,
    *,
    compatibility_class: str,
    now: datetime | None = None,
) -> LivePosture:
    """Read the current posture for one compatibility class.

    Raises :class:`RuntimePostureError` when the row is missing or carries an
    unknown posture or a malformed revision.  Callers decide what that means
    for their posture; this function never substitutes a permissive default.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not isinstance(compatibility_class, str) or not compatibility_class:
        raise RuntimePostureError("compatibility_class_unknown")

    cached = _CACHE.get(compatibility_class)
    if cached is not None:
        if current < cached.expires_at:
            return LivePosture(
                compatibility_class=cached.compatibility_class,
                posture=cached.posture,
                posture_revision=cached.posture_revision,
                observed_at=cached.observed_at,
                expires_at=cached.expires_at,
                source="cache",
            )
        # Expired observations are dropped so a later read cannot resurrect a
        # posture the operator has already rolled back.
        _CACHE.pop(compatibility_class, None)

    independent = await _read_independent_committed_posture(session, compatibility_class=compatibility_class)
    if independent is not None:
        # Proven committed: read on a connection outside the caller's
        # transaction, so neither a retained identity-map row nor the caller's
        # own pending write can be mistaken for live platform state.
        posture, revision = independent
        cacheable = True
    else:
        # No independent connection is available (a session bound to a single
        # shared connection, as some harnesses use).  Report what the caller's
        # transaction sees, re-reading attributes rather than trusting retained
        # ones, and refuse to publish it if this session holds a posture write
        # that may still roll back.
        visible = await _read_session_visible_posture(session, compatibility_class=compatibility_class)
        if visible is None:
            raise RuntimePostureError("runtime_posture_unavailable")
        posture, revision = visible
        cacheable = not _has_uncommitted_posture_writes(session)

    ttl = measured_cache_ttl_seconds()
    observation = LivePosture(
        compatibility_class=compatibility_class,
        posture=posture,
        posture_revision=revision,
        observed_at=current,
        expires_at=current + timedelta(seconds=ttl if cacheable else 0),
        source="live",
    )
    # A failed or reverted audited change must never become a live decision, and
    # an expired entry must be replaced by genuinely fresh committed state
    # rather than by a value carried over from before the rollback.
    if ttl > 0 and cacheable:
        _CACHE[compatibility_class] = observation
    return observation


def _independent_connection_source(session: AsyncSession) -> AsyncEngine | None:
    """The session's engine, when a second connection from it is truly separate.

    ``session.get_bind()`` hands back the *synchronous* engine facade, which
    cannot be used with ``async with``; the ``AsyncEngine`` is on ``session.bind``
    when the session came from an ``async_sessionmaker``.

    Pools that hand out one shared connection (``StaticPool``,
    ``SingletonThreadPool`` — the in-memory SQLite shapes) are rejected: a
    "second" connection there is the *same* connection and the same transaction,
    so reading on it would see the caller's uncommitted writes while claiming
    they were committed.  That is the failure this function exists to avoid, so
    it must not be papered over by a pool that silently aliases connections.
    """
    engine = getattr(session, "bind", None)
    if not isinstance(engine, AsyncEngine):
        return None
    sync_engine = getattr(engine, "sync_engine", None)
    if sync_engine is None or isinstance(sync_engine.pool, StaticPool | SingletonThreadPool):
        return None
    return engine


async def _read_independent_committed_posture(
    session: AsyncSession,
    *,
    compatibility_class: str,
) -> tuple[RuntimePosture, int] | None:
    """Read durably committed posture state outside the caller's transaction.

    Selects columns rather than the mapped entity, so the result can never be
    served from the caller's identity map: ``session.scalar(select(Entity))``
    returns a retained object with its original attribute values, which is how
    a pre-rollback posture survived past the hard staleness ceiling.

    Returns ``None`` when no independent connection is available, leaving the
    caller to fall back to an explicitly guarded session read.  The caller's
    transaction and any savepoint it owns are never touched.
    """
    engine = _independent_connection_source(session)
    if engine is None:
        return None
    statement = select(
        PersonaModelPolicySetting.enforcement_posture,
        PersonaModelPolicySetting.posture_revision,
    ).where(PersonaModelPolicySetting.compatibility_class == compatibility_class)
    async with engine.connect() as connection:
        result = (await connection.execute(statement)).first()
    if result is None:
        # Absent in committed state.  It may still exist uncommitted in the
        # caller's transaction; that is the caller's view, never cacheable.
        return None
    return (coerce_posture(result[0]), coerce_posture_revision(result[1]))


async def _read_session_visible_posture(
    session: AsyncSession,
    *,
    compatibility_class: str,
) -> tuple[RuntimePosture, int] | None:
    """Read what the caller's own transaction sees, refreshing stale attributes.

    ``populate_existing`` is essential: without it a retained identity-map row
    is returned with its original attribute values even though the database has
    moved on.
    """
    row = await session.scalar(
        select(PersonaModelPolicySetting)
        .where(PersonaModelPolicySetting.compatibility_class == compatibility_class)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    return (coerce_posture(row.enforcement_posture), coerce_posture_revision(row.posture_revision))
