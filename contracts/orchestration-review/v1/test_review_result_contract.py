"""Executes the orchestration-review golden fixture against the normative models (#5146).

Run by `.github/workflows/orchestration-review-contract-tests.yml`. A golden fixture
prevents nothing if nothing executes it — the failure mode
`provenance-contract-tests.yml:11` calls out by name — and this file is this
contract's answer to it.

Structure mirrors `contracts/hitl-ticket/v1/test_hitl_ticket_contract.py`: accepted
documents must validate, every rejected variant must be REJECTED, and a missing
fixture fails loudly rather than skipping the contract silently.

Beyond schema mechanics, the classes at the bottom pin the three behaviours the
issue's observations demand — a security-only result cannot read as clean, a head
change invalidates dispositions, and approval is computed from reasons rather than
asserted. Those are the rules a future edit is most likely to soften.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from models import (
    CLEARED_DISPOSITIONS,
    CONCLUSIVE_STAGE_OUTCOMES,
    PUBLICATION_ACCEPTED,
    FindingDisposition,
    FindingSeverity,
    PublicationOutcome,
    ReviewResult,
    ReviewStageName,
    ReviewVerdict,
    StageOutcome,
    invalidate_for_head,
)

GOLDEN_PATH = _HERE / "review-result.golden.json"

with GOLDEN_PATH.open() as fh:
    GOLDEN = json.load(fh)


def _strip_comments(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop `$comment` keys, which are documentation and not part of the shape."""
    return {k: v for k, v in doc.items() if not k.startswith("$")}


ACCEPTED_KEYS = [key for key in GOLDEN if key.startswith("accepted_")]
ACCEPTED = {key: _strip_comments(GOLDEN[key]) for key in ACCEPTED_KEYS}
APPROVE_DOC = ACCEPTED["accepted_result_approve"]
SECURITY_ONLY_DOC = ACCEPTED["accepted_result_security_only_incomplete"]
REFUSED_DOC = ACCEPTED["accepted_result_publication_refused"]
PUBLICATION_FAILED_DOC = ACCEPTED["accepted_result_approve_publication_failed"]
VARIANTS = GOLDEN["rejected_variants"]


def _variant(base: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Apply a rejected-variant spec to a base document."""
    body = dict(base)
    body.update(spec.get("patch", {}))
    for key in spec.get("unset", []):
        body.pop(key, None)
    return body


def _ids(variants: list[dict[str, Any]]) -> list[str]:
    return [v["name"] for v in variants]


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


class TestFixtureIntegrity:
    def test_golden_fixture_exists(self):
        """A missing fixture must fail loudly, not skip the contract silently."""
        assert GOLDEN_PATH.is_file(), (
            f"golden contract fixture not found at {GOLDEN_PATH}"
        )

    def test_fixture_declares_rejected_variants(self):
        """Guards against a future edit that empties the variant list.

        Every parametrized rejection test below would vacuously pass with zero
        variants, and CI would stay green while enforcing nothing.
        """
        assert VARIANTS, "rejected_variants must not be empty"

    def test_fixture_declares_accepted_documents(self):
        assert ACCEPTED, "the fixture must carry at least one accepted document"

    def test_every_variant_explains_itself(self):
        """A variant with no `why` is a rule nobody can maintain."""
        for spec in VARIANTS:
            assert spec.get("why", "").strip(), (
                f"variant {spec['name']!r} must say why it is rejected"
            )

    def test_variant_names_are_unique(self):
        names = _ids(VARIANTS)
        assert len(set(names)) == len(names), "rejected variant names must be unique"


# ---------------------------------------------------------------------------
# Accepted documents
# ---------------------------------------------------------------------------


class TestAcceptedDocuments:
    @pytest.mark.parametrize("key", ACCEPTED_KEYS)
    def test_accepted_document_validates(self, key: str):
        ReviewResult.model_validate(ACCEPTED[key])

    @pytest.mark.parametrize("key", ACCEPTED_KEYS)
    def test_accepted_document_round_trips(self, key: str):
        """Serialize and re-validate: the wire form must survive a round trip.

        This is what makes one shared artifact usable by both sides. If dumping a
        validated result produced something the models reject, the producer could
        not send what the consumer reads.
        """
        result = ReviewResult.model_validate(ACCEPTED[key])
        again = ReviewResult.model_validate(json.loads(result.model_dump_json()))
        assert again == result


# ---------------------------------------------------------------------------
# Rejected variants
# ---------------------------------------------------------------------------


class TestRejectedVariants:
    @pytest.mark.parametrize("spec", VARIANTS, ids=_ids(VARIANTS))
    def test_variant_is_rejected(self, spec: dict[str, Any]):
        body = _variant(APPROVE_DOC, spec)
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(body)


# ---------------------------------------------------------------------------
# The rules the observations demand
# ---------------------------------------------------------------------------


class TestFunctionalStageIsMandatory:
    """A result carrying only a security stage must not validate.

    The recorded runs published clean security reports while real functional
    blockers stayed in the execution trace. Rejecting the shape at the contract
    boundary means a security-only run has to say 'functional: not-run' and give a
    reason, which is representable and unmistakably non-approving.
    """

    def test_security_only_result_is_rejected(self):
        body = dict(APPROVE_DOC)
        body["stages"] = [{"name": "security", "outcome": "completed"}]
        with pytest.raises(ValidationError, match="functional"):
            ReviewResult.model_validate(body)

    def test_honest_security_only_result_validates_but_cannot_approve(self):
        result = ReviewResult.model_validate(SECURITY_ONLY_DOC)
        assert result.verdict is ReviewVerdict.INCOMPLETE
        assert result.stage(ReviewStageName.FUNCTIONAL).outcome is StageOutcome.NOT_RUN
        blockers = result.approval_blockers()
        assert blockers, "a security-only result must never be approval-capable"
        assert any("functional" in reason for reason in blockers)

    def test_blocking_finding_survives_security_filtering(self):
        """The U19 shape: a correctness blocker the scanner filtered below threshold.

        It is still a functional finding, it is still blocking, and it must appear in
        the approval blockers even though the security stage completed cleanly.
        """
        result = ReviewResult.model_validate(SECURITY_ONLY_DOC)
        assert result.stage(ReviewStageName.SECURITY).outcome is StageOutcome.COMPLETED
        assert [f.finding_id for f in result.blocking_findings] == [
            "destructive-apply-gate-fails-open"
        ]
        assert any(
            "destructive-apply-gate-fails-open" in reason
            for reason in result.approval_blockers()
        )


class TestPublicationOutcomeIsRecorded:
    """Completing review is not publishing a verdict.

    Every observed run completed its stages, posted prose, exited successfully, and
    left the pull request's review list empty. `publication` is mandatory so that
    state is recorded rather than inferred from silence.
    """

    def test_unpublished_verdict_blocks_approval(self):
        body = dict(APPROVE_DOC)
        body["publication"] = {
            "outcome": "not-attempted",
            "detail": "the run ended before publishing",
        }
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(body)

    def test_refusal_is_reported_not_smoothed(self):
        result = ReviewResult.model_validate(REFUSED_DOC)
        assert result.publication.outcome is PublicationOutcome.REFUSED
        assert result.publication.detail
        assert any("not published" in reason for reason in result.approval_blockers())

    def test_non_published_outcome_must_explain_itself(self):
        for outcome in ("refused", "failed", "not-attempted"):
            body = dict(REFUSED_DOC)
            body["publication"] = {"outcome": outcome}
            with pytest.raises(ValidationError, match="detail"):
                ReviewResult.model_validate(body)

    def test_verdict_attached_to_another_commit_is_rejected(self):
        """The wrong-revision defect, refused structurally."""
        body = dict(APPROVE_DOC)
        body["publication"] = {
            "outcome": "published",
            "published_head_sha": "f0d2eb968cb5f9d1322da48d92042cd7f45c166a",
            "reference": "pullrequestreview-9999999999",
        }
        with pytest.raises(ValidationError, match="reviewed_head_sha"):
            ReviewResult.model_validate(body)

    def test_published_outcome_is_the_only_accepted_one(self):
        assert PUBLICATION_ACCEPTED == {PublicationOutcome.PUBLISHED}


class TestSelfReview:
    def test_reviewer_run_must_differ_from_author_run(self):
        body = dict(APPROVE_DOC)
        body["lineage"] = {"author_run_id": "run-x", "reviewer_run_id": "run-x"}
        with pytest.raises(ValidationError, match="cannot review its own output"):
            ReviewResult.model_validate(body)

    def test_distinct_runs_do_not_imply_provider_approval(self):
        """The check is necessary, not sufficient — the error text says so, because a
        reader who sees only 'reviewer != author enforced' may assume the
        independent-approval requirement is discharged. It is not; that lives in
        `pr_bindings.evidence_for_binding`.
        """
        body = dict(APPROVE_DOC)
        body["lineage"] = {"author_run_id": "run-x", "reviewer_run_id": "run-x"}
        with pytest.raises(ValidationError, match="not sufficient"):
            ReviewResult.model_validate(body)


class TestBlockingFindingsGateApproval:
    @pytest.mark.parametrize("disposition", ["open", "acknowledged", "stale-head"])
    def test_uncleared_blocking_finding_blocks_approval(self, disposition: str):
        body = dict(APPROVE_DOC)
        body["findings"] = [
            {
                "finding_id": "blocker-1",
                "stage": "functional",
                "severity": "blocking",
                "disposition": disposition,
                "summary": "A blocking defect.",
                "evidence_refs": [],
            }
        ]
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(body)

    def test_resolved_blocking_finding_with_evidence_permits_approval(self):
        body = dict(APPROVE_DOC)
        body["findings"] = [
            {
                "finding_id": "blocker-1",
                "stage": "functional",
                "severity": "blocking",
                "disposition": "resolved",
                "summary": "A blocking defect, re-tested at this head.",
                "evidence_refs": [
                    {"kind": "test-run", "ref": "check-run:1", "head_bound": True}
                ],
            }
        ]
        result = ReviewResult.model_validate(body)
        assert result.approval_blockers() == ()

    def test_resolved_blocking_finding_needs_evidence(self):
        """'Fixed, trust me' is the false-negative shape and is refused."""
        body = dict(APPROVE_DOC)
        body["findings"] = [
            {
                "finding_id": "blocker-1",
                "stage": "functional",
                "severity": "blocking",
                "disposition": "resolved",
                "summary": "A blocking defect, allegedly fixed.",
                "evidence_refs": [],
            }
        ]
        with pytest.raises(ValidationError, match="evidence reference"):
            ReviewResult.model_validate(body)

    def test_non_blocking_findings_do_not_block(self):
        result = ReviewResult.model_validate(APPROVE_DOC)
        assert result.findings and result.findings[0].severity is FindingSeverity.MINOR
        assert result.approval_blockers() == ()

    def test_only_resolved_clears_a_finding(self):
        assert CLEARED_DISPOSITIONS == {FindingDisposition.RESOLVED}

    def test_only_completed_concludes_a_stage(self):
        assert CONCLUSIVE_STAGE_OUTCOMES == {StageOutcome.COMPLETED}


class TestStaleHeadInvalidation:
    """A head change invalidates dispositions and head-bound test evidence.

    What survives is the observation that a finding was seen. What does not survive
    is the claim that it was fixed, because that claim was about specific code.
    """

    NEW_HEAD = "0ce60583ab1cf4e2d7b95a8e6c1f40d3b7a2e591"

    def test_same_head_returns_result_unchanged(self):
        result = ReviewResult.model_validate(APPROVE_DOC)
        assert invalidate_for_head(result, result.subject.reviewed_head_sha) is result

    def test_moved_head_makes_verdict_incomplete(self):
        result = ReviewResult.model_validate(APPROVE_DOC)
        stale = invalidate_for_head(result, self.NEW_HEAD)
        assert stale.verdict is ReviewVerdict.INCOMPLETE
        assert stale.approval_blockers(), (
            "an invalidated result must never be approval-capable"
        )

    def test_moved_head_marks_dispositions_stale(self):
        body = dict(APPROVE_DOC)
        body["findings"] = [
            {
                "finding_id": "blocker-1",
                "stage": "functional",
                "severity": "blocking",
                "disposition": "resolved",
                "summary": "Allegedly fixed at the old head.",
                "evidence_refs": [
                    {"kind": "test-run", "ref": "check-run:1", "head_bound": True}
                ],
            }
        ]
        stale = invalidate_for_head(ReviewResult.model_validate(body), self.NEW_HEAD)
        assert stale.findings[0].disposition is FindingDisposition.STALE_HEAD
        assert stale.findings[0].evidence_refs[0].stale is True
        assert stale.blocking_findings, (
            "a 'resolved' claim about old code must stop clearing the finding"
        )

    def test_moved_head_marks_head_bound_evidence_stale_only(self):
        result = ReviewResult.model_validate(APPROVE_DOC)
        stale = invalidate_for_head(result, self.NEW_HEAD)
        by_kind = {ref.kind: ref for ref in stale.evidence_refs}
        assert by_kind["test-run"].stale is True, (
            "a test run against the old commit is no longer evidence"
        )
        assert by_kind["artifact"].stale is False, (
            "a static document is not invalidated by a head change"
        )

    def test_moved_head_retains_the_inspected_commit(self):
        """The artifact records what was actually read.

        Re-pointing `reviewed_head_sha` at the new head would manufacture exactly
        the wrong-revision claim this contract exists to detect.
        """
        result = ReviewResult.model_validate(APPROVE_DOC)
        stale = invalidate_for_head(result, self.NEW_HEAD)
        assert stale.subject.reviewed_head_sha == result.subject.reviewed_head_sha

    def test_moved_head_downgrades_a_published_verdict(self):
        result = ReviewResult.model_validate(APPROVE_DOC)
        stale = invalidate_for_head(result, self.NEW_HEAD)
        assert stale.publication.outcome is PublicationOutcome.FAILED
        assert "invalidated" in (stale.publication.detail or "")

    def test_malformed_head_is_refused(self):
        result = ReviewResult.model_validate(APPROVE_DOC)
        with pytest.raises(ValueError, match="40 or 64"):
            invalidate_for_head(result, "not-a-sha")


class TestApprovalBlockersReportsEveryReason:
    def test_blockers_are_reasons_not_a_boolean(self):
        """Returning reasons means a caller that logs the answer keeps the why."""
        result = ReviewResult.model_validate(SECURITY_ONLY_DOC)
        blockers = result.approval_blockers()
        assert isinstance(blockers, tuple)
        assert all(isinstance(reason, str) and reason for reason in blockers)

    def test_multiple_independent_reasons_are_all_reported(self):
        result = ReviewResult.model_validate(SECURITY_ONLY_DOC)
        blockers = result.approval_blockers()
        assert len(blockers) >= 3, (
            f"expected functional/blocker/publication reasons, got {blockers}"
        )

    def test_supported_approval_has_no_blockers(self):
        assert ReviewResult.model_validate(APPROVE_DOC).approval_blockers() == ()


class TestCompletedReviewSurvivesAFailedPublication:
    """A concluded functional verdict and a failed publication are both preserved.

    The reported sequence: functional review completes, verdict is `approve`, zero
    blocking findings, then the formal publication returns HTTP 401. Previously
    `_approve_requires_conclusive_review` gated on `approval_blockers()`, which
    includes publication, so this document did **not validate**. A producer holding
    it had exactly two options — drop the artifact, or relabel the verdict
    `incomplete` — and both destroy the record: the first loses the review entirely,
    the second misreports what the reviewer concluded and hides the 401 that an
    operator has to act on.

    So these tests assert both halves at once, because either alone is a defect:
    the verdict is preserved as `approve`, AND the result is still not
    approval-capable. Granting approval here would be the worse bug, and
    `test_it_is_not_approval_capable` exists to fail if a future change to the
    split makes an unpublished approval look clean.
    """

    def test_the_document_validates(self):
        ReviewResult.model_validate(PUBLICATION_FAILED_DOC)

    def test_the_functional_verdict_is_preserved(self):
        result = ReviewResult.model_validate(PUBLICATION_FAILED_DOC)
        assert result.verdict is ReviewVerdict.APPROVE
        assert result.review_blockers() == (), (
            "the reviewing work concluded cleanly; only publication failed"
        )

    def test_the_publication_failure_is_preserved(self):
        result = ReviewResult.model_validate(PUBLICATION_FAILED_DOC)
        assert result.publication.outcome is PublicationOutcome.FAILED
        assert result.publication.published_head_sha is None, (
            "nothing was published, so no commit may be named as published"
        )
        blockers = result.publication_blockers()
        assert len(blockers) == 1
        assert "401" in blockers[0], (
            f"the operator-actionable cause must survive into the reason: {blockers}"
        )

    def test_it_is_not_approval_capable(self):
        """The preservation must not become a grant of approval."""
        result = ReviewResult.model_validate(PUBLICATION_FAILED_DOC)
        assert result.approval_blockers(), (
            "an unpublished approval must still carry a blocker; repository rules "
            "cannot read a verdict that was never recorded"
        )

    def test_the_two_blocker_questions_are_separable(self):
        """ "Approved but unpublished" must be distinguishable from "found a blocker".

        They need opposite responses — retry the publication, versus write new code —
        so a consumer that cannot tell them apart cannot act correctly on either.
        """
        unpublished = ReviewResult.model_validate(PUBLICATION_FAILED_DOC)
        found_blocker = ReviewResult.model_validate(REFUSED_DOC)
        assert unpublished.review_blockers() == ()
        assert unpublished.publication_blockers() != ()
        assert found_blocker.review_blockers() != ()

    def test_approval_blockers_is_still_the_whole_answer(self):
        """The split must not let a reason go missing from the composed list."""
        for doc in ACCEPTED.values():
            result = ReviewResult.model_validate(doc)
            assert set(result.approval_blockers()) == set(
                result.review_blockers()
            ) | set(result.publication_blockers())

    def test_a_retry_at_the_same_head_is_a_distinct_result(self):
        """Republishing later is a new observation, not an edit of this one.

        Pins the fixture property the ledger relies on: the retry carries its own
        `result_id`, so deduplicating on head alone would collapse the successful
        publication into the failed one. That is the observed CONFLICT /
        `action_already_settled`, covered in the gateway's Postgres suite.
        """
        failed = ReviewResult.model_validate(PUBLICATION_FAILED_DOC)
        body = json.loads(json.dumps(PUBLICATION_FAILED_DOC))
        body["result_id"] = f"{failed.result_id}-retry"
        body["publication"] = {
            "outcome": "published",
            "published_head_sha": failed.subject.reviewed_head_sha,
            "reference": "pullrequestreview-5229910599",
            "detail": None,
        }
        retried = ReviewResult.model_validate(body)
        assert retried.result_id != failed.result_id
        assert retried.subject.reviewed_head_sha == failed.subject.reviewed_head_sha
        assert retried.approval_blockers() == ()

    def test_an_unexplained_failure_is_still_refused(self):
        """Relaxing the approve gate must not also relax "explain yourself"."""
        body = json.loads(json.dumps(PUBLICATION_FAILED_DOC))
        body["publication"] = {"outcome": "failed", "detail": "  "}
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(body)

    def test_approve_over_an_open_blocker_is_still_refused(self):
        """The narrowed gate must still refuse the finding-level defects."""
        body = json.loads(json.dumps(PUBLICATION_FAILED_DOC))
        body["findings"] = [
            {
                "finding_id": "still-blocking",
                "stage": "functional",
                "severity": FindingSeverity.BLOCKING.value,
                "disposition": "open",
                "summary": "An open blocking finding, alongside a failed publication.",
                "evidence_refs": [],
            }
        ]
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(body)

    def test_approve_over_a_never_attempted_publication_is_still_refused(self):
        """The line is attempted-and-failed versus never-attempted.

        This is the boundary the relaxation had to respect. `not-attempted` is the
        original defect — stages complete, exit 0, provider never called — and no
        attempt exists whose outcome could support an approval claim. If this test
        fails, the fix for the 401 case has swallowed the defect it was built beside.
        """
        body = json.loads(json.dumps(PUBLICATION_FAILED_DOC))
        body["publication"] = {
            "outcome": PublicationOutcome.NOT_ATTEMPTED.value,
            "detail": "the run ended before attempting a formal verdict",
        }
        with pytest.raises(ValidationError, match="never attempted|silent-skip"):
            ReviewResult.model_validate(body)

    def test_a_provider_refusal_is_also_preserved(self):
        """`refused` is an answer from the provider, so it is attempted too.

        Included because the reviewer's conclusion is just as real when GitHub
        declines to record it as when the call 401s, and losing it costs the same.
        """
        body = json.loads(json.dumps(PUBLICATION_FAILED_DOC))
        body["publication"] = {
            "outcome": PublicationOutcome.REFUSED.value,
            "detail": "the provider declined to record the review",
        }
        result = ReviewResult.model_validate(body)
        assert result.verdict is ReviewVerdict.APPROVE
        assert result.approval_blockers(), "still not approval-capable"

    def test_approve_with_an_unconcluded_functional_stage_is_still_refused(self):
        body = json.loads(json.dumps(PUBLICATION_FAILED_DOC))
        body["stages"] = [
            {
                "name": ReviewStageName.FUNCTIONAL.value,
                "outcome": StageOutcome.NOT_RUN.value,
                "detail": "the run ended before functional review",
            }
        ]
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(body)


# ---------------------------------------------------------------------------
# Strict wire types on exact-identity integers
# ---------------------------------------------------------------------------


#: Every protocol/identity integer, with the object path to reach it on a
#: validated result. Parametrized rather than spelled out per field so a new
#: identity integer added to the contract without strictness shows up here as a
#: missing entry rather than as an untested coercion.
WIRE_INT_FIELDS = [
    ("version", ("version",)),
    ("scope.cycle", ("scope", "cycle")),
    ("authority.claim_generation", ("authority", "claim_generation")),
    ("authority.accepted_plan_version", ("authority", "accepted_plan_version")),
    ("repository.provider_repository_id", ("repository", "provider_repository_id")),
    ("subject.pr_number", ("subject", "pr_number")),
]

#: `true`/`false` are the reported defect: lax `int` coerces them to 1/0. The
#: string and float forms are the same class of silent repair.
NON_INTEGRAL_VALUES = [True, False, "1", 1.0, "  1  ", None]


def _patch_path(
    base: dict[str, Any], path: tuple[str, ...], value: Any
) -> dict[str, Any]:
    """Deep-copy `base` with `path` replaced, then round-trip it through JSON.

    The JSON round trip is deliberate and is what the reproducer requires: the
    consumer must refuse the *serialized* document. Validating a hand-built dict
    could pass a Python `bool` through a path that never parses real wire bytes.
    """
    body = json.loads(json.dumps(base))
    cursor = body
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
    return json.loads(json.dumps(body))


class TestExactIdentityIntegersRejectCoercion:
    """Protocol/identity integers must refuse bool, string and non-integral input.

    Reproducer: starting from `accepted_result_approve` and independently
    replacing `version`, `scope.cycle`, `authority.claim_generation` or
    `repository.provider_repository_id` with JSON `true`, lax `int` validated all
    four and coerced them to `1`. A `claim_generation` of `true` became the
    generation-1 fence; a `provider_repository_id` of `true` became repository 1.

    These are exact-identity comparisons, so a wrong-but-plausible `1` binds
    evidence to the wrong repository or presents a fence nobody issued. The same
    defect was repaired for receipt versions in #5144.
    """

    @pytest.mark.parametrize("value", NON_INTEGRAL_VALUES)
    @pytest.mark.parametrize(
        "name,path", WIRE_INT_FIELDS, ids=[f[0] for f in WIRE_INT_FIELDS]
    )
    def test_non_integral_value_is_rejected(
        self, name: str, path: tuple[str, ...], value: Any
    ):
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(_patch_path(APPROVE_DOC, path, value))

    @pytest.mark.parametrize(
        "name,path", WIRE_INT_FIELDS, ids=[f[0] for f in WIRE_INT_FIELDS]
    )
    def test_rejection_is_not_a_silent_coercion(self, name: str, path: tuple[str, ...]):
        """Pin the actual defect: `true` must not arrive as `1`.

        Asserting only "raises" would still pass if a later refactor moved the
        check into an after-validator that receives an already-coerced integer and
        happens to reject it for an unrelated reason. This asserts the value never
        becomes 1 in the first place.
        """
        try:
            result = ReviewResult.model_validate(_patch_path(APPROVE_DOC, path, True))
        except ValidationError:
            return
        observed = result
        for part in path:
            observed = getattr(observed, part)
        pytest.fail(f"{name} accepted JSON true and coerced it to {observed!r}")

    @pytest.mark.parametrize(
        "name,path", WIRE_INT_FIELDS, ids=[f[0] for f in WIRE_INT_FIELDS]
    )
    def test_genuine_integer_is_still_accepted(self, name: str, path: tuple[str, ...]):
        """Strictness must not break the valid producer.

        `version` is pinned to the contract version and `accepted_plan_version`
        legitimately allows 0, so each field is exercised with a value its own
        range permits rather than one shared number.
        """
        value = (
            1
            if name == "version"
            else (0 if name.endswith("accepted_plan_version") else 7)
        )
        result = ReviewResult.model_validate(_patch_path(APPROVE_DOC, path, value))
        observed = result
        for part in path:
            observed = getattr(observed, part)
        assert observed == value
        assert type(observed) is int

    def test_every_contract_integer_is_covered(self):
        """Fail when a new identity integer is added without a strictness case.

        Introspects the models instead of trusting this list to stay current: the
        gap this contract had was an unstrict field nobody thought to test.
        """
        from models import (
            ContractEnvelope,
            ReviewAuthority,
            ReviewRepository,
            ReviewScope,
            ReviewSubject,
        )

        covered = {name for name, _ in WIRE_INT_FIELDS}
        for prefix, model in (
            ("", ContractEnvelope),
            ("scope", ReviewScope),
            ("authority", ReviewAuthority),
            ("repository", ReviewRepository),
            ("subject", ReviewSubject),
        ):
            for field_name, info in model.model_fields.items():
                if info.annotation is int:
                    qualified = f"{prefix}.{field_name}" if prefix else field_name
                    assert qualified in covered, (
                        f"{qualified} is a lax `int` on the wire: it will coerce JSON true to 1. "
                        "Type it as WireInt and add it to WIRE_INT_FIELDS."
                    )
