"""Resolving an authenticated reviewer upload into recorded evidence (#5146).

The last link in the chain this issue exists to build: a reviewer run produces a
review result, uploads it over the authenticated own-run artifact transport, and
the server turns that upload into a durable ledger record — or into a typed
refusal naming what it could not confirm.

## Why a separate module

:mod:`src.orchestration.review_evidence` owns the *contract* half: parse, compare
every field against protected values, refuse with a typed arm. It takes the
protected values as arguments precisely so it cannot be fooled by its caller's
convenience, and so it is testable without a database, a provider or a pod.

This module owns the *resolution* half: given nothing but an authenticated run,
work out which story, which execution, which pull request, which authoring run
and which artifact references — all from state the reviewer cannot write — and
then call the validator with them. Splitting the two keeps the property that
makes the validator trustworthy: every value it compares against arrives from
here, and there is no argument a worker can set.

## The identity problem this module solves

A reviewer's run id is deliberately **not** derivable from the node. Engine
dispatch mints ``attempt_run_id(node_id, attempt)`` for the run that does the
work (``dispatch_pass.attempt_run_id``); a reviewer arrives through the delegated
path (``graph_dispatch.dispatch_graph``), whose invocation id is a uuid5 over
tenant, parent principal and the parent's chosen request id, and which
deliberately does **not** increment the node's attempt. So:

* ``resolve_registration_target(run_id=<reviewer invocation>)`` raises
  ``UNKNOWN_RUN`` — it scans ``NODE_DISPATCHED`` decisions, and a reviewer has
  none. It is the developer's resolver and is not usable here.
* the reviewer's own credential cannot tell us which run authored the change.

What *is* protected is the pair the reviewer's execution row carries:
``orchestration_node_id`` and ``orchestration_node_attempt``, written by
:mod:`src.agentauth.dispatch` at reservation time from the ``GraphAssignment``
the gateway composed — never from the worker. From that pair the authoring run
is ``attempt_run_id(node_id, attempt)``, which is exactly what the node's
``NODE_DISPATCHED`` decision recorded as ``reason["run_id"]``.

So this module resolves the author from the *node*, and the reviewer from the
*credential*, and those two facts are what make the self-review and
reviewer-substitution arms mean something. Deriving the author from the document
instead would make the check circular, which is the failure
``validate_review_result``'s ``author_run_id`` parameter was added to prevent.

## Verification, not assumption

Three of the validator's four protected inputs are resolved here from state the
reviewer cannot write. The fourth — ``trusted_artifact_refs`` — is the one that
cannot be resolved by a lookup, and it is treated accordingly:

* ``reviewer_run_id`` is the authenticated caller's invocation id.
* ``execution_id`` is the id of the execution row this node and cycle resolve to.
* ``actual_head_sha`` is read from the provider, not from the binding's cached
  column, because the point of the whole artifact is binding review to an exact
  revision and a cached head cannot show that a later push moved it.
* ``trusted_artifact_refs`` is the set of head-bound references the server could
  confirm belong to this run's own artifact namespace. A reference the server
  cannot resolve is **not** passed, so the validator refuses it as
  ``UNTRUSTED_ARTIFACT`` rather than counting an unverifiable test result as
  proof. See :func:`verified_artifact_refs`.

Passing ``None`` for any of these would make the validator record the gap in
``unverified`` and :func:`review_evidence.require_verified_state` would then
refuse the write — which is the designed fail-closed behaviour, not a fallback
this module may quietly rely on.

## What this module deliberately does not do

It does not advance a phase, dispatch a repair, decide merge eligibility, or
write any state beyond the ledger record the evidence itself is. Recording that
a review happened is not deciding what follows from it, and #5146's scope is the
record. A refusal produces a :class:`~.review_evidence.BlockRecord` through
``outstanding_block`` for a caller that wants to persist the condition, and
nothing here activates any authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.orchestration.models import OrchestrationNode
from src.orchestration.review_evidence import (
    ReviewEvidence,
    ReviewEvidenceError,
    ReviewEvidenceRefusal,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ResolvedReviewContext",
    "ReviewIngestOutcome",
    "ingest_review_result",
    "resolve_review_context",
    "verified_artifact_refs",
]

#: How each head-bound reference kind is verified. A kind absent from here cannot be
#: confirmed by this path at all, so it is never placed in the trusted set and the
#: validator refuses the result as ``UNTRUSTED_ARTIFACT`` — the conservative
#: direction, because an unverifiable test result supports no conclusion.
#:
#: The two verifiable kinds are verified by genuinely different reads, which is why
#: this is a mapping rather than a set:
#:
#: * ``artifact`` — an own-run upload. Confirmed by prefix against the run's
#:   server-derived artifact namespace (``artifact_keys.artifact_prefix`` is a digest
#:   of tenant and invocation plus the attempt), which a worker cannot choose.
#: * ``test-run`` / ``check-run`` — a provider check run. Confirmed by asking the
#:   provider which check runs exist **for the reviewed commit**
#:   (``pr_identity.resolve_head_check_runs``). Head-scoped at the request, so a real
#:   check-run id belonging to another commit does not verify.
#:
#: Nothing is verified by "the reviewer said so".
ARTIFACT_REF_KINDS = frozenset({"artifact"})
CHECK_REF_KINDS = frozenset({"test-run", "check-run"})
VERIFIABLE_REF_KINDS = ARTIFACT_REF_KINDS | CHECK_REF_KINDS


@dataclass(frozen=True)
class ResolvedReviewContext:
    """Everything the validator must be given, resolved from protected state.

    Every field here came from a server write or a provider read. None of it came
    from the submitted document, which is what makes comparing the document
    against it meaningful.
    """

    identity: Any
    """The :class:`~.execution_state.ExecutionIdentity` for this node and attempt."""

    binding: Any
    """The active implementation pull-request binding for the story."""

    flow_id: str
    execution_id: str
    author_run_id: str
    reviewer_run_id: str
    actual_head_sha: str
    provider_check_refs: frozenset[str]
    """Check runs the provider confirmed for ``actual_head_sha``. A completed read.

    Never a fallback empty set: :func:`resolve_review_context` refuses when the read
    fails, because "the provider reported no check runs" and "the provider could not
    be asked" mean opposite things to a reader of the resulting ledger row.
    """

    node_id: str
    attempt: int


def _author_run_id(node_id: str, attempt: int) -> str:
    """The run the engine dispatched to author this attempt's change.

    Imported lazily and re-derived rather than read back out of the dispatch
    decision's JSON: ``attempt_run_id`` is the definition, the decision row is a
    record of it, and a caller that reads the record must then also prove the
    record is the current attempt's. Resolving the node's attempt under a lock
    and deriving from it is the same fact with fewer ways to be wrong.
    """
    from src.orchestration.dispatch_pass import attempt_run_id

    return attempt_run_id(node_id, attempt)


async def resolve_review_context(
    session: AsyncSession,
    *,
    org_id: str,
    node_id: str,
    attempt: int,
    reviewer_run_id: str,
    installation_id: int,
) -> ResolvedReviewContext:
    """Resolve the protected values a review submission will be compared against.

    ``org_id``, ``node_id``, ``attempt`` and ``installation_id`` must come from the
    reviewer's authenticated execution record — the DynamoDB item the gateway wrote
    at dispatch — never from the request body. ``reviewer_run_id`` is the
    authenticated caller's invocation id.

    Every arm is a refusal. In particular:

    * a node whose attempt has moved on is refused rather than resolved at its new
      attempt: the review examined the code of the attempt it was dispatched for,
      and silently re-pointing it at a newer attempt would attach a review to work
      it never read.
    * a reviewer whose invocation *is* the attempt's author run is refused here,
      before any document is parsed. The validator refuses this too, from the
      document's own lineage; this arm catches the case where the document lies in
      the other direction.
    * an absent execution row is ``NO_EXECUTION`` rather than a created one. This
      module never creates state.

    Raises:
        ReviewEvidenceError: with the arm naming what could not be resolved.
    """
    from src.orchestration.execution_state import OutcomeKind
    from src.orchestration.execution_store import load_execution
    from src.orchestration.handoff import identity_for_attempt
    from src.orchestration.pr_identity import PrIdentityError, resolve_head_check_runs, resolve_pr_identity
    from src.orchestration.review_evidence import resolve_expected_subject

    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == node_id, OrchestrationNode.org_id == org_id))
    if node is None:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.NO_EXECUTION,
            "The story this review was dispatched for no longer exists, so there is nothing the review can be evidence about.",
        )
    if node.attempts != attempt:
        # Not an error in the reviewer's behaviour — the attempt moved while it
        # worked. But its findings describe the previous attempt's code.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.STALE_CLAIM,
            f"This review was dispatched for attempt {attempt} of the story, but attempt {node.attempts} is now current, "
            "so the work it examined has been superseded.",
        )

    author_run_id = _author_run_id(node_id, attempt)
    if reviewer_run_id == author_run_id:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.SELF_REVIEW,
            "The authenticated reviewing run is the run that authored this attempt's change, so the review is not independent.",
        )

    identity = await identity_for_attempt(session, org_id=org_id, node_id=node_id, attempt=attempt)
    if identity is None:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.NO_EXECUTION,
            "No execution is recorded for the attempt this review was dispatched for, so there is no work for it to be evidence about.",
        )

    # The row id, which is what the contract's `scope.execution_id` names. Read
    # through the store rather than re-queried here so there is one definition of
    # which row a (node, cycle) pair means, and so a stored authority that
    # disagrees with this identity surfaces as the store's own CONFLICT instead of
    # being silently resolved to a row the claim no longer owns.
    outcome = await load_execution(session, identity=identity)
    if outcome is None or outcome.kind is not OutcomeKind.APPLIED or outcome.record is None:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.NO_EXECUTION,
            "The execution this review was dispatched for could not be read as current, so evidence cannot be bound to it.",
        )

    # Raises NO_BINDING / AMBIGUOUS_PR / NOT_IMPLEMENTATION. Resolved from the
    # server-registered binding, so a reviewer naming another pull request is
    # refused by the validator rather than believed.
    binding = await resolve_expected_subject(session, org_id=org_id, node_id=node_id, attempt=attempt)

    try:
        pr = await resolve_pr_identity(org_id=org_id, installation_id=installation_id, repo=binding.repo, pr_number=binding.pr_number)
    except PrIdentityError:
        # Deliberately a refusal and not a fall back to `binding.head_sha`. The
        # cached head cannot show that a later push invalidated the review, and
        # substituting it would turn "the head was not read" into a silent pass —
        # the exact class of defect the `unverified` mechanism exists to prevent.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.HEAD_UNVERIFIED,
            "The pull request's current head could not be read from the provider, so this review cannot be bound to an exact revision.",
        ) from None
    if pr.provider_repository_id != binding.provider_repository_id or pr.provider_pr_node_id != binding.provider_pr_node_id:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.REPOSITORY_MISMATCH,
            "The bound pull request's immutable identity no longer matches the provider, so the binding cannot be trusted.",
        )

    try:
        # Read against the head the provider just reported, not the document's
        # reviewed head. If those differ the result is stale anyway and the validator
        # refuses it; asking about the document's head instead would let a reviewer
        # choose which commit its evidence is verified against.
        check_refs = await resolve_head_check_runs(org_id=org_id, installation_id=installation_id, repo=binding.repo, head_sha=pr.head_sha)
    except PrIdentityError:
        # ARTIFACTS_UNVERIFIED, not an empty set. An empty set would tell the
        # validator "asked, nothing there", and every head-bound check reference
        # would then be refused as untrusted — the same outcome for the wrong reason,
        # and an operator reading `untrusted_artifact` would go looking for a
        # reviewer defect instead of a provider outage.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.ARTIFACTS_UNVERIFIED,
            "The provider's check runs for the reviewed commit could not be read, so the test evidence this review relies on "
            "cannot be confirmed.",
        ) from None

    return ResolvedReviewContext(
        identity=identity,
        binding=binding,
        flow_id=node.flow_id,
        execution_id=outcome.record.id,
        author_run_id=author_run_id,
        reviewer_run_id=reviewer_run_id,
        actual_head_sha=pr.head_sha,
        provider_check_refs=check_refs,
        node_id=node_id,
        attempt=attempt,
    )


async def verified_artifact_refs(
    result: Any,
    *,
    own_prefix: str,
    provider_check_refs: frozenset[str],
) -> frozenset[str]:
    """The head-bound references the server independently confirmed.

    A review that cites test evidence is only as good as that evidence, and a
    reference the server cannot resolve supports nothing. So rather than trusting
    the reviewer's list, this returns the intersection of what the document cites
    with what the server actually confirmed, by the two routes in
    :data:`VERIFIABLE_REF_KINDS`.

    ``provider_check_refs`` must come from a **completed** provider read scoped to
    the reviewed commit. A caller whose read failed must not pass an empty set: an
    empty set is a claim that the provider was asked and reported nothing, and is
    not the same fact as "the provider could not be asked". The ingest path refuses
    in that case instead (``HEAD_UNVERIFIED``), which is why this parameter is
    required rather than defaulted — a default would make the dangerous case the
    easy one to write.

    Returns:
        The confirmed subset. Everything the document cites and this omits becomes
        an ``UNTRUSTED_ARTIFACT`` refusal in the validator. May be legitimately
        empty, which is still a claim of verification and is distinct from ``None``.
    """
    candidates = [ref for ref in result.evidence_refs if ref.head_bound]
    for finding in result.findings:
        candidates.extend(ref for ref in finding.evidence_refs if ref.head_bound)

    verified: set[str] = set()
    for ref in candidates:
        if not isinstance(ref.ref, str):
            continue
        if ref.kind in ARTIFACT_REF_KINDS:
            # Prefix, not substring: a reference merely *containing* another run's
            # prefix would otherwise verify, which is the cross-run confusion the
            # server-derived namespace exists to prevent.
            if own_prefix and ref.ref.startswith(own_prefix):
                verified.add(ref.ref)
        elif ref.kind in CHECK_REF_KINDS and ref.ref in provider_check_refs:
            verified.add(ref.ref)
    return frozenset(verified)



@dataclass(frozen=True)
class ReviewIngestOutcome:
    """What the ingest path concluded, for a caller that must answer a request.

    Carries the refusal *as data* rather than only raising, because the caller is
    an HTTP route that has to distinguish three cases with different responses: a
    recorded evidence row, a validation refusal the reviewer should see and can
    act on, and a ledger outcome that declined the write. Collapsing those into an
    exception would make the route guess.
    """

    evidence: ReviewEvidence | None
    """The validated evidence, present only when validation succeeded."""

    refusal: ReviewEvidenceRefusal | None
    """The arm that refused, when one did."""

    detail: str | None
    """Operator- and reviewer-facing prose for the refusal. Never a secret."""

    ledger: Any | None
    """The store's ``ExecutionOutcome``, present only when a write was attempted."""

    @property
    def recorded(self) -> bool:
        """Whether durable evidence now exists for this submission.

        ``True`` only when the ledger applied the observation. A validated document
        whose ledger write returned ``CONFLICT`` is deliberately **not** recorded:
        the evidence is real but nothing persisted it, and a caller that treated
        that as success would report a record that a later reader cannot find.
        """
        from src.orchestration.execution_state import OutcomeKind

        return self.evidence is not None and self.ledger is not None and self.ledger.kind is OutcomeKind.APPLIED


async def ingest_review_result(
    session: AsyncSession,
    *,
    document: dict[str, Any],
    org_id: str,
    node_id: str,
    attempt: int,
    reviewer_run_id: str,
    installation_id: int,
    own_artifact_prefix: str,
) -> ReviewIngestOutcome:
    """Validate a submitted review result against protected state and record it.

    The composed production path, and the only function here a route should need.
    Resolves every protected value server-side (:func:`resolve_review_context`),
    validates the document against them, requires that no protected check was
    skipped, and persists the reference through the execution ledger.

    ``document`` is the submitted artifact, parsed but otherwise untrusted.
    ``own_artifact_prefix`` is the reviewing run's server-derived artifact key
    prefix, used to decide which head-bound references are verifiable; it must be
    derived from the authenticated execution record, not from the request.

    Does **not** commit. The caller owns the transaction boundary because it also
    owns re-verifying the caller's authority after the write and before the commit
    — the ordering ``registration_routes`` established, which exists so a
    credential revoked mid-request cannot leave a committed row behind.

    Returns:
        ReviewIngestOutcome: with ``evidence`` on success, or ``refusal`` and
        ``detail`` naming the arm. A refusal is returned rather than raised
        because every arm here is a legitimate answer to a well-formed request,
        and the reviewer needs to know which one fired.
    """
    from src.orchestration.review_evidence import (
        record_review_evidence,
        require_verified_state,
        validate_review_result,
    )

    try:
        context = await resolve_review_context(
            session,
            org_id=org_id,
            node_id=node_id,
            attempt=attempt,
            reviewer_run_id=reviewer_run_id,
            installation_id=installation_id,
        )
    except ReviewEvidenceError as error:
        return ReviewIngestOutcome(evidence=None, refusal=error.code, detail=error.message, ledger=None)

    try:
        # Parsed once here so the reference verification below can read the typed
        # result, and validated against the resolved context in the same call. The
        # order matters: `verified_artifact_refs` needs a parsed result, and
        # `validate_review_result` needs the trusted set, so the parse happens
        # first and its refusals surface with the same arms.
        from src.orchestration.review_evidence import parse_review_result

        parsed = parse_review_result(document)
        trusted = await verified_artifact_refs(parsed, own_prefix=own_artifact_prefix, provider_check_refs=context.provider_check_refs)
        evidence = validate_review_result(
            document,
            identity=context.identity,
            binding=context.binding,
            flow_id=context.flow_id,
            author_run_id=context.author_run_id,
            reviewer_run_id=context.reviewer_run_id,
            execution_id=context.execution_id,
            actual_head_sha=context.actual_head_sha,
            trusted_artifact_refs=trusted,
        )
        # Fail closed before persisting. Every protected input was supplied above,
        # so this should never fire — which is exactly why it is called: if a future
        # edit drops one of those arguments, this refuses the write instead of
        # recording a row whose `unverified` gap no later reader can see.
        require_verified_state(evidence)
    except ReviewEvidenceError as error:
        logger.info(
            "review evidence refused node=%s attempt=%s reviewer=%s reason=%s",
            node_id,
            attempt,
            reviewer_run_id,
            error.code.value,
        )
        return ReviewIngestOutcome(evidence=None, refusal=error.code, detail=error.message, ledger=None)

    ledger = await record_review_evidence(session, identity=context.identity, evidence=evidence)
    return ReviewIngestOutcome(evidence=evidence, refusal=None, detail=None, ledger=ledger)
