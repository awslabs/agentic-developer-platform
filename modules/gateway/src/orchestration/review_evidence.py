"""Validate a reviewer run's structured result against protected state (#5146).

## The defect this exists to close

A reviewer run that exits 0 has established nothing about the change under review.
Every observation recorded on #5146 is a variant of that: a run that reproduced a
real correctness blocker and published only a security report; a run whose
functional prose said APPROVE while the pull request's review list stayed empty; a
run whose findings described one commit while the provider attached the verdict to
another. In each case the process succeeded, so nothing downstream could tell
complete review evidence from incomplete review evidence.

The shared artifact in ``contracts/orchestration-review/v1`` gives the reviewer a
shape that can *state* what it did. This module is the half that decides whether to
believe it.

## Why nothing here trusts the submitter

The artifact's ``scope``, ``authority``, ``repository``, ``subject`` and ``lineage``
are all things a model can type into a JSON document. So none of them is accepted:
each is compared against state the reviewer cannot write — the pull-request binding
registered for the node's current attempt, the execution row's plan version and
claim generation, and the provider's own head. A result whose claimed lineage does
not match protected state is refused, which is what "arbitrary issue prose cannot
manufacture evidence" means mechanically.

This is the same property ``pr_binding_routes`` and ``handoff`` rely on, and the
reason :func:`validate_review_result` takes an :class:`ExecutionIdentity` its caller
already resolved rather than any field from the document.

## Every refusal is typed, and absence is never a pass

:class:`ReviewEvidenceRefusal` is a closed set of codes, following
``pr_bindings.BindingRefusal`` for the same reason: "which fail-closed arm fired" is
the whole diagnostic value, and one opaque message is what made the original
failures expensive to diagnose. :func:`refusal_explanation` names what is missing
and who resolves it.

There is deliberately no path from a missing answer to an accepted one. An absent
binding, an unresolvable pull request, an ambiguous candidate and a provider we
could not reach are all refusals, not defaults.

## What "accepted" does and does not mean

:class:`ReviewEvidence` carrying no ``approval_blockers`` means the artifact is
internally complete and correctly bound to the current revision. It is **not** a
merge decision and **not** a substitute for the provider-side requirements — an
approving review from a non-author, required checks, merge state — which
``pr_bindings.evidence_for_binding`` owns and this module never re-decides. The
reviewer-is-not-the-author check enforced by the contract is necessary and not
sufficient; :func:`validate_review_result` re-checks it against *protected* run
identity rather than the document's claim, and still does not discharge the
provider's own independent-approval rule.

## Scope

Producing and validating evidence, and persisting references to it. This module
dispatches no repair, advances no phase, decides no merge eligibility and adds no
route. :func:`record_review_evidence` writes through the existing execution ledger
(#5142) as one prepared action plus one observation, carrying references only —
never a transcript and never a credential.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .execution_state import (
    ActionIntent,
    BlockCode,
    BlockRecord,
    ExecutionIdentity,
    ExecutionOutcome,
    Observation,
    ObservedOutcome,
    OutcomeKind,
)
from .models import BindingRole, BindingState, OrchestrationPullRequestBinding
from .pr_bindings import BindingError, BindingRefusal, active_binding_for_node

__all__ = [
    "REVIEW_CONTRACT_NAME",
    "REVIEW_CONTRACT_VERSION",
    "ReviewEvidence",
    "ReviewEvidenceError",
    "ReviewEvidenceRefusal",
    "evidence_action_intent",
    "evidence_decision_snapshot",
    "evidence_observation",
    "evidence_operation_key",
    "evidence_summary",
    "legacy_review_note",
    "outstanding_block",
    "parse_review_result",
    "record_review_evidence",
    "refusal_explanation",
    "resolve_expected_subject",
    "review_artifact_ref",
    "validate_review_result",
]

REVIEW_CONTRACT_NAME = "orchestration-review"
REVIEW_CONTRACT_VERSION = 1

#: The contract package lives outside this module's import tree on purpose:
#: `contracts/README.md` keeps that directory to schemas and their documentation,
#: with no `pyproject.toml` and no package install. Both the gateway and the worker
#: reach it by path for exactly that reason, so there is ONE validator rather than
#: a copy per consumer — the #4029 drift this whole pattern exists to prevent.
#:
#: Two locations, checked in order, because the repository checkout and the deployed
#: artifact have different shapes and only one of them was previously handled:
#:
#: 1. ``/app/contracts/orchestration-review/v1`` beside ``src/``, which is where the
#:    build stages it. The gateway image's docker context is ``modules/gateway``
#:    (`codebuild/bs-gateway-build.yml`), so a repository-root directory is outside
#:    the context and no ``COPY`` can reach it — the same constraint
#:    ``orchestration-deployments.yaml`` documents for itself. The build stages the
#:    directory into the context first; ``stage-contracts.sh`` does that, and
#:    ``selfcheck`` is the build gate that proves it happened.
#: 2. The repository root, four parents up, for a source checkout and the tests.
#:
#: This ordering matters beyond tidiness. In the image ``src/`` sits directly under
#: ``/app``, so the old unconditional ``parents[4]`` raised ``IndexError`` — not the
#: typed ``CONTRACT_UNAVAILABLE`` refusal this module promises, but a bare crash out
#: of a path that every caller expects to fail closed with a reason.
_CONTRACT_RELATIVE = Path("contracts") / "orchestration-review" / "v1"


def _contract_candidates() -> tuple[Path, ...]:
    """Every location the shared contract may legitimately live, in priority order.

    Returned as a tuple (rather than resolved to one path at import time) so the
    refusal can name all of them: "not found" is only actionable if an operator can
    see where it was looked for.
    """
    here = Path(__file__).resolve()
    candidates = [here.parents[2] / _CONTRACT_RELATIVE]  # /app/contracts — the image
    if len(here.parents) > 4:
        candidates.append(here.parents[4] / _CONTRACT_RELATIVE)  # repo root — a checkout
    return tuple(candidates)


def _contract_dir() -> Path | None:
    """The first candidate that actually carries ``models.py``, or ``None``."""
    for candidate in _contract_candidates():
        if (candidate / "models.py").is_file():
            return candidate
    return None


def _contract_models() -> Any:
    """Import the normative validator from the shared contract directory.

    Imported lazily and by path rather than at module import time so that a gateway
    process which never handles review evidence does not depend on the contracts
    directory being present, and so the failure — if the directory is missing — is
    a clear error at the point of use rather than an import crash at startup.

    Raises:
        ReviewEvidenceError: ``CONTRACT_UNAVAILABLE`` when the validator is not
            present or cannot be loaded. Never an ``IndexError`` or ``ImportError``:
            an artifact built without the contract must refuse review evidence with a
            reason an operator can act on, not crash somewhere further down.
    """
    import importlib.util
    import sys

    cached = sys.modules.get("_adp_orchestration_review_v1")
    if cached is not None:
        return cached
    directory = _contract_dir()
    if directory is None:
        looked = ", ".join(str(candidate) for candidate in _contract_candidates())
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.CONTRACT_UNAVAILABLE,
            "The review-result contract validator is not available in this artifact "
            f"(looked in: {looked}). This build does not carry the shared contract, so "
            "it cannot validate review evidence.",
        )
    path = directory / "models.py"
    spec = importlib.util.spec_from_file_location("_adp_orchestration_review_v1", path)
    if spec is None or spec.loader is None:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.CONTRACT_UNAVAILABLE,
            f"The review-result contract validator at {path} could not be loaded.",
        )
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec_module, and that ordering is load-bearing. `models.py`
    # uses `from __future__ import annotations`, so pydantic resolves its annotations
    # by looking the module up in `sys.modules` by name — a module executed before it
    # is registered leaves `ReviewResult` with unresolved forward references, and
    # every `model_validate` then fails with "is not fully defined" rather than
    # validating. Reordering these two lines looks like tidying and breaks validation.
    sys.modules["_adp_orchestration_review_v1"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # A models.py present but unimportable (an absent pydantic, a partial copy)
        # must not leave a half-initialised module cached under the shared name for
        # the next caller to find and treat as a working validator.
        sys.modules.pop("_adp_orchestration_review_v1", None)
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.CONTRACT_UNAVAILABLE,
            f"The review-result contract validator at {path} could not be imported: {exc!r}.",
        ) from exc
    return module


class ReviewEvidenceRefusal(StrEnum):
    """Why a submitted review result was not accepted as evidence.

    Stable machine-readable codes rather than prose, following
    ``pr_bindings.BindingRefusal``. Two of these reach an operator through the story
    API, and "which arm fired" is the whole diagnostic value: the observed failures
    were expensive to diagnose precisely because "the reviewer ran and nothing
    happened" said nothing about whether the result was malformed, bound to the
    wrong revision, or never published.

    Every member is a refusal. None of them is a state an absent answer falls
    through to.
    """

    MALFORMED_RESULT = "malformed_result"  # Did not validate against the contract
    CONTRACT_UNAVAILABLE = "contract_unavailable"  # The shared validator could not be loaded
    WRONG_CONTRACT = "wrong_contract"  # A document from another contract or version

    # Binding and identity arms. Each compares the document against state the
    # submitter cannot write.
    NO_BINDING = "no_binding"  # No pull request registered for this story
    AMBIGUOUS_PR = "ambiguous_pr"  # Several active bindings; which was reviewed is unknown
    NOT_IMPLEMENTATION = "not_implementation"  # Bound PR is a reviewer artifact
    SUPERSEDED_BINDING = "superseded_binding"  # The binding was replaced
    REPOSITORY_MISMATCH = "repository_mismatch"  # Result names another repository
    PR_MISMATCH = "pr_mismatch"  # Result names another pull request
    TENANT_MISMATCH = "tenant_mismatch"  # Result belongs to another tenant
    SCOPE_MISMATCH = "scope_mismatch"  # Result names another node or cycle
    POLICY_MISMATCH = "policy_mismatch"  # Result was authorized under another accepted plan
    STALE_CLAIM = "stale_claim"  # Claim generation is no longer current
    REVIEWER_MISMATCH = "reviewer_mismatch"  # Named reviewer is not the authenticated producer
    REVIEWER_UNVERIFIED = "reviewer_unverified"  # No authenticated producer to compare against
    EXECUTION_MISMATCH = "execution_mismatch"  # Result names another execution
    EXECUTION_UNBOUND = "execution_unbound"  # Persisted evidence must name its execution
    EXECUTION_UNVERIFIED = "execution_unverified"  # No server-resolved execution to compare against
    HEAD_UNVERIFIED = "head_unverified"  # The provider's current head was never read
    ARTIFACTS_UNVERIFIED = "artifacts_unverified"  # Head-bound references were never checked

    # Evidence-quality arms. The result is well-formed and correctly bound, and
    # still cannot be treated as review evidence for the current revision.
    STALE_HEAD = "stale_head"  # Reviewed commit is not the bound head
    SELF_REVIEW = "self_review"  # Reviewer run is the author run
    UNTRUSTED_ARTIFACT = "untrusted_artifact"  # A referenced artifact could not be trusted


class ReviewEvidenceError(Exception):
    """A review result was refused. Always a refusal, never a default.

    ``code`` is a :class:`ReviewEvidenceRefusal` a caller can put in a response body
    or persist as a typed block, so the reason survives without reading logs.
    """

    def __init__(self, code: ReviewEvidenceRefusal, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ReviewEvidence:
    """A review result that validated and bound to the current revision.

    Deliberately not "an approval". ``approval_blockers`` is carried forward from
    the artifact rather than collapsed into a boolean, because the observed failures
    all involved something true-but-insufficient being read as sufficient. A caller
    deciding anything on this object must consult it, and an empty tuple means only
    that the *artifact* carries no blocker — provider-side independent review,
    required checks and merge state are separate requirements owned by
    ``pr_bindings.evidence_for_binding``.

    ``result`` is the validated contract object. ``artifact_ref`` is the reference
    persisted through the ledger; it is a pointer, never the document.
    """

    result: Any
    artifact_ref: str
    reviewed_head_sha: str
    repo: str
    pr_number: int
    approval_blockers: tuple[str, ...]
    publication_blockers: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()

    @property
    def is_complete_review(self) -> bool:
        """True when the artifact carries no blocker **and** nothing went unchecked.

        Named for what it checks. It is not ``may_merge`` and not ``approved``: this
        object cannot see the provider's review list, its required checks or its
        merge state, and a name suggesting otherwise is how the next caller skips
        the checks that do.

        ``unverified`` is part of the answer because of a reproduced finding: with
        the authenticated producer, the server-resolved execution, the provider's
        current head and the artifact references all omitted, this returned True. A
        caller that checked none of those learned nothing, and "I did not look"
        must not be reported as "I looked and it was fine". A validation missing a
        protected input therefore yields an *incomplete* review rather than a
        complete one, and the production ingestion path refuses outright — see
        ``require_verified_state``.
        """
        return not self.approval_blockers and not self.unverified

    @property
    def review_concluded_without_blockers(self) -> bool:
        """The reviewing work finished clean, whatever happened to publication.

        Exists so "the reviewer approved but publication returned 401" is legible as
        itself rather than as "the reviewer found a problem". They are opposites in
        cause and need opposite responses — retry the publication versus write new
        code — and the observed handling collapsed the first into the second, then
        dropped the artifact entirely.

        Emphatically **not** approval. ``is_complete_review`` stays False while the
        verdict is unpublished, because repository rules cannot read a verdict that
        was never recorded; this property only separates the two questions so an
        operator-facing surface can say which one is outstanding.
        """
        return not set(self.approval_blockers) - set(self.publication_blockers)

    @property
    def publication_is_outstanding(self) -> bool:
        """The only thing standing between this artifact and a clean review is publishing it."""
        return bool(self.publication_blockers) and self.review_concluded_without_blockers


def parse_review_result(document: dict[str, Any]) -> Any:
    """Validate a submitted document against the shared contract.

    Structural validation only — this says the document is a well-formed review
    result, not that it describes work the submitter was entitled to review. That
    is :func:`validate_review_result`'s job, and separating them means a malformed
    payload is distinguishable from a forged one.

    Raises:
        ReviewEvidenceError: ``WRONG_CONTRACT`` when the envelope names another
            contract or version, ``MALFORMED_RESULT`` when it fails validation, and
            ``CONTRACT_UNAVAILABLE`` when the shared validator is missing.
    """
    from pydantic import ValidationError

    models = _contract_models()
    if not isinstance(document, dict):
        raise ReviewEvidenceError(ReviewEvidenceRefusal.MALFORMED_RESULT, "A review result must be a JSON object.")
    # Checked before validation so a v2 document produces "wrong contract" rather
    # than a field-level complaint that reads like a producer bug.
    name, version = document.get("name"), document.get("version")
    if name != REVIEW_CONTRACT_NAME or version != REVIEW_CONTRACT_VERSION:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.WRONG_CONTRACT,
            f"Expected contract {REVIEW_CONTRACT_NAME!r} version {REVIEW_CONTRACT_VERSION}, got {name!r} version {version!r}.",
        )
    try:
        return models.ReviewResult.model_validate(document)
    except ValidationError as error:
        # The validator's own message names the failing rule, which is the whole
        # diagnostic value for a producer. It contains no submitted secret: every
        # field in this contract is an identifier, a reference or a closed
        # vocabulary value.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.MALFORMED_RESULT,
            f"The review result did not validate against the contract: {error}",
        ) from None


async def resolve_expected_subject(
    session: AsyncSession,
    *,
    org_id: str,
    node_id: str,
    attempt: int,
) -> OrchestrationPullRequestBinding:
    """The pull request a review for this story is expected to be about.

    Resolved from the binding the server registered for the node's current attempt
    — never from the submitted result. A reviewer that names a different pull
    request is refused rather than believed, which is the arm the observed
    wrong-revision run needed: it published analysis of one commit and had it
    attached to another, and nothing compared the two.

    Every arm here is a refusal. In particular an ambiguous candidate is refused
    rather than resolved by picking the newest: two active bindings mean the
    association is genuinely unknown, and guessing would accept review evidence for
    an arbitrary pull request.

    There is no superseded arm here: ``active_binding_for_node`` queries on the active
    state, so a superseded row is simply not returned — the absence surfaces as
    ``NO_BINDING``. The ``SUPERSEDED_BINDING`` arm lives in
    :func:`validate_review_result`, which is reachable with a binding the caller
    resolved some other way.

    Raises:
        ReviewEvidenceError: ``NO_BINDING``, ``AMBIGUOUS_PR`` or ``NOT_IMPLEMENTATION``.
    """
    try:
        binding = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=attempt)
    except BindingError as error:
        if error.code is BindingRefusal.AMBIGUOUS_CANDIDATE:
            raise ReviewEvidenceError(
                ReviewEvidenceRefusal.AMBIGUOUS_PR,
                "Several pull requests are bound to this story, so which one was reviewed is unknown.",
            ) from None
        raise ReviewEvidenceError(ReviewEvidenceRefusal.NO_BINDING, f"The expected pull request could not be resolved: {error.code.value}.") from None
    if binding is None:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.NO_BINDING,
            "No pull request is registered for this story, so there is nothing a review can be evidence about.",
        )
    if binding.role != BindingRole.IMPLEMENTATION.value:
        # Reviewing a reviewer-artifact PR is the #4005 recursion shape. Its review
        # is not evidence about the story's delivery, however complete.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.NOT_IMPLEMENTATION,
            "The pull request bound to this story is a reviewer artifact; a review of it is not evidence about the delivered work.",
        )
    return binding


def validate_review_result(
    document: dict[str, Any],
    *,
    identity: ExecutionIdentity,
    binding: OrchestrationPullRequestBinding,
    flow_id: str,
    author_run_id: str,
    reviewer_run_id: str | None = None,
    execution_id: str | None = None,
    actual_head_sha: str | None = None,
    trusted_artifact_refs: frozenset[str] | None = None,
) -> ReviewEvidence:
    """Bind a submitted review result to protected state, or refuse it.

    ``identity``, ``binding``, ``flow_id`` and ``author_run_id`` must all be
    resolved by the caller from state the reviewer cannot write: the execution row's
    plan version and claim generation, the registered pull-request binding, and the
    dispatch record naming which run authored the change. Every corresponding field
    in the document is compared against them, so a forged scope, a forged repository
    or a forged author is a refusal rather than an accepted claim.

    ``reviewer_run_id`` is the run the transport *authenticated* as the producer of
    this artifact — not the run the document names. Supply it on any ingestion path
    where the artifact arrived over an authenticated channel. Checking only
    "reviewer differs from author" is insufficient and was reproduced as such: a
    substituted ``reviewer_run_id`` validated with ``is_complete_review=True``,
    which means the run credited with the review need not be the run that performed
    it. When omitted, no producer authentication is claimed and the document's
    reviewer is treated as unverified — which the ``REVIEWER_UNVERIFIED`` arm
    reports rather than passing off as bound.

    ``execution_id`` is the execution the server resolved for this node and cycle.
    The contract lets a pre-submission producer leave ``scope.execution_id`` unset
    because the runtime may not know it yet, but a *persisted* artifact must be
    bound to the exact execution: an unbound or model-substituted execution is
    refused here rather than recorded against whichever row the caller happened to
    load.

    ``actual_head_sha`` is the provider's current head when the caller has read it.
    When supplied and different from the bound head, it is used for the stale-head
    comparison — a head that moved after binding invalidates the review even though
    the document and the binding agree with each other.

    ``trusted_artifact_refs``, when supplied, is the set of references the caller
    could verify. A head-bound reference outside that set is refused as
    ``UNTRUSTED_ARTIFACT`` rather than silently counted: an unverifiable test result
    supports no conclusion. Left ``None``, no artifact verification is claimed and
    none is performed — the caller is stating it did not check, which keeps "not
    checked" distinguishable from "checked and trusted".

    Returns:
        ReviewEvidence: validated and bound. Carries ``approval_blockers``; an empty
        tuple means the artifact is complete, NOT that the change may merge.

    Raises:
        ReviewEvidenceError: with the specific arm that refused.
    """
    result = parse_review_result(document)
    models = _contract_models()

    if binding.state != BindingState.ACTIVE.value:
        # Checked here rather than trusting the caller's resolution path: a binding
        # that stopped being current is permanently incapable of completing the new
        # scope, and a review of it is evidence about superseded work.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.SUPERSEDED_BINDING,
            "The pull request this review is bound to is no longer the current binding for this story.",
        )
    if result.scope.org_id != identity.org_id:
        # First, because a cross-tenant document must not have any of its other
        # fields compared against this tenant's state.
        raise ReviewEvidenceError(ReviewEvidenceRefusal.TENANT_MISMATCH, "The review result names another tenant.")
    if result.scope.node_id != identity.node_id or result.scope.cycle != identity.cycle or result.scope.flow_id != flow_id:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.SCOPE_MISMATCH,
            f"The review result names flow/node/cycle {result.scope.flow_id}/{result.scope.node_id}/{result.scope.cycle}, "
            f"but this execution is {flow_id}/{identity.node_id}/{identity.cycle}.",
        )
    if result.authority.accepted_plan_version != identity.accepted_plan_version:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.POLICY_MISMATCH,
            "The review result was authorized under a different accepted plan version than the one in force.",
        )
    if result.authority.claim_id != identity.claim_id or result.authority.claim_generation != identity.claim_generation:
        # The generation is the ownership fence. A result produced under an older
        # generation describes work someone else now owns; accepting it would let a
        # superseded run's evidence advance the current owner's delivery.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.STALE_CLAIM,
            "The review result was produced under a claim generation that is no longer current, so another run now owns this work.",
        )

    if result.repository.provider_repository_id != binding.provider_repository_id:
        # Compared on the immutable id, not the name: a rename or transfer
        # re-points `repo`, and the observed failures include findings landing on
        # the wrong repository.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.REPOSITORY_MISMATCH,
            "The review result names a different repository than the one bound to this story.",
        )
    if result.subject.provider_pr_node_id != binding.provider_pr_node_id or result.subject.pr_number != binding.pr_number:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.PR_MISMATCH,
            f"The review result is about pull request #{result.subject.pr_number}, but #{binding.pr_number} is bound to this story.",
        )

    if result.lineage.author_run_id != author_run_id:
        # A submitted author id is a claim; the dispatch record is the fact. A
        # reviewer naming some other run as author could otherwise defeat the
        # self-review check by pointing it at a run it is not.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.SELF_REVIEW,
            "The review result names a different authoring run than the one that delivered this work, so self-review cannot be ruled out.",
        )
    if result.lineage.reviewer_run_id == author_run_id:
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.SELF_REVIEW,
            "The reviewing run is the run that authored the change. Note that a distinct reviewer run still does "
            "not satisfy the provider's independent-approval requirement.",
        )
    if reviewer_run_id is not None and result.lineage.reviewer_run_id != reviewer_run_id:
        # The self-review check above is necessary and not sufficient: it only proves
        # the *named* reviewer differs from the author. A substituted reviewer id
        # passes it while crediting the review to a run that never performed it,
        # which is the reproduced finding. The authenticated producer is the fact.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.REVIEWER_MISMATCH,
            "The review result names a reviewing run that is not the authenticated producer of this artifact.",
        )
    if execution_id is not None and result.scope.execution_id != execution_id:
        # Covers both "names someone else's execution" and "names none at all". A
        # pre-submission producer may legitimately omit it, but evidence about to be
        # persisted must be bound to the exact execution the server resolved — an
        # unbound artifact recorded against a caller-selected row is how evidence
        # ends up attached to work it did not examine.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.EXECUTION_UNBOUND if result.scope.execution_id is None else ReviewEvidenceRefusal.EXECUTION_MISMATCH,
            "The review result does not name the execution this review belongs to."
            if result.scope.execution_id is None
            else "The review result names a different execution than the one resolved for this story.",
        )

    # What this validation was NOT given, recorded before the remaining comparisons
    # so it reaches the caller even on the happy path. Each entry names a protected
    # input the caller did not supply, which means the corresponding comparison above
    # never ran. Kept as prose an operator can read rather than a bare flag, because
    # the actionable part is *which* check is missing.
    unverified: list[str] = []
    if reviewer_run_id is None:
        unverified.append("the reviewing run was not authenticated against the artifact's producer")
    if execution_id is None:
        unverified.append("the execution this review belongs to was not resolved server-side")
    if actual_head_sha is None:
        # The binding's head is a cached value written when the binding was
        # registered. Comparing against it alone cannot establish that no later push
        # moved the head, which is the whole point of binding review to an exact
        # revision.
        unverified.append("the provider's current head was not read, so a later push may have invalidated this review")
    if trusted_artifact_refs is None and _head_bound_refs(result):
        # Only when the result actually leans on head-bound references. A review
        # citing none has nothing to verify, and reporting a missing check that does
        # not apply would train a reader to ignore the field.
        unverified.append("the head-bound artifact references this review relies on were not verified")

    current_head = actual_head_sha or binding.head_sha
    if result.subject.reviewed_head_sha != current_head:
        # Refused rather than invalidated-and-accepted. `invalidate_for_head` exists
        # for a caller that wants to retain the downgraded artifact; as *evidence*
        # for the current revision, a review of different code is simply not it.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.STALE_HEAD,
            f"The review examined commit {result.subject.reviewed_head_sha}, but the pull request's current head is {current_head}, "
            "so its findings and test evidence describe different code.",
        )

    blockers = list(result.approval_blockers())
    if trusted_artifact_refs is not None:
        untrusted = [ref.ref for ref in _head_bound_refs(result) if ref.ref not in trusted_artifact_refs]
        if untrusted:
            raise ReviewEvidenceError(
                ReviewEvidenceRefusal.UNTRUSTED_ARTIFACT,
                f"The review relies on {len(untrusted)} head-bound artifact reference(s) that could not be verified; "
                "unverifiable test evidence supports no conclusion.",
            )

    # Belt and braces against a future contract edit that softens the mandatory
    # functional stage: a result whose functional stage did not conclude must never
    # reach a caller without that reason attached.
    functional = result.stage(models.ReviewStageName.FUNCTIONAL)
    if (functional is None or functional.outcome not in models.CONCLUSIVE_STAGE_OUTCOMES) and not blockers:
        blockers.append("the functional review stage did not conclude")

    # Carried separately, not recomputed by the consumer: the contract owns which
    # reasons are publication reasons, and a second local definition here is how the
    # two sides drift. Read defensively because a producer pinned to an older
    # contract build may not expose the split yet — and in that case an empty tuple
    # is the safe answer, since it makes `publication_is_outstanding` False rather
    # than claiming a clean review.
    publication_blockers = tuple(getattr(result, "publication_blockers", lambda: ())())

    return ReviewEvidence(
        result=result,
        artifact_ref=review_artifact_ref(result),
        reviewed_head_sha=result.subject.reviewed_head_sha,
        repo=binding.repo,
        pr_number=binding.pr_number,
        approval_blockers=tuple(blockers),
        publication_blockers=publication_blockers,
        unverified=tuple(unverified),
    )


#: Which refusal arm reports each unchecked protected input. Keyed on the same order
#: the validator records them so a caller converting "unverified" into a typed block
#: does not have to re-derive the mapping — and so a newly recorded gap without an
#: arm fails loudly in :func:`require_verified_state` rather than being skipped.
_UNVERIFIED_ARMS: tuple[tuple[str, ReviewEvidenceRefusal], ...] = (
    ("was not authenticated", ReviewEvidenceRefusal.REVIEWER_UNVERIFIED),
    ("was not resolved server-side", ReviewEvidenceRefusal.EXECUTION_UNVERIFIED),
    ("current head was not read", ReviewEvidenceRefusal.HEAD_UNVERIFIED),
    ("references this review relies on were not verified", ReviewEvidenceRefusal.ARTIFACTS_UNVERIFIED),
)


def require_verified_state(evidence: ReviewEvidence) -> None:
    """Refuse evidence whose protected inputs were never checked.

    The fail-closed gate for any path that *persists* or *acts on* review evidence.
    :func:`validate_review_result` deliberately still accepts a partially-checked
    document — a pre-submission producer validating its own draft has no
    authenticated producer, no resolved execution and no provider read to offer, and
    forcing it to invent them would be worse. But the production observer does have
    all four, and a reproduced finding showed what happens when that difference is
    left to the caller's discipline: with all four omitted the result reported
    ``is_complete_review=True`` and ``REVIEWER_UNVERIFIED`` was never raised at all,
    existing only in the enum.

    So the rule lives here rather than in each caller. A caller that genuinely only
    wants structural validation simply does not call this; a caller recording
    evidence calls it and cannot forget a check it never passed.

    Raises:
        ReviewEvidenceError: the arm for the first unchecked input, in the order the
            validator records them — producer, execution, head, artifacts.
    """
    for reason in evidence.unverified:
        for fragment, arm in _UNVERIFIED_ARMS:
            if fragment in reason:
                raise ReviewEvidenceError(
                    arm,
                    f"This review evidence cannot be recorded as trusted: {reason}.",
                )
        # An unverified reason with no mapped arm is a bug in this module, not an
        # acceptable pass. Refused generically rather than ignored.
        raise ReviewEvidenceError(
            ReviewEvidenceRefusal.REVIEWER_UNVERIFIED,
            f"This review evidence cannot be recorded as trusted: {reason}.",
        )


def _head_bound_refs(result: Any) -> list[Any]:
    """Every head-bound evidence reference in the result, finding-level included.

    Finding-level references are included because that is where a "resolved"
    blocking finding's proof lives, and an unverifiable proof of a fix is exactly
    the false-negative the observations recorded.
    """
    refs = [ref for ref in result.evidence_refs if ref.head_bound]
    for finding in result.findings:
        refs.extend(ref for ref in finding.evidence_refs if ref.head_bound)
    return refs


def _result_identity(result: Any) -> str:
    """The part of a key that distinguishes one review *result* from another.

    The work coordinates alone are not an identity. Two genuinely different review
    results can share tenant, node, cycle and reviewed commit — a first reviewer
    whose verdict failed to publish, and a second authorized run that published on
    the unchanged head. Folding in ``result_id`` and the reviewing run keeps those
    two distinct while a *replay of the same result* still converges.

    ``result_id`` is producer-chosen and unbounded in the contract, and the ledger's
    ``operation_key`` column is ``String(255)``. So the identity is hashed rather
    than interpolated: a long ``result_id`` must not silently push the key over the
    limit, where it would either raise at persistence time or truncate two distinct
    results back into one. The components are length-prefixed before hashing for the
    reason ``execution_runner.OperationIdentity.key`` gives — a bare ``:`` join is
    not injective, so two different (result, reviewer) pairs could otherwise flatten
    to the same digest.
    """
    parts = (str(result.result_id), str(result.lineage.reviewer_run_id))
    material = ":".join(f"{len(part)}={part}" for part in parts)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def review_artifact_ref(result: Any) -> str:
    """The stable reference under which this review result is recorded.

    Derived from the work — tenant, node, cycle and reviewed commit — *plus* the
    identity of the result itself, rather than generated per submission. The work
    coordinates keep the reference meaningful to an operator; the result identity is
    what makes it a reference to *this* review.

    A resubmission of the same result converges on this same string, so a retry is
    still one review. A different result at the same head — the reproduced
    failed-publication-then-authorized-success sequence — gets its own reference
    instead of colliding with the first and being refused as already settled.
    """
    scope = result.scope
    return f"review:{scope.org_id}:{scope.node_id}:cycle-{scope.cycle}:{result.subject.reviewed_head_sha}:{_result_identity(result)}"


def evidence_operation_key(evidence: ReviewEvidence) -> str:
    """Idempotency key for recording this evidence.

    Scoped to the real result identity, not the head. Keying on the work alone made
    two different complete results at one commit share a single ledger action —
    reproduced on real PostgreSQL: an incomplete review whose publication failed
    with HTTP 401 was recorded and settled first, and the later authorized approval
    at the *unchanged* head returned ``CONFLICT`` / ``action_already_settled``,
    because a settled observation is correctly immutable. The defect was the key, so
    that is what this fixes; the store's immutability is the property being relied
    on, not worked around.

    A replay of the same result still produces the same key, so the duplicate path
    (``observation_already_recorded``) continues to absorb retries.
    """
    scope = evidence.result.scope
    return f"record_review:{scope.node_id}:cycle-{scope.cycle}:{evidence.reviewed_head_sha}:{_result_identity(evidence.result)}"


def evidence_action_intent(evidence: ReviewEvidence) -> ActionIntent:
    """The ledger action that records this review evidence.

    ``artifact_ref`` is a reference and ``detail`` is small, non-sensitive operator
    context — which pull request, which commit, whether the artifact is complete.
    Neither carries the document, a transcript or a credential: these rows are read
    by operators and surfaced in diagnostics, so a secret written here would be a
    disclosure with no revocation path.
    """
    return ActionIntent(
        operation_key=evidence_operation_key(evidence),
        kind="review_evidence",
        artifact_ref=evidence.artifact_ref,
        detail={
            "repo": evidence.repo,
            "pr_number": str(evidence.pr_number),
            "reviewed_head_sha": evidence.reviewed_head_sha,
            "verdict": str(evidence.result.verdict),
            "publication": str(evidence.result.publication.outcome),
            "complete_review": "true" if evidence.is_complete_review else "false",
            # The recorded case this exists for: verdict approve, publication 401.
            # Without this the row shows an incomplete review and an operator cannot
            # tell "retry the publication" from "the reviewer found a problem".
            "publication_outstanding": "true" if evidence.publication_is_outstanding else "false",
        },
    )


def evidence_observation(evidence: ReviewEvidence) -> Observation:
    """The observation settling the recorded evidence.

    ``SUCCEEDED`` records that the evidence was validated and stored — not that the
    review approved anything. An incomplete review is a successfully recorded
    incomplete review, and flattening that into ``FAILED`` would lose the artifact
    the operator needs. ``detail`` carries the blocker reasons so the *why* survives
    in the row rather than only in the caller's logs.
    """
    reasons = "; ".join(evidence.approval_blockers) if evidence.approval_blockers else "no approval blockers in this artifact"
    return Observation(
        operation_key=evidence_operation_key(evidence),
        outcome=ObservedOutcome.SUCCEEDED,
        receipt_ref=evidence.artifact_ref,
        # Bounded so a long finding list cannot turn an operator-facing column into
        # a transcript dump.
        detail=reasons[:900],
    )


def outstanding_block(refusal: ReviewEvidenceRefusal, *, owner: str) -> BlockRecord:
    """A typed block for review evidence that could not be accepted.

    Exists so a refusal persists a resolvable condition and an owner rather than the
    caller inventing free text or — far worse — treating unverifiable review output
    as a completed review. The code mapping is deliberate:
    ``CONTRACT_UNAVAILABLE`` is a provider/platform condition, the identity and
    binding arms need an operator decision, and everything else is a dependency the
    reviewer must satisfy by running again against the current revision.
    """
    if refusal is ReviewEvidenceRefusal.CONTRACT_UNAVAILABLE:
        code = BlockCode.PROVIDER_UNAVAILABLE
    elif refusal in {
        ReviewEvidenceRefusal.NO_BINDING,
        ReviewEvidenceRefusal.AMBIGUOUS_PR,
        ReviewEvidenceRefusal.NOT_IMPLEMENTATION,
        ReviewEvidenceRefusal.SUPERSEDED_BINDING,
        ReviewEvidenceRefusal.REPOSITORY_MISMATCH,
        ReviewEvidenceRefusal.PR_MISMATCH,
    }:
        code = BlockCode.HUMAN_INPUT_REQUIRED
    elif refusal in {
        ReviewEvidenceRefusal.TENANT_MISMATCH,
        ReviewEvidenceRefusal.SCOPE_MISMATCH,
        ReviewEvidenceRefusal.POLICY_MISMATCH,
        ReviewEvidenceRefusal.STALE_CLAIM,
        # A reviewer or execution that cannot be attributed to authenticated state is
        # an authority condition, not a retry: running the reviewer again over the
        # same unauthenticated path would produce the same unattributable artifact.
        ReviewEvidenceRefusal.REVIEWER_MISMATCH,
        ReviewEvidenceRefusal.REVIEWER_UNVERIFIED,
        ReviewEvidenceRefusal.EXECUTION_MISMATCH,
        ReviewEvidenceRefusal.EXECUTION_UNBOUND,
        ReviewEvidenceRefusal.EXECUTION_UNVERIFIED,
    }:
        code = BlockCode.AUTHORITY_UNVERIFIABLE
    elif refusal in {
        # The ingestion path did not read the provider or check the referenced
        # artifacts. Neither is the reviewer's fault and neither is resolved by an
        # operator decision — the platform condition has to clear first.
        ReviewEvidenceRefusal.HEAD_UNVERIFIED,
        ReviewEvidenceRefusal.ARTIFACTS_UNVERIFIED,
    }:
        code = BlockCode.PROVIDER_UNAVAILABLE
    else:
        code = BlockCode.DEPENDENCY_UNSATISFIED
    return BlockRecord(code=code, owner=owner, required_input=refusal_explanation(refusal), detail=f"review_evidence_refusal={refusal.value}")


def refusal_explanation(refusal: ReviewEvidenceRefusal) -> str:
    """Operator-facing prose for a refusal, so a held story explains itself.

    The observed failures were expensive to diagnose because "the reviewer ran and
    the story did not move" was true, unactionable, and in several cases describing
    something that could never resolve on its own. Each reason below names what is
    missing and who can resolve it.
    """
    return {
        ReviewEvidenceRefusal.MALFORMED_RESULT: (
            "The reviewer's structured result did not match the review-result contract, so it cannot be read as evidence. "
            "The reviewer must emit a valid result; the validation message names the failing rule."
        ),
        ReviewEvidenceRefusal.CONTRACT_UNAVAILABLE: (
            "The shared review-result contract could not be loaded, so no review result can be validated. "
            "This is a platform condition, not a reviewer error."
        ),
        ReviewEvidenceRefusal.WRONG_CONTRACT: (
            "The submitted document belongs to a different contract or version than the review-result contract this surface reads."
        ),
        ReviewEvidenceRefusal.NO_BINDING: (
            "No pull request is registered for this story, so a review has nothing to be evidence about. "
            "An operator can record the implementing pull request through the attributed recovery path."
        ),
        ReviewEvidenceRefusal.AMBIGUOUS_PR: (
            "Several pull requests are bound to this story, so which one was reviewed is unknown. An operator must supersede the incorrect binding."
        ),
        ReviewEvidenceRefusal.NOT_IMPLEMENTATION: (
            "The pull request bound to this story is a reviewer artifact, and a review of it is not evidence about the delivered work."
        ),
        ReviewEvidenceRefusal.SUPERSEDED_BINDING: (
            "The pull request bound to this story was superseded; the replacement must be registered before a review of it counts."
        ),
        ReviewEvidenceRefusal.REPOSITORY_MISMATCH: (
            "The review result names a different repository than the one bound to this story, so it describes code from somewhere else."
        ),
        ReviewEvidenceRefusal.PR_MISMATCH: "The review result is about a different pull request than the one bound to this story.",
        ReviewEvidenceRefusal.TENANT_MISMATCH: "The review result names another tenant and cannot be read against this one's state.",
        ReviewEvidenceRefusal.SCOPE_MISMATCH: (
            "The review result names a different flow, story or delivery cycle than the execution it was "
            "submitted for, so it is evidence about other work."
        ),
        ReviewEvidenceRefusal.POLICY_MISMATCH: (
            "The review result was authorized under a different accepted plan version than the one in force, "
            "so the plan it was produced against is no longer current."
        ),
        ReviewEvidenceRefusal.STALE_CLAIM: (
            "The review result was produced under a claim generation that is no longer current, so another run "
            "now owns this work. The current owner must review again."
        ),
        ReviewEvidenceRefusal.REVIEWER_MISMATCH: (
            "The review result credits a reviewing run that is not the authenticated producer of the artifact, so who "
            "actually performed this review cannot be established. The reviewer must publish its result over its own "
            "authenticated run identity."
        ),
        ReviewEvidenceRefusal.REVIEWER_UNVERIFIED: (
            "The review result arrived without an authenticated producer to attribute it to, so the reviewing run is "
            "self-declared. Evidence must be published through the run's authenticated lineage."
        ),
        ReviewEvidenceRefusal.EXECUTION_MISMATCH: (
            "The review result names a different execution than the one resolved for this story, so it is evidence about another attempt."
        ),
        ReviewEvidenceRefusal.EXECUTION_UNBOUND: (
            "The review result does not name the execution it belongs to, so persisted evidence could not be bound to the "
            "work it examined. The producer must include the execution it was dispatched for."
        ),
        ReviewEvidenceRefusal.EXECUTION_UNVERIFIED: (
            "The review evidence was validated without resolving which execution it belongs to, so it cannot be recorded "
            "against a specific attempt. This is a platform condition: the ingestion path must resolve the execution before persisting."
        ),
        ReviewEvidenceRefusal.HEAD_UNVERIFIED: (
            "The provider's current head was never read, so this review could have been invalidated by a later push without "
            "anything noticing. This is a platform condition: the ingestion path must read the pull request's head before persisting."
        ),
        ReviewEvidenceRefusal.ARTIFACTS_UNVERIFIED: (
            "The review relies on head-bound test or artifact references that were never verified, so the evidence behind its "
            "conclusion is unconfirmed. This is a platform condition: the ingestion path must verify referenced artifacts before persisting."
        ),
        ReviewEvidenceRefusal.STALE_HEAD: (
            "The reviewed commit is not the pull request's current head, so the review's findings and test evidence describe different code. "
            "A fresh review of the current head is required."
        ),
        ReviewEvidenceRefusal.SELF_REVIEW: (
            "The reviewing run is the run that authored the change, so the review is not independent. A different run must review it, "
            "and the provider's own approving-review-from-a-non-author requirement still applies separately."
        ),
        ReviewEvidenceRefusal.UNTRUSTED_ARTIFACT: (
            "The review relies on test or check artifacts that could not be verified, so its conclusions are "
            "not supported. The reviewer must re-run them against the current head."
        ),
    }.get(refusal, f"Review evidence was refused: {refusal.value}")


async def record_review_evidence(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    evidence: ReviewEvidence,
) -> ExecutionOutcome:
    """Persist references to validated review evidence through the execution ledger.

    One prepared action carrying the artifact reference. No new table and no new
    endpoint: the ledger (#5142) already models "an externally-visible step and
    what was observed about it", and this is that. The store re-verifies the claim
    generation inside its own transaction, so a generation that advanced between
    validation and this write is a ``CONFLICT`` rather than an accepted record.

    Deliberately does **not** advance a phase. Recording that a review happened is
    not deciding what follows from it, and this module dispatches nothing.

    Returns the store's outcome unchanged, including ``STALE``/``CONFLICT``, so the
    caller sees which arm fired rather than a swallowed failure.

    Raises:
        ReviewEvidenceError: when the evidence carries an unchecked protected input.
            Persisting is the point at which an optional check stops being optional:
            a stored row is what later readers treat as fact, and a row written from
            a validation that never authenticated the producer, resolved the
            execution, read the provider's head or verified the referenced artifacts
            would make "not checked" indistinguishable from "checked". Enforced here,
            at the write, so no ingestion path can reach durable storage around it.
    """
    from .execution_store import prepare_action, record_observation

    require_verified_state(evidence)

    prepared = await prepare_action(session, identity=identity, intent=evidence_action_intent(evidence))
    if prepared.kind is not OutcomeKind.APPLIED or prepared.action is None:
        # A conflict or block means the ledger declined the write; observing against
        # an action that does not exist would be worse than reporting the refusal.
        return prepared
    return await record_observation(session, identity=identity, observation=evidence_observation(evidence))


def evidence_summary(evidence: ReviewEvidence) -> dict[str, object]:
    """The review evidence as a read surface would present it.

    Deliberately excludes the findings' prose and every artifact reference beyond
    the top-level one: the reasons are already summarised in ``approval_blockers``,
    and a read surface that echoed each reference would leak the shape of internal
    storage under a read permission. Mirrors ``pr_bindings.binding_summary``'s
    reasoning about what a story API should and should not surface.
    """
    result = evidence.result
    return {
        "repo": evidence.repo,
        "pr_number": evidence.pr_number,
        "reviewed_head_sha": evidence.reviewed_head_sha,
        "verdict": str(result.verdict),
        "publication_outcome": str(result.publication.outcome),
        "blocking_findings": len(result.blocking_findings),
        "approval_blockers": list(evidence.approval_blockers),
        "complete_review": evidence.is_complete_review,
        # Which protected checks this validation did not perform. Surfaced beside
        # `complete_review` rather than only inside it: a reader seeing an incomplete
        # review needs to know whether the reviewer found something or whether the
        # ingestion path simply never looked, and those call for opposite responses.
        "unverified": list(evidence.unverified),
        # Distinct from `complete_review`, which stays False for an unpublished
        # verdict. This says *why* it is false: the reviewing work concluded and only
        # the formal publication is outstanding. Still not an approval, and a caller
        # must not read it as one — `complete_review` is the field that answers that.
        "publication_outstanding": evidence.publication_is_outstanding,
        "observed_at": result.observed_at.isoformat(),
    }


def legacy_review_note(text: str) -> dict[str, object]:
    """Parse pre-contract reviewer output into a non-authoritative note.

    Existing reviewer runs emit prose, and that must keep working: a worker which
    has not adopted the contract still produces output an operator reads. What it
    must NOT do is become a review verdict, which is why this returns a plain note
    marked ``authoritative: False`` and carrying no verdict field at all.

    There is deliberately no path from this function to :class:`ReviewEvidence`. The
    whole defect was prose being read as a conclusion; a compatibility shim that
    produced evidence from text would reintroduce it with a contract's blessing.
    """
    body = (text or "").strip()
    return {
        "kind": "legacy_review_output",
        "authoritative": False,
        "grants_approval": False,
        # Bounded for the same reason the observation detail is: an operator column
        # is not a transcript store.
        "text": body[:2000],
        "note": "Legacy reviewer output, retained for operators. It is not bound to a reviewed commit and grants no approval.",
    }


def evidence_decision_snapshot(evidence: ReviewEvidence, *, now: datetime) -> str:
    """A compact, sorted JSON snapshot for the existing append-only decision store.

    Sorted keys so two snapshots of the same evidence compare equal, matching
    ``pr_bindings.binding_snapshot``'s convention. References and counts only —
    never the findings' text, which belongs with the artifact the reference points
    at.
    """
    return json.dumps(
        {
            "artifact_ref": evidence.artifact_ref,
            "repo": evidence.repo,
            "pr_number": evidence.pr_number,
            "reviewed_head_sha": evidence.reviewed_head_sha,
            "verdict": str(evidence.result.verdict),
            "publication_outcome": str(evidence.result.publication.outcome),
            "blocking_finding_ids": sorted(finding.finding_id for finding in evidence.result.blocking_findings),
            "approval_blocker_count": len(evidence.approval_blockers),
            "complete_review": evidence.is_complete_review,
            "recorded_at": now.isoformat(),
        },
        sort_keys=True,
    )
