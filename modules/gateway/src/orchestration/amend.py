"""Plan amendment: supersede an accepted plan with a new version, attributably.

Issue #4200 (EPIC #4191, intent #4120). Plans meet reality — a wave needs
splitting, unplanned work arrives, a story turns out to be two. Without a
first-class amendment operation the accepted plan and reality diverge and the plan
quietly stops being the source of truth. This module makes amendment cheap,
validated the same way the original was, attributed, and preserved as history.

**This is a second write path into promotion state** — the exact state agents must
never reach. That is why it does not simply reuse the compile path's assumptions:

- **Authorization is the same permission as gate approval**, never a weaker one.
  `Permission.PLAN_APPROVE` gates both. A softer door into the same room is the
  same hole, and the EPIC's central guarantee is that promotion state cannot be
  reached without an explicit approval authority. Enforced at the route
  (`routes.py`), which is also where the org is server-resolved.

- **Superseding is a normal event, not an error.** The prior plan version stays
  queryable forever: amendment inserts version N+1 and marks N superseded, and
  nothing ever rewrites a plan row in place. "What was approved at that gate"
  therefore stays answerable after any number of amendments — an accepted plan
  that can be edited proves nothing about what was accepted.

**Why validation is delegated, not reimplemented.** The amendment is a full
`LoopProposal` document, not a patch, and it is re-validated by the same
`validate_proposal` and compiled by the same in-transaction `compile_proposal`.
An amendment therefore *cannot* be less validated than an original (AC-29). A
patch format would have needed its own validator, and two validators drift — the
amended path would eventually accept a plan the original path would have refused,
which is the softer-door failure in a different disguise.

**What amendment adds on top of compile.** `compile_proposal` is deliberately
additive: it upserts the proposal's nodes and never touches a node the proposal
omits, because for a first compile there is nothing to omit. Amendment is where
absence becomes meaningful — a node dropped from the new plan must stop being
worked. This module supersedes dropped nodes, reconciles retained definitions and
replaces the live dependency topology. Prior plan documents retain the history.

**Preservation is the point (semantics 4).** A node whose address *and* definition
are unchanged keeps its state, its attempt count and its run history. Re-planning
a wave must not throw away work already accepted in it, so an already-`passed`
node is never reset by an amendment that does not mention it. `compile_proposal`'s
address-keyed upsert gives this for free — it reuses the existing row rather than
replacing it.

**Atomicity.** Everything happens in one `begin_nested()` savepoint. A half-
amended graph — nodes from two plan versions with no coherent state — is one of
the four blast radii the issue names. This module never commits: the caller owns
the transaction, same convention as `compile.py` and `repository.py`.

**No migration.** `orchestration_accepted_plans` is already versioned with a
`superseded_at` discriminator and `orchestration_decisions` is already
append-only, both from the store story's `029_orchestration_graph.py`.
`DecisionKind.PLAN_AMENDED` already exists in the vocabulary.
"""

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .compile import (
    ApprovalContext,
    ProposalRejectedError,
    TenantMismatchError,
    address_of,
    is_legacy_acceptance,
    legacy_naive_policy_plan_hash,
    plan_hash,
    prepare_execution_policy,
    require_accessible_product,
    require_evaluation_acceptor,
    require_unexpired_execution_policy,
    upsert_edges,
    upsert_nodes,
    verify_planning_references,
)
from .models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from .proposal import LoopProposal, validate_proposal
from .repository import OrchestrationRepository
from .state import ActorKind, NodeState, transition

logger = logging.getLogger("bedrockgateway.orchestration.amend")

__all__ = [
    "AmendResult",
    "AmendmentContext",
    "FlowNotFoundError",
    "amend_plan",
    "definition_change_refusal",
    "prerequisites_change_refused",
]


def prerequisites_change_refused(*, state: NodeState | str, before: set, after: set) -> bool:
    """Whether changing this node's prerequisites is refused, given its state.

    The rule: a node past `pending`/`ready` has begun, and its *incoming* edge set is
    what the engine used to decide it could begin. Changing that set after the fact
    either retroactively adds a prerequisite to work already in flight, or removes one
    the node was allowed to start on — neither is representable, so the amendment is
    refused and the author is told to use a new address instead.

    Only the incoming set matters. A started node may gain *successors* freely: that
    delays other work, which is the normal shape of an insertion, and it changes nothing
    about the decision to start this node.

    Pure, takes opaque hashables, and lives here beside the refusal it describes so the
    preview can import it rather than restate it (R-N2a). `amend_plan` compares node-id
    sets because that is what its edge rows hold; `amendment_impact` compares graph
    addresses because it has no ids for nodes that do not exist yet. Both are asking one
    question, and a second copy of it is how a preview comes to promise an acceptance
    that then refuses.
    """
    return NodeState(state) not in {NodeState.PENDING, NodeState.READY} and before != after


def definition_change_refusal(
    *,
    address: str,
    state: NodeState | str,
    attempts: int,
    current_kind: str,
    current_issue_ref: str | None,
    proposed_kind: str,
    proposed_issue_ref: str | None,
    current_document: dict | None = None,
    proposed_document: dict | None = None,
    reserved: bool = False,
) -> str | None:
    """Why reusing this address is refused, or None if the change is allowed.

    Returns the refusal message rather than a boolean so the preview reports the same
    sentence acceptance will raise, instead of a paraphrase a reader has to match up.

    Refusals, in the order `_reconcile_definitions` applies them:

    * A **superseded** address is spent. Reusing it would resurrect an execution
      identity that a prior amendment retired, and `SUPERSEDED` has no outgoing
      transition, so there is no state the row could legally move to.
    * **Kind** is execution identity. A story that becomes an eval keeps the row's
      attempts and evidence while changing what that evidence was supposed to prove.
    * **`issue_ref`** may be corrected only while the node is untouched. Re-pointing a
      started node at another issue re-homes work already attributed to the first.
    * Execution requirements cannot reinterpret a started node's retained history.
      New evidence criteria, input prerequisites or execution bindings need a new address.

    `title` is deliberately not here: it is prose, it changes nothing about execution,
    and `_reconcile_definitions` updates it unconditionally.
    """
    if NodeState(state) is NodeState.SUPERSEDED:
        return f"cannot reuse superseded node {address!r}; use a new node address"
    if current_kind != proposed_kind:
        return f"cannot change kind of node {address!r}; use a new node address"
    if current_issue_ref != proposed_issue_ref and (reserved or attempts or NodeState(state) not in {NodeState.PENDING, NodeState.READY}):
        return f"cannot change issue_ref of started node {address!r}; use a new node address"
    if proposed_document is not None and (reserved or attempts or NodeState(state) not in {NodeState.PENDING, NodeState.READY}):
        before = _execution_definition(current_document or {})
        after = _execution_definition(proposed_document)
        changed = sorted(key for key in before if before[key] != after[key])
        if changed:
            return f"cannot change execution requirements {changed} of started node {address!r} (state {state}); use a new node address"
    return None


def _execution_definition(document: dict) -> dict:
    definition = {
        key: document.get(key)
        for key in (
            "repository_identity",
            "provider_repository_id",
            "repository_connection",
            "execution_owner",
            "execution_adapter",
            "executor",
            "evaluation",
            "delivery_mode",
        )
    }
    definition.update({key: sorted(set(document.get(key) or [])) for key in ("criteria", "evidence_expectations")})
    definition["required_inputs"] = sorted({item["source"].strip() for item in document.get("required_inputs") or []})
    return definition


async def binding_reservations(
    session: AsyncSession, *, flow: OrchestrationFlow, nodes: list[OrchestrationNode], document: dict
) -> tuple[set[str], bool]:
    """Return node-local freezes separately from flow execution reservation."""
    started = {node.id for node in nodes if node.attempts or NodeState(node.state) not in {NodeState.PENDING, NodeState.READY}}
    reserved = set(
        (
            await session.scalars(
                select(OrchestrationExecution.node_id).where(
                    OrchestrationExecution.org_id == flow.org_id,
                    OrchestrationExecution.flow_id == flow.id,
                )
            )
        ).all()
    )
    reserved.update(
        (
            await session.scalars(
                select(OrchestrationPullRequestBinding.node_id).where(
                    OrchestrationPullRequestBinding.org_id == flow.org_id,
                    OrchestrationPullRequestBinding.flow_id == flow.id,
                )
            )
        ).all()
    )
    claims = (
        await session.scalars(
            select(OrchestrationWorkClaim).where(
                OrchestrationWorkClaim.org_id == flow.org_id,
                OrchestrationWorkClaim.owner_kind == "engine_flow",
                OrchestrationWorkClaim.owner_ref == flow.id,
            )
        )
    ).all()
    product_started = bool(reserved or claims or any(node.attempts or (node.kind == "story" and node.id in started) for node in nodes))
    claim_keys = {(claim.provider_repository_id, claim.issue_number) for claim in claims}
    claimed_issues = {claim.issue_number for claim in claims}
    accepted_nodes = {node["address"]: node for node in document.get("nodes", [])}
    for node in nodes:
        accepted = accepted_nodes.get(address_of(flow.slug, node), {})
        try:
            issue_number = int(str(accepted.get("issue_ref", node.issue_ref)).lstrip("#"))
        except ValueError:
            continue
        repository_id = accepted.get("provider_repository_id", node.provider_repository_id)
        if (repository_id, issue_number) in claim_keys or (repository_id is None and issue_number in claimed_issues):
            reserved.add(node.id)
    return started | reserved, product_started


def projection_mismatches(
    flow: OrchestrationFlow, document: dict, nodes: list[OrchestrationNode], *, allow_missing_legacy_repositories: bool = False
) -> list[str]:
    if "product_id" not in document:
        mismatches = ["flow product_id"] if flow.product_id is not None else []
    else:
        mismatches = ["flow product_id"] if flow.product_id != document["product_id"] else []
    for projection in ("origin_ref", "tracker_refs"):
        if getattr(flow, projection) != document.get(projection):
            mismatches.append(f"flow {projection}")
    previous = {node["address"]: node for node in document.get("nodes", [])}
    repository_fields = ("repository_identity", "provider_repository_id", "repository_connection")
    for node in nodes:
        accepted = previous.get(address_of(flow.slug, node))
        if accepted is None:
            continue
        if (
            allow_missing_legacy_repositories
            and accepted.get("repository_identity")
            and accepted.get("provider_repository_id")
            and all(getattr(node, key) is None for key in repository_fields)
        ):
            # Older accepted plans carried repository identity only in their
            # document. An authenticated amendment may hydrate that absent
            # projection via upsert_nodes; definition_change_refusal still
            # compares the proposal with the accepted document for reserved work.
            # Never reinterpret a conflicting or partially populated projection.
            continue
        if any(getattr(node, key) != accepted.get(key) for key in repository_fields):
            mismatches.append(address_of(flow.slug, node))
    return mismatches


class FlowNotFoundError(LookupError):
    """The flow does not exist **within the caller's tenant**.

    Raised identically whether the flow is absent altogether or belongs to
    another org, and the route maps it to 404 — never 403. A 403 would confirm
    that some other tenant owns that `flow_id`, which is existence disclosure: a
    caller could enumerate `flow_id`s and learn which ones are real by reading the
    status code. 404 for both cases leaks nothing.
    """


@dataclass(frozen=True)
class AmendmentContext:
    """Who is amending, and for which tenant. **Server-resolved.**

    Every field must come from the request's authenticated context. `org_id` in
    particular is the tenant whose flow is amended and the tenant the document's
    declared `org_id` is compared against — a document is never re-homed to the
    amender's tenant.

    Separate from `compile.ApprovalContext` despite the identical shape, because
    the two carry different `actor_role` semantics in the decision record and a
    future field on one should not silently appear on the other. `to_approval()`
    converts.
    """

    org_id: str
    actor_id: str
    # Snapshotted by the caller at amendment time. A copy, not a join: roles
    # change, and attribution must reflect the authority actually held when the
    # amendment was made, not whatever the actor holds when the row is later read.
    actor_role: str
    username: str = ""
    # Amending a plan is a human act. Defaulted rather than required for the same
    # reason as ApprovalContext: a service actor must say so explicitly.
    actor_kind: ActorKind = ActorKind.HUMAN
    # Why the plan is being amended, carried verbatim onto the decision record and
    # onto every supersede transition it causes.
    reason: str | None = None

    def to_approval(self) -> ApprovalContext:
        """The equivalent `ApprovalContext`, for the delegated compile path."""
        return ApprovalContext(
            org_id=self.org_id,
            actor_id=self.actor_id,
            actor_role=self.actor_role,
            username=self.username,
            actor_kind=self.actor_kind,
            reason=self.reason,
        )


@dataclass(frozen=True)
class AmendResult:
    """What the amendment produced. Returned only on success.

    `superseded_version` and `plan_version` are both reported because the decision
    record stores the pair, and a caller rendering "v1 → v2" needs both without a
    second query.

    `already_amended` is True when the call was a no-op because the identical
    document was already in force (R-NF2). A caller reporting "N nodes superseded"
    must distinguish that from a real amendment, or a retried submission will
    claim to have changed a graph it merely found.
    """

    flow_id: str
    plan_version: int
    superseded_version: int | None
    decision_id: str
    plan_hash: str
    node_ids: dict[str, str] = field(default_factory=dict)
    nodes_created: int = 0
    nodes_superseded: int = 0
    edges_created: int = 0
    already_amended: bool = False
    receipts_preserved: list[dict] = field(default_factory=list)
    receipts_dropped: list[dict] = field(default_factory=list)
    effective_limits_before: dict | None = None
    effective_limits_after: dict | None = None


SETTLED_EVALUATION_STATES = frozenset({NodeState.PASSED.value, NodeState.WAIVED.value, NodeState.SUPERSEDED.value})


async def settled_evaluation_addresses(session: AsyncSession, *, flow) -> frozenset[str]:
    """Addresses of this flow's evaluation nodes that have already concluded.

    Their evidence contract was checked by the verifier installed when they ran.
    Re-pinning the digest of a concluded evaluation is impossible (its definition
    is retained history) and pointless (it will never run again), so the
    validator is told not to re-check it. Read-only; the amendment's locked read
    of the same rows happens later, inside its savepoint.
    """
    rows = (
        await session.execute(
            select(OrchestrationNode).where(
                OrchestrationNode.org_id == flow.org_id,
                OrchestrationNode.flow_id == flow.id,
                OrchestrationNode.kind == "eval",
                OrchestrationNode.state.in_(SETTLED_EVALUATION_STATES),
            )
        )
    ).scalars()
    return frozenset(address_of(flow.slug, node) for node in rows)


async def amend_plan(
    session: AsyncSession,
    flow_id: str,
    proposal: LoopProposal,
    actor: AmendmentContext,
) -> AmendResult:
    """Supersede a flow's accepted plan with a new version, atomically.

    Args:
        session: The caller's session. **Not committed here** — the caller owns
            the transaction, so an amendment can land together with whatever else
            the request writes.
        flow_id: The flow to amend. Resolved with an `org_id` filter before
            anything is read.
        proposal: The full replacement plan document (not a patch).
        actor: Server-resolved amendment context. `actor.org_id` is authoritative.

    Returns:
        An `AmendResult`. `already_amended` is True when the identical document
        was already in force and nothing was written.

    Raises:
        FlowNotFoundError: No such flow in the caller's tenant. Maps to 404.
        ProposalRejectedError: The document failed authoritative validation. No
            rows written — an amendment cannot smuggle in a plan the original path
            would have refused (AC-29).
        TenantMismatchError: The document declares a different tenant than the
            amender's resolved org. No rows written.
    """
    repo = OrchestrationRepository(session)

    # --- Tenant isolation, before any read of plan state -------------------
    # The org filter is applied here rather than checked after loading, so a
    # cross-org flow_id is indistinguishable from a nonexistent one.
    flow = await repo.get_flow(org_id=actor.org_id, flow_id=flow_id)
    if flow is None:
        raise FlowNotFoundError(f"no orchestration flow {flow_id!r} in this tenant")

    # --- Validation parity (AC-29) -----------------------------------------
    # The same validator the original path runs, run before anything opens a
    # savepoint so a refused document touches no state. Delegating to
    # compile_proposal alone would not be enough: its rejection happens inside its
    # own savepoint, and we need the flow-level checks below to run against a
    # document already known to be well-formed.
    violations = validate_proposal(proposal, settled_evaluations=await settled_evaluation_addresses(session, flow=flow))
    if violations:
        detail = "; ".join(str(violation) for violation in violations)
        raise ProposalRejectedError(
            f"amendment failed authoritative validation with {len(violations)} violation(s): {detail}",
            violations=violations,
        )

    # --- Tenant ownership of the document ----------------------------------
    # Compared, never substituted — a document authored for another tenant is
    # rejected rather than quietly re-homed onto this amender's flow.
    if proposal.org_id != actor.org_id:
        raise TenantMismatchError(
            f"amendment declares org_id {proposal.org_id!r} but the amender's resolved org is {actor.org_id!r}; "
            "an amendment is never re-homed to the amender's tenant"
        )

    # An amendment addressed at a different flow than the one being amended would
    # file its nodes under this flow while their addresses claim another. Rejected
    # rather than reconciled: the address is the node's identity.
    if proposal.flow_slug != flow.slug:
        raise ProposalRejectedError(
            f"amendment declares flow_slug {proposal.flow_slug!r} but flow {flow_id!r} is {flow.slug!r}; an amendment must target the flow it amends"
        )

    # --- Stamp the amended execution policy (#5128) ------------------------
    # An amendment producing a new accepted version IS how a policy is amended, so
    # a re-submitted policy is re-stamped here and the new version carries it.
    #
    # The same deterministic preparation the original path uses, imported rather
    # than reimplemented. It runs before the hash so the stamp is inside what
    # idempotency compares; the time-dependent expiry check runs only after the
    # locked replay check below.
    submitted_proposal = proposal
    approval = actor.to_approval()
    proposal = prepare_execution_policy(proposal, decision=approval, decision_kind=DecisionKind.PLAN_AMENDED)
    legacy_document_hash = legacy_naive_policy_plan_hash(submitted_proposal, decision=approval)

    await verify_planning_references(session, org_id=actor.org_id, proposal=proposal)
    document = proposal.model_dump(mode="json")

    document_hash = plan_hash(proposal)

    # One savepoint for the whole amendment: the new plan row, the superseded
    # prior row, the superseded nodes, the new nodes and the decision land
    # together or not at all.
    async with session.begin_nested():
        # Serialize amendments before reading the current version, including flows
        # with no nodes yet. Dispatch/tick lock nodes in the same ID order below.
        await session.execute(
            select(OrchestrationFlow).where(OrchestrationFlow.org_id == actor.org_id, OrchestrationFlow.id == flow.id).with_for_update()
        )
        await session.refresh(flow)
        in_force = await repo.get_accepted_plan(org_id=actor.org_id, flow_id=flow.id)
        if in_force is not None:
            # A continuation may have committed while this request waited for
            # the flow lock. Do not inspect a pre-lock ORM identity-map snapshot.
            await session.refresh(in_force)
        if in_force is not None and (in_force.plan_document or {}).get("execution_continuation"):
            raise ProposalRejectedError(
                "Shared worker continuations require the bounded append preview/accept path; "
                "a full proposal cannot discard their authority marker or invalidate active worker assignments."
            )

        # --- Idempotency (R-NF2) -------------------------------------------
        # An identical document already in force means this is a retry — a dropped
        # connection, a double-submit. Return what exists rather than writing a
        # second version that differs from the first only in its number.
        if in_force is not None and in_force.plan_hash in {document_hash, legacy_document_hash}:
            existing = await repo.list_nodes(org_id=actor.org_id, flow_id=flow.id)
            preserved, dropped, limits_before, limits_after = await _saved_receipt_dispositions(session, in_force.accepted_by_decision_id)
            return AmendResult(
                flow_id=flow.id,
                plan_version=in_force.version,
                superseded_version=None,
                decision_id=in_force.accepted_by_decision_id or "",
                plan_hash=in_force.plan_hash,
                node_ids={address_of(flow.slug, node): node.id for node in existing},
                already_amended=True,
                receipts_preserved=preserved,
                receipts_dropped=dropped,
                effective_limits_before=limits_before,
                effective_limits_after=limits_after,
            )

        existing_nodes = list(
            (
                await session.scalars(
                    select(OrchestrationNode)
                    .where(OrchestrationNode.org_id == actor.org_id, OrchestrationNode.flow_id == flow.id)
                    .order_by(OrchestrationNode.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if in_force is not None:
            mismatches = projection_mismatches(
                flow,
                in_force.plan_document or {},
                existing_nodes,
                allow_missing_legacy_repositories=await is_legacy_acceptance(session, in_force),
            )
            if mismatches:
                raise ProposalRejectedError(f"accepted identity projection mismatch: {mismatches}; refresh before amending")
        started, product_started = await binding_reservations(
            session, flow=flow, nodes=existing_nodes, document=in_force.plan_document if in_force is not None else {}
        )
        if proposal.product_id != flow.product_id and product_started:
            raise ProposalRejectedError("cannot retarget Product after execution reservation; use a new flow")
        require_evaluation_acceptor(proposal, actor.to_approval(), in_force)
        if proposal.product_id is None and not await is_legacy_acceptance(session, in_force):
            raise ProposalRejectedError("new execution acceptance requires an accessible Product")
        if proposal.product_id is not None:
            await require_accessible_product(session, product_id=proposal.product_id, decision=approval)
        from .amendment_issue_publisher import verify_adopted_story_issues

        issue_violations = await verify_adopted_story_issues(
            session,
            org_id=actor.org_id,
            proposal=proposal,
            base_document=in_force.plan_document if in_force is not None else None,
        )
        if issue_violations:
            raise ProposalRejectedError(
                "adopted story issue validation failed: " + "; ".join(item.rule for item in issue_violations),
                violations=issue_violations,
            )

        # A response-lost retry is identified under the flow lock before time is
        # consulted. Only a genuinely new version must still have live bounds.
        receipts_preserved, receipts_dropped, effective_policy = await _receipt_dispositions(session, in_force, document)
        require_unexpired_execution_policy(proposal.model_copy(update={"execution_policy": effective_policy}))

        superseded_version = in_force.version if in_force is not None else None
        from .amendment_impact import _limits_of
        from .policy_admission import load_in_force_policy

        admission = await load_in_force_policy(session, org_id=actor.org_id, flow_id=flow.id)
        limits_before = _limits_of(admission.policy)
        limits_after = _limits_of(effective_policy)
        effective_limits_before = vars(limits_before) if limits_before is not None else None
        effective_limits_after = vars(limits_after) if limits_after is not None else None

        # --- Supersede the dropped nodes -----------------------------------
        # Unlike additive compilation, absence in the replacement plan
        # is meaningful. Done BEFORE the upsert so the "already on the graph" set
        # is the pre-amendment one — running it after would see the newly inserted
        # nodes too and, since they are all in the new plan, do nothing wrong but
        # cost a pointless second pass.
        proposed_addresses = {node.address for node in proposal.nodes}
        _reconcile_definitions(existing_nodes, proposal, base_document=in_force.plan_document if in_force is not None else {}, reserved_ids=started)
        nodes_superseded = _supersede_absent_nodes(
            existing_nodes,
            proposed_addresses=proposed_addresses,
            flow_slug=flow.slug,
            actor=actor,
        )
        # Every superseded node, not only the ones this amendment retired: the lane
        # settlement is idempotent (a concluded execution and a released claim are
        # skipped), and a lane left open by an earlier amendment is exactly what
        # blocks the successor this amendment is probably creating (#7800).
        await _settle_superseded_lanes(
            session,
            [node for node in existing_nodes if node.state == NodeState.SUPERSEDED.value],
            org_id=actor.org_id,
            created_version=(in_force.version + 1) if in_force is not None else 1,
        )

        # --- Reuse the compile path for everything additive -----------------
        # Nodes with an unchanged address keep their row, and therefore their
        # state, attempts and run history (semantics 4). New nodes land `pending`
        # from the column default.
        node_ids, nodes_created = await upsert_nodes(
            repo,
            proposal=proposal,
            org_id=actor.org_id,
            flow_id=flow.id,
        )
        # Executable edges describe only the current plan. Keeping removed edges
        # would strand successors behind nodes that are now superseded.
        desired = {(node_ids[e.from_address], node_ids[e.to_address]) for e in proposal.edges}
        existing_edges = await repo.list_edges(org_id=actor.org_id, flow_id=flow.id)
        before = {(e.from_node_id, e.to_node_id) for e in existing_edges}
        for node in existing_nodes:
            if address_of(flow.slug, node) not in proposed_addresses:
                continue
            if prerequisites_change_refused(
                state=node.state,
                before={source for source, target in before if target == node.id},
                after={source for source, target in desired if target == node.id},
            ):
                raise ProposalRejectedError(f"cannot change prerequisites of started node {address_of(flow.slug, node)!r}; use a new node address")
        for edge in existing_edges:
            if (edge.from_node_id, edge.to_node_id) not in desired:
                await session.delete(edge)
        await session.flush()
        flow.title = proposal.title
        from src.products.work_scope import assign_flow_project

        await assign_flow_project(session, flow=flow, product_id=proposal.product_id, project_id=proposal.project_id)
        flow.product_id = proposal.product_id
        flow.origin_ref = proposal.origin_ref.model_dump(mode="json") if proposal.origin_ref else None
        flow.tracker_refs = [ref.model_dump(mode="json") for ref in proposal.tracker_refs] if proposal.tracker_refs is not None else None
        edges_created = await upsert_edges(
            repo,
            proposal=proposal,
            org_id=actor.org_id,
            flow_id=flow.id,
            node_ids=node_ids,
        )

        # The decision is appended before the plan row so the plan can point at
        # it — same ordering constraint as the compile path. That ordering means
        # the decision's reason must name a version number that does not exist
        # yet, so it is derived from the same maximum `record_accepted_plan`
        # allocates from and cross-checked against the row actually written below.
        versions = [existing.version for existing in await repo.list_plan_versions(org_id=actor.org_id, flow_id=flow.id)]
        expected_version = (max(versions) + 1) if versions else 1

        record = await repo.append_decision(
            org_id=actor.org_id,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_AMENDED.value,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind=ActorKind(actor.actor_kind).value,
            reason=_amendment_reason(actor.reason, superseded_version, expected_version, nodes_superseded)
            + _receipt_disposition_detail(receipts_preserved, receipts_dropped, effective_limits_before, effective_limits_after),
        )

        # `record_accepted_plan` allocates the next version and marks the prior
        # one superseded. Not reimplemented here: the store story owns that
        # sequencing, and its unique index on (flow_id, version) is what makes a
        # concurrent double-amend fail loudly instead of producing two "version 2"
        # plans.
        plan = await repo.record_accepted_plan(
            org_id=actor.org_id,
            flow_id=flow.id,
            plan_document=document,
            plan_hash=document_hash,
            accepted_by_decision_id=record.id,
        )

        # The decision row is append-only, so a version number that disagreed with
        # the plan actually written could never be corrected. Assert instead: this
        # can only fire if the store's allocation stops matching the maximum it
        # allocates from, and inside the savepoint a raise costs nothing.
        if plan.version != expected_version:
            raise ProposalRejectedError(
                f"plan version allocation disagreed with the attributed version (attributed v{expected_version}, wrote v{plan.version}); "
                "the amendment was rolled back rather than recorded with wrong attribution"
            )

        return AmendResult(
            flow_id=flow.id,
            plan_version=plan.version,
            superseded_version=superseded_version,
            decision_id=record.id,
            plan_hash=document_hash,
            node_ids=node_ids,
            nodes_created=nodes_created,
            nodes_superseded=nodes_superseded,
            edges_created=edges_created,
            receipts_preserved=receipts_preserved,
            receipts_dropped=receipts_dropped,
            effective_limits_before=effective_limits_before,
            effective_limits_after=effective_limits_after,
        )


def _reconcile_definitions(
    existing_nodes: list[OrchestrationNode], proposal: LoopProposal, *, base_document: dict, reserved_ids: set[str] = frozenset()
) -> None:
    """Update unstarted routing without changing execution identity or history."""
    proposed_by_address = {node.address: node for node in proposal.nodes}
    previous = {node["address"]: node for node in base_document.get("nodes", [])}
    for node in existing_nodes:
        address = address_of(proposal.flow_slug, node)
        proposed = proposed_by_address.get(address)
        if proposed is None:
            continue
        refusal = definition_change_refusal(
            address=address,
            state=node.state,
            attempts=node.attempts,
            current_kind=node.kind,
            current_issue_ref=node.issue_ref,
            proposed_kind=proposed.kind,
            proposed_issue_ref=proposed.issue_ref,
            current_document=previous.get(address),
            proposed_document=proposed.model_dump(mode="json"),
            reserved=node.id in reserved_ids,
        )
        if refusal is not None:
            raise ProposalRejectedError(refusal)
        node.issue_ref = proposed.issue_ref
        node.title = proposed.title


def _supersede_absent_nodes(
    existing_nodes: list[OrchestrationNode],
    *,
    proposed_addresses: set[str],
    flow_slug: str,
    actor: AmendmentContext,
) -> int:
    """Transition nodes absent from the new plan to `superseded`. Returns the count.

    Every state change goes through `transition()` (R-N2a) — this function never
    assigns `node.state` on its own authority. That matters most for the
    `passed -> superseded` edge, which the vocabulary marks **human-only**: a
    service actor amending a plan cannot discard accepted work, and the guard, not
    this function, is what enforces it.

    A node already in a terminal-and-superseded state is skipped rather than
    re-transitioned: `SUPERSEDED` has no outgoing edges, so attempting it would be
    rejected, and a rejected transition is evidence of off-plan activity (R-N2b) —
    writing that evidence for the engine's own idempotent re-pass would be a false
    positive in the primary deviation detector.

    A rejection on any other node RAISES rather than being recorded. That is the
    opposite of the tick's behaviour and deliberate: mid-amendment we are inside
    the savepoint, and a node that cannot be superseded means the amendment cannot
    be applied coherently. Recording the rejection and continuing would produce
    exactly the "nodes from two plan versions" state the issue names as a blast
    radius. The raise unwinds the savepoint, so nothing lands.
    """
    superseded = 0

    for node in existing_nodes:
        if address_of(flow_slug, node) in proposed_addresses:
            continue

        current = NodeState(node.state)
        if current is NodeState.SUPERSEDED:
            continue

        result = transition(
            current,
            NodeState.SUPERSEDED,
            actor_kind=ActorKind(actor.actor_kind),
            reason=actor.reason or "superseded by plan amendment",
        )

        if not result.allowed:
            raise ProposalRejectedError(f"amendment cannot supersede node {address_of(flow_slug, node)!r}: {result.rejection_reason}")

        node.state = result.new_state.value
        superseded += 1

    return superseded


async def run_is_live(org_id: str, run_id: str) -> bool:
    """Whether a superseded node's active run may still be working, from its protected record.

    Conservative: anything but a positively ``completed`` protected execution record
    counts as live, so an unreadable store never releases a claim under a worker.
    """
    try:
        import asyncio

        from src.agentauth.engine import get_engine_authority_writer

        store = get_engine_authority_writer().store
        raw = await asyncio.to_thread(store._read, f"TENANT#{org_id}", f"EXEC#{run_id}")
    except Exception:  # noqa: BLE001 - unavailable authority must read as "still live"
        return True
    return not raw or raw.get("status") != {"S": "completed"}


async def _settle_superseded_lanes(session: AsyncSession, nodes: list[OrchestrationNode], *, org_id: str, created_version: int | None) -> int:
    """Close the execution lane of every superseded node given; return how many settled.

    A superseded address has no outgoing transition, so a continuation its execution
    still owes (a review handoff, a redelivery) can never be discharged, and a work
    claim that lane holds on the issue would refuse the successor address forever
    (``held_lease_lapsed``). Observed 2026-10-15 on cos-runtime COS-S15: the
    superseding node sat ``ready`` while the retired node's claim, held by a
    reviewer run that had already vanished, blocked every dispatch.

    The open execution is concluded unconditionally — the node is gone — including
    when the issue's claim has since moved on to a newer generation or been released
    (#7810: X05 and door1 on adp-tools-platform survived every amendment because the
    store refused the stale generation as if the settlement were adopting the lane).
    The claim is released only when this lane still owns it (same generation), it is
    held, and its active run is positively not live; a claim a successor lane now
    holds is that lane's to release, and a worker that is still running keeps the
    claim because its own terminal report releases it once this lane has concluded.

    Every refusal is logged; nothing here raises. ``created_version`` names the
    amendment that retired the node, or ``None`` when the claim sweep is settling a
    lane an earlier amendment could not close.
    """
    from .execution_state import TERMINAL_EXECUTION_STATUSES, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
    from .execution_store import advance_execution, load_execution
    from .handoff import current_identity
    from .models import OrchestrationWorkClaim
    from .work_claims import Disposition, ReleaseReason, release_work

    closed_by = f"plan amendment v{created_version}" if created_version is not None else "the claim sweep"
    settled = 0
    for node in nodes:
        changed = False
        identity = await current_identity(session, org_id=org_id, node_id=node.id)
        if identity is None or identity.claim_id is None:
            continue
        claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
        released_run = claim.claim_event_id if claim is not None and claim.state == "released" else None
        current = await load_execution(session, identity=identity, released_failure_run_id=released_run, closing_superseded_lane=True)
        if current is None or current.kind is not OutcomeKind.APPLIED or current.record is None:
            # Never silent (#7810), and never touch the claim of a lane that cannot
            # even be read: an operator must be able to see why the lane survived.
            logger.warning(
                "could not read the execution of superseded node %s: %s",
                node.id,
                getattr(current, "reason", None) or "no execution record",
            )
            continue
        if current.record.status not in TERMINAL_EXECUTION_STATUSES:
            ended = await advance_execution(
                session,
                identity=identity,
                released_failure_run_id=released_run,
                closing_superseded_lane=True,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.CONCLUDED,
                    status=ExecutionStatus.CONCLUDED,
                    expected_revision=current.record.revision,
                    progress_note=f"Node superseded by {closed_by}; this lane is closed and its successor starts fresh.",
                ),
            )
            if ended.kind is not OutcomeKind.APPLIED:
                # Best effort: the amendment itself is valid; the claim sweep retries
                # this lane every minute (#7800, #7810), so a lane that cannot be closed
                # right now must not reject the plan the owner just approved.
                logger.warning("could not close the execution of superseded node %s: %s", node.id, ended.reason)
                continue
            changed = True
        if claim is not None and claim.state == "held":
            if claim.generation != identity.claim_generation:
                # The issue's claim has moved on to a successor lane. The retired lane
                # only ever owned its own generation, so releasing here would take the
                # successor's claim out from under its live run (#7810).
                logger.info(
                    "superseded node %s lane closed; claim %s is held at generation %s by its successor and is left alone",
                    node.id,
                    claim.id,
                    claim.generation,
                )
            elif claim.active_run_id is None or not await run_is_live(org_id, claim.active_run_id):
                released = await release_work(
                    session,
                    org_id=org_id,
                    claim_id=claim.id,
                    generation=claim.generation,
                    reason=ReleaseReason.ABANDONED,
                    terminal_evidence=f"node superseded:{node.id} by {closed_by}",
                )
                if released.disposition not in {Disposition.ADMITTED, Disposition.DUPLICATE}:
                    logger.warning("could not release the work claim of superseded node %s: %s", node.id, released.reason)
                    continue
                changed = released.disposition is Disposition.ADMITTED or changed
        # A lane that was already closed and whose claim needed nothing is not a
        # settlement; reporting it as one made the sweep's log lie (#7873).
        settled += int(changed)
    return settled


async def settle_open_superseded_lanes(session: AsyncSession, *, limit: int = 50) -> int:
    """Close every superseded node's lane that is still open; run from the claim sweep (#7810).

    The amendment-time settlement sees a lane only at the moment a plan changes, and
    a lane it could not close then (its run was still live, its claim was mid-release)
    used to wait for the next amendment — which may never come. This pass is bounded,
    idempotent (a concluded lane has nothing to settle) and reads node state here, in
    the graph layer, so the execution store keeps never reading it.
    """
    from .execution_state import TERMINAL_EXECUTION_STATUSES
    from .models import OrchestrationExecution

    # Only the node's newest cycle is its lane: that is the row ``current_identity``
    # resolves and the only one this pass can settle. An older-cycle row left open
    # (claude-desktop run-qualification, 2026-10-11: cycle 3 ``runnable`` beside a
    # concluded cycle 9) used to select the node every minute; the settlement then
    # found its current lane already closed, counted it anyway, and every replica
    # logged ``superseded_lanes_settled count=1`` forever (#7873).
    latest_cycle = (
        select(
            OrchestrationExecution.org_id.label("org_id"),
            OrchestrationExecution.node_id.label("node_id"),
            func.max(OrchestrationExecution.cycle).label("cycle"),
        )
        .group_by(OrchestrationExecution.org_id, OrchestrationExecution.node_id)
        .subquery()
    )
    nodes = list(
        (
            await session.execute(
                select(OrchestrationNode)
                .join(
                    OrchestrationExecution,
                    (OrchestrationExecution.node_id == OrchestrationNode.id) & (OrchestrationExecution.org_id == OrchestrationNode.org_id),
                )
                .join(
                    latest_cycle,
                    (latest_cycle.c.org_id == OrchestrationExecution.org_id)
                    & (latest_cycle.c.node_id == OrchestrationExecution.node_id)
                    & (latest_cycle.c.cycle == OrchestrationExecution.cycle),
                )
                .where(
                    OrchestrationNode.state == NodeState.SUPERSEDED.value,
                    OrchestrationExecution.status.not_in(sorted(status.value for status in TERMINAL_EXECUTION_STATUSES)),
                )
                .distinct()
                .order_by(OrchestrationNode.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    settled = 0
    for org_id in sorted({node.org_id for node in nodes}):
        settled += await _settle_superseded_lanes(session, [node for node in nodes if node.org_id == org_id], org_id=org_id, created_version=None)
    return settled


def _amendment_reason(
    reason: str | None,
    superseded_version: int | None,
    created_version: int,
    nodes_superseded: int,
) -> str:
    """Compose the decision record's reason, preserving the actor's own words.

    The version pair and supersede count are appended to the operator's stated
    reason rather than replacing it: the issue requires the superseded → created
    version numbers on the decision row, and `orchestration_decisions` has no
    column for them. Appending keeps the requirement satisfied without a migration
    this story is explicitly not supposed to need, and the actor's justification
    stays readable at the front of the field.

    `superseded_version` is None when the flow had no plan in force — amending a
    flow whose original compile never happened. Rendered as "none" rather than
    "v0", which would name a version that never existed.
    """
    stated = reason or "plan amended"
    from_version = "none" if superseded_version is None else f"v{superseded_version}"
    return f"{stated} [amendment: {from_version} -> v{created_version}, {nodes_superseded} node(s) superseded]"


_RECEIPT_DETAIL = "\n[receipt_disposition/v1] "


def _receipt_disposition_detail(preserved, dropped, limits_before, limits_after) -> str:
    return _RECEIPT_DETAIL + json.dumps(
        {
            "receipts_preserved": preserved,
            "receipts_dropped": dropped,
            "effective_limits_before": limits_before,
            "effective_limits_after": limits_after,
        },
        sort_keys=True,
    )


async def _saved_receipt_dispositions(session, decision_id):
    if not decision_id:
        return [], [], None, None
    decision = await session.get(OrchestrationDecision, decision_id)
    if decision is None:
        raise ProposalRejectedError("amendment decision missing on idempotent replay")
    _, marker, payload = (decision.reason or "").rpartition(_RECEIPT_DETAIL)
    if not marker:
        if decision.kind == DecisionKind.PLAN_AMENDED.value:
            raise ProposalRejectedError("amendment receipt disposition missing on idempotent replay")
        return [], [], None, None
    try:
        saved = json.loads(payload)
        preserved, dropped = saved["receipts_preserved"], saved["receipts_dropped"]
        if not isinstance(preserved, list) or not isinstance(dropped, list):
            raise ValueError("invalid receipt disposition")
        before = saved.get("effective_limits_before")
        after = saved.get("effective_limits_after")
        if (before is not None and not isinstance(before, dict)) or (after is not None and not isinstance(after, dict)):
            raise ValueError("invalid effective limits snapshot")
        return preserved, dropped, before, after
    except (ValueError, KeyError, TypeError) as error:
        raise ProposalRejectedError("amendment receipt disposition cannot be verified") from error


def _receipt_values(policy, kind):
    if policy is None:
        return None
    if kind == "budget":
        from .shared_budget import financial_limits

        return financial_limits(policy).model_dump(mode="json")
    if kind == "retry":
        return policy.limits.max_attempts_per_node
    if kind == "window":
        return {"expires_at": policy.expires_at.isoformat(), "max_wall_clock_seconds": policy.limits.max_wall_clock_seconds}
    return policy.limits.max_concurrent_actions


async def _receipt_dispositions(session, in_force, document):
    from .execution_policy import ExecutionPolicy
    from .plan_lineage import receipt_amendment_change
    from .policy_admission import load_in_force_policy
    from .shared_budget import accepted_document_hash, apply_limits, financial_limits
    from .shared_concurrency import apply_concurrency_limit
    from .shared_retry import apply_retry_limit
    from .shared_window import apply_window

    candidate = ExecutionPolicy.model_validate(document["execution_policy"]) if document.get("execution_policy") else None
    if in_force is None or not isinstance(in_force.plan_document, dict) or not in_force.plan_document.get("execution_policy"):
        return [], [], candidate

    before = await load_in_force_policy(session, org_id=in_force.org_id, flow_id=in_force.flow_id)
    if before.refusal is not None or before.policy is None:
        raise ProposalRejectedError("accepted receipt history cannot be verified; amendment refused")
    source = before.policy
    decisions = {
        "budget": source._shared_budget_decision_id,
        "retry": source._shared_retry_decision_id,
        "window": source._shared_window_decision_id,
        "concurrency": source._shared_concurrency_decision_id,
    }
    if not any(decisions.values()):
        return [], [], candidate
    try:
        if in_force.plan_hash != accepted_document_hash(in_force.plan_document):
            raise ValueError("accepted plan hash changed")
    except ValueError as error:
        raise ProposalRejectedError("accepted receipt plan cannot be verified; amendment refused") from error
    preserved, dropped = [], []
    for kind, decision_id in decisions.items():
        if decision_id is None:
            continue
        decision = await session.get(OrchestrationDecision, decision_id)
        reason = await receipt_amendment_change(session, in_force, document, json.loads(decision.reason))
        if reason is None:
            if kind == "budget":
                candidate = apply_limits(candidate, financial_limits(source), decision_id)
            elif kind == "retry":
                candidate = apply_retry_limit(candidate, source.limits.max_attempts_per_node, decision_id)
            elif kind == "window":
                candidate = apply_window(candidate, source.expires_at, decision_id, source.limits.max_wall_clock_seconds)
            else:
                candidate = apply_concurrency_limit(candidate, source.limits.max_concurrent_actions, decision_id)
        old_value = _receipt_values(source, kind)
        item = {
            "kind": kind,
            "decision_id": decision_id,
            "before": old_value,
            "after": old_value if reason is None else _receipt_values(candidate, kind),
            "reason": reason or "authority_unchanged",
        }
        (preserved if reason is None else dropped).append(item)
    return preserved, dropped, candidate
