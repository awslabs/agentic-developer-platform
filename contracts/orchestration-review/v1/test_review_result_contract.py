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

from models import (  # noqa: E402  (path shim above must run first)
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
        assert GOLDEN_PATH.is_file(), f"golden contract fixture not found at {GOLDEN_PATH}"

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
            "destructive-apply-gate-fails-open" in reason for reason in result.approval_blockers()
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
                "evidence_refs": [{"kind": "test-run", "ref": "check-run:1", "head_bound": True}],
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
        assert stale.approval_blockers(), "an invalidated result must never be approval-capable"

    def test_moved_head_marks_dispositions_stale(self):
        body = dict(APPROVE_DOC)
        body["findings"] = [
            {
                "finding_id": "blocker-1",
                "stage": "functional",
                "severity": "blocking",
                "disposition": "resolved",
                "summary": "Allegedly fixed at the old head.",
                "evidence_refs": [{"kind": "test-run", "ref": "check-run:1", "head_bound": True}],
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
