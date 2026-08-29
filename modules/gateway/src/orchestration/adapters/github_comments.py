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
from ..models import DecisionKind, OrchestrationNode
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

# The same cap the dashboard path applies to `reason` (`routes.py`:166, :277,
# `Query(max_length=2000)`). Applied here because the comment path has no
# FastAPI validator in front of it: without this, the two input paths would
# accept different-sized reasons, and an unbounded comment body would reach an
# append-only table.
_REASON_MAX_LEN = 2000


def _neutralize_marker(reason: str) -> str:
    """Make operator text unable to forge the `[input-path=...]` marker.

    Only `[` can open a marker, so replacing that one character wherever a
    marker-like token starts is sufficient, and is deliberately narrower than
    stripping all brackets: benign text ("see [PR #123]") survives intact while
    `[input-path=dashboard]` becomes `(input-path=dashboard]`, which no grep for
    the real marker matches. Case-insensitive so `grep -i` cannot be fooled
    either. The substitution is 1 char to 1 char, hence length-preserving, so it
    composes with the cap below in either order.

    Pure `str` operations, no `re`: an AST test asserts this module does not
    import `re`, because comment parsing belongs to the caller.
    """
    needle = f"[{_INPUT_PATH_PREFIX}="
    lowered = reason.lower()
    if needle not in lowered:
        return reason
    out = list(reason)
    start = lowered.find(needle)
    while start != -1:
        out[start] = "("
        start = lowered.find(needle, start + 1)
    return "".join(out)


def _stamp_input_path(input_path: InputPath, reason: str | None) -> str:
    """Compose the persisted `reason`, prefixed with the input path.

    The marker is machine-greppable provenance on an append-only audit row, so
    caller-supplied text must not be able to forge it — a comment body carrying
    a literal `[input-path=dashboard]` would otherwise persist a row that a
    marker-grep attributes to the dashboard path.
    """
    marker = f"[{_INPUT_PATH_PREFIX}={input_path.value}]"
    if not reason:
        return marker
    return f"{marker} {_neutralize_marker(reason)[:_REASON_MAX_LEN]}"


class GateAnswerStatus(StrEnum):
    """The outcome of one gate answer.

    Six members rather than a bool because the caller must respond differently to
    each, and collapsing them loses the distinction that matters: two of them are
    recorded evidence to surface, two are refusals that must stay
    indistinguishable from each other, and one is benign concurrency.
    """

    APPLIED = "applied"  # The node moved; a gate decision row was written
    REFUSED_NO_PERMISSION = "refused_no_permission"  # In-org but unauthorized; RECORDED
    REFUSED_ILLEGAL_TRANSITION = "refused_illegal_transition"  # Not at a gate; RECORDED
    REFUSED_UNKNOWN_IDENTITY = "refused_unknown_identity"  # No identity in org; nothing written
    REFUSED_NOT_FOUND = "refused_not_found"  # No such node in org; nothing written
    ALREADY_ANSWERED = "already_answered"  # Lost race — someone answered first


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
            "reason": _stamp_input_path(self.input_path, self.reason),
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
    )
    repo = repository_module.OrchestrationRepository(session)
    appended = await repo.append_decision(**record.to_append_kwargs())

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
