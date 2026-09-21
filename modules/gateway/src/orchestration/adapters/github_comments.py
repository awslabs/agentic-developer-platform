"""GitHub-comment input adapter for gate answers (Issue #4209).

A human answers a gate in one of two ways: by pressing approve in the graph UI, or
by leaving a comment on the GitHub issue that carries the gate. This module makes
the second one a **first-class named input adapter** rather than an older path that
merely still functions — the distinction is the whole point of the story. Under
ruling D-R20 the GitHub-driven mode is the default and remains fully supported
indefinitely; the engine is a per-flow opt-in behind a fail-closed flag.

**One decision shape, two input paths.** :func:`build_gate_decision` is a pure
function that both paths call, so the row a comment writes and the row the
dashboard writes are the same shape, produced by the same code, differing only in
the recorded :class:`InputPath`. That is deliberately not "two writers that agree
today": a second construction site is how two surfaces drift, and the store story
made these rows append-only precisely so gate attribution cannot be rewritten
after the fact.

**Not a weaker door.** An input adapter that skipped an authorization step would be
a privilege-escalation path dressed as a convenience. So this module:

* resolves the commenting GitHub identity to a platform identity **server-side**,
  through ``user_identities`` scoped to the org, and never trusts a login name
  supplied in the comment body;
* mints the resulting context with ``is_admin=False`` **always**, so no comment can
  ever produce a platform admin (that claim is a token claim and a comment is not
  a token);
* applies the **same** ``Permission.PLAN_APPROVE`` check as the dashboard approve
  path, with ``target_org_id`` set to the resolved org;
* re-resolves the node **under that org** in SQL before writing anything, so a
  node id belonging to another tenant resolves to nothing.

**Refusals are recorded, not swallowed** — but only when recording itself discloses
nothing. Where the caller is a verified member of the org, a refusal is persisted
as ``TRANSITION_REJECTED`` (R-N2b: recorded rejections are the primary detector for
off-plan activity). Where the identity does not resolve in the org, or the node does
not exist in it, the refusal writes **nothing** and returns the *same* uniform
message either way — a distinguishable "no such node" versus "not permitted" would
let an outsider enumerate another tenant's graph by commenting.

**One write seam.** :func:`_gate_transition` is the only place this module changes a
node's state, mirroring ``dispatch.py::_dispatch_transition`` and
``tick.py::apply_guarded_transition``. Every change goes through
``state.transition()`` first, and the UPDATE is conditional on the observed state so
two people answering the same gate at once produce one answer, not two.

**Additive by construction.** Nothing here renders a tracker block, parses comment
markup, handles sentinels, or writes branch artifacts. Those belong to the
GitHub-driven flow and this story does not touch them — which is what makes AC-27's
no-observable-change guarantee checkable by reading the diff. Parsing a comment into
a :class:`GateAnswer` is the caller's job; this module starts from the already-parsed
intent.

Requirements: AC-27 (no observable change to the GitHub path), AC-31 / D-R20
(legacy emission stays at parity and is not deprecated), R-N2b (rejected *and*
recorded), R-Q9c / AC-15 (human-only gate edges).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.shared.models.base import utcnow
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext

from .. import repository as repository_module
from ..models import DecisionKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from ..state import ActorKind, NodeState, transition

logger = logging.getLogger("bedrockgateway.orchestration.adapters.github_comments")

__all__ = [
    "GateAnswer",
    "GateAnswerOutcome",
    "GateAnswerStatus",
    "GateDecisionRecord",
    "InputPath",
    "apply_gate_answer",
    "apply_gate_answer_for_context",
    "as_dashboard_decision",
    "build_gate_decision",
]


# The provider key in `user_identities` for GitHub logins. Matches the value the
# connections service already writes and reads (`admin/connections/service.py`),
# so this adapter resolves against the same rows the rest of the platform does
# rather than a second identity notion of its own.
_GITHUB_PROVIDER = "github"


# Returned verbatim for BOTH "this GitHub user is not a member of this org" and
# "no such node in this org". One shared constant rather than two similar strings,
# because the isolation property here is that the two cases are indistinguishable
# to the commenter — and two separately-worded messages drift into distinguishable.
_UNIFORM_REFUSAL = "this gate cannot be answered by this account"


# How long the server-minted context for the permission check is considered valid.
# It exists only to satisfy TokenContext's required field: this context is built
# in-process, used for exactly one `check_permission` call, and never signed,
# serialized, returned, or issued to any caller.
_CONTEXT_TTL = timedelta(minutes=5)


class InputPath(StrEnum):
    """How a human's gate answer reached the engine.

    Recorded for provenance — never read as authority. Both members go through
    the identical permission check, so branching on this value to decide what a
    caller may do would reintroduce exactly the second-class path this story
    exists to prevent.
    """

    DASHBOARD = "dashboard"  # The graph UI's approve/reject action
    GITHUB_COMMENT = "github_comment"  # A comment on the issue carrying the gate


# How the input path is carried on the persisted row. `orchestration_decisions`
# has no provenance column and this story ships **no migration**, so the path
# travels as a machine-greppable prefix on `reason` (Text, already nullable).
#
# A prefix rather than a new column is a real tradeoff, taken deliberately: the
# alternative is DDL on an append-only audit table for a field only an operator
# reads. If provenance later needs indexing, that is a migration in its own
# issue, and this format is what it would backfill from.
_INPUT_PATH_PREFIX = "input-path"
_PLAN_HASH_PREFIX = "plan-hash"

# The same cap the dashboard path applies to `reason` (`routes.py`:166, :277,
# `Query(max_length=2000)`). Applied here because the comment path has no
# FastAPI validator in front of it: without this, the two input paths would
# accept different-sized reasons, and an unbounded comment body would reach an
# append-only table.
_REASON_MAX_LEN = 2000


def _neutralize_marker(reason: str) -> str:
    """Make operator text unable to forge either persisted system marker.

    Only `[` can open a marker, so replacing that one character wherever either
    marker-like token starts is sufficient, and is deliberately narrower than
    stripping all brackets: benign text ("see [PR #123]") survives intact while
    `[input-path=dashboard]` becomes `(input-path=dashboard]`, which no grep for
    the real marker matches. The plan marker must be protected too: otherwise an
    unbound answer whose operator text starts with `[plan-hash=...]` would be
    indistinguishable from a revision-bound answer during retry detection.
    Case-insensitive so `grep -i` cannot be fooled either. The substitution is 1
    char to 1 char, hence length-preserving, so it composes with the cap below in
    either order.

    Pure `str` operations, no `re`: an AST test asserts this module does not
    import `re`, because comment parsing belongs to the caller.
    """
    lowered = reason.lower()
    out = list(reason)
    for prefix in (_INPUT_PATH_PREFIX, _PLAN_HASH_PREFIX):
        needle = f"[{prefix}="
        start = lowered.find(needle)
        while start != -1:
            out[start] = "("
            start = lowered.find(needle, start + 1)
    return "".join(out)


def _stamp_input_path(input_path: InputPath, reason: str | None, *, bound_plan_hash: str | None = None) -> str:
    """Compose the persisted `reason`, prefixed with the input path.

    The marker is machine-greppable provenance on an append-only audit row, so
    caller-supplied text must not be able to forge it — a comment body carrying
    a literal `[input-path=dashboard]` would otherwise persist a row that a
    marker-grep attributes to the dashboard path.
    """
    marker = f"[{_INPUT_PATH_PREFIX}={input_path.value}]"
    if bound_plan_hash is not None:
        marker += f" [{_PLAN_HASH_PREFIX}={bound_plan_hash.strip()}]"
    if not reason:
        return marker
    return f"{marker} {_neutralize_marker(reason)[:_REASON_MAX_LEN]}"


def _bound_plan_hash(reason: str | None) -> str | None:
    """Read the system-stamped plan revision from a successful decision.

    The marker is accepted only immediately after the trusted input-path prefix.
    Operator text is neutralized before persistence, so an unbound decision
    cannot forge this position and later be mistaken for a bound retry.
    """
    if not reason:
        return None
    for input_path in InputPath:
        prefix = f"[{_INPUT_PATH_PREFIX}={input_path.value}] [{_PLAN_HASH_PREFIX}="
        if reason.startswith(prefix):
            end = reason.find("]", len(prefix))
            if end != -1:
                return reason[len(prefix) : end]
    return None


class GateAnswerStatus(StrEnum):
    """The outcome of one gate answer.

    Nine members rather than a bool because the caller must respond differently
    to each, and collapsing them loses the distinction that matters: two of them
    are recorded evidence to surface, two are refusals that must stay
    indistinguishable from each other, one is benign concurrency, one is an
    idempotent replay, one is a precondition the caller asked for and did not
    get, and one is a precondition the caller did not ask for and needed.
    """

    APPLIED = "applied"  # The node moved; a gate decision row was written
    IDEMPOTENT_REPLAY = "idempotent_replay"  # The original bound result is returned
    REFUSED_NO_PERMISSION = "refused_no_permission"  # In-org but unauthorized; RECORDED
    REFUSED_ILLEGAL_TRANSITION = "refused_illegal_transition"  # Not at a gate; RECORDED
    REFUSED_UNKNOWN_IDENTITY = "refused_unknown_identity"  # No identity in org; nothing written
    REFUSED_NOT_FOUND = "refused_not_found"  # No such node in org; nothing written
    ALREADY_ANSWERED = "already_answered"  # Lost race — someone answered first
    # The caller bound the answer to a plan revision and that is not the revision
    # in force. RECORDED, for the same reason the permission refusal is: an
    # attempt to approve a plan that had already moved is exactly the off-plan
    # activity a reader of the decision log needs to see. Nothing is approved.
    REFUSED_STALE_PLAN = "refused_stale_plan"
    # This plan proposes an execution policy, and the answer named no revision, so
    # it cannot be the grant of one (#5331). Approving anyway would arm the graph
    # while the author's bounds stayed inert — unbounded execution under a plan its
    # author believes constrained. RECORDED, and remediable by re-answering with the
    # reviewed revision's hash. Nothing is approved.
    REFUSED_UNBOUND_POLICY_GRANT = "refused_unbound_policy_grant"


@dataclass(frozen=True)
class GateAnswer:
    """A human's already-parsed answer to one gate.

    Parsing a comment body into this is the caller's concern — comment markup,
    sentinels and tracker rendering all belong to the GitHub-driven flow, which
    this story does not modify. The adapter starts here so that no parsing
    semantics live in the engine.

    `github_user_id` is GitHub's **numeric** account id (as a string), not a
    login: logins are renameable and a renamed login could otherwise resolve to a
    different person's platform identity. `org_id` is resolved upstream from the
    repository the comment landed on and is never read from the comment body.
    """

    org_id: str
    node_id: str
    github_user_id: str
    approve: bool
    reason: str | None = None


@dataclass(frozen=True)
class GateDecisionRecord:
    """The decision one gate answer attributes — **the** one decision shape.

    Both input paths build this, so "the comment path and the dashboard path
    agree" is true by construction rather than by two writers happening to match.
    Frozen because a decision record is a statement about something that already
    happened; :func:`to_append_kwargs` is the only way it reaches the database, and
    the repository deliberately exposes no update counterpart.
    """

    org_id: str
    flow_id: str
    node_id: str
    kind: DecisionKind
    actor_id: str
    actor_role: str
    actor_kind: ActorKind
    input_path: InputPath
    # Present only for a successful revision-bound answer. It is stamped into
    # the append-only reason field so a response-lost retry can prove that the
    # original answer was for this exact plan rather than merely finding an old
    # same-actor/same-verb decision after a later amendment.
    bound_plan_hash: str | None = None
    reason: str | None = None
    rejection_reason: str | None = None
    from_state: str | None = None
    to_state: str | None = None

    def to_append_kwargs(self) -> dict[str, Any]:
        """Map 1:1 onto :meth:`OrchestrationRepository.append_decision`.

        The input path is stamped onto `reason` here (see `_INPUT_PATH_PREFIX`)
        rather than at each call site, so the provenance marker cannot be
        forgotten on one path and present on the other.
        """
        return {
            "org_id": self.org_id,
            "flow_id": self.flow_id,
            "node_id": self.node_id,
            "kind": self.kind.value,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "actor_kind": self.actor_kind.value,
            "reason": _stamp_input_path(self.input_path, self.reason, bound_plan_hash=self.bound_plan_hash),
            "rejection_reason": self.rejection_reason,
            "from_state": self.from_state,
            "to_state": self.to_state,
        }


@dataclass(frozen=True)
class GateAnswerOutcome:
    """What one :func:`apply_gate_answer` call did.

    `message` is what may be shown to the commenter. For the two isolation
    refusals it is `_UNIFORM_REFUSAL` verbatim, so the two cases cannot be told
    apart from outside.
    """

    status: GateAnswerStatus
    node_id: str
    message: str
    decision: GateDecisionRecord | None = None
    # The primary key of the appended row, when one was written. Carried on the
    # outcome rather than on `GateDecisionRecord` deliberately: the record is the
    # *shape* both input paths agree on, and `as_dashboard_decision`'s parity
    # assertion compares two records for equality — a per-row id on that dataclass
    # would make two structurally identical decisions unequal.
    decision_id: str | None = None

    @property
    def applied(self) -> bool:
        return self.status is GateAnswerStatus.APPLIED


def build_gate_decision(
    *,
    org_id: str,
    flow_id: str,
    node_id: str,
    actor_id: str,
    actor_role: str,
    input_path: InputPath,
    approve: bool,
    from_state: str,
    reason: str | None = None,
    bound_plan_hash: str | None = None,
) -> GateDecisionRecord:
    """Build the decision record for an *accepted* gate answer.

    Shared by both input paths — this is what "one decision shape" means
    operationally. `actor_kind` is always HUMAN here: answering a gate is a human
    act by definition, and `state.py` makes the edges out of `awaiting_gate`
    human-only so that the engine can never walk a node past a gate itself
    (AC-15).
    """
    return GateDecisionRecord(
        org_id=org_id,
        flow_id=flow_id,
        node_id=node_id,
        kind=DecisionKind.GATE_APPROVED if approve else DecisionKind.GATE_REJECTED,
        actor_id=actor_id,
        actor_role=actor_role,
        actor_kind=ActorKind.HUMAN,
        input_path=input_path,
        bound_plan_hash=bound_plan_hash,
        reason=reason,
        from_state=from_state,
        to_state=(NodeState.PASSED if approve else NodeState.REJECTED_AT_GATE).value,
    )


async def _resolve_platform_identity(
    session: AsyncSession,
    *,
    org_id: str,
    github_user_id: str,
) -> tuple[TokenContext, str] | None:
    """Resolve a GitHub account to a platform identity **inside one org**.

    The org filter is in SQL and non-optional. `user_identities` is unique per
    (provider, provider_user_id, org_id) since migration 021, so the same GitHub
    account may legitimately be linked in more than one tenant; selecting without
    the org filter and taking the first row would let a comment act in whichever
    tenant happened to sort first.

    Returns the minted context and its `team_id`, or None when this GitHub account
    has no identity in this org. Callers must treat None as
    indistinguishable from "no such node" — see `_UNIFORM_REFUSAL`.
    """
    identity = (
        await session.execute(
            select(UserIdentity).where(
                UserIdentity.org_id == org_id,
                UserIdentity.provider == _GITHUB_PROVIDER,
                UserIdentity.provider_user_id == github_user_id,
            )
        )
    ).scalar_one_or_none()

    if identity is None:
        return None

    context = TokenContext(
        user_id=identity.user_id,
        org_id=org_id,
        team_id=identity.team_id,
        department_id="",
        account_type="human",
        # Never True, on any path. A platform-admin claim comes from a verified
        # token; a comment is not a token. Hardcoded rather than derived so no
        # future edit can make it conditional.
        is_admin=False,
        expires_at=utcnow() + _CONTEXT_TTL,
    )
    return context, identity.team_id


async def _record_refusal(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    node_id: str,
    actor_id: str,
    actor_role: str,
    input_path: InputPath,
    reason: str | None,
    rejection_reason: str,
    from_state: str | None,
    to_state: str | None,
) -> tuple[GateDecisionRecord, str]:
    """Persist a refused answer as `TRANSITION_REJECTED`.

    Returns the record and the id of the row written, so a caller that must
    surface the evidence (the dashboard route, which turns it into an HTTP
    response) can point at it.

    Called only for principals already verified as members of `org_id`, so writing
    a row here reveals nothing to an outsider. A refusal that only logged would
    lose the evidence R-N2b requires to be queryable next to the decisions it was
    refused against.
    """
    record = GateDecisionRecord(
        org_id=org_id,
        flow_id=flow_id,
        node_id=node_id,
        kind=DecisionKind.TRANSITION_REJECTED,
        actor_id=actor_id,
        actor_role=actor_role,
        actor_kind=ActorKind.HUMAN,
        input_path=input_path,
        reason=reason,
        rejection_reason=rejection_reason,
        from_state=from_state,
        to_state=to_state,
    )
    repo = repository_module.OrchestrationRepository(session)
    appended = await repo.append_decision(**record.to_append_kwargs())
    return record, appended.id


async def _stale_plan_reason(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    expected_plan_hash: str,
) -> str | None:
    """Why this answer's plan precondition fails, or None when it holds.

    Read in the CALLER's transaction and immediately before the conditional
    UPDATE, which is the whole point: the comparison and the state change are then
    one atomic unit, so a plan amended after this read cannot be the plan the
    answer applied to. A client that re-reads over HTTP and then approves has a
    window between the two calls, and its approval is accepted whatever landed in
    between — that is the hole this closes and the reason the check cannot live in
    a CLI.

    An empty or whitespace-only expectation is a MISMATCH, not "no precondition".
    A caller reaching here has already stated the intent to be bound (the
    parameter is `None` for "unbound"), and the realistic way to arrive with an
    empty string is a shell substitution that produced nothing. Treating it as
    unbound would approve unconditionally for a caller who asked to be guarded —
    fail closed instead.

    Returns a human-readable reason, phrased for the operator who will read it on
    the refused decision row, or None when the expectation matches the plan in
    force.
    """
    expected = expected_plan_hash.strip()
    if not expected:
        return "the expected plan revision was empty, so it could not be checked"

    repo = repository_module.OrchestrationRepository(session)
    in_force = await repo.get_accepted_plan(org_id=org_id, flow_id=flow_id)
    if in_force is None:
        # Nothing is in force, so no revision can match. Reported as its own case:
        # "there is no plan of record" sends an operator somewhere different from
        # "your revision is behind".
        return f"no plan is in force for this flow, so revision {expected} could not be confirmed"
    if in_force.plan_hash != expected:
        # The in-force hash is NOT echoed. The caller already holds a hash for this
        # flow and is authorized to read `GET /flows/{id}/plans`, so this withholds
        # nothing they cannot fetch — but a refusal message is not the place to
        # hand back state, and keeping it out means this string cannot become the
        # way a client learns the current revision instead of re-reading it.
        return f"plan revision {expected} is not the revision in force (version {in_force.version})"
    return None


async def _matching_bound_answer(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    node_id: str,
    actor_id: str,
    approve: bool,
    expected_plan_hash: str,
) -> OrchestrationDecision | None:
    """Return this actor's original same-verb gate decision, if one exists.

    Consulted while holding the flow row lock, once the gate has already moved. The
    tuple ``(actor, verb, exact plan hash, gate)`` is the idempotency identity: a
    lost HTTP response can be replayed without appending a second decision, and a
    different actor or the opposite verb does not match and remains a conflict.

    **This identity deliberately does not depend on what is currently in force**
    (#5331), which is why the caller consults it *before* the staleness comparison
    rather than after. A recorded decision by this actor, with this verb, on this
    gate, bound to this exact hash is proof that this person made this decision about
    this document — a historical fact that no later amendment or policy grant can
    change. Requiring the bound hash to still match the plan of record would make
    every retry of a successful bound approval fail, because granting a proposed
    execution policy records a new plan version by design: the retry of a request
    whose effect already happened would be told its revision was stale.
    """
    kind = DecisionKind.GATE_APPROVED if approve else DecisionKind.GATE_REJECTED
    stmt = (
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == org_id,
            OrchestrationDecision.flow_id == flow_id,
            OrchestrationDecision.node_id == node_id,
            OrchestrationDecision.kind == kind.value,
            OrchestrationDecision.actor_id == actor_id,
            OrchestrationDecision.actor_kind == ActorKind.HUMAN.value,
        )
        .order_by(OrchestrationDecision.created_at.desc())
    )
    expected = expected_plan_hash.strip()
    for decision in (await session.execute(stmt)).scalars():
        if _bound_plan_hash(decision.reason) == expected:
            return decision
    return None


async def _gate_transition(
    session: AsyncSession,
    *,
    node_id: str,
    org_id: str,
    observed_state: str,
    approve: bool,
    reason: str | None,
) -> tuple[int, bool, str | None]:
    """The one place this module changes a node's state.

    Three guards, in order, all required — the same shape as
    ``dispatch.py::_dispatch_transition``, plus one this adapter needs that
    dispatch does not:

    1. The node must actually be **at a gate**. This is a narrowing of *which*
       edge this adapter may request, not a second copy of the transition table:
       ``running -> passed`` is legal for a HUMAN in `state.py` because that is how
       a green **evaluation** promotes a node with no gate required. A *gate
       answer* is a different act, and letting one take that edge would let a
       comment mark work that is still in flight as passed, with no gate ever
       having been raised. ``transition()`` cannot distinguish the two callers, so
       the caller that must be narrower states it here.
    2. ``transition()`` decides whether the edge out of the observed state is
       legal for a HUMAN actor — the vocabulary and the table stay its job.
    3. The UPDATE is conditional on ``state = :observed_state``, so a second
       answer that observed the same prior state matches 0 rows. Two people
       approving the same gate at once produce one transition.

    Returns ``(rows_affected, allowed, rejection_reason)``. ``(0, False, why)`` is
    an authority or vocabulary refusal the caller must record; ``(0, True, None)``
    is a lost race, which is normal and records nothing further.
    """
    if observed_state != NodeState.AWAITING_GATE.value:
        return (
            0,
            False,
            f"node is in '{observed_state}', not '{NodeState.AWAITING_GATE.value}'; only a node awaiting a gate can be answered",
        )

    target = NodeState.PASSED if approve else NodeState.REJECTED_AT_GATE
    result = transition(observed_state, target, actor_kind=ActorKind.HUMAN, reason=reason or "")

    if not result.allowed:  # pragma: no cover - unreachable while guard 1 stands
        # Currently unreachable: guard 1 has already established `awaiting_gate`,
        # and both edges out of it are legal for a HUMAN. Kept, and deliberately
        # not collapsed into an assert, because `transition()` owns the table: if a
        # future edit narrows those edges, this must refuse and record rather than
        # crash or — far worse — proceed to the UPDATE on a refused transition.
        return 0, False, result.rejection_reason

    stmt = (
        update(OrchestrationNode)
        .where(
            OrchestrationNode.id == node_id,
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.state == observed_state,
        )
        .values(state=result.new_state.value, updated_at=utcnow())
    )
    rows = (await session.execute(stmt)).rowcount or 0
    await session.flush()
    return rows, True, None


async def _proposed_policy_grant_target(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    node_id: str,
):
    """The plan whose proposed policy answering THIS node would grant, or None (#5331).

    One resolver, two callers, deliberately. The activation path needs to know
    whether this answer is a grant; the precondition path needs to know whether an
    *unbound* answer is about to arm a graph whose bounds would stay inert. Those two
    questions must never disagree — a refusal that triggered on a wider set than the
    grant would block ordinary wave gates, and one that triggered on a narrower set
    would let exactly the unbounded-execution case through. So neither caller decides
    it; both ask here.

    Returns the parsed in-force `LoopProposal` when all of these hold:

    * a plan is in force for this flow, and it carries a `proposed_execution_policy`;
    * that document's structural acceptance gate exists
      (`registration.acceptance_gate_address` — sole root, gate kind, `accept` ref);
    * the node being answered IS that gate.

    Returns None otherwise, which is the overwhelmingly common case: every policyless
    plan, and every wave or authored gate within a policy-bearing one.

    Raises `PolicyNotAcceptableError` when a policy is proposed but the document
    cannot be read back. Refused rather than ignored: a document we cannot parse is
    one whose bounds we cannot honour, and proceeding would arm the graph while
    discarding authority limits we could see were requested.
    """
    # Imported here rather than at module scope. `registration` imports `compile`,
    # which would make an adapter->registration->compile edge at import time in a
    # package whose layering `test_internal_plane_guard.py` and the module docstrings
    # both take care over. The call happens once per gate answer, so the lookup cost
    # is irrelevant next to keeping the import graph flat.
    from ..compile import PolicyNotAcceptableError
    from ..proposal import LoopProposal
    from ..registration import acceptance_gate_address

    repo = repository_module.OrchestrationRepository(session)
    in_force = await repo.get_accepted_plan(org_id=org_id, flow_id=flow_id)
    if in_force is None:
        return None

    document = in_force.plan_document or {}
    if document.get("proposed_execution_policy") is None:
        # The common case by a wide margin: no policy was proposed, so there is
        # nothing to grant and nothing an unbound answer could leave inert. Checked
        # on the raw dict before parsing, so a plan document from an older schema is
        # a cheap no-op rather than a parse error.
        return None

    try:
        proposal = LoopProposal.model_validate(document)
    except Exception as exc:  # noqa: BLE001 — translated below, cause preserved
        raise PolicyNotAcceptableError(
            f"this plan proposes an execution policy that cannot be read back ({exc}); the gate was not answered. "
            "Re-register the plan against the current schema."
        ) from exc

    gate_address = acceptance_gate_address(proposal)
    if gate_address is None:
        # The plan in force has no single dominating acceptance gate, so no gate
        # answer within it can be read as accepting the whole plan.
        return None

    # Resolve the answered node's own address and compare. The node row is the
    # authority for which node was answered; the document is the authority for which
    # address is the acceptance gate.
    answered = await repo.get_node(org_id=org_id, node_id=node_id)
    if answered is None:
        return None
    answered_address = f"{proposal.flow_slug}/{answered.epic_ref}/{answered.wave_ref}/{answered.node_ref}"
    if answered_address != gate_address:
        # A wave gate or an authored gate. Not this plan's acceptance, so not a
        # grant. Logged at info because an operator tracing "why is my policy not in
        # force" needs to see that the answer landed on a different gate.
        logger.info(
            "orchestration gate answer: node %s (%s) is not this plan's acceptance gate (%s); proposed policy stays inert",
            node_id,
            answered_address,
            gate_address,
        )
        return None

    return proposal


async def _activate_proposed_policy(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
    node_id: str,
    actor_id: str,
    actor_role: str,
    accepted_by_decision_id: str,
) -> bool:
    """Grant a draft's proposed execution policy, if this answer is its grant (#5331).

    The single place a demoted policy becomes authority. A draft registered with a
    policy carries it in `proposed_execution_policy`, a field
    `policy_admission.load_in_force_policy` contains no code to read — so the plan is
    reviewable in full while granting nothing. This promotes it into
    `execution_policy` and records a new accepted-plan version, which is the first
    moment anything in the system will enforce it as authority.

    **Every one of these must hold, and each rules out a different way authority
    could be granted by something other than a person's deliberate act:**

    * the caller established that this was an *approval* and that it was *bound* to
      an expected revision. A rejection is not a grant. An unbound approval is not
      one either — the approver never stated which document they were approving, so
      activating on their behalf would attribute authority to a review of unknown
      content. It does not reach here at all: the caller refuses it up front
      (`REFUSED_UNBOUND_POLICY_GRANT`), because approving without granting would arm
      the graph while the author's bounds stayed inert.
    * the answered node must be this plan's **acceptance gate**, and there must be a
      plan in force carrying a demoted policy. Both are decided by
      `_proposed_policy_grant_target`, which the unbound refusal consults too so the
      two cannot disagree about what counts as a grant. No policy, no-op — the
      acceptance of a policyless plan is byte-identical to what it was before this
      function existed.
    * the acceptor must be **human**. Not decided here: the proposal is handed to
      `compile.accept_execution_policy` unchanged, which refuses a non-human actor
      and is the same function the direct acceptance route uses. That is what keeps
      one place deciding who may grant authority, rather than two that agree today.
      `ActorKind.HUMAN` is correct at this call site because `build_gate_decision`
      already establishes that answering a gate is a human act by construction —
      `state.py` makes every progress edge out of `awaiting_gate` human-only, so the
      engine cannot reach this code path at all.

    Returns True when a policy was granted, False for the (overwhelmingly common)
    no-op. Raises `PolicyNotAcceptableError` when a policy exists but cannot be
    granted — never silently drops it, because a graph armed without its bounds is
    worse than one that refused to arm.
    """
    # Imported here rather than at module scope, for the same layering reason
    # `_proposed_policy_grant_target` defers its own imports.
    from ..compile import ApprovalContext, accept_execution_policy, plan_hash
    from ..registration import promote_proposed_policy

    proposal = await _proposed_policy_grant_target(session, org_id=org_id, flow_id=flow_id, node_id=node_id)
    if proposal is None:
        return False

    repo = repository_module.OrchestrationRepository(session)

    # Promote, then hand to the UNCHANGED acceptance path. `accept_execution_policy`
    # refuses a non-human acceptor and stamps the policy with this principal; the
    # stamp is content-derived plus principal, so the same human re-accepting the
    # same document produces the same stamp and therefore the same hash — which is
    # what makes the replay path above able to recognise a retry after promotion.
    #
    # `promote_proposed_policy` rather than an inlined `model_copy`: it is documented
    # as the inverse of `demote_proposed_policy` and as the ONLY way a demoted policy
    # becomes authority, and a second copy of the field move here would make that
    # claim false. Two implementations of one inverse can drift — and the drift would
    # surface as a plan armed with bounds that are not the ones the human reviewed.
    promoted = promote_proposed_policy(proposal)
    granted = accept_execution_policy(
        promoted,
        decision=ApprovalContext(org_id=org_id, actor_id=actor_id, actor_role=actor_role, actor_kind=ActorKind.HUMAN),
        decision_kind=DecisionKind.PLAN_ACCEPTED,
    )

    # A new version rather than a mutation of the one in force. The revision the
    # human bound their answer to stays readable exactly as they reviewed it —
    # `expected_plan_hash` named it, and rewriting it in place would make the
    # decision row reference a document that no longer exists. `record_accepted_plan`
    # supersedes the prior version under the flow row lock the caller already holds.
    new_plan = await repo.record_accepted_plan(
        org_id=org_id,
        flow_id=flow_id,
        plan_document=granted.model_dump(mode="json"),
        plan_hash=plan_hash(granted),
        accepted_by_decision_id=accepted_by_decision_id,
    )

    # The grant, not the policy. `policy_id` is a content digest and carries no
    # secret material (`ExecutionPolicy` forbids extra fields precisely so it cannot),
    # but the bounds themselves are the owner's business and do not belong in
    # operational logs.
    logger.info(
        "orchestration policy granted flow=%s org=%s principal=%s gate=%s plan_version=%s policy=%s",
        flow_id,
        org_id,
        actor_id,
        node_id,
        new_plan.version,
        (granted.execution_policy.policy_id if granted.execution_policy else None),
    )
    return True


async def apply_gate_answer(
    session: AsyncSession,
    answer: GateAnswer,
    *,
    access: AccessControl,
    input_path: InputPath = InputPath.GITHUB_COMMENT,
) -> GateAnswerOutcome:
    """Apply one already-parsed gate answer, if its author is permitted to.

    Args:
        session: Caller-owned session. Nothing is committed here, so the state
            change and its decision row land atomically or not at all.
        answer: The parsed answer. Its `org_id` is resolved upstream from the
            repository the comment arrived on, never from the comment body.
        access: The same access control the dashboard route uses, so the check is
            the same check rather than a similar one.
        input_path: Recorded provenance. Defaults to the GitHub comment path;
            the dashboard route passes its own.

    Returns:
        A :class:`GateAnswerOutcome`. Only `APPLIED` moved the node.
    """
    org_id = answer.org_id
    node_id = answer.node_id

    resolved_identity = await _resolve_platform_identity(session, org_id=org_id, github_user_id=answer.github_user_id)
    if resolved_identity is None:
        # No identity in this org. Nothing is read about the node and nothing is
        # written, and the message is the shared constant — an outsider learns
        # nothing about whether the node exists.
        logger.warning(
            "orchestration gate answer: no %s identity %s in org %s — refusing",
            _GITHUB_PROVIDER,
            answer.github_user_id,
            org_id,
        )
        return GateAnswerOutcome(
            status=GateAnswerStatus.REFUSED_UNKNOWN_IDENTITY,
            node_id=node_id,
            message=_UNIFORM_REFUSAL,
        )

    context, _team_id = resolved_identity

    return await apply_gate_answer_for_context(
        session,
        context=context,
        node_id=node_id,
        approve=answer.approve,
        reason=answer.reason,
        access=access,
        input_path=input_path,
        refusal_message=_UNIFORM_REFUSAL,
    )


async def apply_gate_answer_for_context(
    session: AsyncSession,
    *,
    context: TokenContext,
    node_id: str,
    approve: bool,
    reason: str | None,
    access: AccessControl,
    input_path: InputPath,
    refusal_message: str | None = None,
    expected_plan_hash: str | None = None,
) -> GateAnswerOutcome:
    """Apply a gate answer for an **already-resolved** platform identity.

    This is the shared core of both input paths, and the seam exists because the
    two paths differ in exactly one respect: how the acting identity is
    established. A GitHub comment carries an account id that must be resolved to a
    platform identity server-side (:func:`apply_gate_answer` does that, then calls
    this); the dashboard arrives with a verified Cognito context and has nothing to
    resolve. Everything *after* identity — the permission check, the at-a-gate
    narrowing guard, the state-conditional UPDATE, the decision append, and the
    recording of refusals — is this function, once, for both.

    That is what "one decision shape, two input paths" means operationally: the
    dashboard is not a second implementation that agrees with the comment path
    today, it is the same code with a different `input_path` stamped on the row.

    Args:
        session: Caller-owned session; nothing is committed here, so the state
            change and its decision row land atomically or not at all.
        context: The verified acting identity. `org_id` on it is the tenant, and it
            must never be built from caller-supplied data.
        node_id: The gate node being answered.
        approve: True approves (`-> passed`), False rejects (`-> rejected_at_gate`).
        reason: Operator text for the decision row.
        access: The same access control both paths use.
        input_path: Recorded provenance.
        refusal_message: Overrides the message on the two isolation refusals. The
            comment path passes `_UNIFORM_REFUSAL` so an outsider cannot tell "no
            such node" from "not a member"; the dashboard path leaves it None and
            gets a specific message, because its caller is an authenticated member
            of the tenant already and the route answers 404 either way.
        expected_plan_hash: When given, the answer is bound to this plan revision:
            it is compared against the plan in force for the node's flow **inside
            this transaction**, and a mismatch refuses without moving the node.
            `None` means "no precondition" and preserves the prior behaviour
            exactly, which is what keeps every existing caller unchanged.

    Returns:
        A :class:`GateAnswerOutcome`. Only `APPLIED` moved the node.
    """
    org_id = context.org_id

    # Re-resolve the node under the resolved org, in SQL. A node id from another
    # tenant resolves to nothing and is refused with the same message as an
    # unknown identity.
    node = (
        await session.execute(
            select(OrchestrationNode).where(
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.id == node_id,
            )
        )
    ).scalar_one_or_none()

    if node is None:
        logger.warning(
            "orchestration gate answer: node %s not found in org %s — refusing",
            node_id,
            org_id,
        )
        return GateAnswerOutcome(
            status=GateAnswerStatus.REFUSED_NOT_FOUND,
            node_id=node_id,
            message=refusal_message or f"no gate node {node_id!r} in this tenant",
        )

    actor_role = (await access.get_user_role(context))[0].value
    observed_state = node.state
    target_state = (NodeState.PASSED if approve else NodeState.REJECTED_AT_GATE).value

    # The same permission, at the same strength, as the dashboard approve path.
    try:
        await access.check_permission(context, Permission.PLAN_APPROVE, target_org_id=org_id)
    except (AccessDeniedError, InvalidScopeError) as exc:
        # In-org but unauthorized: recorded, because this is precisely the
        # off-plan-activity evidence R-N2b exists for. The commenter still gets
        # the uniform message.
        record, decision_id = await _record_refusal(
            session,
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node_id,
            actor_id=context.user_id,
            actor_role=actor_role,
            input_path=input_path,
            reason=reason,
            rejection_reason=f"{Permission.PLAN_APPROVE.value} is required to answer a gate: {exc}",
            from_state=observed_state,
            to_state=target_state,
        )
        logger.warning(
            "orchestration gate answer: user %s lacks %s in org %s — refused and recorded",
            context.user_id,
            Permission.PLAN_APPROVE.value,
            org_id,
        )
        return GateAnswerOutcome(
            status=GateAnswerStatus.REFUSED_NO_PERMISSION,
            node_id=node_id,
            message=refusal_message or f"{Permission.PLAN_APPROVE.value} is required to answer a gate",
            decision=record,
            decision_id=decision_id,
        )

    # --- the revision precondition, INSIDE this transaction ----------------
    # Checked after authority (an unauthorized caller must not learn which plan is
    # in force) and before the transition, so a refusal moves nothing.
    #
    # This is what a client-side re-read cannot do. A CLI that reads the plan, sees
    # the hash it expected and then approves has a window between those two calls
    # in which the plan can be amended; its approval is accepted anyway and the
    # human's decision is recorded against a document they never saw. Here the
    # comparison and the state change share one transaction and one session, so
    # an amendment that commits after this read cannot also be the plan this
    # answer applied to.
    # Serialize bound and unbound answers with draft replacement. Otherwise an
    # unbound answer can inspect a policyless draft, then release a newly replaced
    # policy-bearing graph while leaving its proposed bounds inert.
    await session.execute(select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == node.flow_id).with_for_update())
    await session.refresh(node)
    observed_state = node.state

    if expected_plan_hash is not None:
        # --- replay is decided BEFORE staleness, and only once the gate has moved --
        #
        # Ordering that matters as of #5331. Granting a proposed execution policy
        # records a NEW plan version, so the moment a bound approval succeeds the
        # hash it was bound to is no longer the hash in force — by design, and
        # correctly, because what is in force now includes authority the reviewed
        # revision only proposed. A caller whose response was lost then retries the
        # identical request and, under the previous ordering, would be told its
        # revision is stale: a refusal recorded against a decision that had already
        # taken effect, sending an operator to re-read a plan whose approval already
        # succeeded. Checking the replay first makes the retry idempotent across
        # promotion, which is the whole reason the field is hashed.
        #
        # Guarded on the gate having already moved, so this cannot short-circuit the
        # real precondition: while the node is still `awaiting_gate` nothing has been
        # applied, and staleness is exactly the question to ask.
        #
        # The identity is sound independently of what is in force. A recorded
        # decision by THIS actor, same verb, same gate, bound to THIS exact hash is
        # proof that this person made this decision about this document — a fact no
        # later amendment or grant can alter. A different actor, the opposite verb, or
        # a different hash does not match and remains a conflict.
        if observed_state != NodeState.AWAITING_GATE.value:
            replay = await _matching_bound_answer(
                session,
                org_id=org_id,
                flow_id=node.flow_id,
                node_id=node_id,
                actor_id=context.user_id,
                approve=approve,
                expected_plan_hash=expected_plan_hash,
            )
            if replay is not None:
                record = build_gate_decision(
                    org_id=org_id,
                    flow_id=node.flow_id,
                    node_id=node_id,
                    actor_id=replay.actor_id,
                    actor_role=replay.actor_role,
                    input_path=input_path,
                    approve=approve,
                    from_state=replay.from_state or NodeState.AWAITING_GATE.value,
                    reason=reason,
                    bound_plan_hash=expected_plan_hash,
                )
                logger.info(
                    "orchestration gate answer: replaying original decision %s for node %s",
                    replay.id,
                    node_id,
                )
                return GateAnswerOutcome(
                    status=GateAnswerStatus.IDEMPOTENT_REPLAY,
                    node_id=node_id,
                    message=f"gate {'approved' if approve else 'rejected'}",
                    decision=record,
                    decision_id=replay.id,
                )

        stale_reason = await _stale_plan_reason(
            session,
            org_id=org_id,
            flow_id=node.flow_id,
            expected_plan_hash=expected_plan_hash,
        )
        if stale_reason is not None:
            record, decision_id = await _record_refusal(
                session,
                org_id=org_id,
                flow_id=node.flow_id,
                node_id=node_id,
                actor_id=context.user_id,
                actor_role=actor_role,
                input_path=input_path,
                reason=reason,
                rejection_reason=stale_reason,
                from_state=observed_state,
                to_state=target_state,
            )
            logger.warning(
                "orchestration gate answer: node %s refused on plan precondition — %s",
                node_id,
                stale_reason,
            )
            return GateAnswerOutcome(
                status=GateAnswerStatus.REFUSED_STALE_PLAN,
                node_id=node_id,
                message=(
                    f"this gate was NOT answered: {stale_reason}. Re-read the plan, review the current "
                    "revision, then answer with that revision's hash."
                ),
                decision=record,
                decision_id=decision_id,
            )

    # --- an UNBOUND approval may not arm a graph whose bounds would stay inert ---
    #
    # The other half of #5331's demotion, and the one that is easy to miss. A plan
    # registered with an execution policy carries it in `proposed_execution_policy`,
    # which nothing enforces; only a *bound* approval promotes it. So an unbound
    # approval of that plan's acceptance gate would pass the gate, arm every root
    # behind it, and leave the policy inert — the graph would run under legacy
    # unbounded semantics while its author believes it constrained to their
    # repositories, actions, expiry and spend cap. That is strictly worse than either
    # refusing or granting, so it is refused.
    #
    # Deliberately NOT a silent promotion instead. The approver named no revision, so
    # there is no document they can be said to have reviewed, and granting standing
    # authority on their behalf over content they did not identify is the attribution
    # this story exists to prevent.
    #
    # Scoped by the same resolver the grant uses, so this cannot drift into refusing
    # answers that would never have been grants: every policyless plan (i.e. every
    # flow that exists today) and every wave or authored gate is unaffected, and the
    # `expected_plan_hash is None` guard means no currently-passing caller changes
    # behaviour unless its plan actually proposes a policy.
    if approve and expected_plan_hash is None:
        from ..compile import PolicyNotAcceptableError

        try:
            grant_target = await _proposed_policy_grant_target(session, org_id=org_id, flow_id=node.flow_id, node_id=node_id)
        except PolicyNotAcceptableError as exc:
            # Unreadable proposed authority. Refused on the same grounds, and by the
            # same reasoning as the bound path: bounds we cannot parse are bounds we
            # cannot honour.
            grant_target = None
            unreadable = str(exc)
        else:
            unreadable = None

        if grant_target is not None or unreadable is not None:
            rejection_reason = (
                unreadable
                if unreadable is not None
                else (
                    "this plan proposes an execution policy, which is granted only by an approval bound to the "
                    "revision that was reviewed; an unbound approval would arm the plan with its bounds inert"
                )
            )
            record, decision_id = await _record_refusal(
                session,
                org_id=org_id,
                flow_id=node.flow_id,
                node_id=node_id,
                actor_id=context.user_id,
                actor_role=actor_role,
                input_path=input_path,
                reason=reason,
                rejection_reason=rejection_reason,
                from_state=observed_state,
                to_state=target_state,
            )
            logger.warning(
                "orchestration gate answer: node %s refused — an unbound approval cannot grant this plan's proposed execution policy",
                node_id,
            )
            return GateAnswerOutcome(
                status=GateAnswerStatus.REFUSED_UNBOUND_POLICY_GRANT,
                node_id=node_id,
                message=(
                    f"this gate was NOT answered: {rejection_reason}. Read the plan, review the execution policy it "
                    "proposes, then answer with that revision's hash to accept the plan and its bounds together."
                ),
                decision=record,
                decision_id=decision_id,
            )

    rows, allowed, rejection_reason = await _gate_transition(
        session,
        node_id=node_id,
        org_id=org_id,
        observed_state=observed_state,
        approve=approve,
        reason=reason,
    )

    if not allowed:
        # The node is not at a gate (or the edge is otherwise illegal). Recorded:
        # the caller was authorized, so this is a real attempt worth reading back.
        record, decision_id = await _record_refusal(
            session,
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node_id,
            actor_id=context.user_id,
            actor_role=actor_role,
            input_path=input_path,
            reason=reason,
            rejection_reason=rejection_reason or "transition refused",
            from_state=observed_state,
            to_state=target_state,
        )
        logger.warning(
            "orchestration gate answer: refused for node %s (%s -> %s): %s",
            node_id,
            observed_state,
            target_state,
            rejection_reason,
        )
        return GateAnswerOutcome(
            status=GateAnswerStatus.REFUSED_ILLEGAL_TRANSITION,
            node_id=node_id,
            message=f"this gate is not awaiting an answer: {rejection_reason}",
            decision=record,
            decision_id=decision_id,
        )

    if rows == 0:
        # Someone else answered this gate between our read and our write. Their
        # decision row stands; writing a second one would make the audit trail
        # claim the gate was answered twice.
        logger.info(
            "orchestration gate answer: node %s already answered by a concurrent decision",
            node_id,
        )
        return GateAnswerOutcome(
            status=GateAnswerStatus.ALREADY_ANSWERED,
            node_id=node_id,
            message="this gate was already answered",
        )

    record = build_gate_decision(
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node_id,
        actor_id=context.user_id,
        actor_role=actor_role,
        input_path=input_path,
        approve=approve,
        from_state=observed_state,
        reason=reason,
        bound_plan_hash=expected_plan_hash,
    )
    repo = repository_module.OrchestrationRepository(session)
    appended = await repo.append_decision(**record.to_append_kwargs())

    # --- activate a proposed execution policy, if this answer is its grant ----
    # After the append, so the decision row this policy is accepted *by* already
    # exists and the new plan version can reference it; inside this transaction, so
    # the gate move, the decision and the grant are one atomic fact. A policy that
    # activated without its gate answer, or a gate answer whose grant was lost,
    # would each be a plan whose authority and whose approval record disagree.
    #
    # Every precondition is checked inside `_activate_proposed_policy`: approval
    # only, bound only, the acceptance gate only, and a human acceptor only. It is a
    # no-op for every gate answer in the system that is not exactly that.
    if approve and expected_plan_hash is not None:
        # Imported here for the same layering reason `_activate_proposed_policy`
        # defers its own imports: `compile` is below this adapter, not beside it.
        from ..compile import PolicyNotAcceptableError

        try:
            await _activate_proposed_policy(
                session,
                org_id=org_id,
                flow_id=node.flow_id,
                node_id=node_id,
                actor_id=context.user_id,
                actor_role=actor_role,
                accepted_by_decision_id=appended.id,
            )
        except PolicyNotAcceptableError as exc:
            # The policy the human reviewed cannot be granted — an expired expiry, a
            # tenant mismatch, a field `stamp_policy` refuses. Raised, not swallowed:
            # the alternative is committing an approval that armed the graph while
            # discarding its bounds, which is the "runs unbounded while its author
            # believes it constrained" failure the demotion exists to prevent. The
            # caller's transaction is not committed on an exception, so the gate move
            # and the decision row roll back with it and the gate stays answerable.
            logger.error(
                "orchestration gate answer: node %s approved but its proposed policy could not be granted: %s",
                node_id,
                exc,
            )
            raise

    logger.info(
        "orchestration gate answer: node %s %s by %s via %s",
        node_id,
        record.kind.value,
        context.user_id,
        input_path.value,
    )
    return GateAnswerOutcome(
        status=GateAnswerStatus.APPLIED,
        node_id=node_id,
        message=f"gate {'approved' if approve else 'rejected'}",
        decision=record,
        decision_id=appended.id,
    )


def as_dashboard_decision(record: GateDecisionRecord) -> GateDecisionRecord:
    """The same decision as it would have been recorded from the dashboard.

    Exists for the parity assertion in `test_github_adapter.py`: two records that
    differ *only* in `input_path` must be equal once that field is normalised. A
    helper rather than a hand-rolled `dataclasses.replace` in the test, so the
    property is stated in the module it constrains.
    """
    return replace(record, input_path=InputPath.DASHBOARD)
