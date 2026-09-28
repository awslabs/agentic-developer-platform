"""Serialize deployments on a physical target: acquire, reconcile, release.

Issue #5150 (ENGINE-D1, parent #5131). This module is the only writer of
`orchestration_environment_leases`. The manifest and physical-target contract it
builds on lives in `deployment_manifest.py`; the shapes here are frozen for
[#5151](https://github.com/aws-e/adp/issues/5151) and
[#5152](https://github.com/aws-e/adp/issues/5152).

## The one thing this module guarantees

At most one action is deploying to any one physical target at any one time —
**across tenants**, because a physical target does not belong to a tenant. A
customer's AWS account can be connected twice, under two ids and possibly in two
different tenants, all pointing at one cluster. Code that serialized on the
connection id would let both holders proceed; this module serializes on the
canonical physical-target key instead, so aliases for one surface collide.

## Reusing #5142's concurrency discipline rather than inventing a second one

The merged execution store established the pattern and this module follows it
deliberately, because two different concurrency idioms in one subsystem is how one
of them ends up subtly weaker:

- **Compare-and-set on `revision`**, which advances by exactly one per applied
  write. A caller presents the revision it read and is answered `STALE` without a
  write if the row moved.
- **`SELECT ... FOR UPDATE` without `skip_locked`.** A contended caller must
  *wait* and then observe the winner's committed state. Skipping would read "no
  lease here" and insert a second one — and for a lease, silent skipping is
  precisely what becomes a double-deploy.
- **On SQLite `FOR UPDATE` is a no-op**, so the unique index on
  `canonical_target_key` is the real correctness backstop and the concurrency
  assertions run against real PostgreSQL.
- **No network calls inside the transaction.** The methods take an `AsyncSession`
  and commit nothing; the caller owns the transaction boundary. This is a
  correctness constraint rather than tidiness: `acquire_lease` holds a row lock,
  and an HTTP request inside it would block every other writer on that target for
  as long as the remote end takes to time out.

## Terminal and retryable refusals stay distinct

`STALE` means *another writer moved the row* — re-read, decide again, retry.
`CONFLICT` means *you have no standing here*: the target is held by somebody else,
or your ownership generation has been superseded. A `CONFLICT` is terminal and
must never be retried, because a caller that retries its way past a lost lease is
a displaced actor that kept acting. This is the same distinction the execution
store draws between `stale_revision` and `claim_generation_superseded`, and it is
kept identical on purpose so a consumer of both cannot learn two different rules.

## An expired lease is not proof the old deployment stopped

This is the rule most likely to be "simplified" by a later change, so it is stated
in the code as well as here: `lease_expires_at` records when contact was expected
and did not arrive. A deployment pipeline partitioned from us is still rolling
pods. Expiry therefore never authorizes takeover on its own — `acquire_lease`
requires `reconciled_terminal_evidence` on the existing row before it will hand a
lapsed target to a new holder. A lapsed lease with no reconciled evidence is stuck
*on purpose*; `reconcile_lease` is how a process that actually looked records what
it saw, and only that unblocks a takeover.

## What this module does not do

It starts no workflow, reads back no running deployment, executes no rollback, and
mints no credentials. It does not decide whether a deploy is authorized — that is
`deployment_manifest.resolve_manifest_entry` plus `execution_policy`, and this
module refuses to acquire a lease for anything that did not already resolve to a
`PhysicalTarget` carrying readback evidence.

## Every write requires standing over the stored holder

`acquire_lease`, `reconcile_lease` and `release_lease` all take a `LeaseHolder` and
all prove it against the row's own `(owner_org_id, owner_action_id, owner_generation)`
before writing. `reconcile_lease` is the one where this is least obvious and most
important: recording terminal evidence looks like a passive observation, but that
evidence is the **only** thing that unblocks a takeover of a lapsed target. A caller
that could write it for a lease it does not hold could manufacture its own
authorization to deploy over another tenant's cluster. Authority to write the
evidence is therefore the same authority as the takeover it licenses.

## Cross-tenant information disclosure

A refusal tells the caller the target is held and **nothing else**: not the
holder's org, not its action, not its release, not the account or cluster behind
the key. The story requires scoped generic conflict information, and the reason is
concrete — a refusal that named the holder would let any tenant probe for the
existence and deployment activity of another tenant's infrastructure. `LeaseView`
is returned only to a caller that holds the lease.

This is why standing is checked **before** the compare-and-set in every method
rather than after. The natural ordering — cheap revision check first — leaks: a
caller can present a deliberately stale revision for a target it has an alias for
and read the real holder's identity out of the `STALE` answer. Contention and
staleness must be indistinguishable to a caller without standing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.logging import get_logger

from .deployment_manifest import PhysicalTarget
from .models import OrchestrationEnvironmentLease

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "LeaseError",
    "LeaseHolder",
    "LeaseOutcome",
    "LeaseOutcomeKind",
    "LeaseState",
    "LeaseView",
    "ReleaseReason",
    "acquire_lease",
    "reconcile_lease",
    "release_lease",
]

# How long a holder's contact window is before it is considered lapsed. Matches
# `work_claims.DEFAULT_LEASE_SECONDS` so the two coordination layers do not
# disagree about what "lost contact" means. Note what this value is NOT: a
# timeout after which the target becomes available. Expiry only makes the target
# *eligible for reconciliation*; takeover still needs terminal evidence.
DEFAULT_LEASE_SECONDS = 3_600


class LeaseState(StrEnum):
    """Whether this target is currently held.

    Two members only, and `FREE` exists because the row outlives the hold: a
    released lease keeps its canonicalization evidence so an operator can still
    ask why two aliases were treated as one target. See the model docstring for
    why the row is freed rather than deleted.
    """

    HELD = "held"
    FREE = "free"


class LeaseOutcomeKind(StrEnum):
    """The three answers a lease transition can give.

    Typed rather than stringly so consumers branch on enum identity — a typo in a
    string comparison reads as "not a conflict", failing open on the check that
    exists to fail closed.

    - `APPLIED`: the write happened; the returned view is current.
    - `STALE`: the caller's revision is not the row's, so *another writer moved
      it*. Retryable: re-read and decide again.
    - `CONFLICT`: the caller has no standing — the target is held by another
      action, or the caller's ownership generation was superseded, or a takeover
      was attempted without terminal evidence. **Terminal. Never retry it.**
    """

    APPLIED = "applied"
    STALE = "stale"
    CONFLICT = "conflict"


class ReleaseReason(StrEnum):
    """Why a hold ended. Kept on the row so a freed lease explains itself."""

    COMPLETED = "completed"  # The deployment finished and was observed to finish
    FAILED = "failed"  # The deployment was observed to have failed
    ABANDONED = "abandoned"  # An authorized operator released a stuck hold


class LeaseError(RuntimeError):
    """The request was malformed, or named something that does not exist.

    Distinct from a `CONFLICT` outcome the same way `ExecutionStoreError` is: an
    outcome is an *answer* about the lease, this is "the question could not be
    asked". Callers fail closed on it and must not read it as "the target is
    free".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LeaseHolder:
    """Who is asking to hold a target, and under what authority.

    Frozen so a holder handed to two calls describes the same actor in both.

    - `org_id` is the tenant the action runs for. It is recorded on the row but is
      **not** part of the uniqueness key: one physical target is one target
      regardless of whose alias names it.
    - `action_id` is the action from the #5142 delivery ledger that will perform
      the deployment. The holder is the action, not the run, so the identity
      survives a process restart that keeps the same action.
    - `generation` is the ownership fence, sourced from the same claim generation
      the execution ledger records. An actor at a generation the row has passed is
      superseded and its writes are terminal refusals — this is what stops a
      displaced actor releasing the lease its successor holds.
    """

    org_id: str
    action_id: str
    generation: int

    def __post_init__(self) -> None:
        # Validated at construction rather than at the database: a blank tenant or
        # a zero generation reaching SQL becomes an integrity error with no
        # context, and for `generation` it would silently disable the fence.
        if not str(self.org_id or "").strip():
            raise LeaseError("invalid_holder", "A lease holder must name its tenant (org_id).")
        if not str(self.action_id or "").strip():
            raise LeaseError("invalid_holder", "A lease holder must name the action that will deploy (action_id).")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise LeaseError("invalid_holder", "generation must be a positive integer; generations start at 1.")


@dataclass(frozen=True)
class LeaseView:
    """A stored lease as its **holder** sees it.

    Returned only to a caller that holds the lease, or on a refusal whose caller
    already holds the relevant authority. It is deliberately never returned to a
    caller refused for contention: the fields below would disclose another
    tenant's deployment activity. See the module docstring.
    """

    id: str
    canonical_target_key: str
    state: LeaseState
    revision: int
    owner_org_id: str | None
    owner_action_id: str | None
    owner_generation: int
    manifest_entry_id: str | None
    release_ref: str | None
    evidence_source: str
    evidence_verified_at: datetime | None
    acquired_at: datetime | None
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    reconciled_terminal_evidence: str | None
    reconciled_at: datetime | None

    @property
    def held(self) -> bool:
        return self.state is LeaseState.HELD

    def lapsed_at(self, now: datetime) -> bool:
        """Whether the contact window has passed.

        Named to resist the reading it would otherwise invite: this is **not**
        "available for takeover". A lapsed lease whose previous action was never
        reconciled stays blocked, because lost contact is not an exit.
        """
        expiry = _as_aware(self.lease_expires_at)
        return bool(expiry and expiry <= now)


@dataclass(frozen=True)
class LeaseOutcome:
    """The result of a lease transition: what happened, and the lease if permitted.

    `lease` is populated for `APPLIED` and for refusals the caller has standing to
    see (its own stale revision, its own superseded generation). It is `None` for a
    contention `CONFLICT`, which is the disclosure boundary: the caller learns that
    the target is held and nothing about who holds it.

    `reason` is a stable machine-readable string so an operator can tell which
    fail-closed arm fired without parsing prose.
    """

    kind: LeaseOutcomeKind
    lease: LeaseView | None = None
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.kind is LeaseOutcomeKind.APPLIED


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(moment: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to timezone-aware UTC.

    The columns are `DateTime(timezone=True)`, so PostgreSQL returns aware values,
    but SQLite drops the offset and comparing naive to aware raises `TypeError`.
    Same reasoning and same treatment as `work_claims._as_aware` and
    `execution_store._as_aware`: every writer here is `_now()`/`utcnow()`, both
    UTC, so a naive read-back is a UTC value that lost its label in transit.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _to_view(row: OrchestrationEnvironmentLease) -> LeaseView:
    # Re-checked here as well as in `_locked_lease` because a newly inserted row
    # reaches this function without passing through the locked read.
    state = _require_known_state(row)
    return LeaseView(
        id=row.id,
        canonical_target_key=row.canonical_target_key,
        state=state,
        revision=row.revision,
        owner_org_id=row.owner_org_id,
        owner_action_id=row.owner_action_id,
        owner_generation=row.owner_generation,
        manifest_entry_id=row.manifest_entry_id,
        release_ref=row.release_ref,
        evidence_source=row.evidence_source,
        evidence_verified_at=_as_aware(row.evidence_verified_at),
        acquired_at=_as_aware(row.acquired_at),
        heartbeat_at=_as_aware(row.heartbeat_at),
        lease_expires_at=_as_aware(row.lease_expires_at),
        reconciled_terminal_evidence=row.reconciled_terminal_evidence,
        reconciled_at=_as_aware(row.reconciled_at),
    )


# The generic refusal handed to a caller that lost contention. Deliberately
# identical for "held by another tenant" and "held by another action in your own
# tenant": a distinguishable message would be a probe oracle for the existence of
# another tenant's deployment.
_TARGET_HELD = "target_held"


def _held_conflict() -> LeaseOutcome:
    """Build the opaque contention refusal. No lease, no holder detail, ever."""
    return LeaseOutcome(kind=LeaseOutcomeKind.CONFLICT, lease=None, reason=_TARGET_HELD)


async def _locked_lease(session: AsyncSession, canonical_key: str) -> OrchestrationEnvironmentLease | None:
    """Read a lease for its canonical key under a row lock.

    `with_for_update()` **without** `skip_locked`: a contended row must make the
    second caller wait and then observe the winner's committed state. Skipping
    would read "no lease" and insert a second row for the same target — the exact
    double-hold this module exists to prevent, and it would fail open silently.

    Not filtered by `org_id`, and that is the point: the lookup must find a lease
    held by *any* tenant, because the target is shared even when the aliases
    naming it are not. Isolation is enforced on what gets returned to the caller,
    not on what the lookup can see.

    On SQLite `FOR UPDATE` is a no-op, which is why the unique index on
    `canonical_target_key` is the correctness backstop and why the concurrency
    assertions run against real PostgreSQL.
    """
    stmt = (
        select(OrchestrationEnvironmentLease)
        .where(OrchestrationEnvironmentLease.canonical_target_key == canonical_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        # Validated on the way IN, not on the way out. Checking only when building
        # the returned view would run the check *after* the write had already been
        # decided, so an unrecognised state would fall through every
        # `state == HELD` comparison and be treated as free — handing out a target
        # a newer pod deliberately holds. That fail-open lands precisely in a
        # rolling deploy, where two builds coexist and one knows a member the
        # other does not.
        _require_known_state(row)
    return row


def _require_known_state(row: OrchestrationEnvironmentLease) -> LeaseState:
    """Refuse a row whose state this build does not recognise.

    Fails closed: an unknown state is not "probably free". See `_locked_lease` on
    why this must happen before any transition decision.
    """
    try:
        return LeaseState(row.state)
    except ValueError as exc:
        raise LeaseError(
            "unknown_vocabulary",
            f"Lease {row.id} holds a state this build does not recognise ({row.state}).",
        ) from exc


def _take(
    row: OrchestrationEnvironmentLease,
    *,
    target: PhysicalTarget,
    holder: LeaseHolder,
    manifest_entry_id: str,
    release_ref: str | None,
    lease_seconds: int,
    now: datetime,
) -> None:
    """Write the holder onto a free (or reconciled) row and advance its revision.

    Clears `reconciled_terminal_evidence`, and that clearing is load-bearing: the
    evidence describes the *previous* action's terminal outcome, and leaving it in
    place would mean the next expiry-based takeover would find stale evidence that
    appears to license it. The takeover gate would then be satisfied by a reading
    of a deployment two holders ago.

    Rewrites the canonicalization evidence from the **incoming** target, and that
    is a correctness requirement rather than tidiness. There is one durable row per
    physical target for the life of the platform, so a row acquired today may have
    been inserted by a different tenant, through a different alias, months ago.
    Leaving the insert-time evidence in place would mean the row describes a
    readback that did not authorize the current hold — and `evidence_source` is the
    exact field an operator consults to answer "why did the engine believe these two
    aliases were the same place, and what proved it?". A stale answer there is worse
    than none, because it is indistinguishable from a fresh one.

    Written on every take, not only on a change of action: the incoming
    `PhysicalTarget` is by construction the readback that authorized *this*
    acquisition, so the fresher timestamp is the truthful one even for a same-action
    reacquire.
    """
    # The generation fence is **per action**, and that scoping is the whole of its
    # correctness. Within one action it is monotonic, like the execution store's
    # `_adopt_generation`: raised, never lowered, because lowering would hand the
    # fence back to a superseded process of that same action.
    #
    # Across a *change* of action it is reset to the incoming holder's generation.
    # Clamping it to the previous action's high-water mark instead would leave a new
    # legitimate holder arriving at generation 1 standing behind a stranger's
    # generation 7 — it would be refused as superseded on its own first heartbeat
    # and could never release the lease it legitimately holds. Resetting is safe
    # because a displaced *previous* action is refused earlier, by the
    # held-by-somebody-else arm, and never reaches this comparison at all.
    same_action = row.owner_org_id == holder.org_id and row.owner_action_id == holder.action_id
    row.state = LeaseState.HELD.value
    row.owner_org_id = holder.org_id
    row.owner_action_id = holder.action_id
    row.owner_generation = max(row.owner_generation, holder.generation) if same_action else holder.generation
    row.manifest_entry_id = manifest_entry_id
    row.release_ref = release_ref
    row.revision = row.revision + 1
    row.acquired_at = now
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=lease_seconds)
    row.reconciled_terminal_evidence = None
    row.reconciled_at = None
    row.release_reason = None
    row.released_at = None
    # The readback that authorized THIS hold, replacing whatever the previous
    # holder's alias recorded. See the docstring.
    row.evidence_source = target.evidence.source
    row.evidence_verified_at = _parse_evidence_time(target.evidence.verified_at)
    row.evidence_detail = target.evidence.detail


async def acquire_lease(
    session: AsyncSession,
    *,
    target: PhysicalTarget,
    holder: LeaseHolder,
    manifest_entry_id: str,
    release_ref: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LeaseOutcome:
    """Take exclusive hold of one physical deployment target, or refuse.

    Takes a `PhysicalTarget` rather than a key string, and that signature is a
    guard rather than a convenience: a `PhysicalTarget` cannot be constructed
    without `TargetEvidence`, so there is no code path in which a lease is acquired
    for an identity derived from a caller-supplied account string. The key is
    derived here, from the target, and never accepted from the caller.

    Idempotent for the *same* holder: an action that already holds the target and
    asks again receives `APPLIED` with its existing lease refreshed, so a retry
    after a crash is safe rather than a self-conflict.

    Commits nothing; the caller owns the transaction boundary.

    Returns:
        `APPLIED` with the lease when the target was free (or already held by this
        same action). `CONFLICT` with `reason="target_held"` and **no lease** when
        another action holds it — including when that action's lease has lapsed but
        its outcome has not been reconciled, because lost contact is not an exit.
        `CONFLICT` with `reason="owner_generation_superseded"` when the caller's own
        generation has been overtaken.
    """
    if not str(manifest_entry_id or "").strip():
        # An anonymous hold cannot be audited: an operator looking at a live
        # deployment must be able to see which reviewed approval authorized it.
        raise LeaseError(
            "invalid_acquire",
            "Acquiring a lease requires the manifest entry that authorized it; an unattributed hold cannot be audited.",
        )
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
        raise LeaseError("invalid_acquire", "lease_seconds must be a positive integer.")

    canonical_key = target.canonical_key
    now = _now()

    existing = await _locked_lease(session, canonical_key)
    if existing is not None:
        return _acquire_existing(
            existing,
            target=target,
            holder=holder,
            manifest_entry_id=manifest_entry_id,
            release_ref=release_ref,
            lease_seconds=lease_seconds,
            now=now,
        )

    row = OrchestrationEnvironmentLease(
        canonical_target_key=canonical_key,
        evidence_source=target.evidence.source,
        evidence_verified_at=_parse_evidence_time(target.evidence.verified_at),
        evidence_detail=target.evidence.detail,
        state=LeaseState.HELD.value,
        owner_org_id=holder.org_id,
        owner_action_id=holder.action_id,
        owner_generation=holder.generation,
        manifest_entry_id=manifest_entry_id,
        release_ref=release_ref,
        revision=1,
        acquired_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        created_at=now,
    )
    try:
        # Isolate the insert race from the caller's transaction. Rolling the whole
        # session back here would discard work the same pass already did, and
        # leaving it failed would poison the rest of it. Same treatment as
        # `execution_store.create_execution` and `work_claims.claim_work`.
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Lost the insert race on the unique index. Unlike the execution store —
        # where losing means "the identity we wanted now exists", which is
        # agreement — losing here means *somebody else holds this target*. The
        # winner must be re-read and evaluated, never adopted.
        logger.info("environment lease: lost insert race for a physical target; re-reading the committed holder")
        winner = await _locked_lease(session, canonical_key)
        if winner is None:
            # Not visible from this transaction, which on PostgreSQL means the
            # winner has not committed yet. Reporting "free" would be catastrophic
            # here (two holders), and reporting a retryable error would invite a
            # race, so this is a typed refusal the caller fails closed on.
            raise LeaseError(
                "acquire_race_lost",
                "Another process concurrently acquired this deployment target; retry once it commits.",
            ) from None
        return _acquire_existing(
            winner,
            target=target,
            holder=holder,
            manifest_entry_id=manifest_entry_id,
            release_ref=release_ref,
            lease_seconds=lease_seconds,
            now=now,
        )

    logger.info(
        "environment lease: acquired target for action=%s entry=%s (first hold)",
        holder.action_id,
        manifest_entry_id,
    )
    return LeaseOutcome(kind=LeaseOutcomeKind.APPLIED, lease=_to_view(row))


def _acquire_existing(
    row: OrchestrationEnvironmentLease,
    *,
    target: PhysicalTarget,
    holder: LeaseHolder,
    manifest_entry_id: str,
    release_ref: str | None,
    lease_seconds: int,
    now: datetime,
) -> LeaseOutcome:
    """Decide whether `holder` may take an existing lease row.

    The order of these arms is the security of the whole module, so each is
    explicit about what it refuses.
    """
    if row.state == LeaseState.HELD.value:
        same_action = row.owner_org_id == holder.org_id and row.owner_action_id == holder.action_id
        if same_action:
            if holder.generation < row.owner_generation:
                # Our own action id, but at a generation the row has passed: this
                # process was superseded and a newer one holds the target. Terminal,
                # and the lease IS returned — the caller holds the right action and
                # only its own binding lapsed, so it needs to see what superseded it.
                logger.warning(
                    "environment lease: refusing re-acquire by superseded generation %s (current %s)",
                    holder.generation,
                    row.owner_generation,
                )
                return LeaseOutcome(
                    kind=LeaseOutcomeKind.CONFLICT,
                    lease=_to_view(row),
                    reason="owner_generation_superseded",
                )
            # Same action, current generation: a retry after a crash. Refresh the
            # contact window rather than refusing, so recovery is not punished.
            _take(
                row,
                target=target,
                holder=holder,
                manifest_entry_id=manifest_entry_id,
                release_ref=release_ref,
                lease_seconds=lease_seconds,
                now=now,
            )
            return LeaseOutcome(kind=LeaseOutcomeKind.APPLIED, lease=_to_view(row))

        # Held by somebody else. The ONLY route past this point is a lapsed lease
        # whose previous action was positively reconciled as terminal.
        expiry = _as_aware(row.lease_expires_at)
        lapsed = bool(expiry and expiry <= now)
        if not (lapsed and row.reconciled_terminal_evidence):
            # Includes the dangerous case the story calls out explicitly: the lease
            # HAS expired but nothing reconciled the previous action, so the old
            # pipeline may still be deploying. Expiry is evidence of lost contact,
            # never of an exit, so it does not license takeover.
            logger.info(
                "environment lease: refusing acquire — target is held (lapsed=%s, reconciled=%s)",
                lapsed,
                bool(row.reconciled_terminal_evidence),
            )
            return _held_conflict()

        logger.info(
            "environment lease: taking over a lapsed, reconciled target for action=%s entry=%s",
            holder.action_id,
            manifest_entry_id,
        )

    # Free, or a lapsed hold with reconciled terminal evidence.
    _take(
        row,
        target=target,
        holder=holder,
        manifest_entry_id=manifest_entry_id,
        release_ref=release_ref,
        lease_seconds=lease_seconds,
        now=now,
    )
    return LeaseOutcome(kind=LeaseOutcomeKind.APPLIED, lease=_to_view(row))


async def reconcile_lease(
    session: AsyncSession,
    *,
    canonical_target_key: str,
    expected_revision: int,
    terminal_evidence: str,
    holder: LeaseHolder,
    heartbeat: bool = False,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LeaseOutcome:
    """Record what was observed about the current holder's deployment.

    This is the **only** thing that can unblock a takeover of a lapsed target, and
    that is the whole reason it exists as a separate operation. A process that went
    and looked at the previous deployment — asked the provider for the workflow
    run's conclusion, say — records what it saw here, and only then may a new holder
    acquire the target. Without this step a lapsed lease stays held forever, which
    is the intended fail-closed behaviour rather than a bug to be timed out.

    `heartbeat=True` instead refreshes the current holder's contact window; it
    records continuing liveness rather than an ending.

    ## `holder` is required, and why it must be

    Both modes demand proof of standing over the **stored** holder before they read
    or write anything. `holder` was briefly optional for the reconcile mode, on the
    reasoning that an observer reports what it saw and needs no authority to have
    looked. That reasoning is wrong, and dangerously so: terminal evidence on this
    row is the *only* thing that unblocks a takeover of a lapsed target. Any caller
    able to derive the canonical key — which is derivable from a connection the
    caller legitimately owns — could therefore submit an arbitrary evidence string
    for a lease it has nothing to do with, wait for expiry, and acquire a target
    another tenant is actively deploying to. The evidence write *is* the takeover
    authorization, so it carries the same authority requirement as the takeover.

    Standing is the stored row's `(owner_org_id, owner_action_id)` at a generation
    not behind `owner_generation` — the same fence `release_lease` applies, and for
    the same reason. An observer that is not the holder does not reconcile; it
    reports to whatever owns the holder's action, and that action reconciles.

    Every unauthorized answer is the same opaque contention `CONFLICT`, including
    the stale-revision case. A distinguishable answer — or a returned `LeaseView` —
    would let one tenant present its own alias for a shared physical target,
    deliberately supply a stale revision, and read back the other tenant's
    `owner_org_id`, `owner_action_id`, `manifest_entry_id`, `release_ref` and
    evidence. Standing is therefore established *before* the compare-and-set is
    consulted, not after.

    Args:
        expected_revision: The compare-and-set fence. A mismatch answers `STALE`
            without writing, because whatever moved the row may have changed what
            the caller should record — but only to a caller that has already proven
            standing.
        terminal_evidence: What established that the holder's deployment is over —
            a workflow conclusion, a run id, a check verdict. Required and non-empty
            for a reconcile, because unevidenced reconciliation is exactly the
            expiry-based takeover this module refuses. Ignored when `heartbeat` is
            set.
        holder: The action claiming standing over this lease. Required in both modes.

    Returns:
        `APPLIED` with the updated lease. `STALE` (with the lease) when the caller
        holds standing and only its revision moved. `CONFLICT` with no lease when
        the caller is not the holder, and `CONFLICT` with the lease when the caller
        holds the right action at a superseded generation.
    """
    if not heartbeat and not str(terminal_evidence or "").strip():
        raise LeaseError(
            "missing_terminal_evidence",
            "Reconciling a lease requires evidence that the holder's deployment is over; "
            "an unevidenced reconcile is the expiry-based takeover this module refuses.",
        )
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise LeaseError("invalid_reconcile", "expected_revision must be a positive integer read from the lease.")

    row = await _locked_lease(session, canonical_target_key)
    if row is None:
        raise LeaseError("unknown_lease", "No lease exists for that physical target; nothing to reconcile.")

    # STANDING FIRST — before the compare-and-set, and before anything is returned.
    #
    # Ordering is the security of this function, not a style choice. Checking the
    # revision first and returning `_to_view(row)` on a mismatch is a cross-tenant
    # disclosure: a caller holding its own alias for a shared physical target can
    # derive the key, present a deliberately stale revision, and read back the other
    # tenant's owner, action, manifest entry, release ref and evidence. A caller with
    # no standing must not be able to distinguish "held by someone else" from "your
    # revision moved", so both answers are the same opaque conflict.
    if row.state != LeaseState.HELD.value or row.owner_org_id != holder.org_id or row.owner_action_id != holder.action_id:
        logger.info("environment lease: refusing reconcile by a caller with no standing over the stored holder")
        return _held_conflict()
    if holder.generation < row.owner_generation:
        # Right action, superseded generation. Terminal, and the lease IS returned:
        # this caller holds the correct action and only its own binding lapsed, so it
        # needs to see what superseded it. The same asymmetry `release_lease` applies.
        logger.warning(
            "environment lease: refusing reconcile at superseded generation %s (current %s)",
            holder.generation,
            row.owner_generation,
        )
        return LeaseOutcome(kind=LeaseOutcomeKind.CONFLICT, lease=_to_view(row), reason="owner_generation_superseded")

    if row.revision != expected_revision:
        # Another writer moved it. Retryable, and the lease is returned so the caller
        # can decide again without a second round trip — safe only because standing
        # is already proven above.
        return LeaseOutcome(kind=LeaseOutcomeKind.STALE, lease=_to_view(row), reason="stale_revision")

    now = _now()

    if heartbeat:
        row.heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.revision = row.revision + 1
        return LeaseOutcome(kind=LeaseOutcomeKind.APPLIED, lease=_to_view(row))

    # Record the terminal observation. The hold is deliberately NOT released here:
    # reconciling establishes what happened, while releasing hands the target on,
    # and a single step doing both would mean an observer could free a target
    # without holding any authority over it.
    row.reconciled_terminal_evidence = str(terminal_evidence).strip()
    row.reconciled_at = now
    row.revision = row.revision + 1
    logger.info("environment lease: recorded terminal evidence for a held target; takeover is now permitted")
    return LeaseOutcome(kind=LeaseOutcomeKind.APPLIED, lease=_to_view(row))


async def release_lease(
    session: AsyncSession,
    *,
    canonical_target_key: str,
    holder: LeaseHolder,
    reason: ReleaseReason,
    terminal_evidence: str,
) -> LeaseOutcome:
    """Hand a physical target back, so another action may deploy to it.

    Only the current holder at a current generation may release. The stale-actor
    case is the one that matters and it is refused rather than applied: a
    superseded actor whose release was honoured would free the target *while its
    successor is actively deploying to it*, handing the cluster to whoever asks
    next. That is a worse outcome than the deployment it was trying to tidy up
    after, which is why this is terminal rather than a no-op.

    Args:
        terminal_evidence: What established that the work is over. Required and
            non-empty for the same reason `work_claims.release_work` requires it:
            an unevidenced release is indistinguishable from a lease lapse, and
            this is the call that makes the target re-acquirable.

    Returns:
        `APPLIED` when released, and also when the lease is already free at this
        holder's generation — a retried terminal callback is not an error.
        `CONFLICT` when the caller is not the holder, or is at a superseded
        generation.
    """
    if not str(terminal_evidence or "").strip():
        raise LeaseError(
            "missing_terminal_evidence",
            "Releasing a lease requires evidence that the deployment is over; an unevidenced release is a lease lapse by another name.",
        )

    row = await _locked_lease(session, canonical_target_key)
    if row is None:
        raise LeaseError("unknown_lease", "No lease exists for that physical target; nothing to release.")

    is_holder = row.owner_org_id == holder.org_id and row.owner_action_id == holder.action_id

    if row.state == LeaseState.FREE.value:
        if is_holder and holder.generation >= row.owner_generation:
            # Already released at this generation: a retried terminal callback.
            # Idempotent rather than an error, so at-least-once delivery on the
            # completion path does not produce a spurious failure.
            return LeaseOutcome(kind=LeaseOutcomeKind.APPLIED, lease=_to_view(row), reason="already_released")
        return _held_conflict()

    if not is_holder:
        # Somebody else's lease. Opaque, so a caller cannot use release attempts to
        # discover which targets other tenants hold.
        logger.warning("environment lease: refusing release by a non-holder")
        return _held_conflict()

    if holder.generation < row.owner_generation:
        # THE stale-owner refusal. Applying it would release the replacement's hold
        # on behalf of a superseded actor while the current owner is still
        # deploying. The lease is returned because this caller holds the right
        # action and needs to see what superseded it.
        logger.warning(
            "environment lease: refusing release at stale generation %s (current %s)",
            holder.generation,
            row.owner_generation,
        )
        return LeaseOutcome(kind=LeaseOutcomeKind.CONFLICT, lease=_to_view(row), reason="owner_generation_superseded")

    now = _now()
    # `state` is the single authority on whether the target is held — every arm
    # above and in `_acquire_existing` tests it before consulting an owner column —
    # so the owner fields become the *last-holder* record rather than a live claim.
    row.state = LeaseState.FREE.value
    row.manifest_entry_id = None
    row.release_ref = None
    row.lease_expires_at = None
    row.heartbeat_at = None
    # `owner_org_id`, `owner_action_id` and `owner_generation` are deliberately
    # retained, and each for its own reason. The generation must never go backwards
    # or a superseded actor could re-acquire at its old generation and look current.
    # The org and action are what let a *retried* terminal callback be recognised as
    # the same holder and answered idempotently: clearing them would make the second
    # delivery of an at-least-once completion look like an unrelated actor releasing
    # somebody else's lease, and it would receive a hard conflict for having
    # correctly finished its job.
    row.release_reason = reason.value
    row.released_at = now
    row.reconciled_terminal_evidence = str(terminal_evidence).strip()
    row.reconciled_at = now
    row.revision = row.revision + 1
    logger.info("environment lease: released target (reason=%s)", reason.value)
    return LeaseOutcome(kind=LeaseOutcomeKind.APPLIED, lease=_to_view(row))


def _parse_evidence_time(raw: str) -> datetime:
    """Parse the evidence readback timestamp into an aware UTC datetime.

    Refused rather than defaulted to "now" when unparseable: defaulting would
    stamp the moment of *storage* as the moment of *verification*, making a target
    whose readback is years old look freshly proven. That is a false provenance
    claim on the exact field an operator consults to judge staleness.
    """
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LeaseError(
            "invalid_evidence_time",
            f"Target evidence verified_at must be an ISO-8601 timestamp; got {raw!r}.",
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
