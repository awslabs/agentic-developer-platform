"""Compile an approved loop proposal into orchestration-graph rows.

Issue #4199 (EPIC #4191, intent #4120). This module is the **only** code path that
creates orchestration nodes, and that exclusivity is the security property the
story is built on.

Agents never write these tables. They cannot: no DB credential exists in an agent
pod. An authoring agent submits a `LoopProposal` document, and this function
decides whether that document becomes state. Two checks stand between a document
and the graph, and neither is skippable from the outside:

1. **Re-validation.** `validate_proposal` runs again here, against the same rules
   the advisory CLI runs. The CLI is a convenience for the author — it runs in the
   author's environment and an author can simply not run it. This call is the
   control. AC-29 is the test that proves it: it calls `compile_proposal` directly
   with a document that fails validation, bypassing the CLI entirely, and asserts
   both that it raises and that zero rows land.

2. **Tenant ownership.** The org the plan lands in comes from the
   **server-resolved** approval context, never from the document. The document's
   declared `org_id` is *compared* against it, and a mismatch is **rejected**, not
   silently re-homed. Re-homing would be the worse failure: a plan authored for
   tenant A quietly becoming tenant B's state, with tenant B's operator's name on
   the decision record.

3. **Only an approval rewrites the plan of record.** A compile whose
   `decision_kind` is not in `genesis.APPROVAL_DECISION_KINDS` — #4528's
   `PLAN_DRAFTED`, and any non-approval kind added later — is refused if a plan is
   already in force for the flow. Enforced here rather than in the caller that
   needs it because a guard in a caller protects only that caller: review PR #4558
   reproduced both a plan-of-record rewrite *and* a dispatch of unapproved work
   through a direct call with a non-approval kind, since flow-scoped genesis
   (`_latest_approval_decision_id`) lets a pre-existing human approval root nodes
   appended afterwards.

**Atomicity.** All inserts happen inside a single `begin_nested()` savepoint, so a
failure part-way leaves nothing behind. Without it, a failure between the node
insert and the accepted-plan insert would leave nodes on the graph for a plan that
was never accepted — the graph would show work nobody approved, which is precisely
the trust property this EPIC exists to establish. A savepoint rather than a
`commit()` because **the caller owns the transaction**: gate approval records its
own decision alongside this compile, and the two must land together or not at all.
This module never commits. Same convention as `repository.py`.

**Idempotency (R-NF2).** Compiling the same proposal twice does not double-insert.
Nodes are matched by graph address and edges by endpoint pair, so a retried
approval — a dropped connection, a duplicate delivery — converges instead of
either duplicating the graph or dying on a unique-index violation.

**No migration.** Every table written here was created by the store story's
migration (`029_orchestration_graph.py`). `spec_revision` needs no column of its
own: `plan_document` stores the accepted document verbatim, and the document
carries its own spec revision. Adding a column for a field already inside the
stored document would create two copies of one value.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .execution_policy import PolicyRejectedError, stamp_policy
from .genesis import APPROVAL_DECISION_KINDS
from .models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationFlow
from .proposal import LoopProposal, Violation, split_address, validate_proposal
from .repository import OrchestrationRepository
from .state import ActorKind, NodeState

__all__ = [
    "ApprovalContext",
    "CompileResult",
    "ExpiredExecutionPolicyError",
    "NonApprovalSupersedeError",
    "PolicyNotAcceptableError",
    "ProposalRejectedError",
    "TenantMismatchError",
    "accept_execution_policy",
    "address_of",
    "compile_proposal",
    "plan_hash",
    "prepare_execution_policy",
    "require_unexpired_execution_policy",
    "upsert_edges",
    "upsert_nodes",
]


class ProposalRejectedError(RuntimeError):
    """Raised when a proposal is refused. Nothing has been written.

    Raises rather than returns, unlike `transition()` in `state.py`. The
    difference is deliberate and not an inconsistency: a rejected *transition* is
    evidence to persist (R-N2b, the primary detector for off-plan agent activity),
    so it must survive as a value. A rejected *proposal* has produced no state to
    reason about, and the caller's transaction must not proceed as though it had.
    An exception is the only outcome that cannot be accidentally ignored.
    """

    def __init__(self, message: str, violations: list[Violation] | None = None) -> None:
        super().__init__(message)
        self.violations = violations or []


class TenantMismatchError(ProposalRejectedError):
    """Raised when a document's declared `org_id` is not the approver's org.

    A subclass so a caller that only cares "was this refused?" catches
    `ProposalRejectedError`, while the tenant case stays separately catchable —
    it is a possible attack, not a typo, and a caller may want to alert on it
    rather than just report it back to the author.
    """


class NonApprovalSupersedeError(ProposalRejectedError):
    """Raised when a non-approval compile would supersede an in-force accepted plan.

    The plan of record is what answers "what did a human approve?", and only an
    approval may rewrite it. A compile whose `decision_kind` is not in
    `APPROVAL_DECISION_KINDS` — issue #4528's `PLAN_DRAFTED`, and any future
    non-approval kind — is refused rather than allowed to supersede.

    A subclass of `ProposalRejectedError` so a caller that only asks "was this
    refused?" catches it with everything else, while staying separately catchable:
    like `TenantMismatchError` this is a possible privilege escalation rather than
    an author's mistake, and deserves its own status code and alerting.
    """


class PolicyNotAcceptableError(ProposalRejectedError):
    """Raised when execution policy authority cannot be accepted (#5128).

    Causes include a submitted server-stamped field, a non-human acceptor, an
    unavailable credential reference and expired reviewed bounds. Submission
    routes retain the malformed-policy 422 contract. A revision-bound gate
    promotion is already past validation, so the controls route maps this expected
    state conflict to 409 and rolls back the tentative gate answer.
    """


class ExpiredExecutionPolicyError(PolicyNotAcceptableError):
    """Raised when reviewed execution bounds are already expired."""


@dataclass(frozen=True)
class ApprovalContext:
    """Who approved this plan, and for which tenant. **Server-resolved.**

    Every field here must come from the request's authenticated context, never
    from the proposal document. `org_id` in particular is the tenant the plan
    lands in — that is why the document's own `org_id` is only ever compared
    against this one, never used in its place.
    """

    org_id: str
    actor_id: str
    actor_role: str
    # Approving a plan is a human act. Defaulted rather than required because the
    # overwhelming majority of callers are the gate-approval route; a service
    # actor must say so explicitly.
    actor_kind: ActorKind = ActorKind.HUMAN
    # Free-text justification, carried verbatim onto the decision record.
    reason: str | None = None


@dataclass(frozen=True)
class CompileResult:
    """What the compile produced. Returned only on success.

    `already_compiled` is True when this call was a no-op because the identical
    document was already in force. Callers that report "N nodes created" need to
    distinguish that from a first compile, or a retried approval will claim to
    have created a graph it merely found.
    """

    flow_id: str
    plan_version: int
    decision_id: str
    plan_hash: str
    # Graph address -> node id, for callers that need to address what they made.
    node_ids: dict[str, str] = field(default_factory=dict)
    nodes_created: int = 0
    edges_created: int = 0
    already_compiled: bool = False


# Fields of `LoopProposal` that describe how the plan came to be rather than what
# it executes, and are therefore NOT part of its identity. See `plan_hash`.
HASH_EXCLUDED_FIELDS = frozenset({"description", "design_history"})

# Fields whose key is DROPPED from the canonical JSON when their value is absent,
# rather than serialised as `null`. Distinct from `HASH_EXCLUDED_FIELDS` above: these
# fields ARE part of the plan's identity when present (they carry authority), but a
# document that does not use them must hash exactly as it did before the field
# existed — otherwise every plan predating it rehashes on deploy and every in-flight
# fail-soft retry is refused 409 as a plan-of-record rewrite. See `plan_hash`.
#
# Named rather than inlined so a test asserting the backward-compatibility claim can
# reconstruct "what the older code hashed" from this list instead of hardcoding one
# key — a hardcoded key is how such a test starts failing the moment a second
# omitted field is added, reporting a regression where the property still holds.
HASH_OMITTED_WHEN_ABSENT = frozenset({"execution_policy", "proposed_execution_policy"})


def plan_hash(proposal: LoopProposal) -> str:
    """Stable SHA-256 of a proposal document.

    Canonicalised with sorted keys and no incidental whitespace so that two
    semantically identical documents hash identically regardless of field order —
    otherwise idempotency would depend on JSON key ordering, and a re-serialised
    resubmission of the same plan would look like a different plan.

    Note this hashes the document as *authored*, including its declared `org_id`.
    That is intended: the hash answers "is this the same document?", and a
    document differing only in declared tenant is not the same document.

    **`description` and `design_history` are excluded (#4885), and that exclusion
    is load-bearing twice over.**

    Semantically: they are provenance *about* how the plan came to be, not the
    plan's executable content. Two documents differing only in their use-case
    sentence describe the same graph, the same waves and the same dependencies —
    they are the same plan, and the hash answers exactly that question.

    Operationally: `plan_hash` is what idempotency compares. Including the new
    fields would change the hash of every document that carries them, so a
    fail-soft retry spanning the #4885 deploy — the worker retries by
    construction — would no longer match its own in-force plan. It would fall
    through the idempotency return and be refused 409 as a plan-of-record
    rewrite, turning a dropped connection into a permanent failure. `EXCLUDED`
    is a frozenset rather than an inline literal so a field added to it here
    cannot be forgotten by a reader who only greps for the field name.

    **`execution_policy` is deliberately NOT excluded (#5128)**, which is worth
    stating because the symmetry with the two fields above is misleading. Those are
    provenance; a policy is authority. Two documents differing only in what they
    authorize are *not* the same plan, and excluding the policy would make an
    amendment that widened a repository list or raised a spend cap hash identically
    to the narrower plan already in force — so it would hit the idempotency return
    and be silently discarded as a retry. The widening would appear to succeed and
    have no effect.

    This is safe for retries only because the policy's stamped id is *derived* from
    its content rather than minted per acceptance (`execution_policy.stamp_policy`).
    A minted id would reintroduce exactly the #4885 failure this docstring
    describes.

    **A policyless document omits the key rather than serialising it as `null`**,
    which is the other half of keeping #5128 retry-safe. `model_dump` emits
    `"execution_policy": null` for every legacy plan, and that key alone changes the
    canonical JSON — so every plan authored before this field existed would hash
    differently after the deploy, and an in-flight retry would be refused 409 as a
    plan-of-record rewrite. Dropping the key when there is no policy makes the
    legacy document byte-identical to what it was, so adding the field is a true
    no-op for the plans that do not use it. `exclude_none` would be the wrong tool
    here: it would also strip nulls from nested policy fields and from unrelated
    optional fields, changing hashes it has no business changing.

    **`proposed_execution_policy` is covered too, and omitted when absent (#5331)**,
    on both counts for the reasons above. Covered, because two drafts differing only
    in the authority they ask a human to grant are not the same draft: hashing them
    alike would make the wider proposal indistinguishable from a retry of the
    narrower one, so a human's bound acceptance of the revision they reviewed could
    arm a revision they did not. Omitted when absent, because every existing plan —
    accepted or draft — has no such key, and emitting `null` would rehash all of them
    on deploy and turn every in-flight fail-soft retry into a 409.

    Note what this means for binding, which is the point of the whole field: because
    a demoted policy is NOT stamped at registration, the hash computed over a draft
    carrying one *is* the hash that lands in force. So a policy-bearing plan becomes
    bindable to an exact revision, which is precisely what it was not while the
    policy had to be stamped by whoever compiled it.
    """
    document = proposal.model_dump(mode="json", exclude=HASH_EXCLUDED_FIELDS)
    # Both policy keys, by the same rule and for the same reason. Driven off the
    # named constant rather than written twice, so a third such field cannot be added
    # to the model and silently left out of this.
    for policy_field in HASH_OMITTED_WHEN_ABSENT:
        if document.get(policy_field) is None:
            document.pop(policy_field, None)
    # Adding an optional suite must not change hashes of pre-E1 accepted plans.
    # Explicit suites remain covered, so weakening one is never a retry.
    for node in document.get("nodes", []):
        if node.get("evaluation") is None:
            node.pop("evaluation", None)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_evaluation_acceptor(proposal, decision, in_force=None):
    if ActorKind(decision.actor_kind) is ActorKind.HUMAN:
        return
    previous = (in_force.plan_document or {}).get("nodes", []) if in_force else []
    if any(node.evaluation is not None for node in proposal.nodes) or any(node.get("evaluation") is not None for node in previous):
        raise PolicyNotAcceptableError("Only a human acceptance may establish, change or remove an evaluation specification")


def prepare_execution_policy(
    proposal: LoopProposal,
    *,
    decision: ApprovalContext,
    decision_kind: DecisionKind,
) -> LoopProposal:
    """Deterministically bind submitted policy bounds without consulting time.

    The stamp is content-derived and the UTC normalization is deterministic, so
    callers can hash this result and resolve an identical in-force acceptance
    under its lock before applying the wall-clock expiry guard. A caller creating
    a new accepted plan must call :func:`require_unexpired_execution_policy`
    before writing it.
    """
    if decision_kind != DecisionKind.PLAN_DRAFTED:
        require_evaluation_acceptor(proposal, decision)
        if proposal.proposed_execution_policy is not None:
            raise PolicyNotAcceptableError(
                "Proposed policy bounds cannot remain inert on an accepted plan. "
                "Accept the draft through its revision-bound gate, or submit an execution_policy for explicit human acceptance."
            )
    policy = proposal.execution_policy
    if policy is None:
        return proposal

    if ActorKind(decision.actor_kind) is not ActorKind.HUMAN:
        raise PolicyNotAcceptableError(
            f"a {decision.actor_kind.value if isinstance(decision.actor_kind, ActorKind) else decision.actor_kind!r} actor "
            f"cannot accept an execution policy (attempted via {decision_kind.value!r}); a model may propose one, but accepting "
            "delegated authority is a human act. Submit the plan without a policy, or have an authorized human accept it."
        )

    # Every writer uses UTC. Normalizing a legacy naive value here keeps both the
    # policy identity and the plan hash stable across retries on every host.
    expires_at = policy.expires_at if policy.expires_at.tzinfo is not None else policy.expires_at.replace(tzinfo=UTC)
    policy = policy.model_copy(update={"expires_at": expires_at})

    try:
        stamped = stamp_policy(policy, principal_id=decision.actor_id, org_id=decision.org_id)
    except PolicyRejectedError as exc:
        raise PolicyNotAcceptableError(str(exc)) from exc

    return proposal.model_copy(update={"execution_policy": stamped})


def require_unexpired_execution_policy(proposal: LoopProposal) -> None:
    """Refuse new authority whose exact reviewed expiry is no longer live."""
    policy = proposal.execution_policy
    if policy is None:
        return

    expires_at = policy.expires_at if policy.expires_at.tzinfo is not None else policy.expires_at.replace(tzinfo=UTC)
    now = utcnow()
    if expires_at <= now:
        raise ExpiredExecutionPolicyError(
            f"the execution policy this plan proposes expired at {expires_at.isoformat()} and cannot be accepted "
            f"(it is now {now.isoformat()}). Accepting it would arm the plan with bounds every dispatch is then denied "
            "under. The expiry is part of what was reviewed and is not extended here: request a new plan and accept that."
        )


def accept_execution_policy(
    proposal: LoopProposal,
    *,
    decision: ApprovalContext,
    decision_kind: DecisionKind,
) -> LoopProposal:
    """Bind policy authority and refuse already-expired bounds.

    This immediate path is used when no persisted-plan replay can exist, including
    revision-bound gate promotion. Direct compilation and amendment use the two
    phases separately so a response-lost retry can return its committed result
    even when the same bounds expire before the retry arrives.
    """
    prepared = prepare_execution_policy(proposal, decision=decision, decision_kind=decision_kind)
    require_unexpired_execution_policy(prepared)
    return prepared


async def compile_proposal(
    session: AsyncSession,
    proposal: LoopProposal,
    decision: ApprovalContext,
    *,
    decision_kind: DecisionKind = DecisionKind.PLAN_ACCEPTED,
    initial_states: dict[str, NodeState] | None = None,
) -> CompileResult:
    """Validate a proposal authoritatively and compile it to rows, atomically.

    This is the engine's single ingress for plan state. See the module docstring
    for why each guarantee below is structural rather than conventional.

    Args:
        session: The caller's session. Its transaction is **not** committed here —
            gate approval commits this compile together with its own writes.
        proposal: The document to compile.
        decision: Server-resolved approval context. `decision.org_id` is the
            tenant the plan lands in; the document's declared `org_id` is only
            compared against it.
        decision_kind: The kind of decision row this compile records. Defaults to
            `PLAN_ACCEPTED`, which is what every acceptance path wants. Issue
            #4528's draft registration passes `PLAN_DRAFTED` — a kind absent from
            `genesis.APPROVAL_DECISION_KINDS`, so the compiled graph exists and is
            readable while being unable to root a dispatch. Parameterised here
            rather than forked into a second compiler because a second
            "insert the proposal's nodes" is free to accept a document this one
            would refuse, which is exactly what AC-29 forbids.
        initial_states: Graph address -> the state that node is *created* in, for
            addresses this compile creates. Anything omitted uses the column
            default (`pending`). This is a creation-time value, not a transition:
            `transition()` is the authority for *changing* a node's state and is
            untouched by this. #4528's acceptance gate is born in `awaiting_gate`
            because there is no legal edge into that state from `pending`, so the
            alternative would be a second, unguarded writer of node state.

    Returns:
        A `CompileResult`. `already_compiled` is True when the identical document
        was already in force and nothing was written.

    Raises:
        ProposalRejectedError: The document failed validation. No rows written.
        TenantMismatchError: The document declares a different tenant than the
            approver's resolved org. No rows written.
        NonApprovalSupersedeError: `decision_kind` is not an approval kind and a
            plan is already in force for the flow. No rows written.
        PolicyNotAcceptableError: The document carries an execution policy that a
            non-human actor tried to accept, or that declared a server-stamped
            field. No rows written.
    """
    # --- Gate 1: authoritative re-validation -------------------------------
    # The advisory CLI may or may not have run. This is the control, and it runs
    # before anything opens a savepoint so a refused document touches no state.
    violations = validate_proposal(proposal)
    if violations:
        detail = "; ".join(str(violation) for violation in violations)
        raise ProposalRejectedError(
            f"loop proposal failed authoritative validation with {len(violations)} violation(s): {detail}",
            violations=violations,
        )

    # --- Gate 2: tenant ownership -----------------------------------------
    # Compared, never substituted. A mismatch is rejected rather than re-homed.
    if proposal.org_id != decision.org_id:
        raise TenantMismatchError(
            f"proposal declares org_id {proposal.org_id!r} but the approver's resolved org is {decision.org_id!r}; "
            "a proposal is never re-homed to the approver's tenant"
        )

    # --- Gate 2a: stamp the execution policy, if the document carries one -----
    # Runs BEFORE the hash and the document dump below, deliberately: the stamp is
    # part of the plan's executable content, so it must be inside what
    # `plan_hash` covers and inside what the plan store holds. Stamping afterwards
    # would persist an unstamped policy and hash a document that never existed.
    #
    # `stamp_policy` is content-derived (see `execution_policy`), so this is
    # idempotent in the way that matters: a resubmission of the same document
    # stamps to the same id and therefore the same `document_hash`, and the
    # idempotency return below still recognises it as a retry.
    proposal = prepare_execution_policy(proposal, decision=decision, decision_kind=decision_kind)

    repo = OrchestrationRepository(session)
    document = proposal.model_dump(mode="json")
    document_hash = plan_hash(proposal)

    # Everything below is one savepoint: nodes, edges, the decision and the
    # accepted-plan row land together or not at all. A partial compile would put
    # nodes on the graph for a plan nobody accepted.
    async with session.begin_nested():
        flow = await _resolve_flow(repo, proposal=proposal, org_id=decision.org_id)
        # A create/resubmit can resolve an existing flow. Serialize it with
        # continuation acceptance and bounded append before reading its policy;
        # otherwise this ingress could replace a shared plan mid-execution.
        await session.execute(
            select(OrchestrationFlow)
            .where(OrchestrationFlow.org_id == decision.org_id, OrchestrationFlow.id == flow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        in_force = await session.scalar(
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == decision.org_id,
                OrchestrationAcceptedPlan.flow_id == flow.id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        if in_force is not None and (in_force.plan_document or {}).get("execution_continuation"):
            raise ProposalRejectedError(
                "Shared worker continuations require the bounded append preview/accept path; "
                "a full proposal cannot discard their authority marker or invalidate active worker assignments."
            )

        # --- Idempotency (R-NF2) -------------------------------------------
        # An identical document already in force means this is a retry. Return
        # what exists rather than writing a second identical plan version.
        if in_force is not None and in_force.plan_hash == document_hash:
            existing = await repo.list_nodes(org_id=decision.org_id, flow_id=flow.id)
            return CompileResult(
                flow_id=flow.id,
                plan_version=in_force.version,
                decision_id=in_force.accepted_by_decision_id or "",
                plan_hash=document_hash,
                node_ids={address_of(flow.slug, node): node.id for node in existing},
                already_compiled=True,
            )

        # Consult the clock only after the locked replay check. A request that
        # committed before expiry but lost its response must return that result;
        # genuinely new authority still fails before any plan mutation.
        require_unexpired_execution_policy(proposal)

        if proposal.execution_policy is not None and proposal.execution_policy.user_credentials is not None:
            from src.shared.services.credential_resolver import CredentialNotFoundError

            from .user_credentials import validate_user_credential_authority

            try:
                await validate_user_credential_authority(session, policy=proposal.execution_policy, user_id=decision.actor_id)
            except CredentialNotFoundError as exc:
                raise PolicyNotAcceptableError("selected user credential is unavailable to the plan owner") from exc

        # --- Gate 3: only an approval may rewrite the plan of record ----------
        # Checked *after* the idempotency return above, deliberately: an identical
        # document writes nothing at all, so a retry must stay a no-op rather than
        # become a 409 the moment a plan is in force. That ordering is the whole
        # reason a fail-soft caller (issue #4528's worker) can retry safely.
        #
        # This lives here, in the primitive, rather than in the calling module that
        # needs it. `registration.py` has its own stricter refusal, but a guard in a
        # caller only protects that caller: reached directly with a non-approval
        # `decision_kind`, this function would supersede the in-force plan (a
        # plan-of-record rewrite) and — because `_latest_approval_decision_id` is
        # flow-scoped — leave the new nodes rooted by the *human's* pre-existing
        # approval, dispatching work nobody accepted under that human's identity.
        # Both escalations from review PR #4558 reproduce through that route, so the
        # invariant belongs where no caller can bypass it. `APPROVAL_DECISION_KINDS`
        # is imported from `genesis.py` rather than restated so this and the rule
        # that roots dispatch cannot disagree about what an approval is.
        if in_force is not None and decision_kind.value not in APPROVAL_DECISION_KINDS:
            raise NonApprovalSupersedeError(
                f"a {decision_kind.value!r} compile cannot supersede plan version {in_force.version}, which is in force "
                f"for flow {flow.slug!r}: only an approval decision may rewrite the plan of record. Use the amendment "
                "path (PLAN_APPROVE), or target a flow with no plan in force."
            )

        if decision_kind != DecisionKind.PLAN_DRAFTED:
            require_evaluation_acceptor(proposal, decision, in_force)
        node_ids, nodes_created = await upsert_nodes(
            repo,
            proposal=proposal,
            org_id=decision.org_id,
            flow_id=flow.id,
            initial_states=initial_states,
        )
        edges_created = await upsert_edges(repo, proposal=proposal, org_id=decision.org_id, flow_id=flow.id, node_ids=node_ids)

        # The decision is appended before the plan row so the plan can point at
        # it: `accepted_by_decision_id` is nullable only because one of the two
        # has to be inserted first, not because it is optional information.
        record = await repo.append_decision(
            org_id=decision.org_id,
            flow_id=flow.id,
            kind=decision_kind.value,
            actor_id=decision.actor_id,
            actor_role=decision.actor_role,
            actor_kind=ActorKind(decision.actor_kind).value,
            reason=decision.reason,
        )

        plan = await repo.record_accepted_plan(
            org_id=decision.org_id,
            flow_id=flow.id,
            plan_document=document,
            plan_hash=document_hash,
            accepted_by_decision_id=record.id,
        )

        return CompileResult(
            flow_id=flow.id,
            plan_version=plan.version,
            decision_id=record.id,
            plan_hash=document_hash,
            node_ids=node_ids,
            nodes_created=nodes_created,
            edges_created=edges_created,
        )


# --- Shared with amend.py ---------------------------------------------------
# The three helpers below are module-public (no leading underscore) because
# `amend.py` calls them. Issue #4200 amends a plan by reusing this compile path
# rather than reimplementing it: a second copy of "insert the proposal's nodes"
# could accept a document this one would refuse, and validation parity between
# the original and amended paths is exactly what AC-29 forbids breaking.


def address_of(flow_slug: str, node) -> str:
    """Reassemble a stored node's graph address from its components.

    The store holds the four segments denormalised (there is no container table to
    join out of), so the address is composed on read rather than selected.
    """
    return f"{flow_slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"


async def _resolve_flow(repo: OrchestrationRepository, *, proposal: LoopProposal, org_id: str):
    """Find the flow this proposal is for within the tenant, or create it.

    Matched by slug, scoped to `org_id`: the slug is the flow segment of every
    node's address, so it is what the document actually identifies. Two tenants
    may each have a `delivery-loop` flow, and they are different flows — which is
    why the lookup is over the tenant's flows and never global.

    The design-loop capture fields (#4885) are written **on creation only**. An
    existing flow keeps whatever it already has, which is the same rule
    `title` and `intent_ref` have always followed here: this function resolves a
    flow, it does not reconcile one. Updating them on every compile would let a
    later document silently rewrite the recorded design history of a flow whose
    gates a human already answered — and an amendment (`amend.py` reaches this
    same path) would overwrite the inception record of the plan it amends.
    """
    # Issue #4898: an indexed tenant-scoped lookup rather than scanning every flow
    # in the tenant. Same result, and it cannot be defeated by flow count — the
    # previous full scan was correct only while it stayed unbounded, which is why
    # `list_flows` carries a warning against adding a limit. `create_flow` closes
    # the remaining read-then-write race against the uniqueness index.
    existing = await repo.get_flow_by_slug(org_id=org_id, slug=proposal.flow_slug)
    if existing is not None:
        return existing

    return await repo.create_flow(
        org_id=org_id,
        slug=proposal.flow_slug,
        title=proposal.title,
        intent_ref=proposal.intent_ref,
        description=proposal.description,
        # Dumped to plain JSON, not handed over as the Pydantic model: this value
        # goes into a JSON column, and `mode="json"` is what turns the validated
        # `approved_at` datetimes back into the ISO strings the column stores and
        # the API re-serialises. `None` stays `None` — the absent case must not
        # become `{}`, which would render as a design history with no stages.
        design_history=(proposal.design_history.model_dump(mode="json") if proposal.design_history else None),
    )


async def upsert_nodes(
    repo: OrchestrationRepository,
    *,
    proposal: LoopProposal,
    org_id: str,
    flow_id: str,
    initial_states: dict[str, NodeState] | None = None,
) -> tuple[dict[str, str], int]:
    """Insert the proposal's nodes, reusing any that already exist by address.

    Reuse rather than insert-and-hope is what makes idempotency hold for a
    *partial* overlap, not just the identical-document case. The store's
    `uq_orchestration_nodes_address` index would reject a duplicate anyway, so the
    alternative is not "two nodes" but "an IntegrityError mid-transaction" — a
    retry after a partial failure would fail forever instead of converging.

    Every newly created node starts in `NodeState.PENDING`, which is the column
    default in `models.py`. Not passed explicitly: a literal here would be a
    second place the initial state is decided, and the two could disagree.

    `initial_states` overrides that default per address, and applies **only to
    nodes this call creates** — a node already on the graph keeps whatever state
    it reached, so a retried registration cannot reset a gate a human already
    answered back to `awaiting_gate`.
    """
    existing = {address_of(proposal.flow_slug, node): node for node in await repo.list_nodes(org_id=org_id, flow_id=flow_id)}

    node_ids: dict[str, str] = {}
    created = 0

    for proposed in proposal.nodes:
        present = existing.get(proposed.address)
        if present is not None:
            node_ids[proposed.address] = present.id
            continue

        _, epic_ref, wave_ref, node_ref = split_address(proposed.address)
        override = (initial_states or {}).get(proposed.address)
        node = await repo.add_node(
            org_id=org_id,
            flow_id=flow_id,
            epic_ref=epic_ref,
            wave_ref=wave_ref,
            node_ref=node_ref,
            kind=proposed.kind,
            title=proposed.title,
            issue_ref=proposed.issue_ref,
            state=override.value if override is not None else None,
        )
        node_ids[proposed.address] = node.id
        created += 1

    return node_ids, created


async def upsert_edges(
    repo: OrchestrationRepository,
    *,
    proposal: LoopProposal,
    org_id: str,
    flow_id: str,
    node_ids: dict[str, str],
) -> int:
    """Insert the proposal's edges, skipping any already present.

    Endpoints are resolved through `node_ids`, which validation has already
    guaranteed covers every edge endpoint (rule 3 rejects a dangling endpoint), so
    a `KeyError` here would mean validation and compile disagree — impossible
    while both call the same `validate_proposal`.
    """
    present = {(edge.from_node_id, edge.to_node_id) for edge in await repo.list_edges(org_id=org_id, flow_id=flow_id)}

    created = 0
    for proposed in proposal.edges:
        pair = (node_ids[proposed.from_address], node_ids[proposed.to_address])
        if pair in present:
            continue
        await repo.add_edge(org_id=org_id, flow_id=flow_id, from_node_id=pair[0], to_node_id=pair[1])
        present.add(pair)
        created += 1

    return created


# Re-exported so a caller asserting "compiled nodes start pending" imports the
# value from the same place the compiler does, rather than spelling the literal.
INITIAL_NODE_STATE = NodeState.PENDING
