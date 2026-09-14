"""Shared issue ownership: the one admission path every launch path goes through.

Issue #5127 (EPIC #4191). A delivery plan can start work through two independent
paths — the engine tick (`dispatch_pass`) and direct dispatch (GitHub webhook →
SQS → worker). Neither consults the other, so one issue can acquire two
concurrent mutating runs: competing branches, duplicated spend, and a review
surface where nobody can tell which run's output is authoritative. This module is
the single place that decides whether a launch is allowed to proceed.

**Why one shared service instead of a check per path.** A protection that each
path implements for itself is a protection that each path can drift out of. The
contract here is deliberately narrow — `claim_work`, `bind_run`, `release_work`,
`force_handover` — so every caller passes the same guard and a new launch path
cannot accidentally acquire a weaker version of it.

**What the owner is.** The owner is the *lane*, not the run. Developer, reviewer
and repair runs on one issue are sequential phases of the same delivery work, so
keying ownership on the run would refuse the reviewer that is supposed to follow
the developer, and an issue's earlier merged PR must not permanently suppress
later authorized persona work. One claim, one owner, one *mutating run at a time*
(`active_run_id`), many runs over its life.

**What a lease is not.** `lease_expires_at` records when contact was expected and
did not arrive. It is evidence of lost contact, never of an exit, and it never
authorizes a takeover by itself — that is the single most tempting mistake here,
because "the lease expired so the old worker must be gone" is false exactly when
it is dangerous: a worker partitioned from the database is still pushing commits.
Takeover therefore requires a positive `exited` verdict from
`activity.liveness` (reused, not restated, so this path and the activity read
path cannot disagree about what "finished" means) plus an authorized recorded
decision.

**The limit of a database fence.** Advancing `generation` stops the *next*
gateway-mediated action by a stale run. It does **not** revoke a GitHub
installation token that has already been issued — no row can. So
`force_handover` refuses unless the caller can attest that outstanding effects
and credentials were reconciled. Claiming otherwise would advertise fencing this
layer cannot deliver.

**No transport route in this module.** The story allows an
`/internal/orchestration/work-claims` route "if a transport route is required".
None is added here, and that is a hard constraint rather than a deferral:
`tests/orchestration/test_internal_plane_guard.py` fails the build if any
internal-plane module so much as imports this package, because agent pods can
call every `/internal/v1/*` route with any method. Out-of-process callers
(webhook Lambda, worker) need a transport that does not hand agents write access
to promotion state; designing it is the integration step, and inventing one here
would either break that guard or quietly weaken it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.liveness import compute_liveness
from src.shared.logging import get_logger

from .models import ClaimState, OrchestrationWorkClaim

logger = get_logger(__name__)

__all__ = [
    "ClaimBinding",
    "ClaimOwner",
    "ClaimReceipt",
    "Disposition",
    "OwnerKind",
    "ReleaseReason",
    "WorkClaimError",
    "DEFAULT_LEASE_SECONDS",
    "claim_work",
    "bind_run",
    "release_work",
    "force_handover",
    "heartbeat",
]

# How long an admitted claim's lease runs before it is considered lapsed. Mirrors
# nothing else deliberately: the stall threshold in `stall.py` answers "should an
# operator look at this?", which is a different question from "has contact been
# lost?". Expiry is an input to reconciliation, never an authorization to take
# over, so this value cannot cause a double-admission if it is set too low.
DEFAULT_LEASE_SECONDS = 3_600


class OwnerKind(StrEnum):
    """Which launch path owns a claim.

    Recorded because the two paths need different reconciliation: an engine-owned
    claim can be resolved against the graph, a directly-dispatched one only
    against the ingress ledger. Stored as `String(32)`, so a new member needs no
    DDL.
    """

    ENGINE_FLOW = "engine_flow"  # An orchestration flow/lane (engine tick)
    DIRECT_DISPATCH = "direct_dispatch"  # Webhook / adp-trigger / EventBridge


class Disposition(StrEnum):
    """The outcome of an admission attempt.

    Four members because collapsing any two of them loses information the caller
    must act on differently. `DUPLICATE` is not `ADMITTED` (the caller must not
    start a second run, but nothing is wrong); `CONFLICT` is not `BLOCKED` (a
    conflict is another owner working normally, a block is an unresolved
    ownership question a human may need to settle).
    """

    ADMITTED = "admitted"  # Caller holds the claim and may start work
    DUPLICATE = "duplicate"  # This same event already claimed; original receipt
    CONFLICT = "conflict"  # Another owner holds this issue
    BLOCKED = "blocked"  # Ownership unresolved; fail closed, do not start


class ReleaseReason(StrEnum):
    """Why a claim stopped being held. Kept on the row so it explains itself."""

    COMPLETED = "completed"  # Work finished; evidence observed
    FAILED = "failed"  # Run ended without completing
    ABANDONED = "abandoned"  # Proven exited without a terminal report
    HANDOVER = "handover"  # Forced handover by recorded decision


class WorkClaimError(RuntimeError):
    """An admission call was malformed or referenced something that does not exist.

    Distinct from a `CONFLICT`/`BLOCKED` receipt: those are *answers* about
    ownership, this is "the question could not be asked". Callers must fail closed
    on it rather than treating it as an absent owner.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RunLivenessResolver(Protocol):
    """Resolves a run's ingress row, for liveness reconciliation.

    A Protocol rather than a concrete dependency so this module does not import
    the DynamoDB-backed resolver (and so tests can supply an in-memory one).
    `budget.run_binding.RunBindingResolver` satisfies it as-is.
    """

    async def resolve(self, run_id: str) -> dict | None: ...


@dataclass(frozen=True)
class ClaimBinding:
    """Which issue is being claimed.

    `provider_repository_id` is the provider's immutable numeric repository id,
    and it is a required input rather than something this module derives from a
    repo name. No current dispatch path carries it — every one of them keys on the
    mutable `owner/name` string — so supplying it is part of integrating a path.
    Deriving it from a name here would reintroduce exactly the rename bypass the
    column exists to close, and would do it invisibly.
    """

    org_id: str
    provider_repository_id: int
    issue_number: int

    def __post_init__(self) -> None:
        if not str(self.org_id).strip():
            raise WorkClaimError("invalid_binding", "A work claim requires a tenant; org_id was empty.")
        # Rejected rather than coerced. A falsy repository id is the shape a
        # caller produces when it *had* no immutable id and passed a default
        # through — admitting it would file every such caller's claims under
        # repository 0, making one shared owner for unrelated repositories.
        if not isinstance(self.provider_repository_id, int) or isinstance(self.provider_repository_id, bool) or self.provider_repository_id <= 0:
            raise WorkClaimError(
                "invalid_binding",
                f"provider_repository_id must be a positive provider integer, got {self.provider_repository_id!r}. "
                "Pass the immutable repository id; a repository name cannot substitute for it.",
            )
        if not isinstance(self.issue_number, int) or isinstance(self.issue_number, bool) or self.issue_number <= 0:
            raise WorkClaimError("invalid_binding", f"issue_number must be a positive integer, got {self.issue_number!r}.")


@dataclass(frozen=True)
class ClaimOwner:
    """Who owns the claim — the flow/lane, not the individual run."""

    kind: OwnerKind
    ref: str

    def __post_init__(self) -> None:
        if not str(self.ref).strip():
            raise WorkClaimError("invalid_owner", "A work claim requires an owner reference.")


@dataclass(frozen=True)
class ClaimReceipt:
    """The answer to an admission attempt.

    `claim_id` and `generation` together are what a later `bind_run` presents:
    the id alone would let a stale run bind to a claim that has since moved on.
    """

    disposition: Disposition
    claim_id: str | None = None
    generation: int | None = None
    # Set on CONFLICT/BLOCKED: a stable machine-readable reason, so an operator
    # can tell *which* fail-closed arm fired. "another lane owns this issue" and
    # "the previous owner may still be running" are both refusals with very
    # different responses.
    reason: str | None = None
    # On CONFLICT, who holds it — enough for an operator to find the other run
    # without exposing anything cross-tenant (same org by construction).
    holder_ref: str | None = None

    @property
    def admitted(self) -> bool:
        """True only for ADMITTED. `DUPLICATE` must not start a second run."""
        return self.disposition is Disposition.ADMITTED


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(moment: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to timezone-aware UTC before comparing it.

    The columns are `DateTime(timezone=True)`, so PostgreSQL returns aware values
    — but SQLite (and any driver that drops the offset) returns naive ones, and
    comparing naive to aware raises `TypeError`. Without this, the lease
    comparison in `claim_work` would crash *at admission*, which fails in the
    worst available direction: a caller that cannot get an answer about ownership
    has to fail closed, so an entire tenant's dispatch stops.

    Treating a naive value as UTC is sound rather than merely convenient: every
    writer here is `utcnow()`/`_now()`, both of which produce UTC, so a naive
    read-back is a UTC value that lost its label in transit.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def _locked_claim(session: AsyncSession, binding: ClaimBinding) -> OrchestrationWorkClaim | None:
    """Read the claim row for a binding under a row lock.

    `with_for_update()` without `skip_locked`: a contended row must make the
    second caller *wait* and then observe the winner's committed state, not be
    skipped. Skipping here would read "no owner" and admit a second run, which is
    the exact bug this module exists to prevent. On SQLite (where the semantic
    tests run) `FOR UPDATE` is a no-op, which is why the unique index — not this
    lock — is the correctness backstop, and why the concurrency assertions run
    against real PostgreSQL.
    """
    stmt = (
        select(OrchestrationWorkClaim)
        .where(
            OrchestrationWorkClaim.org_id == binding.org_id,
            OrchestrationWorkClaim.provider_repository_id == binding.provider_repository_id,
            OrchestrationWorkClaim.issue_number == binding.issue_number,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim_work(
    session: AsyncSession,
    *,
    binding: ClaimBinding,
    owner: ClaimOwner,
    event_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ClaimReceipt:
    """Admit at most one owner for an issue, or refuse with a reason.

    The single admission point for every launch path. Does not commit — the caller
    owns the transaction boundary, matching every other orchestration pass.

    Args:
        binding: Which issue. Requires the immutable provider repository id.
        owner: The claiming lane.
        event_id: The event this admission is attributed to. A replay of the same
            event returns the original receipt instead of being admitted twice,
            which is what makes at-least-once delivery on either path safe.
        lease_seconds: Lease length for the admitted claim.

    Returns:
        `ADMITTED` (start work), `DUPLICATE` (already admitted for this event —
        do NOT start a second run), `CONFLICT` (another owner holds it), or
        `BLOCKED` (ownership unresolved; fail closed).

    Raises:
        WorkClaimError: the request was malformed. Callers fail closed.
    """
    if not str(event_id or "").strip():
        # Without an event id a replay is indistinguishable from a fresh launch,
        # so idempotency would silently not exist. Refused rather than defaulted.
        raise WorkClaimError("missing_event_id", "An admission attempt must name the event it is attributed to.")
    event_id = str(event_id).strip()

    existing = await _locked_claim(session, binding)
    now = _now()

    if existing is None:
        # No row for this issue yet. The unique index is what makes this safe under
        # concurrency: a second transaction inserting the same binding fails on it
        # rather than producing a second owner.
        claim = OrchestrationWorkClaim(
            org_id=binding.org_id,
            provider_repository_id=binding.provider_repository_id,
            issue_number=binding.issue_number,
            owner_kind=owner.kind.value,
            owner_ref=owner.ref,
            state=ClaimState.HELD.value,
            generation=1,
            claim_event_id=event_id,
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        session.add(claim)
        try:
            await session.flush()
        except IntegrityError:
            # Lost the insert race. The winner is committed (or committing), so the
            # honest answer is a conflict — NOT a retry that could admit us behind
            # the winner's back. The caller sees one admission and one refusal,
            # which is the required outcome.
            logger.info(
                "work claim: lost insert race for org=%s repo=%s issue=%s owner=%s — refusing as conflict",
                binding.org_id,
                binding.provider_repository_id,
                binding.issue_number,
                owner.ref,
            )
            raise WorkClaimError(
                "claim_race_lost",
                f"Another admission claimed issue {binding.issue_number} concurrently; this attempt is refused.",
            ) from None

        logger.info(
            "work claim: admitted org=%s repo=%s issue=%s owner=%s:%s generation=1",
            binding.org_id,
            binding.provider_repository_id,
            binding.issue_number,
            owner.kind.value,
            owner.ref,
        )
        return ClaimReceipt(disposition=Disposition.ADMITTED, claim_id=claim.id, generation=claim.generation)

    # --- A row exists. Duplicate check comes FIRST, before any ownership test. ---
    # A redelivery of the event that produced the current generation must get its
    # original receipt back even when the claim is now held by a *different* owner
    # or already released — otherwise a retry of an event we already honored reads
    # as a conflict and the caller reports a spurious refusal for work it did do.
    if existing.claim_event_id and existing.claim_event_id == event_id:
        logger.info(
            "work claim: duplicate event %s for org=%s issue=%s — returning original receipt (generation=%s)",
            event_id,
            binding.org_id,
            binding.issue_number,
            existing.generation,
        )
        return ClaimReceipt(
            disposition=Disposition.DUPLICATE,
            claim_id=existing.id,
            generation=existing.generation,
            reason="event_already_admitted",
        )

    if existing.state == ClaimState.HELD.value:
        # Someone owns this issue. Note what is deliberately absent: there is no
        # "…unless the lease expired" arm. An expired lease means contact was
        # lost, and a partitioned worker is still working. Reclaiming requires
        # `force_handover` and its evidence.
        lease_expires_at = _as_aware(existing.lease_expires_at)
        lease_lapsed = bool(lease_expires_at and lease_expires_at <= now)
        reason = "held_lease_lapsed" if lease_lapsed else "held_by_other_owner"
        logger.info(
            "work claim: refused org=%s issue=%s — %s (holder=%s generation=%s)",
            binding.org_id,
            binding.issue_number,
            reason,
            existing.owner_ref,
            existing.generation,
        )
        return ClaimReceipt(
            disposition=Disposition.CONFLICT,
            claim_id=existing.id,
            generation=existing.generation,
            reason=reason,
            holder_ref=existing.owner_ref,
        )

    if existing.state != ClaimState.RELEASED.value:
        # An unrecognised state is indeterminate, not free. Same reasoning as
        # `compute_liveness`: a value this build has never heard of must not be
        # read as "nobody owns this".
        logger.warning(
            "work claim: org=%s issue=%s has unrecognised state %r — blocking admission",
            binding.org_id,
            binding.issue_number,
            existing.state,
        )
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=existing.id,
            generation=existing.generation,
            reason="unrecognised_claim_state",
        )

    # Released: ordered reuse. The generation advances so anything issued under
    # the previous owner is permanently distinguishable, and the row's release
    # history is preserved rather than overwritten by a fresh insert.
    existing.owner_kind = owner.kind.value
    existing.owner_ref = owner.ref
    existing.state = ClaimState.HELD.value
    existing.generation = existing.generation + 1
    existing.active_run_id = None
    existing.claim_event_id = event_id
    existing.claimed_at = now
    existing.heartbeat_at = now
    existing.lease_expires_at = now + timedelta(seconds=lease_seconds)
    existing.release_reason = None
    existing.released_at = None
    await session.flush()

    logger.info(
        "work claim: readmitted org=%s repo=%s issue=%s owner=%s:%s generation=%s",
        binding.org_id,
        binding.provider_repository_id,
        binding.issue_number,
        owner.kind.value,
        owner.ref,
        existing.generation,
    )
    return ClaimReceipt(disposition=Disposition.ADMITTED, claim_id=existing.id, generation=existing.generation)


async def bind_run(
    session: AsyncSession,
    *,
    claim_id: str,
    generation: int,
    run_id: str,
) -> ClaimReceipt:
    """Bind a concrete run to a claim the caller was admitted for.

    The compare-and-set that makes a stale generation unable to start work. A
    worker that was admitted, lost its claim to a recorded handover, and only then
    reached startup presents generation N against a row at N+1 and is refused —
    which is the check that stops the *second* worker in a handover from running
    concurrently with the replacement.

    Args:
        claim_id: From the admission receipt.
        generation: From the admission receipt. Checked, not trusted.
        run_id: The run now executing under this claim.

    Returns:
        `ADMITTED` when bound; `BLOCKED` with a reason when the generation is
        stale, the claim is not held, or another run is already bound.
    """
    if not str(run_id or "").strip():
        raise WorkClaimError("missing_run_id", "Binding a run requires the run id.")
    run_id = str(run_id).strip()

    stmt = select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == claim_id).with_for_update().execution_options(populate_existing=True)
    claim = (await session.execute(stmt)).scalar_one_or_none()

    if claim is None:
        # Fail closed: an unknown claim is not an absent owner.
        raise WorkClaimError("unknown_claim", f"Claim {claim_id} does not exist; no run can be bound to it.")

    if claim.generation != generation:
        logger.warning(
            "work claim: refusing to bind run %s to claim %s — stale generation %s (current %s)",
            run_id,
            claim_id,
            generation,
            claim.generation,
        )
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="stale_generation",
        )

    if claim.state != ClaimState.HELD.value:
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="claim_not_held",
        )

    if claim.active_run_id and claim.active_run_id != run_id:
        # One mutating run at a time. A second run under a *current* generation is
        # the double-admission this module prevents, so it is refused even though
        # the generation checks out.
        logger.warning(
            "work claim: claim %s already bound to run %s; refusing run %s",
            claim_id,
            claim.active_run_id,
            run_id,
        )
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="run_already_bound",
            holder_ref=claim.active_run_id,
        )

    claim.active_run_id = run_id
    claim.heartbeat_at = _now()
    await session.flush()
    return ClaimReceipt(disposition=Disposition.ADMITTED, claim_id=claim.id, generation=claim.generation)


async def heartbeat(
    session: AsyncSession,
    *,
    claim_id: str,
    generation: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ClaimReceipt:
    """Extend a held claim's lease. Generation-checked like `bind_run`.

    A stale generation cannot refresh a lease: allowing it would let a superseded
    worker keep the row looking alive and starve the replacement it was handed
    over from.
    """
    stmt = select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == claim_id).with_for_update().execution_options(populate_existing=True)
    claim = (await session.execute(stmt)).scalar_one_or_none()
    if claim is None:
        raise WorkClaimError("unknown_claim", f"Claim {claim_id} does not exist.")

    if claim.generation != generation or claim.state != ClaimState.HELD.value:
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="stale_generation" if claim.generation != generation else "claim_not_held",
        )

    now = _now()
    claim.heartbeat_at = now
    claim.lease_expires_at = now + timedelta(seconds=lease_seconds)
    await session.flush()
    return ClaimReceipt(disposition=Disposition.ADMITTED, claim_id=claim.id, generation=claim.generation)


async def release_work(
    session: AsyncSession,
    *,
    claim_id: str,
    generation: int,
    reason: ReleaseReason,
    terminal_evidence: str,
) -> ClaimReceipt:
    """Release a claim so the issue can be legitimately claimed again.

    Releasing is what keeps sequential persona work possible: the developer's
    release is what lets the reviewer be admitted, so an issue's earlier merged
    PR does not permanently suppress later authorized work.

    Args:
        reason: Why the claim ended; kept on the row.
        terminal_evidence: What established that the work is over — a run status,
            a decision id, a check conclusion. Required (non-empty) because a
            release with no evidence is indistinguishable from a lease lapse, and
            this is the call that makes the issue re-claimable.

    Returns:
        `ADMITTED` when released. `DUPLICATE` when the claim is already released
        at this generation, so a retried terminal callback is not an error.
        `BLOCKED` on a stale generation.
    """
    if not str(terminal_evidence or "").strip():
        raise WorkClaimError(
            "missing_terminal_evidence",
            "Releasing a claim requires evidence that the work is over; an unevidenced release is a lease lapse by another name.",
        )

    stmt = select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == claim_id).with_for_update().execution_options(populate_existing=True)
    claim = (await session.execute(stmt)).scalar_one_or_none()
    if claim is None:
        raise WorkClaimError("unknown_claim", f"Claim {claim_id} does not exist; nothing to release.")

    if claim.generation != generation:
        # A stale release is refused, not applied. Applying it would release the
        # *replacement's* claim on behalf of a superseded run — handing the issue
        # to whoever asks next while the current owner is still working.
        logger.warning(
            "work claim: refusing release of claim %s at stale generation %s (current %s)",
            claim_id,
            generation,
            claim.generation,
        )
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="stale_generation",
        )

    if claim.state == ClaimState.RELEASED.value:
        return ClaimReceipt(
            disposition=Disposition.DUPLICATE,
            claim_id=claim.id,
            generation=claim.generation,
            reason="already_released",
        )

    now = _now()
    claim.state = ClaimState.RELEASED.value
    claim.active_run_id = None
    claim.release_reason = reason.value
    claim.released_at = now
    # Cleared so a released row cannot read as "lease still running".
    claim.lease_expires_at = None
    await session.flush()

    logger.info(
        "work claim: released claim %s generation=%s reason=%s evidence=%s",
        claim_id,
        claim.generation,
        reason.value,
        terminal_evidence,
    )
    return ClaimReceipt(disposition=Disposition.ADMITTED, claim_id=claim.id, generation=claim.generation)


async def force_handover(
    session: AsyncSession,
    *,
    claim_id: str,
    decision_id: str,
    resolver: RunLivenessResolver,
    effects_reconciled: bool,
    credentials_reconciled: bool,
    now: datetime | None = None,
) -> ClaimReceipt:
    """Take a claim away from its current owner. Every guard here is required.

    Four independent conditions, all of which must hold. Each one alone has a
    failure mode that produces two concurrent workers on one issue:

    1. **A recorded authorizing decision** (`decision_id`). Handover is an
       operator action, not something a competing dispatch can grant itself.
    2. **A positive `exited` verdict** for the bound run, from
       `activity.liveness`. `live` and `unverifiable` both block — and
       `unverifiable` is the important one: it is what a partitioned-but-working
       worker looks like, and treating it as gone is how the old and the
       replacement worker end up committing to the same branch.
    3. **Reconciled effects.** Outstanding branches/PRs/comments must be accounted
       for before a new generation starts producing more of them.
    4. **Reconciled credentials.** A database fence cannot revoke an already-issued
       GitHub installation token. If the caller cannot attest that outstanding
       credentials were reconciled, this refuses rather than advertising a fence it
       does not have.

    Returns:
        `ADMITTED` with the *new* generation when the handover is allowed — the
        claim is left `RELEASED` at an advanced generation, so the replacement
        goes through normal `claim_work` admission rather than being spliced in
        here. `BLOCKED` with a reason otherwise.
    """
    if not str(decision_id or "").strip():
        raise WorkClaimError("missing_decision", "A forced handover requires the id of the decision that authorized it.")

    stmt = select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == claim_id).with_for_update().execution_options(populate_existing=True)
    claim = (await session.execute(stmt)).scalar_one_or_none()
    if claim is None:
        raise WorkClaimError("unknown_claim", f"Claim {claim_id} does not exist.")

    if claim.state != ClaimState.HELD.value:
        return ClaimReceipt(
            disposition=Disposition.DUPLICATE if claim.state == ClaimState.RELEASED.value else Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="claim_not_held",
        )

    # --- Guard 2: proven exit. Checked before the attestations, because this is
    # the one the caller cannot simply assert. ---
    if claim.active_run_id:
        try:
            row = await resolver.resolve(claim.active_run_id)
        except Exception as exc:
            # Refuse, do not degrade. An unreadable liveness record is precisely
            # the case where assuming an exit is unsafe.
            logger.warning("work claim: liveness lookup faulted for run %s; blocking handover: %s", claim.active_run_id, exc)
            return ClaimReceipt(
                disposition=Disposition.BLOCKED,
                claim_id=claim.id,
                generation=claim.generation,
                reason="liveness_unavailable",
            )

        if row is None:
            # No ingress row means no positive evidence of an exit. It is NOT
            # evidence of absence — the row may not have been written yet.
            return ClaimReceipt(
                disposition=Disposition.BLOCKED,
                claim_id=claim.id,
                generation=claim.generation,
                reason="liveness_unknown",
            )

        verdict = compute_liveness(
            row.get("status"),
            str(row.get("arrived_at") or ""),
            now or _now(),
            row.get("status_updated_at"),
        )
        if verdict != "exited":
            logger.info(
                "work claim: blocking handover of claim %s — run %s is %s, not exited",
                claim_id,
                claim.active_run_id,
                verdict,
            )
            return ClaimReceipt(
                disposition=Disposition.BLOCKED,
                claim_id=claim.id,
                generation=claim.generation,
                reason=f"run_{verdict}",
            )

    # --- Guards 3 and 4: reconciliation attestations. ---
    if not effects_reconciled:
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="effects_not_reconciled",
        )
    if not credentials_reconciled:
        return ClaimReceipt(
            disposition=Disposition.BLOCKED,
            claim_id=claim.id,
            generation=claim.generation,
            reason="credentials_not_reconciled",
        )

    now_ts = now or _now()
    claim.state = ClaimState.RELEASED.value
    claim.active_run_id = None
    claim.release_reason = ReleaseReason.HANDOVER.value
    claim.released_at = now_ts
    claim.lease_expires_at = None
    # Advanced HERE, at the handover, not at the replacement's admission: the old
    # generation must stop being current the moment the handover is recorded, even
    # if no replacement is ever admitted. Also cleared of its event id, so the
    # superseded event cannot return a DUPLICATE receipt for the new generation.
    claim.generation = claim.generation + 1
    claim.claim_event_id = None
    await session.flush()

    logger.info(
        "work claim: forced handover of claim %s authorized by decision %s — generation advanced to %s",
        claim_id,
        decision_id,
        claim.generation,
    )
    return ClaimReceipt(disposition=Disposition.ADMITTED, claim_id=claim.id, generation=claim.generation)
