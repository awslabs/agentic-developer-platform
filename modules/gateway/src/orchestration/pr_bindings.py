"""The story-to-PR association: register it, validate it, read it back.

Issue #5301 (EPIC #4191). This module owns the missing contract between "an agent
opened a pull request" and "the engine knows which story that pull request
delivers". It is the only module that writes `orchestration_pr_bindings`.

--------------------------------------------------------------------------------
The bug this closes
--------------------------------------------------------------------------------

`results.GitHubEvidenceSource.merged_story` asks GitHub one question: was this
*issue* closed, and was the last thing that closed it a merged PR with green
checks? A PR whose body says `Issue #5049` rather than `Closes #5049` creates no
closing event, so the issue stays open and the query returns nothing — while the
implementation PR sits merged. The story waits forever on evidence that will never
arrive. That is the observed U11 shape, and it is a **missing association**, not
polling latency: no amount of waiting produces a closing event that was never
created, and manually closing the issue cannot supply the required PR closer
either.

So the fix is not a better query, a closing keyword, or a title search. It is that
the association was never recorded. Once it is, reconciliation reads the bound PR
directly and issue closure becomes a projection of delivery rather than its sole
authority.

--------------------------------------------------------------------------------
Where authority comes from — the load-bearing decision
--------------------------------------------------------------------------------

**The caller does not name the story it is binding to.** It names its own PR and
presents its run reference; `resolve_registration_target` derives tenant, flow,
node and attempt server-side from the `NODE_DISPATCHED` decision that dispatched
that run. A caller can therefore only ever bind a PR to the story its own run was
dispatched for, and it cannot bind on behalf of another tenant, another flow, or a
story it was never given.

This is the same inversion of trust `draft_binding.py` and `budget/run_binding.py`
apply, and for the same reason: the shared worker principal is identical for every
run on the platform, so a caller-asserted target reduces to "whatever the caller
typed". Read those modules before changing anything here.

What is deliberately NOT authority anywhere in this path:

* **PR title text and branch naming.** Discovery hints. `agent/issue-5301` is a
  convention a human can type by hand.
* **A plain `Issue #...` mention in the body.** The very thing whose absence of
  authority caused this bug; promoting it to evidence would re-introduce it from
  the other side.
* **Worker exit status.** A green worker is not merged code. `results.py` already
  refuses to equate the two and this module does not weaken that.
* **A self-reported approval.** Review evidence is read from the provider, never
  from the registering caller's claim about itself.

--------------------------------------------------------------------------------
Implementation PRs versus reviewer artifacts
--------------------------------------------------------------------------------

A reviewer run pushes its transcript to the same issue's branch family, so "a
merged PR referencing this issue" is satisfiable by a PR containing no
implementation at all. Completing a story on one would mark delivery done on the
strength of a review log. `BindingRole` makes the distinction explicit and
`completion_candidate` refuses anything that is not `IMPLEMENTATION`.

The authenticated registration route derives the role from the protected execution
persona. Story runs may be dispatched for reviewers as well as developers; the
node kind alone is not authority to label their PR as implementation. A caller
may additionally downgrade its registration to a reviewer artifact. Related
reviewer-PR identity defect: #4005.

--------------------------------------------------------------------------------
Heads, retries and replacement
--------------------------------------------------------------------------------

`head_sha` is what makes review/check evidence falsifiable. A new commit changes
the head, and a binding whose head has moved cannot inherit the eligibility its
previous head earned — otherwise an approval of one diff would silently authorize
a different one. `register_binding` therefore treats a head change on the same PR
as a **repair of that binding** (the common case: the agent pushes a fix to its own
open PR), refreshing the head so the next reconciliation re-verifies against it.

An explicitly authorized *replacement* — a genuinely different PR for the same
story — supersedes rather than deletes: the prior row keeps its provenance and
moves to `SUPERSEDED`, which permanently fences it from completing the new scope.
Nothing here deletes a binding.

--------------------------------------------------------------------------------
The seam to #5148 / #5149, stated rather than assumed
--------------------------------------------------------------------------------

#5148 (merge eligibility) and #5149 (merge execution and its receipt) are OPEN and
their modules do not exist at this commit — verified, not assumed. This module
therefore does not import them, does not invent their API, and does not stand up a
second merge controller or an independent merge store.

What it does instead is read merge/review/check state as **provider truth** about
the bound PR, which is independently verifiable today. The seam is
`MergeEvidence` below: when #5148/#5149 land they consume this same binding and can
supply their verified receipt, and `evidence_for_binding` gains a receipt source
without reconciliation changing shape. Owner: #5149 for the receipt contract.
Coordinate through #5134.

--------------------------------------------------------------------------------
What a binding cannot do
--------------------------------------------------------------------------------

It authorizes nothing on its own. It transitions no node, approves nothing, and
completes nothing. Completion still requires provider-verified merge, green
required checks and a non-author approving review — none of which the registering
agent can fabricate. That, plus the server-resolved target above, is what makes it
safe for a run to register its own binding under nothing more than its run
credential (`src/agentauth/pr_binding_routes.py`), instead of an admin permission
that would have to reach every ordinary user in every tenant to be usable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.logging import get_logger
from src.shared.models.base import utcnow

from .dispatch_pass import attempt_run_id, resolve_installation_id
from .models import (
    BindingRole,
    BindingState,
    DecisionKind,
    NodeKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from .state import ActorKind

logger = get_logger(__name__)

__all__ = [
    "BindingError",
    "BindingRefusal",
    "MergeEvidence",
    "PullRequestIdentity",
    "RegistrationTarget",
    "completion_candidate",
    "evidence_for_binding",
    "active_binding_for_node",
    "binding_summary",
    "hold_explanation",
    "recover_binding",
    "register_binding",
    "resolve_registration_target",
]


class BindingRefusal(StrEnum):
    """Why a registration or a completion read was refused.

    Stable machine-readable codes rather than prose, because two of these are
    surfaced to an operator through the story API and "which fail-closed arm fired"
    is the whole diagnostic value. Collapsing them into one opaque message is what
    made the original failure expensive to diagnose: `awaiting_merge` with a generic
    "waiting for a merged pull request" said nothing about whether the PR was
    unknown, unmerged, unreviewed or superseded.
    """

    MISSING_RUN_ID = "missing_run_id"  # Caller named no run to resolve against
    UNKNOWN_RUN = "unknown_run"  # No dispatch record for that run
    STALE_RUN = "stale_run"  # Dispatch is not the node's current attempt
    NOT_A_STORY = "not_a_story"  # Only story nodes carry implementation PRs
    TENANT_MISMATCH = "tenant_mismatch"  # Run belongs to another tenant
    REPOSITORY_MISMATCH = "repository_mismatch"  # PR is not in the dispatched repo
    AMBIGUOUS_CANDIDATE = "ambiguous_candidate"  # Several active bindings; refuse
    ALREADY_BOUND_ELSEWHERE = "already_bound_elsewhere"  # PR bound to another story
    INCOMPLETE_IDENTITY = "incomplete_identity"  # Missing immutable provider ids

    # Completion-read refusals, surfaced as the story's explicit hold reason.
    NO_BINDING = "no_binding"  # Nothing registered for this story
    NOT_IMPLEMENTATION = "not_implementation"  # Reviewer-artifact PR only
    NOT_MERGED = "not_merged"  # Bound PR is not merged
    HEAD_MOVED = "head_moved"  # Provider head differs from the bound head
    CHECKS_NOT_GREEN = "checks_not_green"  # Required checks not successful
    NO_INDEPENDENT_REVIEW = "no_independent_review"  # No non-author approval
    SUPERSEDED = "superseded"  # Binding was replaced
    SCOPE_CHANGED = "scope_changed"


class BindingError(Exception):
    """A registration could not be established. Always a refusal, never a default.

    `code` is a `BindingRefusal` value the route puts in the response body and the
    story API surfaces as the hold reason, so an operator can tell which arm fired
    without reading logs.
    """

    def __init__(self, code: BindingRefusal, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PullRequestIdentity:
    """The pull request being bound, as the provider identifies it.

    `provider_repository_id` and `provider_pr_node_id` are the immutable ids and
    are what the row keys on; `repo` and `pr_number` are the mutable display names.
    Both are required: a caller that cannot supply the immutable pair is refused
    (`INCOMPLETE_IDENTITY`) rather than bound on the strength of a name that a
    rename or transfer can re-point.
    """

    provider_repository_id: int
    provider_pr_node_id: str
    repo: str
    pr_number: int
    head_sha: str


@dataclass(frozen=True)
class RegistrationTarget:
    """The story a registration may bind to, resolved from server-written state.

    Every field here comes from the `NODE_DISPATCHED` decision, the node row, or the
    tenant's installation record — never from the caller.
    """

    org_id: str
    flow_id: str
    node_id: str
    attempt: int
    run_id: str
    repo: str
    issue: int
    installation_id: int
    accepted_scope: str | None = None


@dataclass(frozen=True)
class MergeEvidence:
    """Provider truth about a bound PR, as reconciliation needs it.

    The seam to #5148/#5149 (see the module docstring). Today every field is read
    from the provider; when the merge controller lands it can populate the same
    shape from its verified receipt, and `evidence_for_binding` gains a source
    without `results.py` changing.

    `approved_by_non_author` is separate from `approving_review_count` on purpose. A
    bot that merges its own PR after GitHub refuses its self-approval — the exact
    U11 shape — produces a merged PR with zero *independent* approvals. Counting
    reviews without excluding the author would accept it.
    """

    merged: bool
    head_sha: str
    checks_successful: bool
    approved_by_non_author: bool
    merge_commit_sha: str | None = None
    merged_at: str | None = None
    url: str | None = None
    provider_repository_id: int | None = None
    provider_pr_node_id: str | None = None


def binding_snapshot(binding: OrchestrationPullRequestBinding) -> dict:
    """Full revision retained in the existing append-only decision store."""
    names = (
        "id",
        "revision",
        "org_id",
        "flow_id",
        "node_id",
        "attempt",
        "run_id",
        "provider_repository_id",
        "provider_pr_node_id",
        "repo",
        "pr_number",
        "installation_id",
        "head_sha",
        "role",
        "state",
        "registered_by",
        "registered_by_kind",
        "recovery_reason",
        "superseded_reason",
        "accepted_scope",
    )
    return {name: getattr(binding, name) for name in names}


def binding_scope_matches(binding: OrchestrationPullRequestBinding, node: OrchestrationNode) -> bool:
    if not binding.accepted_scope:
        return False
    scope = json.loads(binding.accepted_scope)
    return scope.get("node") == {"kind": node.kind, "issue_ref": node.issue_ref, "title": node.title}


async def _accepted_scope(session: AsyncSession, node: OrchestrationNode, decision: OrchestrationDecision | None = None) -> str:
    query = select(OrchestrationAcceptedPlan).where(
        OrchestrationAcceptedPlan.org_id == node.org_id,
        OrchestrationAcceptedPlan.flow_id == node.flow_id,
    )
    if decision is not None:
        query = query.where(OrchestrationAcceptedPlan.created_at <= decision.created_at)
    plan = (await session.execute(query.order_by(OrchestrationAcceptedPlan.version.desc()).limit(1))).scalar_one_or_none()
    definition = {"kind": node.kind, "issue_ref": node.issue_ref, "title": node.title}
    if plan is not None:
        suffix = f"/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
        proposed = next((item for item in plan.plan_document.get("nodes", []) if item["address"].endswith(suffix)), None)
        if proposed is not None:
            definition = {key: proposed.get(key) for key in definition}
    dispatch = json.loads(decision.reason or "{}") if decision else {}
    return json.dumps(
        {
            "node": definition,
            "dispatch_decision_id": decision.id if decision else None,
            "root_decision_id": dispatch.get("root_decision_id"),
            "accepted_plan_id": plan.id if plan else None,
            "accepted_plan_hash": plan.plan_hash if plan else None,
            "accepted_plan_version": plan.version if plan else None,
        },
        sort_keys=True,
    )


def _record_revision(session: AsyncSession, binding: OrchestrationPullRequestBinding, *, actor_id: str, actor_kind: ActorKind) -> None:
    session.add(
        OrchestrationDecision(
            org_id=binding.org_id,
            flow_id=binding.flow_id,
            node_id=binding.node_id,
            kind=DecisionKind.PR_BINDING_CHANGED.value,
            actor_id=actor_id,
            actor_role=actor_kind.value,
            actor_kind=actor_kind.value,
            reason=json.dumps(binding_snapshot(binding)),
        )
    )


async def resolve_registration_target(
    session: AsyncSession,
    *,
    run_id: str | None,
    expected_org_id: str | None = None,
) -> RegistrationTarget:
    """Resolve which story a run may bind a PR to. Server-side, fail-closed.

    The caller supplies only a reference to its own run. Everything the binding is
    filed under is read from the `NODE_DISPATCHED` decision that dispatched it, so a
    caller cannot bind across tenants, flows or stories no matter what it sends.

    `expected_org_id` is a **defence in depth check, not the access control**. The
    tenant a binding is filed under is always the one on the resolved node row; when
    a caller arrives with its own authenticated tenant (the operator-plane recovery
    path) this asserts the two agree, so a cross-tenant run reference is refused
    loudly rather than resolving to another tenant's story. It is optional because the
    self-registration path has no independently authenticated tenant to compare
    against — there, the resolution *is* the scoping. Note that this is deliberately
    NOT `TokenContext.attributed_org_id`, which is caller-influenced and must never
    gate access (#4132).

    Raises:
        BindingError: `MISSING_RUN_ID`, `UNKNOWN_RUN`, `STALE_RUN`, `NOT_A_STORY`,
            or `TENANT_MISMATCH`.
    """
    asserted = (run_id or "").strip()
    if not asserted:
        raise BindingError(
            BindingRefusal.MISSING_RUN_ID,
            "This caller must assert the run it is registering on behalf of; no run reference was sent.",
        )

    # The dispatch decision is the authority. Scanning `NODE_DISPATCHED` rows for the
    # one whose `reason.run_id` matches is deliberate: `run_id` is not a column, and
    # adding one would be a second source of truth for a value the decision already
    # carries. Newest first, so a re-dispatched node resolves to its current attempt.
    decisions = (
        (
            await session.execute(
                select(OrchestrationDecision)
                .where(OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value)
                .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            )
        )
        .scalars()
        .all()
    )

    for decision in decisions:
        try:
            dispatch = json.loads(decision.reason or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(dispatch, dict) or dispatch.get("run_id") != asserted:
            continue
        if decision.node_id is None:
            continue

        node = (
            await session.execute(
                select(OrchestrationNode).where(
                    OrchestrationNode.id == decision.node_id,
                    OrchestrationNode.org_id == decision.org_id,
                )
            )
        ).scalar_one_or_none()
        if node is None:
            continue

        # Refused rather than silently resolving to the other tenant's story. An
        # authenticated caller presenting a run reference from a different tenant is
        # either misconfigured or probing; either way it must not learn that the run
        # exists by getting a different error than for an unknown one.
        if expected_org_id is not None and node.org_id != expected_org_id:
            raise BindingError(
                BindingRefusal.TENANT_MISMATCH,
                f"Run {asserted} belongs to a different tenant; a pull request cannot be bound across tenants.",
            )

        # A run whose dispatch is not the node's CURRENT attempt has been superseded
        # by a retry. Letting it register would bind a stale run's PR to live work,
        # which is the "old PR completes the new scope" failure this story forbids.
        # `attempt_run_id` is re-derived rather than trusted, matching `results.py`.
        if dispatch.get("attempt") != node.attempts or asserted != attempt_run_id(node.id, node.attempts):
            raise BindingError(
                BindingRefusal.STALE_RUN,
                f"Run {asserted} dispatched attempt {dispatch.get('attempt')} of this story, "
                f"but attempt {node.attempts} is current; a superseded run cannot register a binding.",
            )

        if node.kind != NodeKind.STORY.value:
            raise BindingError(
                BindingRefusal.NOT_A_STORY,
                f"Run {asserted} belongs to a {node.kind} node; only story nodes carry implementation pull requests.",
            )

        # The dispatch record carries `repo` and `issue` but not the installation, so
        # it is resolved from the tenant's own record through the shared fail-closed
        # resolver `results.py` and `dispatch_pass.py` already use. Reused rather than
        # re-derived for the reason its own docstring gives: a second implementation
        # of a fail-closed check drifts, and it drifts invisibly.
        installation_id = await resolve_installation_id(session, org_id=node.org_id)
        return RegistrationTarget(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            attempt=node.attempts,
            run_id=asserted,
            repo=str(dispatch.get("repo") or ""),
            issue=int(dispatch.get("issue") or 0),
            # Zero when the tenant's installation is absent or ambiguous. Never
            # treated as usable: reconciliation cannot call the provider without it,
            # so such a story holds with a stated reason instead of passing.
            installation_id=int(installation_id or 0),
            accepted_scope=await _accepted_scope(session, node, decision),
        )

    raise BindingError(
        BindingRefusal.UNKNOWN_RUN,
        f"Run {asserted} has no engine dispatch record, so there is no story to bind a pull request to.",
    )


def _resolve_role(declared: BindingRole | None) -> BindingRole:
    """Honor the role derived by the authenticated caller's protected persona.

    A story node can dispatch a reviewer. The route must pass REVIEWER_ARTIFACT
    for those runs; a caller-declared downgrade can only reduce eligibility.
    """
    return BindingRole.REVIEWER_ARTIFACT if declared is BindingRole.REVIEWER_ARTIFACT else BindingRole.IMPLEMENTATION


async def active_binding_for_node(
    session: AsyncSession,
    *,
    org_id: str,
    node_id: str,
    attempt: int,
) -> OrchestrationPullRequestBinding | None:
    """The one active binding for this node's current attempt, or None.

    Raises:
        BindingError: `AMBIGUOUS_CANDIDATE` when several active bindings exist for
            one attempt. That is refused rather than resolved by picking the newest:
            two active bindings mean the association is genuinely unknown, and
            guessing would complete a story on an arbitrary PR. An operator resolves
            it by superseding the wrong one.
    """
    rows = (
        (
            await session.execute(
                select(OrchestrationPullRequestBinding)
                .where(
                    OrchestrationPullRequestBinding.org_id == org_id,
                    OrchestrationPullRequestBinding.node_id == node_id,
                    OrchestrationPullRequestBinding.attempt == attempt,
                    OrchestrationPullRequestBinding.state == BindingState.ACTIVE.value,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    if len(rows) > 1:
        raise BindingError(
            BindingRefusal.AMBIGUOUS_CANDIDATE,
            f"Story {node_id} has {len(rows)} active pull-request bindings for attempt {attempt}; "
            "supersede the incorrect one before this story can complete.",
        )
    return rows[0]


async def active_bindings_for_flow(
    session: AsyncSession,
    *,
    org_id: str,
    flow_id: str,
) -> dict[str, OrchestrationPullRequestBinding | BindingRefusal]:
    """Every story's active binding in one flow, keyed by `node_id`.

    For the journey view, which needs all of them at once. One query rather than
    `active_binding_for_node` per node: the graph route already builds its dispatch
    and cost maps this way, and a per-node call would make the read linear in node
    count for a page that renders every node anyway.

    Ambiguity is reported per node rather than raised, because one story with two
    active bindings must not blank the whole journey view — the value for that node
    is `AMBIGUOUS_CANDIDATE`, and every other story still renders. `attempt` is not
    filtered here; the caller matches against each node's current attempt, exactly as
    it already does for dispatch records, so a superseded attempt's binding is not
    shown as current.
    """
    rows = (
        (
            await session.execute(
                select(OrchestrationPullRequestBinding).where(
                    OrchestrationPullRequestBinding.org_id == org_id,
                    OrchestrationPullRequestBinding.flow_id == flow_id,
                    OrchestrationPullRequestBinding.state == BindingState.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    found: dict[str, OrchestrationPullRequestBinding | BindingRefusal] = {}
    for row in rows:
        existing = found.get(row.node_id)
        if existing is None:
            found[row.node_id] = row
        elif isinstance(existing, OrchestrationPullRequestBinding) and existing.attempt == row.attempt:
            found[row.node_id] = BindingRefusal.AMBIGUOUS_CANDIDATE
        elif isinstance(existing, OrchestrationPullRequestBinding) and row.attempt > existing.attempt:
            # Different attempts are not ambiguous: the newer one is the candidate,
            # and the caller still checks it against the node's current attempt.
            found[row.node_id] = row
    return found


async def register_binding(
    session: AsyncSession,
    *,
    target: RegistrationTarget,
    pr: PullRequestIdentity,
    actor_id: str,
    actor_kind: ActorKind,
    declared_role: BindingRole | None = None,
    replaces_reason: str | None = None,
    recovery_reason: str | None = None,
) -> tuple[OrchestrationPullRequestBinding, bool]:
    """Bind a pull request to the story its run was dispatched for.

    The unique PR row is the current pointer. Registration and every revision
    append a snapshot to the existing decision history. A new attempt reuses that
    pointer while retaining earlier heads, run attribution and accepted scope.
    Exact retries write nothing. All changes serialize with completion on the node.

    Args:
        target: The server-resolved story. Never caller-supplied.
        pr: The pull request's provider identity.
        actor_id: Who registered it, for provenance.
        actor_kind: SERVICE for a self-registering run, HUMAN for a recovery.
        declared_role: A caller may downgrade itself to `REVIEWER_ARTIFACT`; it
            cannot upgrade. See `_resolve_role`.
        replaces_reason: When set, an authorized replacement: any other active
            binding for this attempt is superseded with this reason. Absent, an
            existing rival binding is left alone and surfaces as
            `AMBIGUOUS_CANDIDATE`, because silently replacing one would let a
            second PR take over a story with no record of the decision.
        recovery_reason: Set only on an attributed recovery of historical unbound
            work. Non-NULL marks the row as human-established.

    Returns:
        `(binding, created)` — `created` is False when an existing binding was
        returned or repaired, so a caller can report a retry as a retry.

    Raises:
        BindingError: `INCOMPLETE_IDENTITY`, `REPOSITORY_MISMATCH`,
            `ALREADY_BOUND_ELSEWHERE`, or `AMBIGUOUS_CANDIDATE`.
    """
    if not pr.provider_pr_node_id.strip() or pr.provider_repository_id <= 0 or pr.pr_number <= 0 or not pr.head_sha.strip():
        # Refused rather than bound on the mutable name alone. A binding keyed only
        # on `owner/name` is one a rename can re-point, which is the property an
        # association must not have.
        raise BindingError(
            BindingRefusal.INCOMPLETE_IDENTITY,
            "A binding requires the provider's immutable repository id and pull-request node id, plus a head commit.",
        )

    # The PR must live in the repository this story was dispatched into. Without
    # this, a caller holding a valid run reference could bind a PR from an unrelated
    # repository — the run resolves its story correctly and the PR is still wrong.
    if target.repo and pr.repo and pr.repo.lower() != target.repo.lower():
        raise BindingError(
            BindingRefusal.REPOSITORY_MISMATCH,
            f"Pull request {pr.repo}#{pr.pr_number} is not in {target.repo}, the repository this story was dispatched into.",
        )

    # Registration, replacement and completion use the same node lock. This
    # serializes distinct PRs for one story and fences attempts resolved before
    # a concurrent retry. Provider reads happen before taking this lock.
    node = (
        await session.execute(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.id == target.node_id,
                OrchestrationNode.org_id == target.org_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if node is None or node.attempts != target.attempt or node.flow_id != target.flow_id:
        raise BindingError(BindingRefusal.STALE_RUN, "The story attempt changed while registering its pull request.")
    if replaces_reason and actor_kind != ActorKind.HUMAN:
        raise BindingError(BindingRefusal.AMBIGUOUS_CANDIDATE, "Replacing an implementation pull request requires explicit human authority.")
    scope = target.accepted_scope or await _accepted_scope(session, node)
    if json.loads(scope)["node"] != {"kind": node.kind, "issue_ref": node.issue_ref, "title": node.title}:
        raise BindingError(BindingRefusal.SCOPE_CHANGED, "The accepted story definition changed since this run was dispatched.")

    existing = (
        await session.execute(
            select(OrchestrationPullRequestBinding)
            .where(
                OrchestrationPullRequestBinding.org_id == target.org_id,
                OrchestrationPullRequestBinding.provider_repository_id == pr.provider_repository_id,
                OrchestrationPullRequestBinding.provider_pr_node_id == pr.provider_pr_node_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.node_id != target.node_id:
        raise BindingError(BindingRefusal.ALREADY_BOUND_ELSEWHERE, "This pull request already implements a different story.")
    if existing is not None and existing.state == BindingState.SUPERSEDED.value:
        raise BindingError(BindingRefusal.SUPERSEDED, "This pull request was explicitly superseded and cannot be adopted again.")

    rivals = (
        (
            await session.execute(
                select(OrchestrationPullRequestBinding)
                .where(
                    OrchestrationPullRequestBinding.org_id == target.org_id,
                    OrchestrationPullRequestBinding.node_id == target.node_id,
                    OrchestrationPullRequestBinding.state == BindingState.ACTIVE.value,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    rivals = [row for row in rivals if existing is None or row.id != existing.id]
    if rivals and not replaces_reason:
        raise BindingError(
            BindingRefusal.AMBIGUOUS_CANDIDATE,
            "This story has another implementation pull request; an operator must explicitly authorize its replacement.",
        )
    next_role = _resolve_role(declared_role).value
    if existing is not None and declared_role != BindingRole.REVIEWER_ARTIFACT:
        # A service retry cannot promote a reviewer artifact. An attributed human
        # recovery may attest that the PR now carries implementation work.
        next_role = BindingRole.IMPLEMENTATION.value if actor_kind == ActorKind.HUMAN and recovery_reason else existing.role
    changing = existing is None or any(
        (
            existing.head_sha != pr.head_sha,
            existing.attempt != target.attempt,
            existing.accepted_scope != scope,
            existing.role != next_role,
            bool(rivals),
        )
    )
    if changing and node.state in {"passed", "superseded"}:
        raise BindingError(BindingRefusal.STALE_RUN, "A completed or superseded story cannot change its implementation binding.")

    def supersede_rivals():
        for rival in rivals:
            rival.state = BindingState.SUPERSEDED.value
            rival.superseded_reason = replaces_reason[:255]
            rival.superseded_at = utcnow()
            rival.revision += 1
            _record_revision(session, rival, actor_id=actor_id, actor_kind=actor_kind)

    if existing is not None:
        supersede_rivals()
        if changing:
            # Earlier snapshots are immutable decisions, so updating the current
            # pointer preserves the old attempt, head, scope and provenance.
            existing.attempt = target.attempt
            existing.run_id = target.run_id
            existing.head_sha = pr.head_sha
            existing.repo = pr.repo
            existing.pr_number = pr.pr_number
            existing.installation_id = target.installation_id
            existing.accepted_scope = scope
            existing.registered_by = actor_id
            existing.registered_by_kind = actor_kind.value
            existing.recovery_reason = recovery_reason
            existing.role = next_role
            existing.revision += 1
            existing.updated_at = utcnow()
            _record_revision(session, existing, actor_id=actor_id, actor_kind=actor_kind)
        await session.flush()
        return existing, False

    binding = OrchestrationPullRequestBinding(
        org_id=target.org_id,
        flow_id=target.flow_id,
        node_id=target.node_id,
        attempt=target.attempt,
        run_id=target.run_id,
        provider_repository_id=pr.provider_repository_id,
        provider_pr_node_id=pr.provider_pr_node_id,
        repo=pr.repo,
        pr_number=pr.pr_number,
        installation_id=target.installation_id,
        head_sha=pr.head_sha,
        role=next_role,
        state=BindingState.ACTIVE.value,
        registered_by=actor_id,
        registered_by_kind=actor_kind.value,
        recovery_reason=recovery_reason,
        revision=1,
        accepted_scope=scope,
    )
    try:
        async with session.begin_nested():
            session.add(binding)
            await session.flush()
    except IntegrityError:
        # Another story can race on the same immutable PR. Its unique index wins,
        # but that is a conflict, never successful registration for this story.
        raise BindingError(BindingRefusal.ALREADY_BOUND_ELSEWHERE, "This pull request was concurrently bound to another story.") from None
    supersede_rivals()
    _record_revision(session, binding, actor_id=actor_id, actor_kind=actor_kind)
    await session.flush()
    return binding, True


async def recover_binding(
    session: AsyncSession,
    *,
    org_id: str,
    node_id: str,
    pr: PullRequestIdentity,
    installation_id: int,
    actor_id: str,
    reason: str,
    replaces_reason: str | None = None,
) -> OrchestrationPullRequestBinding:
    """Attributed recovery of historical unbound work.

    For stories delivered before this contract existed, whose PR merged without a
    binding ever being registered. It is deliberately *not* automatic: a human with
    approval authority states which PR delivered the story and why, and the row
    records both. No title search adopts a candidate, and there is no direct DB
    edit path.

    The recovery still establishes only the *association*. It does not assert that
    the PR is merged, reviewed or green — reconciliation verifies all of that
    against the provider afterwards exactly as it does for a self-registered
    binding. So a recovery cannot complete a story whose evidence is missing; that
    story stays on an explicit hold with a stated reason. This is what keeps the U11
    reproduction honest: recording its binding does not accept its bot merge.

    Raises:
        BindingError: as `register_binding`, plus `NOT_A_STORY` / `UNKNOWN_RUN`
            when the node is not a recoverable story in this tenant.
    """
    node = (
        await session.execute(
            select(OrchestrationNode).where(
                OrchestrationNode.id == node_id,
                OrchestrationNode.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if node is None:
        raise BindingError(
            BindingRefusal.UNKNOWN_RUN,
            f"No story {node_id} in this tenant, so no binding can be recovered for it.",
        )
    if node.kind != NodeKind.STORY.value:
        raise BindingError(
            BindingRefusal.NOT_A_STORY,
            f"Node {node_id} is a {node.kind} node; only story nodes carry implementation pull requests.",
        )

    decision = (
        await session.execute(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value,
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    dispatch = json.loads(decision.reason or "{}") if decision else {}
    if dispatch.get("attempt") != node.attempts or dispatch.get("run_id") != attempt_run_id(node.id, node.attempts):
        raise BindingError(BindingRefusal.STALE_RUN, "Recovery requires the story's current dispatched attempt.")
    scope = json.loads(await _accepted_scope(session, node))
    scope.update(dispatch_decision_id=decision.id, root_decision_id=dispatch.get("root_decision_id"))
    target = RegistrationTarget(
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        attempt=node.attempts,
        run_id=attempt_run_id(node.id, node.attempts),
        repo=str(dispatch.get("repo") or ""),
        issue=int(dispatch.get("issue") or 0),
        installation_id=installation_id,
        accepted_scope=json.dumps(scope, sort_keys=True),
    )
    binding, _ = await register_binding(
        session,
        target=target,
        pr=pr,
        actor_id=actor_id,
        actor_kind=ActorKind.HUMAN,
        recovery_reason=reason,
        replaces_reason=replaces_reason,
    )
    logger.info(f"Recovered pull-request binding for story {node_id}: {pr.repo}#{pr.pr_number} attributed to {actor_id}")
    return binding


def completion_candidate(binding: OrchestrationPullRequestBinding | None) -> BindingRefusal | None:
    """Whether this binding is even eligible to be checked for completion.

    Returns the refusal reason, or `None` when the binding may proceed to evidence
    verification. Separated from `evidence_for_binding` so the cheap structural
    checks happen before any provider call, and so the reason is available to the
    story API when no call is made at all.
    """
    if binding is None:
        return BindingRefusal.NO_BINDING
    if binding.state == BindingState.SUPERSEDED.value:
        return BindingRefusal.SUPERSEDED
    if binding.role != BindingRole.IMPLEMENTATION.value:
        # A reviewer-artifact PR completes nothing, however merged and green it is.
        return BindingRefusal.NOT_IMPLEMENTATION
    return None


def evidence_for_binding(
    binding: OrchestrationPullRequestBinding,
    evidence: MergeEvidence | None,
) -> tuple[str | None, BindingRefusal | None]:
    """Decide completion from provider truth about the bound PR.

    Returns `(url, None)` when the story may complete, or `(None, refusal)` with the
    specific reason it may not. Every arm is a refusal rather than a default: an
    absent answer never becomes a pass.

    The four requirements, and why each is here:

    * **Merged.** An open PR has delivered nothing.
    * **Head unchanged.** If the provider's head differs from the bound head, new
      commits arrived after this binding was registered and any review or check
      evidence describes different code. Fresh evidence is required, which is what
      #5301's "a changed head requires fresh eligibility evidence" means in
      practice.
    * **Required checks successful.** Unchanged from the prior contract.
    * **An approving review from someone other than the author.** This is the arm
      the U11 reproduction fails, and deliberately so: its PR was merged by the
      reviewer bot *after GitHub refused its formal self-approval*, so it carries no
      independent approval. Accepting it would let a bot complete its own story, and
      the issue explicitly declines to authorize that merge.
    """
    if evidence is None:
        # No answer from the provider is not a pass. It is also not an error worth
        # failing the node over — the next tick asks again.
        return None, BindingRefusal.NOT_MERGED
    if not evidence.merged:
        return None, BindingRefusal.NOT_MERGED
    if evidence.provider_repository_id != binding.provider_repository_id or evidence.provider_pr_node_id != binding.provider_pr_node_id:
        return None, BindingRefusal.REPOSITORY_MISMATCH
    if not evidence.head_sha or evidence.head_sha != binding.head_sha:
        return None, BindingRefusal.HEAD_MOVED
    if not evidence.merge_commit_sha or not evidence.merged_at:
        return None, BindingRefusal.NOT_MERGED
    if not evidence.checks_successful:
        return None, BindingRefusal.CHECKS_NOT_GREEN
    if not evidence.approved_by_non_author:
        return None, BindingRefusal.NO_INDEPENDENT_REVIEW
    return evidence.url or f"https://github.com/{binding.repo}/pull/{binding.pr_number}", None


def hold_explanation(refusal: BindingRefusal) -> str:
    """Operator-facing prose for a hold, so a waiting story explains itself.

    The original failure was expensive to diagnose because `awaiting_merge` said
    only "waiting for the issue to be completed by a merged pull request" — which
    was true, unactionable, and in U11's case describing something that could never
    happen. Each reason below names what is missing and who can resolve it.
    """
    return {
        BindingRefusal.NO_BINDING: (
            "No pull request is registered for this story, so its merge cannot be verified. "
            "If the work was delivered before pull-request binding existed, an operator can record the "
            "implementing pull request through the attributed recovery path."
        ),
        BindingRefusal.SUPERSEDED: "The pull request bound to this story was superseded; the replacement must be registered before it can complete.",
        BindingRefusal.NOT_IMPLEMENTATION: "The pull request bound to this story is a reviewer artifact, which cannot complete delivery work.",
        BindingRefusal.NOT_MERGED: "The bound pull request is not merged yet.",
        BindingRefusal.HEAD_MOVED: (
            "The bound pull request has new commits since it was registered, so its earlier review and checks "
            "no longer describe the current code. Fresh review and check evidence is required."
        ),
        BindingRefusal.CHECKS_NOT_GREEN: "The bound pull request's required checks have not succeeded.",
        BindingRefusal.NO_INDEPENDENT_REVIEW: (
            "The bound pull request has no approving review from someone other than its author, so its merge "
            "is not independently reviewed. A reviewer other than the author must approve it."
        ),
        BindingRefusal.AMBIGUOUS_CANDIDATE: (
            "Several pull requests are bound to this story, so which one delivered it is unknown. An operator must supersede the incorrect binding."
        ),
        BindingRefusal.SCOPE_CHANGED: (
            "The accepted story scope changed after this run was dispatched. "
            "An operator must verify and recover its implementation binding for the current scope."
        ),
    }.get(refusal, f"Completion evidence is unavailable: {refusal.value}")


def binding_summary(binding: OrchestrationPullRequestBinding) -> dict[str, object]:
    """The bound PR as the story API surfaces it.

    Deliberately excludes `registered_by` and `recovery_reason`: those are the
    provenance record, and this payload is read under `USAGE_READ` on the graph
    route. Surfacing who attested to a binding under a spend-read permission is the
    same escalation `test_internal_plane_guard.py` pins for approval records.
    """
    return {
        "repo": binding.repo,
        "pr_number": binding.pr_number,
        "url": f"https://github.com/{binding.repo}/pull/{binding.pr_number}",
        "head_sha": binding.head_sha,
        "role": binding.role,
        "state": binding.state,
    }


async def refresh_reviewed_head(session, *, identity, binding_id, revision, accepted_scope, head_sha):
    """Refresh the same PR after an R1-verified review, inside K2 settlement.

    This trusted adapter receives the provider head already checked before the
    phase decision. It changes no scope, run provenance or PR identity. The
    existing revision history records the new pointer for the merge observer.
    """
    binding = await active_binding_for_node(session, org_id=identity.org_id, node_id=identity.node_id, attempt=identity.cycle)
    if (
        binding is None
        or binding.id != binding_id
        or binding.role != BindingRole.IMPLEMENTATION.value
        or binding.revision != revision
        or binding.accepted_scope != accepted_scope
    ):
        raise BindingError(BindingRefusal.STALE_RUN, "The reviewed PR binding changed before phase settlement.")
    # Binding writers serialize on this pointer. There is no provider I/O in the
    # settlement, and a conflicting revision rolls back the phase transition.
    await session.refresh(binding, with_for_update=True)
    if binding.revision != revision or binding.state != BindingState.ACTIVE.value or binding.accepted_scope != accepted_scope:
        raise BindingError(BindingRefusal.STALE_RUN, "The reviewed PR binding changed before phase settlement.")
    if binding.head_sha != head_sha:
        binding.head_sha = head_sha
        binding.revision += 1
        binding.updated_at = utcnow()
        _record_revision(session, binding, actor_id="system:review-cycle", actor_kind=ActorKind.SERVICE)
        await session.flush()
